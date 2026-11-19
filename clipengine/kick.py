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

def pusher_error_code(raw: str) -> int:
    """The code of a pusher:error frame (its data may be an object or a JSON string), 0 if none (pure)."""
    try:
        d = json.loads(raw).get("data")
        d = json.loads(d) if isinstance(d, str) else d
        return int((d or {}).get("code") or 0)
    except (ValueError, AttributeError, TypeError):
        return 0


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


class ChatUnavailable(RuntimeError):
    """Kick refused chat to this client for good (e.g. its realtime key/cluster changed)."""


class KickChat(threading.Thread):
    """Anonymous read-only Kick chat (Pusher) with the sentinel's Chat interface."""
    def __init__(self, source: str, chatroom_id: int | None = None):
        super().__init__(daemon=True)
        self.slug, self.chatroom_id = slug_of(source), chatroom_id
        self.msgs, self.lock = collections.deque(), threading.Lock()
        self.connected, self.stop_flag, self.total, self.why = False, False, 0, ""
        self.audio_only = False

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
                    code = pusher_error_code(raw) if ev == "pusher:error" else 0
                    if 4000 <= code < 4100:                # 4001 "app key not in this cluster": Kick moved its chat
                        raise ChatUnavailable(f"Kick chat refused this client (pusher {code})")
                    if ev == "pusher:ping":
                        ws.send(json.dumps({"event": "pusher:pong", "data": {}}))
                    elif m:
                        self.add(m)
            except ChatUnavailable as exc:
                # not a hiccup: Kick won't serve chat to this client — run audio-only, don't retry forever
                self.connected, self.why, self.audio_only = False, str(exc)[:80], True
                print(f"[kick] {self.why} — this sentinel runs audio-only", flush=True)
                return
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


# ---- radar discovery: live streams in the category -------------------------------------------------
#
# UNVERIFIED like the rest of this file. Two ways in, tried in this order:
#   1. Kick's official API, when this machine has an app's credentials: an app access token
#      (POST id.kick.com/oauth/token, client_credentials), the category's id
#      (GET api.kick.com/public/v1/categories?q=<name>), then its live streams
#      (GET api.kick.com/public/v1/livestreams?category_id=<id>&sort=viewer_count).
#      Credentials come from env vars, never from a committed file: KICK_CLIENT_ID / KICK_CLIENT_SECRET,
#      or the env var names given as config/ops.json "kick_client_id" / "kick_client_secret" ("env:NAME").
#   2. The website's own unauthenticated listings (PUBLIC_LISTINGS) — what kick.com's category page reads.
# Either answer is normalized to radar stream rows {login: "kick:<slug>", name, viewers, title, started,
# platform: "kick", lang}. Every failure is one short reason ("403", "timeout", "blocked") for the radar.

TOKEN_URL = "https://id.kick.com/oauth/token"
OFFICIAL = "https://api.kick.com/public/v1"
PUBLIC_LISTINGS = (
    "https://kick.com/stream/livestreams/{lang}?page=1&limit={limit}&subcategory={slug}&sort=desc&strict=true",
    "https://kick.com/api/v1/subcategories/{slug}/livestreams?page=1&limit={limit}",
)
DEFAULT_CATEGORY_SLUG = "grand-theft-auto-vi"


def category_slug(game: str) -> str:
    """"Grand Theft Auto VI" -> "grand-theft-auto-vi" (Kick's subcategory slugs; config/radar.json
    "kick_category" overrides it)."""
    return re.sub(r"[^a-z0-9]+", "-", (game or "").lower()).strip("-") or DEFAULT_CATEGORY_SLUG


def credentials(ops_cfg: dict | None = None, env: dict | None = None) -> tuple[str, str] | None:
    """(client_id, client_secret) from env vars, or None. A literal secret written into ops.json is ignored."""
    import os
    env = os.environ if env is None else env
    ops_cfg = ops_cfg or {}

    def pick(key: str, default_env: str) -> str:
        v = str(ops_cfg.get(key) or "")
        name = v[4:] if v.startswith("env:") else v[1:] if v.startswith("$") else ""
        if name:
            return env.get(name, "")
        if key == "kick_client_id" and v:          # a client id is not a secret; the secret only ever via env
            return v
        return env.get(default_env, "")
    cid, sec = pick("kick_client_id", "KICK_CLIENT_ID"), pick("kick_client_secret", "KICK_CLIENT_SECRET")
    return (cid, sec) if cid and sec else None


