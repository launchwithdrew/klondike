"""`clipengine metrics` — the weekly numbers that say what changed for the user (spec §6).

Six sections, each with its time window, this host's profile, the sample count behind every number, and — when a
number can't be computed — the reason it is unavailable (never a silent zero):

  coverage     observed media minutes / roster live minutes, catch-up-covered and unknown minutes, process uptime,
               longest dark interval, holes > 10 s with and without a cause, the off-host heartbeat
  overnight    per night (local 00–08): live minutes, drafts (live vs saved-stream), approved winners, muted-payoff
               losses / probed windows by route, or "no stream"
  cost         per 100 drafted candidates: full transcriptions, audio minutes, final encodes, heavy-job time, early
               (before transcription) and late rejects; the early-filter rollout gate and why it is or isn't met
  speed        p50/p90 by stage (moment→draft, draft→review, draft→pre-render, moment→first verification), reserve stock (hours under
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
import re
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
        recorded = _json(os.path.join(d, "recording.json"))
        login = recorded.get("source") or name.removeprefix("sentinel-").removesuffix("-live")
        out.setdefault(login, []).append(d)
    return out


# ---- 1 · coverage and recovery ---------------------------------------------------------------------------

def _union(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for start, end in sorted(spans):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _minutes(seconds: float) -> float:
    return round(seconds / 60, 2)


def coverage(root: str, since: float, now: float) -> dict:
    from bisect import bisect_left, bisect_right
    from collections import Counter
    from .admission import _dir

    live = _live(root, since, now)
    if not live:
        return _na("no roster live sessions on record in this window (runs/.cycle.json seen_live) — "
                   "either nobody streamed or no cycle ran")
    recs = _rec_dirs(root)
    cov = _json(os.path.join(root, "runs", "coverage.json"))
    by = cov.get("by_creator") or {}
    runs = os.path.join(root, "runs")
    journals = _rows(os.path.join(_dir(runs), "batches.jsonl"))
    scans: dict[str, list[tuple[float, float]]] = {}
    for batch in journals:
        for window in batch.get("completed_windows") or []:
            if window["end"] > since and window["start"] < now:
                scans.setdefault(window["login"], []).append((window["start"], window["end"]))
    exact_scans = any("completed_windows" in batch and (batch.get("until") or 0) > since
                      and (batch.get("since") or 0) < now for batch in journals)
    chats: dict[str, list[tuple[float, float]]] = {}
    for event in _rows(os.path.join(runs, "tier1", "coverage.jsonl")):
        if event["end"] > since and event["start"] < now:
            for login in event["channels"]:
                chats.setdefault(login, []).append((event["start"], event["end"]))
    roster = _json(os.path.join(root, "config", "creators.json")).get("creators", {})
    known_starts = _json(os.path.join(runs, ".cycle.json")).get("seen_live", {})
    live_s = rec_s = vod_s = chat_s = 0.0
    gaps = set()
    per, sessions, retained, indexed = {}, [], [], []
    for login, ivs in live.items():
        segs = []
        for d in recs.get(login, []):
            for r in _rows(os.path.join(d, "index.jsonl")):
                if "gap" in r:
                    if since <= r["t"] <= now and r["d"] > 10:
                        gaps.add((login, r["t"], r["d"], str(r.get("gap") or "unknown")))
                elif r.get("f"):
                    span = (r["t"], r["t"] + r.get("d", 2.0))
                    segs.append(span)
                    if os.path.isfile(os.path.join(d, "segments", r["f"])):
                        retained.append(r["t"])
        media = _union(segs)
        ends = [end for _, end in media]
        starts = [start for start, _ in media]
        indexed.extend((start, end, login) for start, end in segs if end > since and start <= now)
        vods = (roster.get(login) or {}).get("vods") is not False
        vod_spans = _union(scans.get(login, [])) if vods else []
        if vods and not exact_scans:
            watermark = max(float((by.get(kind) or {}).get(login, cov.get(kind + "_until") or 0))
                            for kind in ("catchup", "sweep"))
            vod_spans = [(since, watermark)] if watermark > since else []
        chat_spans = _union(chats.get(login, []))
        creator_recorded = creator_live = 0.0
        for start, end in _union(ivs):
            recorded = media[bisect_right(ends, start):bisect_left(starts, end)]
            covered = _union(recorded + vod_spans)
            total_s = end - start
            recorded_s = sum(_overlap(a, b, start, end) for a, b in recorded)
            covered_s = sum(_overlap(a, b, start, end) for a, b in covered)
            scanned_s = max(0.0, covered_s - recorded_s)
            observed_s = sum(_overlap(a, b, start, end) for a, b in _union(covered + chat_spans))
            chat_only_s = max(0.0, observed_s - covered_s)
            first = max(start, recorded[0][0]) if recorded else None
            actual_start_in_window = any(s == start and e >= start for s, e in known_starts.get(login, []))
            sessions.append({"login": login, "from": start, "to": end, "live_min": _minutes(total_s),
                             "recorded_min": _minutes(recorded_s), "scanned_only_min": _minutes(scanned_s),
                             "chat_only_min": _minutes(chat_only_s) if chats else None,
                             "unknown_min": _minutes(max(0.0, total_s - observed_s)),
                             "first_indexed_segment_at": first,
                             "late_start_s": round(first - start, 2) if first is not None and actual_start_in_window else None,
                             "opening_unrecorded_s": round((first if first is not None else end) - start, 2)})
            creator_live += total_s
            creator_recorded += recorded_s
            vod_s += scanned_s
            chat_s += chat_only_s
        live_s += creator_live
        rec_s += creator_recorded
        per[login] = round(creator_recorded / creator_live, 3) if creator_live else None
    from . import supervise as SV
    incidents = _merged_incidents(root)
    spans, unreconciled = incident_spans(incidents, now)
    inc = [sp for sp in spans if sp[0] < now and sp[1] > since]
    dark = [min(now, b) - max(since, a) for a, b, _ in inc]
    hb = _json(os.path.join(root, "config", "ops.json")).get("heartbeat_url")
    oldest = round((now - min(retained)) / 3600, 1) if retained else None
    gap_causes = Counter(cause for _, _, _, cause in gaps)
    capture_recoveries = _capture_recoveries(incidents, indexed, since, now)
    worst = sorted(per.items(), key=lambda kv: kv[1] if kv[1] is not None else 9)[:3]
    return {"live_min": round(live_s / 60), "recorded_min": round(rec_s / 60),
            "observed_share": round(rec_s / live_s, 3) if live_s else None,
            "catchup_covered_min": round(vod_s / 60),
            "saved_stream_basis": "completed windows" if exact_scans else "legacy watermark estimate",
            "chat_only_min": _minutes(chat_s) if chats else None,
            "unknown_min": round(max(0.0, live_s - rec_s - vod_s - chat_s) / 60),
            "sessions": sessions, "capture_recoveries": capture_recoveries,
            "covered_share": round((rec_s + vod_s) / live_s, 3) if live_s else None,
            "streams": len(sessions), "incidents": len(inc), "dark_min": round(sum(dark) / 60, 1),
            "unreconciled_incidents": [u for u in unreconciled if since <= u["dark_from"] < now],
            "longest_dark_min": round(max(dark) / 60, 1) if dark else 0.0,
            "uptime_share": round(1 - sum(dark) / (now - since), 4),
            "holes_over_10s": len(gaps), "holes_explained": len(gaps) - gap_causes["unknown"],
            "holes_ads": sum(n for cause, n in gap_causes.items() if cause.startswith("Twitch ad break")),
            "hole_causes": dict(sorted(gap_causes.items())),
            "heartbeat_monitor": "off-host monitor configured" if hb else "local only (no heartbeat_url)",
            "oldest_recuttable_h": oldest,
            "lowest_coverage": [{"login": k, "share": v} for k, v in worst]}



def _capture_recoveries(incidents: list[dict], segments: list[tuple[float, float, str]],
                        since: float, now: float) -> list[dict]:
    """Separate process recovery from first indexed media. Never bridge a later outage."""
    from bisect import bisect_left, bisect_right

    rows = sorted((r for r in incidents if r.get("dark_from")), key=lambda r: r["dark_from"])
    by_start = sorted(segments)
    by_end = sorted((end, start, login) for start, end, login in segments)
    starts = [s for s, _, _ in by_start]
    ends = [e for e, _, _ in by_end]
    result = []
    for i, incident in enumerate(rows):
        restart = _ts(incident.get("recovered_at"))
        if restart is None or not since <= restart <= now:
            continue
        cutoff = rows[i + 1]["dark_from"] if i + 1 < len(rows) else now
        previous = bisect_right(ends, incident["dark_from"]) - 1
        following = bisect_left(starts, restart)
        before = by_end[previous] if previous >= 0 else None
        after = by_start[following] if following < len(by_start) and starts[following] < cutoff else None
        result.append({"id": incident["id"], "cause": incident.get("cause"), "restart_at": restart,
                       "last_segment_end": before[0] if before else None, "last_segment_login": before[2] if before else None,
                       "first_segment_at": after[0] if after else None, "first_segment_login": after[2] if after else None,
                       "restart_to_segment_s": round(after[0] - restart, 2) if after else None})
    return result

def incident_spans(incidents: list[dict], now: float) -> tuple[list[tuple[float, float, str]], list[dict]]:
    """(start, end, id) of each dark period, never overstated. An incident's end is its recorded recovery, else its
    own recorded downtime (dark_min), else the next incident's start (the leader was back to die again), else — when
    it is still open — now, but only if it is the LAST incident (the leader may still be down). An older incident
    with no end evidence is "unreconciled": listed, not counted (Nov 22: one orphaned open record read as 21.7 h)."""
    rows = sorted([r for r in incidents if r.get("dark_from")], key=lambda r: r["dark_from"])
    spans, orphans = [], []
    for k, r in enumerate(rows):
        a = float(r["dark_from"])
        nxt = float(rows[k + 1]["dark_from"]) if k + 1 < len(rows) else None
        if r.get("status") == "closed-unknown" and not r.get("recovered_at"):
            orphans.append({"id": r.get("id"), "dark_from": a, "why": r.get("why") or "recovery not recorded"})
            continue
        if r.get("recovered_at"):
            b = float(r["recovered_at"])
        elif r.get("dark_min") is not None:
            b = a + float(r["dark_min"]) * 60
        elif nxt is not None:
            orphans.append({"id": r.get("id"), "dark_from": a, "why": "no recovery recorded; the leader came back "
                            "before the next incident"})
            continue
        else:
            b = now                                        # the newest incident, still open: dark until now
        if nxt is not None:
            b = min(b, nxt)
        spans.append((a, max(a, b), r.get("id") or ""))
    return spans, orphans


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

def _draft_timelines(root: str, drafts: list[dict], drops: dict[str, dict], ledger: list[dict],
                     attempts: list[dict], since: float, now: float) -> dict:
    """Join the operating receipts by their identifiers. Gaps in older receipts stay visible."""
    from collections import defaultdict
    from .admission import _dir, draft_key

    jobs, claims, delivery = defaultdict(list), defaultdict(list), defaultdict(list)
    for row in _rows(os.path.join(_dir(os.path.join(root, "runs")), "log.jsonl")):
        if row.get("draft_key") and since <= (row.get("at") or 0) <= now:
            jobs[row["draft_key"]].append({k: row.get(k) for k in
                ("kind", "stage", "ready_at", "at", "finished_at", "slot", "ok", "aged_publish", "wait_causes")})
    approvals = {row["draft_key"]: row for row in _rows(os.path.join(root, "data", "clipped.jsonl"))
                 if row.get("draft_key") and row.get("slug") and row.get("taste") == "yes"}
    for row in attempts:
        if row.get("slug") and (stamp := _ts(row.get("ts"))) is not None and stamp <= now:
            claims[row["slug"]].append({"event": row.get("ev"), "attempt_id": row.get("id"), "at": stamp,
                                       "platform": row.get("platform")})
    for row in ledger:
        stamp = _ts(row.get("at"))
        if (row.get("slug") and row.get("verified_by") and row.get("url") and not row.get("override")
                and not row.get("found_existing") and stamp is not None and stamp <= now):
            delivery[row["slug"]].append({"platform": row.get("platform"), "verified_at": stamp,
                                          "published_at": _ts(row.get("published_at"))})
    timelines = {}
    for draft in drafts:
        key = draft_key(draft)
        if key is None:
            continue
        status = draft.get("status")
        slug = (approvals.get(key) or {}).get("slug") or draft.get("slug")
        ready, archived = _ts(draft.get("drafted_at")), _ts(draft.get("archived_at"))
        drop = _ts((drops.get(slug) or {}).get("dropped_at"))
        if not jobs[key] and not any(t is not None and since <= t <= now for t in (ready, archived, drop)):
            continue
        timelines[key] = {
            "id": draft["id"], "route": _route(draft), "status": status, "slug": slug,
            "moment_at": _moment(draft), "draft_ready_at": ready, "seen_at": _ts(draft.get("seen_at")),
            "judge_recorded_at": _ts(draft.get("judged_at")), "dropped_at": drop,
            "archived_at": archived if status == "archived" else None,
            "stale_archive": status == "archived" and str(draft.get("reason") or "").startswith("stale:"),
            "jobs": sorted(jobs[key], key=lambda r: r["at"]), "attempts": sorted(claims[slug], key=lambda r: r["at"]),
            "delivery": sorted(delivery[slug], key=lambda r: r["verified_at"]),
        }
    return timelines


def _stale_archives(drafts: list[dict]) -> dict:
    from collections import Counter

    counts: Counter = Counter()
    for draft in drafts:
        if draft.get("status") != "archived" or not str(draft.get("reason") or "").startswith("stale:"):
            continue
        archived = _ts(draft.get("archived_at"))
        seen = _ts(draft.get("seen_at")) or _ts(draft.get("judged_at"))
        classification = "legacy_time_unknown" if archived is None else (
            "unseen" if seen is None or seen > archived else "seen")
        counts[classification] += 1
    return {k: counts[k] for k in ("seen", "unseen", "legacy_time_unknown")}


def speed(root: str, since: float, now: float) -> dict:
    drafts, drops = _drafts(root), {d.get("slug"): d for d in _drops(root)}
    ledger = _rows(os.path.join(root, "ledger.jsonl"))
    first_live: dict[str, float] = {}
    for r in ledger:
        if r.get("override") or not r.get("verified_by") or not r.get("url") or r.get("found_existing"):
            continue
        t = _ts(r.get("at"))
        if r.get("slug") and t:
            first_live[r["slug"]] = min(t, first_live.get(r["slug"], t))
    stages = {"moment→draft": [], "moment→draft (live)": [], "moment→draft (catch-up)": [], "draft→review": [],
              "draft→pre-render": [], "moment→first verification": []}
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
            stages["moment→first verification"].append((t - m) / 60)
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
            "draft_timelines": _draft_timelines(root, drafts, drops, ledger, att, since, now),
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
        if why and any(r.get("rights_reason") for r in inv):
            why = "posting rights require owner review"
    except Exception as exc:
        ready, later, why = None, None, f"bank unreadable ({str(exc)[:60]})"
    d = os.path.join(root, "runs", "metrics")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "stock.jsonl"), "a") as fh:
        fh.write(json.dumps({"at": now or time.time(), "ready": ready, "later": later, "why": why}) + "\n")


# ---- 5 · publishing and outcomes ------------------------------------------------------------------------

def _stream_outcomes(root: str, since: float, now: float, ledger: list[dict], zone) -> dict:
    from . import moments

    days: dict[str, dict] = {}
    required = {"youtube": 60, "instagram": 75, "tiktok": 75}
    for sessions in _live(root, since, now).values():
        for start, end in _union(sessions):
            while start < end:
                day = dt.datetime.fromtimestamp(start, zone).date()
                midnight = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), tzinfo=zone).timestamp()
                stop = min(end, midnight)
                entry = days.setdefault(day.isoformat(), {"eligible_min": 0, "approvals": [], "verified_all": 0,
                                                          "verified_by_30m": 0, "unknown_30m": 0, "late_all": 0})
                entry["eligible_min"] += (stop - start) / 60
                start = stop
    approvals = _rows(os.path.join(root, "data", "clipped.jsonl"))
    approved = {r["slug"]: r for r in approvals if r.get("slug") and r.get("taste") == "yes"}
    receipts = sorted(_drops(root), key=lambda r: _ts(r.get("dropped_at")) or 0)
    accepted = []
    for receipt in receipts:
        slug = receipt.get("slug")
        editor = approved.get(slug)
        moment = _ts(receipt.get("moment_at"))
        dropped = _ts(receipt.get("dropped_at"))
        grade = receipt.get("grade")
        grade = grade.get("score") if isinstance(grade, dict) else None
        platforms = [p for p in receipt.get("platforms") or [] if p in required]
        if (not editor or moment is None or not since <= moment <= now or dropped is None or not moment <= dropped <= now
                or not isinstance(grade, (int, float)) or not any(grade >= required[p] for p in platforms)):
            continue
        day = dt.datetime.fromtimestamp(moment, zone).date().isoformat()
        login = editor.get("login") or receipt.get("login")
        bounds = moments.window(receipt)
        if day not in days or not login or not bounds or moments.match(login, *bounds, accepted):
            continue
        accepted.append({**receipt, "login": login})
        by_platform = {}
        for post in ledger:
            checked = _ts(post.get("at"))
            platform = post.get("platform")
            if (post.get("slug") != slug or platform not in platforms or post.get("override")
                    or post.get("found_existing") or not post.get("verified_by") or not post.get("url")
                    or post.get("status", "live") != "live" or checked is None or not dropped <= checked <= now):
                continue
            published = _ts(post.get("published_at"))
            if published is not None and not dropped <= published <= checked:
                published = None
            existing = by_platform.get(platform)
            if existing is None or checked < existing["verified_at"]:
                by_platform[platform] = {"url": post["url"], "verified_at": checked, "published_at": published}
        timely = "incomplete"
        if set(by_platform) == set(required) and all(grade >= bar for bar in required.values()):
            days[day]["verified_all"] += 1
            upper_bounds = [p["published_at"] or p["verified_at"] for p in by_platform.values()]
            if max(upper_bounds) - moment <= 1800:
                timely = "proved within 30 min"
                days[day]["verified_by_30m"] += 1
            elif any(p["published_at"] is not None and p["published_at"] - moment > 1800 for p in by_platform.values()):
                timely = "proved late"
                days[day]["late_all"] += 1
            else:
                timely = "unknown (verification after deadline)"
                days[day]["unknown_30m"] += 1
        verified = [p["verified_at"] for p in by_platform.values()]
        days[day]["approvals"].append({"slug": slug, "moment_at": moment, "approved_at": dropped,
                                       "verified": by_platform, "first_verified_at": min(verified) if verified else None,
                                       "last_verified_at": max(verified) if verified else None, "deadline": timely})
    return {day: {**entry, "eligible_min": round(entry["eligible_min"], 2),
                  "approved_winners": len(entry["approvals"])} for day, entry in sorted(days.items())}


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
            "overrides_excluded": len(ovr), "stream_days": _stream_outcomes(root, since, now, ledger, zone)}


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
                     ("publishing", lambda: publishing(root, since, now)), ("recommend", lambda: recommend(root, now)),
                     ("standing", lambda: standing(root, now))):
        try:
            rep[name] = fn()
        except Exception as exc:                        # one broken source never hides the other five
            rep[name] = _na(f"failed to compute ({type(exc).__name__}: {str(exc)[:80]})")
    return rep


def standing(root: str, now: float) -> dict:
    """The weekly standing lines (OP_SPEC §6, §10): money thresholds and stale rights evidence, so both trend."""
    from pathlib import Path
    from . import brief
    r = Path(root)
    ledger = [row for row in brief.rows(r / "ledger.jsonl") if row.get("status") == "live"]
    money, _ = brief.money(r, now, ledger, brief.rows(r / "data" / "performance.jsonl"))
    roster = brief.read(r / "config" / "creators.json", {}).get("creators") or {}
    rights = brief.rights_status(roster, now)
    stale = [x["login"] for x in rights if x["need"]]
    from . import facecam
    boxes = facecam.box_recheck(now, path=os.path.join(root, "runs", "facecam_checks.jsonl"),
                                creators=os.path.join(root, "config", "creators.json"))
    return {"money": money, "rights_stale": len(stale), "rights_total": len(rights), "rights_stale_logins": stale,
            "webcam_boxes": boxes, "webcam_recheck": len(boxes["recheck"])}


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
               f"{c['catchup_covered_min']} ({c.get('saved_stream_basis', 'basis unavailable')}) · recorded or scanned {c['covered_share']:.0%} · chat-only {_f(c.get('chat_only_min'))} · unknown {c['unknown_min']} · uptime "
               f"{c['uptime_share']:.1%} ({c['incidents']} incident(s), longest dark {c['longest_dark_min']} min"
               + (f", {len(c['unreconciled_incidents'])} with no recovery recorded — not counted"
                  if c.get('unreconciled_incidents') else "") + ") · "
               f"holes>10s {c['holes_over_10s']} ({c['holes_explained']} explained, {c['holes_ads']} ads) · "
               f"oldest re-cuttable {_f(c['oldest_recuttable_h'])} h · "
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
    for day, row in p.get("stream_days", {}).items():
        out.append(f"stream day {day}: {row['eligible_min']:g} eligible min · {row['approved_winners']} approved · "
                   f"{row['verified_all']} verified on all three · {row['verified_by_30m']} proved within 30 min · "
                   f"{row['unknown_30m']} deadline unknown")
    r = rep["recommend"]
    plats = {k: v for k, v in r.items() if k not in ("timezone", "predictions", "posting")}
    rec = " · ".join(f"{k}: " + (v["use"] if "use" in v else ", ".join(f"{h['hour']:02d}h (median {h['median_views']}, "
                                                                        f"n={h['n']})" for h in v["best_hours"])
                                 + f" [{v['from']}→{v['to']}]") for k, v in plats.items()) or r.get("posting", "")
    out.append(f"recommend  {rec} ({r['timezone']}) · {r['predictions']}")
    st = rep.get("standing") or {}
    if st:
        out.append("standing   " + (_f(st) if "unavailable" in st else
                   f"{st['money']} · rights evidence {st['rights_stale']}/{st['rights_total']} missing or stale "
                   f"(>90d; `clipengine rights --stale`)"))
        if isinstance(st.get("webcam_boxes"), dict):
            from . import facecam
            out.append("standing   " + facecam.recheck_line(st["webcam_boxes"]))
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
           ("publishing", "missing_reads_over_24h"), ("standing", "rights_stale"),
           ("standing", "webcam_recheck"))


def compare(a_path: str, b_path: str) -> list[str]:
    a, b = json.load(open(a_path)), json.load(open(b_path))
    out = [f"  {'':<34} {'A':>10} {'B':>10} {'B/A':>7}"]
    for sec, key in COMPARE:
        x, y = (a.get(sec) or {}).get(key), (b.get(sec) or {}).get(key)
        num = lambda v: v if isinstance(v, (int, float)) else None
        ratio = f"{num(y) / num(x):.2f}" if num(x) and num(y) is not None else "–"
        out.append(f"  {sec + ' ' + key:<34} {_f(num(x)):>10} {_f(num(y)):>10} {ratio:>7}")
    stg = lambda r, k: (((r.get("speed") or {}).get("stages") or {}).get(k) or {}).get("p50_min")
    for k in ("moment→draft", "moment→live", "moment→first verification"):
        x, y = stg(a, k), stg(b, k)
        out.append(f"  {'speed ' + k + ' p50 min':<34} {_f(x):>10} {_f(y):>10} "
                   f"{(f'{y / x:.2f}' if x and y is not None else '–'):>7}")
    return out


# ---- the funnel: where moments are lost (v0.5) -------------------------------------------------------------

ROUTES = (("viewer clips", ("viewer clip", "crowd")), ("streamer reaction", ("reaction",)),
          ("tier-1 chat spike", ("tier-1",)), ("sweep", ("sweep",)), ("catch-up", ("catch-up",)),
          ("live spike", ("live spike", "")))


def _route(d: dict) -> str:
    via = str(d.get("via") or d.get("source") or "live spike").lower()
    for name, needles in ROUTES:
        if any(n and n in via for n in needles):
            return name
    return "live spike"


TIMELINE_GUESS = "the stream's own timeline jumped (the streamer's connection)"   # autopilot.TIMELINE_GUESS
UNMEASURED_JUMP = "timeline jump, NOT measured (recorded before the sequence check)"


def funnel(root: str | None = None, now: float | None = None, days: float = 3.0) -> dict:
    """Every route from moment to post, with the reason at each loss: drafts → dropped by the engine → archived by
    triage → waiting → skipped by the editor → published; the viewer-clipped moments we caught or missed (and why);
    recording holes by cause."""
    from collections import Counter
    root = root or REPO
    now = now or time.time()
    since = now - days * DAY
    drafts = [d for d in _drafts(root) if (t := _ts(d.get("drafted_at")) or _moment(d)) and since <= t <= now]
    routes: dict[str, Counter] = {}
    why: dict[str, Counter] = {"dropped": Counter(), "archived": Counter(), "skipped": Counter()}
    for d in drafts:
        st = d.get("status") or "new"
        st = "published" if st in ("published", "posted") else st
        r = routes.setdefault(_route(d), Counter())
        r["drafts"] += 1
        r[st] += 1
        if st in why:
            reason = str(d.get("reason") or d.get("triage_reason") or "no reason saved")
            why[st][re.sub(r"\d+(\.\d+)?", "#", reason)[:70]] += 1
    out = {"window_days": days, "routes": {k: dict(v) for k, v in routes.items()},
           "stale_archives": _stale_archives(drafts), "top_reasons": {k: v.most_common(5) for k, v in why.items()}}
    try:                                                   # the crowd's vote: what viewers clipped, did we have it?
        from . import scouts, tier1
        runs = os.path.join(root, "runs")
        roster = _json(os.path.join(root, "config", "creators.json")).get("creators") or {}
        sd = scouts._drafts(runs)
        spikes = tier1.load_spikes(since, path=os.path.join(runs, "tier1", "spikes.jsonl"))
        clips = [c for c in scouts.observations(now, days * 24, runs) if c.get("views", 0) >= 25
                 and c.get("login") in roster]
        plans, spans = tier1.scout_records(since, runs)
        rows = [scouts.match(c, sd, spikes, scouts._recorded(c["login"], c["t"], runs),
                             tier1.scout_reason(c["login"], c["t"], plans, spans)) for c in clips]
        caught = [r for r in rows if r["caught"]]
        out["viewer_clipped"] = {"moments": len(rows), "caught": len(caught),
                                 "ahead": sum(1 for r in caught if (r.get("lead_s") or 0) > 0),
                                 "missed_why": Counter(r.get("why") or "unknown" for r in rows if not r["caught"]).most_common(5)}
        out["coverage"] = coverage_by_creator(plans, since, now)
        out["clip_lists"] = clip_list_saturation(runs, plans, since, now)
    except Exception as exc:
        out["viewer_clipped"] = _na(f"scouts data unreadable ({type(exc).__name__})")
    holes: Counter = Counter()
    for idx in glob.glob(os.path.join(root, "runs", "*", "index.jsonl")):
        for r in _rows(idx):
            if "gap" in r and since <= r.get("t", 0) <= now and r.get("d", 0) > 10:
                g = str(r.get("gap") or "unknown")
                if g == TIMELINE_GUESS:   # labelled before v0.7 read the playlist's sequence numbers
                    g = UNMEASURED_JUMP
                holes[re.sub(r"\d+(\.\d+)?", "#", g)[:70]] += 1
    out["holes"] = {"over_10s": sum(holes.values()), "by_cause": holes.most_common(6)}
    try:                                                   # §1.4: a trigger in shadow, scored apart
        from . import cliplog, shadow
        evs = []
        for d in glob.glob(os.path.join(root, "runs", "*")):
            if os.path.exists(os.path.join(d, cliplog.NAME)):
                evs += [e for e in cliplog.load(d, since) if (e.get("t") or 0) <= now]
        out["shadow"] = shadow.live_recall(evs)
    except Exception as exc:
        out["shadow"] = _na(f"events unreadable ({type(exc).__name__})")
    return out


def coverage_by_creator(plans: list[dict], since: float, now: float) -> dict:
    """Per roster creator, over the cycles in the window: how many found them live (watched or left out) and how
    many of those put a chat scout on them, with the commonest reason when not (pure)."""
    from collections import Counter
    per: dict[str, Counter] = {}
    n = 0
    for p in plans:
        if not since <= p.get("at", 0) <= now:
            continue
        n += 1
        for l in p.get("watched") or ():
            per.setdefault(l, Counter())["watched"] += 1
        for l, why in (p.get("left_out") or {}).items():
            if why.startswith("live, playing"):
                continue                               # another game: not a moment we'd want
            per.setdefault(l, Counter())[why] += 1
    out = {}
    for l, c in per.items():
        live = sum(c.values())
        reasons = [(k, v) for k, v in c.most_common() if k != "watched"]
        out[l] = {"live_cycles": live, "watched": c["watched"], "share": round(c["watched"] / live, 3),
                  "why_not": reasons[0][0] if reasons else None}
    return {"cycles": n, "creators": out}


def clip_list_saturation(runs: str, plans: list[dict], since: float, now: float) -> dict:
    """Per creator: clip checks whose list was too full to show a fresh 10-view clip (the top-100-by-views list
    holds only clips above that) — the chat scout's polls (runs/tier1/crowd.jsonl; the plan in force says who was
    polled) and each sentinel's (events.jsonl poll rows). Until Helix Get Clips (A1, N1) this is the blind spot."""
    import bisect
    from collections import Counter
    from . import tier1
    polls, full = Counter(), Counter()
    src, ages = Counter(), {"helix": [], "gql": []}
    ats = [p["at"] for p in plans]
    for r in tier1._jsonl(os.path.join(runs, "tier1", "crowd.jsonl")):
        t = r.get("t", 0)
        if not since <= t <= now:
            continue
        s = r.get("src") or "gql"
        src[s] += 1
        ages.setdefault(s, []).extend(r.get("ages") or [])
        i = bisect.bisect_right(ats, t) - 1
        for l in (plans[i].get("watched") or []) if i >= 0 else []:
            polls[l] += 1
        for l in r.get("full") or []:
            full[l] += 1
            if i < 0:
                polls[l] += 1
    for d in glob.glob(os.path.join(runs, "sentinel-*")):
        login = os.path.basename(d)[len("sentinel-"):]
        for name in ("events.1.jsonl", "events.jsonl"):
            for r in tier1._jsonl(os.path.join(d, name)):
                if r.get("ev") != "poll" or not r.get("ran") or not since <= r.get("t", 0) <= now:
                    continue
                polls[login] += 1
                if (r.get("more") or (r.get("n") or 0) >= 100) and (r.get("min_views") or 0) >= 10:
                    full[login] += 1
    per = {l: {"checks": polls[l], "full": full[l], "share": round(full[l] / polls[l], 3) if polls[l] else 1.0}
           for l in full}
    seen = {s: {"polls": src[s], "clips": len(a), "p50_s": _pct(a, 0.5), "within_2min": round(sum(x <= 120 for x in a) / max(len(a), 1), 2)}
            for s, a in ages.items() if src[s]} if src else {}
    return {"checks": sum(polls.values()), "creators": per, "first_seen": seen}


