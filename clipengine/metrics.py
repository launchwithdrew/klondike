"""`clipengine metrics` — the weekly numbers that say what changed for the user (spec §6).

Six sections, each with its time window, this host's profile, the sample count behind every number, and — when a
number can't be computed — the reason it is unavailable (never a silent zero):

  coverage     observed media minutes / roster live minutes, catch-up-covered and unknown minutes, process uptime,
               longest dark interval, holes > 10 s with and without a cause, the off-host heartbeat
  overnight    per night (local 00–08): live minutes, drafts (live vs saved-stream), approved winners, muted-payoff
               losses / probed windows by route, or "no stream"
  cost         per 100 drafted candidates: full transcriptions, audio minutes, final encodes, heavy-job time, early
               (before transcription) and late rejects; the early-filter rollout gate and why it is or isn't met
  speed        p50/p90 by stage (moment→draft, draft→review, draft→pre-render, moment→live), reserve stock (hours under
               two ready clips), daily yield, claim→live
  publishing   gate-approved moments per day, verified links per platform, missed slots by reason, 1 h/24 h/72 h
               reads (missing reads kept apart from zero views), overrides left out
  recommend    posting hours from measured performance (timezone, observations, date range) — or the configured
               prime hours when evidence is thin; last week's predictions scored

`--save` writes runs/metrics/week-<date>.json; `--compare A B` prints two saved weeks side by side as absolute numbers
and ratios (the review's "10x" is a hypothesis — this is how it gets measured).
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
import statistics
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAY = 86400.0
NIGHT = (0, 8)                       # local hours counted as "overnight"
THIN_POSTS = 20                      # fewer measured posts than this: recommend the configured schedule


# ---- small readers ------------------------------------------------------------------------------------

def _rows(path: str) -> list[dict]:
    out = []
    try:
        for line in open(path):
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return out


def _json(path: str) -> dict:
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return {}


def _ts(v) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))], 1)


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _zone(root: str):
    tz = _json(os.path.join(root, "config", "distribution.json")).get("timezone") or "America/New_York"
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(tz), tz
    except Exception:
        return dt.timezone.utc, "UTC"


def _na(why: str) -> dict:
    return {"unavailable": why}


def profile(root: str) -> dict:
    """The host profile every metric is reported under."""
    try:
        from . import host as H
        n, src = H.cpus()
        mem = H.mem_total_gb()
    except Exception:
        n, src, mem = None, "unknown", None
    ops = _json(os.path.join(root, "config", "ops.json"))
    from . import __version__
    return {"host": os.uname().nodename, "cpus": n, "cpus_from": src, "mem_gb": mem,
            "profile": ops.get("profile", "full"), "engine": __version__}


def _live(root: str, since: float, now: float) -> dict[str, list[tuple[float, float]]]:
    seen = _json(os.path.join(root, "runs", ".cycle.json")).get("seen_live") or {}
    out = {}
    for login, ivs in seen.items():
        cut = [(max(s, since), min(e, now)) for s, e in ivs if e > since and s < now]
        if cut:
            out[login] = cut
    return out


def _rec_dirs(root: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for idx in glob.glob(os.path.join(root, "runs", "*", "index.jsonl")):
        d = os.path.dirname(idx)
        name = os.path.basename(d)
        login = name.removeprefix("sentinel-").removesuffix("-live")
        out.setdefault(login, []).append(d)
    return out


# ---- 1 · coverage and recovery ---------------------------------------------------------------------------

def coverage(root: str, since: float, now: float) -> dict:
    live = _live(root, since, now)
    if not live:
        return _na("no roster live sessions on record in this window (runs/.cycle.json seen_live) — "
                   "either nobody streamed or no cycle ran")
    recs = _rec_dirs(root)
    cov = _json(os.path.join(root, "runs", "coverage.json"))
    vod_mark = max(float(cov.get("catchup_until") or 0), float(cov.get("sweep_until") or 0))   # completed scans
    roster = _json(os.path.join(root, "config", "creators.json")).get("creators", {})
    live_s = rec_s = vod_s = 0.0
    holes, explained, ads, per = 0, 0, 0, {}
    for login, ivs in live.items():
        segs = []
        for d in recs.get(login, []):
            for r in _rows(os.path.join(d, "index.jsonl")):
                if "gap" in r:
                    if since <= r["t"] <= now and r["d"] > 10:
                        holes += 1
                        cause = str(r.get("gap") or "")
                        ads += cause.startswith("Twitch ad break")
                        explained += bool(cause) and cause != "unknown"
                elif r.get("f"):
                    segs.append((r["t"], r["t"] + r.get("d", 2.0)))
        l_s = sum(e - s for s, e in ivs)
        r_s = sum(_overlap(a, b, s, e) for s, e in ivs for a, b in segs)
        r_s = min(r_s, l_s)
        vods = (roster.get(login) or {}).get("vods") is not False
        v_s = sum(_overlap(s, e, since, vod_mark) for s, e in ivs) if vods else 0.0
        v_s = max(0.0, min(v_s, l_s - r_s))
        live_s += l_s; rec_s += r_s; vod_s += v_s
        per[login] = round(r_s / l_s, 3) if l_s else None
    from . import supervise as SV
    inc = [r for r in _merged_incidents(root) if (r.get("dark_from") or 0) < now and
           (r.get("recovered_at") or now) > since]
    dark = [min(now, r.get("recovered_at") or now) - max(since, r["dark_from"]) for r in inc if r.get("dark_from")]
    hb = _json(os.path.join(root, "config", "ops.json")).get("heartbeat_url")
    worst = sorted(per.items(), key=lambda kv: kv[1] if kv[1] is not None else 9)[:3]
    return {"live_min": round(live_s / 60), "recorded_min": round(rec_s / 60),
            "observed_share": round(rec_s / live_s, 3) if live_s else None,
            "catchup_covered_min": round(vod_s / 60), "unknown_min": round(max(0.0, live_s - rec_s - vod_s) / 60),
            "covered_share": round((rec_s + vod_s) / live_s, 3) if live_s else None,
            "streams": len(live), "incidents": len(inc), "dark_min": round(sum(dark) / 60, 1),
            "longest_dark_min": round(max(dark) / 60, 1) if dark else 0.0,
            "uptime_share": round(1 - sum(dark) / (now - since), 4),
            "holes_over_10s": holes, "holes_explained": explained, "holes_ads": ads,
            "heartbeat_monitor": "off-host monitor configured" if hb else "local only (no heartbeat_url)",
            "lowest_coverage": [{"login": k, "share": v} for k, v in worst]}


def _merged_incidents(root: str) -> list[dict]:
    rows: dict[str, dict] = {}
    for r in _rows(os.path.join(root, "runs", "incidents.jsonl")):
        if r.get("id"):
            rows[r["id"]] = {**rows.get(r["id"], {}), **r}
    return list(rows.values())


# ---- 2 · overnight yield and losses -----------------------------------------------------------------------

def _drafts(root: str) -> list[dict]:
    from . import sentinel as SN
    try:
        return SN.load_queue(os.path.join(root, "runs"))
    except Exception:
        return []


def _drops(root: str) -> list[dict]:
    out = []
    for p in glob.glob(os.path.join(root, "outbox", "*", "post.json")):
        m = _json(p)
        if m:
            out.append(m)
    return out


def _moment(row: dict) -> float | None:
    return _ts(row.get("payoff")) or _ts(row.get("moment_at")) or _ts(row.get("start"))


def overnight(root: str, since: float, now: float) -> dict:
    zone, tzname = _zone(root)
    live = _live(root, since, now)
    drafts, drops = _drafts(root), _drops(root)
    probes = _rows(os.path.join(root, "runs", "heavy", "probes.jsonl"))
    nights, day = [], dt.datetime.fromtimestamp(since, zone).replace(hour=0, minute=0, second=0, microsecond=0)
    while day.timestamp() < now:
        a, b = day.replace(hour=NIGHT[0]).timestamp(), day.replace(hour=NIGHT[1]).timestamp()
        if b > since and a < now:
            a, b = max(a, since), min(b, now)
            lm = sum(_overlap(s, e, a, b) for ivs in live.values() for s, e in ivs) / 60
            ds = [d for d in drafts if (m := _moment(d)) is not None and a <= m < b]
            wins = {w.get("slug") for w in drops if (m := _moment(w)) is not None and a <= m < b}
            pr = [p for p in probes if a <= p.get("at", 0) < b and not p.get("cached")]
            muted = {}
            for p in pr:
                r = muted.setdefault(p.get("route", "?"), [0, 0])
                r[1] += 1
                r[0] += bool(p.get("skipped"))
            nights.append({"night": day.strftime("%m-%d"), "live_min": round(lm),
                           "outcome": (f"{len(wins)} winner(s)" if lm >= 1 else
                                       "live time not on record" if ds or wins else "no stream"),
                           "drafts": len(ds), "drafts_live": sum(1 for d in ds if d.get("rec") and not d.get("scan")),
                           "drafts_vod": sum(1 for d in ds if d.get("scan")), "winners": len(wins),
                           "muted_by_route": {k: f"{v[0]}/{v[1]}" for k, v in muted.items()}})
        day += dt.timedelta(days=1)
    return {"timezone": tzname, "hours": f"{NIGHT[0]:02d}–{NIGHT[1]:02d}", "nights": nights,
            "winners": sum(n["winners"] for n in nights), "streamed_nights": sum(n["live_min"] >= 1 for n in nights)}


# ---- 3 · cost and quality ------------------------------------------------------------------------------

def cost(root: str, since: float, now: float) -> dict:
    jobs = [r for r in _rows(os.path.join(root, "runs", "heavy", "log.jsonl")) if since <= r.get("at", 0) <= now]
    work = [r for r in _rows(os.path.join(root, "runs", "heavy", "work.jsonl")) if since <= r.get("at", 0) <= now]
    probes = [r for r in _rows(os.path.join(root, "runs", "heavy", "probes.jsonl")) if since <= r.get("at", 0) <= now]
    drafts = [d for d in _drafts(root) if (t := _ts(d.get("drafted_at")) or _moment(d)) and since <= t <= now]
    n = len(drafts)
    if not n and not jobs:
        return _na("no drafts and no heavy jobs in this window")
    asr = [r for r in jobs if r.get("stage") == "transcribe"]
    enc = [r for r in jobs if r.get("stage") in ("render", "card")]
    asr_min = sum(r["media_s"] for r in work if r.get("stage") == "transcribe") / 60
    per = (lambda x: round(100 * x / n, 1)) if n else (lambda x: None)
    early = sum(1 for p in probes if p.get("skipped")) + sum(1 for d in drafts if d.get("route") == "archive")
    late = sum(1 for d in drafts if d.get("status") in ("skipped", "dropped"))
    saved = sum(p.get("seconds", 0) for p in probes if p.get("skipped")) / 60
    out = {"candidates": n, "asr_calls": len(asr), "asr_calls_per_100": per(len(asr)),
           "asr_audio_min": round(asr_min, 1) if work else None,
           "asr_audio_min_per_100": per(asr_min) if work else None,
           "final_encodes": len(enc), "final_encodes_per_100": per(len(enc)),
           "heavy_job_min": round(sum(r.get("ran_s", 0) for r in jobs) / 60, 1),
           "early_rejects": early, "late_rejects": late, "probe_saved_audio_min": round(saved, 1),
           "false_rejects": _na("no sampled reject audit yet — mark sampled rejects to measure it"),
           "prefilter_recall": _na("needs human-labelled winners on held-out replay")}
    if not work:
        out["asr_audio_min"] = _na("audio minutes are logged from v0.4.14 on (runs/heavy/work.jsonl)")
    out["filter_gate"] = ("advisory: insufficient evidence — the rollout gate needs ≥30% fewer transcription minutes at "
                          "≥95% retention of human-confirmed winners on held-out replay, with sample counts")
    return out


# ---- 4 · speed and inventory ---------------------------------------------------------------------------

def speed(root: str, since: float, now: float) -> dict:
    drafts, drops = _drafts(root), {d.get("slug"): d for d in _drops(root)}
    ledger = _rows(os.path.join(root, "ledger.jsonl"))
    first_live: dict[str, float] = {}
    for r in ledger:
        if r.get("override"):
            continue
        t = _ts(r.get("at"))
        if r.get("slug") and t:
            first_live[r["slug"]] = min(t, first_live.get(r["slug"], t))
    stages = {"moment→draft": [], "moment→draft (live)": [], "moment→draft (catch-up)": [], "draft→review": [],
              "draft→pre-render": [], "moment→live": []}
    for d in drafts:
        m, made = _moment(d), _ts(d.get("drafted_at"))
        if made is None or not since <= made <= now:
            continue
        if m and made >= m:
            stages["moment→draft"].append((made - m) / 60)
            stages["moment→draft (catch-up)" if d.get("scan") else "moment→draft (live)"].append((made - m) / 60)
        seen = _ts(d.get("seen_at")) or _ts(d.get("judged_at"))
        if seen and seen >= made:
            stages["draft→review"].append((seen - made) / 60)
        pre = _pre_at(d)
        if pre and pre >= made:
            stages["draft→pre-render"].append((pre - made) / 60)
    for slug, t in first_live.items():
        m = _moment(drops.get(slug) or {})
        if m and since <= t <= now and t >= m:
            stages["moment→live"].append((t - m) / 60)
    st = {k: {"n": len(v), "p50_min": _pct(v, .5), "p90_min": _pct(v, .9)} for k, v in stages.items()}
    stock = [r for r in _rows(os.path.join(root, "runs", "metrics", "stock.jsonl")) if since <= r.get("at", 0) <= now]
    if len(stock) >= 2:
        low = sum(b["at"] - a["at"] for a, b in zip(stock, stock[1:]) if (a.get("ready") or 0) < 2 and b["at"] - a["at"] <= 3600)
        stock_out = {"samples": len(stock), "hours_below_two": round(low / 3600, 1),
                     "ready_now": stock[-1].get("ready"), "why_low_last": stock[-1].get("why")}
    else:
        stock_out = _na("reserve stock is sampled each cycle from v0.4.14 on (runs/metrics/stock.jsonl)")
    days = max(1.0, (now - since) / DAY)
    dropped = [d for d in drops.values() if (t := _ts(d.get("dropped_at"))) and since <= t <= now]
    att = _rows(os.path.join(root, "data", "attempts.jsonl"))
    claims = {a["id"]: a for a in att if a.get("ev") == "claim" and since <= (a.get("ts") or 0) <= now}
    c2l = [(a["ts"] - claims[a["id"]]["ts"]) / 60 for a in att
           if a.get("ev") == "live" and a.get("id") in claims and a.get("ts")]
    return {"stages": st, "stock": stock_out, "daily_yield": round(len(dropped) / days, 2),
            "claim_to_live": {"n": len(c2l), "p50_min": _pct(c2l, .5), "p90_min": _pct(c2l, .9)}}


def _pre_at(d: dict) -> float | None:
    if not (d.get("pre") and d.get("rec")):
        return None
    meta = os.path.join(d["rec"], "drafts", d["id"] + ".pre.json")
    return _ts(_json(meta).get("at"))


def note_stock(root: str | None = None, now: float | None = None) -> None:
    """cycle: one sample of the reserve (ready-now clips) for the weekly stock metric."""
    root = root or REPO
    try:
        from . import bank as BK
        inv = BK.inventory(now)
        ready = len({r["slug"] for r in inv if r["state"] == "ready"})
        later = len({r["slug"] for r in inv if r["state"] == "waiting"})
        why = None if ready >= 2 else ("nothing finished waiting" if not inv else "all stock is posted or scheduled")
    except Exception as exc:
        ready, later, why = None, None, f"bank unreadable ({str(exc)[:60]})"
    d = os.path.join(root, "runs", "metrics")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "stock.jsonl"), "a") as fh:
        fh.write(json.dumps({"at": now or time.time(), "ready": ready, "later": later, "why": why}) + "\n")


# ---- 5 · publishing and outcomes ------------------------------------------------------------------------

def publishing(root: str, since: float, now: float) -> dict:
    ledger = _rows(os.path.join(root, "ledger.jsonl"))
    perf = _rows(os.path.join(root, "data", "performance.jsonl"))
    att = _rows(os.path.join(root, "data", "attempts.jsonl"))
    drops = [d for d in _drops(root) if (t := _ts(d.get("dropped_at"))) and since <= t <= now]
    zone, _ = _zone(root)
    per_day: dict[str, set] = {}
    for d in drops:
        per_day.setdefault(dt.datetime.fromtimestamp(_ts(d["dropped_at"]), zone).strftime("%m-%d"), set()).add(d.get("slug"))
    rows = [r for r in ledger if (t := _ts(r.get("at"))) and since <= t <= now]
    ovr = [r for r in rows if r.get("override")]
    live = [r for r in rows if not r.get("override") and r.get("url")]
    plat: dict[str, dict] = {}
    for r in live:
        p = plat.setdefault(r.get("platform", "?"), {"links": 0, "verified": 0})
        p["links"] += 1
        p["verified"] += bool(r.get("verified_by"))
    missed: dict[str, int] = {}
    for a in att:
        if a.get("ev") == "failed" and since <= (a.get("ts") or 0) <= now:
            why = str(a.get("why") or "unknown")
            k = "account" if "account" in why.lower() else why[:40]
            missed[k] = missed.get(k, 0) + 1
    zone_days = {dt.datetime.fromtimestamp(t, zone).strftime("%m-%d")
                 for t in (since + i * DAY for i in range(int((now - since) / DAY) + 1)) if t <= now}
    dry = sorted(zone_days - set(per_day))
    if dry:
        missed["no approved moment that day"] = len(dry)
    reads: dict[tuple, list] = {}
    for r in perf:
        reads.setdefault((r.get("slug"), r.get("platform")), []).append(r)
    windows = {"1h": 0, "24h": 0, "72h": 0}
    missing_24, zero_views = 0, 0
    for r in live:
        rs = reads.get((r.get("slug"), r.get("platform")), [])
        ages = [x.get("age_h") or 0 for x in rs]
        windows["1h"] += any(a >= 1 for a in ages)
        windows["24h"] += any(a >= 24 for a in ages)
        windows["72h"] += any(a >= 72 for a in ages)
        age = (now - _ts(r["at"])) / 3600
        if age > 24 and not rs:
            missing_24 += 1
        zero_views += any(x.get("views") == 0 for x in rs)
    return {"approved_moments_per_day": {k: len(v) for k, v in sorted(per_day.items())},
            "approved_moments": sum(len(v) for v in per_day.values()), "verified_by_platform": plat,
            "missed_by_reason": missed, "posts": len(live), "reads": windows,
            "missing_reads_over_24h": missing_24, "zero_view_reads": zero_views,
            "overrides_excluded": len(ovr)}


# ---- 6 · recommendations --------------------------------------------------------------------------------

def recommend(root: str, now: float) -> dict:
    zone, tzname = _zone(root)
    ledger = {(r.get("slug"), r.get("platform")): r for r in _rows(os.path.join(root, "ledger.jsonl"))
              if not r.get("override")}
    perf = _rows(os.path.join(root, "data", "performance.jsonl"))
    best: dict[tuple, dict] = {}
    for r in perf:
        k = (r.get("slug"), r.get("platform"))
        if k in ledger and r.get("views") is not None and (k not in best or (r.get("age_h") or 0) > (best[k].get("age_h") or 0)):
            best[k] = r
    out = {"timezone": tzname}
    cfg = _json(os.path.join(root, "config", "distribution.json"))
    prime = cfg.get("prime_hours") or [17, 21]
    for plat in sorted({k[1] for k in best}):
        obs = []
        for (slug, p), r in best.items():
            if p != plat:
                continue
            t = _ts(ledger[(slug, p)].get("at"))
            if t:
                obs.append((dt.datetime.fromtimestamp(t, zone).hour, r["views"], t))
        if len(obs) < THIN_POSTS:
            out[plat] = {"observations": len(obs), "use": f"configured prime hours {prime} (evidence thin: "
                                                          f"{len(obs)} of {THIN_POSTS} measured posts)"}
            continue
        by: dict[int, list] = {}
        for h, v, _ in obs:
            by.setdefault(h, []).append(v)
        ranked = sorted(((statistics.median(v), h, len(v)) for h, v in by.items() if len(v) >= 3), reverse=True)[:3]
        ts = [t for *_, t in obs]
        out[plat] = {"observations": len(obs), "best_hours": [{"hour": h, "median_views": m, "n": n} for m, h, n in ranked],
                     "from": dt.datetime.fromtimestamp(min(ts), zone).strftime("%Y-%m-%d"),
                     "to": dt.datetime.fromtimestamp(max(ts), zone).strftime("%Y-%m-%d")}
    if len(out) == 1:
        out["posting"] = f"configured prime hours {prime} (no measured posts yet)"
    try:
        from . import learn as LN
        out["predictions"] = LN.forecast_summary(os.path.join(root, "data", "predictions.jsonl"), now) or \
            "no predictions on record yet"
    except Exception:
        out["predictions"] = "unavailable"
    return out


# ---- the report -----------------------------------------------------------------------------------------

def weekly(root: str | None = None, now: float | None = None, days: float = 7.0) -> dict:
    root = root or REPO
    now = now or time.time()
    since = now - days * DAY
    rep = {"window": {"from": since, "to": now, "days": days}, "profile": profile(root)}
    for name, fn in (("coverage", lambda: coverage(root, since, now)), ("overnight", lambda: overnight(root, since, now)),
                     ("cost", lambda: cost(root, since, now)), ("speed", lambda: speed(root, since, now)),
                     ("publishing", lambda: publishing(root, since, now)), ("recommend", lambda: recommend(root, now))):
        try:
            rep[name] = fn()
        except Exception as exc:                        # one broken source never hides the other five
            rep[name] = _na(f"failed to compute ({type(exc).__name__}: {str(exc)[:80]})")
    return rep


def _f(v) -> str:
    if isinstance(v, dict) and "unavailable" in v:
        return f"unavailable — {v['unavailable']}"
    return "–" if v is None else str(v)


def lines(rep: dict) -> list[str]:
    w, pr = rep["window"], rep["profile"]
    fmt = lambda t: dt.datetime.fromtimestamp(t).strftime("%m-%d %H:%M")
    out = [f"metrics {fmt(w['from'])} → {fmt(w['to'])} ({w['days']:g} d) · host {pr['host']} {pr['cpus']} CPUs "
           f"{pr['mem_gb']} GB · profile {pr['profile']} · engine {pr['engine']}"]
    c = rep["coverage"]
    out.append("coverage   " + (_f(c) if "unavailable" in c else
               f"{c['recorded_min']}/{c['live_min']} live min recorded ({c['observed_share']:.0%}) over {c['streams']} "
               f"stream(s) · scanned from saved streams (catch-up / sweep: chat + viewer clips) "
               f"{c['catchup_covered_min']} · covered in all {c['covered_share']:.0%} · unknown {c['unknown_min']} · uptime "
               f"{c['uptime_share']:.1%} ({c['incidents']} incident(s), longest dark {c['longest_dark_min']} min) · "
               f"holes>10s {c['holes_over_10s']} ({c['holes_explained']} explained, {c['holes_ads']} ads) · "
               f"{c['heartbeat_monitor']}"))
    o = rep["overnight"]
    out.append("overnight  " + (_f(o) if "unavailable" in o else
               f"{o['winners']} winner(s) over {o['streamed_nights']}/{len(o['nights'])} streamed nights "
               f"({o['hours']} {o['timezone']}): " + " · ".join(
                   f"{n['night']} {n['outcome']} ({n['live_min']} live min, {n['drafts_live']} live/{n['drafts_vod']} VOD "
                   f"drafts" + (f", muted {n['muted_by_route']}" if n['muted_by_route'] else "") + ")"
                   for n in o["nights"])))
    k = rep["cost"]
    out.append("cost       " + (_f(k) if "unavailable" in k else
               f"per 100 of {k['candidates']} candidates: {_f(k['asr_calls_per_100'])} transcriptions "
               f"({_f(k['asr_audio_min_per_100'])} audio min), {_f(k['final_encodes_per_100'])} final encodes · heavy jobs "
               f"{k['heavy_job_min']} min · early rejects {k['early_rejects']} (saved {k['probe_saved_audio_min']} audio "
               f"min), late {k['late_rejects']} · false rejects: {_f(k['false_rejects'])} · filter {k['filter_gate']}"))
    s = rep["speed"]
    if "unavailable" in s:
        out.append("speed      " + _f(s))
    else:
        st = " · ".join(f"{name} p50 {_f(v['p50_min'])}/p90 {_f(v['p90_min'])} min (n={v['n']})"
                        for name, v in s["stages"].items())
        sk = s["stock"]
        stock = _f(sk) if "unavailable" in sk else (f"{sk['ready_now']} ready now, {sk['hours_below_two']} h under two "
                                                     f"({sk['samples']} samples)")
        out.append(f"speed      {st} · stock: {stock} · yield {s['daily_yield']}/day · claim→live p50 "
                   f"{_f(s['claim_to_live']['p50_min'])} min (n={s['claim_to_live']['n']})")
    p = rep["publishing"]
    out.append("publishing " + (_f(p) if "unavailable" in p else
               f"{p['approved_moments']} approved moment(s) {p['approved_moments_per_day']} · links " +
               (", ".join(f"{k} {v['verified']}/{v['links']} verified" for k, v in p["verified_by_platform"].items()) or "none")
               + f" · missed {p['missed_by_reason'] or 'none'} · reads 1h {p['reads']['1h']}/24h {p['reads']['24h']}/72h "
               f"{p['reads']['72h']} of {p['posts']} · missing >24h {p['missing_reads_over_24h']} · zero-view reads "
               f"{p['zero_view_reads']} · overrides left out {p['overrides_excluded']}"))
    r = rep["recommend"]
    plats = {k: v for k, v in r.items() if k not in ("timezone", "predictions", "posting")}
    rec = " · ".join(f"{k}: " + (v["use"] if "use" in v else ", ".join(f"{h['hour']:02d}h (median {h['median_views']}, "
                                                                        f"n={h['n']})" for h in v["best_hours"])
                                 + f" [{v['from']}→{v['to']}]") for k, v in plats.items()) or r.get("posting", "")
    out.append(f"recommend  {rec} ({r['timezone']}) · {r['predictions']}")
    return out


def save(rep: dict, root: str | None = None) -> str:
    d = os.path.join(root or REPO, "runs", "metrics")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"week-{dt.datetime.fromtimestamp(rep['window']['to']):%Y%m%d-%H%M}.json")
    json.dump(rep, open(path, "w"), indent=1, default=str)
    return path


COMPARE = (("coverage", "observed_share"), ("coverage", "unknown_min"), ("coverage", "longest_dark_min"),
           ("overnight", "winners"), ("cost", "asr_audio_min_per_100"), ("cost", "final_encodes_per_100"),
           ("cost", "heavy_job_min"), ("speed", "daily_yield"), ("publishing", "approved_moments"),
           ("publishing", "missing_reads_over_24h"))


def compare(a_path: str, b_path: str) -> list[str]:
    a, b = json.load(open(a_path)), json.load(open(b_path))
    out = [f"  {'':<34} {'A':>10} {'B':>10} {'B/A':>7}"]
    for sec, key in COMPARE:
        x, y = (a.get(sec) or {}).get(key), (b.get(sec) or {}).get(key)
        num = lambda v: v if isinstance(v, (int, float)) else None
        ratio = f"{num(y) / num(x):.2f}" if num(x) and num(y) is not None else "–"
        out.append(f"  {sec + ' ' + key:<34} {_f(num(x)):>10} {_f(num(y)):>10} {ratio:>7}")
    stg = lambda r, k: (((r.get("speed") or {}).get("stages") or {}).get(k) or {}).get("p50_min")
    for k in ("moment→draft", "moment→live"):
        x, y = stg(a, k), stg(b, k)
        out.append(f"  {'speed ' + k + ' p50 min':<34} {_f(x):>10} {_f(y):>10} "
                   f"{(f'{y / x:.2f}' if x and y is not None else '–'):>7}")
    return out
