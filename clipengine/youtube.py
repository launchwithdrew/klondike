"""YouTube Live sources ("yt:@handle", a channel URL or a watch URL) — robust live detection, HLS
playlist refresh before it expires, and live chat (best effort).

  live detection  the channel's /live page names the current broadcast (cheap, no yt-dlp); yt-dlp
                  resolves the HLS playlist only when there is one.
  HLS refresh     googlevideo playlist URLs carry their expiry (/expire/<unix>/ or expire=<unix>); the
                  recorder re-resolves 15 min before it, and on any playlist failure.
  chat            YouTube's web live-chat feed (the innertube get_live_chat endpoint the popout chat
                  uses): the first page's continuation, then poll it as the page does. Text from viewers
                  is data, never instructions. It is best effort: when it can't be read (no chat, a
                  members-only chat, a changed format) the reader says so once and the sentinel falls back
                  to audio-only cleanly — `fallback` is True and nothing else changes.
"""
from __future__ import annotations

import collections
import json
import re
import threading
import time
import urllib.parse

from . import twitch as T

H = {"Accept-Language": "en-US,en;q=0.8"}
LIVE_CHAT_PAGE = "https://www.youtube.com/live_chat?is_popout=1&v={vid}"
GET_LIVE_CHAT = "https://www.youtube.com/youtubei/v1/live_chat/get_live_chat?prettyPrint=false"
REFRESH_BEFORE_S = 15 * 60
HISTORY_S = 1800.0


def page_url(source: str) -> str:
    """The URL that names the live broadcast for a source."""
    url = source[3:] if source.startswith("yt:") else source
    if url.startswith("@"):
        return f"https://www.youtube.com/{url}/live"
    if re.fullmatch(r"[\w-]{11}", url):
        return f"https://www.youtube.com/watch?v={url}"
    return url


def live_from_html(html: str) -> dict:
    """{live, video_id, title} from a channel's /live page or a watch page (pure)."""
    vid = None
    m = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([\w-]{11})"', html) or \
        re.search(r'"videoDetails":\{.{0,300}?"videoId":"([\w-]{11})"', html, re.S)
    if m:
        vid = m.group(1)
    blocked = re.search(r'"playabilityStatus":\{"status":"LOGIN_REQUIRED"', html) is not None
    live = bool(re.search(r'"isLive(?:Now|Content)?":true', html)) and \
        not re.search(r'"isUpcoming":true', html) and vid is not None
    if re.search(r'"isLiveNow":false', html):
        live = False
    t = re.search(r'"videoDetails":\{[^{}]*?"title":"((?:[^"\\]|\\.)*)"', html)
    out = {"live": live, "video_id": vid, "title": json.loads(f'"{t.group(1)}"') if t else ""}
    if blocked:          # YouTube's bot wall for this machine's address: say so instead of "not live"
        out["error"] = "YouTube asks this machine to sign in (bot check) — run YouTube sources from another network"
    return out


def live_status(source: str) -> dict:
    """Is this source live right now, and on which video (one page fetch)."""
    try:
        html = T.http(page_url(source), headers=H, attempts=2, timeout=20).decode("utf-8", "replace")
    except Exception as exc:
        return {"live": False, "video_id": None, "title": "", "error": str(exc)[:120]}
    return live_from_html(html)


def expires_at(url: str) -> float | None:
    """The unix time a googlevideo playlist URL stops working (pure)."""
    m = re.search(r"/expire/(\d{9,11})(?:/|$)", url) or re.search(r"[?&]expire=(\d{9,11})", url)
    return float(m.group(1)) if m else None


def needs_refresh(url: str, resolved_at: float, now: float | None = None) -> bool:
    """Re-resolve before the playlist URL expires; with no expiry in it, every 4 h (pure)."""
    now = now or time.time()
    exp = expires_at(url)
    if exp:
        return now >= exp - REFRESH_BEFORE_S
    return now - resolved_at > 4 * 3600


