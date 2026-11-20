"""Attention — ONE line of what needs the agent right now, or nothing.

Two kinds of items:
  * state  — recomputed by every `cycle` (drafts worth a look, clips due to post, overdue posts, the daily
             report). Stored in runs/attention.json under "current"; shown until marked read with --seen,
             and shown again only if its key changes (an overdue post gets a new key once it turns overdue).
  * events — failures any process reports as they happen (a sentinel died, a draft failed, a recorder
             hole, the radar failed, a catch-up failed). Appended to runs/attention.jsonl (append-only,
             so sentinels and cycle never overwrite each other); shown until the next --seen.

`clipengine attention` prints the line (nothing at all when nothing needs you); `--seen` marks it read.
When the line gains items it never notified before, the notify hook fires (at most once a minute):
config/ops.json "notify_cmd" (a shell command; the line is in env CLIPENGINE_ATTENTION) and/or
"notify_url" (POST JSON {"text": line}, e.g. an ntfy.sh topic — see AGENTS.md).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS = os.path.join(REPO, "runs")
NOTIFY_EVERY_S = 60

# lower = shown first. Overdue posts first (item 11), then due posts, failures, drafts, setup looks
# (a suggested webcam box), the daily report.
PRIO = {"overdue": 0, "due": 1, "failure": 2, "draft": 3, "setup": 4, "report": 5}


def _paths(root: str | None = None) -> tuple[str, str]:
    root = root or RUNS
    return os.path.join(root, "attention.json"), os.path.join(root, "attention.jsonl")


def _load(root: str | None = None) -> dict:
    try:
        return json.load(open(_paths(root)[0]))
    except (OSError, ValueError):
        return {}


def _save(st: dict, root: str | None = None) -> None:
    p = _paths(root)[0]
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    json.dump(st, open(tmp, "w"), indent=1)
    os.replace(tmp, p)


def event(kind: str, text: str, key: str | None = None, root: str | None = None, now: float | None = None) -> None:
    """Report something that went wrong (any process may call this; it only appends one line)."""
    p = _paths(root)[1]
    os.makedirs(os.path.dirname(p), exist_ok=True)
    rec = {"at": now or time.time(), "kind": kind if kind in PRIO or kind == "resolved" else "failure", "key": key or f"{kind}:{text[:60]}",
           "text": " ".join(str(text).split())[:200]}
    with open(p, "a") as fh:
        fh.write(json.dumps(rec) + "\n")


def set_current(items: list[dict], root: str | None = None) -> None:
    """cycle: the state items as of now (each {kind, key, text, ...})."""
    st = _load(root)
    st["current"] = items
    _save(st, root)


def _events(since: float, root: str | None = None) -> list[dict]:
    out = []
    try:
        for line in open(_paths(root)[1]):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("at", 0) > since:
                out.append(r)
    except OSError:
        pass
    return out


def pending(root: str | None = None) -> list[dict]:
    st = _load(root)
    seen = st.get("seen_keys") or {}
    items = {i["key"]: i for i in (st.get("current") or []) if i.get("key") not in seen}
    for e in _events(st.get("seen_at", 0), root):
        if e["kind"] == "resolved":
            items.pop(e["key"], None)
        else:
            items[e["key"]] = e                           # newest report of the same thing wins
    return sorted(items.values(), key=lambda i: (PRIO.get(i.get("kind"), 9), -float(i.get("score") or 0),
                                                  i.get("at", 0)))


def line(items: list[dict] | None = None, root: str | None = None) -> str:
    """The one line ('' when nothing needs the agent)."""
    items = pending(root) if items is None else items
    if not items:
        return ""
    parts: list[str] = []
    by: dict[str, list[dict]] = {}
    for i in items:
        by.setdefault(i.get("kind", "failure"), []).append(i)
    if by.get("urgent"):
        parts.append(f"🔥 {len(by['urgent'])} URGENT draft(s) — be first: " + ", ".join(i["text"] for i in by["urgent"][:3])
                     + " → clipengine queue")
    overdue = by.get("overdue") or []
    posts = [i for i in overdue if not str(i.get("key", "")).startswith("measure:")]
    measures = [i for i in overdue if str(i.get("key", "")).startswith("measure:")]
    if posts:
        parts.append(f"⏰ {len(posts)} OVERDUE post(s): " + ", ".join(i["text"] for i in posts[:3])
                     + " → clipengine to-post")
    if measures:
        parts.append(f"⏰ {len(measures)} OVERDUE analytics read(s): " + ", ".join(i["text"] for i in measures[:3])
                     + " → clipengine to-measure")
    if by.get("due"):
        parts.append(f"{len(by['due'])} clip(s) due to post: " + ", ".join(i["text"] for i in by["due"][:3])
                     + " → clipengine to-post")
    if by.get("failure"):
        parts.append("⚠ " + "; ".join(i["text"] for i in by["failure"][:4])
                     + (f" (+{len(by['failure']) - 4} more)" if len(by["failure"]) > 4 else ""))
    if by.get("draft"):
        best = by["draft"][0]
        parts.append(f"{len(by['draft'])} new draft(s) worth a look — best {best['text']} → clipengine queue")
    if by.get("setup"):                                   # a one-time look, e.g. a suggested webcam box
        parts.append("🎥 " + "; ".join(i["text"] for i in by["setup"][:2]))
    if by.get("report"):
        parts.append("daily report: " + by["report"][0]["text"])
    return "attention: " + " · ".join(parts)


def mark_seen(root: str | None = None, now: float | None = None) -> int:
    now = now or time.time()
    items = pending(root)
    st = _load(root)
    seen = {k: v for k, v in (st.get("seen_keys") or {}).items() if now - v < 3 * 86400}
    for i in items:
        seen[i["key"]] = now
    st["seen_keys"], st["seen_at"] = seen, now
    _save(st, root)
    _trim_events(root, now)
    return len(items)


def _trim_events(root: str | None, now: float) -> None:
    """Keep the event log small: drop lines older than 3 days."""
    p = _paths(root)[1]
    try:
        if os.path.getsize(p) < 256_000:
            return
        keep = [l for l in open(p) if json.loads(l).get("at", 0) > now - 3 * 86400]
        open(p, "w").writelines(keep)
    except (OSError, ValueError):
        pass


# ---- notify ----------------------------------------------------------------------------------------

def notify(cfg: dict, root: str | None = None, now: float | None = None) -> str | None:
    """Fire notify_cmd / notify_url when the line gained items never notified before (≤ once a minute).
    Returns what it did (for the cycle output), or None."""
    cmd, url = cfg.get("notify_cmd"), cfg.get("notify_url")
    if not cmd and not url:
        return None
    now = now or time.time()
    items = pending(root)
    st = _load(root)
    told = set(st.get("notified_keys") or [])
    new = [i for i in items if i["key"] not in told]
    if not new:
        return None
    if now - st.get("notify_at", 0) < NOTIFY_EVERY_S:
        return None                                        # rate limit: the next cycle sends it
    text = line(items)
    errs = []
    if cmd:
        try:
            r = subprocess.run(cmd, shell=True, env={**os.environ, "CLIPENGINE_ATTENTION": text},
                               capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                errs.append(f"notify_cmd exited {r.returncode}")
        except Exception as exc:
            errs.append(f"notify_cmd failed ({str(exc)[:60]})")
    if url:
        try:
            req = urllib.request.Request(url, data=json.dumps({"text": text}).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=15).read()
        except Exception as exc:
            errs.append(f"notify_url failed ({str(exc)[:60]})")
    st = _load(root)
    if not errs:                                           # failed (offline?): keep them for the next try
        st["notified_keys"] = (list(told) + [i["key"] for i in new])[-500:]
    st["notify_at"] = now
    _save(st, root)
    return "notify: " + ("; ".join(errs) if errs else f"sent ({len(new)} new)")
