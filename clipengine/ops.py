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
import os
import shutil
import signal
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS = os.path.join(REPO, "runs")
STATE = os.path.join(RUNS, ".cycle.json")
CONFIG = os.path.join(REPO, "config", "ops.json")
DEFAULTS = {"max_sentinels": 5, "buffer_h": 1.0, "min_free_gb": 15, "stats_every_min": 60,
            "learn_daily_after_utc": 7, "draft_keep_h": 12}


def config() -> dict:
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG):
        cfg.update({k: v for k, v in json.load(open(CONFIG)).items() if not k.startswith("_")})
    return cfg


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


def _detach(cmd: list[str], log: str, cwd: str = REPO) -> int:
    os.makedirs(os.path.dirname(log), exist_ok=True)
    env = {**os.environ, "HF_HUB_DISABLE_XET": "1", "PYTHONUNBUFFERED": "1"}
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


def start(channel: str, buffer_h: float | None = None) -> str:
    d = sentinel_dir(channel)
    st = sentinel_status(d) if os.path.isdir(d) else None
    if st and st["alive"]:
        return f"{st['login']} already running"
    os.makedirs(d, exist_ok=True)
    for f in ("STOPPED",):
        try:
            os.remove(os.path.join(d, f))
        except OSError:
            pass
    open(os.path.join(d, "source"), "w").write(channel)
    buf = buffer_h if buffer_h is not None else config()["buffer_h"]
    pid = _detach([sys.executable, "-m", "clipengine", "sentinel", "--channel", channel, "--out", d,
                   "--buffer", str(buf)], d + ".log")
    open(os.path.join(d, "sentinel.pid"), "w").write(str(pid))
    # a fresh start is "alive" until its first heartbeat is due
    json.dump({"login": os.path.basename(d).replace("sentinel-", ""), "beat": time.time(), "pid": pid,
               "chat_connected": False, "moments": 0}, open(os.path.join(d, "sentinel.json"), "w"))
    return f"+{os.path.basename(d).replace('sentinel-', '')} started"


def stop(channel_or_dir: str, why: str = "stopped") -> str:
    d = channel_or_dir if os.path.isdir(channel_or_dir) else sentinel_dir(channel_or_dir)
    st = sentinel_status(d)
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


# ---- the cycle ---------------------------------------------------------------------------------------

def _posting_machine() -> bool:
    return os.path.isdir(os.path.join(REPO, "chrome-profile"))


def _poster_running() -> bool:
    lock = os.path.join(REPO, ".engine.lock")
    try:
        return _alive(int(open(lock).read().strip()))
    except (OSError, ValueError):
        return False


def _free_gb() -> float:
    return shutil.disk_usage(REPO).free / 1e9


def _prune(cfg: dict) -> list[str]:
    """Free disk: drafts older than draft_keep_h, then recordings of stopped sentinels."""
    out = []
    cutoff = time.time() - cfg["draft_keep_h"] * 3600
    n = 0
    for f in glob.glob(os.path.join(RUNS, "sentinel-*", "drafts", "*")):
        if os.path.getmtime(f) < cutoff:
            try:
                os.remove(f); n += 1
            except OSError:
                pass
    if n:
        out.append(f"removed {n} old draft files")
    if _free_gb() < cfg["min_free_gb"]:
        for st in all_sentinels():
            if st["stopped"] or not st["alive"]:
                seg = os.path.join(st["dir"], "segments")
                if os.path.isdir(seg):
                    shutil.rmtree(seg, ignore_errors=True)
                    try:
                        os.remove(os.path.join(st["dir"], "index.jsonl"))
                    except OSError:
                        pass
                    out.append(f"cleared {st['login']}'s old recording")
    return out


def _git(*a: str) -> bool:
    return subprocess.run(["git", "-C", REPO, *a], capture_output=True).returncode == 0


