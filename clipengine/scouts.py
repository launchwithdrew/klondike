"""Were our scouts first? — every moment viewers clipped on a roster streamer, checked against our drafts.

A viewer clip that gets views is the strongest proof a moment was worth posting, and its creation time is
when the crowd reacted. For each one in the last few hours this says: did a scout draft it, and was our
draft ready BEFORE viewers made their clip (a lead) or after (late)? If we missed it, why: a full sentinel
was recording but didn't fire (the detector missed it), only the chat scout watched, or nobody watched.

  clipengine scouts [--hours 6] [--min-views 25] [--explain]

--explain gives every miss ONE concrete cause from the sentinel's own event log (runs/<sentinel>/events.jsonl,
cliplog): sentinel down while the recorder ran, the clip never in its list (the list hides fresh clips), under
10 views at every check, its window outside the recording, folded into another draft, a hung draft thread.
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
import statistics as st
import time

from . import untrusted as UT

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS = os.path.join(REPO, "runs")
BEFORE_S, AFTER_S = 120.0, 30.0       # a viewer clip covers the ~30-60 s before it was made


def _iso(s: str) -> float:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def viewer_clips(login: str, since: float, min_views: int) -> list[dict]:
    from . import twitch as T
    from . import live as L
    q = """query($l: String!) { user(login: $l) { clips(first: 50, criteria: {period: LAST_DAY, sort: VIEWS_DESC}) {
      edges { node { slug title createdAt viewCount durationSeconds videoOffsetSeconds video { createdAt }
      game { name } } } } } }"""
    edges = (((T.gql(q, {"l": login}).get("user") or {}).get("clips") or {}).get("edges") or [])
    out = []
    for e in edges:
        n = e["node"]
        try:
            t = L.moment_time(n)                 # a replay clip is the moment it shows, not when it was made
        except (KeyError, ValueError):
            continue
        if t >= since and int(n.get("viewCount") or 0) >= min_views:
            out.append({"login": login, "t": t, "views": int(n["viewCount"]), "title": n.get("title") or "",
                        "slug": n["slug"], "duration": n.get("durationSeconds"),
                        "game": ((n.get("game") or {}).get("name") or "")})
    return out


def _drafts(root: str = RUNS) -> list[dict]:
    rows = []
    for p in glob.glob(os.path.join(root, "*", "queue.jsonl")):
        for line in open(p):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("login") and r.get("start") and r.get("at") and r.get("engine") is not None:
                try:
                    # never the queue file's mtime: every line in it shares that (24 Nov: a live spike drafted the
                    # minute viewers clipped it read 1002 min late — the file's last append was an archive line)
                    r["_ready"] = _iso(r["drafted_at"]) if r.get("drafted_at") else _iso(r["at"])
                    r["_ready_known"] = bool(r.get("drafted_at"))
                except ValueError:
                    continue
                rows.append(r)
    # saved-stream hand-offs and what became of them (sentinel.run_fallback): a viewer clip our recording missed is
    # handed off without a queue line, so this is the only record that it was tried
    for p in glob.glob(os.path.join(root, "*", "fallback.jsonl")):
        for line in open(p):
            try:
                f = json.loads(line)
                rows.append({"login": f["login"], "start": float(f["start"]), "stop": float(f["stop"]),
                             "fallback": "saved stream", "fallback_result": str(f.get("result") or ""),
                             "_ready": float(f.get("at") or 0), "_ready_known": True, "engine": 0})
            except (ValueError, KeyError, TypeError):
                continue
    return rows


_SPANS: dict[str, tuple[float, list[tuple[float, float]]]] = {}


def _spans(idx: str) -> list[tuple[float, float]]:
    """The stretches a recording index actually covers (segments merged across gaps <= 10 s). The index is
    append-only across sessions, so its first line and mtime say nothing about the hours in between."""
    try:
        mt = os.path.getmtime(idx)
    except OSError:
        return []
    hit = _SPANS.get(idx)
    if hit and hit[0] == mt:
        return hit[1]
    segs = []
    try:
        for line in open(idx):
            try:
                r = json.loads(line)
                segs.append((float(r["t"]), float(r["t"]) + float(r.get("d") or 2.0)))
            except (ValueError, KeyError, TypeError):
                continue
    except OSError:
        return []
    out: list[tuple[float, float]] = []
    for a, b in sorted(segs):
        if out and a <= out[-1][1] + 10:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    _SPANS[idx] = (mt, out)
    return out


def _recorded(login: str, t: float, root: str = RUNS) -> bool:
    """Was this moment inside a stretch we actually recorded (not merely between the first and last segment)?"""
    import bisect
    for d in glob.glob(os.path.join(root, f"*{login}*")):
        spans = _spans(os.path.join(d, "index.jsonl"))
        i = bisect.bisect_right([a for a, _ in spans], t + 5) - 1
        if i >= 0 and spans[i][1] >= t - 5:
            return True
    return False


def match(clip: dict, drafts: list[dict], spikes: list[dict], recorded: bool, scout: str | None = None) -> dict:
    """One viewer clip vs our drafts (pure). lead_s > 0: our draft was ready that long before the clip."""
    lo, hi = clip["t"] - BEFORE_S, clip["t"] + AFTER_S
    near = [d for d in drafts if d["login"] == clip["login"] and d["start"] < hi and d["stop"] > lo]
    hit = [d for d in near if not d.get("fallback")]      # a hand-off line holds no draft: catch-up's draft does
    if hit:
        d = min(hit, key=lambda d: d["_ready"])
        return {**clip, "caught": True, "draft": d["id"],
                "lead_s": round(clip["t"] - d["_ready"]) if d.get("_ready_known", True) else None,
                "via": d.get("via") or "sentinel", "status": d.get("status")}
    if near:
        res = next((d["fallback_result"] for d in near if d.get("fallback_result")), None)
        return {**clip, "caught": False, "why": (f"not in our recording; the saved-stream draft: {res}" if res else
                                                 "the sentinel fired, but the moment wasn't in our recording "
                                                 "and the saved-stream draft never queued")}
    spiked = any(s.get("login") == clip["login"] and lo <= s.get("t", s.get("fire", 0)) <= hi for s in spikes)
    # scout: tier1.scout_reason at the moment — "watched", why no scout was on it, or None (no records: guess
    # from spikes, which can't tell a quiet watched stream from an unwatched one)
    why = ("recorded, but the sentinel didn't fire" if recorded else
           "the chat scout saw a spike, no recorder (catch-up drafts it from the VOD)" if spiked else
           "chat scout only, no spike" if scout == "watched" else
           f"no scout was on this stream ({scout})" if scout else
           "chat scout only, no spike" if spikes is not None and any(s.get("login") == clip["login"] for s in spikes)
           else "no scout was on this stream")
    return {**clip, "caught": False, "why": why}


def _events(login: str, since: float, root: str | None = None) -> list[dict]:
    from . import cliplog as CL
    out: list[dict] = []
    for d in glob.glob(os.path.join(root or RUNS, f"*{login}*")):
        out.extend(CL.load(d, since))
    return sorted(out, key=lambda e: e.get("t", 0))


def explain(row: dict, events: list[dict]) -> str:
    """A miss's concrete cause: the sentinel's log when it has one, else the scouts' own reason."""
    from . import cliplog as CL
    if row.get("caught"):
        return ""
    return CL.explain(row, events, row.get("recorded", False)) or (
        row["why"] + (" (no sentinel event log for that time)" if row.get("recorded") else ""))


