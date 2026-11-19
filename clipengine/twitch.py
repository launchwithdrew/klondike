"""Twitch access for the clip engine — public endpoints only, no sign-in.

Replaces three things from v1:
  * TwitchDownloaderCLI chatdownload (a .NET binary that timed out from the
    VM and could not fetch a full 35 h VOD in 600 s) -> `download_chat`,
    pure Python, parallel by time range, resumable from a cache file.
  * The sequential HLS segment loop copied into recut.py / hls_source.py /
    cut_clips.py -> `Vod.playlists` + `download_window` with a thread pool.
  * Guessing the game from the VOD title -> `Vod.chapters` / `game_at`,
    Twitch's own chapter markers ("08:xx of this VOD is one game, 09:xx another"
    was a hand-learned lesson in the training report; the VOD says so).

Same public web-player calls v1 already used (GQL + usher). Only clip
creators who allow clipping. Nothing here posts, signs in or touches an
account.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import re
import sys
import time
from http.client import HTTPException   # IncompleteRead etc. (a module-level `http` below shadows the package)
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

GQL_URL = "https://gql.twitch.tv/gql"
CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"  # public web-player client id (v1 used the same)
UA = "Mozilla/5.0"

_TOKEN_QUERY = """query PlaybackAccessToken($isLive: Boolean!, $login: String!, $isVod: Boolean!, $vodID: ID!, $playerType: String!) {
  streamPlaybackAccessToken(channelName: $login, params: {platform: "web", playerBackend: "mediaplayer", playerType: $playerType}) @include(if: $isLive) { value signature }
  videoPlaybackAccessToken(id: $vodID, params: {platform: "web", playerBackend: "mediaplayer", playerType: $playerType}) @include(if: $isVod) { value signature }
}"""

_META_QUERY = """query($id: ID!) { video(id: $id) {
  id title lengthSeconds createdAt broadcastType
  owner { login displayName }
  game { displayName }
  moments(momentRequestType: VIDEO_CHAPTER_MARKERS) { edges { node {
    positionMilliseconds durationMilliseconds description
    details { ... on GameChangeMomentDetails { game { displayName } } } } } }
} }"""

_COMMENTS_QUERY = """query($id: ID!, $t: Int) { video(id: $id) {
  comments(contentOffsetSeconds: $t) {
    edges { node { id contentOffsetSeconds commenter { login }
      message { fragments { text emote { emoteID } } } } }
    pageInfo { hasNextPage } } } }"""


def log(msg: str) -> None:
    print(f"[twitch] {msg}", file=sys.stderr, flush=True)


def parse_vod_id(value: str) -> str:
    value = str(value).strip()
    m = re.search(r"videos/(\d+)", value) or re.search(r"(\d{6,})", value)
    return m.group(1) if m else value


def http(url: str, data: bytes | None = None, headers: dict | None = None,
         timeout: float = 60, attempts: int = 4) -> bytes:
    """GET/POST with retries and exponential backoff (1s, 3s, 9s ...).

    v1 waited 15 s then 45 s on every flake; Twitch's dropped connections
    ("Remote end closed connection") almost always clear in a second or
    two, so short backoff first, longer only if it keeps failing.
    """
    hdrs = {"User-Agent": UA}
    hdrs.update(headers or {})
    last: Exception | None = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs,
                                         method="POST" if data else "GET")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (400, 401, 403, 404):  # not transient
                break
        except (urllib.error.URLError, TimeoutError, OSError, HTTPException) as e:
            last = e                            # incl. IncompleteRead: a proxy that truncates responses
        if i < attempts - 1:
            time.sleep(3 ** i)
    if isinstance(last, HTTPException) or (isinstance(last, OSError) and "IncompleteRead" in repr(last)):
        body = _curl(url, data, hdrs, timeout)   # some proxies cut Python's downloads short but not curl's
        if body is not None:
            return body
    raise RuntimeError(f"request failed after retries: {url[:100]} ({last})")


def _curl(url: str, data: bytes | None, headers: dict, timeout: float) -> bytes | None:
    import shutil
    import subprocess
    if not shutil.which("curl"):
        return None
    cmd = ["curl", "-sS", "--fail", "--compressed", "--max-time", str(int(timeout))]
    for k, v in headers.items():
        cmd += ["-H", f"{k}: {v}"]
    if data:
        cmd += ["-X", "POST", "--data-binary", "@-"]
    try:
        p = subprocess.run(cmd + [url], input=data, capture_output=True, timeout=timeout + 10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout if p.returncode == 0 and p.stdout else None


def gql(query: str, variables: dict | None = None) -> dict:
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    out = json.loads(http(GQL_URL, data=body, headers={
        "Client-ID": CLIENT_ID, "Content-Type": "application/json"}))
    if out.get("errors") and not out.get("data"):
        raise RuntimeError(f"GQL error: {str(out['errors'])[:300]}")
    return out.get("data") or {}


# --------------------------------------------------------------------------
# VOD metadata + chapters
# --------------------------------------------------------------------------
@dataclass
class Chapter:
    start_s: float
    end_s: float
    game: str


@dataclass
class Vod:
    id: str
    title: str = ""
    length_s: int = 0
    created_at: str = ""
    login: str = ""
    display_name: str = ""
    game: str = ""
    chapters: list[Chapter] = field(default_factory=list)

    @classmethod
    def fetch(cls, vod_id: str) -> "Vod":
        vod_id = parse_vod_id(vod_id)
        v = gql(_META_QUERY, {"id": vod_id}).get("video")
        if not v:
            raise RuntimeError(
                f"VOD {vod_id} not found (deleted, expired, or sub-only). "
                "Twitch keeps past broadcasts 7-60 days; this tool never signs in.")
        chapters = []
        for e in ((v.get("moments") or {}).get("edges") or []):
            n = e["node"]
            g = ((n.get("details") or {}).get("game") or {}).get("displayName") or n.get("description") or ""
            s = n["positionMilliseconds"] / 1000
            chapters.append(Chapter(s, s + n["durationMilliseconds"] / 1000, g))
        game = (v.get("game") or {}).get("displayName") or ""
        # A VOD that is still recording reports its open chapter with zero
        # (or stale) duration: e.g. a chapter of 685-685 s, so the
        # whole GTA section read as "Just Chatting" and got a centre crop
        # (facecam + HUD cut off). The last chapter runs to the end of the VOD.
        chapters.sort(key=lambda c: c.start_s)
        if chapters:
            chapters[-1].end_s = max(chapters[-1].end_s, float(v["lengthSeconds"]) + 86400)
            for a, b in zip(chapters, chapters[1:]):
                a.end_s = b.start_s
        if not chapters and game:
            chapters = [Chapter(0, float(v["lengthSeconds"]), game)]
        return cls(id=vod_id, title=v.get("title") or "", length_s=int(v["lengthSeconds"]),
                   created_at=v.get("createdAt") or "",
                   login=(v.get("owner") or {}).get("login") or "",
                   display_name=(v.get("owner") or {}).get("displayName") or "",
                   game=game, chapters=chapters)

    def game_at(self, t: float) -> str:
        """The game Twitch says was being played at VOD second t."""
        for c in self.chapters:
            if c.start_s <= t < c.end_s:
                return c.game
        return self.game

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "length_s": self.length_s,
                "created_at": self.created_at, "login": self.login,
                "display_name": self.display_name, "game": self.game,
                "chapters": [c.__dict__ for c in self.chapters]}


_CHANNEL_VIDEOS = """query($login: String!) { user(login: $login) {
  login displayName stream { id createdAt }
  videos(first: 5, type: ARCHIVE, sort: TIME) { edges { node {
    id title createdAt lengthSeconds status } } } } }"""


def latest_vod(login: str) -> dict:
    """Newest archived broadcast of a channel. While the creator is live,
    Twitch is already writing this VOD, so `length_s` grows — scanning the
    last N hours of it clips a stream that is still running."""
    u = gql(_CHANNEL_VIDEOS, {"login": login.lower()}).get("user")
    if not u:
        raise RuntimeError(f"channel '{login}' not found")
    edges = (u.get("videos") or {}).get("edges") or []
    if not edges:
        raise RuntimeError(f"'{login}' has no public past broadcasts (VODs off, or sub-only)")
    n = edges[0]["node"]
    return {"id": n["id"], "title": n["title"], "created_at": n["createdAt"],
            "length_s": int(n["lengthSeconds"]), "live": bool(u.get("stream")),
            "status": n.get("status")}


_CLIPS = """query($login: String!, $period: ClipsPeriod) { user(login: $login) {
  clips(first: 100, criteria: {period: $period, sort: VIEWS_DESC}) {
    edges { node { slug title viewCount createdAt durationSeconds videoOffsetSeconds
      video { id } } } } } }"""


def crowd_moments(vod: "Vod", start_s: float = 0, end_s: float | None = None) -> list[dict]:
    """Where viewers clipped this VOD, merged into moments, most-viewed first.

    Viewers clip within seconds of a moment, publicly, with the exact VOD
    offset and a title saying what happened. That is the strongest "this
    is viral" signal there is — the crowd has already voted. Launch week
    The most-clipped moments of a launch day reached thousands of views
    within hours.
    """
    import datetime
    end_s = vod.length_s if end_s is None else end_s
    try:
        age_h = (datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(
            vod.created_at.replace("Z", "+00:00"))).total_seconds() / 3600
    except ValueError:
        age_h = 999
    period = "LAST_DAY" if age_h < 24 else "LAST_WEEK" if age_h < 24 * 7 else "LAST_MONTH"
    u = gql(_CLIPS, {"login": vod.login, "period": period}).get("user") or {}
    clips = [e["node"] for e in ((u.get("clips") or {}).get("edges") or [])
             if (e["node"].get("video") or {}).get("id") == vod.id
             and e["node"].get("videoOffsetSeconds") is not None]
    clips = [c for c in clips if start_s - 60 <= c["videoOffsetSeconds"] < end_s]
    clips.sort(key=lambda c: c["videoOffsetSeconds"])
    now = datetime.datetime.now(datetime.timezone.utc)
    moments: list[dict] = []
    for c in clips:
        s, e = float(c["videoOffsetSeconds"]), float(c["videoOffsetSeconds"] + c["durationSeconds"])
        try:
            mins = max(1.0, (now - datetime.datetime.fromisoformat(c["createdAt"].replace("Z", "+00:00"))).total_seconds() / 60)
        except ValueError:
            mins = 60.0
        if moments and (s - moments[-1]["start"] <= 45 or s <= moments[-1]["end"] - 10):
            m = moments[-1]                       # same scene: overlapping or near-identical start
            if c["viewCount"] > m["best"]["views"]:
                m["best"] = {"start": s, "end": e, "views": c["viewCount"]}
            m["end"] = max(m["end"], e)
            m["views"] += c["viewCount"]
            m["clips"] += 1
            m["views_per_min"] += c["viewCount"] / mins
            m["titles"].append(c["title"])
        else:
            moments.append({"start": s, "end": e, "views": c["viewCount"], "clips": 1,
                            "best": {"start": s, "end": e, "views": c["viewCount"]},
                            "views_per_min": c["viewCount"] / mins, "titles": [c["title"]],
                            "top_slug": c["slug"]})
    for m in moments:
        m["views_per_min"] = round(m["views_per_min"], 1)
        m["titles"] = [t for t in dict.fromkeys(t.strip() for t in m["titles"]) if t][:4]
    return sorted(moments, key=lambda m: -m["views"])


# --------------------------------------------------------------------------
# Chat
# --------------------------------------------------------------------------
def _page(vod_id: str, t: int) -> tuple[list[dict], bool]:
    data = gql(_COMMENTS_QUERY, {"id": vod_id, "t": int(t)})
    c = ((data.get("video") or {}).get("comments")) or {}
    out = []
    for e in c.get("edges") or []:
        n = e["node"]
        frags = (n.get("message") or {}).get("fragments") or []
        out.append({
            "id": n["id"],
            "t": float(n["contentOffsetSeconds"]),
            "user": (n.get("commenter") or {}).get("login") or "",
            "text": "".join(f.get("text") or "" for f in frags).strip(),
            "emotes": sum(1 for f in frags if f.get("emote")),
        })
    return out, bool((c.get("pageInfo") or {}).get("hasNextPage"))


def _crawl(vod_id: str, start: int, end: int) -> list[dict]:
    """Walk chat forward from `start` to `end` by content offset.

    Each request returns the next ~60 messages at/after the offset; the
    next request starts at the last message's second. Messages are deduped
    by id. If a page brings nothing new (>60 messages in one second), step
    one second forward rather than loop forever.
    """
    seen: set[str] = set()
    out: list[dict] = []
    t = start
    while t < end:
        page, more = _page(vod_id, t)
        new = [m for m in page if m["id"] not in seen and m["t"] < end]
        for m in new:
            seen.add(m["id"])
        out.extend(new)
        if not page or not more:
            break
        last = int(page[-1]["t"])
        if page[-1]["t"] >= end:
            break
        t = last if new and last > t else t + 1
    return out


def download_chat(vod_id: str, start_s: float = 0, end_s: float | None = None,
                  workers: int = 12, chunk_s: int = 300,
                  cache: str | None = None) -> list[dict]:
    """All chat messages in [start_s, end_s), sorted by time.

    Split into `chunk_s` slices crawled in parallel. A 1 h slice of a big channel
    (~10k messages) is ~170 requests; with 12 workers that is seconds,
    not minutes. `cache` (a .json path) is reused if it already covers the
    range, so re-scans cost nothing.
    """
    vod_id = parse_vod_id(vod_id)
    if end_s is None:
        end_s = Vod.fetch(vod_id).length_s
    if cache and os.path.isfile(cache):
        c = json.load(open(cache))
        if c.get("start_s", 1e18) <= start_s and c.get("end_s", -1) >= end_s:
            msgs = [m for m in c["messages"] if start_s <= m["t"] < end_s]
            log(f"chat cache hit: {len(msgs)} messages from {cache}")
            return msgs
    t0 = time.time()
    slices = [(int(s), int(min(s + chunk_s, end_s)))
              for s in range(int(start_s), int(end_s), chunk_s)]
    msgs: dict[str, dict] = {}
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_crawl, vod_id, a, b): (a, b) for a, b in slices}
        for f in cf.as_completed(futs):
            for m in f.result():
                msgs[m["id"]] = m
    out = sorted(msgs.values(), key=lambda m: m["t"])
    log(f"chat: {len(out):,} messages in {fmt(start_s)}-{fmt(end_s)} "
        f"({len(slices)} slices, {time.time() - t0:.1f}s)")
    if cache:
        os.makedirs(os.path.dirname(os.path.abspath(cache)), exist_ok=True)
        json.dump({"vod_id": vod_id, "start_s": start_s, "end_s": end_s,
                   "messages": out}, open(cache, "w"))
    return out


def load_chat_file(path: str) -> list[dict]:
    """Read a TwitchDownloader chat JSON (v1 format) or our own cache."""
    d = json.load(open(path, encoding="utf-8"))
    if isinstance(d, dict) and "messages" in d:
        return d["messages"]
    comments = d.get("comments", d) if isinstance(d, dict) else d
    out = []
    for i, c in enumerate(comments):
        off = c.get("content_offset_seconds", c.get("offset"))
        if off is None:
            continue
        msg = c.get("message") or {}
        frags = msg.get("fragments") or []
        out.append({
            "id": str(c.get("_id", i)),
            "t": float(off),
            "user": ((c.get("commenter") or {}).get("name")
                     or (c.get("commenter") or {}).get("display_name") or ""),
            "text": (msg.get("body") or "".join(f.get("text", "") for f in frags)).strip(),
            "emotes": sum(1 for f in frags if f.get("emoticon") or f.get("emote")),
        })
    return sorted(out, key=lambda m: m["t"])


# --------------------------------------------------------------------------
# HLS
# --------------------------------------------------------------------------
@dataclass
class Playlist:
    url: str
    segments: list[str]
    starts: list[float]
    durs: list[float]

    @property
    def length_s(self) -> float:
        return self.starts[-1] + self.durs[-1] if self.starts else 0.0

    def covering(self, start_s: float, end_s: float) -> list[int]:
        return [j for j in range(len(self.segments))
                if self.starts[j] + self.durs[j] > start_s and self.starts[j] < end_s]


def playlists(vod_id: str) -> dict:
    """{'video': Playlist, 'audio': Playlist|None, 'video_label': str}."""
    vod_id = parse_vod_id(vod_id)
    tok = (gql(_TOKEN_QUERY, {"isLive": False, "login": "", "isVod": True,
                              "vodID": vod_id, "playerType": "site"})
           .get("videoPlaybackAccessToken") or {})
    if not tok.get("value"):
        raise RuntimeError(f"no playback token for VOD {vod_id} — deleted, expired or sub-only")
    qs = urllib.parse.urlencode({
        "allow_source": "true", "allow_audio_only": "true", "allow_spectre": "false",
        "player": "twitchweb", "playlist_include_framerate": "true",
        "token": tok["value"], "sig": tok["signature"]})
    master = http(f"https://usher.ttvnw.net/vod/{vod_id}.m3u8?{qs}").decode()
    lines = master.splitlines()
    variants, audio = [], None
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF:") and i + 1 < len(lines):
            attrs = dict(re.findall(r'([A-Z-]+)=("[^"]*"|[^,]*)', line))
            url = lines[i + 1].strip()
            if "audio_only" in url or attrs.get("VIDEO", "").strip('"') == "audio_only":
                audio = url
            else:
                variants.append((int(attrs.get("BANDWIDTH", "0") or 0),
                                 attrs.get("RESOLUTION", "").strip('"'), url))
    if not variants:
        raise RuntimeError(f"VOD {vod_id}: master playlist has no video variants")
    variants.sort(reverse=True)
    bw, res, vurl = variants[0]
    thumb_url = variants[-1][2]          # lowest bandwidth (160p-360p): thumbnails only
    return {"video": load_playlist(vurl), "audio": load_playlist(audio) if audio else None,
            "thumb_url": thumb_url, "video_label": f"{res} ({bw // 1000} kbps)"}


def load_playlist(url: str) -> Playlist:
    text = http(url).decode("utf-8", "replace")
    segs, durs = [], []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            try:
                durs.append(float(line.split(":", 1)[1].split(",", 1)[0]))
            except ValueError:
                durs.append(10.0)
        elif line and not line.startswith("#"):
            segs.append(urllib.parse.urljoin(url, line))
    n = min(len(segs), len(durs))
    starts, t = [], 0.0
    for d in durs[:n]:
        starts.append(t)
        t += d
    return Playlist(url, segs[:n], starts, durs[:n])


def download_window(pl: Playlist, start_s: float, end_s: float, out_path: str,
                    pad_s: float = 2.0, workers: int = 8) -> float:
    """Fetch the segments covering [start-pad, end+pad] IN PARALLEL and
    concatenate them in order. Returns the VOD time of the first byte
    (seek to start_s - returned value inside the file).

    v1 fetched ~148 segments one after another (2.5-6 min per clip, the
    loop's bottleneck). 8 parallel fetches make it network-bound instead
    of latency-bound.
    """
    idx = pl.covering(start_s - pad_s, end_s + pad_s)
    if not idx:
        raise RuntimeError(f"no segments cover {start_s:.0f}-{end_s:.0f}s "
                           f"(VOD is {pl.length_s:.0f}s long)")
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        blobs = list(ex.map(lambda j: http(pl.segments[j], timeout=60), idx))
    with open(out_path, "wb") as fh:
        for b in blobs:
            fh.write(b)
    return pl.starts[idx[0]]


def fmt(s: float) -> str:
    s = max(0, int(round(s)))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def parse_time(v: str | float | int | None) -> float | None:
    """'2h', '1h30m', '01:30:00', '5400' -> seconds."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    v = v.strip()
    if ":" in v:
        parts = [float(p) for p in v.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0.0)
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s?)?", v)
    if m and any(m.groups()):
        h, mi, s = (float(x) if x else 0.0 for x in m.groups())
        return h * 3600 + mi * 60 + s
    return float(v)
