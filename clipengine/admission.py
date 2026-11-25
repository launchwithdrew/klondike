"""Give live capture first claim on the machine: one admission decision for every heavy job.

Recording never goes through here — it is light and must never wait. Heavy work does: speech transcription
and video encodes, whichever route asked for them (a sentinel's draft, catch-up, the sweep, pre-renders,
publish, a manual scan). At most `heavy_slots` run at once (config/ops.json; the lite profile is 1, the full
profile 2). A live moment normally gets the next slot. A publish waiting three minutes goes ahead of live
work when a slot opens. Catch-up, sweep, pre-renders and historical scans follow. While free memory is below
`heavy_min_free_gb` nothing heavy starts, so a big job can't push the recorders into the out-of-memory killer.

Every wait and run is logged in runs/heavy/log.jsonl (kind, stage, seconds waiting, seconds running), so
the report can show queue time separately from compute time; `clipengine jobs` shows who runs and who waits
and why. Slots are OS file locks: a killed job frees its slot at once.
"""
from __future__ import annotations

import contextlib
import fcntl
import glob
import json
import os
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS = os.path.join(REPO, "runs")
PRIORITY = {"live": 0, "publish": 1, "catchup": 2, "sweep": 3, "prerender": 3, "scan": 4}
PUBLISH_WAIT_S = 180
POLL_S = 2.0
_local = threading.local()


def _dir(root: str | None = None) -> str:
    """runs/heavy (CLIPENGINE_ADMISSION_ROOT moves it: the test suite uses a temporary one, so a test run on a
    working machine never adds its renders to the real timings)."""
    return os.path.join(root or os.environ.get("CLIPENGINE_ADMISSION_ROOT") or RUNS, "heavy")


def kind_of(argv: list[str] | None = None) -> str:
    """Which route this process is (CLIPENGINE_JOB_KIND wins; else read from the command line)."""
    env = os.environ.get("CLIPENGINE_JOB_KIND")
    if env in PRIORITY:
        return env
    argv = sys.argv if argv is None else argv
    words = set(argv)
    if "sentinel" in words or "record" in words:
        return "live"
    if "catchup" in words:
        return "sweep" if "--sweep" in words else "catchup"
    if "prerender" in words:
        return "prerender"
    if {"scan", "watch"} & words:
        return "scan"
    return "publish"                                  # an operator command: someone is waiting on it


def settings() -> tuple[int, float]:
    try:
        from .ops import config
        cfg = config()
    except Exception:
        cfg = {}
    lite = cfg.get("profile") == "lite"
    return int(cfg.get("heavy_slots") or (1 if lite else 2)), float(cfg.get("heavy_min_free_gb", 0.8))


def free_gb() -> float | None:
    """Memory free for a heavy job: a container's headroom under its limit (host.py), not the host's."""
    try:
        from . import host as H
        v = H.mem_available_gb()
        if v is not None:
            return v
    except Exception:
        pass
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1e6
    except OSError:
        pass
    return None                                       # unknown (macOS): don't block on it


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def _order(waiter: dict, now: float) -> tuple:
    aged = waiter["kind"] == "publish" and now - waiter["since"] >= PUBLISH_WAIT_S
    return (-1 if aged else waiter["prio"], waiter["since"], waiter["file"])


def waiters(root: str | None = None, *, now: float | None = None) -> list[dict]:
    out = []
    for f in glob.glob(os.path.join(_dir(root), "wait", "*.json")):
        try:
            w = json.load(open(f))
        except (OSError, ValueError):
            continue
        if not _alive(w.get("pid")):
            try:
                os.remove(f)
            except OSError:
                pass
            continue
        out.append({**w, "file": f})
    now = time.time() if now is None else now
    return sorted(out, key=lambda w: _order(w, now))


def running(root: str | None = None) -> list[dict]:
    out = []
    for f in sorted(glob.glob(os.path.join(_dir(root), "slot-*.json"))):
        try:
            r = json.load(open(f))
        except (OSError, ValueError):
            continue
        if _alive(r.get("pid")) and r.get("held"):
            out.append(r)
    return out


