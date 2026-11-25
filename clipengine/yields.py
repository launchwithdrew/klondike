"""Roster ranked by yield (OP_SPEC §2.2): which creators actually produce moments worth posting, per hour live.

Four parts, each turned into a percentile among the roster creators that have it (so no unit dominates):
  crowd     viewer-clip views per hour of the game streamed (public: the last 7 days' clips and saved streams)
  moments   viewer-clipped moments (25+ views) per hour of the game streamed (public; Twitch lists a channel's top
            100 clips a week, so a busy creator's count is capped — crowd isn't)
  strong    our drafts scoring 60+ (the lowest platform bar) per hour we saw them live (this install's queue)
  ship      published / judged drafts, shrunk toward 25% until there are a few verdicts (this install's queue)
A part with fewer than 3 creators is left out; the rest are re-weighted. A creator with no part has no yield
(None) and keeps the radar's neutral 0.5.

`cycle` refreshes data/yield.json once a day (one GQL call per roster creator, ~20 s); `clipengine yield` prints
the table. The radar blends it into its `history` term (with posted clips' performance from `learn`), so the
sentinels' slots go to the creators who pay off; a live roster creator the radar's list didn't have is ordered by
it too.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(REPO, "data", "yield.json")
WEIGHTS = {"crowd": 0.3, "moments": 0.2, "strong": 0.3, "ship": 0.2}
STRONG_BAR = 60          # the lowest platform bar (config/distribution.json YouTube)
MOMENT_VIEWS = 25        # what scouts counts as a viewer-clipped moment
MIN_HOURS = 2.0          # fewer hours of the game than this: per-hour numbers are noise
MIN_CREATORS = 3
SHIP_PRIOR = (1, 4)      # (published, judged) added before dividing: 25% until verdicts accumulate
DAY = 86400.0

_PUBLIC = """query($l: String!) { user(login: $l) {
  videos(first: 40, type: ARCHIVE, sort: TIME) { edges { node { createdAt lengthSeconds game { name } } } }
  clips(first: 100, criteria: {period: LAST_WEEK, sort: VIEWS_DESC}) { edges { node { viewCount game { name } } } } } }"""


def _ep(s: str) -> float:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def public_one(login: str, game: str, now: float, gql=None) -> dict:
    """{"hours", "views", "moments"} of `game` over the last 7 days, from Twitch's public listings."""
    from . import twitch as T
    u = ((gql or T.gql)(_PUBLIC, {"l": login}) or {}).get("user") or {}
    g = game.lower()
    hours = sum(float(v.get("lengthSeconds") or 0) for v in (e["node"] for e in (u.get("videos") or {}).get("edges") or [])
                if ((v.get("game") or {}).get("name") or "").lower() == g and _ep(v["createdAt"]) >= now - 7 * DAY) / 3600
    every = [e["node"] for e in (u.get("clips") or {}).get("edges") or []]
    if hours >= MIN_HOURS and not every:
        # hours of streaming and not one clip of anything all week: Twitch answered with an empty list (seen Nov 24
        # on 2 creators with 90+ clips a minute later) — a failed read, not a creator with no yield
        raise RuntimeError("empty clip list")
    clips = [c for c in every if ((c.get("game") or {}).get("name") or "").lower() == g]
    return {"hours": round(hours, 1), "views": sum(int(c.get("viewCount") or 0) for c in clips),
            "moments": sum(1 for c in clips if int(c.get("viewCount") or 0) >= MOMENT_VIEWS)}


def own(root: str, since: float, now: float) -> dict[str, dict]:
    """Per creator from this install: hours seen live, drafts, strong drafts, published, judged."""
    from . import metrics as MX
    out: dict[str, dict] = {}
    for login, ivs in MX._live(root, since, now).items():
        out.setdefault(login, {})["live_h"] = round(sum(e - s for s, e in ivs) / 3600, 1)
    for d in MX._drafts(root):
        t = MX._ts(d.get("drafted_at")) or MX._moment(d)
        login = str(d.get("login") or "").lower()
        if not login or not t or not since <= t <= now:
            continue
        o = out.setdefault(login, {})
        st = d.get("status") or "new"
        o["drafts"] = o.get("drafts", 0) + 1
        if st != "dropped" and float(d.get("best") or 0) >= STRONG_BAR:
            o["strong"] = o.get("strong", 0) + 1
        if st in ("published", "posted"):
            o["published"] = o.get("published", 0) + 1
        if st in ("published", "posted", "skipped"):
            o["judged"] = o.get("judged", 0) + 1
    return out


def parts(public: dict[str, dict], mine: dict[str, dict]) -> dict[str, dict[str, float]]:
    """Raw per-creator values of each part (pure); a creator lacks a part it has no evidence for."""
    raw: dict[str, dict[str, float]] = {}
    for login, p in public.items():
        if (p.get("hours") or 0) >= MIN_HOURS:
            raw.setdefault(login, {})["crowd"] = math.log1p(p.get("views", 0) / p["hours"])
            raw[login]["moments"] = p.get("moments", 0) / p["hours"]
    for login, o in mine.items():
        if (o.get("live_h") or 0) >= MIN_HOURS:
            raw.setdefault(login, {})["strong"] = o.get("strong", 0) / o["live_h"]
        if o.get("judged"):
            raw.setdefault(login, {})["ship"] = (o.get("published", 0) + SHIP_PRIOR[0]) / (o["judged"] + SHIP_PRIOR[1])
    return raw


