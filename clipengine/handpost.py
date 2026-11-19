"""Posting with the agent's own browser — for agents whose signed-in browser the engine can't drive.

Some agents run on a VM whose signed-in browser lives elsewhere (a cloud browser their tools open tabs
in), so the engine's poster has nothing to sign in to. Set `"post_with": "agent"` in config/ops.json:
the engine still cuts, checks, scores and schedules every clip into outbox/, and the agent uploads it
with its own browser tools, then records the post so the repeat guard, the ledger and the learning
all keep working:

  clipengine to-post                                   what's due now: file + the exact text per platform
  clipengine posted --slug S --platform tiktok --url U  after it's live (moves the clip to posted/ when done)
  clipengine views  --slug S --platform tiktok --views N  optional: a view count read off the post
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
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


def mark(slug: str, platform: str, url: str) -> str:
    outbox, posted, ledger_p, _ = _dirs()
    if platform not in PLATFORMS:
        raise SystemExit(f"platform must be one of {', '.join(PLATFORMS)}")
    if not url.startswith("http"):
        raise SystemExit("--url must be the live post's link (https://…)")
    d = os.path.join(outbox, slug)
    meta = json.load(open(os.path.join(d, "post.json"))) if os.path.isdir(d) else {}
    clip = os.path.join(d, "clip.mp4")
    sha = _sha(clip) if os.path.isfile(clip) else None
    ledger = _ledger()
    if _live(platform, slug, sha, ledger):
        return f"{slug} on {platform}: already recorded as live"
    rec = {"platform": platform, "slug": slug, "sha256": sha, "title": meta.get("title", slug), "url": url,
           "status": "live", "posted_by": "agent", "at": dt.datetime.now(dt.timezone.utc).isoformat()}
    if meta.get("moment_at"):
        try:
            m = dt.datetime.fromisoformat(meta["moment_at"].replace("Z", "+00:00"))
            rec["moment_to_live_min"] = round((dt.datetime.now(dt.timezone.utc) - m).total_seconds() / 60)
        except ValueError:
            pass
    with open(ledger_p, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    ledger.append(rec)
    left = [p for p in (meta.get("platforms") or list(PLATFORMS)) if not _live(p, slug, sha, ledger)]
    if os.path.isdir(d) and not left:
        os.makedirs(posted, exist_ok=True)
        shutil.move(d, os.path.join(posted, slug))
        return f"{slug}: live on {platform} — all platforms done, moved to posted/"
    return f"{slug}: live on {platform}" + (f" — still to post: {', '.join(left)}" if left else "")


def views(slug: str, platform: str, n: int) -> str:
    perf = _dirs()[3]
    os.makedirs(os.path.dirname(perf), exist_ok=True)
    with open(perf, "a") as fh:
        fh.write(json.dumps({"slug": slug, "platform": platform, "views": int(n), "source": "agent",
                             "checked_at": dt.datetime.now(dt.timezone.utc).isoformat()}) + "\n")
    return f"{slug} on {platform}: {int(n):,} views recorded"
