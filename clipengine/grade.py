"""Banger score — one number (0–100) that decides whether, and where, a clip posts.

  final = 60% engine score  +  40% operator rubric

Engine score: what the engine can measure about the moment.

  crowd      viewer clips: views and how fast they arrive (strongest signal)
  chat       how far chat jumped above its normal level
  audio      speech + excitement, not silence or music
  streamer   their clips' views with us vs our median (data/weights.json)
  freshness  half-life decay since the moment happened

A signal a source can't provide (no chat for a live recording) is left out and
the rest are re-weighted, so a clip isn't punished for where it came from.
Penalties multiply: unsafe / hard-failed / already published = 0; a poll,
music, a dead open, a silent stretch, thin speech each cut it.

Operator rubric: four yes/no checks made after LOOKING at the frames —
  hook     something happens or is said in the first two seconds
  context  it makes sense to someone who never watched the stream
  payoff   what the headline promises is visible in our cut
  share    a fan would send it to a friend

Tiers (config/grading.json): banger ≥ 75, solid ≥ 60, else skip. The
distribution gate turns the score into platforms and times.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(REPO, "config", "grading.json")

RUBRIC = ("hook", "context", "payoff", "share")

DEFAULTS = {
    "engine_share": 0.6,
    "weights": {"crowd": 35, "chat": 20, "audio": 15, "streamer": 15, "freshness": 15},
    "crowd_vpm_cap": 100, "crowd_views_cap": 20000,
    "chat_scale": 6.0,
    "freshness_half_life_h": 3.0,
    "penalties": {"poll": 0.5, "music": 0.5, "lyrics": 0.5, "dead_open": 0.8, "silent": 0.85, "thin": 0.7,
                  "cutscene": 0.5, "static": 0.6, "dark": 0.5},
    "tiers": {"banger": 75, "solid": 60},
}


def config() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    if os.path.exists(CONFIG):
        user = json.load(open(CONFIG))
        for k, v in user.items():
            if k.startswith("_"):
                continue
            cfg[k] = {**cfg[k], **v} if isinstance(v, dict) and isinstance(cfg.get(k), dict) else v
    return cfg


def _unit_log(x: float, cap: float) -> float:
    return 0.0 if not x or x <= 0 else max(0.0, min(1.0, math.log10(1 + x) / math.log10(1 + cap)))


def _penalty(flags: list[str], cfg: dict) -> tuple[float, list[str]]:
    p, why = 1.0, []
    pen = cfg["penalties"]
    for f in flags or []:
        fl = f.lower()
        for key, needle in (("poll", "chat was asked"), ("music", "music"), ("lyrics", "lyrics"),
                            ("dead_open", "dead open"), ("silent", "silent stretch"), ("thin", "thin speech"),
                            ("cutscene", "cutscene"), ("static", "static ("), ("dark", "dark (")):
            if needle in fl:
                p *= pen[key]
                why.append(f"{key} ×{pen[key]}")
                break
    return p, why


def engine_score(sig: dict, cfg: dict | None = None) -> dict:
    """sig: crowd_vpm, crowd_views, chat_score, audio_factor (None = not available),
    streamer_factor (None = unknown → neutral), age_h, flags, unsafe, hard_fail, published."""
    cfg = cfg or config()
    w = cfg["weights"]
    parts: dict[str, float | None] = {
        "crowd": None if sig.get("crowd_vpm") is None and sig.get("crowd_views") is None else
        max(_unit_log(sig.get("crowd_vpm") or 0, cfg["crowd_vpm_cap"]),
            _unit_log(sig.get("crowd_views") or 0, cfg["crowd_views_cap"])),
        "chat": None if sig.get("chat_score") is None else 1 - math.exp(-max(0.0, sig["chat_score"]) / cfg["chat_scale"]),
        "audio": None if sig.get("audio_factor") is None else max(0.0, min(1.0, (sig["audio_factor"] - 0.4) / 0.9)),
        "streamer": 0.5 if sig.get("streamer_factor") is None else max(0.0, min(1.0, (sig["streamer_factor"] - 0.5) / 1.5)),
        "freshness": 0.5 ** (max(0.0, sig.get("age_h") or 0.0) / cfg["freshness_half_life_h"]),
    }
    used = {k: v for k, v in parts.items() if v is not None}
    total_w = sum(w[k] for k in used) or 1
    base = 100 * sum(w[k] * v for k, v in used.items()) / total_w
    mult, why = _penalty(sig.get("flags") or [], cfg)
    for hard, label in (("unsafe", "unsafe"), ("hard_fail", "hard fail"), ("published", "already published")):
        if sig.get(hard):
            mult, why = 0.0, why + [f"{label}: {sig[hard]}"]
    return {"score": round(base * mult, 1), "base": round(base, 1),
            "parts": {k: (None if v is None else round(v, 2)) for k, v in parts.items()}, "penalties": why}


def rubric_score(passed: list[str] | None) -> float | None:
    if passed is None:
        return None
    bad = [p for p in passed if p not in RUBRIC]
    if bad:
        raise ValueError(f"unknown rubric check(s) {bad}; use {', '.join(RUBRIC)}")
    return 100.0 * len(set(passed)) / len(RUBRIC)


def banger(engine: float, rubric: float | None, cfg: dict | None = None) -> dict:
    """Final score + tier. Without a rubric yet, reports the best the clip could reach."""
    cfg = cfg or config()
    share = cfg["engine_share"]
    if rubric is None:
        best = round(share * engine + (1 - share) * 100, 1)
        return {"score": None, "best_possible": best, "tier": None}
    s = round(share * engine + (1 - share) * rubric, 1)
    t = cfg["tiers"]
    tier = "banger" if s >= t["banger"] else "solid" if s >= t["solid"] else "skip"
    return {"score": s, "tier": tier, "engine": engine, "rubric": rubric}


def parse_rubric(text: str | None) -> list[str] | None:
    if text is None:
        return None
    t = text.strip().lower()
    if t in ("", "none"):
        return []
    return [x.strip() for x in t.split(",") if x.strip()]


# ---- signals from each source -------------------------------------------------

def _factor(login: str) -> float | None:
    from .radar import learned_weights
    return (learned_weights().get("streamers") or {}).get((login or "").lower())


def signals_from_candidate(c: dict, vod: dict, now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    crowd = c.get("crowd") or {}
    try:
        at = dt.datetime.fromisoformat(vod["created_at"].replace("Z", "+00:00")) + dt.timedelta(seconds=c["peak_s"])
        age_h = max(0.0, (now - at).total_seconds() / 3600)
    except (KeyError, ValueError):
        age_h = 0.0
    safety = c.get("safety") or {}
    return {"crowd_vpm": crowd.get("views_per_min") if crowd else None,
            "crowd_views": crowd.get("views") if crowd else None,
            "chat_score": c.get("chat_score"), "audio_factor": c.get("audio_factor"),
            "streamer_factor": _factor(vod.get("login")), "age_h": age_h,
            "flags": c.get("flags") or [], "unsafe": safety.get("why") if safety.get("flagged") else "",
            "hard_fail": c.get("hard_fail") or ""}


def signals_from_live(rec_dir: str, login: str, start_ts: float, end_ts: float,
                      words: list[dict] | None = None, now: float | None = None) -> dict:
    """A window of a live recording: crowd from viewer clips that overlap it, no chat."""
    from . import live as L
    import time
    now = now or time.time()
    crowd_vpm = crowd_views = None
    try:
        for m in L.live_moments(rec_dir):
            if min(end_ts, m["end"]) - max(start_ts, m["start"]) > 3:
                crowd_views = (crowd_views or 0) + m["views"]
                age_min = max(1.0, (now - m["end"]) / 60)
                crowd_vpm = (crowd_vpm or 0) + m["views"] / age_min
    except Exception:
        pass
    speech = sum(w["end"] - w["start"] for w in (words or []))
    dur = max(1.0, end_ts - start_ts)
    audio = None if words is None else 0.4 + 0.9 * min(1.0, speech / (0.5 * dur))
    flags = ["thin speech"] if words is not None and speech < 0.2 * dur else []
    return {"crowd_vpm": crowd_vpm, "crowd_views": crowd_views, "chat_score": None, "audio_factor": audio,
            "streamer_factor": _factor(login), "age_h": max(0.0, (now - end_ts) / 3600), "flags": flags}
