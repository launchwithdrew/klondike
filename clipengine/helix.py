"""Twitch's official clips feed (Helix Get Clips, A1): every clip made in the last 2 hours, by creation time.

The public clip list the engine reads otherwise is a channel's top 100 by views, so on a busy channel a clip made a
minute ago can't appear until it out-views the 100th (`funnel` names those "too full" checks). Helix lists clips by
WHEN they were made, so a 10-view clip shows the minute it exists.

Helix needs an app pass (a Twitch "app access token": reads public data only, no account access, ~60 days). Two
ways to give the engine one, neither puts the app's secret in a file:
  - an agent whose secret vault can only attach a secret to its own web requests (Muse): the agent asks Twitch for
    the pass itself — POST https://id.twitch.tv/oauth2/token?client_id=<ID>&grant_type=client_credentials with the
    vault adding `client_secret` as a query parameter (Twitch reads it there: checked Nov 24) — and hands the reply to
    `clipengine twitch-pass --set` on stdin. The pass is checked with Twitch and kept in .private/twitch_pass.json
    (owner-only, never committed).
  - a machine with environment variables: TWITCH_CLIENT_SECRET (and the ID) — the engine asks for passes itself.
The app's Client ID is not a secret (Twitch sends it with every request): `"twitch_client_id"` in config/ops.json,
or TWITCH_CLIENT_ID. A week before the pass runs out — or the moment Twitch refuses it — attention says so, once a
day, and the engine falls back to the public list until there's a new one.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
import urllib.parse

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASS = os.path.join(REPO, ".private", "twitch_pass.json")
IDS = os.path.join(REPO, "runs", "tier1", "helix_ids.json")
API = "https://api.twitch.tv/helix"
WARN_DAYS = 7
DAY = 86400.0


def client_id() -> str | None:
    if os.environ.get("TWITCH_CLIENT_ID"):
        return os.environ["TWITCH_CLIENT_ID"].strip()
    try:
        from . import ops as O
        return (O.config().get("twitch_client_id") or "").strip() or None
    except Exception:
        return None


def _http(url: str, data: bytes | None = None, headers: dict | None = None) -> dict:
    from . import twitch as T
    return json.loads(T.http(url, data=data, headers=headers or {}, attempts=2, timeout=20))


def validate(token: str) -> dict:
    """Twitch's own check of a pass: {"client_id", "expires_in", ...}; raises when it's refused."""
    return _http("https://id.twitch.tv/oauth2/validate", headers={"Authorization": f"OAuth {token}"})


def parse(raw: str) -> str:
    """A pass from what the agent pasted: the bare token, or Twitch's whole JSON reply."""
    raw = (raw or "").strip()
    if raw.startswith("{"):
        raw = str(json.loads(raw).get("access_token") or "")
    if not raw or any(c.isspace() for c in raw):
        raise ValueError("that isn't a Twitch pass (expected the access_token, or Twitch's whole reply)")
    return raw


def _save(rec: dict, path: str | None = None) -> None:
    path = path or PASS
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(rec, fh)
    os.replace(path + ".tmp", path)


def set_pass(raw: str, path: str | None = None, check=validate, now: float | None = None) -> dict:
    """Check a pass with Twitch (it must belong to our app) and keep it; returns what was saved."""
    now = now or time.time()
    token = parse(raw)
    info = check(token)
    cid = client_id()
    if cid and info.get("client_id") and info["client_id"] != cid:
        raise ValueError(f"that pass belongs to another Twitch app ({info['client_id'][:6]}…), not ours ({cid[:6]}…)")
    rec = {"token": token, "client_id": info.get("client_id") or cid, "set_at": now,
           "expires_at": now + float(info.get("expires_in") or 0)}
    _save(rec, path)
    return rec


def load(path: str | None = None) -> dict | None:
    try:
        return json.load(open(path or PASS))
    except (OSError, ValueError):
        return None


def mint(cid: str, secret: str) -> dict:
    """A machine with TWITCH_CLIENT_SECRET in its environment asks for a pass itself."""
    body = urllib.parse.urlencode({"client_id": cid, "client_secret": secret,
                                   "grant_type": "client_credentials"}).encode()
    return _http("https://id.twitch.tv/oauth2/token", data=body,
                 headers={"Content-Type": "application/x-www-form-urlencoded"})


