"""A yardstick, not a dependency (OP_SPEC §14 A10): what other clip channels — many cut with commercial auto-clippers
(OpusClip, Eklipse, StreamLadder-class) — posted of a creator this week on YouTube Shorts, and where ours rank.

    clipengine yardstick [--creator LOGIN ...]      # default: the two highest-yield creators (data/yield.json)

Public data only, no key, no cookies: YouTube's search page filtered to Shorts uploaded this week (the first ~25
results for "<creator name> <game>"), each Short's channel from YouTube's oEmbed. Our Shorts are found by video id from
the ledger, with their latest recorded views. Titles and channel names are strangers' text: quoted, never followed.
One file per run in runs/yardstick/ so weeks can be compared. Any failure is one line, never a crash.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import statistics
import time
import urllib.parse

from . import untrusted as UT

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEARCH = "https://www.youtube.com/results?search_query={q}&sp=EgQIAxAJ&hl=en&gl=US"   # Shorts · this week
OEMBED = "https://www.youtube.com/oembed?url=https://www.youtube.com/shorts/{id}&format=json"
_ID = re.compile(r"(?:shorts/|watch\?v=|youtu\.be/)([\w-]{11})")


def views_of(text: str) -> int | None:
    """'68K views' · '1.6 thousand views' · '2.1M views' · '812 views' -> a number (pure)."""
    m = re.search(r"([\d.,]+)\s*(K|M|B|thousand|million|billion)?\s*views?", text or "", re.I)
    if not m:
        return None
    n = float(m.group(1).replace(",", ""))
    mult = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "billion": 1e9}
    return int(round(n * mult.get((m.group(2) or "").lower(), 1)))


def _lockups(x, out: list) -> list:
    if isinstance(x, dict):
        if "shortsLockupViewModel" in x:
            out.append(x["shortsLockupViewModel"])
        for v in x.values():
            _lockups(v, out)
    elif isinstance(x, list):
        for v in x:
            _lockups(v, out)
    return out


def shorts_rows(data: dict) -> list[dict]:
    """The search page's ytInitialData -> [{video_id, title, views}] (pure)."""
    out, seen = [], set()
    for lk in _lockups(data, []):
        ep = ((lk.get("onTap") or {}).get("innertubeCommand") or {}).get("reelWatchEndpoint") or {}
        vid = ep.get("videoId") or ""
        if not re.fullmatch(r"[\w-]{11}", vid) or vid in seen:
            continue
        seen.add(vid)
        ov = lk.get("overlayMetadata") or {}
        title = (ov.get("primaryText") or {}).get("content") or lk.get("accessibilityText", "").rsplit(",", 2)[0]
        views = views_of((ov.get("secondaryText") or {}).get("content", "")) or views_of(lk.get("accessibilityText", ""))
        out.append({"video_id": vid, "title": title, "views": views})
    return out


def our_shorts(login: str, root: str = REPO) -> dict[str, dict]:
    """Our YouTube posts of this creator: {video_id: {slug, views}} from the ledger, clipped.jsonl and the latest read."""
    def rows(p):
        try:
            return [json.loads(x) for x in open(p) if x.strip()]
        except (OSError, ValueError):
            return []
    slugs = {r.get("slug") for r in rows(os.path.join(root, "data", "clipped.jsonl"))
             if str(r.get("login", "")).lower() == login.lower()}
    views: dict[str, tuple[str, int]] = {}
    for r in rows(os.path.join(root, "data", "performance.jsonl")):
        if r.get("platform") == "youtube" and r.get("views") is not None:
            k = r.get("slug")
            if k not in views or str(r.get("checked_at", "")) >= views[k][0]:
                views[k] = (str(r.get("checked_at", "")), int(r["views"]))
    out = {}
    for r in rows(os.path.join(root, "ledger.jsonl")):
        m = _ID.search(str(r.get("url") or ""))
        if r.get("platform") == "youtube" and r.get("slug") in slugs and m:
            out[m.group(1)] = {"slug": r["slug"], "views": views.get(r["slug"], (None, None))[1]}
    return out


