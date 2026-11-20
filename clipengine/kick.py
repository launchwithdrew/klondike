"""Kick sources ("kick:<slug>") behind the same interfaces as Twitch.

Two routes, chosen per machine:
  official API    when KICK_CLIENT_ID / KICK_CLIENT_SECRET are in the environment: an app token
                  (id.kick.com, client_credentials) and api.kick.com/public/v1 — `channels?slug=` for live
                  status, title, viewers, category (up to 50 creators in one call), `livestreams` for the
                  radar. It gives NO playback URL and NO chatroom id (the `stream.url`/`key` fields there are
                  the broadcaster's own ingest, empty for anyone else), so recording and chat still need:
  website         kick.com/api/v2/channels/<slug>: `livestream` while live, `playback_url` (the stream's HLS
                  master on Amazon IVS) and the chatroom id. Cloudflare refuses some machines (403); then
                  live status still works through the official API, but recording does not.

  recording       live.record() with this resolver: the best video variant of that master playlist,
                  timed by #EXT-X-PROGRAM-DATE-TIME like Twitch (no stitched ads to skip).
  chat            Kick's public Pusher websocket, channel chatrooms.<chatroom id>.v2, event
                  App\\Events\\ChatMessageEvent — anonymous, read-only. KickChat has the sentinel's Chat
                  interface (connected, window, total), so tier 1 and tier 2 use it unchanged. Kick refuses it
                  from some networks (pusher 4001): the sentinel then runs audio-only.
  viewers' clips  kick.com/api/v2/channels/<slug>/clips (newest first): each clip names the livestream it
                  was cut from, its views and when it was made — tier 1 turns a fresh one with views on the
                  stream that is live now into a spike, like Twitch's viewer clips.
  saved streams   kick.com/api/v2/channels/<slug>/videos: every past broadcast (and the live one) with its
                  start, length and an HLS `source` on stream.kick.com — catch-up and the sweep draft from it.

Verified from a cloud machine (Nov 19 2026): the website listing, channel API, clips and videos listings.
NOT verified: the official API with real keys (no keys there; api.kick.com answers 401 without one, as
documented), Pusher chat (refused, 4001), and any video download (the IVS playback hosts, stream.kick.com
and clips.kick.com are blocked from that network). A machine that runs Kick sources needs kick.com,
ws-us2.pusher.com (chat), *.live-video.net (live video) and stream.kick.com (saved streams).
"""
from __future__ import annotations

import collections
import os
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
            "chatroom_id": (js.get("chatroom") or {}).get("id"), "livestream_id": ls.get("id") if live else None}


def live_status(source: str, creds: tuple[str, str] | None | bool = True) -> dict:
    """{live, title, viewers, started, game, playback_url, chatroom_id, livestream_id, route} — never raises.
    With app credentials (`creds`; True = from the environment) the official API says whether the creator is
    live; the website's channel API then adds what only it has (playback URL, chatroom id). Without them the
    website answers alone. `route` says which answered; `playback_note` says why there is no playback URL."""
    creds = env_credentials() if creds is True else creds
    off = None
    if creds:
        try:
            off = official_channels([source], creds).get(slug_of(source))
            if off is None:
                raise RuntimeError(f"no Kick channel {slug_of(source)!r}")
        except Exception as exc:
            off = {"error": f"official API {why(exc)}"}
    if off and "error" not in off and not off["live"]:
        return {**off, "route": "official API"}           # offline: nothing more to ask kick.com
    try:
        web = live_from_api(channel(source))
    except Exception as exc:
        msg = str(exc)
        if "403" in msg:
            msg += " — kick.com refused this client (Cloudflare); run Kick sources from a machine it allows"
        web = {"live": False, "error": msg[:200]}
    if off and "error" not in off:
        out = {**off, "route": "official API"}
        if out["live"] and not web.get("error"):
            out.update(playback_url=web.get("playback_url"), chatroom_id=web.get("chatroom_id"),
                       livestream_id=web.get("livestream_id"))
        elif out["live"]:
            out["playback_note"] = ("the official API gives no playback URL and kick.com's channel API refused "
                                    f"this machine ({why(RuntimeError(web['error']))}) — can't record or read chat")
        return out
    return {**web, "route": "website" + (f" ({off['error']})" if off else "")}
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
    if st.get("live") and not st.get("playback_url"):
        raise RuntimeError(f"{source} is live but can't be recorded here: "
                           + (st.get("playback_note") or "no playback URL in kick.com's answer"))
    if not st.get("live"):
        raise RuntimeError(f"{source} is not live" + (f" ({st['error']})" if st.get("error") else ""))
    master = T.http(st["playback_url"], attempts=2, timeout=20).decode("utf-8", "replace")
    return best_variant(master, st["playback_url"])


