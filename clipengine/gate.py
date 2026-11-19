"""Distribution gate — winners only, within each platform's budget.

For a graded clip, decide per platform: post now, post at a scheduled time
(the gap since the last post hasn't passed), or don't post there at all.

  below the platform's minimum score   → never there
  daily cap reached (rolling 24 h)      → not there today
  gap since the last post not passed    → scheduled for when it has
  moment older than stale_after_h       → only if score ≥ stale_min_score

What it has learned (data/weights.json, from `clipengine learn`) adjusts that:

  thresholds   each platform's minimum moves ±5 a day at most when posts flop / don't
  fit          a streamer who does better on one platform than overall gets up to +8
               there (and down to −8 where they do worse)
  format       a layout / length / headline pattern that does better or worse for us: up to ±5
  windows      a clip that isn't time-critical (moment older than time_critical_h) waits for the best
               open posting hour (learned per platform; prime time before there's data), up to
               window_max_wait_h; fresh moments still post as soon as a slot opens
  exploration  a streamer we know little about (< 3 reads) may post up to 5 points under
               a platform's bar, at most once in every 10 posts on that platform —
               otherwise the engine only ever learns about who it already posts

TikTok and Instagram judge an account partly by its average results, so a weak
post costs more than it earns; YouTube Shorts tolerates more volume. The
numbers live in config/distribution.json (starting values, tuned by learn).

Counts come from ledger.jsonl (live posts) plus anything already dropped and
waiting in outbox/ (its post.json schedule), so two drops never claim one slot.
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(REPO, "config", "distribution.json")
LEDGER = os.path.join(REPO, "ledger.jsonl")
OUTBOX = os.path.join(REPO, "outbox")

DEFAULTS = {
    "platforms": {
        "tiktok": {"min_score": 75, "daily_cap": 4, "gap_min": 120},
        "instagram": {"min_score": 75, "daily_cap": 4, "gap_min": 120},
        "youtube": {"min_score": 60, "daily_cap": 8, "gap_min": 45},
    },
    "stale_after_h": 6,
    "stale_min_score": 85,
    "max_wait_h": 8,
    "timezone": "America/New_York",     # posting hours are learned and scheduled in this local time
    "prime_hours": [17, 18, 19, 20, 21],  # until a platform has enough reads to learn its own hours
    "time_critical_h": 2,               # a moment younger than this posts as soon as a slot opens
    "window_max_wait_h": 6,             # an older one waits at most this long for a good hour
}


def config() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    if os.path.exists(CONFIG):
        user = json.load(open(CONFIG))
        for p, v in (user.get("platforms") or {}).items():
            cfg["platforms"][p] = {**cfg["platforms"].get(p, {}), **v}
        for k in ("stale_after_h", "stale_min_score", "max_wait_h", "timezone", "prime_hours", "time_critical_h",
                  "window_max_wait_h"):
            if k in user:
                cfg[k] = user[k]
        import time as _t
        for o in user.get("temporary") or []:         # e.g. a launch-night budget: applies until it expires
            try:
                until = dt.datetime.fromisoformat(str(o["until"]).replace("Z", "+00:00")).timestamp()
            except (KeyError, ValueError):
                continue
            if _t.time() < until:
                for p, v in (o.get("platforms") or {}).items():
                    cfg["platforms"][p] = {**cfg["platforms"].get(p, {}), **v}
    return cfg


def _ts(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def booked(ledger_path: str = LEDGER, outbox: str = OUTBOX) -> dict[str, list[float]]:
    """Every post time per platform: live posts (ledger) + drops waiting in outbox."""
    times: dict[str, list[float]] = {}
    if os.path.exists(ledger_path):
        for line in open(ledger_path):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("status") != "live":
                continue
            t = _ts(r.get("posted_at") or r.get("at") or r.get("time"))
            if t is None:
                t = _ts(r.get("moment_at"))
                if t is not None and r.get("moment_to_live_min") is not None:
                    t += 60 * float(r["moment_to_live_min"])
            if t is not None:
                times.setdefault(r["platform"], []).append(t)
    for pj in glob.glob(os.path.join(outbox, "*", "post.json")):
        try:
            p = json.load(open(pj))
        except (OSError, ValueError):
            continue
        sched = p.get("schedule") or {}
        dropped = _ts(p.get("dropped_at")) or os.path.getmtime(pj)
        for plat in p.get("platforms") or []:
            times.setdefault(plat, []).append(_ts(sched.get(plat)) or dropped)
    return times


def _learned() -> dict:
    try:
        return json.load(open(os.path.join(REPO, "data", "weights.json")))
    except (OSError, ValueError):
        return {}


def _explored_recently(plat: str, clipped: list[dict], last_n: int = 10) -> bool:
    mine = [r for r in clipped if plat in (r.get("platforms") or [])][-last_n:]
    return any(plat in (r.get("explore") or []) for r in mine)


def posting_hours(learned: dict, plat: str, cfg: dict) -> list[int]:
    w = ((learned.get("windows") or {}).get(plat) or {})
    return list(w.get("hours") or cfg.get("prime_hours") or DEFAULTS["prime_hours"])


def next_window(t: float, hours: list[int], tz: str) -> float:
    """The earliest time ≥ t whose local hour is one of `hours` (t itself if it already is)."""
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(tz)
    except Exception:
        zone = dt.timezone.utc
    if not hours:
        return t
    local = dt.datetime.fromtimestamp(t, zone)
    if local.hour in hours:
        return t
    top = local.replace(minute=0, second=0, microsecond=0)
    for i in range(1, 25):
        cand = top + dt.timedelta(hours=i)
        cand = dt.datetime.fromtimestamp(cand.timestamp(), zone)      # DST-safe: re-derive the local hour
        if cand.hour in hours:
            return cand.timestamp()
    return t


def decide(score: float, moment_ts: float | None, now: float | None = None,
           times: dict[str, list[float]] | None = None, cfg: dict | None = None,
           login: str | None = None, learned: dict | None = None, clipped: list[dict] | None = None,
           clip: dict | None = None) -> dict:
    """→ {platform: {"action": "now"|"scheduled"|"skip", "at": unix|None, "why": str, "explore"?: True}}

    clip (optional): {"title", "length_s", "layout"} — the format nudge from what we've learned."""
    import time
    cfg = cfg or config()
    now = now or time.time()
    times = booked() if times is None else times
    learned = _learned() if learned is None else learned
    if clipped is None:
        try:
            from .publish import load_clipped
            clipped = load_clipped()
        except Exception:
            clipped = []
    login = (login or "").lower()
    stale = moment_ts is not None and (now - moment_ts) / 3600 > cfg["stale_after_h"]
    known = (learned.get("streamer_posts") or {}).get(login, 0)
    fmt_adj = 0.0
    if clip:
        from .learn import format_factor
        fmt_adj = max(-5.0, min(5.0, 25.0 * (format_factor(learned, clip.get("title"), clip.get("length_s"),
                                                              clip.get("layout"), clip.get("caption_style")) - 1.0)))
    for_window = moment_ts is not None and (now - moment_ts) / 3600 >= cfg.get("time_critical_h", 2)
    tz = cfg.get("timezone", DEFAULTS["timezone"])
    out = {}
    for plat, rule in cfg["platforms"].items():
        ts = sorted(times.get(plat, []))
        bar = rule["min_score"] + int((learned.get("thresholds") or {}).get(plat, 0))
        adj = 0.0
        f, base = ((learned.get("fit") or {}).get(plat) or {}).get(login), (learned.get("streamers") or {}).get(login)
        if f and base:
            adj = max(-8.0, min(8.0, 8.0 * (f / base - 1.0)))
        eff = round(score + adj + fmt_adj, 1)
        explore = False
        if eff < bar:
            if login and known < 3 and eff >= bar - 5 and not _explored_recently(plat, clipped):
                explore = True
            else:
                why = (f"score {eff:g} < {plat} minimum {bar}" + (f" (fit {adj:+.0f})" if adj else "")
                       + (f" (format {fmt_adj:+.0f})" if fmt_adj else ""))
                out[plat] = {"action": "skip", "at": None, "why": why}
                continue
        if stale and score < cfg["stale_min_score"]:
            out[plat] = {"action": "skip", "at": None,
                         "why": f"moment is over {cfg['stale_after_h']} h old and score < {cfg['stale_min_score']}"}
            continue
        day = [t for t in ts if t > now - 86400]          # last 24 h + anything already scheduled
        cap = rule["daily_cap"]
        try:                                              # new accounts warm up: fewer posts at first
            from . import brand as B
            pack = B.load()
            if pack.get("warmup"):
                from .support import warmup_cap
                cap = warmup_cap(plat, cap, now=now, started=pack.get("started"))
        except Exception:
            pass
        if len(day) >= cap:
            why = f"{plat} daily cap {cap} reached" + (" (new-account warm-up)" if cap < rule["daily_cap"] else "")
            out[plat] = {"action": "skip", "at": None, "why": why}
            continue
        last = max(ts) if ts else None
        slot = now if last is None else max(now, last + 60 * rule["gap_min"])
        windowed = False
        if for_window and slot - now <= cfg["max_wait_h"] * 3600:      # not time-critical: wait for a good hour
            best = next_window(slot, posting_hours(learned, plat, cfg), tz)
            if best != slot and best - now <= min(cfg["max_wait_h"], cfg.get("window_max_wait_h", 6)) * 3600:
                slot, windowed = best, True
        if slot - now > cfg["max_wait_h"] * 3600:
            out[plat] = {"action": "skip", "at": None, "why": f"next {plat} slot is over {cfg['max_wait_h']} h away"}
        elif slot - now < 60:
            out[plat] = {"action": "now", "at": None, "why": "exploring a new streamer" if explore else "slot open"}
        else:
            out[plat] = {"action": "scheduled", "at": slot,
                         "why": (f"best posting hour ({dt.datetime.fromtimestamp(slot, _zone(tz)):%H}:00 {tz.split('/')[-1]})"
                                 if windowed else f"{rule['gap_min']} min gap after the last {plat} post")}
        if explore and out[plat]["action"] != "skip":
            out[plat]["explore"] = True
    return out


def _zone(tz: str):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(tz)
    except Exception:
        return dt.timezone.utc


def summary(decision: dict) -> str:
    parts = []
    for p, d in decision.items():
        if d["action"] == "now":
            parts.append(f"{p}: now")
        elif d["action"] == "scheduled":
            parts.append(f"{p}: {dt.datetime.fromtimestamp(d['at'], dt.timezone.utc):%H:%M} UTC")
        else:
            parts.append(f"{p}: skip ({d['why']})")
    return " · ".join(parts)
