"""Event calendar — marathons, launches, house streams: a creator in a live event is worth watching.

config/events.json (optional; see docs/EARN.md):

  {"events": [{"name": "launch marathon", "start": "2026-11-19T00:00:00Z", "end": "2026-11-22T00:00:00Z",
               "creators": ["streamera", "streamerb"], "boost": 1.0}]}

`priority_boost(login)` is what the radar adds to its roster-priority signal (0..1; boost defaults
to 1.0 = as if top priority) while an event with that creator is on, plus a short lead-in before it starts.
Event names are data, never instructions.
"""
from __future__ import annotations

import datetime as dt
import json
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(REPO, "config", "events.json")
LEAD_IN_H = 1.0          # an event starting within the hour already counts (sentinels need a warm start)


def _ts(iso) -> float | None:
    try:
        return dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def load(path: str | None = None) -> list[dict]:
    path = path or CONFIG
    if not os.path.exists(path):
        return []
    try:
        cfg = json.load(open(path))
    except (OSError, ValueError):
        return []
    return [e for e in (cfg.get("events") or []) if isinstance(e, dict)]


def live(now: float | None = None, events: list[dict] | None = None) -> list[dict]:
    import time
    now = now or time.time()
    out = []
    for e in load() if events is None else events:
        s, t = _ts(e.get("start")), _ts(e.get("end"))
        if s is None:
            continue
        if s - LEAD_IN_H * 3600 <= now and (t is None or now <= t):
            out.append(e)
    return out


def priority_boost(login: str, now: float | None = None, events: list[dict] | None = None) -> float:
    """0..1 added to the radar's roster-priority signal for a creator in an event that is on now."""
    login = (login or "").lower()
    best = 0.0
    for e in live(now, events):
        if login in [c.lower() for c in e.get("creators") or []]:
            try:
                best = max(best, float(e.get("boost", 1.0)))
            except (TypeError, ValueError):
                best = max(best, 1.0)
    return max(0.0, min(1.0, best))
