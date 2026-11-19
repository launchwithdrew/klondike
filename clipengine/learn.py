"""Learn from results — turn views into the weights the radar and banger score use.

Reads data/performance.jsonl (view snapshots: stats.mjs, or added by hand),
ledger.jsonl (when each post went live) and data/clipped.jsonl (who each clip
was of, and the banger score it had), then writes data/weights.json:

  streamers   {login: factor}  their clips' views ÷ our median on the same
              platform (1.0 = typical, 2.0 = twice). Shrunk toward 1.0 when there
              are few posts, so one lucky clip doesn't crown a streamer.
  fit         {platform: {login: factor}}  the same per platform (TikTok may love
              a streamer Instagram ignores); the gate nudges scores with it
  checkpoints medians at 1 h, 24 h and 72 h; a post uses its 24 h read, else its
              1 h early read at half weight, else 72 h
  thresholds  per-platform change to the minimum score, one ±5 step a day at most
              (bounded −10..+15): up when posts that only just cleared the bar do
              much worse than clearly strong ones, down when they do as well —
              the gate applies it
  headlines   which headline patterns (length, CAPS words, quotes, numbers) got
              more views
  streamer_posts  how many reads each streamer has (the gate explores the unknown)

and reports whether high banger scores really got more views.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import statistics as st

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PERF = os.path.join(REPO, "data", "performance.jsonl")
LEDGER = os.path.join(REPO, "ledger.jsonl")
CLIPPED = os.path.join(REPO, "data", "clipped.jsonl")
WEIGHTS = os.path.join(REPO, "data", "weights.json")

TARGET_AGE_H = 24.0
PRIOR_POSTS = 3          # shrinkage: a streamer needs a few posts before their factor moves far from 1.0


def _rows(path: str) -> list[dict]:
    out = []
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not any(k.startswith("_") for k in r):
                out.append(r)
    return out


def _ts(iso):
    try:
        return dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def login_for(slug: str, clipped: dict, creators: dict) -> str | None:
    if slug in clipped and clipped[slug].get("login"):
        return clipped[slug]["login"].lower()
    head = slug.split("_", 1)[0].lower()            # older posts: slug starts with the headline name ("speed_snake")
    for login, c in creators.items():
        if head in (login.lower(), str(c.get("name", "")).lower()):
            return login.lower()
    return None


CHECKPOINTS = {"1h": (1.0, 0.5, 6.0), "24h": (24.0, 12.0, 48.0), "72h": (72.0, 48.0, 120.0)}


def best_snapshot(perf: list[dict], live_at: dict, target: float = TARGET_AGE_H,
                  lo: float = 0.0, hi: float = 1e9) -> dict[tuple, dict]:
    """(slug, platform) → the snapshot nearest `target` hours after posting, within [lo, hi]."""
    best: dict[tuple, dict] = {}
    for r in perf:
        if r.get("views") is None:
            continue
        key = (r["slug"], r["platform"])
        age = r.get("age_h")
        if age is None:
            t0, t1 = live_at.get(key), _ts(r.get("checked_at"))
            age = (t1 - t0) / 3600 if t0 and t1 else target
        if not lo <= age <= hi:
            continue
        cur = best.get(key)
        if cur is None or abs(age - target) < abs(cur["_age"] - target):
            best[key] = {**r, "_age": age}
    return best


def _shrink(vals: list[float], prior: float, k: int = PRIOR_POSTS) -> float:
    return (sum(vals) + prior * k) / (len(vals) + k)


def headline_features(title: str) -> dict:
    import re
    words = re.findall(r"[A-Za-z][A-Za-z']+", title or "")
    caps = sum(1 for w in words if len(w) > 1 and w.isupper())
    n = len(title or "")
    return {"length": "short (<50)" if n < 50 else "medium (50-79)" if n < 80 else "long (80+)",
            "caps": "no CAPS word" if caps == 0 else "1-2 CAPS words" if caps <= 2 else "3+ CAPS words",
            "quote": "has a quote" if '"' in (title or "") else "no quote",
            "number": "has a number" if re.search(r"\d", title or "") else "no number"}


def learn(perf: list[dict], ledger: list[dict], clipped_rows: list[dict], creators: dict,
          previous: dict | None = None, now: float | None = None) -> dict:
    import time
    now = now or time.time()
    previous = previous or {}
    live_at = {(r["slug"], r["platform"]): _ts(r.get("at")) for r in ledger if r.get("status") == "live"}
    titles = {r["slug"]: r.get("title") for r in ledger if r.get("title")}
    clipped = {r["slug"]: r for r in clipped_rows if r.get("slug")}
    # views at each checkpoint, each judged against that checkpoint's own median
    cp_snaps = {name: best_snapshot(perf, live_at, *spec) for name, spec in CHECKPOINTS.items()}
    cp_medians = {}
    for name, snaps in cp_snaps.items():
        by: dict[str, list[float]] = {}
        for (slug, plat), r in snaps.items():
            by.setdefault(plat, []).append(float(r["views"]))
        cp_medians[name] = {p: st.median(v) for p, v in by.items() if v}
    snaps, medians = cp_snaps["24h"], cp_medians["24h"]
    # one ratio per post: the 24 h read, else the 1 h early read (half weight), else 72 h
    ratio: dict[tuple, tuple[float, float]] = {}
    for name, w in (("72h", 1.0), ("1h", 0.5), ("24h", 1.0)):
        for key, r in cp_snaps[name].items():
            med = cp_medians[name].get(key[1])
            if med:
                ratio[key] = (float(r["views"]) / med, w)
    per_login: dict[str, list[float]] = {}
    per_fit: dict[tuple, list[float]] = {}
    for (slug, plat), (rv, w) in ratio.items():
        login = login_for(slug, clipped, creators)
        if not login:
            continue
        val = 1.0 + (rv - 1.0) * w
        per_login.setdefault(login, []).append(val)
        per_fit.setdefault((plat, login), []).append(val)
    streamers = {l: round(_shrink(v, 1.0), 3) for l, v in per_login.items()}
    fit: dict[str, dict[str, float]] = {}
    for (plat, login), v in per_fit.items():
        fit.setdefault(plat, {})[login] = round(_shrink(v, streamers.get(login, 1.0)), 3)
    report = []
    graded = [(clipped[s]["grade"], rv) for (s, p), (rv, w) in ratio.items()
              if s in clipped and clipped[s].get("grade") is not None and w == 1.0]
    if len(graded) >= 8:
        hi = [v for g, v in graded if g >= 80]
        lo = [v for g, v in graded if g < 80]
        if hi and lo:
            ok = st.median(hi) > st.median(lo)
            report.append(f"calibration: score ≥80 → {st.median(hi):.2f}× median, <80 → {st.median(lo):.2f}× "
                          f"({'scores predict views' if ok else 'scores do NOT predict views — review config/grading.json'})")
    else:
        report.append(f"calibration: {len(graded)} graded posts with 24 h views so far (needs 8)")
    # thresholds move by themselves, one step a day at most, within bounds: are the posts that only
    # just cleared the bar (score < bar + 10) worth it next to the clearly strong ones?
    from . import gate as GT
    gcfg = GT.config()
    thresholds = dict(previous.get("thresholds") or {})
    changed_at = dict(previous.get("thresholds_changed_at") or {})
    for plat in {p for (_, p) in ratio}:
        bar = (gcfg["platforms"].get(plat) or {}).get("min_score", 75) + thresholds.get(plat, 0)
        pg = [(clipped[s]["grade"], rv) for (s, p), (rv, w) in ratio.items() if p == plat and s in clipped
              and clipped[s].get("grade") is not None and w == 1.0]
        marginal = [rv for g, rv in pg if g < bar + 10]
        strong = [rv for g, rv in pg if g >= bar + 10]
        step = 0
        if len(marginal) >= 3 and len(strong) >= 3:
            if st.median(marginal) < 0.6 * st.median(strong):
                step = 5
            elif len(pg) >= 10 and st.median(marginal) >= 0.95 * st.median(strong):
                step = -5
        last = _ts(changed_at.get(plat) or "1970-01-01T00:00:00+00:00") or 0
        if step and now - last > 20 * 3600:
            thresholds[plat] = max(-10, min(15, thresholds.get(plat, 0) + step))
            changed_at[plat] = dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat()
            report.append(f"{plat}: minimum score {'raised' if step > 0 else 'lowered'} by 5 "
                          f"(now {thresholds[plat]:+d} vs config): posts just over the bar got "
                          f"{st.median(marginal):.2f}× vs {st.median(strong):.2f}× for strong ones")
    # headlines: which patterns get more views (24 h ratio), shown once a bucket has 3+ posts
    heads: dict[str, dict[str, list[float]]] = {}
    for (slug, plat), (rv, w) in ratio.items():
        if w != 1.0 or not titles.get(slug):
            continue
        for k, v in headline_features(titles[slug]).items():
            heads.setdefault(k, {}).setdefault(v, []).append(rv)
    headlines = {k: {b: {"median_x": round(st.median(v), 2), "posts": len(v)} for b, v in d.items() if len(v) >= 3}
                 for k, d in heads.items()}
    for k, d in headlines.items():
        if len(d) >= 2:
            top = max(d.items(), key=lambda kv: kv[1]["median_x"])
            low = min(d.items(), key=lambda kv: kv[1]["median_x"])
            if top[1]["median_x"] >= 1.3 * low[1]["median_x"]:
                report.append(f"headlines: {top[0]} {top[1]['median_x']:.2f}× vs {low[0]} {low[1]['median_x']:.2f}×")
    return {"updated": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(), "target_age_h": TARGET_AGE_H,
            "platforms": {p: {"median_views": round(m, 1), "posts": len([1 for (s, q) in snaps if q == p])}
                          for p, m in medians.items()},
            "checkpoints": {c: {p: round(m, 1) for p, m in ms.items()} for c, ms in cp_medians.items()},
            "streamers": streamers, "streamer_posts": {l: len(v) for l, v in per_login.items()},
            "fit": fit, "thresholds": thresholds, "thresholds_changed_at": changed_at,
            "headlines": headlines, "report": report}


def run(write: bool = True) -> dict:
    from .publish import creators
    try:
        previous = json.load(open(WEIGHTS))
    except (OSError, ValueError):
        previous = {}
    res = learn(_rows(PERF), _rows(LEDGER), _rows(CLIPPED), creators(), previous)
    print(f"LEARN — {sum(res['streamer_posts'].values())} post reads (24 h medians: "
          + ", ".join(f"{p} {d['median_views']:,.0f}" for p, d in res["platforms"].items()) + ")")
    for login, f in sorted(res["streamers"].items(), key=lambda x: -x[1]):
        print(f"  {login:<16} {f:.2f}× our median")
    for line in res["report"]:
        print("  " + line)
    if write:
        os.makedirs(os.path.dirname(WEIGHTS), exist_ok=True)
        json.dump(res, open(WEIGHTS, "w"), indent=1)
        print(f"wrote {os.path.relpath(WEIGHTS, REPO)}")
    return res
