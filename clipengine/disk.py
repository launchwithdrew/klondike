"""Disk space as the OS will actually enforce it: the filesystem's free space AND the user's quota.

A user quota (EDQUOT) can stop every write while the filesystem still reports plenty free — 3 freezes on 23 Nov
at ~100 GB used with 49 GB "free". Every free-space decision (recording buffer size, the cycle's prune, doctor)
reads `free_gb()` here: the smaller of the two.

The quota comes from, in order:
  1. `"disk_quota_gb"` in config/ops.json (what this install may use; measured against this folder's size)
  2. the OS `quota` tool, when it reports a limit for the filesystem this folder is on
  3. learned: the folder's size the last time a write failed with EDQUOT / ENOSPC (runs/.disk_quota.json,
     forgotten after 7 days without another hit, in case the quota was raised)
"""
from __future__ import annotations

import errno
import json
import os
import re
import shutil
import subprocess
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS = os.path.join(REPO, "runs")
LEARNED = os.path.join(RUNS, ".disk_quota.json")
LEARN_DAYS = 7
SIZE_TTL_S = 120.0
KEEP_UNDER = 0.80          # recording buffers are sized to keep this install under 80% of its quota
WARN_AT = 0.85             # one attention line a day at 85%
FULL = {errno.ENOSPC, getattr(errno, "EDQUOT", 122)}

_SIZE: dict[str, tuple[float, float]] = {}


def tree_gb(path: str = REPO, fresh: bool = False) -> float:
    """Bytes under `path` (files only, symlinks not followed), cached for two minutes."""
    hit = _SIZE.get(path)
    if hit and not fresh and time.time() - hit[0] < SIZE_TTL_S:
        return hit[1]
    total = 0
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            st = e.stat(follow_symlinks=False)
                            total += st.st_blocks * 512 or st.st_size
                    except OSError:
                        continue
        except OSError:
            continue
    gb = total / 1e9
    _SIZE[path] = (time.time(), gb)
    return gb


def freed(gb: float, path: str = REPO) -> None:
    """Tell the size cache that `gb` was just deleted under `path` (so the next decision doesn't wait for a
    re-walk): every cached folder that contains it shrinks too (the repo, and home when the quota counts it)."""
    p = os.path.abspath(path)
    for k, hit in list(_SIZE.items()):
        if p == k or p.startswith(k.rstrip(os.sep) + os.sep):
            _SIZE[k] = (hit[0], max(0.0, hit[1] - gb))


def parse_quota(text: str, device: str | None = None) -> dict | None:
    """`quota -w` output → {"limit_gb", "used_gb"} for `device` (or the row with the least headroom).
    Blocks are 1 KiB; the hard limit is what EDQUOT enforces (the soft one when no hard limit is set)."""
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0].startswith("/") and re.fullmatch(r"\d+\*?", parts[1]):
            used, soft, hard = int(parts[1].rstrip("*")), int(parts[2]), int(parts[3])
            lim = hard or soft
            if lim:
                rows.append({"device": parts[0], "used_gb": used * 1024 / 1e9, "limit_gb": lim * 1024 / 1e9})
    if not rows:
        return None
    mine = [r for r in rows if device and r["device"] == device]
    return min(mine or rows, key=lambda r: r["limit_gb"] - r["used_gb"])


def _os_quota() -> dict | None:
    if not shutil.which("quota"):
        return None
    try:
        out = subprocess.run(["quota", "-w"], capture_output=True, text=True, timeout=10).stdout
        dev = subprocess.run(["df", "--output=source", REPO], capture_output=True, text=True, timeout=10).stdout.split()
        q = parse_quota(out, dev[-1] if len(dev) > 1 else None)
        return {**q, "source": "quota tool"} if q else None
    except (OSError, subprocess.SubprocessError):
        return None


def _learned(now: float | None = None) -> dict | None:
    try:
        d = json.load(open(LEARNED))
        if (now or time.time()) - float(d["at"]) <= LEARN_DAYS * 86400 and float(d["limit_gb"]) > 0:
            return d
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def counted(cfg: dict | None = None) -> str:
    """The folder whose size the quota counts: "disk_quota_counts" in config/ops.json (e.g. "~" when the quota is
    the whole home folder, 24 Nov: home 96 GB vs this repo 75 GB), else this install."""
    v = (cfg or {}).get("disk_quota_counts")
    p = os.path.abspath(os.path.expanduser(str(v))) if v else REPO
    return p if os.path.isdir(p) else REPO


