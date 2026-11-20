"""Keep the capture loop alive when the agent, a worker or the host goes away — and say exactly what happened.

  lease       one capture leader per installation: an OS file lock (runs/autopilot.lock) held for the leader's
              whole life. The kernel drops it when the process dies — SIGKILL, out-of-memory, crash — so a stale
              pid can never look alive. runs/autopilot.lease.json carries the generation, pid, host boot and
              the heartbeat. A service, a manual start and a sentinel's revival all converge on one leader: the
              service waits as standby, the others step aside.
  heartbeat   a thread that writes the lease every 30 s (a long cycle can't block it), notices host sleep
              (wall clock jumped further than the monotonic clock), exits the loop when the cycle phase makes no
              progress for too long (so the service restarts it), and optionally pings an off-host URL
              (config/ops.json "heartbeat_url": a dead-man's-switch service that alerts when pings stop — the
              only thing that can tell you a sleeping or dead host is down).
  incidents   runs/incidents.jsonl: one record per dark period of the leader — when it went dark, the last
              progress, the exit evidence, a cause class (operator stop is not an incident; reboot, signal,
              crash, stalled, suspension, unknown) and when and how it recovered. The attention line shows an
              open incident once and its recovery once. Three deaths inside an hour is a crash loop.
  service     `clipengine supervisor install|status|stop|resume|uninstall`: a native service that starts the
              loop at login (macOS launchd LaunchAgent) or with the user's systemd, and restarts it whenever it
              exits without being stopped on purpose. Where neither exists (a container, an ephemeral host) the
              loop runs detached and status says so: deaths are then only noticed by a sentinel or the next
              command, and reconciled when it is back.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS = os.path.join(REPO, "runs")

BEAT_S = 30.0                    # heartbeat period
MISSED_BEATS = 3                 # a leader whose heartbeat is older than this many beats is gone
SUSPEND_JUMP_S = 60.0            # wall clock ahead of the monotonic clock by this much = the host slept
STALL_S = 40 * 60                # a cycle phase with no progress this long: exit so the service restarts us
PING_EVERY_S = 60.0              # off-host heartbeat pings
CRASH_LOOP_N = 3                 # this many incidents within an hour is a crash loop
EXIT_STOPPED = 0                 # exit codes the service sees: 0 = stopped on purpose (no restart)
EXIT_RESTART = 75                # anything else = restart me


def _p(name: str, root: str | None = None) -> str:
    return os.path.join(root or RUNS, name)


def _read(path: str) -> dict:
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return {}


def _write(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, path)


# ---- host evidence --------------------------------------------------------------------------------

def boot_time() -> float | None:
    """When this host last booted (epoch seconds), or None when the OS doesn't say."""
    try:
        for line in open("/proc/stat"):
            if line.startswith("btime "):
                return float(line.split()[1])
    except OSError:
        pass
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=5).stdout
            m = re.search(r"sec\s*=\s*(\d+)", out)
            return float(m.group(1)) if m else None
        except (OSError, subprocess.SubprocessError):
            return None
    return None


def boot_id() -> str:
    try:
        return open("/proc/sys/kernel/random/boot_id").read().strip()
    except OSError:
        b = boot_time()
        return f"boot@{int(b)}" if b else ""


# ---- the lease -------------------------------------------------------------------------------------