def why(exc: Exception) -> str:
    """A failure as one short reason: the HTTP status, timeout, blocked, unreachable."""
    msg = str(exc)
    m = re.search(r"HTTP Error (\d{3})|\b(40[0-9]|42[0-9]|5\d\d)\b", msg)
    if m:
        return m.group(1) or m.group(2)
    low = msg.lower()
    if "timed out" in low or "timeout" in low:
        return "timeout"
    if "blocked" in low or "not allowed" in low or "no rule allows" in low:
        return "blocked"
    return "unreachable"


def _get_json(url: str, headers: dict | None = None, timeout: float = 10) -> dict | list:
    return json.loads(T.http(url, headers={**HEADERS, **(headers or {})}, attempts=1, timeout=timeout))


def _token(cid: str, sec: str, timeout: float = 10) -> str:
    body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": cid,
                                   "client_secret": sec}).encode()
    js = json.loads(T.http(TOKEN_URL, data=body, attempts=1, timeout=timeout,
                           headers={"Content-Type": "application/x-www-form-urlencoded"}))
    tok = js.get("access_token")
    if not tok:
        raise RuntimeError("no access_token in Kick's token answer")
    return tok


def rows_from_listing(js, langs: list[str] | None = None) -> list[dict]:
    """Normalize a Kick livestream listing (official API or the website's) to radar rows (pure).
    Tolerates both shapes: official {data: [{slug, stream_title, viewer_count, started_at, language}]},
    website {data: [{session_title, viewer_count, start_time, language, channel: {slug, user: {username}}}]}."""
    items = js
    if isinstance(js, dict):
        items = js.get("data") if js.get("data") is not None else js.get("livestreams", [])
        if isinstance(items, dict):
            items = items.get("livestreams") or items.get("data") or []
    want = {l.lower() for l in (langs or [])}
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        ch = it.get("channel") or {}
        slug = ch.get("slug") or it.get("channel_slug") or it.get("slug")    # website: slug is the stream's
        slug = str(slug or "").lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", slug):     # stranger-written: only a plain slug
            continue
        lang = str(it.get("language") or "").lower()[:2]
        if want and lang and lang not in want:
            continue
        name = (ch.get("user") or {}).get("username") or ch.get("username") or slug
        out.append({"login": "kick:" + slug, "name": name,
                    "viewers": int(it.get("viewer_count") or it.get("viewers") or 0),
                    "title": it.get("stream_title") or it.get("session_title") or "",
                    "started": it.get("started_at") or it.get("start_time") or it.get("created_at"),
                    "platform": "kick", "lang": lang,
                    "mature": bool(it.get("has_mature_content") or it.get("is_mature"))})
    out.sort(key=lambda r: -r["viewers"])
    return out


def category_streams(game: str, langs: list[str] | None = None, slug: str | None = None, limit: int = 50,
                     creds: tuple[str, str] | None = None, timeout: float = 10) -> dict:
    """Live Kick streams in the category: {ok, via, rows, why}. Never raises."""
    slug = slug or category_slug(game)
    tried = []
    if creds:
        try:
            tok = _token(*creds, timeout=timeout)
            auth = {"Authorization": f"Bearer {tok}"}
            cats = _get_json(f"{OFFICIAL}/categories?q={urllib.parse.quote(game)}", auth, timeout)
            cat = next((c for c in (cats.get("data") or []) if str(c.get("name", "")).lower() == game.lower()),
                       None) or next(iter(cats.get("data") or []), None)
            if not cat:
                raise RuntimeError(f"no Kick category named {game!r}")
            q = urllib.parse.urlencode({"category_id": cat["id"], "limit": min(limit, 100), "sort": "viewer_count",
                                        **({"language": langs[0]} if langs and len(langs) == 1 else {})})
            js = _get_json(f"{OFFICIAL}/livestreams?{q}", auth, timeout)
            return {"ok": True, "via": "official API", "rows": rows_from_listing(js, langs), "why": ""}
        except Exception as exc:
            tried.append(f"official API {why(exc)}")
    for tpl in PUBLIC_LISTINGS:
        url = tpl.format(lang=(langs or ["en"])[0], limit=limit, slug=slug)
        try:
            js = _get_json(url, timeout=timeout)
            return {"ok": True, "via": url.split("?")[0].replace("https://", ""), "rows": rows_from_listing(js, langs),
                    "why": ""}
        except Exception as exc:
            tried.append(why(exc))
    reasons = list(dict.fromkeys(tried))
    return {"ok": False, "via": "", "rows": [], "why": ", ".join(reasons) or "unreachable"}