def rank(raw: dict[str, dict[str, float]], roster: dict | None = None) -> dict[str, dict]:
    """{login: {"yield": 0-1, "parts": {part: percentile}}} (pure). Percentiles are among creators with that part
    (ties share the mean rank); parts with fewer than MIN_CREATORS creators are left out."""
    pool = {l: v for l, v in raw.items() if roster is None or l in roster}
    pct: dict[str, dict[str, float]] = {l: {} for l in pool}
    for part in WEIGHTS:
        have = sorted((v[part], l) for l, v in pool.items() if part in v)
        if len(have) < MIN_CREATORS:
            continue
        vals = [x for x, _ in have]
        for x, l in have:
            lo = vals.index(x)
            hi = len(vals) - 1 - vals[::-1].index(x)
            pct[l][part] = round(((lo + hi) / 2) / (len(vals) - 1), 3)
    out = {}
    for l, ps in pct.items():
        if not ps:
            continue
        w = sum(WEIGHTS[p] for p in ps)
        out[l] = {"yield": round(sum(WEIGHTS[p] * v for p, v in ps.items()) / w, 3), "parts": ps}
    return out


def refresh(root: str | None = None, now: float | None = None, gql=None, write: bool = True) -> dict:
    """Measure both halves and write data/yield.json; returns what it wrote. A creator whose public read fails is
    left out of the public half (named under "errors"), never the whole refresh."""
    from . import radar as R
    root = root or REPO
    now = now or time.time()
    try:
        roster = json.load(open(os.path.join(root, "config", "creators.json"))).get("creators") or {}
    except (OSError, ValueError):
        roster = {}
    game = R.config().get("game", "Grand Theft Auto VI")
    public, errors = {}, {}
    for login in roster:
        if ":" in login:                     # Kick / YouTube: no Twitch listings
            continue
        for attempt in (1, 2):                   # one retry: a hiccup shouldn't cost a creator its day
            try:
                public[login] = public_one(login, game, now, gql)
                errors.pop(login, None)
                break
            except Exception as exc:
                errors[login] = str(exc)[:80]
    mine = own(root, now - 14 * DAY, now)
    ranked = rank(parts(public, mine), roster)
    rep = {"updated": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(timespec="seconds"),
           "window": {"public_days": 7, "own_days": 14}, "weights": WEIGHTS,
           "creators": {l: {**ranked.get(l, {"yield": None}), "public": public.get(l), "own": mine.get(l)}
                        for l in roster if ":" not in l},
           **({"errors": errors} if errors else {})}
    if write:
        path = os.path.join(root, "data", "yield.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w") as fh:
            json.dump(rep, fh, indent=1)
        os.replace(path + ".tmp", path)
    return rep


def load(path: str | None = None) -> dict[str, float]:
    """{login: yield 0-1} from data/yield.json; {} when there isn't one."""
    try:
        d = json.load(open(path or PATH))
    except (OSError, ValueError):
        return {}
    return {l: float(v["yield"]) for l, v in (d.get("creators") or {}).items() if v.get("yield") is not None}


def lines(rep: dict, top: int = 50) -> list[str]:
    """The table `clipengine yield` prints: best first, each part's percentile and the numbers behind it."""
    rows = sorted(rep.get("creators", {}).items(), key=lambda kv: -(kv[1].get("yield") if kv[1].get("yield") is not None else -1))
    out = [f"yield (updated {rep.get('updated', '?')}; public: last 7 days, ours: last 14 days)",
           f"{'yield':>5}  {'creator':<18} {'crowd':>5} {'mom.':>5} {'strong':>6} {'ship':>5}   behind it"]
    for l, v in rows[:top]:
        ps, pub, mine = v.get("parts") or {}, v.get("public") or {}, v.get("own") or {}
        f = lambda k: f"{ps[k]:.2f}" if k in ps else "  -  "
        why = []
        if pub.get("hours"):
            why.append(f"{pub['hours']:.0f} h streamed, {pub.get('views', 0):,} clip views, {pub.get('moments', 0)} moments")
        if mine:
            why.append(f"ours: {mine.get('live_h', 0):.0f} h live, {mine.get('strong', 0)}/{mine.get('drafts', 0)} strong drafts, "
                       f"{mine.get('published', 0)}/{mine.get('judged', 0)} shipped")
        y = f"{v['yield']:.2f}" if v.get("yield") is not None else "  -  "
        out.append(f"{y:>5}  {l:<18} {f('crowd'):>5} {f('moments'):>5} {f('strong'):>6} {f('ship'):>5}   "
                   + ("; ".join(why) or "no evidence yet"))
    if rep.get("errors"):
        out.append(f"public read failed for {len(rep['errors'])}: " + ", ".join(sorted(rep["errors"]))[:120])
    return out
