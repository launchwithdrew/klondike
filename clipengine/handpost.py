"""Posting with the agent's own browser — for agents whose signed-in browser the engine can't drive.

Some agents run on a VM whose signed-in browser lives elsewhere (a cloud browser their tools open tabs
in), so the engine's poster has nothing to sign in to. Set `"post_with": "agent"` in config/ops.json:
the engine still cuts, checks, scores and schedules every clip into outbox/, and the agent uploads it
with its own browser tools, then records the post so the repeat guard, the ledger and the learning
all keep working:

  clipengine to-post                                   what's due now: file + the exact text per platform
  clipengine posted --slug S --platform tiktok --url U  after it's live (moves the clip to posted/ when done)
  clipengine views  --slug S --platform tiktok --views N [--watch-s --skip --shares ...]  what analytics show
  clipengine to-measure                                 posts due a 1 h / 24 h / 72 h analytics read (with links)
  clipengine followers --platform P --count N           follower count (report: progress to payout programs)
"""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLATFORMS = ("youtube", "instagram", "tiktok")


def _dirs():
    return (os.path.join(REPO, "outbox"), os.path.join(REPO, "posted"), os.path.join(REPO, "ledger.jsonl"),
            os.path.join(REPO, "data", "performance.jsonl"))


def enabled() -> bool:
    from .ops import config
    return config().get("post_with") == "agent"


def _ledger() -> list[dict]:
    path = _dirs()[2]
    out = []
    if os.path.exists(path):
        for line in open(path):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _live(platform: str, slug: str, sha: str | None, ledger: list[dict]) -> dict | None:
    return next((r for r in ledger if r.get("platform") == platform and r.get("status") == "live"
                 and (r.get("slug") == slug or (sha and r.get("sha256") == sha))), None)


def captions(meta: dict) -> dict:
    """Exactly what the engine's own poster would type on each platform."""
    t, d = meta.get("title", ""), meta.get("description", "")
    return {"youtube": {"title": t, "description": d},
            "instagram": {"caption": f"{t}\n{d}".strip()},
            "tiktok": {"caption": f"{t} {d}".strip()}}


def due(now: float | None = None) -> list[dict]:
    outbox = _dirs()[0]
    now = now or dt.datetime.now(dt.timezone.utc).timestamp()
    ledger = _ledger()
    items = []
    for name in sorted(os.listdir(outbox)) if os.path.isdir(outbox) else []:
        d = os.path.join(outbox, name)
        meta_p, clip = os.path.join(d, "post.json"), os.path.join(d, "clip.mp4")
        if not (os.path.isfile(meta_p) and os.path.isfile(clip)):
            continue
        meta = json.load(open(meta_p))
        sha = _sha(clip)
        sched = meta.get("schedule") or {}
        plats, later = [], {}
        for p in meta.get("platforms") or list(PLATFORMS):
            if _live(p, meta.get("slug", name), sha, ledger):
                continue
            at = sched.get(p)
            ts = dt.datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp() if at else None
            if ts and ts > now:
                later[p] = at
            else:
                plats.append(p)
        if plats or later:
            items.append({"slug": meta.get("slug", name), "file": clip, "now": plats, "later": later,
                          "captions": captions(meta), "sha256": sha})
    return items


def show() -> list[dict]:
    items = due()
    if not items:
        print("to-post: nothing due")
        return items
    for it in items:
        if not it["now"]:
            print(f"{it['slug']}: scheduled — " + ", ".join(f"{p} at {a}" for p, a in it["later"].items()))
            continue
        print(f"{it['slug']}  file: {it['file']}")
        for p in it["now"]:
            c = it["captions"][p]
            if p == "youtube":
                print(f"  youtube (Shorts upload) — title: {c['title']}\n    description: {c['description']}")
            else:
                print(f"  {p} — caption: {c['caption']}")
        if it["later"]:
            print("  later: " + ", ".join(f"{p} at {a}" for p, a in it["later"].items()))
        print(f"  then: clipengine posted --slug {it['slug']} --platform <p> --url <link>")
    return items