def cycle(top: int | None = None, radar: bool = True, quiet_queue: bool = False) -> dict:
    from . import sentinel as SN
    cfg = config()
    top = top or cfg["max_sentinels"]
    state = _state()
    lines, notes = [], []

    # 1. sentinels: revive the dead, follow the radar
    sents = {s["login"]: s for s in all_sentinels()}
    for s in sents.values():
        if not s["stopped"] and not s["alive"]:
            notes.append(start(s["source"] or s["login"]).replace("started", "restarted"))
    if radar:
        try:
            from . import radar as R
            res = R.run(None, None, show=top + 3, quiet=True)
            live = {r["login"] for r in res["streamers"]}
            want = [r["login"] for r in res["streamers"][:top + 3]
                    if r.get("action") in ("scan", "record")][:top]
            sents = {s["login"]: s for s in all_sentinels()}          # fresh: includes ones just revived
            running = [l for l, s in sents.items() if s["alive"] and not s["stopped"]]
            for l in running:                                   # stop the ones whose stream ended
                if not l.startswith("yt-") and l not in live:
                    notes.append(stop(l, "stopped (offline)"))
            running = [l for l in running if l in live or l.startswith("yt-")]
            for l in want:
                if l in running:
                    continue
                if len(running) >= top:                         # full: a cooled-off streamer gives up its slot
                    cold = [r for r in running if r not in want and not r.startswith("yt-")]
                    if not cold:
                        break
                    notes.append(stop(cold[-1], "stopped (cooled off)"))
                    running.remove(cold[-1])
                notes.append(start(l)); running.append(l)
            state["radar_at"] = time.time()
            hot = ", ".join(f"{r['login']} {r['score']:.0f}" for r in res["streamers"][:top + 2])
            moments = [m for m in res["moments"] if not m["unsafe"] and not m["published"]][:1]
            lines.append(f"radar: {len(live)} live — top: {hot}" + (
                f" · hottest viewer-clipped moment: {moments[0]['login']} \"{moments[0]['titles'][0][:40]}\" "
                f"({moments[0]['vpm']:.0f} views/min) → `{moments[0]['cut']}`" if moments else ""))
        except Exception as exc:
            notes.append(f"radar failed: {str(exc)[:80]}")
    alive = [s for s in all_sentinels() if s["alive"] and not s["stopped"]]
    lines.append("sentinels: " + (", ".join(
        f"{s['login']} ({s['beat'].get('moments', 0)} moments)" for s in alive) or "none running")
        + (f"  [{'; '.join(notes)}]" if notes else ""))

    # 2. poster
    if _posting_machine():
        if not _poster_running():
            _detach(["node", os.path.join(REPO, "engine", "post.mjs"), "--watch"],
                    os.path.join(REPO, "logs", "poster.log"), cwd=os.path.join(REPO, "engine"))
            lines.append("poster: started (node engine/post.mjs --watch)")
        else:
            lines.append("poster: running")
    else:
        lines.append("poster: not this machine (no signed-in chrome-profile/) — drops are posted by the posting machine")

    # 3. hourly stats, daily learn
    now = time.time()
    if _posting_machine() and now - state.get("stats_at", 0) >= cfg["stats_every_min"] * 60:
        r = subprocess.run(["node", os.path.join(REPO, "engine", "stats.mjs")], capture_output=True, text=True,
                           cwd=os.path.join(REPO, "engine"), timeout=600)
        state["stats_at"] = now
        tail = (r.stdout.strip().splitlines() or [""])[-1]
        lines.append("stats: " + (tail[-90:] if r.returncode == 0 else f"failed — {(r.stderr or tail)[-90:]}"))
    today = dt.datetime.now(dt.timezone.utc)
    if today.hour >= cfg["learn_daily_after_utc"] and state.get("learn_day") != today.strftime("%Y-%m-%d"):
        from . import learn as LN
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            res = LN.run(write=True)
        state["learn_day"] = today.strftime("%Y-%m-%d")
        _git("add", "data/weights.json")
        if _git("-c", "user.name=clipengine", "-c", "user.email=clipengine@users.noreply.github.com",
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
    rows = SN.show_queue() if not quiet_queue else []
    if not rows:
        print("queue: empty — nothing needs you")
    return {"lines": lines, "queue": rows}


# ---- doctor ----------------------------------------------------------------------------------------

def doctor(accounts: bool = False) -> bool:
    ok_all = True

    def check(name: str, ok: bool, fix: str = "") -> None:
        nonlocal ok_all
        ok_all &= ok
        print(f"{'✓' if ok else '✗'} {name}" + ("" if ok else f" — {fix}"))

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
    check("logo", os.path.isfile(B.asset(B.load(fresh=True)["logo"].get("file", ""))),
          "clipengine init makes one (brand/kit/logo.png), or set logo.file in brand/brand.json")
    check("fonts", os.path.exists(os.path.join(REPO, "assets", "fonts", "NotoColorEmoji.ttf")), "git pull (assets/ is in the repo)")
    check("posting machine (signed-in chrome-profile/)", _posting_machine(),
          "the channel owner signs in once: cd engine && node post.mjs --login")
    if accounts and _posting_machine():
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
    check("git push access", subprocess.run(["git", "-C", REPO, "push", "--dry-run", "-q", "origin", "HEAD:refs/heads/_doctor_push_check"],
                         capture_output=True).returncode == 0,
          "this machine needs push access to the repo")
    free = _free_gb()
    check(f"disk ({free:.0f} GB free)", free >= config()["min_free_gb"], f"free up space (needs {config()['min_free_gb']} GB)")
    for f in ("radar.json", "grading.json", "distribution.json", "creators.json", "sentinel_model.json"):
        try:
            json.load(open(os.path.join(REPO, "config", f)))
            check(f"config/{f}", True)
        except Exception as exc:
            check(f"config/{f}", False, str(exc)[:80])
    print("READY" if ok_all else "NOT READY — fix the ✗ lines, then run doctor again")
    return ok_all
