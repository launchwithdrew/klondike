"""Operations — so one agent (Muse) runs the whole op with one command, on a Mac or Linux.

  cycle    the entire rhythm, idempotent, ~5 lines of output. Run it every 5–10 minutes:
             1. sentinels: restart any that died; start one on each of the radar's top
                streamers (up to `max_sentinels`); stop ones whose streamer went offline
             2. poster: make sure `node engine/post.mjs --watch` is running (posting machine)
             3. hourly: views (engine/stats.mjs) · daily: learn (+ push data/weights.json)
             4. disk: keep free space above `min_free_gb` (old drafts and recordings go first)
             5. queue: one line per sentinel draft worth a look — the only part that needs you
  doctor   preflight: everything this machine needs, ✓ or ✗ with the fix. Run it first.
  start / stop   one sentinel by hand.

Background processes are started detached with start_new_session (no `setsid`/`nohup`, which
macOS lacks), and tracked with pid files, so they survive the agent's shell ending.
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import re
import os
import shutil
import signal
import subprocess
import sys
import time

from . import untrusted as UT

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS = os.path.join(REPO, "runs")
STATE = os.path.join(RUNS, ".cycle.json")
CONFIG = os.path.join(REPO, "config", "ops.json")
DEFAULTS = {"max_sentinels": 5, "buffer_h": 1.0, "min_free_gb": 15, "stats_every_min": 60,
            "learn_daily_after_utc": 7, "draft_keep_h": 12, "profile": "full", "whisper_model": "small",
            "render_preset": "veryfast", "buffer_max_h": 6.0}
# "profile": "lite" in config/ops.json — for small machines (under 8 GB RAM or 4 cores): fewer sentinels, the
# smaller Whisper model, a faster x264 preset, a shorter recording buffer. Keys set in ops.json still win.
PROFILES = {"full": {},
            "lite": {"max_sentinels": 2, "whisper_model": "tiny", "render_preset": "ultrafast", "buffer_max_h": 1.5,
                     "tier1_max": 15}}


def config() -> dict:
    cfg = dict(DEFAULTS)
    own = {}
    if os.path.exists(CONFIG):
        own = {k: v for k, v in json.load(open(CONFIG)).items() if not k.startswith("_")}
    cfg.update(PROFILES.get(own.get("profile") or "full", {}))
    cfg.update(own)
    return cfg


def whisper_model() -> str:
    try:
        return config().get("whisper_model") or "small"
    except Exception:
        return "small"


def machine() -> dict:
    """RAM (GB) and CPU cores of this machine."""
    try:
        ram = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e9
    except (ValueError, OSError, AttributeError):
        ram = 0.0
    return {"ram_gb": round(ram, 1), "cores": os.cpu_count() or 1}


def suggest_profile(ram_gb: float, cores: int, current: str) -> str | None:
    """doctor: a small machine on the full profile should switch to lite (and only then)."""
    if current == "lite" or (ram_gb >= 8 and cores >= 4) or ram_gb <= 0:
        return None
    return (f"this machine is small ({ram_gb:.0f} GB RAM, {cores} cores) — set \"profile\": \"lite\" in "
            "config/ops.json (2 sentinels, Whisper tiny, faster renders, 1.5 h buffer)")


def _state() -> dict:
    try:
        return json.load(open(STATE))
    except (OSError, ValueError):
        return {}


def _save_state(s: dict) -> None:
    os.makedirs(RUNS, exist_ok=True)
    json.dump(s, open(STATE, "w"), indent=1)


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def _detach(cmd: list[str], log: str, cwd: str = REPO, env: dict | None = None) -> int:
    os.makedirs(os.path.dirname(log), exist_ok=True)
    env = {**os.environ, "HF_HUB_DISABLE_XET": "1", "PYTHONUNBUFFERED": "1", **(env or {})}
    with open(log, "a") as fh:
        p = subprocess.Popen(cmd, cwd=cwd, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             env=env, start_new_session=True)
    return p.pid


# ---- sentinels ------------------------------------------------------------------------------------

def sentinel_dir(channel: str) -> str:
    from .live import source_name
    return os.path.join(RUNS, f"sentinel-{source_name(channel)}")


def sentinel_status(d: str) -> dict:
    pid = None
    try:
        pid = int(open(os.path.join(d, "sentinel.pid")).read().split()[0])
    except (OSError, ValueError, IndexError):
        pass
    beat = {}
    try:
        beat = json.load(open(os.path.join(d, "sentinel.json")))
    except (OSError, ValueError):
        pass
    stopped = os.path.exists(os.path.join(d, "STOPPED"))
    alive = _alive(pid) and (time.time() - beat.get("beat", time.time()) < 180)
    return {"dir": d, "login": beat.get("login") or os.path.basename(d).replace("sentinel-", ""),
            "pid": pid, "alive": alive, "stopped": stopped, "beat": beat,
            "source": (open(os.path.join(d, "source")).read().strip() if os.path.exists(os.path.join(d, "source")) else None)}


def all_sentinels() -> list[dict]:
    return [sentinel_status(d) for d in sorted(glob.glob(os.path.join(RUNS, "sentinel-*"))) if os.path.isdir(d)]


def recorder_pid(d: str) -> int | None:
    try:
        return int(open(os.path.join(d, "recorder.pid")).read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def start_recorder(channel: str, d: str, buf: float) -> bool:
    """The recording runs as its own process, so restarting a sentinel (an update, a crash) never opens a hole
    in it. Returns True when it had to be started."""
    if _alive(recorder_pid(d)):
        return False
    pid = _detach([sys.executable, "-m", "clipengine", "record", "--channel", channel, "--out", d,
                   "--buffer", f"{buf}h", "--supervised"], d + ".rec.log")
    open(os.path.join(d, "recorder.pid"), "w").write(str(pid))
    return True


RECORDER_STALL_S = 360          # an alive recorder with no new media this long (longer than an ad break) is stuck


def recorder_stalled(d: str, now: float | None = None) -> float | None:
    """Seconds since the recorder's last segment when it is alive but stuck (the process lives, no media
    arrives), else None. Only the stuck recorder is restarted — healthy sources and the sentinel keep going."""
    from . import autopilot as AP
    now = time.time() if now is None else now
    if not _alive(recorder_pid(d)) or not os.path.exists(os.path.join(d, "index.jsonl")):
        return None
    try:
        started = os.path.getmtime(os.path.join(d, "recorder.pid"))
    except OSError:
        started = now
    age = AP.recorder_stats(d, now - 6 * 3600, now).get("last_segment_age_s")
    age = (now - started) if age is None else min(age, now - started)
    return age if age > RECORDER_STALL_S else None


def stop_recorder(d: str) -> None:
    pid = recorder_pid(d)
    if pid and _alive(pid):
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except OSError:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass


def start(channel: str, buffer_h: float | None = None) -> str:
    from . import autopilot as AP
    d = sentinel_dir(channel)
    st = sentinel_status(d) if os.path.isdir(d) else None
    if AP.policy(os.path.basename(d).replace("sentinel-", "")) == "never":
        return f"{os.path.basename(d).replace('sentinel-', '')} not started — doesn't allow clipping (policy: never)"
    if st and st["alive"]:
        if not st["stopped"]:
            start_recorder(channel, d, buffer_h or AP.buffer_hours(config(), _free_gb(), AP.explicit_buffer()))
        return f"{st['login']} already running"
    os.makedirs(d, exist_ok=True)
    for f in ("STOPPED",):
        try:
            os.remove(os.path.join(d, f))
        except OSError:
            pass
    open(os.path.join(d, "source"), "w").write(channel)
    buf = buffer_h if buffer_h is not None else AP.buffer_hours(config(), _free_gb(), AP.explicit_buffer())
    start_recorder(channel, d, buf)
    pid = _detach([sys.executable, "-m", "clipengine", "sentinel", "--channel", channel, "--out", d,
                   "--buffer", str(buf), "--no-record"], d + ".log")
    open(os.path.join(d, "sentinel.pid"), "w").write(str(pid))
    # a fresh start is "alive" until its first heartbeat is due
    json.dump({"login": os.path.basename(d).replace("sentinel-", ""), "beat": time.time(), "pid": pid,
               "chat_connected": False, "moments": 0}, open(os.path.join(d, "sentinel.json"), "w"))
    return f"+{os.path.basename(d).replace('sentinel-', '')} started"


def stop(channel_or_dir: str, why: str = "stopped", keep_recorder: bool = False) -> str:
    d = channel_or_dir if os.path.isdir(channel_or_dir) else sentinel_dir(channel_or_dir)
    st = sentinel_status(d)
    if not keep_recorder:
        stop_recorder(d)
    if st["pid"] and _alive(st["pid"]):
        try:
            os.killpg(os.getpgid(st["pid"]), signal.SIGTERM)
        except OSError:
            try:
                os.kill(st["pid"], signal.SIGTERM)
            except OSError:
                pass
    open(os.path.join(d, "STOPPED"), "w").write(why)
    return f"-{st['login']} {why}"


def auto_update(state: dict, cfg: dict, now: float | None = None) -> str | None:
    """A channel's own copy keeps itself current with no one doing anything: every cycle it checks the
    engine's repository, and when the maintainers have pushed a release it installs it with `update`
    (tested on this machine, rolled back if it fails, local fixes kept, sentinels restarted) — at a quiet
    moment, never mid-render. One line in the cycle output. Off with "auto_update": false in config/ops.json."""
    from . import repo as RP
    import glob as _glob
    now = now or time.time()
    if cfg.get("auto_update") is False:
        return None
    if not RP.public():
        return _follow_main(state)
    state["update_check_at"] = now
    g = lambda *a: subprocess.run(["git", "-C", REPO, *a], capture_output=True, text=True, timeout=120)
    if g("fetch", "-q", "origin").returncode != 0:
        return None                                   # offline for a moment: try next hour
    behind = g("rev-list", "--count", "HEAD..@{u}").stdout.strip()
    if not behind or behind == "0":
        return None
    new = g("log", "-1", "--format=%s", "@{u}").stdout.strip()
    busy = [f for f in _glob.glob(os.path.join(REPO, "runs", "**", "*.src.ts"), recursive=True)
            if now - os.path.getmtime(f) < 900]
    if busy:                                          # a clip is rendering: install next cycle instead
        return f"update: {new} is ready — installing after the clip that's rendering now"
    import contextlib
    import io
    from . import support as SP
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            ok = SP.update(quiet=True)
    except SystemExit as exc:
        return f"update: couldn't install {new} ({str(exc)[:80]}) — will retry next hour"
    if ok:
        from . import version_on_disk
        return f"update: installed engine v{version_on_disk()} — {new} (tests passed here; sentinels restarted)"
    return f"update: {new} failed its tests on this machine — rolled back, still running the previous version " \
           "(send `clipengine support --problem \"update failed its tests\"` to the maintainers)"


def _follow_main(state: dict) -> str | None:
    """The shared operation repo: take whatever the factory merged into main (tested before it was merged),
    with drops, ledger and data — and when engine code changed, restart the sentinels on it and re-run
    setup if dependencies changed. Silent when nothing changed."""
    g = lambda *a: subprocess.run(["git", "-C", REPO, *a], capture_output=True, text=True, timeout=180)
    before = g("rev-parse", "HEAD").stdout.strip()
    if g("pull", "-q", "--rebase", "--autostash").returncode != 0:
        g("rebase", "--abort")
        return "update: couldn't pull main (a conflict?) — run `git status`; nothing was changed"
    after = g("rev-parse", "HEAD").stdout.strip()
    if not before or before == after:
        return None
    changed = g("diff", "--name-only", before, after).stdout.split()
    code = [f for f in changed if f.startswith(("clipengine/", "engine/", "tools/", "setup/")) or f in ("requirements.txt",)]
    if not code:
        return None                                    # just drops / ledger / data from another machine
    if any(f in ("requirements.txt", "engine/package-lock.json") or f.startswith("setup/") for f in changed):
        subprocess.run(["bash", os.path.join(REPO, "setup", "setup.sh"), "--quiet"], cwd=REPO, capture_output=True)
    restarted = restart_sentinels(changed)
    head = g("log", "-1", "--format=%h %s").stdout.strip()
    from . import version_on_disk
    return f"update: engine v{version_on_disk()} — now on {head[:70]}" + (
        f" — {len(restarted)} sentinel(s) restarted on it" if restarted else "")


# what a running sentinel / recorder has in memory: only a change to these restarts them after an update
SENTINEL_CODE = ("clipengine/sentinel.py", "clipengine/signals.py", "clipengine/live.py", "clipengine/twitch.py",
                 "clipengine/audio.py", "clipengine/grade.py", "clipengine/safety.py", "clipengine/vision.py",
                 "clipengine/censor.py", "clipengine/untrusted.py", "clipengine/attention.py",
                 "clipengine/prerender.py", "clipengine/youtube.py", "clipengine/tier1.py", "clipengine/kick.py", "clipengine/render.py", "clipengine/card.py", "clipengine/brand.py",
                 "clipengine/speaker.py", "clipengine/facecam.py",
                 "config/sentinel_model.json", "config/grading.json", "requirements.txt")
RECORDER_CODE = ("clipengine/live.py", "clipengine/twitch.py", "clipengine/youtube.py", "clipengine/kick.py")


def restart_sentinels(changed: list[str] | None = None) -> list[str]:
    """Running sentinels keep the old code in memory: after an update that touched their code, restart each on
    it. The recorder (its own process) restarts only when the recorder's code changed — a restart opens a
    hole (a fresh pre-roll ad), so it is avoided whenever it can be. changed=None: restart everything."""
    if changed is not None and not any(f in SENTINEL_CODE for f in changed):
        return []
    rec_too = changed is None or any(f in RECORDER_CODE for f in changed)
    out = []
    for s in all_sentinels():
        if not (s.get("pid") and _alive(s["pid"])) or s.get("stopped"):
            continue
        src = s.get("source") or s["login"]
        stop(s["dir"], "restarting on the new version", keep_recorder=not rec_too)
        if rec_too:
            for _ in range(40):
                if not _alive(recorder_pid(s["dir"])):
                    break
                time.sleep(0.25)
        for _ in range(40):                        # let it exit before starting again
            if not _alive(s["pid"]):
                break
            time.sleep(0.25)
        out.append(start(src).replace("started", "restarted on the new version"))
    return out


def stop_all() -> list[str]:
    """Stop every sentinel and the poster on this machine (cycle starts them again when it next runs)."""
    out = [stop(s["dir"]) for s in all_sentinels() if s.get("pid") and _alive(s["pid"])]
    for s in all_sentinels():                       # a recorder can outlive its sentinel
        if recorder_pid(s["dir"]) and _alive(recorder_pid(s["dir"])):
            stop_recorder(s["dir"])
            out.append(f"-{s['login']} recorder stopped")
    if _poster_running():
        try:
            os.kill(int(open(os.path.join(REPO, ".engine.lock")).read().strip()), signal.SIGTERM)
            out.append("-poster stopped")
        except (OSError, ValueError):
            pass
    return out or ["nothing was running"]


# ---- the cycle ---------------------------------------------------------------------------------------

def _can_push() -> bool:
    """Can this machine push to the operation repo? If the GitHub CLI is signed in but git isn't using
    its login yet (`gh auth login` alone doesn't do that), connect them once and check again."""
    dry = lambda: subprocess.run(["git", "-C", REPO, "push", "--dry-run", "-q", "origin",
                                  "HEAD:refs/heads/_doctor_push_check"], capture_output=True).returncode == 0
    if dry():
        return True
    if shutil.which("gh") and subprocess.run(["gh", "auth", "status"], capture_output=True).returncode == 0:
        subprocess.run(["gh", "auth", "setup-git"], capture_output=True)
        return dry()
    return False


def _posting_machine() -> bool:
    """This machine posts: it has the engine's signed-in Chrome, or uses the agent's own browser."""
    return os.path.isdir(os.path.join(REPO, "chrome-profile")) or bool(config().get("browser_cdp_url"))


def _poster_running() -> bool:
    lock = os.path.join(REPO, ".engine.lock")
    try:
        return _alive(int(open(lock).read().strip()))
    except (OSError, ValueError):
        return False


def _free_gb() -> float:
    return shutil.disk_usage(REPO).free / 1e9


def _mem_available_gb() -> float | None:
    """Memory free for new work (Linux: MemAvailable; macOS: free + inactive + speculative pages)."""
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1e6
    except OSError:
        pass
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
        page = int(re.search(r"page size of (\d+)", out).group(1))
        pages = sum(int(re.search(rf"{k}:\s+(\d+)", out).group(1)) for k in ("Pages free", "Pages inactive", "Pages speculative"))
        return pages * page / 1e9
    except Exception:
        return None


MIN_CORES = 4
MIN_FREE_RAM_GB = 4.0


def scout_cap(state: dict, cfg: dict, load_per_core: float | None, mem_gb: float | None, holes: int,
              live_roster: int, now: float) -> tuple[int, str]:
    """How many full scouts this machine runs now (pure but for state). Starts at max_sentinels; one more when
    there's room (load < 0.6 per core, 3 GB+ free, no recording holes last cycle) and more roster streamers are
    live than scouts; one fewer when it's strained (load > 0.85 per core, < 1.5 GB free, or holes). At most
    one step per 10 min, between 2 and max_sentinels_cap. "adaptive_sentinels": false pins max_sentinels."""
    base = int(cfg["max_sentinels"])
    if cfg.get("adaptive_sentinels", True) is False:
        return base, "fixed (adaptive_sentinels: false)"
    hi = max(base, int(cfg.get("max_sentinels_cap", 10)))
    cap = min(hi, max(2, int(state.get("cap") or base)))
    if now - float(state.get("cap_at") or 0) < 600:
        return cap, state.get("cap_why") or "steady"
    strained = [w for w, bad in (("CPU busy", load_per_core is not None and load_per_core > 0.85),
                                 ("memory low", mem_gb is not None and mem_gb < 1.5),
                                 (f"{holes} recording hole(s)", holes > 0)) if bad]
    room = (load_per_core is not None and load_per_core < 0.6 and mem_gb is not None and mem_gb >= 3 and not holes)
    why = "steady"
    state["degraded"] = bool(strained) and cap <= 2      # at the floor and still strained: lite renders too
    if strained and cap > 2:
        cap, why = cap - 1, "fewer: " + ", ".join(strained)
    elif room and live_roster > cap and cap < hi:
        cap, why = cap + 1, f"more: room (load {load_per_core:.2f}/core, {mem_gb:.0f} GB free)"
    if why != "steady":
        state["cap_at"] = now
    state["cap"], state["cap_why"] = cap, why
    return cap, why


def _prune(cfg: dict) -> list[str]:
    """Free disk: drafts older than draft_keep_h, then recordings of stopped sentinels — never what a queued
    draft (status new) still needs: its draft files, and the recording around its window."""
    from . import autopilot as AP
    from .sentinel import load_queue
    out = []
    now = time.time()
    cutoff = now - cfg["draft_keep_h"] * 3600
    try:
        keep_ids = {r["id"] for r in load_queue(RUNS) if r.get("status") == "new" and now - r.get("stop", 0) < 86400}
    except Exception:
        keep_ids = set()
    n = 0
    for f in glob.glob(os.path.join(RUNS, "sentinel-*", "drafts", "*")):
        if os.path.basename(f).split(".")[0] in keep_ids:
            continue
        if os.path.getmtime(f) < cutoff:
            try:
                os.remove(f); n += 1
            except OSError:
                pass
    if n:
        out.append(f"removed {n} old draft files")
    if _free_gb() < cfg["min_free_gb"]:
        for st in all_sentinels():
            if (st["stopped"] or not st["alive"]) and not _alive(recorder_pid(st["dir"])):
                seg = os.path.join(st["dir"], "segments")
                if not os.path.isdir(seg):
                    continue
                keep = AP.protected_windows(st["dir"])
                if keep:                                   # only the stretches no queued draft needs
                    gone = 0
                    for f in glob.glob(os.path.join(seg, "*.ts")):
                        try:
                            t = int(os.path.basename(f)[:-3]) / 1000
                        except ValueError:
                            continue
                        if not AP.is_protected(t, 2.0, keep):
                            os.remove(f); gone += 1
                    if gone:
                        out.append(f"cleared most of {st['login']}'s old recording (kept what {len(keep)} queued draft(s) need)")
                    continue
                shutil.rmtree(seg, ignore_errors=True)
                try:
                    os.remove(os.path.join(st["dir"], "index.jsonl"))
                except OSError:
                    pass
                out.append(f"cleared {st['login']}'s old recording")
    return out


PRERENDER_PID = os.path.join(RUNS, ".prerender.pid")


def maybe_prerender(cfg: dict) -> str | None:
    """Start `clipengine prerender --pending` in the background when a queued draft worth a look has no
    final render yet (one at a time). Off with "prerender": false in config/ops.json."""
    if cfg.get("prerender", True) is False:
        return None
    try:
        pid = int(open(PRERENDER_PID).read().strip())
    except (OSError, ValueError):
        pid = None
    if _alive(pid):
        return None
    from . import prerender as PR
    try:
        todo = PR.pending()
    except Exception:
        return None
    if not todo:
        return None
    pid = _detach([sys.executable, "-m", "clipengine", "prerender", "--pending"], os.path.join(RUNS, "prerender.log"))
    os.makedirs(RUNS, exist_ok=True)
    open(PRERENDER_PID, "w").write(str(pid))
    return f"prerender: {len(todo)} draft(s) getting their final render in the background"


def _git(*a: str) -> bool:
    return subprocess.run(["git", "-C", REPO, *a], capture_output=True).returncode == 0


def plan_sentinels(rows: list[dict], moments: list[dict], roster: dict, top: int,
                   promoted: list[str] | None = None, live_extra: set[str] | None = None,
                   restricted: set[str] | None = None) -> list[str]:
    """Who should have a sentinel (tier 2), in order (pure but for data/vod_access.json when `restricted`
    isn't given):
    1. roster creators with no saved VODs ("vods": false, or subscriber-only VODs — learned, vodaccess.py)
       the moment they're live — anything before their recording starts is gone for good, so they beat
       radar heat;
    2. creators whose tier-1 chat is hot right now (`promoted`, hottest first) — a moment is building;
    3. the radar's picks (action scan/record);
    4. free slots go to the streamers of the hottest viewer-clipped moments, then the rest by radar score.
    Creators with policy "never" never get one."""
    from . import autopilot as AP
    live = {r["login"] for r in rows} | set(live_extra or ())
    ok = lambda l: l in live and AP.policy(l, roster) != "never"
    if restricted is None:
        from . import vodaccess as VA
        restricted = VA.restricted_set()
    first = [l for l, c in roster.items() if (c.get("vods") is False or l.lower() in restricted) and ok(l)]
    prom = [l for l in (promoted or []) if ok(l)]
    picked = [r["login"] for r in rows if r.get("action") in ("scan", "record", "start") and ok(r["login"])]
    hot = [m["login"] for m in moments if not m.get("unsafe") and not m.get("published") and ok(m["login"])]
    rest = [r["login"] for r in rows if ok(r["login"])]
    return list(dict.fromkeys(first + prom + picked + hot + rest))[:top]


def _note_live(state: dict, rows: list[dict], roster: dict, now: float) -> None:
    """Remember roster creators' live sessions (schedule learns live-only creators' hours from these)."""
    import datetime as _dt
    seen = state.setdefault("seen_live", {})
    for r in rows:
        if r["login"] not in roster or not r.get("started"):
            continue
        try:
            s0 = _dt.datetime.fromisoformat(r["started"].replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        lst = seen.setdefault(r["login"], [])
        if lst and abs(lst[-1][0] - s0) < 600:
            lst[-1][1] = now
        else:
            lst.append([s0, now])
        seen[r["login"]] = lst[-60:]
    state["any_live"] = any(r["login"] in roster for r in rows)


def _missed_minutes(state: dict, rows: list[dict], roster: dict, now: float) -> list[str]:
    """Keep the period before a live-only recording starts visible until a first segment arrives."""
    import datetime as _dt

    from . import attention as AT
    from . import live as L
    from . import vodaccess as VA
    out = []
    named = state.setdefault("missed_named", [])
    waiting = state.setdefault("missed_waiting", [])
    restricted = VA.restricted_set(now)
    for r in rows:
        login, started = r["login"], r.get("started")
        if not started or ((roster.get(login) or {}).get("vods") is not False and login.lower() not in restricted):
            continue
        key = f"{login}@{started}"
        if key in named:
            continue
        try:
            s0 = _dt.datetime.fromisoformat(started.replace("Z", "+00:00")).timestamp()
            first = next((row["t"] for row in L.load_index(sentinel_dir(login)) if row["t"] >= s0 - 60), None)
        except (OSError, ValueError):
            continue
        if first is None:
            if now - s0 < 60 or key in waiting:
                continue
            waiting.append(key)
            line = f"{login} (no VOD) has waited {(now - s0) / 60:.0f} min for its first recorded segment"
        else:
            named.append(key)
            missing = first - s0
            if missing <= 0:
                if key in waiting:
                    AT.event("resolved", "", key=f"missed:{key}", now=now)
                continue
            span = f"{missing / 60:.0f} min" if missing >= 120 else f"{missing:.0f} s"
            line = f"{login} (no VOD) lost {span} before the first recorded segment"
        AT.event("failure", line, key=f"missed:{key}", now=now)
        out.append(line)
    state["missed_named"] = named[-50:]
    state["missed_waiting"] = waiting[-50:]
    return out


def _attention_items(rows: list[dict], cfg: dict, state: dict, now: float) -> list[dict]:
    """The state half of the attention line, as of this cycle."""
    from .sentinel import is_urgent
    items = [{"kind": "urgent" if is_urgent(r) or r.get("angle_urgent") else "draft", "key": f"draft:{r['id']}",
              "text": f"{r['id']} {r.get('best', 0):.0f}", "score": r.get("best", 0)} for r in rows]
    from . import handpost as HP
    if HP.enabled():
        for it in HP.due(now):
            try:
                meta = json.load(open(os.path.join(os.path.dirname(it["file"]), "post.json")))
            except (OSError, ValueError):
                meta = {}
            for p in it["now"]:
                at = (meta.get("schedule") or {}).get(p) or meta.get("dropped_at")
                grace = 15 if (meta.get("schedule") or {}).get(p) else 30
                ts = None
                try:
                    import datetime as _dt
                    ts = _dt.datetime.fromisoformat(str(at).replace("Z", "+00:00")).timestamp() if at else None
                except ValueError:
                    pass
                late = ts is not None and now - ts > grace * 60
                items.append({"kind": "overdue" if late else "due",
                              "key": f"{'overdue' if late else 'due'}:{it['slug']}:{p}",
                              "text": f"{it['slug']} {p}" + (f" ({(now - ts) / 60:.0f} min late)" if late else "")})
    for item in HP.to_measure(now, lookback_h=float("inf")):
        if item["missing"]:
            slug, platform = item["slug"], item["platform"]
            items.append({"kind": "overdue", "key": f"measure:{slug}:{platform}",
                          "text": f"measure {slug} on {platform}"})
    if state.get("report_text") and state.get("report_day"):
        items.append({"kind": "report", "key": f"report:{state['report_day']}", "text": state["report_text"]})
    return items


def _daily_report(state: dict, cfg: dict, now: float) -> str | None:
    import datetime as _dt
    loc = _dt.datetime.fromtimestamp(now).astimezone()
    day = loc.strftime("%Y-%m-%d")
    if loc.hour < cfg.get("report_after_local_hour", 9) or state.get("report_day") == day:
        return None
    import contextlib, io
    from . import support as SP
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            text = SP.report(1.0)
    except Exception as exc:
        text = f"REPORT unavailable ({str(exc)[:60]})"
    state["report_day"], state["report_text"] = day, " | ".join(" ".join(l.split()) for l in text.splitlines() if l.strip())
    return "report: today's report is on the attention line (clipengine attention)"


class _Lock:
    """One cycle at a time on a machine (autopilot + a hand-run cycle must not race)."""
    def __enter__(self):
        os.makedirs(RUNS, exist_ok=True)
        self.fh = open(os.path.join(RUNS, ".cycle.lock"), "w")
        try:
            import fcntl
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.ok = True
        except ImportError:
            self.ok = True
        except OSError:
            self.ok = False
        return self

    def __exit__(self, *a):
        self.fh.close()


def cycle(top: int | None = None, radar: bool = True, quiet_queue: bool = False) -> dict:
    with _Lock() as lk:
        if not lk.ok:
            print("cycle: another cycle is running right now — skipped")
            return {"lines": [], "queue": [], "skipped": True}
        return _cycle(top, radar, quiet_queue)


def _cycle(top: int | None = None, radar: bool = True, quiet_queue: bool = False) -> dict:
    from . import sentinel as SN
    from . import attention as AT
    from . import autopilot as AP
    from .publish import creators
    cfg = config()
    state = _state()
    lines, notes = [], []
    now = time.time()
    roster = creators()
    if not top:
        try:
            lpc = os.getloadavg()[0] / max(1, os.cpu_count() or 1)
        except OSError:
            lpc = None
        top, cap_why = scout_cap(state, cfg, lpc, _mem_available_gb(), int(state.get("last_holes") or 0),
                                 int(state.get("live_roster") or 0), now)
        if cap_why.startswith(("more", "fewer")):
            notes.append(f"full scouts → {top} ({cap_why})")

    # 0. auto-update (public copies): once an hour, take a new release — tested here, rolled back if it fails
    line = auto_update(state, cfg)
    if line:
        lines.append(line)

    # 0b. catch-up: a gap since the last cycle → scan the saved streams of that time (background)
    try:
        line = AP.maybe_catchup(state, now, cfg)
        if line:
            lines.append(line)
    except Exception as exc:
        AT.event("failure", f"catch-up couldn't start ({str(exc)[:80]})", key=f"catchup-start:{int(now) // 3600}")
    # 0c. the sweep: every 30 min, the roster's recent saved streams (chat + viewer clips) for missed moments
    try:
        line = AP.maybe_sweep(state, now, cfg)
        if line:
            lines.append(line)
    except Exception as exc:
        AT.event("failure", f"sweep couldn't start ({str(exc)[:80]})", key=f"sweep-start:{int(now) // 3600}")
    prev_cycle = state.get("cycle_at") or now - 600
    state["cycle_at"] = now

    # 1. sentinels: revive the dead (and their recorders), follow the radar
    sents = {s["login"]: s for s in all_sentinels()}
    for s in sents.values():
        if s["stopped"]:
            continue
        if not s["alive"]:
            notes.append(start(s["source"] or s["login"]).replace("started", "restarted"))
            AT.event("failure", f"sentinel {s['login']} had died — restarted", key=f"died:{s['login']}:{int(now) // 600}")
        elif not _alive(recorder_pid(s["dir"])) and os.path.exists(os.path.join(s["dir"], "recorder.pid")):
            start_recorder(s["source"] or s["login"], s["dir"], AP.buffer_hours(cfg, _free_gb(), AP.explicit_buffer()))
            notes.append(f"{s['login']}'s recorder restarted")
            AT.event("failure", f"{s['login']}'s recorder had stopped — restarted", key=f"recdied:{s['login']}:{int(now) // 600}")
        else:
            stuck = recorder_stalled(s["dir"], now)
            if stuck:
                stop_recorder(s["dir"])
                start_recorder(s["source"] or s["login"], s["dir"], AP.buffer_hours(cfg, _free_gb(), AP.explicit_buffer()))
                notes.append(f"{s['login']}'s recorder was stuck ({stuck / 60:.0f} min without media) — restarted")
                AT.event("failure", f"{s['login']}'s recorder was alive but stuck ({stuck / 60:.0f} min without media) "
                         "— restarted it alone", key=f"recstuck:{s['login']}:{int(now) // 600}")
    if radar:
        try:
            from . import radar as R
            res = R.run(None, None, show=top + 3, quiet=True)
            from . import scouts
            scouts.remember(res.get("viewer_clips") or [], now, RUNS)
            live = set(res.get("live_logins") or [r["login"] for r in res["streamers"]])   # every live stream, not only ranked ones
            from .live import source_name as _sn0
            live |= {_sn0(l) for l in live if l.startswith("kick:")}      # a Kick sentinel runs as kick-<slug>
            # Kick / YouTube rows off the roster are listed for a person to judge: no automatic sentinel
            plan_rows = [r for r in res["streamers"] if r.get("platform") not in ("kick", "yt") or r["login"] in roster]
            if any(k.startswith("kick:") for k in roster):     # Kick creators: their own live check
                from . import kick as K
                from .live import source_name as _sn
                kl = K.live_roster(roster)
                live |= kl | {_sn(k) for k in kl}
            _note_live(state, res["streamers"], roster, now)
            # tier 1: chat-only on every live roster creator; a hot one is promoted to tier 2
            from . import tier1 as TR
            try:
                n1 = TR.ensure(TR.tier1_channels(roster, live, cfg))
                if n1:
                    notes.append(n1)
                prom = TR.promoted(state, TR.load_state(), cfg, now, live)
                line = TR.start_spike_catchup(state, TR.spike_catchups(state, roster, now, cfg))
                if line:
                    lines.append(line)
            except Exception as exc:
                prom = []
                notes.append(f"tier 1 failed: {str(exc)[:60]}")
            want = plan_sentinels(plan_rows, res["moments"], roster, top, promoted=prom, live_extra=live)
            state["watch_today"] = want
            for l in prom:
                if l in want and l not in {s_["login"] for s_ in all_sentinels() if s_["alive"] and not s_["stopped"]}:
                    notes.append(f"{l} promoted to tier 2 (chat heating up)")
            sents = {s["login"]: s for s in all_sentinels()}          # fresh: includes ones just revived
            running = [l for l, s in sents.items() if s["alive"] and not s["stopped"]]
            for l in running:                                   # stop the ones whose stream ended / not allowed
                if not l.startswith("yt-") and (l not in live or AP.policy(l, roster) == "never"):
                    notes.append(stop(l, "stopped (offline)" if l not in live else "stopped (policy: never)"))
            running = [l for l in running if (l in live and AP.policy(l, roster) != "never") or l.startswith("yt-")]
            from .live import source_name as _sname
            for l in want:
                if l in running or _sname(l) in running:      # kick:<slug> runs as kick-<slug>
                    continue
                if len(running) >= top:                         # full: a cooled-off streamer gives up its slot
                    cold = [r for r in running if r not in want and not r.startswith("yt-")]
                    if not cold:
                        break
                    notes.append(stop(cold[-1], "stopped (cooled off)"))
                    running.remove(cold[-1])
                notes.append(start(l)); running.append(l)
            notes += _missed_minutes(state, res["streamers"], roster, now)
            state["radar_at"] = time.time()
            hot = ", ".join(f"{r['login']} {r['score']:.0f}" for r in res["streamers"][:top + 2])
            moments = [m for m in res["moments"] if not m["unsafe"] and not m["published"]][:1]
            lines.append(f"radar: {res.get('live_count', len(live))} live — top: {hot}" + (
                f" · hottest viewer-clipped moment: {moments[0]['login']} {UT.quote(moments[0]['titles'][0], 40)} "
                f"({moments[0]['vpm']:.0f} views/min) → " + (f"`{moments[0]['cut']}`" if moments[0].get("cut")
                                                              else moments[0].get("note", "")) if moments else ""))
        except Exception as exc:
            notes.append(f"radar failed: {str(exc)[:80]}")
            AT.event("failure", f"radar failed ({str(exc)[:80]})", key=f"radar:{int(now) // 1800}")
    alive = [s for s in all_sentinels() if s["alive"] and not s["stopped"]]
    try:
        state["live_roster"] = len(set(live) & set(roster))
    except NameError:
        pass
    holes = []
    for s in all_sentinels():
        for g in AP.holes_since(s["dir"], prev_cycle - 60):
            holes.append(g)
            AT.event("failure", f"{s['login']}'s recording has a {g['len']:.0f} s hole at "
                     f"{AP._hm(g['t'])} — {g['cause']}", key=f"hole:{s['login']}:{int(g['t'])}")
    state["last_holes"] = sum(1 for g in holes if not str(g.get("cause", "")).startswith("ad"))
    lines.append("sentinels: " + (", ".join(
        f"{s['login']} ({s['beat'].get('moments', 0)} moments)" for s in alive) or "none running")
        + (f"  [{'; '.join(notes)}]" if notes else "")
        + (f"  ⚠ {len(holes)} recording hole(s) — clipengine sentinels" if holes else ""))

    # 2. poster
    from . import handpost as HP
    if HP.enabled():
        n = sum(1 for it in HP.due() if it["now"])
        lines.append(f"poster: you post with your own browser — {n} clip(s) due: clipengine to-post" if n
                     else "poster: you post with your own browser — nothing due")
    elif _posting_machine():
        if not _poster_running():
            _detach(["node", os.path.join(REPO, "engine", "post.mjs"), "--watch"],
                    os.path.join(REPO, "logs", "poster.log"), cwd=os.path.join(REPO, "engine"))
            lines.append("poster: started (node engine/post.mjs --watch)")
        else:
            lines.append("poster: running")
    else:
        from . import repo as RP
        lines.append("poster: not signed in yet — the user signs in once: cd engine && node post.mjs --login"
                     if RP.public() else
                     "poster: not this machine (no signed-in chrome-profile/) — drops are posted by the posting machine")

    # 3. hourly stats, daily learn
    if _posting_machine() and not HP.enabled() and now - state.get("stats_at", 0) >= cfg["stats_every_min"] * 60:
        r = subprocess.run(["node", os.path.join(REPO, "engine", "stats.mjs")], capture_output=True, text=True,
                           cwd=os.path.join(REPO, "engine"), timeout=600)
        state["stats_at"] = now
        tail = (r.stdout.strip().splitlines() or [""])[-1]
        lines.append("stats: " + (tail[-90:] if r.returncode == 0 else f"failed — {(r.stderr or tail)[-90:]}"))
    today = dt.datetime.now(dt.timezone.utc)
    test_run = os.environ.get("CLIPENGINE_NO_SYNC") == "1"      # a test run never rewrites tracked data
    if not test_run and today.hour >= cfg["learn_daily_after_utc"] and state.get("learn_day") != today.strftime("%Y-%m-%d"):
        from . import learn as LN
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            res = LN.run(write=True)
        state["learn_day"] = today.strftime("%Y-%m-%d")
        from . import repo as RP
        if RP.sync():
            _git("add", "data/weights.json")
        if RP.sync() and _git("-c", "user.name=clipengine", "-c", "user.email=clipengine@users.noreply.github.com",
                "commit", "-qm", "learn: daily weights"):
            _git("pull", "-q", "--no-rebase") and _git("push", "-q")
        lines.append("learn: updated data/weights.json" + (f" — {res['report'][0]}" if res.get("report") else ""))

    # 3b. opt-in community insights, once a day
    try:
        from . import insights as IN
        if IN.config().get("enabled") and now - state.get("insights_at", 0) >= 86400:
            lines.append(IN.submit(quiet=True))
            state["insights_at"] = now
    except Exception:
        pass

    # 3c. the daily report, onto the attention line
    line = _daily_report(state, cfg, now)
    if line:
        lines.append(line)

    # 3d. pre-render queued drafts the sentinels didn't (an older draft, a failed try): publish takes seconds
    try:                                                     # route every new draft before anything renders
        from . import triage as TR
        tline = TR.apply()
        if tline:
            lines.append(tline)
    except Exception as exc:
        AT.event("failure", f"triage failed ({str(exc)[:80]})", key=f"triage:{int(time.time()) // 3600}")
    try:                                                     # the approved finish: a changed look is said once
        from . import finish as FN
        fc = FN.check()
        if fc["state"] == "drift":
            AT.event("failure", f"finish changed since the approved {FN.label(FN.approved())}: "
                     + "; ".join(fc["changes"][:2]) + " — clips with it won't drop; `clipengine finish check`",
                     key=f"finish-drift:{fc['key']}")
        elif fc["state"] == "ok":
            st = FN.check_still()                           # once per engine version (remembered)
            if st and not st.get("same"):
                AT.event("failure", f"this engine draws the approved {FN.label(FN.approved())} differently: "
                         f"{st.get('why', '')} — look at `clipengine finish check --still` before posting",
                         key=f"finish-still:{st.get('engine')}:{fc['key']}")
    except Exception:
        pass
    line = maybe_prerender(cfg)
    if line:
        lines.append(line)

    # 4. disk
    freed = _prune(cfg)
    free = _free_gb()
    if free < cfg["min_free_gb"]:
        lines.append(f"disk: ⚠ only {free:.0f} GB free — stop a sentinel (`clipengine stop --channel X`)")
    elif freed:
        lines.append(f"disk: {free:.0f} GB free ({'; '.join(freed)})")

    _save_state(state)
    for l in lines:
        print(l)
    # 5. the only part that needs judgment
    rows = SN.show_queue(seen=False) if not quiet_queue else []
    if not rows:
        print("queue: empty — nothing needs you")
    # 6. attention: one line of what needs the agent, and the notify hook when it gained something new
    try:
        AT.set_current(_attention_items(rows, cfg, state, now))
        extra = [i for i in AT.pending() if i.get("kind") != "draft"]
        if extra:
            print(AT.line(extra))
        sent = AT.notify(cfg)
        if sent and "failed" in sent:
            print(sent)
    except Exception as exc:
        print(f"attention: couldn't update ({str(exc)[:80]})")
    return {"lines": lines, "queue": rows}


# ---- doctor ----------------------------------------------------------------------------------------

def doctor(accounts: bool = False) -> bool:
    ok_all = True
    from . import __version__
    sha = subprocess.run(["git", "-C", REPO, "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    print(f"engine v{__version__}" + (f" ({sha})" if sha else "") + f" · profile {config().get('profile', 'full')}")

    def check(name: str, ok: bool, fix: str = "") -> None:
        nonlocal ok_all
        ok_all &= ok
        print(f"{'✓' if ok else '✗'} {name}" + ("" if ok else f" — {fix}"))

    from . import repo as RP
    for mod in ("faster_whisper", "numpy", "PIL", "websocket", "yt_dlp"):
        try:
            __import__(mod)
            check(f"python: {mod}", True)
        except Exception:
            check(f"python: {mod}", False, "bash setup/setup.sh")
    hub = os.path.expanduser(os.environ.get("HF_HOME", "~/.cache/huggingface"))
    check("whisper models (tiny + small)",
          all(glob.glob(os.path.join(hub, "**", f"*faster-whisper-{m}*"), recursive=True) for m in ("tiny", "small")),
          "bash setup/setup.sh (downloads ~540 MB)")
    for b in ("ffmpeg", "ffprobe", "node", "git"):
        check(b, shutil.which(b) is not None, f"install {b}")
    check("poster deps (engine/node_modules)", os.path.isdir(os.path.join(REPO, "engine", "node_modules", "playwright-core")),
          "cd engine && npm ci")
    from . import brand as B
    check("brand pack (brand/brand.json)", os.path.exists(B.path()), "clipengine init (sets up this channel)")
    if RP.public() and os.path.exists(B.path()):
        check("user agreed to the risks", bool(B.load(fresh=True).get("risks_acknowledged")),
              "read the user the risks (clipengine init prints them), then add \"risks_acknowledged\": \"<date>\" "
              "to brand/brand.json once they say yes")
    check("logo", os.path.isfile(B.asset(B.load(fresh=True)["logo"].get("file", ""))),
          "clipengine init makes one (brand/kit/logo.png), or set logo.file in brand/brand.json")
    check("fonts", os.path.exists(os.path.join(REPO, "assets", "fonts", "NotoColorEmoji.ttf")), "git pull (assets/ is in the repo)")
    # R2 / W4: headroom against the profile, stated rather than discovered
    cores = os.cpu_count() or 1
    try:
        lpc = os.getloadavg()[0] / cores
    except OSError:
        lpc = None
    mem = _mem_available_gb()
    from . import autopilot as _AP
    buf = _AP.buffer_hours(config(), _free_gb(), _AP.explicit_buffer())
    small = cores < MIN_CORES or (mem is not None and mem < MIN_FREE_RAM_GB)
    print(f"  {'!' if small else '\u2713'} machine: {cores} cores, load {lpc:.2f}/core" if lpc is not None else
          f"  {'!' if small else '\u2713'} machine: {cores} cores", end="")
    print(f", {mem:.1f} GB memory free" if mem is not None else "", end="")
    print(f", {_free_gb():.0f} GB disk free, recording buffer {buf:g} h per sentinel")
    if small:
        print(f"    below the comfortable minimum ({MIN_CORES} cores, {MIN_FREE_RAM_GB:g} GB free): expect slow scans — "
              "set \"profile\": \"lite\" in config/ops.json, or use a bigger machine")
    if buf < 3:
        print("    the recording buffer is under 3 h (disk): a moment found later than that can only be re-cut from the "
              "saved stream (VOD creators) — free disk space for a longer buffer")
    from . import twitch as _TW
    if _TW.auth_token():
        print("  \u2713 recording logged in (TWITCH_AUTH_TOKEN): ad-free if the account has Turbo or a sub to that "
              "channel; sub-only VODs open")
    else:
        print("  \u00b7 recording logged out: ad breaks become gaps (optional: set TWITCH_AUTH_TOKEN in the environment "
              "\u2014 docs/troubleshooting.md)")
    from . import supervise as _SV
    from . import autopilot as _AP
    _back = _AP.revive_supervised("doctor")
    if _back:
        print(f"  \u00b7 {_back}")
    _kind = _SV.manager()
    _boot = _SV.boot_mode() if _SV.settings() else ""
    _cold = _boot.startswith(("on the next", "cron @reboot is set"))
    if _SV.installed(_kind):
        print(f"  \u2713 supervisor: {_kind} service restarts the autopilot after any exit that wasn't a stop")
        print("  " + ("\u00b7" if _cold else "\u2713") + f" after a reboot: {_boot}")
    elif _SV.keeper_alive():
        print("  \u2713 supervisor: the engine's keeper restarts the autopilot after any exit that wasn't a stop")
        print("  " + ("\u00b7" if _cold else "\u2713") + f" after a reboot: {_boot}")
    elif _kind != "none":
        print(f"  \u00b7 supervisor: not installed \u2014 `clipengine supervisor install` so the autopilot survives "
              "crashes, the agent session ending and reboots")
    else:
        print("  \u00b7 supervisor: no launchd / user systemd here \u2014 `clipengine supervisor install` and the "
              "engine's keeper restarts the autopilot instead")
    if config().get("heartbeat_url"):
        print("  \u2713 off-host heartbeat: pinged every minute (your monitor alerts when pings stop)")
    else:
        print("  \u00b7 off-host heartbeat: off \u2014 a dead or sleeping host can't alert you (optional: "
              "config/ops.json \"heartbeat_url\")")
    try:
        from . import finish as _FN
        _fl = _FN.lines()[0]
        _ok = _FN.check()["state"]
        _st = _FN.check_still() if _ok != "none" else None
        _mark = "\u00b7" if _ok == "none" else ("\u2713" if _ok == "ok" and (not _st or _st.get("same")) else "\u2717")
        print(f"  {_mark} {_fl}" + ("" if not _st else " · reference frame " + ("same on this engine" if _st.get("same")
                                                                              else "DIFFERENT: " + _st.get("why", ""))))
    except Exception as exc:
        print(f"  \u00b7 finish: not checked ({str(exc)[:80]})")
    cdp = config().get("browser_cdp_url")
    from . import handpost as _HP
    if _HP.enabled():
        check("posting: by your own browser (post_with: agent) — clipengine to-post", True)
    elif cdp:
        import urllib.request
        try:
            urllib.request.urlopen(cdp.rstrip("/") + "/json/version", timeout=5).read()
            ok = True
        except Exception:
            ok = False
        check(f"posting browser (your own, at {cdp})", ok, "start it with remote debugging on that port, or remove "
              "browser_cdp_url from config/ops.json to use the engine's own Chrome")
    else:
        check("posting machine (signed-in chrome-profile/)", _posting_machine(),
              "the channel owner signs in once: cd engine && node post.mjs --login (or use your own browser: "
              "docs/troubleshooting.md)")
    if accounts and _HP.enabled():
        from . import verify
        for platform, handle, ok, reason in verify.public_accounts():
            check(f"{platform} public profile @{handle or '(missing)'}", ok, reason)
    elif accounts and _posting_machine():
        r = subprocess.run(["node", "post.mjs", "--check"], cwd=os.path.join(REPO, "engine"), capture_output=True,
                           text=True, timeout=180)
        try:
            res = json.loads(r.stdout.strip().splitlines()[-1])
            for p, v in res.items():
                check(f"signed in: {p}", bool(v.get("ok")), v.get("why", "") + " — owner signs in: node post.mjs --login")
        except Exception:
            check("account check", False, (r.stderr or r.stdout)[-120:])
    from . import twitch as T
    try:
        T.gql("query { game(name: \"Grand Theft Auto VI\") { id } }")
        check("twitch data (gql.twitch.tv)", True)
    except Exception as exc:
        check("twitch data (gql.twitch.tv)", False, str(exc)[:80])
    try:
        import websocket
        from .sentinel import _ws_kwargs
        ws = websocket.create_connection("wss://irc-ws.chat.twitch.tv:443", timeout=10, **_ws_kwargs())
        ws.close()
        check("twitch live chat (irc-ws.chat.twitch.tv)", True)
    except Exception as exc:
        check("twitch live chat (irc-ws.chat.twitch.tv)", False, str(exc)[:80])
    from .publish import creators as _creators
    kicks = [k for k in _creators() if k.startswith("kick:")]
    if kicks:                                       # Kick sources: its API must answer this machine
        from . import kick as K
        st = K.live_status(kicks[0])
        check("kick data (kick.com)", not st.get("error"), (st.get("error") or "")[:120] +
              " — needs kick.com, ws-us2.pusher.com and the *.live-video.net playback hosts; `clipengine kick-check` "
              "says which step fails")
    from . import repo as RP
    if not RP.public() and os.environ.get("CLIPENGINE_NO_SYNC") == "1":
        print("· test run (CLIPENGINE_NO_SYNC=1): nothing is committed or pushed, tracked data isn't rewritten")
    elif RP.sync():
        check("git push access", _can_push(), "gh auth login --web (approve as the repo's GitHub account), "
              "then gh auth setup-git")
    else:
        check("engine updates reachable", subprocess.run(["git", "-C", REPO, "ls-remote", "-q", "origin", "HEAD"],
                                                          capture_output=True, timeout=60).returncode == 0,
              "this GitHub account needs access to the engine's repository — ask whoever invited you")
        check("this copy only pulls (never pushes to the engine)", RP.lock_push(), "bash setup/setup.sh")
    prof = os.path.join(REPO, "chrome-profile")
    if os.path.isdir(prof):
        try:
            os.chmod(prof, 0o700)
        except OSError:
            pass
        check("sign-ins private to this user (chrome-profile/)", (os.stat(prof).st_mode & 0o077) == 0,
              "chmod 700 chrome-profile")
    mc = machine()
    tip = suggest_profile(mc["ram_gb"], mc["cores"], config().get("profile", "full"))
    print(f"! {tip}" if tip else f"✓ machine ({mc['ram_gb']:.0f} GB RAM, {mc['cores']} cores, profile "
          f"{config().get('profile', 'full')})")
    free = _free_gb()
    check(f"disk ({free:.0f} GB free)", free >= config()["min_free_gb"], f"free up space (needs {config()['min_free_gb']} GB)")
    for f in ("radar.json", "grading.json", "distribution.json", "creators.json", "sentinel_model.json"):
        fp = os.path.join(REPO, "config", f)
        if not os.path.exists(fp):
            if f == "creators.json":
                check(f"config/{f}", False, "clipengine init (sets up this channel's streamers)")
            continue                                # the others are optional: built-in defaults apply
        try:
            json.load(open(fp))
            check(f"config/{f}", True)
        except Exception as exc:
            check(f"config/{f}", False, f"not valid JSON ({str(exc)[:60]}) — fix it or delete it to use the defaults")
    print("READY" if ok_all else "NOT READY — fix the ✗ lines, then run doctor again")
    return ok_all