OVERRIDE_HALF_WINDOW_S = 30   # an override with no drop: the moment is guarded ±30 s around --moment-at


def _drop_meta(slug: str) -> tuple[str, dict]:
    """The clip's drop folder (outbox/ while platforms are due, posted/ once all are done) and its post.json."""
    outbox, posted, _, _ = _dirs()
    for d in (os.path.join(outbox, slug), os.path.join(posted, slug)):
        if os.path.isfile(os.path.join(d, "post.json")):
            return d, json.load(open(os.path.join(d, "post.json")))
    return os.path.join(outbox, slug), {}


def mark(slug: str, platform: str, url: str, confirmed: bool = False, override: str | None = None,
         login: str | None = None, moment_at: str | None = None) -> str:
    """Record a live post. Only what the gate approved records by itself: the clip needs a drop (publish /
    publish-queued) and the platform must be one the drop lists. Anything else is an operator override — it
    needs `override` (the reason, kept in the ledger, and learn leaves it out of the weights); with no drop it
    also needs the creator and moment time, so the repeat guard still knows the moment."""
    from . import attention
    from . import moments
    from . import verify

    outbox, posted, ledger_p, _ = _dirs()
    if platform not in PLATFORMS:
        raise SystemExit(f"platform must be one of {', '.join(PLATFORMS)}")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", slug):
        raise SystemExit("--slug must be the clip's slug")
    override = (override or "").strip() or None
    d, meta = _drop_meta(slug)
    guard: dict = {}
    if not meta:
        if not override:
            raise SystemExit(f"POST NOT RECORDED: {slug} has no drop — only clips that went through publish / "
                             "publish-queued record by themselves. Posted anyway on the owner's say-so? Re-run with "
                             '--override "reason" --login CREATOR --moment-at ISO-TIME')
        t = moments.timestamp(moment_at) if moment_at else None
        if not login or t is None:
            raise SystemExit("POST NOT RECORDED: an override with no drop needs --login CREATOR and --moment-at "
                             "(the moment's UTC time, e.g. 2026-11-19T21:42:37Z) so the repeat guard knows it")
        guard = moments.metadata(login, t - OVERRIDE_HALF_WINDOW_S, t + OVERRIDE_HALF_WINDOW_S)
        guard["moment_at"] = moments.iso(t)
    elif meta.get("platforms") and platform not in meta["platforms"] and not override:
        raise SystemExit(f"POST NOT RECORDED: the gate approved {slug} for {', '.join(meta['platforms'])} only, "
                         f'not {platform}. Posted anyway on the owner\'s say-so? Re-run with --override "reason"')
    clip = os.path.join(d, "clip.mp4")
    sha = _sha(clip) if os.path.isfile(clip) else None
    ledger = _ledger()
    if _live(platform, slug, sha, ledger):
        return f"{slug} on {platform}: already recorded as live"
    event_key = f"post-verification:{slug}:{platform}"
    attention_root = os.path.join(REPO, "runs")
    try:
        verification = verify.post(platform, url, confirmed)
    except ValueError as exc:
        attention.event("overdue", f"verify {slug} on {platform}: {exc}", key=event_key, root=attention_root)
        raise SystemExit(f"POST NOT RECORDED: {exc}") from None
    rec = {"platform": platform, "slug": slug, "sha256": sha, "title": meta.get("title", slug), "url": url,
           "status": "live", "verified_by": verification, "posted_by": "agent", "at": dt.datetime.now(dt.timezone.utc).isoformat()}
    for field in ("login", "moment_at", "moment_start_at", "moment_end_at"):
        if meta.get(field) or guard.get(field):
            rec[field] = meta.get(field) or guard[field]
    if override:
        rec["override"] = override
    if rec.get("moment_at"):
        try:
            m = dt.datetime.fromisoformat(rec["moment_at"].replace("Z", "+00:00"))
            rec["moment_to_live_min"] = round((dt.datetime.now(dt.timezone.utc) - m).total_seconds() / 60)
        except ValueError:
            pass
    with open(ledger_p, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        ledger = _ledger()
        if _live(platform, slug, sha, ledger):
            return f"{slug} on {platform}: already recorded as live"
        fh.write(json.dumps(rec) + "\n")
    ledger.append(rec)
    attention.event("resolved", "", key=event_key, root=attention_root)
    from . import sentinel
    for draft in sentinel.load_queue(attention_root):
        if draft.get("slug") == slug and not draft.get("posted_at"):
            sentinel.note(draft, posted_at=rec["at"])
    left = [p for p in (meta.get("platforms") or list(PLATFORMS)) if not _live(p, slug, sha, ledger)]
    if not meta:
        _sync([ledger_p], f"posted by agent (override): {slug} ({platform})")
        return f"{slug}: live on {platform} — recorded as an override (outside the gate; not learned from)"
    if os.path.isdir(d) and d.startswith(outbox) and not left:
        os.makedirs(posted, exist_ok=True)
        shutil.move(d, os.path.join(posted, slug))
        _sync([ledger_p, d, os.path.join(posted, slug)], f"posted by agent: {slug} ({platform})")
        return f"{slug}: live on {platform} — all platforms done, moved to posted/"
    _sync([ledger_p], f"posted by agent{' (override)' if override else ''}: {slug} ({platform})")
    note = " (override: outside the gate; not learned from)" if override else ""
    return f"{slug}: live on {platform}{note}" + (f" — still to post: {', '.join(left)}" if left else "")


def _sync(paths: list[str], msg: str) -> None:
    """In the shared operation repo, a recorded post or view is committed and pushed like the poster's own
    (one machine's record is everyone's repeat guard). A public copy never touches git."""
    from . import repo as RP
    if not RP.sync():
        return
    import subprocess
    g = lambda *a: subprocess.run(["git", "-C", REPO, *a], capture_output=True, text=True)
    g("add", *[os.path.relpath(p, REPO) for p in paths if os.path.exists(p)])
    if g("diff", "--cached", "--quiet").returncode == 0:
        return
    g("-c", "user.name=clipengine", "-c", "user.email=clipengine@users.noreply.github.com", "commit", "-qm", msg)
    g("pull", "-q", "--no-rebase", "--no-edit")
    g("push", "-q")


RICH = {"watch_s": "average watch time (s)", "skip": "skip rate (%)", "shares": "shares", "saves": "saves",
        "follows": "follows from the post", "likes": "likes", "comments": "comments"}


def _live_at(slug: str, platform: str, ledger: list[dict] | None = None) -> float | None:
    for r in ledger if ledger is not None else _ledger():
        if r.get("slug") == slug and r.get("platform") == platform and r.get("status") == "live":
            try:
                return dt.datetime.fromisoformat(str(r.get("at")).replace("Z", "+00:00")).timestamp()
            except ValueError:
                return None
    return None


def views(slug: str, platform: str, n: int, now: float | None = None, **rich) -> str:
    """Record what a post's analytics show: views, plus whatever else the agent read (watch time, skip rate,
    shares, saves, follows, likes, comments). Shares and watch time teach more than views do."""
    if platform not in PLATFORMS:
        raise SystemExit(f"platform must be one of {', '.join(PLATFORMS)}")
    perf = _dirs()[3]
    now = now or dt.datetime.now(dt.timezone.utc).timestamp()
    rec = {"slug": slug, "platform": platform, "views": int(n)}
    for k in RICH:
        v = rich.get(k)
        if v is None:
            continue
        if k == "skip":
            v = float(v)
            if not 0 <= v <= 100:
                raise SystemExit("--skip is a percentage, 0-100")
            rec["skip_pct"] = round(v, 1)
        elif k == "watch_s":
            rec["watch_s"] = round(float(v), 2)
        else:
            rec[k] = int(v)
    t0 = _live_at(slug, platform)
    if t0:
        rec["age_h"] = round((now - t0) / 3600, 2)
    rec.update({"source": "agent", "checked_at": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat()})
    os.makedirs(os.path.dirname(perf), exist_ok=True)
    with open(perf, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    _sync([perf], f"views: {slug} {platform} {int(n)}")
    extra = [f"{rec[k]}{'s' if k == 'watch_s' else '%' if k == 'skip_pct' else ' ' + k}"
             for k in ("watch_s", "skip_pct", "shares", "saves", "follows") if k in rec]
    return f"{slug} on {platform}: {int(n):,} views recorded" + (f" ({', '.join(extra)})" if extra else "")


# ---- what to measure next ----------------------------------------------------------------------------

READS = (("1h", 1.0, 0.5, 6.0), ("24h", 24.0, 12.0, 48.0), ("72h", 72.0, 48.0, 120.0))   # same windows as learn


def to_measure(now: float | None = None, ledger: list[dict] | None = None, perf: list[dict] | None = None,
               lookback_h: float = 7 * 24) -> list[dict]:
    """Live posts due a 1 h / 24 h / 72 h analytics read, and posts over 24 h old with no read at all."""
    now = now or dt.datetime.now(dt.timezone.utc).timestamp()
    ledger = _ledger() if ledger is None else ledger
    if perf is None:
        perf = []
        if os.path.exists(_dirs()[3]):
            for line in open(_dirs()[3]):
                try:
                    perf.append(json.loads(line))
                except ValueError:
                    pass
    ages: dict[tuple, list[float]] = {}
    for r in perf:
        if r.get("views") is None or not r.get("slug"):
            continue
        key = (r["slug"], r.get("platform"))
        t0 = _live_at(*key, ledger)
        age = r.get("age_h")
        if age is None and t0:
            try:
                age = (dt.datetime.fromisoformat(str(r.get("checked_at")).replace("Z", "+00:00")).timestamp() - t0) / 3600
            except ValueError:
                age = None
        ages.setdefault(key, []).append(age if age is not None else -1.0)
    out, seen = [], set()
    for r in ledger:
        if r.get("status") != "live" or not r.get("slug"):
            continue
        key = (r["slug"], r.get("platform"))
        if key in seen:
            continue
        seen.add(key)
        t0 = _live_at(*key, ledger)
        if not t0:
            continue
        age = (now - t0) / 3600
        if age > lookback_h:
            continue
        have = ages.get(key, [])
        due = None
        for name, target, lo, hi in READS:
            if target <= age <= hi and not any(lo <= a <= hi for a in have):
                due = name
        item = {"slug": key[0], "platform": key[1], "url": r.get("url", ""), "age_h": round(age, 1), "due": due,
                "missing": age > 24 and not have}
        if due or item["missing"]:
            out.append(item)
    out.sort(key=lambda i: (not i["missing"], -i["age_h"]))
    return out


def show_measure(now: float | None = None, limit: int = 15) -> list[dict]:
    items = to_measure(now, lookback_h=float("inf"))
    if not items:
        print("to-measure: nothing due")
        return items
    for it in items[:limit]:
        tag = "NO DATA YET — " if it["missing"] else ""
        what = f"{it['due']} read" if it["due"] else "any read"
        print(f"{tag}{it['slug']} {it['platform']} ({it['age_h']:g} h) {what}: {it['url'] or '(no link)'}")
    if len(items) > limit:
        print(f"... +{len(items) - limit} more")
    print("then: clipengine views --slug S --platform P --views N [--watch-s --skip --shares --saves --follows "
          "--likes --comments]")
    return items


# ---- followers (money thresholds) --------------------------------------------------------------------

def followers(platform: str, count: int, now: float | None = None) -> str:
    """Record an account's follower/subscriber count read off its profile (report tracks payout thresholds)."""
    if platform not in PLATFORMS:
        raise SystemExit(f"platform must be one of {', '.join(PLATFORMS)}")
    if count < 0:
        raise SystemExit("--count must be 0 or more")
    path = os.path.join(REPO, "data", "followers.jsonl")
    now = now or dt.datetime.now(dt.timezone.utc).timestamp()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps({"platform": platform, "followers": int(count),
                             "at": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat()}) + "\n")
    _sync([path], f"followers: {platform} {int(count)}")
    return f"{platform}: {int(count):,} followers recorded"