def token(path: str | None = None, now: float | None = None) -> str | None:
    """A usable pass, or None (no pass, expired, refused): callers then use the public list."""
    now = now or time.time()
    rec = load(path)
    if rec and not rec.get("refused_at") and rec.get("expires_at", 0) > now + 60:
        return rec["token"]
    secret, cid = os.environ.get("TWITCH_CLIENT_SECRET"), client_id()
    if secret and cid:
        try:
            r = mint(cid, secret)
            rec = {"token": r["access_token"], "client_id": cid, "set_at": now,
                   "expires_at": now + float(r.get("expires_in") or 0)}
            _save(rec, path)
            return rec["token"]
        except Exception:
            return None
    return None


def refused(path: str | None = None, now: float | None = None) -> None:
    """Twitch said 401 to the pass: stop using it, and say so (attention)."""
    rec = load(path)
    if rec and not rec.get("refused_at"):
        rec["refused_at"] = now or time.time()
        _save(rec, path)


def get(endpoint: str, params: list[tuple[str, str]], path: str | None = None) -> dict:
    tok, cid = token(path), client_id()
    if not tok or not cid:
        raise RuntimeError("no Twitch pass")
    url = f"{API}/{endpoint}?" + urllib.parse.urlencode(params)
    try:
        return _http(url, headers={"Client-Id": cid, "Authorization": f"Bearer {tok}"})
    except RuntimeError as exc:
        if "401" in str(exc):
            refused(path)
        raise


def user_ids(logins: list[str], cache: str | None = None, fetch=None) -> dict[str, str]:
    """{login: broadcaster id} — cached (ids never change), asked 100 at a time for the rest."""
    cache = cache or IDS
    try:
        have = json.load(open(cache))
    except (OSError, ValueError):
        have = {}
    want = [l for l in dict.fromkeys(l.lower() for l in logins) if l not in have]
    for i in range(0, len(want), 100):
        d = (fetch or get)("users", [("login", l) for l in want[i:i + 100]])
        for u in d.get("data") or []:
            have[u["login"].lower()] = u["id"]
    if want:
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        with open(cache, "w") as fh:
            json.dump(have, fh)
    return {l.lower(): have[l.lower()] for l in logins if l.lower() in have}


def recent_clips(broadcaster_id: str, since: float, fetch=None) -> list[dict]:
    """Clips made since `since`, in the shape the public list uses ({slug, title, createdAt, viewCount})."""
    started = dt.datetime.fromtimestamp(since, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    d = (fetch or get)("clips", [("broadcaster_id", broadcaster_id), ("started_at", started), ("first", "100")])
    return [{"slug": c["id"], "title": c.get("title") or "", "createdAt": c["created_at"],
             "viewCount": int(c.get("view_count") or 0), "durationSeconds": float(c.get("duration") or 0)}
            for c in d.get("data") or []]


def status(path: str | None = None, now: float | None = None) -> tuple[str, str]:
    """(state, one plain line): state is ok | expiring | refused | expired | none."""
    now = now or time.time()
    rec = load(path)
    env = bool(os.environ.get("TWITCH_CLIENT_SECRET") and client_id())
    if not rec:
        return ("ok" if env else "none",
                "Twitch pass: made by this machine (TWITCH_CLIENT_SECRET)" if env else
                "Twitch pass: none — fresh clips come from the public top-100 list (busy channels hide new ones)")
    days = (rec.get("expires_at", 0) - now) / DAY
    if rec.get("refused_at"):
        return "refused", "Twitch pass: REFUSED by Twitch — get a new one (clipengine twitch-pass --help)"
    if days <= 0:
        return "expired", "Twitch pass: EXPIRED — get a new one (clipengine twitch-pass --help)"
    until = dt.datetime.fromtimestamp(rec["expires_at"], dt.timezone.utc).strftime("%b %d")
    if days <= WARN_DAYS:
        return "expiring", f"Twitch pass: expires in {days:.0f} day(s) ({until}) — get a new one (clipengine twitch-pass --help)"
    return "ok", f"Twitch pass: good until {until} ({days:.0f} days) — fresh clips seen the minute they're made"


RENEW = """How to give the engine a Twitch pass (one request, every ~60 days):
  1. Ask Twitch for one, with the app's secret added from your vault as the query parameter `client_secret`:
       POST https://id.twitch.tv/oauth2/token?client_id={cid}&grant_type=client_credentials
  2. Hand Twitch's whole reply (or just its access_token) to the engine on stdin — never as a command argument:
       … | .venv/bin/python -m clipengine twitch-pass --set
  It's checked with Twitch and kept in .private/twitch_pass.json (owner-only, never committed)."""