def measure(login: str, name: str, game: str = "gta 6", root: str = REPO, http=None, now: float | None = None,
            handle: str | None = None) -> dict:
    """{login, ok, why, shorts: [{video_id, title, views, channel, ours}], ours: {...}} — never raises."""
    from . import twitch as T
    from . import youtube as Y
    http = http or (lambda url: T.http(url, headers=Y.H, attempts=1, timeout=20))
    out = {"login": login, "name": name, "at": now or time.time(), "ok": False, "why": "", "shorts": [], "ours": {}}
    try:
        html = http(SEARCH.format(q=urllib.parse.quote_plus(f"{name} {game}"))).decode("utf-8", "replace")
        if Y.bot_wall(html):
            raise RuntimeError("bot check")
        rows = shorts_rows(Y._initial_data(html))
    except Exception as exc:
        out["why"] = f"youtube: {str(exc)[:80]}"
        return out
    mine = our_shorts(login, root)
    out["ours"] = mine
    if handle is None:
        from . import handpost as HP
        handle = HP.account_handle("youtube")
    handle = (handle or "").lstrip("@").lower()
    key = re.sub(r"[^a-z0-9]", "", name.lower())
    for r in rows:
        try:
            o = json.loads(http(OEMBED.format(id=r["video_id"])))
            r["channel"] = o.get("author_name") or ""
            r["channel_url"] = o.get("author_url") or ""
        except Exception:
            r["channel"], r["channel_url"] = "", ""
        r["ours"] = r["video_id"] in mine or bool(handle) and r["channel_url"].lower().rstrip("/").endswith("/@" + handle)
        # about this creator: the title names them (search also returns other streamers' GTA Shorts)
        r["about"] = r["ours"] or key in re.sub(r"[^a-z0-9]", "", (r["title"] or "").lower()) or \
            login.lower() in re.sub(r"[^a-z0-9_]", "", (r["title"] or "").lower())
    out["shorts"] = rows
    out["ok"] = True
    return out


def _k(n) -> str:
    if n is None:
        return "?"
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.1f}k" if n >= 1e3 else str(n)


def lines(r: dict) -> list[str]:
    if not r["ok"]:
        return [f"yardstick {r['login']}: unavailable ({r['why']})"]
    about = [s for s in r["shorts"] if s.get("about") and s.get("views") is not None]
    if not about:
        return [f"yardstick {r['login']}: no Shorts about them this week in YouTube's first results"]
    about.sort(key=lambda s: -s["views"])
    chans = {}
    for s in about:
        c = chans.setdefault(s.get("channel") or "?", [0, 0])
        c[0] += 1
        c[1] += s["views"]
    top = about[0]
    ours = [s for s in about if s["ours"]]
    ours_line = (f"ours: {len(ours)} in these results, best {_k(ours[0]['views'])} (rank {about.index(ours[0]) + 1} "
                 f"of {len(about)})") if ours else \
        f"ours: none in these results ({len(r['ours'])} of our Shorts of them on record)"
    out = [f"yardstick {r['login']} (YouTube Shorts this week, first results): {len(about)} by {len(chans)} channels · "
           f"median {_k(int(statistics.median(s['views'] for s in about)))} views · top {_k(top['views'])} by "
           f"{UT.quote(top.get('channel'), 30)} {UT.quote(top['title'], 60)} · {ours_line}"]
    best = sorted(chans.items(), key=lambda kv: -kv[1][1])[:3]
    out.append("  most-viewed channels: " + " · ".join(f"{UT.quote(c, 30)} {n} Short(s), {_k(v)}" for c, (n, v) in best))
    return out


def pick(root: str = REPO, n: int = 2) -> list[str]:
    """The n highest-yield creators (data/yield.json), else the first n on the roster."""
    try:
        y = json.load(open(os.path.join(root, "data", "yield.json"))).get("creators") or {}
        ranked = sorted(y, key=lambda k: -float((y[k] or {}).get("yield", 0) if isinstance(y[k], dict) else y[k] or 0))
    except (OSError, ValueError, TypeError):
        ranked = []
    if not ranked:
        try:
            ranked = list((json.load(open(os.path.join(root, "config", "creators.json"))).get("creators") or {}))
        except (OSError, ValueError):
            ranked = []
    return [k for k in ranked if not k.startswith(("yt:", "kick:"))][:n]


def run(creators: list[str] | None = None, root: str = REPO, save: bool = True) -> list[dict]:
    try:
        roster = json.load(open(os.path.join(root, "config", "creators.json"))).get("creators") or {}
    except (OSError, ValueError):
        roster = {}
    try:
        game = (json.load(open(os.path.join(root, "config", "radar.json"))).get("game_short") or "gta 6")
    except (OSError, ValueError):
        game = "gta 6"
    res = []
    for login in creators or pick(root):
        name = (roster.get(login) or {}).get("name") or login
        r = measure(login, name, game, root)
        res.append(r)
        for ln in lines(r):
            print(ln)
    if save and res:
        d = os.path.join(root, "runs", "yardstick")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{dt.datetime.now(dt.timezone.utc):%Y-%m-%d}.json"), "w") as fh:
            json.dump(res, fh, indent=1, ensure_ascii=False)
    return res