def live_roster(roster: dict, creds: tuple[str, str] | None | bool = True) -> set[str]:
    """Kick creators on the roster that are live now: one official-API call for up to 50 of them when this
    machine has app credentials, else one website call each."""
    kicks = [k for k in roster if is_kick(k)]
    creds = env_credentials() if creds is True else creds
    if kicks and creds:
        try:
            live = set()
            for i in range(0, len(kicks), 50):
                got = official_channels(kicks[i:i + 50], creds)
                live |= {k for k in kicks[i:i + 50] if (got.get(slug_of(k)) or {}).get("live")}
            return live
        except Exception:
            pass                                         # the website route below
    return {k for k in kicks if live_status(k, creds=None).get("live")}


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


_TOKENS: dict[str, tuple[str, float]] = {}       # client id -> (app token, expires at): one token per hour, not per call


def app_token(creds: tuple[str, str], timeout: float = 10, now: float | None = None) -> str:
    """An app access token for the official API, reused until a minute before it expires."""
    now = now or time.time()
    cached = _TOKENS.get(creds[0])
    if cached and cached[1] > now + 60:
        return cached[0]
    body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": creds[0],
                                   "client_secret": creds[1]}).encode()
    js = json.loads(T.http(TOKEN_URL, data=body, attempts=1, timeout=timeout,
                           headers={"Content-Type": "application/x-www-form-urlencoded"}))
    tok = js.get("access_token")
    if not tok:
        raise RuntimeError("no access_token in Kick's token answer")
    _TOKENS[creds[0]] = (tok, now + float(js.get("expires_in") or 3600))
    return tok


def env_credentials() -> tuple[str, str] | None:
    """credentials() from this machine's environment and config/ops.json (never raises)."""
    try:
        from . import ops as O
        cfg = O.config()
    except Exception:
        cfg = {}
    return credentials(cfg)


def channel_from_official(it: dict) -> dict:
    """One official /public/v1/channels item -> the live_status shape (pure). No playback URL or chatroom id:
    the official API doesn't give them to anyone but the broadcaster."""
    st = it.get("stream") or {}
    live = bool(st.get("is_live"))
    return {"live": live, "title": it.get("stream_title") or "", "viewers": int(st.get("viewer_count") or 0) if live else 0,
            "started": st.get("start_time") if live else None, "game": (it.get("category") or {}).get("name"),
            "playback_url": None, "chatroom_id": None, "livestream_id": None,
            "lang": str(st.get("language") or "")[:2].lower()}


def official_channels(sources: list[str], creds: tuple[str, str], timeout: float = 10) -> dict[str, dict]:
    """GET api.kick.com/public/v1/channels?slug=a&slug=b… (up to 50) -> {slug: live_status shape}. Raises on failure."""
    slugs = [slug_of(s) for s in sources][:50]
    q = urllib.parse.urlencode([("slug", s) for s in slugs])
    js = _get_json(f"{OFFICIAL}/channels?{q}", {"Authorization": f"Bearer {app_token(creds, timeout)}"}, timeout)
    return {str(it.get("slug") or "").lower(): channel_from_official(it) for it in (js.get("data") or [])
            if isinstance(it, dict)}


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
            tok = app_token(creds, timeout)
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
            return {"ok": True, "via": url.split("?")[0].replace("https://", "") + (f" ({tried[0]})" if creds and tried else ""),
                    "rows": rows_from_listing(js, langs), "why": ""}
        except Exception as exc:
            tried.append(why(exc))
    reasons = list(dict.fromkeys(tried))
    return {"ok": False, "via": "", "rows": [], "why": ", ".join(reasons) or "unreachable"}