def causes(rows: list[dict]) -> list[tuple[str, int]]:
    """Misses grouped by cause (numbers stripped so like causes count together), most common first."""
    import re
    tally: dict[str, int] = {}
    for r in rows:
        if not r.get("caught") and r.get("cause"):
            k = re.sub(r"\(most seen: \d+\)|\([^)]*cutoff up to \d+ views\)|\(\S+-\d{6}\)|\S+-\d{6}", "", r["cause"])
            k = re.sub(r"\s+", " ", k).strip(" —:")
            tally[k] = tally.get(k, 0) + 1
    return sorted(tally.items(), key=lambda kv: -kv[1])


def report(hours: float = 6.0, min_views: int = 25, now: float | None = None, explain_: bool = False) -> dict:
    from .publish import creators
    now = now or time.time()
    since = now - hours * 3600
    try:
        from . import tier1 as T1
        spikes = T1.load_spikes(since)
    except Exception:
        spikes = []
    drafts = _drafts()
    from . import tier1 as T1
    plans, spans = T1.scout_records(since)
    try:
        from .radar import config as _rc
        game = str(_rc().get("game") or "")
    except Exception:
        game = ""
    rows, observations_, off_game = [], [], 0
    for login, c in creators().items():
        src = str(c.get("source") or login)
        if ":" in src or str(c.get("policy") or "").lower() == "never":
            continue                                   # Twitch viewer clips only
        try:
            clips = viewer_clips(login, since, min_views)
        except Exception:
            continue
        if game:                                       # another game's moment isn't ours to catch
            off_game += sum(1 for cl in clips if cl.get("game") and cl["game"].lower() != game.lower())
            clips = [cl for cl in clips if not cl.get("game") or cl["game"].lower() == game.lower()]
        observations_.extend(clips)
        evs = _events(login, since - 3600) if explain_ else []
        for cl in clips:
            rec = _recorded(login, cl["t"])
            row = {**match(cl, drafts, spikes, rec, T1.scout_reason(login, cl["t"], plans, spans)), "recorded": rec}
            if explain_ and not row["caught"]:
                row["cause"] = explain(row, evs)
            rows.append(row)
    remember(observations_, now)
    rows.sort(key=lambda r: -r["views"])
    caught = [r for r in rows if r["caught"]]
    leads = [r["lead_s"] for r in caught if r.get("lead_s") is not None]   # unknown ready time: no claim
    return {"hours": hours, "min_views": min_views, "moments": rows, "caught": len(caught), "total": len(rows),
            "median_lead_s": round(st.median(leads)) if leads else None,
            "first": sum(1 for x in leads if x > 0), "causes": causes(rows) if explain_ else [],
            "off_game": off_game}


