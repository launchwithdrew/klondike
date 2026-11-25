"""Live recorder — for streamers whose stream is NOT archived as a VOD.

Some big streamers are live on Twitch with viewers clipping them, but their
broadcast is not saved as a public VOD, so the VOD-based engine cannot reach
them. This module records the public live stream as it airs into a rolling
buffer on disk (default 2 h), with the wall-clock time of every 2 s segment
(Twitch's #EXT-X-PROGRAM-DATE-TIME), and skips Twitch's stitched ads.

Then `live_clips` reads the viewer clips made during the buffer (their
creation time is public even with no VOD) and cuts each one from OUR
recording: a viewer clip covers the ~N seconds before the viewer pressed the
button, and viewers watch a few seconds behind live, so the moment sits in
[created_at - duration - delay, created_at - delay + 2].

Recording goes FORWARD only: what aired before the recorder started is gone.
Run it on a machine that stays up, one process per streamer.
Public playback only, no sign-in. Only creators who allow clipping.
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import subprocess
import os
import re
import threading
import time
import urllib.parse

from . import twitch as T
from . import untrusted as UT

SEG_RE = re.compile(r"#EXT-X-PROGRAM-DATE-TIME:(\S+)\s*#EXTINF:([\d.]+),([^\n]*)\n(\S+)")


def _epoch(iso: str) -> float:
    return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def live_variant(login: str, quality: str = "best", tries: int = 6, rank: int = 0) -> str:
    """URL of a live media playlist we can actually reach (hosts rotate;
    retry with a fresh token until one is reachable)."""
    last = None
    for _ in range(tries):
        tok = T.gql(T._TOKEN_QUERY, {"isLive": True, "login": login.lower(), "isVod": False,
                                     "vodID": "", "playerType": "site"}, playback=True).get("streamPlaybackAccessToken")
        if not tok or not tok.get("value"):
            raise RuntimeError(f"{login} is not live")
        qs = urllib.parse.urlencode({"allow_source": "true", "allow_audio_only": "true",
                                     "player": "twitchweb", "fast_bread": "true",
                                     "token": tok["value"], "sig": tok["signature"]})
        master = T.http(f"https://usher.ttvnw.net/api/channel/hls/{login.lower()}.m3u8?{qs}").decode()
        lines = master.splitlines()
        variants = []
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF:") and i + 1 < len(lines):
                bw = int((re.search(r"BANDWIDTH=(\d+)", line) or [0, 0])[1])
                url = lines[i + 1].strip()
                if "audio_only" not in line:
                    variants.append((bw, url))
        variants.sort(reverse=True)
        url = variants[min(rank, len(variants) - 1)][1] if quality == "best" else variants[-1][1]
        try:
            pl = T.http(url, attempts=1).decode("utf-8", "replace")
            seg = next((m.group(4) for m in SEG_RE.finditer(pl) if "live" in m.group(3).lower()), None)
            if seg:                         # segments can come from a different host than the playlist
                T.http(seg, attempts=1, timeout=15)
            return url
        except Exception as exc:           # host not reachable from here: new token, new host
            last = exc
            time.sleep(1)
    raise RuntimeError(f"no reachable live playlist for {login}: {last}")


def is_youtube(source: str) -> bool:
    return source.startswith(("yt:", "https://www.youtube.com/", "https://youtube.com/", "https://youtu.be/"))


def youtube_variant(source: str) -> str:
    """Live HLS media playlist for a YouTube live stream ("yt:@handle", a channel or a watch URL)."""
    from . import youtube as YT
    return YT.variant(source)


def _youtube_variant_ytdlp_only(source: str) -> str:
    import yt_dlp
    url = source[3:] if source.startswith("yt:") else source
    if url.startswith("@"):
        url = f"https://www.youtube.com/{url}/live"
    opts = {"quiet": True, "no_warnings": True, "format": "best[protocol^=m3u8]/best", "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info.get("is_live"):
        raise RuntimeError(f"{source} is not live")
    hls = info.get("url") or next((f["url"] for f in info.get("formats", []) if "m3u8" in (f.get("protocol") or "")), None)
    if not hls:
        raise RuntimeError(f"no live HLS playlist for {source}")
    return hls


def source_name(source: str) -> str:
    """A short name for files and logs: Twitch login, the YouTube handle, or kick-<slug>."""
    if source.startswith("kick:"):
        return "kick-" + re.sub(r"[^a-z0-9_-]", "", source[5:].lower())
    if not is_youtube(source):
        return source.lower()
    m = re.search(r"@([A-Za-z0-9_.-]+)", source)
    return ("yt-" + (m.group(1) if m else re.sub(r"\W+", "", source)[-11:])).lower()


_SEG_ANY = re.compile(r"#EXTINF:([\d.]+),[^\n]*\n(\S+)")
_SEQ_RE = re.compile(r"#EXT-X-MEDIA-SEQUENCE:(\d+)")


def media_sequence(playlist: str) -> tuple[int | None, int]:
    """(first media-sequence number, number of segments) of an HLS media playlist; (None, n) without the tag."""
    m = _SEQ_RE.search(playlist)
    return (int(m.group(1)) if m else None), len(_SEG_ANY.findall(playlist))


def slipped(last_seq: int | None, first_seq: int | None) -> int:
    """Segments that left the playlist between two reads: the next one we needed (last_seq + 1) is already gone."""
    if last_seq is None or first_seq is None:
        return 0
    return max(0, first_seq - last_seq - 1)
_MAP_RE = re.compile(r'#EXT-X-MAP:URI="([^"]+)"')


def init_segment(playlist: str, playlist_url: str, cache: dict) -> bytes:
    """fMP4 streams (#EXT-X-MAP) put the codec setup in one init segment; every media segment needs it
    in front to be playable on its own. Returns b"" for plain MPEG-TS playlists."""
    m = _MAP_RE.search(playlist)
    if not m:
        return b""
    uri = urllib.parse.urljoin(playlist_url, m.group(1))
    if uri not in cache:
        cache.clear()                                   # a new playlist session brings a new init
        cache[uri] = T.http(uri, attempts=3, timeout=20)
    return cache[uri]


SEG_WORKERS = 4        # segments fetched at once: one at a time, a new connection each, couldn't keep up (24 Nov)
STEP_DOWN_AFTER = 3    # loops in a row that lost segments before the recorder drops one rendition
MAX_RANK = 2           # source -> 720p60 -> 720p30 at most: a smaller picture beats a recording full of holes


def _fetch_all(urls: list[str]) -> list:
    """Download segments in parallel, results in order: bytes, or the Exception that download raised."""
    def one(u):
        try:
            return T.http(u, attempts=2, timeout=20)
        except Exception as exc:
            return exc
    if len(urls) <= 1:
        return [one(u) for u in urls]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(SEG_WORKERS, len(urls))) as ex:
        return list(ex.map(one, urls))


def record(login: str, out_dir: str, buffer_s: float = 7200, stop_after_s: float | None = None) -> None:
    """Record a live channel into out_dir/segments/<epoch_ms>.ts + index.jsonl.
    Keeps the newest `buffer_s` seconds. Skips stitched ads (Twitch).

    `login` is a Twitch login, or a YouTube source ("yt:@handle", channel or watch URL): YouTube
    has no stitched ads, and when its playlist carries no PROGRAM-DATE-TIME the segments are
    timed by arrival."""
    youtube = is_youtube(login)
    kick = login.startswith("kick:")
    if kick:
        from . import kick as K
        resolve = lambda: K.variant(login)
    else:
        resolve = (lambda: youtube_variant(login)) if youtube else \
            (lambda: live_variant(login, rank=rank[0]) if rank[0] else live_variant(login))
    rank = [0]                           # which rendition (0 = source); drops a step when we keep falling behind
    seg_dir = os.path.join(out_dir, "segments")
    os.makedirs(seg_dir, exist_ok=True)
    index = os.path.join(out_dir, "index.jsonl")
    json.dump({"login": source_name(login), "source": login, "started": time.time()},
              open(os.path.join(out_dir, "recording.json"), "w"))
    seen: dict[str, None] = {}           # insertion-ordered: trimming keeps the NEWEST urls
    failed_hosts: set[str] = set()
    inits: dict[str, bytes] = {}
    from . import autopilot as AP
    prev_end, prev_wall = _index_end(index)
    # what happened since the last saved segment — names the cause of a gap (ad break, reconnect, ...)
    since = {"fresh": True, "ads": 0, "reconnects": 0, "failed": 0, "behind": 0, "disk": 0}
    url = resolve()
    last_seq: int | None = None          # the newest media-sequence number we have read (ads included)
    seq_known = False
    loops: list[float] = []              # each loop's duration over the last minute (fetch + downloads + writes)
    slow_loop_s, fetched_at = 0.0, None
    behind_streak = 0
    stop_prune = _start_pruner(out_dir, seg_dir, buffer_s)   # its own thread: disk work never delays a fetch
    t_start, n_ads, n_segs = time.time(), 0, 0
    t_resolved = time.time()
    print(f"[live] recording {login} -> {out_dir} (buffer {buffer_s / 3600:.1f} h)", flush=True)
    last_beat = time.time()
    while stop_after_s is None or time.time() - t_start < stop_after_s:
        if time.time() - last_beat > 60:
            slow = max(loops, default=0.0)
            print(f"[live] {dt.datetime.now():%H:%M:%S} {login}: {n_segs} segments "
                  f"({n_segs * 2 / 60:.1f} min recorded), {n_ads} ad segments skipped, slowest loop {slow:.1f} s", flush=True)
            _write_loop_stats(out_dir, loops, rank[0])
            loops, last_beat = [], time.time()
        loop_t0 = time.time()
        try:
            pl = T.http(url, attempts=2, timeout=15).decode()
        except Exception as exc:
            print(f"[live] playlist fetch failed ({str(exc)[:80]}) — re-resolving", flush=True)
            since["reconnects"] += 1
            last_seq = None                              # a new playlist session numbers its own segments
            try:
                url, t_resolved = resolve(), time.time()
            except Exception as exc:
                print(f"[live] {exc} — retrying in 30 s", flush=True)
                time.sleep(30)
            continue
        if youtube and _yt_refresh(url, t_resolved):     # before the playlist URL expires (it says when)
            print("[live] YouTube playlist URL expires soon — refreshing it", flush=True)
            try:
                url, t_resolved = resolve(), time.time()
                last_seq = None
            except Exception:
                pass
        first_seq, n_in_pl = media_sequence(pl)
        seq_known = first_seq is not None
        lost = slipped(last_seq, first_seq)
        if lost:
            since["behind"] += lost
            slow_loop_s = max(slow_loop_s, (time.time() - fetched_at) if fetched_at else 0.0)
        behind_streak = behind_streak + 1 if lost else 0
        if behind_streak >= STEP_DOWN_AFTER and not youtube and not kick and rank[0] < MAX_RANK:
            rank[0] += 1
            behind_streak = 0
            print(f"[live] {login}: fell behind the playlist {STEP_DOWN_AFTER} loops in a row — "
                  f"recording one rendition lower (step {rank[0]})", flush=True)
            try:
                url, t_resolved = resolve(), time.time()
                last_seq = None
                since["reconnects"] += 1
                continue
            except Exception as exc:
                print(f"[live] couldn't switch rendition ({str(exc)[:80]}) — staying", flush=True)
        if first_seq is not None:
            last_seq = max(last_seq or 0, first_seq + n_in_pl - 1)
        fetched_at = time.time()
        ad_ranges = "stitched-ad" in pl or "twitch-stitched-ad" in pl
        try:
            init = init_segment(pl, url, inits)
        except Exception as exc:
            print(f"[live] init segment fetch failed ({str(exc)[:80]}) — retrying", flush=True)
            time.sleep(2.0)
            continue
        segs = [(m.group(1), float(m.group(2)), m.group(3), m.group(4)) for m in SEG_RE.finditer(pl)]
        if (youtube or kick) and not segs:             # no PROGRAM-DATE-TIME: time new segments by arrival
            fresh = [(None, float(m.group(1)), "live", m.group(2)) for m in _SEG_ANY.finditer(pl)
                     if m.group(2) not in seen]
            t_next = time.time() - sum(d for _, d, _, _ in fresh)
            for i, (_, d, ti, u) in enumerate(fresh):
                segs.append((t_next, d, ti, u))
                t_next += d
        todo, ads_run = [], 0
        for when, dur, title, seg_url in segs:
            if seg_url.startswith("/") or not seg_url.startswith("http"):
                seg_url = urllib.parse.urljoin(url, seg_url)
            if seg_url in seen:
                continue
            seen[seg_url] = None
            if not youtube and not kick and "live" not in title.lower():   # Twitch titles content "live"; ads "Amazon" etc.
                n_ads += 1
                ads_run += 1                      # belongs to the next live segment: it names the hole before it
                continue
            todo.append((when if isinstance(when, (int, float)) else _epoch(when), dur, seg_url, ads_run))
            ads_run = 0
        since["ads"] += ads_run                   # ads at the playlist's end: the next loop's first segment
        for (t0, dur, seg_url, ads_before), data in zip(todo, _fetch_all([u for _, _, u, _ in todo])):
            since["ads"] += ads_before
            path = os.path.join(seg_dir, f"{int(t0 * 1000)}.ts")
            if isinstance(data, Exception):       # download first: a failure must not leave an empty .ts behind
                exc = data
                host = seg_url.split("/")[2]
                since["failed"] += 1
                if host not in failed_hosts:
                    failed_hosts.add(host)
                    print(f"[live] segment download failed from {host} ({str(exc)[:100]}) — "
                          "if this repeats, that host is blocked from this machine", flush=True)
                continue
            if not _write_or_clear(path, init + data):    # fMP4: each saved segment carries its init
                since["disk"] += 1                        # the disk is full even after clearing: a hole, named
                continue
            with open(index, "a") as fh:
                if prev_end is not None and t0 > prev_end + AP.GAP_S:
                    # the hole, written down with its likely cause: a cut across it says why, not "pruned"
                    cause = AP.gap_cause(since["fresh"], since["ads"], since["reconnects"], since["failed"],
                                         prev_age_s=time.time() - (prev_wall or prev_end),
                                         behind=since["behind"], seq_known=seq_known, disk=since["disk"])
                    hole = {"t": round(prev_end, 3), "d": round(t0 - prev_end, 3), "gap": cause}
                    if since["behind"]:
                        hole.update(skipped=since["behind"], loop_s=round(slow_loop_s, 1))
                    fh.write(json.dumps(hole) + "\n")
                fh.write(json.dumps({"t": t0, "d": dur, "f": os.path.basename(path)}) + "\n")
            if prev_end is None or t0 + dur > prev_end:
                prev_end, prev_wall = t0 + dur, time.time()
            since = {"fresh": False, "ads": 0, "reconnects": 0, "failed": 0, "behind": 0, "disk": 0}
            slow_loop_s = 0.0
            n_segs += 1
        loops.append(time.time() - loop_t0)
        if n_segs and n_segs % 150 == 0:
            print(f"[live] {login}: {n_segs} segments ({n_segs * 2 / 60:.0f} min), {n_ads} ad segments skipped"
                  f"{' (ad break now)' if ad_ranges else ''}", flush=True)
        if len(seen) > 5000:
            seen = dict.fromkeys(list(seen)[-2000:])
        time.sleep(2.0)
    stop_prune.set()


def _write_or_clear(path: str, data: bytes) -> bool:
    """Save a segment. A full disk or quota (ENOSPC / EDQUOT) never stops the recorder: it learns the quota,
    clears the oldest recording no queued draft needs, and tries once more. False = still full (skip it)."""
    from . import disk as DK
    for attempt in (0, 1):
        try:
            with open(path, "wb") as fh:
                fh.write(data)
            return True
        except OSError as exc:
            try:
                os.remove(path)                        # never leave a partial segment behind
            except OSError:
                pass
            if not DK.learn_full(exc):
                raise
            if attempt == 0:
                n, gb = DK.prune_oldest(2.0, root=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(path)))))
                print(f"[live] disk full ({os.strerror(exc.errno)}) — cleared the oldest {gb:.1f} GB "
                      f"({n} segments), retrying", flush=True)
    return False


PRUNE_EVERY_S = 30.0
_PRUNERS: dict[str, "threading.Event"] = {}


def _start_pruner(out_dir: str, seg_dir: str, buffer_s: float) -> "threading.Event":
    """One pruning thread per recording folder: a restarted record() (it can raise) stops the previous one."""
    old = _PRUNERS.get(out_dir)
    if old is not None:
        old.set()
    stop = threading.Event()
    _PRUNERS[out_dir] = stop
    threading.Thread(target=_prune_loop, args=(out_dir, seg_dir, buffer_s, stop), daemon=True).start()
    return stop


def _prune_loop(out_dir: str, seg_dir: str, buffer_s: float, stop: "threading.Event") -> None:
    """The buffer's pruning, every 30 s on its own thread (it lists the whole segment folder and re-reads the
    queue's protected windows: on a busy host that took seconds inside the fetch loop, and segments slid off)."""
    from . import autopilot as AP
    protect, protect_at = [], 0.0
    while not stop.wait(PRUNE_EVERY_S):
        try:
            if time.time() - protect_at > 60:          # windows a queued draft still needs: never pruned
                protect, protect_at = AP.protected_windows(out_dir), time.time()
            prune_buffer(seg_dir, buffer_s, protect)
        except Exception as exc:
            print(f"[live] prune failed ({str(exc)[:80]})", flush=True)


def _write_loop_stats(out_dir: str, loops: list[float], rendition: int = 0) -> None:
    """The last minute's loop timings, for `sentinels` / the funnel: slow loops are how we fall behind."""
    if not loops:
        return
    try:
        srt = sorted(loops)
        json.dump({"at": round(time.time()), "loops": len(loops), "p50_s": round(srt[len(srt) // 2], 2),
                   "max_s": round(srt[-1], 2), "over_6s": sum(1 for x in loops if x > 6),
                   "rendition": rendition},
                  open(os.path.join(out_dir, "loop.json"), "w"))
    except OSError:
        pass


def prune_buffer(seg_dir: str, buffer_s: float, protect: list[tuple[float, float]], now: float | None = None) -> int:
    """Drop segments older than the buffer, except any a queued draft still needs (its window + transcribed handles).
    Returns how many were removed."""
    from . import autopilot as AP
    cutoff = (time.time() if now is None else now) - buffer_s
    durs = AP.segment_durations(os.path.dirname(seg_dir)) if protect else {}
    gone = 0
    for f in glob.glob(os.path.join(seg_dir, "*.ts")):
        try:
            t_f = int(os.path.basename(f)[:-3]) / 1000
        except ValueError:
            continue
        # a segment's real length: a long one starting just before a protected window still holds its first seconds
        if t_f < cutoff and not AP.is_protected(t_f, durs.get(os.path.basename(f), 2.0), protect):
            os.remove(f)
            gone += 1
    return gone


def _yt_refresh(url: str, resolved_at: float) -> bool:
    from . import youtube as YT
    return YT.needs_refresh(url, resolved_at)


def record_supervised(source: str, out_dir: str, buffer_s: float) -> None:
    """record() forever: the stream can drop, go offline, or land on a host this machine can't reach."""
    delay = 15
    while True:
        try:
            record(source, out_dir, buffer_s)
            delay = 15
        except Exception as exc:
            print(f"[live] recorder stopped ({str(exc)[:100]}) — retrying in {delay}s", flush=True)
        time.sleep(delay)
        delay = min(300, delay * 2)


def _index_end(index: str) -> tuple[float | None, float | None]:
    """End (wall clock) of what an earlier run already recorded, and when that file was last written."""
    try:
        last = None
        for line in open(index):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if "f" in r and (last is None or r["t"] + r["d"] > last):
                last = r["t"] + r["d"]
        return last, os.path.getmtime(index)
    except OSError:
        return None, None


def gaps_in(rec_dir: str, t0: float, t1: float) -> list[dict]:
    """Recorded holes overlapping [t0, t1] ({"t", "d", "gap": cause})."""
    out = []
    try:
        for line in open(os.path.join(rec_dir, "index.jsonl")):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if "gap" in r and r["t"] < t1 and r["t"] + r["d"] > t0:
                out.append(r)
    except OSError:
        pass
    return out


def why_missing(rec_dir: str, t0: float, t1: float) -> str:
    """Plain-language reason a window can't be cut in full from the recording."""
    g = gaps_in(rec_dir, t0, t1)
    if g:
        lost = sum(min(t1, x["t"] + x["d"]) - max(t0, x["t"]) for x in g)
        return (f"the recording has a {lost:.0f} s hole there — {g[0]['gap']} "
                f"({dt.datetime.fromtimestamp(g[0]['t'], dt.timezone.utc):%H:%M:%S}–"
                f"{dt.datetime.fromtimestamp(g[0]['t'] + g[0]['d'], dt.timezone.utc):%H:%M:%S} UTC)")
    try:
        rec = json.load(open(os.path.join(rec_dir, "recording.json")))
    except (OSError, ValueError):
        rec = {}
    idx = [r for r in load_index(rec_dir)] if os.path.exists(os.path.join(rec_dir, "index.jsonl")) else []
    if not idx:
        return "nothing is recorded yet (the recorder can't reach the stream — see its log)"
    if t1 < idx[0]["t"]:
        first = dt.datetime.fromtimestamp(idx[0]["t"], dt.timezone.utc)
        started = rec.get("started")
        return (f"that moment is before the oldest kept recording ({first:%H:%M:%S} UTC) — "
                + ("the buffer pruned it" if started and started < idx[0]["t"] - 60 else "the recorder wasn't running yet"))
    if t0 > idx[-1]["t"] + idx[-1]["d"]:
        return "that moment hasn't been recorded yet (or the recorder stopped)"
    return "segments in that window are missing (pruned, or a download failed)"


def load_index(rec_dir: str) -> list[dict]:
    out = []
    try:                                               # one listing, not a stat per line of a days-long index
        on_disk = set(os.listdir(os.path.join(rec_dir, "segments")))
    except OSError:
        on_disk = set()
    for line in open(os.path.join(rec_dir, "index.jsonl")):
        if '"f"' not in line:                          # a gap marker, not a segment
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if "f" not in r:
            continue
        if r["f"] in on_disk:
            out.append(r)
    out.sort(key=lambda r: r["t"])
    # A restarted recorder can save the same stretch again under slightly different timestamps
    # (Twitch's PROGRAM-DATE-TIME differs per playlist session). Keep the first copy, drop overlaps,
    # or every cut across that stretch repeats it and the timeline after it shifts.
    kept: list[dict] = []
    for r in out:
        if kept and r["t"] < kept[-1]["t"] + kept[-1]["d"] - 0.5:
            continue
        kept.append(r)
    return kept


def cut_source(rec_dir: str, t0: float, t1: float, out_ts: str) -> float:
    """Concatenate recorded segments covering wall-clock [t0, t1] into out_ts.
    Returns the wall-clock time of its first frame (pass as source_t0).

    The segments' own timestamps can jump (a recorder restart starts a new playlist session, a
    missed segment leaves a hole), and ffmpeg seeks and trims by those timestamps — so a moment
    after a jump was cut from the wrong place, or came out short and lost its payoff. The concat
    demuxer re-times every segment to its wall-clock place instead: file time == wall time − first."""
    idx = [r for r in load_index(rec_dir) if r["t"] + r["d"] > t0 - 2 and r["t"] < t1 + 2]
    if not idx:
        raise RuntimeError("that moment is not in the recording: " + why_missing(rec_dir, t0, t1))
    lst = out_ts + ".txt"
    with open(lst, "w") as fh:
        for i, r in enumerate(idx):
            path = os.path.abspath(os.path.join(rec_dir, "segments", r["f"])).replace("'", "'\\''")
            span = (idx[i + 1]["t"] - r["t"]) if i + 1 < len(idx) else r["d"]
            fh.write(f"file '{path}'\nduration {max(0.05, span):.3f}\n")
    p = subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy",
                        "-f", "mpegts", out_ts], capture_output=True, text=True)
    os.remove(lst)
    if p.returncode != 0 or not os.path.exists(out_ts) or os.path.getsize(out_ts) == 0:
        with open(out_ts, "wb") as fh:                    # fall back to raw bytes (old behaviour)
            for r in idx:
                fh.write(open(os.path.join(rec_dir, "segments", r["f"]), "rb").read())
    return idx[0]["t"]


_CLIPS_LIVE = """query($login: String!) { user(login: $login) {
  clips(first: 100, criteria: {period: LAST_DAY, sort: VIEWS_DESC}) {
    edges { node { slug title viewCount createdAt durationSeconds videoOffsetSeconds video { createdAt } } }
    pageInfo { hasNextPage } } } }"""
REPLAY_LAG_S = 600       # a clip made this long after its moment was clipped from the replay, not live


# A live-made clip is made this long after its moment ends (createdAt − the clip's end on the stream's clock),
# median of 50 audio-matched clips on 7 creators (U2, Nov 24). The spread is wide (see CLIP_LAG_RANGE_S): viewers watch a little
# behind live, open the clip editor, trim, then save. Only used when a clip has no saved-stream place.
CLIP_LAG_S = 67.0
CLIP_LAG_RANGE_S = (0.5, 155.0)   # p05, p95 of 50 audio-matched clips, 7 creators


def clip_lag(c: dict) -> float | None:
    """Seconds between a clip's moment (its end in the saved stream) and when it was made; None = no saved stream.
    31% of clips (845 measured, Nov 23) were made > 10 min after the moment: from the replay, hours or days later.
    Their createdAt says nothing about when the moment happened."""
    w = T.clip_wall(c)
    if w is None:
        return None
    try:
        return _epoch(c["createdAt"]) - w[1]
    except (KeyError, ValueError, TypeError):
        return None


def moment_time(c: dict) -> float:
    """When a clip's moment ended, wall clock: createdAt for a live-made clip, its saved-stream place for a replay clip."""
    lag = clip_lag(c)
    if lag is not None and lag > REPLAY_LAG_S:
        return _epoch(c["createdAt"]) - lag
    return _epoch(c["createdAt"])


def _raw_spans(rec_dir: str) -> list[tuple[float, float]]:
    """Every stretch this recording ever saved, pruned segments included (their index lines stay), merged
    across gaps of 10 s or less."""
    out: list[list[float]] = []
    try:
        with open(os.path.join(rec_dir, "index.jsonl")) as fh:
            for line in fh:
                if '"f"' not in line:
                    continue
                try:
                    r = json.loads(line)
                    t, d = float(r["t"]), float(r.get("d") or 2.0)
                except (ValueError, KeyError, TypeError):
                    continue
                if out and t <= out[-1][1] + 10:
                    out[-1][1] = max(out[-1][1], t + d)
                else:
                    out.append([t, t + d])
    except OSError:
        pass
    return [(a, b) for a, b in out]


def live_clips(rec_dir: str, delay_s: float | None = None) -> tuple[list[dict], list[dict], dict]:
    """Viewer clips made while we were recording: (moments, clips, info).
    moments: wall-clock windows, overlapping clips merged, most-viewed first.
    clips: every clip in the list with where it stands — "in window", "replay clip", "before the recording",
    "pruned before we saw it", "not recorded yet" — so the sentinel's event log can say what happened to each.
    info: the list itself — n clips, more pages?, its lowest view count (the cutoff a fresh clip must pass to be
    visible at all), the newest clip's age, and the recorded span."""
    meta = json.load(open(os.path.join(rec_dir, "recording.json")))
    idx = load_index(rec_dir)
    if not idx:
        return [], [], {}
    have0, have1 = idx[0]["t"], idx[-1]["t"] + idx[-1]["d"]
    spans = _raw_spans(rec_dir)                          # pruned segments still mark what we recorded
    u = T.gql(_CLIPS_LIVE, {"login": meta["login"]}).get("user") or {}
    lst = u.get("clips") or {}
    edges = lst.get("edges") or []
    ms: list[dict] = []
    clips: list[dict] = []
    for e in edges:
        c = e["node"]
        try:
            created = _epoch(c["createdAt"])
        except (KeyError, ValueError):
            continue
        w = T.clip_wall(c)
        if w is not None:                    # its place in the live saved stream: within a few s (U2)
            start, end, how = w[0] - 2, w[1] + 2, "saved-stream place"
        else:                                # live-only creator: createdAt minus the typical lag, a rough guess
            end = created - (CLIP_LAG_S if delay_s is None else delay_s) + 2
            start, how = end - (c.get("durationSeconds") or 0) - 2, "createdAt guess"
        row = {"slug": c.get("slug"), "views": int(c.get("viewCount") or 0), "created": created,
               "start": round(start, 1), "end": round(end, 1), "placed": how}
        clips.append(row)
        lag = clip_lag(c)
        if lag is not None and lag > REPLAY_LAG_S:
            row["state"] = "replay clip"     # clipped from the replay: createdAt isn't the moment (catch-up has it)
            continue
        if start < have0:
            # recorded once (pruned since) or never: the first is a buffer too short, the second a late start
            was = any(a <= start + 2 and b >= end - 2 for a, b in spans)
            row["state"] = "pruned before we saw it" if was else "before the recording"
            continue
        if end > have1:
            row["state"] = "not recorded yet"
            continue
        row["state"] = "in window"
        hit = next((m for m in ms if start <= m["end"] - 5 and end >= m["start"] + 5), None)
        if hit:
            hit["views"] += row["views"]
            hit["clips"] += 1
            hit["titles"].append(c["title"])
            hit["slugs"].append(row["slug"])
            if row["views"] > hit["best"]["views"]:
                hit["best"] = {"start": start, "end": end, "views": row["views"], "slug": row["slug"],
                               "created": created, "placed": how}
        else:
            ms.append({"start": start, "end": end, "views": row["views"], "clips": 1, "titles": [c["title"]],
                       "slugs": [row["slug"]], "best": {"start": start, "end": end, "views": row["views"],
                                                        "slug": row["slug"], "created": created, "placed": how}})
    info = {"n": len(clips), "more": bool((lst.get("pageInfo") or {}).get("hasNextPage")),
            "min_views": min((r["views"] for r in clips), default=None),
            "newest_age_s": round(time.time() - max(r["created"] for r in clips)) if clips else None,
            "rec": [round(have0), round(have1)]}
    return sorted(ms, key=lambda m: -m["views"]), clips, info


def live_moments(rec_dir: str, delay_s: float | None = None) -> list[dict]:
    """Viewer clips made while we were recording, as wall-clock windows,
    overlapping ones merged, most-viewed first."""
    return live_clips(rec_dir, delay_s)[0]


def render_live(rec_dir: str, top: int = 3, out_dir: str | None = None, model: str = "small",
                watermark: str | None = None) -> list[dict]:
    """Cut + caption the most-viewed viewer-clipped moments from the recording."""
    from . import audio as A
    from . import render as V
    out_dir = out_dir or os.path.join(rec_dir, "renders")
    os.makedirs(out_dir, exist_ok=True)
    login = json.load(open(os.path.join(rec_dir, "recording.json")))["login"]
    reports = []
    for m in live_moments(rec_dir)[:top]:
        b = m["best"]
        src = os.path.join(out_dir, f"_src_{int(b['start'])}.ts")
        first = cut_source(rec_dir, b["start"] - 4, b["end"] + 4, src)
        x = A.decode_pcm(src, 0, None)
        early = A.gate(x, first, route="live-clips", region=(b["start"], b["end"]))
        if early.observation in A.SKIP_OBS:
            os.remove(src)
            print(f"[live] skipped {int(b['start'])}: {A.skip_reason(early.observation)}", flush=True)
            continue
        tr = A.transcribe(x, model, offset=first, prompt=A.vocab_prompt(login=login))
        words = A.real_words(tr)
        s, e = b["start"], b["end"]
        inside = [w for w in words if s <= w["start"] <= e]
        if inside:                                     # trim dead air at the ends only
            s, e = max(s, inside[0]["start"] - 1.0), min(e, inside[-1]["end"] + 1.5)
        e = max(e, s + 12)
        name = f"{login}_{dt.datetime.fromtimestamp(s).strftime('%H%M%S')}"
        out = os.path.join(out_dir, name + ".mp4")
        info = V.render(src, first, s, e, words, out, crop="blur", watermark=watermark)
        os.remove(src)
        rep = {**info, "views": m["views"], "titles": m["titles"][:3], "length_s": round(e - s, 1),
               "status": "DRAFT — a person must watch it and approve before posting"}
        json.dump(rep, open(os.path.join(out_dir, name + ".report.json"), "w"), indent=2)
        print(f"[live] {out}  {e - s:.0f}s  {m['views']:,} views  {UT.quote(m['titles'][0], 50)}", flush=True)
        reports.append(rep)
    return reports


def render_window(rec_dir: str, start_ts: float, end_ts: float, out: str, title: str | None = None,
                  quote: str | None = None, layout: str = "stack", model: str | None = None,
                  watermark: str | None = None, fixes: dict | None = None,
                  extra_censor: set[str] | None = None, drop_captions: list | None = None,
                  words: list[dict] | None = None, payoff_ts: float | None = None) -> dict:
    """Render any wall-clock window of a recording (moments found by reading
    the recording itself, not only viewer clips)."""
    from . import audio as A
    from . import render as V
    import sys as _sys
    say = lambda m: print(f"[render] {m}", file=_sys.stderr, flush=True)   # stdout stays the JSON report
    src = out + ".src.ts"
    say(f"cutting {end_ts - start_ts:.0f} s from the recording…")
    first = cut_source(rec_dir, start_ts - 3, end_ts + 3, src)
    ck = out + ".words.json"                     # resume: a run killed after transcribing doesn't transcribe again
    if words is None:
        try:
            c = json.load(open(ck))
            if c.get("start") == start_ts and c.get("end") == end_ts:
                words = c["words"]
                say("resuming: reusing the transcript a killed run already made")
        except (OSError, ValueError, KeyError):
            pass
    if words is None:
        say("transcribing (the slow part: a few minutes on a CPU)…")
        if not model:
            from . import ops as O
            model = O.whisper_model()
        x = A.decode_pcm(src, 0, None)
        early = A.gate(x, first, route="publish-live", region=(start_ts, end_ts))
        if early.observation in A.SKIP_OBS:
            os.remove(src)
            raise RuntimeError(f"{A.skip_reason(early.observation)} — {start_ts:.0f}–{end_ts:.0f}")
        with open(os.path.join(rec_dir, "recording.json")) as stream:
            login = json.load(stream).get("login")
        words = A.real_words(A.transcribe(x, model, offset=first, prompt=A.vocab_prompt(login=login)))
        with open(ck + ".tmp", "w") as fh:
            json.dump({"start": start_ts, "end": end_ts, "words": words}, fh)
        os.replace(ck + ".tmp", ck)
    else:
        say("reusing the draft's transcript (no second transcription)")
        words = [dict(w) for w in words]
    say("rendering the vertical clip…")
    from . import censor as C, safety as SF
    original_speech = SF.screen_words(words, start_ts, end_ts) if fixes else None
    words = C.apply_fixes(words, fixes)          # e.g. {"kill me?": "kill him?"}
    info = V.render(src, first, start_ts, end_ts, words, out, crop="blur", layout=layout,
                    title=title, quote=quote, watermark=watermark, extra_censor=extra_censor,
                    drop_captions=drop_captions,
                    payoff_s=(payoff_ts - start_ts) if payoff_ts is not None else None)
    if original_speech and original_speech["flagged"]:
        info["speech_safety"] = original_speech
    os.remove(src)
    got = media_duration(out)
    want = end_ts - start_ts
    if got and got < want - 1.5:      # never hand over a clip that silently lost its ending
        raise RuntimeError(f"render came out {want - got:.1f} s short ({got:.1f} of {want:.1f} s) — "
                           f"{why_missing(rec_dir, start_ts, end_ts)}; pick a window inside the recording or trim "
                           "with --end")
    return info


def media_duration(path: str) -> float | None:
    try:
        return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                     path], capture_output=True, text=True).stdout.strip())
    except (ValueError, OSError):
        return None