# ---- kick-check: what works from THIS machine -----------------------------------------------------

def check(game: str, langs: list[str] | None = None, cat_slug: str | None = None, channel_src: str | None = None,
          seconds: float = 60.0, creds: tuple[str, str] | None = None, sleep=time.sleep) -> dict:
    """Try each step Kick needs, in order, and say exactly which worked:
    route (official API or website, and why), site (kick.com answers), listing (the category's live streams),
    channel (the top streamer's API), record (its HLS master -> variant -> a media segment), clips (the viewers'
    clips listing), chat (Pusher messages for `seconds`). Kick refusing chat (pusher 4001) is audio mode,
    not a failure: READY when everything else works."""
    out: dict[str, dict] = {}
    out["route"] = {"ok": True, "note": route_note(creds)}
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
    st = live_status(src, creds=creds)
    out["channel"] = {"ok": bool(st.get("live")),
                      "note": (f"{src} live, {st.get('viewers', 0):,} viewers via {st.get('route', 'website')}" if st.get("live")
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
        out["record"] = {"ok": False, "note": st.get("playback_note") or "no playback URL"}
    try:
        n = recent_clips(src, pages=1)
        out["clips"] = {"ok": True, "note": f"{len(n)} viewer clip(s) today"}
    except Exception as exc:
        out["clips"] = {"ok": False, "note": why(exc)}
    if st.get("chatroom_id") or st.get("live"):
        chat = KickChat(src, st.get("chatroom_id"))
        chat.start()
        waited = 0.0
        while waited < seconds:
            sleep(min(5.0, seconds - waited))
            waited += 5.0
        chat.stop_flag = True
        if not chat.total and getattr(chat, "audio_only", False):
            # Kick refuses its chat to outside clients (pusher 4001): not a failure — Kick runs audio + viewers' clips
            out["chat"] = {"ok": True, "audio": True, "note": f"refused by Kick ({chat.why or 'pusher 4001'}) — "
                                                              "audio + viewers' clips mode"}
        else:
            out["chat"] = {"ok": chat.total > 0,
                           "note": (f"{chat.total} messages in {seconds:.0f} s" if chat.total else
                                    f"no messages in {seconds:.0f} s" + (f" ({chat.why})" if chat.why else
                                                                         "" if chat.connected else " (never connected)"))}
    else:
        out["chat"] = {"ok": False, "note": "no chatroom id"}
    return {"steps": out, "source": src, "ok": all(s["ok"] for s in out.values())}


def route_note(creds) -> str:
    """Which route this machine uses for Kick, and why (pure)."""
    if creds:
        return ("official API (KICK_CLIENT_ID/SECRET set) for live status + listing; it has no playback URL "
                "or chat, so recording and chat still use kick.com")
    return "website only (no KICK_CLIENT_ID / KICK_CLIENT_SECRET in the environment)"


def check_line(res: dict) -> str:
    """ONE line: each step ✓/✗ with what happened (pure)."""
    order = ("route", "site", "listing", "channel", "record", "clips", "chat")
    st = res["steps"]
    parts = [f"{k} {'✓' if st[k]['ok'] else '✗'} {st[k]['note']}" for k in order if k in st]
    audio = (st.get("chat") or {}).get("audio")
    verdict = ("NOT READY" if not res["ok"] else "READY (audio mode) — Kick works from this machine: audio + viewers' "
               "clips, Kick refuses chat" if audio else "OK — Kick works from this machine")
    return f"kick-check: {verdict} · " + " · ".join(parts)


# ---- viewers' clips and saved streams (kick.com/api/v2, verified Nov 19 2026) -----------------------
#
# clips   GET kick.com/api/v2/channels/<slug>/clips?cursor=0&sort=date&time=day -> {clips: [...], nextCursor}
#         each clip: id, livestream_id (the broadcast it was cut from), views/view_count, duration (s),
#         created_at (UTC, "…Z"), title. `vod_starts_at` is 0 and `started_at` empty in practice, so a clip's
#         place in its broadcast is only known for clips made WHILE it was live: the moment ends ≈ created_at.
#         Clips made later from an old broadcast (common) are skipped — their time says nothing.
# videos  GET kick.com/api/v2/channels/<slug>/videos -> [{id (= livestream id), start_time "YYYY-MM-DD HH:MM:SS"
#         UTC, duration (ms, 0 while live), is_live, source (HLS master on stream.kick.com), session_title,
#         categories: [{name, slug}]}] — the live broadcast is listed too, with a growing `source`.

CLIPS = "https://kick.com/api/v2/channels/{slug}/clips?cursor={cursor}&sort=date&time=day"
VIDEOS = "https://kick.com/api/v2/channels/{slug}/videos"


def _ts(s) -> float | None:
    import datetime as dt
    if not s or str(s).startswith("0001"):
        return None
    try:
        d = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)).timestamp()


