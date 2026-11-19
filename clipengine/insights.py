"""Community insights — opt-in, anonymous, and only numbers.

Off until the user says yes. When on, `cycle` submits once a day: which detector signals fired before
moments that did well, banger scores against views (as ratios to this channel's own median), which
settings are used, and which known problems `diagnose` matched. Never handles, channel name, streamer
names, titles, clips, transcripts, chat, or anything identifying. A random contributor id (not tied to
the user) groups submissions so the same channel isn't double-counted.

Sharing must be write-only — no contributor may read or change anyone else's submissions — so until
that channel exists, nothing leaves this machine: each day's anonymous numbers are kept in
local/insights/outbox/ and will be sent once sharing opens (with the user's yes already recorded).

  clipengine insights --show       exactly what would be sent
  clipengine insights --enable     (only after the user agrees)   --disable   --submit
"""
from __future__ import annotations

import datetime as dt
import json
import os
import statistics as st
import subprocess
import time
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(REPO, "config", "insights.json")
LOCAL = os.path.join(REPO, "local", "insights")


def config() -> dict:
    try:
        return json.load(open(CONFIG))
    except (OSError, ValueError):
        return {"enabled": False, "repo": "", "contributor": ""}


def _save(cfg: dict) -> None:
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    json.dump(cfg, open(CONFIG, "w"), indent=2)


def _rows(path: str) -> list[dict]:
    out = []
    if os.path.exists(path):
        for line in open(path):
            try:
                r = json.loads(line)
                if not any(str(k).startswith("_") for k in r):
                    out.append(r)
            except ValueError:
                pass
    return out


def payload(now: float | None = None) -> dict:
    """The anonymous numbers this channel would share. Pure aggregation of local files."""
    from . import brand as B
    from .sentinel import load_queue
    now = now or time.time()
    perf = _rows(os.path.join(REPO, "data", "performance.jsonl"))
    clipped = {r["slug"]: r for r in _rows(os.path.join(REPO, "data", "clipped.jsonl")) if r.get("slug")}
    latest: dict[tuple, float] = {}
    for r in perf:
        if r.get("views") is not None:
            latest[(r["slug"], r["platform"])] = float(r["views"])
    med = {}
    for (s, p), v in latest.items():
        med.setdefault(p, []).append(v)
    med = {p: st.median(v) for p, v in med.items() if v}
    outcomes = []
    for (s, p), v in latest.items():
        c = clipped.get(s)
        if not c or not med.get(p):
            continue
        outcomes.append({"platform": p, "views_ratio": round(v / med[p], 3), "grade": c.get("grade"),
                         "engine": c.get("engine"), "rubric_checks": len(c.get("rubric") or []),
                         "explore": bool(c.get("explore"))})
    fired = []
    for q in load_queue():
        if q.get("features"):
            fired.append({"p": q.get("p"), "features": q["features"], "engine": q.get("engine"),
                          "outcome": q.get("status")})          # published / skipped / new — no ids or names
    b = B.load()
    settings = {"layout": b["layout"].get("kind"), "words_per_card": b["captions"].get("words_per_card"),
                "card_position": b["card"].get("position"), "warmup": bool(b.get("warmup"))}
    try:
        from .diagnose import match, recent_log_text
        problems = {h["id"]: h["count"] for h in match(recent_log_text())}
    except Exception:
        problems = {}
    version = subprocess.run(["git", "-C", REPO, "log", "-1", "--format=%h"], capture_output=True, text=True).stdout.strip()
    return {"schema": 1, "contributor": config().get("contributor") or "", "engine": version,
            "date": dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime("%Y-%m-%d"),
            "outcomes": outcomes, "fired": fired[-500:], "settings": settings, "problems": problems}


def show() -> dict:
    p = payload()
    print(f"would share (anonymous): {len(p['outcomes'])} post outcomes (views as ratios to your median), "
          f"{len(p['fired'])} detector firings (signal numbers only), settings {p['settings']}, "
          f"problems seen {p['problems'] or 'none'}. No handles, names, titles, clips or chat.")
    if p["outcomes"]:
        print("example outcome:", json.dumps(p["outcomes"][0]))
    return p


def enable(repo: str | None = None) -> None:
    cfg = config()
    cfg["enabled"] = True
    cfg["contributor"] = cfg.get("contributor") or uuid.uuid4().hex[:16]
    if repo:
        cfg["repo"] = repo
    _save(cfg)
    print("insights: on (anonymous numbers only, kept on this machine until sharing opens; "
          "`clipengine insights --disable` turns it off)")


def disable() -> None:
    cfg = config(); cfg["enabled"] = False; _save(cfg)
    print("insights: off")


def submit(quiet: bool = False) -> str:
    """Keep today's anonymous numbers locally. Nothing is sent until a write-only channel exists."""
    cfg = config()
    if not cfg.get("enabled"):
        return "insights: off"
    p = payload()
    out = os.path.join(LOCAL, "outbox")
    os.makedirs(out, exist_ok=True)
    json.dump(p, open(os.path.join(out, f"{p['date']}.json"), "w"))
    return (f"insights: kept on this machine ({len(p['outcomes'])} outcomes, {len(p['fired'])} firings) — "
            "sharing opens in a later release; nothing was sent")