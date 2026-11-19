"""Radar — where to point the engine, decided fresh every run.

Instead of a fixed roster, the radar reads the whole Twitch category:

  * every live stream in the category (in our languages), with viewers,
  * every viewer clip in the category from the last day — who, when, views,
    and how fast those views are arriving,

and scores each streamer as an *opportunity* (0–100):

  audience   live viewers now, log scale                         (30)
  heat       viewer-clip views per minute, last `heat_window_h`  (35)
  history    their clips' views with us vs our median            (20)
  priority   roster priority (config/creators.json)              (15)

Streamers off the roster appear when they earn it. It also lists the hottest
viewer-clipped *moments* across the whole category — the crowd has already
voted on those — with the exact command to cut each one.

Weights, game, languages and thresholds live in config/radar.json; the
history signal comes from data/weights.json (written by `clipengine learn`).
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import math
import os

from . import twitch as T
from . import untrusted as UT

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(REPO, "config", "radar.json")
WEIGHTS = os.path.join(REPO, "data", "weights.json")

DEFAULTS = {
    "game": "Grand Theft Auto VI",
    "languages": ["en"],
    "top_streams": 40,
    "min_viewers": 3000,
    "heat_window_h": 2,
    "trending_window_h": 6,
    "weights": {"audience": 30, "heat": 35, "history": 20, "priority": 15},
    "audience_cap": 300000,
    "heat_cap_vpm": 200,
    "act_score": 45,
}

_STREAMS = """query($name: String!, $first: Int, $langs: [String!]) { game(name: $name) {
  streams(first: $first, options: {languages: $langs}) { edges { node {
    viewersCount title createdAt broadcaster { login displayName } } } } } }"""

_CLIPS = """query($name: String!, $langs: [Language!], $first: Int) { game(name: $name) {
  clips(first: $first, criteria: {period: LAST_DAY, sort: VIEWS_DESC, languages: $langs}) { edges { node {
    slug title viewCount createdAt durationSeconds videoOffsetSeconds
    broadcaster { login displayName } video { id } } } } } }"""


def config() -> dict:
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG):
        user = json.load(open(CONFIG))
        cfg.update({k: v for k, v in user.items() if not k.startswith("_")})
        cfg["weights"] = {**DEFAULTS["weights"], **user.get("weights", {})}
    return cfg


def learned_weights() -> dict:
    """data/weights.json → {"streamers": {login: factor}}; factor 1.0 = our median."""
    try:
        return json.load(open(WEIGHTS))
    except (OSError, ValueError):
        return {}


def live_streams(cfg: dict) -> list[dict]:
    d = T.gql(_STREAMS, {"name": cfg["game"], "first": cfg["top_streams"],
                         "langs": [l.lower() for l in cfg["languages"]]})
    out = []
    for e in (((d.get("game") or {}).get("streams") or {}).get("edges") or []):
        n = e["node"]
        b = n.get("broadcaster") or {}
        if not b.get("login"):
            continue
        out.append({"login": b["login"].lower(), "name": b.get("displayName") or b["login"],
                    "viewers": int(n.get("viewersCount") or 0), "title": n.get("title") or "",
                    "started": n.get("createdAt")})
    return out


def category_clips(cfg: dict) -> list[dict]:
    """The day's most-viewed viewer clips in the category. A flaky network that keeps cutting the big
    response short gets a smaller one instead (and the radar never dies over the clips list)."""
    d = {}
    for first in (100, 40, 15):
        try:
            d = T.gql(_CLIPS, {"name": cfg["game"], "langs": [l.upper() for l in cfg["languages"]], "first": first})
            break
        except RuntimeError as exc:
            print(f"[radar] viewer clips (first {first}) failed: {str(exc)[:80]} — trying a smaller list", file=sys.stderr)
    out = []
    for e in (((d.get("game") or {}).get("clips") or {}).get("edges") or []):
        n = e["node"]
        b = n.get("broadcaster") or {}
        if not b.get("login"):
            continue
        out.append({"slug": n["slug"], "title": n.get("title") or "", "views": int(n.get("viewCount") or 0),
                    "created": n["createdAt"], "duration": float(n.get("durationSeconds") or 0),
                    "offset": n.get("videoOffsetSeconds"), "video": (n.get("video") or {}).get("id"),
                    "login": b["login"].lower(), "name": b.get("displayName") or b["login"]})
    return out


def _age_min(iso: str, now: dt.datetime) -> float:
    try:
        return max(1.0, (now - dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))).total_seconds() / 60)
    except (ValueError, AttributeError):
        return 9999.0


def _unit_log(x: float, cap: float) -> float:
    """0..1 on a log scale, 1 at `cap`."""
    if x <= 0:
        return 0.0
    return max(0.0, min(1.0, math.log10(1 + x) / math.log10(1 + cap)))


def _event_boost(login: str, now: dt.datetime) -> float:
    try:
        from .events import priority_boost
        return priority_boost(login, now.timestamp())
    except Exception:
        return 0.0


def score_streamers(streams: list[dict], clips: list[dict], roster: dict, learned: dict,
                    cfg: dict, now: dt.datetime) -> list[dict]:
    """Opportunity score per live streamer (pure: no network)."""
    w = cfg["weights"]
    heat: dict[str, float] = {}
    for c in clips:
        age = _age_min(c["created"], now)
        if age <= cfg["heat_window_h"] * 60:
            heat[c["login"]] = heat.get(c["login"], 0.0) + c["views"] / age
    factors = (learned.get("streamers") or {})
    rows = []
    for s in streams:
        if s["viewers"] < cfg["min_viewers"]:
            continue
        r = roster.get(s["login"], {})
        f = factors.get(s["login"])
        hist = 0.5 if f is None else max(0.0, min(1.0, (float(f) - 0.5) / 1.5))
        pri = {1: 1.0, 2: 0.5}.get(r.get("priority"), 0.0)
        pri = min(1.0, pri + _event_boost(s["login"], now))       # a creator in a live event (config/events.json)
        parts = {
            "audience": w["audience"] * _unit_log(s["viewers"], cfg["audience_cap"]),
            "heat": w["heat"] * _unit_log(heat.get(s["login"], 0.0), cfg["heat_cap_vpm"]),
            "history": w["history"] * hist,
            "priority": w["priority"] * pri,
        }
        score = round(sum(parts.values()), 1)
        rows.append({**s, "score": score, "parts": {k: round(v, 1) for k, v in parts.items()},
                     "heat_vpm": round(heat.get(s["login"], 0.0), 1), "on_roster": bool(r),
                     "vods": r.get("vods"), "policy": str(r.get("policy") or "allowed").lower()})
    # creators who don't allow clipping (config/creators.json "policy": "never") are listed last
    return sorted(rows, key=lambda r: (r["policy"] == "never", -r["score"]))


def trending_moments(clips: list[dict], cfg: dict, now: dt.datetime,
                     clipped: list[dict] | None = None) -> list[dict]:
    """Category viewer clips merged into moments, hottest (views/min) first (pure)."""
    from . import safety as SF
    window = cfg["trending_window_h"] * 60
    fresh = [c for c in clips if _age_min(c["created"], now) <= window]
    fresh.sort(key=lambda c: (c["login"], c["video"] or "", c["offset"] if c["offset"] is not None else 0,
                              c["created"]))
    moments: list[dict] = []
    for c in fresh:
        vpm = c["views"] / _age_min(c["created"], now)
        m = moments[-1] if moments else None
        same = (m and m["login"] == c["login"] and m["video"] == c["video"] and (
            (c["offset"] is not None and m["start"] is not None and abs(c["offset"] - m["start"]) <= 45) or
            (c["offset"] is None and abs(_age_min(c["created"], now) - m["age_min"]) <= 1.5)))
        if same:
            m["views"] += c["views"]
            m["vpm"] += vpm
            m["clips"] += 1
            m["titles"].append(c["title"])
            if c["offset"] is not None:
                m["end"] = max(m["end"], c["offset"] + c["duration"])
        else:
            moments.append({"login": c["login"], "name": c["name"], "video": c["video"],
                            "start": c["offset"], "end": (c["offset"] or 0) + c["duration"],
                            "views": c["views"], "vpm": vpm, "clips": 1, "titles": [c["title"]],
                            "age_min": round(_age_min(c["created"], now)), "top_slug": c["slug"]})
    for m in moments:
        m["vpm"] = round(m["vpm"], 1)
        m["titles"] = [t for t in dict.fromkeys(t.strip() for t in m["titles"]) if t][:4]
        sf = SF.screen(titles=m["titles"])
        m["unsafe"] = sf["why"] if sf["flagged"] else ""
        m["published"] = ""
        for r in clipped or []:
            if m["video"] and r.get("source") == m["video"] and m["start"] is not None and \
               min(m["end"], r["end_s"]) - max(m["start"], r["start_s"]) > 10:
                m["published"] = r.get("slug", "yes")
        if m["video"] and m["start"] is not None:
            a, b = max(0, int(m["start"]) - 60), int(m["end"]) + 60
            m["cut"] = f"scan --vod {m['video']} --from {a} --to {b} --top 4 --probe 3 --out runs/{m['login']}-{a}"
        else:
            m["cut"] = None                    # nothing to run: no saved stream to cut from
            m["note"] = "no saved stream — only a sentinel's recording can cut it (publish-live if one was running)"
    return sorted(moments, key=lambda m: (bool(m["unsafe"]) or bool(m["published"]), -m["vpm"]))


def _action(row: dict, cfg: dict) -> str:
    if row.get("policy") == "never":
        return "never"
    if row["score"] < cfg["act_score"]:
        return "watch"
    if row["vods"] is True:
        return "scan"
    if row["vods"] is False:
        return "record"
    try:                                        # off the roster: is this stream being saved as a VOD?
        v = T.latest_vod(row["login"])
        return "scan" if v.get("live") else "record"
    except Exception:
        return "record"


def run(out: str | None = None, hours: float | None = None, show: int = 15, quiet: bool = False) -> dict:
    from .publish import creators, load_clipped
    cfg = config()
    if hours:
        cfg["trending_window_h"] = hours
    now = dt.datetime.now(dt.timezone.utc)
    streams, clips = live_streams(cfg), category_clips(cfg)
    rows = score_streamers(streams, clips, creators(), learned_weights(), cfg, now)
    for r in rows[:show]:
        r["action"] = _action(r, cfg)
    moments = trending_moments(clips, cfg, now, load_clipped())
    roster = creators()
    never = {l for l, c in roster.items() if str(c.get("policy") or "").lower() == "never"}
    if never:
        for m in moments:
            if m["login"] in never and not m["unsafe"]:
                m["unsafe"] = "doesn't allow clipping (policy: never)"
        moments.sort(key=lambda m: (bool(m["unsafe"]) or bool(m["published"]), -m["vpm"]))
    res = {"at": now.isoformat(), "game": cfg["game"], "streamers": rows, "moments": moments,
           "live_count": len(streams), "live_logins": [s["login"] for s in streams]}
    if quiet:
        return res

    print(f"RADAR {now:%H:%M} UTC — {cfg['game']} ({','.join(cfg['languages'])}): "
          f"{len(streams)} live, {len(clips)} viewer clips today\n")
    print(f"{'score':>5}  {'streamer':<18}{'viewers':>9}{'clip v/min':>11}  action   (audience/heat/history/priority)")
    for r in rows[:show]:
        p = r["parts"]
        tag = "" if r["on_roster"] else "  NEW"
        print(f"{r['score']:5.0f}  {r['login']:<18}{r['viewers']:>9,}{r['heat_vpm']:>11.1f}  {r['action']:<8} "
              f"({p['audience']:.0f}/{p['heat']:.0f}/{p['history']:.0f}/{p['priority']:.0f}){tag}")
    print(f"\nHOTTEST MOMENTS (last {cfg['trending_window_h']:g} h, by views per minute)")
    for m in moments[:show]:
        flag = f"  ⛔ {m['unsafe']}" if m["unsafe"] else (f"  ✓ published as {m['published']}" if m["published"] else "")
        print(f"  {m['vpm']:7.1f}/min {m['views']:>7,} views  {m['login']:<16} {m['age_min']:>4}m ago  "
              f"{UT.quote(m['titles'][0], 48)}{flag}")
        if not flag:
            print(f"           -> python -m clipengine {m['cut']}" if m.get("cut") else f"           ({m.get('note', '')})")
    if out:
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        json.dump(res, open(out, "w"), indent=1)
        print(f"\nsaved {out}")
    return res
