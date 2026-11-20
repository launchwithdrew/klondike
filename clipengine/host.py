"""What this machine can really do — the limits it runs under, not what the hardware underneath says.

In a container (a VM slice, a cloud workspace, Docker) `os.cpu_count()` and /proc/meminfo describe the HOST: a
1.5-CPU / 6 GB container on a 16-core host looks like 16 cores and 60 GB. Sizing on that over-commits — too many
scouts, Whisper on 16 threads fighting for 1.5 CPUs, heavy jobs admitted until the container's OOM killer ends a
scan. This module reads the effective limits:

  CPUs     the CPU affinity mask, and the cgroup CPU quota (v2 cpu.max, v1 cfs_quota/period) — the smaller one
  memory   the cgroup memory limit (v2 memory.max, v1 limit_in_bytes) minus what the cgroup uses (less reclaimable
           page cache), or the host's MemAvailable — the smaller one
  busy     how much of the effective CPUs this cgroup used since the last look (cpu.stat), not the host's load
  encoder  a short timed x264 encode at the profile's preset (realtime factor) and which hardware H.264
           encoders actually work here (listed is not enough: each is tried)

`clipengine bench` (bench.py) builds on it: bounded stage timings and a prediction against the speed targets.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time

CG = "/sys/fs/cgroup"
HW_ENCODERS = ("h264_videotoolbox", "h264_nvenc", "h264_qsv", "h264_vaapi", "h264_v4l2m2m", "h264_amf")


def _read(path: str) -> str | None:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def _v2_dirs(root: str = CG, proc_cgroup: str | None = None) -> list[str]:
    """This process's cgroup v2 directory and its ancestors (limits nest: the tightest one wins)."""
    text = proc_cgroup if proc_cgroup is not None else (_read("/proc/self/cgroup") or "")
    rel = next((l.split("::", 1)[1] for l in text.splitlines() if l.startswith("0::")), None)
    if rel is None or not os.path.exists(os.path.join(root, "cgroup.controllers")):
        return []
    parts = [p for p in rel.strip("/").split("/") if p]
    dirs = [os.path.join(root, *parts[:i]) for i in range(len(parts), -1, -1)]
    return [d for d in dirs if os.path.isdir(d)]


def _v1(root: str, ctrl: str, name: str) -> str | None:
    for c in (ctrl, "cpu,cpuacct", "cpuacct,cpu") if ctrl == "cpu" else (ctrl,):
        v = _read(os.path.join(root, c, name))
        if v is not None:
            return v
    return None


def cpu_quota(root: str = CG, proc_cgroup: str | None = None) -> float | None:
    """CPUs the cgroup may use (e.g. 1.5), or None when unlimited / unknown."""
    best = None
    for d in _v2_dirs(root, proc_cgroup):
        v = _read(os.path.join(d, "cpu.max"))
        if v and not v.startswith("max"):
            q, p = v.split()[:2]
            n = int(q) / int(p)
            best = n if best is None else min(best, n)
    if best is None:
        q, p = _v1(root, "cpu", "cpu.cfs_quota_us"), _v1(root, "cpu", "cpu.cfs_period_us")
        if q and p and int(q) > 0 and int(p) > 0:
            best = int(q) / int(p)
    return best


def cpus(root: str = CG, proc_cgroup: str | None = None) -> tuple[float, str]:
    """(effective CPUs, where that number comes from)."""
    host = os.cpu_count() or 1
    try:
        aff = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        aff = host
    q = cpu_quota(root, proc_cgroup)
    if q is not None and q < aff:
        return round(q, 2), f"cgroup quota (host has {host})"
    if aff < host:
        return float(aff), f"CPU affinity (host has {host})"
    return float(host), "all of the host"


def threads(root: str = CG) -> int:
    """Worker threads to give a CPU-bound job (Whisper, x264): the effective CPUs, at least 1."""
    return max(1, int(round(cpus(root)[0])))


def _host_mem_available_gb() -> float | None:
    v = _read("/proc/meminfo")
    if v:
        m = re.search(r"MemAvailable:\s+(\d+)", v)
        if m:
            return int(m.group(1)) / 1e6
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
            page = int(re.search(r"page size of (\d+)", out).group(1))
            pages = sum(int(re.search(rf"{k}:\s+(\d+)", out).group(1))
                        for k in ("Pages free", "Pages inactive", "Pages speculative"))
            return pages * page / 1e9
        except Exception:
            return None
    return None


def _stat(text: str | None, key: str) -> int:
    m = re.search(rf"^{key}\s+(\d+)", text or "", re.M)
    return int(m.group(1)) if m else 0


def mem_limit(root: str = CG, proc_cgroup: str | None = None) -> tuple[float, float] | None:
    """(limit GB, used GB excluding reclaimable cache) of the tightest memory cgroup, or None."""
    best = None
    for d in _v2_dirs(root, proc_cgroup):
        lim = _read(os.path.join(d, "memory.max"))
        if lim and lim != "max":
            cur = int(_read(os.path.join(d, "memory.current")) or 0)
            used = max(0, cur - _stat(_read(os.path.join(d, "memory.stat")), "inactive_file"))
            if best is None or int(lim) - used < (best[0] - best[1]) * 1e9:
                best = (int(lim) / 1e9, used / 1e9)
    if best is None:
        lim = _v1(root, "memory", "memory.limit_in_bytes")
        if lim and int(lim) < 1 << 60:
            cur = int(_v1(root, "memory", "memory.usage_in_bytes") or 0)
            used = max(0, cur - _stat(_v1(root, "memory", "memory.stat"), "total_inactive_file"))
            best = (int(lim) / 1e9, used / 1e9)
    return best