def show(hours: float = 6.0, min_views: int = 25, explain_: bool = False) -> dict:
    r = report(hours, min_views, explain_=explain_)
    if not r["total"]:
        print(f"scouts: no viewer clip with {min_views}+ views on the roster in the last {hours:g} h")
        return r
    lead = r["median_lead_s"]
    head = (f"scouts: caught {r['caught']}/{r['total']} moments viewers clipped ({min_views}+ views, last {hours:g} h)"
            + (f" · we were first on {r['first']}/{r['caught']}, median "
               f"{abs(lead) / 60:.1f} min {'ahead of' if lead >= 0 else 'behind'} the viewers' clip" if lead is not None else ""))
    print(head + (f" · {r['off_game']} clip(s) of other games left out" if r.get("off_game") else ""))
    for m in r["moments"][:15]:
        ago = (time.time() - m["t"]) / 60
        if m["caught"]:
            side = (f"{abs(m['lead_s']) / 60:.1f} min {'AHEAD' if m['lead_s'] >= 0 else 'late'}"
                    if m.get("lead_s") is not None else "ready time ?")
            print(f"  ✓ {m['login']:<16} {m['views']:>6} views  {ago:4.0f}m ago  {side:<14} {m['draft']} ({m['via']})")
        else:
            print(f"  ✗ {m['login']:<16} {m['views']:>6} views  {ago:4.0f}m ago  {m.get('cause') or m['why']}  "
                  f"{UT.quote(m['title'], 40)}")
    if explain_:
        misses = r["total"] - r["caught"]
        rec = [m for m in r["moments"] if not m["caught"] and m.get("recorded")]
        top = causes(rec)
        print(f"misses: {misses} · recorded but not drafted: {len(rec)}"
              + (f" · top cause: {top[0][0]} ({top[0][1]}/{len(rec)})" if top else ""))
        for k, n in r["causes"][:8]:
            print(f"  {n:>4} × {k}")
    return r


def remember(clips: list[dict], now: float | None = None, root: str | None = None) -> None:
    """Keep a bounded week of observations from the radar and explicit scouts checks."""
    import tempfile

    if not clips:
        return
    now = time.time() if now is None else now
    root = root or RUNS
    path = os.path.join(root, "scout-observations.json")
    import fcntl
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, ".scout-observations.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        rows = {r["slug"]: r for r in observations(now, hours=168, root=root)}
        for clip in clips:
            if not clip.get("slug") or not clip.get("login"):
                continue
            try:
                at = clip.get("t") if clip.get("t") is not None else _iso(clip["created"])
                views = int(clip.get("views") or 0)
            except (ValueError, TypeError, KeyError):
                continue
            if not isinstance(at, (int, float)) or not now - 7 * 86400 <= at <= now + 60:
                continue
            old = rows.get(clip["slug"]) or {}
            rows[clip["slug"]] = {**old, "slug": clip["slug"], "login": clip["login"], "t": at,
                                  "title": clip.get("title", ""), "views": max(views, old.get("views", 0)),
                                  "duration": clip.get("duration") or old.get("duration")}
        retain = sorted(rows.values(), key=lambda row: row["t"], reverse=True)[:2500]
        with tempfile.NamedTemporaryFile(mode="w", dir=root, prefix=".scouts-", delete=False) as stream:
            json.dump(retain, stream)
        os.replace(stream.name, path)