def clip_rows(js) -> list[dict]:
    """A clips answer -> [{id, livestream_id, views, created, duration, title}], newest first (pure)."""
    out = []
    for c in (js.get("clips") if isinstance(js, dict) else js) or []:
        if not isinstance(c, dict) or not c.get("id"):
            continue
        t = _ts(c.get("created_at"))
        if t is None:
            continue
        out.append({"id": str(c["id"]), "livestream_id": str(c.get("livestream_id") or ""),
                    "views": int(c.get("views") or c.get("view_count") or 0), "created": t,
                    "duration": float(c.get("duration") or 0), "title": str(c.get("title") or "")[:120]})
    out.sort(key=lambda c: -c["created"])
    return out


def recent_clips(source: str, since: float = 0.0, pages: int = 5, timeout: float = 10) -> list[dict]:
    """Newest clips of a channel, paging back until `since` (raises on failure)."""
    out, cursor = [], "0"
    for _ in range(max(1, pages)):
        js = _get_json(CLIPS.format(slug=slug_of(source), cursor=urllib.parse.quote(cursor)), timeout=timeout)
        rows = clip_rows(js)
        out += rows
        cursor = js.get("nextCursor") if isinstance(js, dict) else None
        if not cursor or not rows or rows[-1]["created"] < since:
            break
    return [c for c in out if c["created"] >= since]


def video_rows(js) -> list[dict]:
    """A videos answer -> [{id, start, length_s, live, source, title, game}], newest first (pure)."""
    out = []
    for v in js if isinstance(js, list) else (js or {}).get("data") or []:
        if not isinstance(v, dict) or not v.get("id"):
            continue
        s = _ts(v.get("start_time") or v.get("created_at"))
        if s is None:
            continue
        src = str(v.get("source") or "")
        cats = v.get("categories") or []
        out.append({"id": str(v["id"]), "start": s, "length_s": float(v.get("duration") or 0) / 1000,
                    "live": bool(v.get("is_live")), "source": src if src.startswith("https://") else None,
                    "title": str(v.get("session_title") or "")[:120], "game": cats[0].get("name") if cats else None})
    out.sort(key=lambda v: -v["start"])
    return out


def videos(source: str, timeout: float = 10) -> list[dict]:
    return video_rows(_get_json(VIDEOS.format(slug=slug_of(source)), timeout=timeout))


def vod_end(v: dict, now: float | None = None) -> float:
    return (now or time.time()) if v["live"] else v["start"] + v["length_s"]


def vods_overlapping(source: str, since: float, until: float, now: float | None = None) -> list[dict]:
    """Saved broadcasts overlapping [since, until], with the overlap as stream offsets a/b (like Twitch's)."""
    out = []
    for v in videos(source):
        e = vod_end(v, now)
        if e > since and v["start"] < until and v["source"]:
            out.append({**v, "a": max(0.0, since - v["start"]), "b": min(e, until) - v["start"]})
    return out


def live_clip_moments(clips: list[dict], vods: list[dict], min_views: int = 10, now: float | None = None,
                      merge_s: float = 45.0) -> list[dict]:
    """Clips made while their broadcast was live, as moments in wall-clock time, nearby clips of one broadcast
    merged, most-viewed first (pure): [{vod, start, end, views, clips, titles}]."""
    byid = {v["id"]: v for v in vods}
    ms: list[dict] = []
    for c in sorted(clips, key=lambda c: c["created"]):
        v = byid.get(c["livestream_id"])
        if not v or not (v["start"] <= c["created"] <= vod_end(v, now) + 60):
            continue                                  # cut later from an old broadcast: its time says nothing
        end, start = c["created"], c["created"] - max(10.0, min(c["duration"], 60.0))
        m = ms[-1] if ms else None
        if m and m["vod"]["id"] == v["id"] and end - m["end"] <= merge_s:
            m["end"], m["views"], m["clips"] = max(m["end"], end), m["views"] + c["views"], m["clips"] + 1
            m["titles"].append(c["title"])
        else:
            ms.append({"vod": v, "start": start, "end": end, "views": c["views"], "clips": 1, "titles": [c["title"]]})
    return sorted([m for m in ms if m["views"] >= min_views], key=lambda m: -m["views"])