# ---- kick-check: what works from THIS machine -----------------------------------------------------

def check(game: str, langs: list[str] | None = None, cat_slug: str | None = None, channel_src: str | None = None,
          seconds: float = 60.0, creds: tuple[str, str] | None = None, sleep=time.sleep) -> dict:
    """Try each step Kick needs, in order, and say exactly which worked:
    site (kick.com answers), listing (the category's live streams), channel (the top streamer's API),
    record (its HLS master -> variant -> a media segment), chat (Pusher messages for `seconds`)."""
    out: dict[str, dict] = {}
    lst = category_streams(game, langs, cat_slug, creds=creds)
    out["listing"] = {"ok": lst["ok"], "note": (f"{len(lst['rows'])} live via {lst['via']}" if lst["ok"]
                                                else lst["why"])}
    if lst["ok"]:
        out["site"] = {"ok": True, "note": "kick.com answers"}
    else:
        try:
            T.http("https://kick.com/", headers={"Accept": "text/html"}, attempts=1, timeout=10)
            out["site"] = {"ok": True, "note": "kick.com answers"}
        except Exception as exc:
            out["site"] = {"ok": False, "note": why(exc) + (" (Cloudflare or this network refuses it)"
                                                            if why(exc) == "403" else "")}
    src = channel_src or (lst["rows"][0]["login"] if lst["rows"] else None)
    if not src:
        out["channel"] = {"ok": False, "note": "no live GTA VI stream on Kick to try (pass --channel kick:<slug>)"}
        return {"steps": out, "source": None, "ok": False}
    src = src if is_kick(src) else "kick:" + src
    st = live_status(src)
    out["channel"] = {"ok": bool(st.get("live")),
                      "note": (f"{src} live, {st.get('viewers', 0):,} viewers" if st.get("live")
                               else st.get("error") and why(RuntimeError(st["error"])) or f"{src} is not live")}
    if st.get("live") and st.get("playback_url"):
        try:
            v = best_variant(T.http(st["playback_url"], attempts=1, timeout=15).decode("utf-8", "replace"),
                             st["playback_url"])
            media = T.http(v, attempts=1, timeout=15).decode("utf-8", "replace")
            seg = next((l.strip() for l in media.splitlines() if l.strip() and not l.startswith("#")), None)
            if not seg:
                raise RuntimeError("media playlist has no segments")
            n = len(T.http(urllib.parse.urljoin(v, seg), attempts=1, timeout=20))
            out["record"] = {"ok": n > 0, "note": f"got a {n / 1e6:.1f} MB segment ({urllib.parse.urlparse(v).hostname})"}
        except Exception as exc:
            out["record"] = {"ok": False, "note": f"{why(exc)} ({str(exc)[:60]})"}
    else:
        out["record"] = {"ok": False, "note": "no playback URL"}
    if st.get("chatroom_id") or st.get("live"):
        chat = KickChat(src, st.get("chatroom_id"))
        chat.start()
        waited = 0.0
        while waited < seconds:
            sleep(min(5.0, seconds - waited))
            waited += 5.0
        chat.stop_flag = True
        out["chat"] = {"ok": chat.total > 0,
                       "note": (f"{chat.total} messages in {seconds:.0f} s" if chat.total else
                                f"no messages in {seconds:.0f} s" + (f" ({chat.why})" if chat.why else
                                                                     "" if chat.connected else " (never connected)"))}
    else:
        out["chat"] = {"ok": False, "note": "no chatroom id"}
    return {"steps": out, "source": src, "ok": all(s["ok"] for s in out.values())}


def check_line(res: dict) -> str:
    """ONE line: each step ✓/✗ with what happened (pure)."""
    order = ("site", "listing", "channel", "record", "chat")
    parts = [f"{k} {'✓' if res['steps'][k]['ok'] else '✗'} {res['steps'][k]['note']}" for k in order if k in res["steps"]]
    verdict = "OK — Kick works from this machine" if res["ok"] else "NOT READY"
    return f"kick-check: {verdict} · " + " · ".join(parts)
