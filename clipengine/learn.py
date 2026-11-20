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
  formats     the same for layout, caption style and length bucket (format_factor() turns it into a nudge)
  parts       per judge score part (grade.engine_score parts): worth of posts high vs low on it
  taste       streamers whose clips keep failing the frame check / rubric / --taste no / queue skip: pushed down
  windows     per platform, the local hours whose posts did best (prime time until there's data);
              the gate schedules clips that aren't time-critical into them
  summary     ONE plain line: what changed this run and why (report shows it)

What a post is worth is not its views alone: shares (and saves, at half) and average watch time, each
against our median on that platform, outweigh raw views when the agent recorded them (`clipengine
views --shares --watch-s --skip ...`). A post with only views is judged on views, as before.

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
TASTE = os.path.join(REPO, "data", "taste.jsonl")        # explicit --taste no verdicts

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


# what a post is worth: weights of each signal when it was recorded (renormalised over what's there)
QUALITY_WEIGHTS = {"views": 0.25, "shares": 0.40, "watch": 0.25, "skip": 0.10}
MIN_WINDOW_READS = 10        # reads on a platform before its posting hours are learned (prime time until then)


def _median_of(rows: list[dict], fn) -> float | None:
    vals = [v for v in (fn(r) for r in rows) if v is not None]
    return st.median(vals) if vals else None


def _shares(r: dict) -> float | None:
    if r.get("shares") is None and r.get("saves") is None:
        return None
    return float(r.get("shares") or 0) + 0.5 * float(r.get("saves") or 0)


def quality(r: dict, med: dict) -> tuple[float, dict]:
    """One post's worth vs our median on its platform → (ratio, parts). Views only → the views ratio."""
    cap = lambda x: max(0.1, min(5.0, x))
    parts = {}
    if med.get("views"):
        parts["views"] = cap(float(r["views"]) / med["views"])
    sh = _shares(r)
    if sh is not None and med.get("shares") is not None:
        parts["shares"] = cap((sh + 1.0) / (med["shares"] + 1.0))
    if r.get("watch_s") is not None and med.get("watch"):
        parts["watch"] = cap(float(r["watch_s"]) / med["watch"])
    if r.get("skip_pct") is not None and med.get("skip") is not None:
        parts["skip"] = cap((100.0 - float(r["skip_pct"])) / max(1.0, 100.0 - med["skip"]))
    if not parts:
        return 1.0, parts
    tw = sum(QUALITY_WEIGHTS[k] for k in parts)
    return sum(QUALITY_WEIGHTS[k] * v for k, v in parts.items()) / tw, parts


def length_bucket(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    return "<20 s" if seconds < 20 else "20-35 s" if seconds < 35 else "35-60 s" if seconds <= 60 else "60 s+"


def format_features(title: str | None, length_s: float | None = None, layout: str | None = None,
                    caption_style: str | None = None) -> dict:
    f = headline_features(title or "") if title else {}
    if length_s is not None:
        f["length_s"] = length_bucket(length_s)
    f["layout"] = layout or "stack"
    if caption_style:
        f["caption"] = caption_style
    return f


def format_factor(learned: dict, title: str | None = None, length_s: float | None = None,
                  layout: str | None = None, caption_style: str | None = None) -> float:
    """How this clip's format did for us (1.0 = typical, bounded 0.8-1.2): the learned buckets it falls in."""
    fm = learned.get("formats") or {}
    xs = []
    for k, b in format_features(title, length_s, layout, caption_style).items():
        d = (fm.get(k) or {}).get(b)
        if d and d.get("posts", 0) >= 3 and len(fm.get(k) or {}) >= 2:
            xs.append(_shrink([d["median_x"]] * d["posts"], 1.0))
    if not xs:
        return 1.0
    return round(max(0.8, min(1.2, st.median(xs))), 3)


def _local_hour(ts: float, tz: str) -> int:
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.fromtimestamp(ts, ZoneInfo(tz)).hour
    except Exception:
        return dt.datetime.fromtimestamp(ts, dt.timezone.utc).hour


def posting_windows(by_plat_hour: dict[str, list[tuple[int, float]]], tz: str, default: list[int]) -> dict:
    """Per platform: the local hours whose posts (±1 h) did best, once there are enough reads."""
    out = {}
    for plat, rows in by_plat_hour.items():
        if len(rows) < MIN_WINDOW_READS:
            out[plat] = {"hours": list(default), "source": "default", "reads": len(rows), "tz": tz}
            continue
        score = {}
        for h in range(24):
            near = [q for hh, q in rows if min((hh - h) % 24, (h - hh) % 24) <= 1]
            if len(near) >= 2:
                score[h] = _shrink(near, 1.0)
        good = sorted((h for h, v in score.items() if v >= 1.05), key=lambda h: -score[h])[:6]
        out[plat] = ({"hours": sorted(good), "source": "learned", "reads": len(rows), "tz": tz,
                      "factor": {str(h): round(score[h], 2) for h in sorted(good)}} if good else
                     {"hours": list(default), "source": "default", "reads": len(rows), "tz": tz})
    return out


def taste(clipped_rows: list[dict], drafts: list[dict] | None = None,
          verdicts: list[dict] | None = None) -> dict[str, dict]:
    """Per streamer: clips that failed the frame check / rubric (≤1 check true), were skipped in the queue,
    or got an explicit `--taste no` ("would a fan be glad to see this posted?" — data/taste.jsonl)."""
    t: dict[str, dict] = {}
    for r in verdicts or []:
        login = (r.get("login") or "").lower()
        if login and r.get("verdict") == "no":
            d = t.setdefault(login, {"miss": 0, "seen": 0})
            d["seen"] += 1
            d["miss"] += 1
            d["taste_no"] = d.get("taste_no", 0) + 1
    for r in clipped_rows:
        login = (r.get("login") or "").lower()
        if not login or r.get("rubric") is None:
            continue
        d = t.setdefault(login, {"miss": 0, "seen": 0})
        d["seen"] += 1
        d["miss"] += int(len(r.get("rubric") or []) <= 1)
    for r in drafts or []:
        login = (r.get("login") or "").lower()
        if login and r.get("status") == "skipped" and r.get("reason") != "taste":   # those are in `verdicts`
            d = t.setdefault(login, {"miss": 0, "seen": 0})
            d["seen"] += 1
            d["miss"] += 1
    for d in t.values():
        rate = d["miss"] / d["seen"] if d["seen"] else 0.0
        d["factor"] = round(max(0.5, 1.0 - 0.5 * rate), 3) if d["miss"] >= 3 and rate >= 0.6 else 1.0
    return t


def learn(perf: list[dict], ledger: list[dict], clipped_rows: list[dict], creators: dict,
          previous: dict | None = None, now: float | None = None, drafts: list[dict] | None = None,
          verdicts: list[dict] | None = None) -> dict:
    import time
    now = now or time.time()
    previous = previous or {}
    # posts the owner overrode past the gate measure the owner's call, not the engine's — keep them out
    overridden = {(r.get("slug"), r.get("platform")) for r in ledger if r.get("override")}
    perf = [r for r in perf if (r.get("slug"), r.get("platform")) not in overridden]
    ledger = [r for r in ledger if not r.get("override")]
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
    # the medians a post is judged against, per checkpoint and platform (shares, watch time, skip rate too)
    cp_med: dict[str, dict[str, dict]] = {}
    for name, sn in cp_snaps.items():
        for plat in {p for (_, p) in sn}:
            rows = [r for (s_, p), r in sn.items() if p == plat]
            cp_med.setdefault(name, {})[plat] = {
                "views": cp_medians[name].get(plat), "shares": _median_of(rows, _shares),
                "watch": _median_of(rows, lambda r: r.get("watch_s")),
                "skip": _median_of(rows, lambda r: r.get("skip_pct"))}
    # one ratio per post: the 24 h read, else the 1 h early read (half weight), else 72 h
    ratio: dict[tuple, tuple[float, float]] = {}
    evidence: dict[tuple, dict] = {}
    for name, w in (("72h", 1.0), ("1h", 0.5), ("24h", 1.0)):
        for key, r in cp_snaps[name].items():
            med = cp_med[name][key[1]]
            if med.get("views"):
                q, parts = quality(r, med)
                ratio[key] = (q, w)
                evidence[key] = {"parts": parts, "shares": _shares(r), "med_shares": med.get("shares"),
                                 "watch_s": r.get("watch_s"), "med_watch": med.get("watch"), "views": r["views"]}
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
    # taste: a streamer whose clips keep failing the frame check / rubric is pushed down (radar + gate read it)
    tst = taste(clipped_rows, drafts, verdicts)
    for login, d in tst.items():
        if d["factor"] < 1.0:
            streamers[login] = round(streamers.get(login, 1.0) * d["factor"], 3)
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
    # formats: layout and length bucket (+ the headline patterns above), judged on the same worth
    fmts: dict[str, dict[str, list[float]]] = {}
    part_rows: dict[str, list[tuple[float, float]]] = {}
    for (slug, plat), (rv, w) in ratio.items():
        if w != 1.0:
            continue
        c = clipped.get(slug) or {}
        length = (c["end_s"] - c["start_s"]) if c.get("end_s") is not None and c.get("start_s") is not None else None
        for k, v in format_features(titles.get(slug) or c.get("title"), length, c.get("layout"),
                                    c.get("caption_style")).items():
            fmts.setdefault(k, {}).setdefault(v, []).append(rv)
        for part, pv in (c.get("parts") or {}).items():
            if pv is not None:
                part_rows.setdefault(part, []).append((float(pv), rv))
    formats = {k: {b: {"median_x": round(st.median(v), 2), "posts": len(v)} for b, v in d.items() if len(v) >= 3}
               for k, d in fmts.items()}
    formats = {k: d for k, d in formats.items() if d}
    # the judge's score parts: do posts high on a part (above that part's median) do better than low ones?
    judge_parts = {}
    for part, rows in part_rows.items():
        if len(rows) < 6:
            continue
        mid = st.median(v for v, _ in rows)
        hi_ = [q for v, q in rows if v > mid]
        lo_ = [q for v, q in rows if v <= mid]
        if len(hi_) >= 3 and len(lo_) >= 3:
            judge_parts[part] = {"high_x": round(st.median(hi_), 2), "low_x": round(st.median(lo_), 2), "posts": len(rows)}
    for part, d in sorted(judge_parts.items(), key=lambda kv: -abs(kv[1]["high_x"] - kv[1]["low_x"]))[:1]:
        if d["high_x"] >= 1.3 * d["low_x"] or d["low_x"] >= 1.3 * d["high_x"]:
            report.append(f"judge part {part}: high {d['high_x']:.2f}× vs low {d['low_x']:.2f}×"
                          + (" — it predicts results" if d["high_x"] > d["low_x"] else " — it does NOT predict results"))
    for k in ("length_s", "layout", "caption"):
        d = formats.get(k) or {}
        if len(d) >= 2:
            top = max(d.items(), key=lambda kv: kv[1]["median_x"])
            low = min(d.items(), key=lambda kv: kv[1]["median_x"])
            if top[1]["median_x"] >= 1.3 * low[1]["median_x"]:
                report.append(f"format: {top[0]} {top[1]['median_x']:.2f}× vs {low[0]} {low[1]['median_x']:.2f}×")
    # posting hours (local): which hours' posts did best, per platform
    tz = gcfg.get("timezone", "America/New_York")
    by_hour: dict[str, list[tuple[int, float]]] = {}
    for (slug, plat), (rv, w) in ratio.items():
        t0 = live_at.get((slug, plat))
        if t0 and w == 1.0:
            by_hour.setdefault(plat, []).append((_local_hour(t0, tz), rv))
    windows = posting_windows(by_hour, tz, gcfg.get("prime_hours", GT.DEFAULTS["prime_hours"]))
    summary = explain(previous, streamers, thresholds, windows, tst, ratio, evidence, clipped, creators, titles)
    report.insert(0, summary)
    changes = forecast_rows(previous, streamers, ratio, evidence, clipped, creators, tst)
    forecasts = []
    for (slug, platform), row in snaps.items():
        login = login_for(slug, clipped, creators)
        posted = live_at.get((slug, platform))
        median = cp_med.get("24h", {}).get(platform)
        if login and posted is not None and median and median.get("views"):
            forecasts.append({"login": login, "posted_at": posted, "quality": quality(row, median)[0]})
    return {"updated": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(), "target_age_h": TARGET_AGE_H,
            "summary": summary, "formats": formats, "windows": windows, "parts": judge_parts,
            "taste": {l: d for l, d in tst.items() if d["factor"] < 1.0},
            "rich_reads": sum(1 for e in evidence.values() if len(e["parts"]) > 1),
            "platforms": {p: {"median_views": round(m, 1), "posts": len([1 for (s, q) in snaps if q == p])}
                          for p, m in medians.items()},
            "checkpoints": {c: {p: round(m, 1) for p, m in ms.items()} for c, ms in cp_medians.items()},
            "streamers": streamers, "streamer_posts": {l: len(v) for l, v in per_login.items()},
            "fit": fit, "thresholds": thresholds, "thresholds_changed_at": changed_at,
            "headlines": headlines, "report": report, "weight_changes": changes, "_forecast_samples": forecasts}


def explain(previous: dict, streamers: dict, thresholds: dict, windows: dict, tst: dict, ratio: dict,
            evidence: dict, clipped: dict, creators: dict, titles: dict) -> str:
    """ONE plain line: what changed in this run and why."""
    parts = []
    old = previous.get("streamers") or {}
    moves = sorted(((l, old.get(l, 1.0), f) for l, f in streamers.items() if abs(f - old.get(l, 1.0)) >= 0.1),
                   key=lambda x: -abs(x[2] - x[1]))
    for login, was, now_f in moves[:2]:
        why = ""
        if tst.get(login, {}).get("factor", 1.0) < 1.0:
            d = tst[login]
            why = (f"{d['miss']} of {d['seen']} clips failed the frame check/taste"
                   + (f" ({d['taste_no']} --taste no)" if d.get("taste_no") else ""))
        else:
            mine = [(k, e) for k, e in evidence.items() if login_for(k[0], clipped, creators) == login]
            shared = [(k, e) for k, e in mine if e.get("shares") is not None and e.get("med_shares") is not None]
            if shared:
                k, e = max(shared, key=lambda x: x[1]["shares"] - x[1]["med_shares"])
                why = (f"\"{(titles.get(k[0]) or k[0])[:36]}\" got {e['shares']:g} shares on {k[1]} "
                       f"vs our median {e['med_shares']:g}")
            elif mine:
                why = f"{len(mine)} reads at {st.median(ratio[k][0] for k, _ in mine):.2f}× our median"
        parts.append(f"{login} {'up' if now_f > was else 'down'} {was:.2f}→{now_f:.2f}×" + (f" ({why})" if why else ""))
    prev_t = previous.get("thresholds") or {}
    for plat, v in thresholds.items():
        if v != prev_t.get(plat, 0):
            parts.append(f"{plat} bar {v - prev_t.get(plat, 0):+d} (posts just over it underperformed)"
                         if v > prev_t.get(plat, 0) else f"{plat} bar {v - prev_t.get(plat, 0):+d} (marginal posts did fine)")
    prev_w = previous.get("windows") or {}
    for plat, w in windows.items():
        if w["source"] == "learned" and w["hours"] != (prev_w.get(plat) or {}).get("hours"):
            parts.append(f"best {plat} hours now {','.join(f'{h}:00' for h in w['hours'])} {w['tz'].split('/')[-1]}")
    if parts:
        return ("learned: " + "; ".join(parts))[:220]
    rich = sum(1 for e in evidence.values() if len(e["parts"]) > 1)
    return (f"learned: nothing changed — {len(ratio)} post reads ({rich} with shares/watch time)"
            + ("; record --shares/--watch-s with views to learn faster" if rich < len(ratio) / 2 else ""))[:220]


def run(write: bool = True) -> dict:
    from .publish import creators
    try:
        previous = json.load(open(WEIGHTS))
    except (OSError, ValueError):
        previous = {}
    try:
        from .sentinel import load_queue
        drafts = load_queue()
    except Exception:
        drafts = []
    res = learn(_rows(PERF), _rows(LEDGER), _rows(CLIPPED), creators(), previous, drafts=drafts,
                verdicts=_rows(TASTE))
    print(f"LEARN — {sum(res['streamer_posts'].values())} post reads (24 h medians: "
          + ", ".join(f"{p} {d['median_views']:,.0f}" for p, d in res["platforms"].items()) + ")")
    for login, f in sorted(res["streamers"].items(), key=lambda x: -x[1]):
        print(f"  {login:<16} {f:.2f}× our median")
    for line in res["report"]:
        print("  " + line)
    for row in res["weight_changes"]:
        print("  " + change_line(row))
    forecasts = res.pop("_forecast_samples")
    if write:
        os.makedirs(os.path.dirname(WEIGHTS), exist_ok=True)
        path = os.path.join(os.path.dirname(WEIGHTS), "predictions.jsonl")
        for row in update_forecasts(path, res["weight_changes"], forecasts, _ts(res["updated"])):
            if row["kind"] == "forecast":
                print(f"  next 7 days: {row['login']} {row['expected']} at 24h")
            else:
                print(f"  forecast scored: {row['login']} {row['result']} ({row['samples']} reads)")
        json.dump(res, open(WEIGHTS, "w"), indent=1)
        print(f"wrote {os.path.relpath(WEIGHTS, REPO)}")
    return res


def forecast_rows(previous: dict, streamers: dict, ratios: dict, evidence: dict, clipped: dict,
                  creators: dict, tastes: dict) -> list[dict]:
    changes = []
    old = previous.get("streamers") or {}
    readings = {}
    for (slug, platform), data in evidence.items():
        login = login_for(slug, clipped, creators)
        if login:
            readings.setdefault(login, []).append((ratios[(slug, platform)][0], data))
    for login in sorted(old.keys() | streamers.keys()):
        before, after = old.get(login, 1.0), streamers.get(login, 1.0)
        if before == after:
            continue
        samples = readings.get(login, [])
        quality_values = [q for q, _ in samples]
        shares = [r["shares"] for _, r in samples if r.get("shares") is not None]
        watch = [r["watch_s"] for _, r in samples if r.get("watch_s") is not None]
        taste = tastes.get(login) or {}
        changes.append({"login": login, "previous": before, "weight": after, "reads": len(samples),
                        "median_quality": round(st.median(quality_values), 2) if quality_values else None,
                        "median_shares": st.median(shares) if shares else None,
                        "median_watch_s": st.median(watch) if watch else None,
                        "failed": taste.get("miss", 0), "judged": taste.get("seen", 0)})
    return changes


def change_line(row: dict) -> str:
    evidence = [f"{row['reads']} reads"]
    for field, label, suffix in (("median_quality", "quality", "×"), ("median_shares", "shares+saves", ""),
                                 ("median_watch_s", "watch", "s")):
        if row.get(field) is not None:
            evidence.append(f"{label} {row[field]:g}{suffix}")
    if row.get("judged"):
        evidence.append(f"{row['failed']}/{row['judged']} failed judgment")
    return f"{row['login']} {row['previous']:.3f}→{row['weight']:.3f}× ({', '.join(evidence)})"


def update_forecasts(path: str, changes: list[dict], outcomes: list[dict], now: float) -> list[dict]:
    """Forecast the next seven days of relative 24h quality and score the matured windows."""
    from collections import defaultdict

    import fcntl

    with open(path, "a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        history = _rows(path)
        forecasts = [r for r in history if r.get("kind") == "forecast"]
        scored = {r["id"] for r in history if r.get("kind") == "result"}
        by_login = defaultdict(list)
        for sample in outcomes:
            by_login[sample["login"]].append(sample)
        appended = []
        for r in forecasts:
            if r["id"] in scored or now < r["until"] + 86400:
                continue
            measured = [s["quality"] for s in by_login[r["login"]] if r["from"] <= s["posted_at"] < r["until"]]
            actual = st.median(measured) if measured else None
            right = actual is not None and (actual >= 1 if r["expected"] == "above median" else actual < 1)
            appended.append({"kind": "result", "id": r["id"], "login": r["login"], "at": now,
                             "samples": len(measured), "quality": round(actual, 3) if actual is not None else None,
                             "result": "right" if right else "wrong" if measured else "unscored"})
        active = {r["login"] for r in forecasts if now < r["until"]}
        for change in changes:
            login = change["login"]
            if login in active or abs(change["weight"] - change["previous"]) < 0.05:
                continue
            if not change["reads"] and not change["judged"]:
                continue
            expected = "above median" if change["weight"] >= 1 else "below median"
            appended.append({"kind": "forecast", "id": f"{login}:{int(now)}", "login": login,
                             "from": now, "until": now + 7 * 86400, "expected": expected, "evidence": change})
        if appended:
            stream.write("".join(json.dumps(r) + "\n" for r in appended))
        return appended


def forecast_summary(path: str, now: float) -> str | None:
    from collections import Counter
    rows = _rows(path)
    if not rows:
        return None
    recent = Counter(r["result"] for r in rows if r.get("kind") == "result" and now - 7 * 86400 <= r["at"] <= now)
    pending = sum(r.get("kind") == "forecast" and r["from"] <= now < r["until"] + 86400 for r in rows)
    return f"predictions: {recent['right']} right · {recent['wrong']} wrong · {recent['unscored']} unscored · {pending} pending"
