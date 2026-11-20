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
import time
import urllib.parse

from . import twitch as T
from . import untrusted as UT

SEG_RE = re.compile(r"#EXT-X-PROGRAM-DATE-TIME:(\S+)\s*#EXTINF:([\d.]+),([^\n]*)\n(\S+)")


def _epoch(iso: str) -> float:
    return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def live_variant(login: str, quality: str = "best", tries: int = 6) -> str:
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
        url = variants[0][1] if quality == "best" else variants[-1][1]
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
        resolve = (lambda: youtube_variant(login)) if youtube else (lambda: live_variant(login))
    seg_dir = os.path.join(out_dir, "segments")
    os.makedirs(seg_dir, exist_ok=True)
    index = os.path.join(out_dir, "index.jsonl")
    json.dump({"login": source_name(login), "source": login, "started": time.time()},
              open(os.path.join(out_dir, "recording.json"), "w"))
    seen: set[str] = set()
    failed_hosts: set[str] = set()
    inits: dict[str, bytes] = {}
    from . import autopilot as AP
    prev_end, prev_wall = _index_end(index)
    # what happened since the last saved segment — names the cause of a gap (ad break, reconnect, ...)
    since = {"fresh": True, "ads": 0, "reconnects": 0, "failed": 0}
    protect, protect_at = [], 0.0
    url = resolve()
    t_start, n_ads, n_segs = time.time(), 0, 0
    t_resolved = time.time()
    print(f"[live] recording {login} -> {out_dir} (buffer {buffer_s / 3600:.1f} h)", flush=True)
    last_beat = time.time()
    while stop_after_s is None or time.time() - t_start < stop_after_s:
        if time.time() - last_beat > 60:
            print(f"[live] {dt.datetime.now():%H:%M:%S} {login}: {n_segs} segments "
                  f"({n_segs * 2 / 60:.1f} min recorded), {n_ads} ad segments skipped", flush=True)
            last_beat = time.time()
        try:
            pl = T.http(url, attempts=2, timeout=15).decode()
        except Exception as exc:
            print(f"[live] playlist fetch failed ({str(exc)[:80]}) — re-resolving", flush=True)
            since["reconnects"] += 1
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
            except Exception:
                pass
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
        for when, dur, title, seg_url in segs:
            if seg_url.startswith("/") or not seg_url.startswith("http"):
                seg_url = urllib.parse.urljoin(url, seg_url)
            if seg_url in seen:
                continue
            seen.add(seg_url)
            if not youtube and not kick and "live" not in title.lower():   # Twitch titles content "live"; ads "Amazon" etc.
                n_ads += 1
                since["ads"] += 1
                continue
            t0 = when if isinstance(when, (int, float)) else _epoch(when)
            path = os.path.join(seg_dir, f"{int(t0 * 1000)}.ts")
            try:
                data = T.http(seg_url, attempts=2, timeout=20)
            except Exception as exc:              # download first: a failure must not leave an empty .ts behind
                host = seg_url.split("/")[2]
                since["failed"] += 1
                if host not in failed_hosts:
                    failed_hosts.add(host)
                    print(f"[live] segment download failed from {host} ({str(exc)[:100]}) — "
                          "if this repeats, that host is blocked from this machine", flush=True)
                continue
            with open(path, "wb") as fh:
                fh.write(init + data)                     # fMP4: each saved segment carries its init
            with open(index, "a") as fh:
                if prev_end is not None and t0 > prev_end + AP.GAP_S:
                    # the hole, written down with its likely cause: a cut across it says why, not "pruned"
                    cause = AP.gap_cause(since["fresh"], since["ads"], since["reconnects"], since["failed"],
                                         prev_age_s=time.time() - (prev_wall or prev_end))
                    fh.write(json.dumps({"t": round(prev_end, 3), "d": round(t0 - prev_end, 3), "gap": cause}) + "\n")
                fh.write(json.dumps({"t": t0, "d": dur, "f": os.path.basename(path)}) + "\n")
            if prev_end is None or t0 + dur > prev_end:
                prev_end, prev_wall = t0 + dur, time.time()
            since = {"fresh": False, "ads": 0, "reconnects": 0, "failed": 0}
            n_segs += 1
        # prune the buffer
        cutoff = time.time() - buffer_s
        if time.time() - protect_at > 60:              # windows a queued draft still needs: never pruned
            protect, protect_at = AP.protected_windows(out_dir), time.time()
        for f in glob.glob(os.path.join(seg_dir, "*.ts")):
            t_f = int(os.path.basename(f)[:-3]) / 1000
            if t_f < cutoff and not AP.is_protected(t_f, 2.0, protect):
                os.remove(f)
        if n_segs and n_segs % 150 == 0:
            print(f"[live] {login}: {n_segs} segments ({n_segs * 2 / 60:.0f} min), {n_ads} ad segments skipped"
                  f"{' (ad break now)' if ad_ranges else ''}", flush=True)
        if len(seen) > 5000:
            seen = set(list(seen)[-2000:])
        time.sleep(2.0)


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
    for line in open(os.path.join(rec_dir, "index.jsonl")):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if "f" not in r:                               # a gap marker, not a segment
            continue
        if os.path.isfile(os.path.join(rec_dir, "segments", r["f"])):
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
    edges { node { slug title viewCount createdAt durationSeconds } } } } }"""


def live_moments(rec_dir: str, delay_s: float = 8.0) -> list[dict]:
    """Viewer clips made while we were recording, as wall-clock windows,
    overlapping ones merged, most-viewed first."""
    meta = json.load(open(os.path.join(rec_dir, "recording.json")))
    idx = load_index(rec_dir)
    if not idx:
        return []
    have0, have1 = idx[0]["t"], idx[-1]["t"] + idx[-1]["d"]
    u = T.gql(_CLIPS_LIVE, {"login": meta["login"]}).get("user") or {}
    ms: list[dict] = []
    for e in ((u.get("clips") or {}).get("edges") or []):
        c = e["node"]
        end = _epoch(c["createdAt"]) - delay_s + 2
        start = end - c["durationSeconds"] - 2
        if start < have0 or end > have1:
            continue
        hit = next((m for m in ms if start <= m["end"] - 5 and end >= m["start"] + 5), None)
        if hit:
            hit["views"] += c["viewCount"]
            hit["clips"] += 1
            hit["titles"].append(c["title"])
            if c["viewCount"] > hit["best"]["views"]:
                hit["best"] = {"start": start, "end": end, "views": c["viewCount"]}
        else:
            ms.append({"start": start, "end": end, "views": c["viewCount"], "clips": 1,
                       "titles": [c["title"]], "best": {"start": start, "end": end, "views": c["viewCount"]}})
    return sorted(ms, key=lambda m: -m["views"])


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
                  words: list[dict] | None = None) -> dict:
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
    from . import censor as C
    words = C.apply_fixes(words, fixes)          # e.g. {"kill me?": "kill him?"}
    info = V.render(src, first, start_ts, end_ts, words, out, crop="blur", layout=layout,
                    title=title, quote=quote, watermark=watermark, extra_censor=extra_censor,
                    drop_captions=drop_captions)
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