def mem_available_gb(root: str = CG, proc_cgroup: str | None = None, host: float | None = None) -> float | None:
    """Memory free for new work: the container's headroom when it has a limit, never more than the host's."""
    host = _host_mem_available_gb() if host is None else host
    lim = mem_limit(root, proc_cgroup)
    if lim:
        room = max(0.0, lim[0] - lim[1])
        return round(room if host is None else min(host, room), 2)
    return None if host is None else round(host, 2)


def mem_total_gb(root: str = CG) -> float:
    lim = mem_limit(root)
    try:
        phys = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e9
    except (ValueError, OSError, AttributeError):
        phys = 0.0
    return round(min(phys, lim[0]) if lim and phys else (lim[0] if lim else phys), 1)


def cpu_used_s(root: str = CG, proc_cgroup: str | None = None) -> float | None:
    """CPU seconds this cgroup has used (for a busy fraction over an interval)."""
    for d in _v2_dirs(root, proc_cgroup)[:1]:
        txt = _read(os.path.join(d, "cpu.stat"))
        if txt and re.search(r"^usage_usec\s", txt, re.M):
            return _stat(txt, "usage_usec") / 1e6
    v = _v1(root, "cpuacct", "cpuacct.usage") or _v1(root, "cpu", "cpuacct.usage")
    return int(v) / 1e9 if v and v.isdigit() else None


def busy(state: dict, now: float | None = None, root: str = CG) -> float | None:
    """Share of the effective CPUs in use since the last call (0–1+). Keeps its sample in `state`. Without
    cgroup accounting: the load average per effective CPU."""
    now = time.time() if now is None else now
    used, n = cpu_used_s(root), cpus(root)[0]
    prev = state.get("cpu_sample")
    state["cpu_sample"] = [now, used] if used is not None else None
    if used is not None and prev and prev[1] is not None and now - prev[0] >= 5:
        return round((used - prev[1]) / ((now - prev[0]) * n), 2)
    try:
        return round(os.getloadavg()[0] / n, 2)
    except OSError:
        return None


# ---- the encoder -----------------------------------------------------------------------------------------

def _encode(codec_args: list[str], seconds: float, size: str = "1080x1920", timeout: float = 60) -> float | None:
    """Realtime factor of encoding `seconds` of a synthetic vertical clip (2.0 = twice as fast as playback)."""
    with tempfile.TemporaryDirectory(prefix="klondike-enc-") as tmp:
        out = os.path.join(tmp, "o.mp4")
        cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30:duration={seconds}",
               *codec_args, "-an", out]
        t0 = time.monotonic()
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            return None
        dt = time.monotonic() - t0
        if p.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) < 1000:
            return None
        return round(seconds / max(dt, 1e-3), 2)


def hardware_encoders() -> dict[str, bool]:
    """Each hardware H.264 encoder ffmpeg lists, and whether a 1 s encode with it actually works here."""
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    found = {}
    for enc in HW_ENCODERS:
        if re.search(rf"\s{enc}\s", out):
            args = ["-vaapi_device", "/dev/dri/renderD128", "-vf", "format=nv12,hwupload", "-c:v", enc] \
                if enc == "h264_vaapi" else ["-c:v", enc]
            found[enc] = _encode(args, 1.0, timeout=20) is not None
    return found


def encoder_probe(preset: str, seconds: float = 3.0, cache: bool = True) -> dict:
    """libx264 at this preset: realtime factor; plus which hardware encoders work. Cached a day per host/preset."""
    from . import __version__
    from .admission import _dir
    path = os.path.join(_dir(), "host.json")
    key = f"{socket.gethostname()}:{preset}:{__version__}:{cpus()[0]}"
    try:
        memo = json.load(open(path))
    except (OSError, ValueError):
        memo = {}
    hit = memo.get(key)
    if cache and hit and time.time() - hit.get("at", 0) < 86400:
        return hit
    rt = _encode(["-c:v", "libx264", "-preset", preset, "-threads", str(threads()), "-pix_fmt", "yuv420p"], seconds)
    res = {"preset": preset, "x264_realtime": rt, "hardware": hardware_encoders(), "at": time.time()}
    memo = {k: v for k, v in memo.items() if time.time() - v.get("at", 0) < 7 * 86400}
    memo[key] = res
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump(memo, fh, indent=1)
    except OSError:
        pass
    return res


def summary(root: str = CG) -> dict:
    n, src = cpus(root)
    lim = mem_limit(root)
    return {"cpus": n, "cpus_from": src, "host_cpus": os.cpu_count() or 1, "mem_total_gb": mem_total_gb(root),
            "mem_limited": bool(lim), "mem_available_gb": mem_available_gb(root), "host": socket.gethostname()}