def funnel_lines(f: dict) -> list[str]:
    cols = ("drafts", "dropped", "archived", "new", "skipped", "published")
    out = [f"funnel, last {f['window_days']:g} days — {'route':<18}" + "".join(f"{c:>10}" for c in cols)]
    for name, c in sorted(f["routes"].items(), key=lambda kv: -kv[1].get("drafts", 0)):
        out.append(f"{'':<27}{name:<18}" + "".join(f"{c.get(k, 0):>10}" for k in cols))
    labels = {"dropped": "engine dropped", "archived": "triage archived", "skipped": "editor skipped"}
    for k, v in f["top_reasons"].items():
        if v:
            out.append(f"  {labels[k]}: " + " · ".join(f"{n}× {r}" for r, n in v))
    archives = f.get("stale_archives") or {}
    if any(archives.values()):
        out.append(f"  stale archives: {archives['unseen']} without recorded review · {archives['seen']} reviewed · "
                   f"{archives['legacy_time_unknown']} with archive time unavailable")
    vc = f["viewer_clipped"]
    if "unavailable" in vc:
        out.append("  viewer-clipped moments: " + _f(vc))
    else:
        out.append(f"  viewer-clipped moments (25+ views): {vc['moments']} — we had {vc['caught']} "
                   f"({vc['ahead']} ready before the viewers' clip), missed {vc['moments'] - vc['caught']}"
                   + (": " + " · ".join(f"{n}× {w}" for w, n in vc["missed_why"]) if vc["missed_why"] else ""))
    cov = f.get("coverage") or {}
    if cov.get("cycles"):
        cr = cov["creators"]
        full = [l for l, c in cr.items() if c["share"] >= 0.999]
        low = sorted((kv for kv in cr.items() if kv[1]["share"] < 0.999), key=lambda kv: kv[1]["share"])
        out.append(f"  coverage ({cov['cycles']} cycles): {len(cr)} roster creators live in the game — "
                   f"chat scout on every cycle for {len(full)}"
                   + ("; less: " + " · ".join(f"{l} {c['share']:.0%} ({c['why_not']})" for l, c in low[:6])
                      if low else ""))
    elif "unavailable" not in vc:
        out.append("  coverage: no cycle plans on record yet (runs/tier1/plan.jsonl, written from this version on)")
    cl = f.get("clip_lists") or {}
    for s, v in (cl.get("first_seen") or {}).items():       # A1: how soon a fresh clip is seen, by source
        if v.get("clips"):
            out.append(f"  fresh clips first seen ({'official feed' if s == 'helix' else 'public top-100 list'}, "
                       f"{v['polls']} polls): p50 {v['p50_s']:.0f}s after they were made, {v['within_2min']:.0%} within 2 min"
                       f" ({v['clips']} clips)")
    if cl.get("creators"):
        worst = sorted(cl["creators"].items(), key=lambda kv: -kv[1]["share"])
        out.append("  clip lists too full to show a fresh 10-view clip: "
                   + " · ".join(f"{l} {c['share']:.0%} of {c['checks']} checks" for l, c in worst[:6])
                   + " (Helix Get Clips, N1, would see them)")
    from . import shadow as _SH
    sl = _SH.recall_line(f["shadow"]) if isinstance(f.get("shadow"), dict) and "triggers" in f["shadow"] else None
    if sl:
        out.append("  " + sl)
    h = f["holes"]
    out.append(f"  recording holes >10s: {h['over_10s']}"
               + (" — " + " · ".join(f"{n}× {c}" for c, n in h["by_cause"]) if h["by_cause"] else ""))
    return out