def variant(source: str) -> str:
    """Live HLS media playlist for a YouTube source, via yt-dlp (raises when not live)."""
    import yt_dlp
    st = live_status(source)
    url = f"https://www.youtube.com/watch?v={st['video_id']}" if st.get("video_id") else page_url(source)
    if st.get("video_id") is None and not st.get("error"):
        raise RuntimeError(f"{source} is not live")
    opts = {"quiet": True, "no_warnings": True, "format": "best[protocol^=m3u8]/best", "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info.get("is_live"):
        raise RuntimeError(f"{source} is not live")
    hls = info.get("url") or next((f["url"] for f in info.get("formats", []) if "m3u8" in (f.get("protocol") or "")), None)
    if not hls:
        raise RuntimeError(f"no live HLS playlist for {source}")
    return hls


# ---- live chat ---------------------------------------------------------------------------------

def chat_bootstrap(html: str) -> dict:
    """From the popout live-chat page: the first continuation and the web client version (pure)."""
    ver = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', html)
    cont = None
    for key in ("invalidationContinuationData", "timedContinuationData", "reloadContinuationData"):
        m = re.search(r'"%s":\{[^{}]*?"continuation":"([^"]+)"' % key, html)
        if m:
            cont = m.group(1)
            break
    return {"continuation": cont, "client_version": ver.group(1) if ver else "2.20251101.00.00"}


def parse_chat(resp: dict) -> tuple[list[dict], str | None, float]:
    """One get_live_chat response -> (messages {t, user, text, emotes}, next continuation, wait s) (pure)."""
    lc = ((resp.get("continuationContents") or {}).get("liveChatContinuation") or {})
    msgs = []
    for a in lc.get("actions") or []:
        item = ((a.get("addChatItemAction") or {}).get("item") or {})
        r = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
        if not r:
            continue
        runs = (r.get("message") or {}).get("runs") or []
        text, emotes = "", 0
        for x in runs:
            if "text" in x:
                text += x["text"]
            elif "emoji" in x:
                emotes += 1
                e = x["emoji"]
                text += (e.get("shortcuts") or [""])[0] or (e.get("emojiId") or "")
        try:
            t = int(r.get("timestampUsec")) / 1e6
        except (TypeError, ValueError):
            t = time.time()
        msgs.append({"t": t, "user": r.get("authorExternalChannelId") or "", "text": text.strip(), "emotes": emotes})
    nxt, wait = None, 5.0
    for c in lc.get("continuations") or []:
        d = c.get("invalidationContinuationData") or c.get("timedContinuationData") or c.get("liveChatReplayContinuationData")
        if d and d.get("continuation"):
            nxt = d["continuation"]
            wait = max(1.0, min(10.0, float(d.get("timeoutMs") or 5000) / 1000))
            break
    return msgs, nxt, wait


class YouTubeChat(threading.Thread):
    """YouTube live chat with the sentinel's Chat interface (connected, window, total). Best effort:
    `fallback` turns True when the chat can't be read, and the sentinel judges on audio alone."""
    def __init__(self, source: str, video_id: str | None = None, fetch=None):
        super().__init__(daemon=True)
        self.source, self.video_id = source, video_id
        self.msgs, self.lock = collections.deque(), threading.Lock()
        self.connected, self.fallback, self.stop_flag, self.total = False, False, False, 0
        self.why = ""
        self.started = time.time()
        self.log_session = fetch is None                # the real reader (a sentinel's) writes data/sources.jsonl
        self.fetch = fetch or (lambda url, data=None: T.http(url, data=data, attempts=2, timeout=20,
                                                            headers={**H, "Content-Type": "application/json"}
                                                            if data else H))

    def _give_up(self, why: str) -> None:
        self.connected, self.fallback, self.why = False, True, why
        print(f"[sentinel] YouTube chat unavailable ({why}) — judging on audio only", flush=True)

    def run(self):
        if self.log_session:
            threading.Thread(target=self._log_loop, daemon=True).start()
        fails = 0
        while not self.stop_flag:
            try:
                if not self.video_id:
                    st = live_status(self.source)
                    if not st.get("video_id"):
                        raise RuntimeError(st.get("error") or "not live")
                    self.video_id = st["video_id"]
                boot = chat_bootstrap(self.fetch(LIVE_CHAT_PAGE.format(vid=self.video_id)).decode("utf-8", "replace"))
                if not boot["continuation"]:
                    return self._give_up("no live chat on this stream")
                cont, ctx = boot["continuation"], {"client": {"clientName": "WEB", "clientVersion": boot["client_version"],
                                                              "hl": "en"}}
                self.connected, self.fallback, fails = True, False, 0
                while not self.stop_flag and cont:
                    body = json.dumps({"context": ctx, "continuation": cont}).encode()
                    msgs, cont, wait = parse_chat(json.loads(self.fetch(GET_LIVE_CHAT, body)))
                    self.add(msgs)
                    time.sleep(wait)
                if not cont:
                    raise RuntimeError("chat ended")
            except Exception as exc:
                self.connected = False
                fails += 1
                if fails >= 3:
                    self._give_up(str(exc)[:80])
                    time.sleep(300)                    # try again later: a stream can turn chat on
                    fails, self.video_id = 0, None
                else:
                    time.sleep(10 * fails)

    def mode(self) -> str:
        return "chat" if self.total else "audio-only"

    def _log_loop(self, every_s: float = 60.0) -> None:
        while True:
            try:
                log_session(self.source, self.started, self.mode(), self.total, self.why)
            except Exception:
                pass                                     # bookkeeping never stops the reader
            if self.stop_flag:
                return
            time.sleep(every_s)

    def add(self, msgs: list[dict]) -> None:
        now = time.time()
        with self.lock:
            for m in msgs:
                self.msgs.append({**m, "t": min(m["t"], now)})
                self.total += 1
            while self.msgs and self.msgs[0]["t"] < now - HISTORY_S - 10:
                self.msgs.popleft()

    def window(self, t0: float, t1: float) -> list[dict]:
        with self.lock:
            return [m for m in self.msgs if t0 <= m["t"] < t1]

    def count(self, t0, t1):                           # no viewer clips on YouTube
        return 0


# ---- radar discovery: live streams of the game on YouTube ------------------------------------------
#
# Two ways in, tried in this order:
#   1. the YouTube Data API v3, when YOUTUBE_API_KEY is in the environment (never in a file): search.list
#      (type=video, eventType=live, q, relevanceLanguage) -> videos.list (liveStreamingDetails: concurrent
#      viewers, start) -> channels.list (snippet.customUrl: the @handle). ~102 quota units a run, so the radar
#      asks at most every `youtube_every_min` (20) — about 7,300 of the default 10,000 units a day.
#   2. YouTube's own live search results page (plain HTTP GET, results?search_query=…&sp=<Live filter>): the
#      page's ytInitialData lists each live video with "N watching", its title and its channel's /@handle.
# No browser impersonation, no cookies: when YouTube shows this machine its bot check, that's ONE quiet radar
# line ("youtube: bot check") and the radar carries on. Language: the API's relevanceLanguage / the video's
# audio language when it says; on the plain route a title heuristic (looks_english) — rows say which ("lang").
# Verified Nov 19 2026: the plain route from a cloud machine (20 live rows). NOT verified: the Data API route.

SEARCH_PAGE = "https://www.youtube.com/results?search_query={q}&sp=EgJAAQ%253D%253D&hl=en&gl=US"
DATA_API = "https://www.googleapis.com/youtube/v3"
_WALL = re.compile(r"Sign in to confirm you.{0,3}re not a bot|unusual traffic from your computer|"
                   r'"status":"LOGIN_REQUIRED"|consent\.youtube\.com|www\.google\.com/sorry/', re.I)
_HANDLE = re.compile(r"^/?@([A-Za-z0-9_.\-]{3,30})$")
_GAME_RE = re.compile(r"\bgta\s*-?\s*(?:6|vi)\b|grand\s+theft\s+auto\s+(?:6|vi)\b", re.I)
# words that mark a title as another language (the plain route has no language field)
_NOT_EN = re.compile(r"\b(?:en directo|en vivo|modo historia|directo|jugando|parte|español|espanol|"
                     r"türkçe|turkce|yayını|yayini|özel|canlı|nederlands|deutsch|geld machen|auf deutsch|"
                     r"français|francais|en direct|português|portugues|ao vivo|jogando|italiano|in diretta|"
                     r"polski|na żywo|русский|стрим|hindi|हिंदी|bahasa|indonesia|tiếng việt|"
                     r"العربية|بث مباشر)\b", re.I)


def looks_english(title: str) -> bool:
    """A cheap guess from a title alone (pure): mostly Latin letters and no other language's marker words."""
    letters = [c for c in title if c.isalpha()]
    if letters and sum(1 for c in letters if c.isascii()) / len(letters) < 0.7:
        return False
    return not _NOT_EN.search(title)


def bot_wall(html: str) -> bool:
    return bool(_WALL.search(html[:400000]))


def _text(x) -> str:
    if not isinstance(x, dict):
        return ""
    return x.get("simpleText") or "".join(r.get("text", "") for r in x.get("runs") or [])


def _viewers(txt: str) -> int:
    """'145,637 watching' / '1.2K watching' -> a number (pure)."""
    m = re.search(r"([\d.,]+)\s*([KkMm]?)", txt or "")
    if not m:
        return 0
    n = float(m.group(1).replace(",", "")) if m.group(2) else float(re.sub(r"[.,]", "", m.group(1)))
    return int(n * {"k": 1e3, "m": 1e6}.get(m.group(2).lower(), 1))


def _initial_data(html: str) -> dict:
    m = re.search(r"var ytInitialData\s*=\s*(\{.*?\});\s*</script>", html, re.S) or \
        re.search(r'window\["ytInitialData"\]\s*=\s*(\{.*?\});', html, re.S)
    if not m:
        raise RuntimeError("no ytInitialData in the page (YouTube changed it)")
    return json.loads(m.group(1))


def _renderers(x, out: list) -> list:
    if isinstance(x, dict):
        if "videoRenderer" in x:
            out.append(x["videoRenderer"])
        for v in x.values():
            _renderers(v, out)
    elif isinstance(x, list):
        for v in x:
            _renderers(v, out)
    return out


def _row(handle: str, name: str, viewers: int, title: str, vid: str, started=None, lang: str = "") -> dict | None:
    m = _HANDLE.match(handle or "")
    if not m:                                      # stranger-written: only a plain @handle becomes a command
        return None
    login = "yt:@" + m.group(1).lower()
    return {"login": login, "name": name or m.group(1), "viewers": int(viewers), "title": title, "started": started,
            "platform": "yt", "lang": lang, "video_id": vid if re.fullmatch(r"[\w-]{11}", vid or "") else None}


def search_rows(html: str, langs: list[str] | None = None, game_re=_GAME_RE) -> list[dict]:
    """YouTube's live search results page -> radar rows, most watched first (pure). Only videos YouTube marks
    live now; with langs=["en"] titles that look like another language are left out."""
    rows, seen = [], set()
    for v in _renderers(_initial_data(html), []):
        badges = " ".join(str((b.get("metadataBadgeRenderer") or {}).get("style", "")) for b in v.get("badges") or [])
        overlays = json.dumps(v.get("thumbnailOverlays") or [])
        if "LIVE" not in badges and '"LIVE"' not in overlays:
            continue
        title = _text(v.get("title"))
        if game_re and not game_re.search(title):
            continue
        lang = ""
        if langs and "en" in [l.lower() for l in langs]:
            if not looks_english(title):
                continue
            lang = "en?"                           # a guess from the title: the page has no language field
        own = ((v.get("ownerText") or v.get("longBylineText") or {}).get("runs") or [{}])[0]
        handle = ((own.get("navigationEndpoint") or {}).get("browseEndpoint") or {}).get("canonicalBaseUrl", "")
        r = _row(handle, own.get("text", ""), _viewers(_text(v.get("viewCountText"))), title, v.get("videoId", ""),
                 lang=lang)
        if r and r["login"] not in seen:
            seen.add(r["login"])
            rows.append(r)
    return sorted(rows, key=lambda r: -r["viewers"])


def api_rows(search: dict, videos: dict, channels: dict, langs: list[str] | None = None, game_re=_GAME_RE) -> list[dict]:
    """The Data API's search.list + videos.list + channels.list answers -> radar rows (pure)."""
    handles = {c.get("id"): (c.get("snippet") or {}).get("customUrl", "") for c in channels.get("items") or []}
    want = {l.lower() for l in (langs or [])}
    rows, seen = [], set()
    for v in videos.get("items") or []:
        sn, ls = v.get("snippet") or {}, v.get("liveStreamingDetails") or {}
        if sn.get("liveBroadcastContent") not in (None, "live") or ls.get("actualEndTime"):
            continue
        title = sn.get("title") or ""
        if game_re and not game_re.search(title):
            continue
        lang = str(sn.get("defaultAudioLanguage") or sn.get("defaultLanguage") or "")[:2].lower()
        if want and lang and lang not in want:
            continue
        if want and not lang and "en" in want and not looks_english(title):
            continue
        h = handles.get(sn.get("channelId"), "")
        r = _row(h if h.startswith("@") else "@" + h if h else "", sn.get("channelTitle", ""),
                 int(ls.get("concurrentViewers") or 0), title, v.get("id", ""), ls.get("actualStartTime"),
                 lang or ("en?" if "en" in want else ""))
        if r and r["login"] not in seen:
            seen.add(r["login"])
            rows.append(r)
    return sorted(rows, key=lambda r: -r["viewers"])


def why(exc: Exception) -> str:
    msg = str(exc)
    if "bot check" in msg:
        return "bot check"
    m = re.search(r"HTTP Error (\d{3})", msg)
    if m:
        return {"403": "403 (quota or key refused)" if "googleapis" in msg else "403"}.get(m.group(1), m.group(1))
    low = msg.lower()
    if "timed out" in low or "timeout" in low:
        return "timeout"
    if "ytinitialdata" in low:
        return "page format changed"
    return "unreachable"


def category_streams(query: str, langs: list[str] | None = None, key: str | None = None, timeout: float = 15,
                     game_re=_GAME_RE) -> dict:
    """Live YouTube streams for the query: {ok, via, rows, why}. Never raises."""
    tried = []
    if key:
        try:
            q = urllib.parse.urlencode({"part": "snippet", "type": "video", "eventType": "live", "q": query,
                                        "maxResults": 50, "order": "viewCount", "key": key,
                                        **({"relevanceLanguage": langs[0]} if langs else {})})
            s = json.loads(T.http(f"{DATA_API}/search?{q}", attempts=1, timeout=timeout))
            ids = [i["id"]["videoId"] for i in s.get("items") or [] if (i.get("id") or {}).get("videoId")]
            vids = json.loads(T.http(f"{DATA_API}/videos?" + urllib.parse.urlencode(
                {"part": "snippet,liveStreamingDetails", "id": ",".join(ids), "key": key}), attempts=1,
                timeout=timeout)) if ids else {"items": []}
            chs = sorted({(v.get("snippet") or {}).get("channelId") for v in vids.get("items") or []} - {None})
            chans = json.loads(T.http(f"{DATA_API}/channels?" + urllib.parse.urlencode(
                {"part": "snippet", "id": ",".join(chs), "key": key}), attempts=1, timeout=timeout)) if chs else {}
            return {"ok": True, "via": "Data API", "rows": api_rows(s, vids, chans, langs, game_re), "why": ""}
        except Exception as exc:
            tried.append(f"Data API {why(exc)}")
    try:
        html = T.http(SEARCH_PAGE.format(q=urllib.parse.quote_plus(query)), headers=H, attempts=1,
                      timeout=timeout).decode("utf-8", "replace")
        if bot_wall(html):
            raise RuntimeError("bot check")
        return {"ok": True, "via": "live search page" + (f" ({tried[0]})" if tried else ""),
                "rows": search_rows(html, langs, game_re), "why": ""}
    except Exception as exc:
        tried.append(why(exc))
    return {"ok": False, "via": "", "rows": [], "why": ", ".join(dict.fromkeys(tried))}


def api_key(env: dict | None = None) -> str | None:
    """YOUTUBE_API_KEY from the environment only (never from a committed file)."""
    import os
    return ((os.environ if env is None else env).get("YOUTUBE_API_KEY") or "").strip() or None


# ---- attended sessions: one line per yt: sentinel session in data/sources.jsonl ----------------------
#
# Each YouTube sentinel session keeps ONE line, rewritten in place every minute: {source, started, updated, mode
# (chat | audio-only), chat_msgs, drafts, why}. `clipengine sources` prints the last 10, for STATUS.md.

def _repo() -> str:
    import os
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sources_file() -> str:
    import os
    return os.path.join(_repo(), "data", "sources.jsonl")


def drafts_since(source: str, since: float) -> int:
    """Drafts (not drops) the source's sentinel queued since `since`."""
    import os
    from . import live as L
    import datetime as dt
    qf = os.path.join(_repo(), "runs", f"sentinel-{L.source_name(source)}", "queue.jsonl")
    ids = set()
    try:
        for line in open(qf):
            try:
                r = json.loads(line)
                t = dt.datetime.fromisoformat(r["at"]).timestamp()
            except (ValueError, KeyError, TypeError):
                continue
            if t >= since and r.get("status") != "dropped":
                ids.add(r.get("id"))
    except OSError:
        pass
    return len(ids)


def log_session(source: str, started: float, mode: str, msgs: int, why: str = "", path: str | None = None,
                now: float | None = None, drafts: int | None = None) -> dict:
    """Write (or rewrite) this session's one line in data/sources.jsonl."""
    import fcntl
    import os
    path = path or sources_file()
    now = now or time.time()
    row = {"source": source, "started": round(started, 1), "updated": round(now, 1), "mode": mode,
           "chat_msgs": int(msgs), "drafts": drafts_since(source, started) if drafts is None else drafts,
           **({"why": why[:80]} if why and mode != "chat" else {})}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            lines = open(path).read().splitlines()
        except OSError:
            lines = []
        out, done = [], False
        for line in lines:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("source") == source and r.get("started") == row["started"]:
                r, done = row, True
            out.append(json.dumps(r))
        if not done:
            out.append(json.dumps(row))
        tmp = path + ".tmp"
        open(tmp, "w").write("\n".join(out[-500:]) + "\n")
        os.replace(tmp, path)
    return row


def sources_lines(rows: list[dict], now: float | None = None, n: int = 10) -> list[str]:
    """The last n sessions, newest first, one line each (pure)."""
    import datetime as dt
    now = now or time.time()
    out = []
    for r in sorted(rows, key=lambda r: -r.get("started", 0))[:n]:
        dur = max(0.0, r.get("updated", r["started"]) - r["started"]) / 60
        live = now - r.get("updated", 0) < 180
        mode = (f"chat ({r.get('chat_msgs', 0):,} msgs)" if r.get("mode") == "chat"
                else "audio-only" + (f" ({r['why']})" if r.get("why") else ""))
        out.append(f"{r['source']:<24} {dt.datetime.fromtimestamp(r['started'], dt.timezone.utc):%b %d %H:%M} UTC  "
                   f"{int(dur // 60)}h{int(dur % 60):02d}m  {mode}  {r.get('drafts', 0)} draft(s)"
                   + ("  · live now" if live else ""))
    return out


def show_sources(n: int = 10, path: str | None = None) -> list[str]:
    rows = []
    try:
        for line in open(path or sources_file()):
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    lines = sources_lines(rows, n=n)
    print("SOURCES — the last YouTube sentinel sessions (data/sources.jsonl)")
    for l in lines or ["  none yet: a yt: sentinel writes its line here while it runs"]:
        print("  " + l if not l.startswith("  ") else l)
    return lines