def _log(rec: dict, root: str | None = None) -> None:
    try:
        with open(os.path.join(_dir(root), "log.jsonl"), "a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def note_media(stage: str, media_s: float, route: str | None = None, root: str | None = None) -> None:
    """How much media a heavy job processed (audio seconds transcribed, video seconds encoded) — the weekly cost
    metrics divide by it (metrics.py)."""
    try:
        os.makedirs(_dir(root), exist_ok=True)
        with open(os.path.join(_dir(root), "work.jsonl"), "a") as fh:
            fh.write(json.dumps({"at": time.time(), "stage": stage, "media_s": round(float(media_s), 1),
                                 "route": route}) + "\n")
    except OSError:
        pass


def draft_key(draft: dict | None) -> str | None:
    """Queue IDs reuse the same UTC clock time on different days."""
    if not draft or not draft.get("id"):
        return None
    from .moments import timestamp
    at = timestamp(draft.get("at"))
    if at is None:
        at = timestamp(draft.get("start"))
    return f"{draft['id']}@{int(at // 86400)}" if at is not None else None


@contextlib.contextmanager
def track_draft(draft: dict | None):
    previous = getattr(_local, "draft_key", None)
    _local.draft_key = draft_key(draft) or previous
    try:
        yield
    finally:
        _local.draft_key = previous


@contextlib.contextmanager
def slot(stage: str, kind: str | None = None, root: str | None = None, slots: int | None = None,
         min_free_gb: float | None = None, poll_s: float = POLL_S, free=free_gb, say=print):
    """Hold one heavy-job slot for the duration (re-entrant within a process)."""
    if os.environ.get("CLIPENGINE_NO_ADMISSION") == "1" or getattr(_local, "depth", 0):
        _local.depth = getattr(_local, "depth", 0) + 1
        try:
            yield
        finally:
            _local.depth -= 1
        return
    kind = kind or kind_of()
    n_default, mem_default = settings()
    n = max(1, slots or n_default)
    floor = mem_default if min_free_gb is None else min_free_gb
    d = _dir(root)
    os.makedirs(os.path.join(d, "wait"), exist_ok=True)
    since = time.time()
    me = os.path.join(d, "wait", f"{PRIORITY.get(kind, 4)}-{time.time_ns()}-{os.getpid()}.json")
    waiter = {"kind": kind, "stage": stage, "prio": PRIORITY.get(kind, 4), "pid": os.getpid(),
              "since": since, "file": me}
    told = None
    reasons: dict[str, dict] = {}
    fd, idx = None, None
    try:
        while True:
            reason = None
            now = time.time()
            ahead = [w for w in waiters(root, now=now) if _order(w, now) < _order(waiter, now)]
            mem = free()
            holders = []
            if ahead:
                reason = f"waiting behind {ahead[0]['kind']} ({ahead[0]['stage']})"
                wait_cause = "priority"
                holders = [ahead[0]]
            elif mem is not None and mem < floor:
                reason = f"waiting: only {mem:.1f} GB memory free (recording comes first)"
                wait_cause = "memory"
            else:
                for i in range(n):
                    f = os.open(os.path.join(d, f"slot-{i}.lock"), os.O_CREAT | os.O_RDWR, 0o644)
                    try:
                        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        fd, idx = f, i
                        break
                    except BlockingIOError:
                        os.close(f)
                if fd is not None:
                    # The check above and the lock are two steps: a waiter stalled between them (a loaded host)
                    # could take a slot a waiter that aged in the meantime is owed. Look again holding it.
                    now = time.time()
                    if not any(_order(w, now) < _order(waiter, now) for w in waiters(root, now=now)):
                        break
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)
                    fd, idx = None, None
                    continue
                wait_cause = "capacity"
                holders = running(root)
                reason = "waiting: all heavy slots busy" + (f" ({', '.join(r['kind'] + ' ' + r['stage'] for r in holders)})"
                                                            if holders else "")
            trace = reasons.setdefault(wait_cause, {"first_at": now, "polls": 0})
            trace.update(last_at=now, polls=trace["polls"] + 1,
                         blockers=[{k: w.get(k) for k in ("pid", "kind", "stage", "since")} for w in holders])
            if wait_cause == "memory":
                trace["last_free_gb"] = mem
            with open(me + ".tmp", "w") as fh:
                json.dump({**waiter, "reason": reason}, fh)
            os.replace(me + ".tmp", me)
            if reason != told and kind in ("publish", "scan") and time.time() - since > 1:
                say(f"[{stage}] {reason}")
                told = reason
            time.sleep(poll_s)
    finally:
        for path in (me, me + ".tmp"):
            try:
                os.remove(path)
            except OSError:
                pass
    waited = time.time() - since
    info = os.path.join(d, f"slot-{idx}.json")
    json.dump({"kind": kind, "stage": stage, "pid": os.getpid(), "since": time.time(), "held": True}, open(info, "w"))
    _local.depth = 1
    t0 = time.time()
    ok = False
    try:
        yield
        ok = True
    finally:
        _local.depth = 0
        try:
            json.dump({"held": False}, open(info, "w"))
        except OSError:
            pass
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        _log({"at": t0, "kind": kind, "stage": stage, "waited_s": round(waited, 1), "ready_at": since,
              "slot": idx, "pid": os.getpid(), "draft_key": getattr(_local, "draft_key", None), "aged_publish": kind == "publish" and t0 - since >= PUBLISH_WAIT_S,
              "wait_causes": reasons, "finished_at": time.time(),
              "ran_s": round(time.time() - t0, 1), "ok": ok}, root)