class Lease:
    """The capture leader's exclusive lease. acquire() → True when this process is now the leader."""

    def __init__(self, root: str | None = None):
        self.root = root or RUNS
        self.lock_path = _p("autopilot.lock", self.root)
        self.path = _p("autopilot.lease.json", self.root)
        self.fd: int | None = None
        self.generation = 0

    def acquire(self, wait_s: float = 0.0, poll_s: float = 5.0, should_stop=lambda: False) -> bool:
        os.makedirs(self.root, exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        deadline = time.time() + wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() >= deadline or should_stop():
                    os.close(fd)
                    return False
                time.sleep(poll_s)
        self.fd = fd
        prev = _read(self.path)
        self.generation = int(prev.get("generation") or 0) + 1
        now = time.time()
        _write(_p("autopilot.prev-lease.json", self.root), prev) if prev else None
        _write(self.path, {"generation": self.generation, "pid": os.getpid(), "host": socket.gethostname(),
                           "boot_id": boot_id(), "started_at": now, "heartbeat_at": now, "progress_at": now,
                           "phase": "starting", "started_by": os.environ.get("CLIPENGINE_STARTED_BY", "manual")})
        return True

    def update(self, **kw) -> None:
        st = _read(self.path)
        if st.get("generation") != self.generation:
            return                                          # someone else's lease now: never overwrite it
        st.update(kw)
        _write(self.path, st)

    def release(self) -> None:
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None


def holder(root: str | None = None) -> dict | None:
    """The live leader's lease, or None when nobody holds the lock (whatever any pid file says)."""
    path = _p("autopilot.lock", root)
    if not os.path.exists(path):
        return None
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    except BlockingIOError:
        return _read(_p("autopilot.lease.json", root)) or {"pid": None}
    finally:
        os.close(fd)


def ever_started(root: str | None = None) -> bool:
    return os.path.exists(_p("autopilot.lease.json", root))


# ---- intentional stop --------------------------------------------------------------------------------

def stopped(root: str | None = None) -> dict | None:
    """The operator's stop (it survives reboots: the service sees it and stays down)."""
    return _read(_p("autopilot.stopped", root)) or None


def mark_stopped(by: str = "operator", root: str | None = None, now: float | None = None) -> None:
    _write(_p("autopilot.stopped", root), {"at": now or time.time(), "by": by})


def clear_stopped(root: str | None = None) -> None:
    try:
        os.remove(_p("autopilot.stopped", root))
    except OSError:
        pass


# ---- exit evidence and incidents ----------------------------------------------------------------------

def record_exit(generation: int, reason: str, code: int, root: str | None = None) -> None:
    _write(_p("autopilot.exit.json", root), {"generation": generation, "reason": reason, "code": code,
                                             "at": time.time(), "pid": os.getpid()})


def incidents(root: str | None = None, since: float = 0.0) -> list[dict]:
    """Each incident's latest state (the file is append-only: an open record, then its recovery)."""
    by: dict[str, dict] = {}
    try:
        for line in open(_p("incidents.jsonl", root)):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            by[r["id"]] = {**by.get(r["id"], {}), **r}
    except OSError:
        pass
    return sorted((r for r in by.values() if r.get("dark_from", 0) >= since or r.get("status") == "open"),
                  key=lambda r: r.get("dark_from", 0))


def _append_incident(rec: dict, root: str | None = None) -> None:
    os.makedirs(root or RUNS, exist_ok=True)
    with open(_p("incidents.jsonl", root), "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.write(json.dumps(rec) + "\n")


def classify(prev: dict, exit_rec: dict, now: float, host_boot: float | None, this_boot: str) -> tuple[str, str] | None:
    """(cause, evidence) for the previous leader's end — None when it ended on purpose (stop / self-update)."""
    if exit_rec and exit_rec.get("generation") == prev.get("generation"):
        reason = str(exit_rec.get("reason") or "")
        if reason in ("stop", "update"):
            return None
        kind = reason.split(":", 1)[0]
        if kind in ("signal", "crash", "stalled"):
            return kind, reason
        return "unknown", reason
    last = float(prev.get("heartbeat_at") or prev.get("started_at") or now)
    if (prev.get("boot_id") and this_boot and prev["boot_id"] != this_boot) or (host_boot and host_boot > last):
        return "reboot", "the host restarted after the last heartbeat"
    return "unknown", ("no exit record: killed without warning (SIGKILL, the out-of-memory killer) or the host "
                       "lost the process — historical cause unknown")


def reconcile(recovered_by: str | None, root: str | None = None, now: float | None = None,
              prev: dict | None = None) -> dict | None:
    """Account for the previous leader. With recovered_by (a new leader just took the lease) the incident is
    recorded recovered; without (a status check found nobody leading) it is recorded open, once."""
    from . import attention as AT
    now = time.time() if now is None else now
    prev = prev if prev is not None else _read(_p("autopilot.prev-lease.json" if recovered_by else
                                                  "autopilot.lease.json", root))
    if not prev or not prev.get("generation"):
        return None
    gen = int(prev["generation"])
    stop = stopped(root)
    last = float(prev.get("heartbeat_at") or prev.get("started_at") or now)
    if stop and float(stop.get("at", 0)) >= float(prev.get("started_at", 0)):
        return None                                         # the operator stopped it: not an incident
    known = {r["id"]: r for r in incidents(root)}
    iid = f"ap-{prev.get('boot_id', '')[:8]}-{gen}"
    cls = classify(prev, _read(_p("autopilot.exit.json", root)), now, boot_time(), boot_id())
    if cls is None:
        return None
    if not recovered_by and now - last < BEAT_S * MISSED_BEATS:
        return None                                         # not missed long enough yet
    cause, evidence = cls
    dark_min = round((now - last) / 60, 1)
    rec = known.get(iid)
    if rec is None:
        rec = {"id": iid, "component": "autopilot", "generation": gen, "dark_from": last,
               "last_progress": prev.get("progress_at"), "last_phase": prev.get("phase"),
               "detected_at": now, "cause": cause, "evidence": evidence[:300], "status": "open",
               "recovery_attempts": 0}
        _append_incident(rec, root)
        if not recovered_by:
            AT.event("failure", f"autopilot is down {dark_min:.0f} min ({cause}) — incident {iid}; "
                     "`clipengine supervisor status`", key=f"incident:{iid}", root=root)
    if recovered_by and rec.get("status") != "recovered":
        upd = {"id": iid, "status": "recovered", "recovered_at": now, "recovered_by": recovered_by,
               "dark_min": dark_min}
        _append_incident(upd, root)
        rec = {**rec, **upd}
        AT.event("resolved", "", key=f"incident:{iid}", root=root)
        AT.event("failure", f"autopilot was down {dark_min:.0f} min ({cause}: {evidence[:70]}) — back, "
                 f"recovered by {recovered_by}; incident {iid}", key=f"incident-recovered:{iid}", root=root)
    hour = [r for r in incidents(root, since=now - 3600)]
    if len(hour) >= CRASH_LOOP_N:
        AT.event("failure", f"CRASH LOOP: the autopilot died {len(hour)} times in the last hour — last cause "
                 f"{hour[-1].get('cause')}; `clipengine diagnose`", key=f"crash-loop:{int(now) // 3600}", root=root)
    return rec


def crash_loop_delay(root: str | None = None, now: float | None = None) -> float:
    """Back off before cycling again when the leader keeps dying (30 s, 60 s … up to 10 min)."""
    now = time.time() if now is None else now
    n = len(incidents(root, since=now - 3600))
    return 0.0 if n < CRASH_LOOP_N else min(600.0, 30.0 * 2 ** (n - CRASH_LOOP_N))


# ---- the heartbeat ------------------------------------------------------------------------------------

class Heartbeat(threading.Thread):
    def __init__(self, lease: Lease, beat_s: float = BEAT_S, stall_s: float = STALL_S, ping_url: str | None = None,
                 on_stall=None, clock=time.time, mono=time.monotonic):
        super().__init__(daemon=True, name="klondike-heartbeat")
        self.lease, self.beat_s, self.stall_s, self.ping_url = lease, beat_s, stall_s, ping_url
        self.clock, self.mono = clock, mono
        self.on_stall = on_stall or self._exit_stalled
        self.halt = threading.Event()
        self.phase, self.phase_at = "starting", clock()
        self.last_wall, self.last_mono = clock(), mono()
        self.last_ping = 0.0
        self.ping_error: str | None = None

    def set_phase(self, phase: str) -> None:
        self.phase, self.phase_at = phase, self.clock()
        self.lease.update(phase=phase, progress_at=self.phase_at)

    def beat(self) -> None:
        wall, mono = self.clock(), self.mono()
        slept = (wall - self.last_wall) - (mono - self.last_mono)
        if slept > SUSPEND_JUMP_S:
            iid = f"sleep-{int(self.last_wall)}"
            _append_incident({"id": iid, "component": "host", "cause": "suspension", "dark_from": self.last_wall,
                              "detected_at": wall, "status": "recovered", "recovered_at": wall,
                              "recovered_by": "wake", "dark_min": round(slept / 60, 1),
                              "evidence": f"wall clock advanced {slept:.0f} s more than the monotonic clock"},
                             self.lease.root)
        self.last_wall, self.last_mono = wall, mono
        self.lease.update(heartbeat_at=wall, phase=self.phase)
        if self.phase == "cycle" and wall - self.phase_at > self.stall_s:
            self.on_stall(wall - self.phase_at)
        if self.ping_url and wall - self.last_ping >= PING_EVERY_S:
            self.last_ping = wall
            try:
                urllib.request.urlopen(self.ping_url, timeout=10).read(64)
                self.ping_error = None
            except Exception as exc:                        # the host may be offline: try again next minute
                self.ping_error = str(exc)[:80]

    def _exit_stalled(self, for_s: float) -> None:
        record_exit(self.lease.generation, f"stalled: no cycle progress for {for_s / 60:.0f} min", EXIT_RESTART,
                    self.lease.root)
        os._exit(EXIT_RESTART)

    def run(self) -> None:
        while not self.halt.wait(self.beat_s):
            try:
                self.beat()
            except Exception:                               # a heartbeat must never kill the leader
                pass


def heartbeat_age(root: str | None = None, now: float | None = None) -> float | None:
    st = _read(_p("autopilot.lease.json", root))
    if not st.get("heartbeat_at"):
        return None
    return (time.time() if now is None else now) - float(st["heartbeat_at"])


# ---- native service -----------------------------------------------------------------------------------

def _label() -> str:
    return "com.klondike.autopilot." + hashlib.sha1(REPO.encode()).hexdigest()[:8]


def _unit() -> str:
    return "klondike-autopilot-" + hashlib.sha1(REPO.encode()).hexdigest()[:8] + ".service"


def _run(cmd: list[str], timeout: float = 30) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)


def manager() -> str:
    """launchd | systemd | none (degraded: no service manager this engine can use here)."""
    if sys.platform == "darwin" and shutil.which("launchctl"):
        return "launchd"
    if sys.platform.startswith("linux") and shutil.which("systemctl"):
        rc, _ = _run(["systemctl", "--user", "show-environment"], timeout=10)
        if rc == 0:
            return "systemd"
    return "none"


def _service_path(kind: str) -> str:
    home = os.path.expanduser("~")
    if kind == "launchd":
        return os.path.join(home, "Library", "LaunchAgents", _label() + ".plist")
    return os.path.join(home, ".config", "systemd", "user", _unit())


def _loop_cmd(every: float, sessions: bool) -> list[str]:
    return [sys.executable, "-m", "clipengine", "autopilot", "run", "--every", f"{every:g}"] + \
           (["--sessions"] if sessions else [])


def render_service(kind: str, every: float, sessions: bool) -> str:
    cmd = _loop_cmd(every, sessions)
    log = _p("autopilot.log")
    if kind == "launchd":
        from xml.sax.saxutils import escape as x
        args = "".join(f"\n    <string>{x(c)}</string>" for c in cmd)
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{_label()}</string>
  <key>ProgramArguments</key>
  <array>{args}
  </array>
  <key>WorkingDirectory</key><string>{x(REPO)}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>CLIPENGINE_STARTED_BY</key><string>service</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
    <key>PATH</key><string>{x(os.environ.get("PATH", "/usr/bin:/bin"))}</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>{x(log)}</string>
  <key>StandardErrorPath</key><string>{x(log)}</string>
</dict>
</plist>
"""
    q = lambda c: '"' + c.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return f"""[Unit]
Description=Klondike capture autopilot ({REPO})
StartLimitIntervalSec=3600
StartLimitBurst=20

[Service]
Type=simple
WorkingDirectory={REPO}
Environment=CLIPENGINE_STARTED_BY=service PYTHONUNBUFFERED=1
ExecStart={" ".join(q(c) for c in cmd)}
Restart=on-failure
RestartSec=30
StandardOutput=append:{log}
StandardError=append:{log}

[Install]
WantedBy=default.target
"""


def installed(kind: str | None = None) -> bool:
    kind = kind or manager()
    return kind != "none" and os.path.exists(_service_path(kind))


def install(every: float, sessions: bool = False) -> str:
    kind = manager()
    if kind == "none":
        return ("supervisor: no service manager here (no launchd, no user systemd) — DEGRADED: the autopilot runs "
                "detached; a death is noticed by a sentinel or the next command and reconciled when it's back. "
                "Use a supported always-on host, and set config/ops.json \"heartbeat_url\" so you hear about outages.")
    path = _service_path(kind)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").write(render_service(kind, every, sessions))
    clear_stopped()
    if kind == "launchd":
        uid = os.getuid()
        _run(["launchctl", "bootout", f"gui/{uid}/{_label()}"])
        rc, out = _run(["launchctl", "bootstrap", f"gui/{uid}", path])
        when = "now, and at every login of this macOS user (a LaunchAgent needs the user logged in)"
    else:
        _run(["systemctl", "--user", "daemon-reload"])
        rc, out = _run(["systemctl", "--user", "enable", "--now", _unit()])
        linger = _run(["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger"])[1]
        when = ("now and at boot" if "Linger=yes" in linger else
                "now and at login — run `loginctl enable-linger` once to start it at boot without a login")
    if rc != 0:
        return f"supervisor: wrote {path} but {kind} refused it: {out[:160]}"
    return f"supervisor: installed ({kind}) — the autopilot starts {when}, and restarts within 30 s of any exit that wasn't a stop"


def uninstall() -> str:
    kind = manager()
    path = _service_path(kind) if kind != "none" else ""
    if not path or not os.path.exists(path):
        return "supervisor: not installed"
    if kind == "launchd":
        _run(["launchctl", "bootout", f"gui/{os.getuid()}/{_label()}"])
    else:
        _run(["systemctl", "--user", "disable", "--now", _unit()])
    os.remove(path)
    return "supervisor: uninstalled (the autopilot is no longer restarted by the system)"


def service_stop() -> None:
    kind = manager()
    if not installed(kind):
        return
    if kind == "launchd":
        _run(["launchctl", "kill", "SIGTERM", f"gui/{os.getuid()}/{_label()}"])
    else:
        _run(["systemctl", "--user", "stop", _unit()])


def service_start() -> bool:
    kind = manager()
    if not installed(kind):
        return False
    if kind == "launchd":
        rc, _ = _run(["launchctl", "kickstart", f"gui/{os.getuid()}/{_label()}"])
    else:
        rc, _ = _run(["systemctl", "--user", "start", _unit()])
    return rc == 0


def status(root: str | None = None, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    kind = manager()
    lead = holder(root)
    age = heartbeat_age(root, now)
    inc = incidents(root, since=now - 7 * 86400)
    return {"manager": kind, "installed": installed(kind), "leader": lead, "heartbeat_age_s": age,
            "stopped": stopped(root), "open": [i for i in inc if i.get("status") == "open"],
            "week": [i for i in inc if i.get("status") != "open"]}


def status_lines(root: str | None = None, now: float | None = None) -> list[str]:
    st = status(root, now)
    kind = st["manager"]
    svc = ({"launchd": "launchd LaunchAgent (starts at login)", "systemd": "systemd user service"}.get(kind, kind)
           if st["installed"] else ("not installed — `clipengine supervisor install`" if kind != "none"
                                    else "DEGRADED: no service manager here — deaths are noticed late"))
    lines = [f"supervisor: {svc}"]
    lead = st["leader"]
    if st["stopped"]:
        lines.append(f"leader: stopped on purpose by {st['stopped'].get('by')} — `clipengine autopilot start` resumes")
    elif lead:
        age = st["heartbeat_age_s"]
        lines.append(f"leader: pid {lead.get('pid')} generation {lead.get('generation')} · started by "
                     f"{lead.get('started_by', '?')} · heartbeat {age:.0f} s ago · {lead.get('phase', '?')}"
                     + ("  ← LATE" if age is not None and age > BEAT_S * MISSED_BEATS else ""))
    else:
        lines.append("leader: NONE — nothing is capturing" + (" (never started)" if not ever_started(root) else ""))
    for i in st["open"]:
        lines.append(f"OPEN incident {i['id']}: {i.get('cause')} since {time.strftime('%H:%M', time.localtime(i['dark_from']))}"
                     f" — {i.get('evidence', '')[:90]}")
    if st["week"]:
        dark = sum(float(i.get("dark_min") or 0) for i in st["week"])
        causes: dict[str, int] = {}
        for i in st["week"]:
            causes[i.get("cause", "unknown")] = causes.get(i.get("cause", "unknown"), 0) + 1
        lines.append(f"last 7 days: {len(st['week'])} incident(s), {dark:.0f} min dark — "
                     + ", ".join(f"{k} ×{v}" for k, v in sorted(causes.items())))
    return lines
