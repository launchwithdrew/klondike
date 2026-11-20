"""The bank — every finished clip, per platform, with one state, so nothing is posted twice or lost mid-upload.

States of one clip on one platform (outbox/<slug> × the platforms its drop lists):

  waiting    scheduled for later (the distribution gate's slot) — reserve inventory
  ready      due now, nobody has it
  claimed    `clipengine claim --slug S --platform P`: one poster holds it under a lease (30 min,
             config/ops.json "claim_lease_min"). The claim IS the attempt record, written before the clip is
             handed to the browser. Claims are atomic (an OS lock): two posters can't take the same platform.
  uncertain  the lease ran out with no outcome — the upload may or may not have happened. Nobody can claim it
             again until it is reconciled: open the account; if it is live, `posted --url …`; if it isn't,
             `attempt --slug S --platform P --failed "why"`. It goes on the attention line as overdue.
  live       recorded by `posted` (the ledger)

`attempt --failed "why"` closes an attempt that didn't post (an upload error, a refused upload), and the platform
is ready again; `attempt --release` gives back a claim that never reached the browser. Every step is one line in
data/attempts.jsonl (committed like the ledger), so another session — or another machine after a pull — sees it.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import secrets
import socket
import time
from contextlib import contextmanager

from . import handpost as HP

LEASE_MIN = 30.0
OPEN = ("claimed", "uncertain")


def _paths() -> tuple[str, str]:
    return os.path.join(HP.REPO, "data", "attempts.jsonl"), os.path.join(HP.REPO, "runs", "bank.lock")


def lease_min() -> float:
    try:
        from .ops import config
        return float(config().get("claim_lease_min") or LEASE_MIN)
    except Exception:
        return LEASE_MIN


def poster_name() -> str:
    return os.environ.get("CLIPENGINE_POSTER") or socket.gethostname()


@contextmanager
def _locked():
    _, lock = _paths()
    os.makedirs(os.path.dirname(lock), exist_ok=True)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def events() -> list[dict]:
    path, _ = _paths()
    out = []
    try:
        with open(path) as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def _append(ev: dict) -> None:
    path, _ = _paths()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(ev) + "\n")


def _iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat(timespec="seconds")


def state(slug: str, platform: str, now: float | None = None, evs: list[dict] | None = None,
          ledger: list[dict] | None = None, sha: str | None = None) -> dict:
    """{state, attempt, by, until, failures, last_failure} for one clip on one platform (ignores schedules)."""
    now = time.time() if now is None else now
    ledger = HP._ledger() if ledger is None else ledger
    mine = [e for e in (events() if evs is None else evs) if e.get("slug") == slug and e.get("platform") == platform]
    fails = [e for e in mine if e.get("ev") == "failed"]
    out = {"state": "ready", "attempt": None, "by": None, "until": None, "failures": len(fails),
           "last_failure": fails[-1].get("why") if fails else None}
    if HP._live(platform, slug, sha, ledger):
        return {**out, "state": "live"}
    last = mine[-1] if mine else None
    if last and last.get("ev") == "claim":
        st = "claimed" if now < float(last.get("until", 0)) else "uncertain"
        return {**out, "state": st, "attempt": last["id"], "by": last.get("by"), "until": last.get("until")}
    return out


def inventory(now: float | None = None) -> list[dict]:
    """One row per drop × platform: the bank."""
    now = time.time() if now is None else now
    outbox = HP._dirs()[0]
    evs, ledger, rows = events(), HP._ledger(), []
    for name in sorted(os.listdir(outbox)) if os.path.isdir(outbox) else []:
        d = os.path.join(outbox, name)
        meta_p, clip = os.path.join(d, "post.json"), os.path.join(d, "clip.mp4")
        if not (os.path.isfile(meta_p) and os.path.isfile(clip)):
            continue
        meta = json.load(open(meta_p))
        slug, sha = meta.get("slug", name), HP._sha(clip)
        for p in meta.get("platforms") or list(HP.PLATFORMS):
            st = state(slug, p, now, evs, ledger, sha)
            at = (meta.get("schedule") or {}).get(p)
            ts = dt.datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp() if at else None
            if st["state"] == "ready" and ts and ts > now:
                st = {**st, "state": "waiting"}
            rows.append({"slug": slug, "platform": p, "file": clip, "meta": meta, "sha256": sha, "scheduled": at,
                         "moment_at": meta.get("moment_at"), **st})
    return rows


def _row(slug: str, platform: str, now: float) -> dict | None:
    return next((r for r in inventory(now) if r["slug"] == slug and r["platform"] == platform), None)


def claim(slug: str, platform: str, by: str | None = None, now: float | None = None) -> dict:
    """Take one clip × platform for posting: atomic, leased, and recorded BEFORE the browser hand-off."""
    from . import migrate as MG
    MG.guard_publish()                           # only the leading machine posts
    by = by or poster_name()
    with _locked():
        now = time.time() if now is None else now
        r = _row(slug, platform, now)
        if r is None:
            raise SystemExit(f"NOT CLAIMED: {slug} has no drop for {platform} in outbox/ (`clipengine bank` lists them)")
        if r["state"] == "live":
            raise SystemExit(f"NOT CLAIMED: {slug} is already live on {platform}")
        if r["state"] == "waiting":
            raise SystemExit(f"NOT CLAIMED: {slug} on {platform} is scheduled for {r['scheduled']} — not due yet")
        if r["state"] == "claimed":
            raise SystemExit(f"NOT CLAIMED: {r['by']} holds {slug} on {platform} until {_iso(r['until'])} "
                             f"(attempt {r['attempt']})")
        if r["state"] == "uncertain":
            raise SystemExit(reconcile_hint(r))
        aid = f"a-{secrets.token_hex(4)}"
        until = now + lease_min() * 60
        _append({"ev": "claim", "id": aid, "slug": slug, "platform": platform, "by": by, "at": _iso(now),
                 "ts": now, "until": until, "sha256": r["sha256"]})
    HP._sync([_paths()[0]], f"claim: {slug} ({platform}) by {by}")
    return {**r, "state": "claimed", "attempt": aid, "by": by, "until": until,
            "caption": HP.captions(r["meta"])[platform]}


def reconcile_hint(r: dict) -> str:
    return (f"RECONCILE FIRST: attempt {r['attempt']} ({r['by']}) on {r['slug']} → {r['platform']} has no outcome and its "
            f"lease ended {_iso(r['until'])} — the upload may have happened. Open the account: live → `clipengine "
            f"posted --slug {r['slug']} --platform {r['platform']} --url …`; not there → `clipengine attempt --slug "
            f"{r['slug']} --platform {r['platform']} --failed \"why\"`")


def close(slug: str, platform: str, outcome: str, why: str | None = None, url: str | None = None,
          now: float | None = None) -> str | None:
    """End the open attempt (claimed or uncertain): live / failed / released. None when there was none."""
    with _locked():
        now = time.time() if now is None else now
        st = state(slug, platform, now)
        if outcome != "live" and st["state"] == "live":
            raise SystemExit(f"{slug} is already live on {platform} — nothing to fail")
        last = next((e for e in reversed(events()) if e.get("slug") == slug and e.get("platform") == platform), None)
        if not last or last.get("ev") != "claim":
            if outcome == "live":
                return None                                # a post recorded without a claim: nothing open
            raise SystemExit(f"no open attempt on {slug} → {platform} (claim it first: `clipengine claim`)")
        ev = {"ev": outcome, "id": last["id"], "slug": slug, "platform": platform, "at": _iso(now), "ts": now,
              "by": poster_name()}
        if why:
            ev["why"] = why.strip()[:300]
        if url:
            ev["url"] = url
        _append(ev)
    if outcome != "live":                                  # posted() commits the ledger and this file together
        HP._sync([_paths()[0]], f"attempt {outcome}: {slug} ({platform})")
    return last["id"]


def show(now: float | None = None) -> list[dict]:
    rows = inventory(now)
    if not rows:
        print("bank: empty — nothing finished is waiting to post")
        return rows
    order = {"uncertain": 0, "claimed": 1, "ready": 2, "waiting": 3, "live": 4}
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    print("bank: " + ", ".join(f"{counts[k]} {k}" for k in sorted(counts, key=order.get)))
    for r in sorted(rows, key=lambda r: (order[r["state"]], r["slug"], r["platform"])):
        extra = {"uncertain": f"attempt {r['attempt']} by {r['by']}, lease ended {_iso(r['until'] or 0)} — reconcile",
                 "claimed": f"{r['by']} until {_iso(r['until'] or 0)}",
                 "waiting": f"at {r['scheduled']}",
                 "ready": f"`clipengine claim --slug {r['slug']} --platform {r['platform']}`"
                 + (f" · {r['failures']} failed before: {r['last_failure']}" if r["failures"] else ""),
                 "live": ""}[r["state"]]
        print(f"  {r['state']:<9} {r['slug']} → {r['platform']}  {extra}".rstrip())
    return rows


def uncertain(now: float | None = None) -> list[dict]:
    return [r for r in inventory(now) if r["state"] == "uncertain"]
