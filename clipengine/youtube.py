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
        self.fetch = fetch or (lambda url, data=None: T.http(url, data=data, attempts=2, timeout=20,
                                                            headers={**H, "Content-Type": "application/json"}
                                                            if data else H))

    def _give_up(self, why: str) -> None:
        self.connected, self.fallback, self.why = False, True, why
        print(f"[sentinel] YouTube chat unavailable ({why}) — judging on audio only", flush=True)

    def run(self):
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
