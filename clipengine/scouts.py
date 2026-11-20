"""Were our scouts first? — every moment viewers clipped on a roster streamer, checked against our drafts.

A viewer clip that gets views is the strongest proof a moment was worth posting, and its creation time is
when the crowd reacted. For each one in the last few hours this says: did a scout draft it, and was our
draft ready BEFORE viewers made their clip (a lead) or after (late)? If we missed it, why: a full sentinel
was recording but didn't fire (the detector missed it), only the chat scout watched, or nobody watched.

  clipengine scouts [--hours 6] [--min-views 25]
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
    q = """query($l: String!) { user(login: $l) { clips(first: 50, criteria: {period: LAST_DAY, sort: VIEWS_DESC}) {
      edges { node { slug title createdAt viewCount durationSeconds } } } } }"""
    edges = (((T.gql(q, {"l": login}).get("user") or {}).get("clips") or {}).get("edges") or [])
    out = []
    for e in edges:
        n = e["node"]
        try:
            t = _iso(n["createdAt"])
        except (KeyError, ValueError):
            continue
        if t >= since and int(n.get("viewCount") or 0) >= min_views:
            out.append({"login": login, "t": t, "views": int(n["viewCount"]), "title": n.get("title") or "",
                        "slug": n["slug"], "duration": n.get("durationSeconds")})
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
                    r["_ready"] = _iso(r["drafted_at"]) if r.get("drafted_at") else max(_iso(r["at"]), os.path.getmtime(p))
                except ValueError:
                    continue
                rows.append(r)
    return rows


def _recorded(login: str, t: float, root: str = RUNS) -> bool:
    for d in glob.glob(os.path.join(root, f"*{login}*")):
        idx = os.path.join(d, "index.jsonl")
        if not os.path.exists(idx):
            continue
        try:
            first = json.loads(open(idx).readline() or "{}").get("t", 0)
            last = os.path.getmtime(idx)
        except (OSError, ValueError):
            continue
        if first - 5 <= t <= last + 5:
            return True
    return False


def match(clip: dict, drafts: list[dict], spikes: list[dict], recorded: bool) -> dict:
    """One viewer clip vs our drafts (pure). lead_s > 0: our draft was ready that long before the clip."""
    lo, hi = clip["t"] - BEFORE_S, clip["t"] + AFTER_S
    hit = [d for d in drafts if d["login"] == clip["login"] and d["start"] < hi and d["stop"] > lo]
    if hit:
        d = min(hit, key=lambda d: d["_ready"])
        return {**clip, "caught": True, "draft": d["id"], "lead_s": round(clip["t"] - d["_ready"]),
                "via": d.get("via") or "sentinel", "status": d.get("status")}
    spiked = any(s.get("login") == clip["login"] and lo <= s.get("t", s.get("fire", 0)) <= hi for s in spikes)
    why = ("recorded, but the sentinel didn't fire" if recorded else
           "the chat scout saw a spike, no recorder (catch-up drafts it from the VOD)" if spiked else
           "chat scout only, no spike" if spikes is not None and any(s.get("login") == clip["login"] for s in spikes)
           else "no scout was on this stream")
    return {**clip, "caught": False, "why": why}


def report(hours: float = 6.0, min_views: int = 25, now: float | None = None) -> dict:
    from .publish import creators
    now = now or time.time()
    since = now - hours * 3600
    try:
        from . import tier1 as T1
        spikes = T1.load_spikes(since)
    except Exception:
        spikes = []
    drafts = _drafts()
    rows, observations_ = [], []
    for login, c in creators().items():
        src = str(c.get("source") or login)
        if ":" in src or str(c.get("policy") or "").lower() == "never":
            continue                                   # Twitch viewer clips only
        try:
            clips = viewer_clips(login, since, min_views)
        except Exception:
            continue
        observations_.extend(clips)
        for cl in clips:
            rows.append(match(cl, drafts, spikes, _recorded(login, cl["t"])))
    remember(observations_, now)
    rows.sort(key=lambda r: -r["views"])
    caught = [r for r in rows if r["caught"]]
    leads = [r["lead_s"] for r in caught]
    return {"hours": hours, "min_views": min_views, "moments": rows, "caught": len(caught), "total": len(rows),
            "median_lead_s": round(st.median(leads)) if leads else None,
            "first": sum(1 for x in leads if x > 0)}


def show(hours: float = 6.0, min_views: int = 25) -> dict:
    r = report(hours, min_views)
    if not r["total"]:
        print(f"scouts: no viewer clip with {min_views}+ views on the roster in the last {hours:g} h")
        return r
    lead = r["median_lead_s"]
    head = (f"scouts: caught {r['caught']}/{r['total']} moments viewers clipped ({min_views}+ views, last {hours:g} h)"
            + (f" · we were first on {r['first']}/{r['caught']}, median "
               f"{abs(lead) / 60:.1f} min {'ahead of' if lead >= 0 else 'behind'} the viewers' clip" if lead is not None else ""))
    print(head)
    for m in r["moments"][:15]:
        ago = (time.time() - m["t"]) / 60
        if m["caught"]:
            side = f"{abs(m['lead_s']) / 60:.1f} min {'AHEAD' if m['lead_s'] >= 0 else 'late'}"
            print(f"  ✓ {m['login']:<16} {m['views']:>6} views  {ago:4.0f}m ago  {side:<14} {m['draft']} ({m['via']})")
        else:
            print(f"  ✗ {m['login']:<16} {m['views']:>6} views  {ago:4.0f}m ago  {m['why']}  {UT.quote(m['title'], 40)}")
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
    week = [match(clip, drafts, spikes, _recorded(clip["login"], clip["t"], root)) for clip in clips]
    today = [row for row in week if row["t"] >= now - hours * 3600]
    caught = [row for row in today if row["caught"]]
    misses = Counter(row["why"] for row in week if not row["caught"])
    return {"total": len(today), "caught": len(caught), "first": sum(row["lead_s"] > 0 for row in caught),
            "week_reason": misses.most_common(1)[0][0] if misses else None,
            "best_miss": max((row for row in today if not row["caught"]), key=lambda row: row["views"], default=None)}


def category_shape(now: float, root: str) -> str | None:
    import re
    from collections import Counter

    clips = sorted(observations(now, 24, root),
                   key=lambda r: r.get("views", 0) / max(1, (now - r["t"]) / 60), reverse=True)[:20]
    if not clips:
        return None
    durations = [r["duration"] for r in clips if r.get("duration")]
    hooks = Counter("question" if "?" in r["title"] else "reaction" if "!" in r["title"] else "statement" for r in clips)
    ignore = set("the a and or in to of this that for you with just gets gta when from how what it his".split())
    words = Counter(word.lower() for row in clips for word in set(re.findall(r"[A-Za-z]{3,}", row["title"]))
                    if word.lower() not in ignore)
    common = [word for word, count in words.most_common(5) if count >= 2]
    return (f"category shapes: {len(clips)} fastest viewer clips, "
            + (f"{st.median(durations):.0f}s median, " if durations else "")
            + f"{hooks.most_common(1)[0][0]} hooks"
            + (f", recurring words {UT.clean(', '.join(common), 55)}" if common else ""))


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