_LIVE_ID: dict[str, tuple[str | None, float]] = {}     # slug -> (live broadcast id, when looked up)


def live_broadcast_id(source: str, now: float | None = None, ttl: float = 300) -> str | None:
    """The id of the broadcast that is live now (from the videos listing; cached 5 min)."""
    now = now or time.time()
    slug = slug_of(source)
    hit = _LIVE_ID.get(slug)
    if hit and now - hit[1] < ttl:
        return hit[0]
    vid = next((v["id"] for v in videos(source) if v["live"]), None)
    _LIVE_ID[slug] = (vid, now)
    return vid


def crowd_nodes(source: str, now: float | None = None) -> list[dict]:
    """tier 1's crowd watch: this channel's clips of the broadcast that is live NOW, in the shape tier1.crowd_spike
    reads ({slug, createdAt, viewCount, title}). Clips cut now from an older broadcast are left out."""
    live_id = live_broadcast_id(source, now)
    if not live_id:
        return []
    import datetime as dt
    out = []
    for c in clip_rows(_get_json(CLIPS.format(slug=slug_of(source), cursor="0"))):
        if c["livestream_id"] == live_id:
            out.append({"slug": "kick-" + c["id"], "viewCount": c["views"], "title": c["title"],
                        "createdAt": dt.datetime.fromtimestamp(c["created"], dt.timezone.utc).isoformat()})
    return out


# ---- drafting from a saved broadcast (catch-up, the sweep, tier-1 spikes) ---------------------------
#
# The broadcast's HLS (stream.kick.com master -> best variant -> media playlist) is fetched for just the
# moment's stretch, written as a recording (segments/<epoch_ms>.ts + index.jsonl, what live.record writes),
# and drafted by the sentinel's own draft_moment — so publish-queued treats it like any sentinel draft.
# Segments are timed by #EXT-X-PROGRAM-DATE-TIME when the playlist has it, else by adding up #EXTINF from
# the broadcast's start. UNVERIFIED: stream.kick.com is blocked from the machine this was built on.

def vod_segments(media: str, base: str, start: float) -> list[tuple[float, float, str]]:
    """A media playlist -> [(wall start, duration, url)] (pure)."""
    from . import live as L
    pdt = list(L.SEG_RE.finditer(media))
    if pdt:
        return [(_ts(m.group(1)) or start, float(m.group(2)), urllib.parse.urljoin(base, m.group(4))) for m in pdt]
    out, t = [], start
    for m in L._SEG_ANY.finditer(media):
        d = float(m.group(1))
        out.append((t, d, urllib.parse.urljoin(base, m.group(2))))
        t += d
    return out


def vod_to_rec(vod: dict, a: float, b: float, rec_dir: str, http=None) -> int:
    """Fetch the broadcast's segments covering wall-clock [a, b] into rec_dir (a recording). Returns how many
    are there now. Raises when the broadcast's video can't be read from this machine."""
    from . import live as L
    http = http or (lambda u: T.http(u, attempts=2, timeout=30))
    master = http(vod["source"]).decode("utf-8", "replace")
    var = best_variant(master, vod["source"])
    media = master if var == vod["source"] else http(var).decode("utf-8", "replace")
    segs = [s for s in vod_segments(media, var, vod["start"]) if s[0] + s[1] > a - 1 and s[0] < b + 1]
    if not segs:
        raise RuntimeError("the saved broadcast doesn't reach that moment yet")
    seg_dir = os.path.join(rec_dir, "segments")
    os.makedirs(seg_dir, exist_ok=True)
    meta = os.path.join(rec_dir, "recording.json")
    if not os.path.exists(meta):
        json.dump({"login": L.source_name("kick:" + vod.get("slug", "")) if vod.get("slug") else "", "source":
                   "kick vod", "started": time.time()}, open(meta, "w"))
    cache: dict = {}
    n = 0
    with open(os.path.join(rec_dir, "index.jsonl"), "a") as idx:
        for t0, d, u in segs:
            f = f"{int(t0 * 1000)}.ts"
            path = os.path.join(seg_dir, f)
            if not os.path.exists(path):
                data = http(u)
                init = L.init_segment(media, var, cache) if "#EXT-X-MAP" in media else b""
                with open(path, "wb") as fh:
                    fh.write(init + data)
                idx.write(json.dumps({"t": t0, "d": d, "f": f}) + "\n")
            n += 1
    return n