def observations(now: float, hours: float, root: str | None = None) -> list[dict]:
    path = os.path.join(root or RUNS, "scout-observations.json")
    try:
        with open(path) as stream:
            rows = json.load(stream)
    except (ValueError, OSError):
        return []
    return [row for row in rows if now - hours * 3600 <= row.get("t", 0) <= now]


def cached_report(hours: float, now: float, root: str, roster: dict) -> dict:
    from collections import Counter
    from . import tier1
    drafts = _drafts(root)
    spikes = tier1.load_spikes(now - 7 * 86400, path=os.path.join(root, "tier1", "spikes.jsonl"))
    clips = [row for row in observations(now, 168, root) if row.get("views", 0) >= 25
             and row.get("login") in roster and roster[row["login"]].get("policy") != "never"]
    plans, spans = tier1.scout_records(now - 7 * 86400, root)
    week = [match(clip, drafts, spikes, _recorded(clip["login"], clip["t"], root),
                  tier1.scout_reason(clip["login"], clip["t"], plans, spans)) for clip in clips]
    today = [row for row in week if row["t"] >= now - hours * 3600]
    caught = [row for row in today if row["caught"]]
    misses = Counter(row["why"] for row in week if not row["caught"])
    return {"total": len(today), "caught": len(caught), "first": sum((row.get("lead_s") or 0) > 0 for row in caught),
            "week_reason": misses.most_common(1)[0][0] if misses else None,
            "best_miss": max((row for row in today if not row["caught"]), key=lambda row: row["views"], default=None)}


SHAPE_MIN_CLIPS = 5          # fewer viewer clips than this (or creators below) = sparse: no pattern claimed
SHAPE_MIN_CREATORS = 2


def category_shape(now: float, root: str) -> str | None:
    import re
    from collections import Counter

    seen = observations(now, 24, root)
    clips = sorted(seen, key=lambda r: r.get("views", 0) / max(1, (now - r["t"]) / 60), reverse=True)[:20]
    if not clips:
        return None
    creators = len({r.get("login") for r in clips if r.get("login")})
    source = f"(last 24 h: {len(clips)} of {len(seen)} viewer clips, {creators} creator{'s' if creators != 1 else ''})"
    if len(clips) < SHAPE_MIN_CLIPS or creators < SHAPE_MIN_CREATORS:
        return f"category shapes: sparse {source} — too little to call a pattern"
    durations = [r["duration"] for r in clips if r.get("duration")]
    hooks = Counter("question" if "?" in r["title"] else "reaction" if "!" in r["title"] else "statement" for r in clips)
    ignore = set("the a and or in to of this that for you with just gets gta when from how what it his".split())
    words = Counter(word.lower() for row in clips for word in set(re.findall(r"[A-Za-z]{3,}", row["title"]))
                    if word.lower() not in ignore)
    common = [word for word, count in words.most_common(5) if count >= 2]
    return (f"category shapes: {len(clips)} fastest viewer clips, "
            + (f"{st.median(durations):.0f}s median, " if durations else "")
            + f"{hooks.most_common(1)[0][0]} hooks"
            + (f", recurring words {UT.clean(', '.join(common), 55)}" if common else "") + f" {source}")


def group_angles(queue: list[dict], roster: dict, calendar: list[dict]) -> list[dict]:
    """Collapse nearby drafts from different creators in a shared house or event."""
    from . import events
    from . import sentinel

    def groups(draft):
        login = draft.get("login")
        house = (roster.get(login) or {}).get("house")
        at = draft.get("payoff") or (draft["start"] + draft["stop"]) / 2
        keys = {("house", house.casefold())} if isinstance(house, str) and house else set()
        keys.update(("event", event.get("name") or event.get("start"))
                    for event in events.live(at, calendar) if login in (event.get("creators") or []))
        return keys, at

    remaining = sorted(queue, key=lambda draft: -draft.get("best", 0))
    result = []
    while remaining:
        lead = remaining.pop(0)
        keys, at = groups(lead)
        if not keys:
            result.append(lead)
            continue
        angles = [lead]
        keep = []
        for draft in remaining:
            other, when = groups(draft)
            if keys & other and abs(when - at) <= 60 and draft.get("login") not in {a.get("login") for a in angles}:
                angles.append(draft)
            else:
                keep.append(draft)
        remaining = keep
        if len(angles) > 1:
            lead = {**lead, "angles": [angle["id"] for angle in angles],
                    "angle_urgent": any(sentinel.is_urgent(angle) for angle in angles)}
        result.append(lead)
    return result