def _cfg(cfg: dict | None) -> dict:
    if cfg is not None:
        return cfg
    try:
        from . import ops as O
        return O.config()
    except Exception:
        return {}


def quota(cfg: dict | None = None) -> dict | None:
    """{"limit_gb", "used_gb", "source"} or None when no quota is known."""
    cfg = _cfg(cfg)
    where = counted(cfg)
    v = cfg.get("disk_quota_gb")
    if isinstance(v, (int, float)) and v > 0:
        return {"limit_gb": float(v), "used_gb": tree_gb(where), "source": "config disk_quota_gb"
                + ("" if where == REPO else f", counting {where}")}
    q = _os_quota()
    if q:
        return q
    d = _learned()
    if d:
        return {"limit_gb": float(d["limit_gb"]), "used_gb": tree_gb(where), "source": "learned from a full-disk error"}
    return None


def status(cfg: dict | None = None) -> dict:
    fs = shutil.disk_usage(REPO).free / 1e9
    q = quota(cfg)
    out = {"fs_free_gb": fs, "free_gb": fs, "quota": q}
    if q:
        out["free_gb"] = max(0.0, min(fs, q["limit_gb"] - q["used_gb"]))
        out["used_pct"] = q["used_gb"] / q["limit_gb"]
    return out


def free_gb(cfg: dict | None = None) -> float:
    return status(cfg)["free_gb"]


def sizing_free_gb(cfg: dict) -> float:
    """What the buffer sizing may plan with: free space, and under a quota only up to KEEP_UNDER of it
    (buffer_hours subtracts min_free_gb, so this adds it back on the quota side)."""
    st = status(cfg)
    q = st["quota"]
    if not q:
        return st["free_gb"]
    return min(st["fs_free_gb"], KEEP_UNDER * q["limit_gb"] - q["used_gb"] + float(cfg.get("min_free_gb", 15)))


def learn_full(exc: OSError, now: float | None = None) -> bool:
    """A write failed: if it was a full disk / quota, remember this folder's size as the limit. True = it was."""
    if getattr(exc, "errno", None) not in FULL:
        return False
    try:
        os.makedirs(RUNS, exist_ok=True)
        size = tree_gb(counted(_cfg(None)), fresh=True)
        tmp = LEARNED + ".tmp"
        json.dump({"limit_gb": round(size, 2), "at": now or time.time(), "errno": exc.errno,
                   "why": os.strerror(exc.errno)}, open(tmp, "w"))
        os.replace(tmp, LEARNED)
    except OSError:
        pass                                   # the disk is full: remembering it may fail too
    return True


def line(cfg: dict | None = None) -> str:
    """doctor / report: "48 GB free (quota 100 GB, 52% used, config disk_quota_gb)"."""
    st = status(cfg)
    q = st["quota"]
    if not q:
        return f"{st['free_gb']:.0f} GB free (no user quota found)"
    return (f"{st['free_gb']:.0f} GB free (quota {q['limit_gb']:.0f} GB, {st['used_pct']:.0%} used, {q['source']}; "
            f"filesystem {st['fs_free_gb']:.0f} GB)")


def recordings(root: str = RUNS) -> list[str]:
    return [os.path.dirname(p) for p in
            __import__("glob").glob(os.path.join(root, "*", "segments"))]


def prune_oldest(need_gb: float, root: str = RUNS) -> tuple[int, float]:
    """Free `need_gb` by deleting the oldest recorded segments across every recording (running ones too),
    never a stretch a queued draft still needs. Returns (files, GB) removed."""
    from . import autopilot as AP
    segs = []
    for rec in recordings(root):
        keep = AP.protected_windows(rec)
        durs = AP.segment_durations(rec) if keep else {}
        for e in os.scandir(os.path.join(rec, "segments")):
            if not e.name.endswith(".ts"):
                continue
            try:
                t = int(e.name[:-3]) / 1000
            except ValueError:
                continue
            if keep and AP.is_protected(t, durs.get(e.name, 2.0), keep):
                continue
            segs.append((t, e.path))
    segs.sort()
    n, gone = 0, 0.0
    for _, p in segs:
        if gone >= need_gb:
            break
        try:
            st = os.stat(p)
            sz = (st.st_blocks * 512 or st.st_size) / 1e9
            os.remove(p)
        except OSError:
            continue
        n, gone = n + 1, gone + sz
    freed(gone)
    return n, gone