def draft_vod_moment(source: str, vod: dict, moment: dict, via: str, draft=None, fetch=None) -> dict | None:
    """Draft one moment of a saved Kick broadcast into the queue (runs/catchup-kick-<slug>/queue.jsonl).
    `moment`: {fire, start, stop, p} (a chat spike) or with `crowd_end` (viewers' clip end). Never raises."""
    from . import live as L
    from . import sentinel as SN
    REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    login = L.source_name(source)
    rec = os.path.join(REPO, "runs", f"catchup-{login}")
    pay = moment.get("crowd_end", moment.get("fire", moment["stop"])) or moment["stop"]
    try:
        (fetch or vod_to_rec)({**vod, "slug": slug_of(source)}, pay - 35, pay + 20, rec)
    except Exception as exc:
        print(f"[catchup] {source} broadcast {vod['id']}: video unreadable here ({str(exc)[:90]})", flush=True)
        return None
    try:
        e = (draft or SN.draft_moment)(rec, login, {**moment, "source": via, "game": vod.get("game")}, wait_s=0)
    except Exception as exc:
        print(f"[catchup] {source}: draft failed ({str(exc)[:90]})", flush=True)
        return None
    return e


def catchup(source: str, since: float, until: float, keep: int = 3, min_views: int = 10,
            via: str = "catch-up (saved stream)", now: float | None = None, draft=None) -> list[dict]:
    """Kick's catch-up for [since, until]: there is no chat replay on Kick, so the moments are the viewers'
    clips made while it was live; the best `keep` are drafted from the saved broadcast."""
    vods = vods_overlapping(source, since, until, now)
    if not vods:
        return []
    clips = recent_clips(source, since=since - 120)
    ms = [m for m in live_clip_moments(clips, vods, min_views, now) if since <= m["end"] <= until + 60]
    from . import autopilot as AP
    from . import live as L
    rows = AP._queue_rows()
    ms = [m for m in ms if not AP._overlaps_any(L.source_name(source), m["start"] - 20, m["end"] + 10, rows)]
    out = []
    for m in ms[:keep]:
        mom = {"fire": m["end"], "start": m["start"] - 20, "stop": m["end"] + 10, "p": 0.0, "crowd_end": m["end"],
               "crowd_new": round(m["views"] / max(1.0, ((now or time.time()) - m["end"]) / 60) / 5, 2)}
        e = draft_vod_moment(source, m["vod"], mom, via + " 🔥", draft=draft)
        if e:
            out.append(e)
    return out


def catchup_spike(sp: dict, draft=None, now: float | None = None) -> list[dict]:
    """A tier-1 spike on a Kick creator (chat or viewers' clip) no sentinel covered: draft it from the live /
    saved broadcast."""
    src = sp["login"]
    v = next((v for v in vods_overlapping(src, sp["start"], sp["stop"], now)), None)
    if not v:
        print(f"[catchup] {src}: no saved broadcast covers the spike at {sp['fire']:.0f}", flush=True)
        return []
    mom = {k: sp[k] for k in ("fire", "start", "stop", "p")}
    if sp.get("kind") == "crowd":
        mom["crowd_end"] = sp["peak"]
    e = draft_vod_moment(src, v, mom, "catch-up (tier-1 " + ("viewers' clip" if sp.get("kind") == "crowd"
                                                              else "chat spike") + ")", draft=draft)
    return [e] if e else []
