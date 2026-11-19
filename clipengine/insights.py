"""Community insights — opt-in, anonymous, and only numbers.

Off until the user says yes. When on, `cycle` submits once a day: which detector signals fired before
moments that did well, banger scores against views (as ratios to this channel's own median), which
settings are used, and which known problems `diagnose` matched. Never handles, channel name, streamer
names, titles, clips, transcripts, chat, or anything identifying. A random contributor id (not tied to
the user) groups submissions so the same channel isn't double-counted.

Submissions go to a private repo the maintainers grant access to (config/insights.json "repo"); the
shared detector is retrained on them each release.

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
    print("insights: on (anonymous numbers only; `clipengine insights --disable` turns it off)"
          + ("" if cfg.get("repo") else " — no submission repo set yet: --repo <git url> from the maintainers"))


def disable() -> None:
    cfg = config(); cfg["enabled"] = False; _save(cfg)
    print("insights: off")


def submit(quiet: bool = False) -> str:
    cfg = config()
    if not cfg.get("enabled"):
        return "insights: off"
    if not cfg.get("repo"):
        return "insights: on, but no submission repo set (ask the maintainers for the repo URL)"
    p = payload()
    clone = os.path.join(LOCAL, "repo")
    if not os.path.isdir(os.path.join(clone, ".git")):
        os.makedirs(LOCAL, exist_ok=True)
        r = subprocess.run(["git", "clone", "-q", "--depth", "1", cfg["repo"], clone], capture_output=True, text=True)
        if r.returncode != 0:
            return "insights: couldn't reach the submission repo (this account may not have access yet)"
    g = lambda *a: subprocess.run(["git", "-C", clone, *a], capture_output=True, text=True)
    g("pull", "-q", "--rebase")
    d = os.path.join(clone, "submissions", p["contributor"])
    os.makedirs(d, exist_ok=True)
    json.dump(p, open(os.path.join(d, f"{p['date']}.json"), "w"))
    g("add", "-A")
    g("-c", "user.name=clip-engine", "-c", "user.email=clip-engine@users.noreply.github.com", "commit", "-qm",
      f"insights {p['contributor']} {p['date']}")
    ok = g("push", "-q").returncode == 0
    return f"insights: {'submitted' if ok else 'saved locally, push failed (will retry)'} ({len(p['outcomes'])} outcomes, {len(p['fired'])} firings)"
