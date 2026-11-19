"""Kick sources ("kick:<slug>") behind the same interfaces as Twitch.

  live detection  GET https://kick.com/api/v2/channels/<slug>: `livestream` is set while live, and
                  `playback_url` is the stream's HLS master playlist (Amazon IVS).
  recording       live.record() with this resolver: the best video variant of that master playlist,
                  timed by #EXT-X-PROGRAM-DATE-TIME like Twitch (no stitched ads to skip).
  chat            Kick's public Pusher websocket, channel chatrooms.<chatroom id>.v2, event
                  App\\Events\\ChatMessageEvent — anonymous, read-only. KickChat has the sentinel's Chat
                  interface (connected, window, total), so tier 1 and tier 2 use it unchanged.

UNVERIFIED against the live service: this machine's network refuses kick.com and ws-us2.pusher.com, so it
was built and tested against fixtures of those documented formats. A machine that runs Kick sources needs:
kick.com (API), ws-us2.pusher.com (chat), and the IVS playback hosts under live-video.net (master on
*.playback.live-video.net, segments on *.hls.live-video.net — names vary per stream). kick.com sits behind
Cloudflare, which can refuse non-browser clients; `doctor` says so when it does.
"""
from __future__ import annotations

import collections
import json
import re
import threading
import time
import urllib.parse

from . import twitch as T

API = "https://kick.com/api/v2/channels/{slug}"
PUSHER = "wss://ws-us2.pusher.com/app/32cbd69e4b950bf97679?protocol=7&client=js&version=8.4.0-rc2&flash=false"
HEADERS = {"Accept": "application/json", "Accept-Language": "en-US,en;q=0.8", "Referer": "https://kick.com/"}
HISTORY_S = 1800.0
EMOTE_RE = re.compile(r"\[emote:(\d+):([^\]]*)\]")


def is_kick(source: str) -> bool:
    return source.startswith("kick:")


def slug_of(source: str) -> str:
    return source[5:].strip().lower() if is_kick(source) else source.lower()


def channel(slug: str) -> dict:
    return json.loads(T.http(API.format(slug=slug_of(slug)), headers=HEADERS, attempts=2, timeout=20))


def live_from_api(js: dict) -> dict:
    """{live, title, viewers, started, game, playback_url, chatroom_id} from the channel API (pure)."""
    ls = js.get("livestream") or {}
    live = bool(ls) and ls.get("is_live", True) is not False
    cats = ls.get("categories") or []
    return {"live": live, "title": ls.get("session_title") or "", "viewers": ls.get("viewer_count") or 0,
            "started": ls.get("created_at") or ls.get("start_time"), "game": (cats[0].get("name") if cats else None),
            "playback_url": js.get("playback_url") if live else None,
            "chatroom_id": (js.get("chatroom") or {}).get("id")}


def live_status(source: str) -> dict:
    try:
        return live_from_api(channel(source))
    except Exception as exc:
        msg = str(exc)
        if "403" in msg:
            msg += " — kick.com refused this client (Cloudflare); run Kick sources from a machine it allows"
        return {"live": False, "error": msg[:200]}


def best_variant(master: str, base: str) -> str:
    """The highest-bandwidth video variant of an HLS master playlist (pure)."""
    lines = master.splitlines()
    best = None
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF:") and i + 1 < len(lines):
            if "audio_only" in line.lower():
                continue
            bw = int((re.search(r"BANDWIDTH=(\d+)", line) or [0, 0])[1])
            if best is None or bw > best[0]:
                best = (bw, urllib.parse.urljoin(base, lines[i + 1].strip()))
    if not best:
        if "#EXTINF" in master:
            return base                                  # already a media playlist
        raise RuntimeError("no video variant in the Kick master playlist")
    return best[1]


def variant(source: str) -> str:
    """The live media playlist to record (raises when not live)."""
    st = live_status(source)
    if not st.get("live") or not st.get("playback_url"):
        raise RuntimeError(f"{source} is not live" + (f" ({st['error']})" if st.get("error") else ""))
    master = T.http(st["playback_url"], attempts=2, timeout=20).decode("utf-8", "replace")
    return best_variant(master, st["playback_url"])


def live_roster(roster: dict) -> set[str]:
    """Kick creators on the roster that are live now (one API call each)."""
    return {k for k in roster if is_kick(k) and live_status(k).get("live")}


# ---- chat ---------------------------------------------------------------------------------------

def parse_frame(raw: str) -> tuple[str, dict | None]:
    """One Pusher frame -> (event, a chat message {t, user, text, emotes} or None) (pure)."""
    try:
        f = json.loads(raw)
    except ValueError:
        return "", None
    ev = f.get("event") or ""
    if ev != "App\\Events\\ChatMessageEvent":
        return ev, None
    d = f.get("data")
    if isinstance(d, str):
        try:
            d = json.loads(d)
        except ValueError:
            return ev, None
    d = d or {}
    content = d.get("content") or ""
    emotes = len(EMOTE_RE.findall(content))
    text = EMOTE_RE.sub(lambda m: m.group(2), content).strip()          # [emote:37226:KEKW] -> KEKW
    t = None
    try:
        import datetime as dt
        t = dt.datetime.fromisoformat(str(d.get("created_at")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        pass
    user = str((d.get("sender") or {}).get("id") or (d.get("sender") or {}).get("username") or "")
    return ev, {"t": t, "user": user, "text": text, "emotes": emotes}


class KickChat(threading.Thread):
    """Anonymous read-only Kick chat (Pusher) with the sentinel's Chat interface."""
    def __init__(self, source: str, chatroom_id: int | None = None):
        super().__init__(daemon=True)
        self.slug, self.chatroom_id = slug_of(source), chatroom_id
        self.msgs, self.lock = collections.deque(), threading.Lock()
        self.connected, self.stop_flag, self.total, self.why = False, False, 0, ""

    def run(self):
        import websocket
        from .sentinel import _ws_kwargs
        delay = 2
        while not self.stop_flag:
            try:
                if not self.chatroom_id:
                    self.chatroom_id = live_from_api(channel(self.slug)).get("chatroom_id")
                    if not self.chatroom_id:
                        raise RuntimeError("no chatroom id")
                ws = websocket.create_connection(PUSHER, timeout=30, **_ws_kwargs())
                ws.send(json.dumps({"event": "pusher:subscribe",
                                    "data": {"auth": "", "channel": f"chatrooms.{self.chatroom_id}.v2"}}))
                self.connected, delay = True, 2
                while not self.stop_flag:
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        ws.send(json.dumps({"event": "pusher:ping", "data": {}}))
                        continue
                    ev, m = parse_frame(raw)
                    if ev == "pusher:ping":
                        ws.send(json.dumps({"event": "pusher:pong", "data": {}}))
                    elif m:
                        self.add(m)
            except Exception as exc:
                self.connected, self.why = False, str(exc)[:80]
                print(f"[kick] chat reconnect in {delay}s ({self.why})", flush=True)
                time.sleep(delay)
                delay = min(60, delay * 2)

    def add(self, m: dict, now: float | None = None) -> None:
        now = now or time.time()
        with self.lock:
            self.msgs.append({**m, "t": now})          # arrival time: same clock as the judge's windows
            self.total += 1
            while self.msgs and self.msgs[0]["t"] < now - HISTORY_S - 10:
                self.msgs.popleft()

    def window(self, t0: float, t1: float) -> list[dict]:
        with self.lock:
            return [m for m in self.msgs if t0 <= m["t"] < t1]

    def count(self, t0, t1):                          # no viewer-clip polling on Kick (yet)
        return 0
