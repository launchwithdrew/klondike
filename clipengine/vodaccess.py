"""Subscriber-only saved streams — remembered, so nothing keeps failing on them.

Some creators keep their past broadcasts for subscribers: the VOD exists, but its playlist answers
403. Their saved streams can't be scanned, so for the engine they are live-only creators:

  * the sentinel planner gives them a full sentinel the moment they're live (like "vods": false) —
    what isn't recorded live is gone;
  * the sweep, catch-up and tier-1 spike drafts skip them quietly instead of failing every time.

It's learned, not configured: data/vod_access.json {login: {"vod": id, "at": epoch}}. An entry expires
after RECHECK_DAYS so a creator who opens their VODs again is noticed (one scan attempt re-marks them).
"""
from __future__ import annotations

import json
import os
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(REPO, "data", "vod_access.json")
RECHECK_DAYS = 3.0


class SubOnlyVod(RuntimeError):
    """A VOD whose playlist answered 403: subscriber-only (or otherwise restricted) saved stream."""


def is_restricted_error(exc: BaseException) -> bool:
    return isinstance(exc, SubOnlyVod) or ("403" in str(exc) and "usher.ttvnw.net/vod" in str(exc))


def _load(path: str | None = None) -> dict:
    try:
        d = json.load(open(path or PATH))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def mark(login: str, vod_id: str, now: float | None = None, path: str | None = None) -> bool:
    """Remember that `login`'s saved streams are subscriber-only. True the first time (until it expires)."""
    login = (login or "").lower()
    if not login:
        return False
    d = _load(path)
    new = not restricted(login, now, path)
    d[login] = {"vod": str(vod_id), "at": now or time.time()}
    os.makedirs(os.path.dirname(path or PATH), exist_ok=True)
    tmp = (path or PATH) + ".tmp"
    json.dump(d, open(tmp, "w"), indent=1, sort_keys=True)
    os.replace(tmp, path or PATH)
    return new


def restricted(login: str, now: float | None = None, path: str | None = None) -> bool:
    e = _load(path).get((login or "").lower())
    return bool(e) and (now or time.time()) - float(e.get("at", 0)) < RECHECK_DAYS * 86400


def restricted_set(now: float | None = None, path: str | None = None) -> set[str]:
    now = now or time.time()
    return {l for l, e in _load(path).items() if now - float(e.get("at", 0)) < RECHECK_DAYS * 86400}


def live_only(login: str, creator: dict | None, now: float | None = None, path: str | None = None) -> bool:
    """No saved streams to fall back on: "vods": false in the roster, or subscriber-only VODs (learned)."""
    return (creator or {}).get("vods") is False or restricted(login, now, path)