def stats(root: str | None = None, since: float = 0.0) -> dict:
    """p50/p90 waiting and running seconds per stage (for the report)."""
    rows: dict[str, dict[str, list[float]]] = {}
    try:
        for line in open(os.path.join(_dir(root), "log.jsonl")):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("at", 0) >= since:
                s = rows.setdefault(r["stage"], {"wait": [], "run": []})
                s["wait"].append(r["waited_s"]); s["run"].append(r["ran_s"])
    except OSError:
        pass

    def pct(v: list[float], q: float) -> float:
        v = sorted(v)
        return round(v[min(len(v) - 1, int(q * len(v)))], 1) if v else 0.0
    return {k: {"n": len(v["run"]), "wait_p50": pct(v["wait"], .5), "wait_p90": pct(v["wait"], .9),
                "run_p50": pct(v["run"], .5), "run_p90": pct(v["run"], .9)} for k, v in rows.items()}


def show(root: str | None = None) -> list[str]:
    n, floor = settings()
    lines = [f"heavy jobs: {n} slot(s) · nothing heavy starts under {floor:g} GB free memory"]
    for r in running(root):
        lines.append(f"  running  {r['kind']:<9} {r['stage']:<12} {time.time() - r['since']:.0f} s")
    for w in waiters(root):
        lines.append(f"  waiting  {w['kind']:<9} {w['stage']:<12} {time.time() - w['since']:.0f} s — {w.get('reason') or ''}")
    for stage, s in sorted(stats(root, time.time() - 86400).items()):
        lines.append(f"  24 h {stage:<12} n={s['n']} wait p50 {s['wait_p50']} s / p90 {s['wait_p90']} s · "
                     f"run p50 {s['run_p50']} s / p90 {s['run_p90']} s")
    pl = probe_line(root)
    if pl:
        lines.append(pl)
    lines += batch_lines(root)
    return lines


def batch_lines(root: str | None = None, n: int = 3) -> list[str]:
    """The last catch-up / sweep batches: window, what they queued, and any source they could NOT cover."""
    from . import autopilot as AP
    out = []
    for b in AP.batches(root, n):
        when = time.strftime("%m-%d %H:%M", time.localtime(b["since"])) + "→" + time.strftime("%H:%M", time.localtime(b["until"]))
        fail = b.get("failed") or []
        tail = (f" · NOT covered: {', '.join(sorted({f['login'] for f in fail}))} (watermark held at "
                f"{time.strftime('%H:%M', time.localtime(b['covered_until']))})") if fail else " · fully covered"
        out.append(f"  batch {b['kind']:<7} {when}: {b['queued']} queued from {b['windows']} window(s){tail}")
    return out


def probe_line(root: str | None = None, since_s: float = 86400) -> str | None:
    """What the early mute probe saved in the last day: windows stopped before Whisper, by route."""
    cut, rows = time.time() - since_s, []
    try:
        with open(os.path.join(_dir(root), "probes.jsonl")) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("at", 0) >= cut:
                    rows.append(r)
    except OSError:
        return None
    if not rows:
        return None
    skipped = [r for r in rows if r.get("skipped_asr")]
    by: dict[str, int] = {}
    for r in skipped:
        by[r.get("route", "vod")] = by.get(r.get("route", "vod"), 0) + 1
    return (f"  24 h early probe: {len(skipped)} of {len(rows)} windows stopped before transcription "
            f"({sum(r.get('asr_s_saved', 0) for r in skipped):.0f} s of audio"
            + (f"; {sum(1 for r in skipped if r.get('cached'))} from the cache" if any(r.get("cached") for r in skipped) else "")
            + ")" + (" · " + ", ".join(f"{k} {v}" for k, v in sorted(by.items())) if by else ""))
