"""Reliability — never lose a moment to plumbing.

  autopilot   a detached background loop that runs `cycle` every N minutes (config/ops.json
              "autopilot_every_min", default 5) with no agent tokens. `--sessions`: only inside the
              roster's usual stream hours (see schedule). Pid in runs/autopilot.pid, log in
              runs/autopilot.log, state in runs/autopilot.json. It re-execs itself when the engine's code
              changes (cycle pulls main), so it always runs the newest version.
  schedule    each roster creator's usual go-live hours, learned from their recent broadcasts (Twitch
              videos: createdAt + lengthSeconds) plus the live sessions cycle has seen itself, and the
              next session window in local time.
  catch-up    when a cycle runs after a gap (> 15 min since the last), the saved streams (VODs) of the
              roster creators are scanned for the minutes nothing was running (chat replay + viewer clips,
              the normal scan), and the best moments are added to the queue as drafts that
              `publish-queued` publishes through the normal VOD path.
  recorder    per-sentinel recording health (segments, bytes, holes > 3 s with their likely cause).
  buffer      the default recording buffer sized from free disk (1–6 h; config/ops.json buffer_h wins).
  policy      config/creators.json per creator "policy": "allowed" (default) | "pools" | "never".
  backup      zip brand/, config/, data/, ledger.jsonl, local/patches; restore one.
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import math
import os
import signal
import subprocess
import sys
import time

from . import attention as AT
from . import supervise as SV

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS = os.path.join(REPO, "runs")
PID = os.path.join(RUNS, "autopilot.pid")
LOG = os.path.join(RUNS, "autopilot.log")
APSTATE = os.path.join(RUNS, "autopilot.json")
SCHEDULE = os.path.join(RUNS, "schedule.json")
CATCHUP_PID = os.path.join(RUNS, "catchup.pid")

CATCHUP_GAP_S = 15 * 60          # a cycle after this long without one scans what was missed
GAP_S = 3.0                      # a recording jump longer than this is a gap
HOLE_ALERT_S = 10.0              # ... and longer than this goes to the attention line
SESSION_SHARE = 0.25             # an hour of the day is "usual" when they were live in it on ≥ 25 % of days
LEAD_MIN = 15                    # start this long before a usual session
GB_PER_H = 2.5                   # what one sentinel's recording costs per hour (source quality)


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def _read_pid(path: str) -> int | None:
    try:
        return int(open(path).read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _json(path: str, default=None):
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return {} if default is None else default


# ---- policy -----------------------------------------------------------------------------------------

def policy(login: str, roster: dict | None = None) -> str:
    """"allowed" (default), "pools" (clipping allowed through the creator's clip pool / program — treated as
    allowed by the engine; the agent follows the pool's rules), or "never" (the creator does not allow
    clipping: no sentinel, listed last in the radar, publish refuses)."""
    if roster is None:
        from .publish import creators
        roster = creators()
    p = str((roster.get((login or "").lower()) or {}).get("policy") or "allowed").lower()
    return p if p in ("allowed", "pools", "never") else "allowed"


def refuse_never(login: str, roster: dict | None = None) -> None:
    if policy(login, roster) == "never":
        raise SystemExit(f"PUBLISH REFUSED: {login} does not allow clipping (config/creators.json policy: never)")


# ---- buffer by disk ---------------------------------------------------------------------------------

def buffer_hours(cfg: dict, free_gb: float, explicit: float | None = None) -> float:
    """Hours of recording each sentinel keeps: config/ops.json buffer_h when it is set there, otherwise
    sized so max_sentinels recordings fit above min_free_gb — between 1 and 6 hours."""
    if explicit is not None:
        return float(explicit)
    per = (free_gb - cfg.get("min_free_gb", 15)) / max(1, cfg.get("max_sentinels", 5))
    top = float(cfg.get("buffer_max_h", 6.0))                   # the lite profile keeps a shorter buffer
    return round(max(1.0, min(top, per / cfg.get("record_gb_per_h", GB_PER_H))), 1)


def explicit_buffer() -> float | None:
    """buffer_h only when the operator wrote it in config/ops.json (the built-in default doesn't count)."""
    v = _json(os.path.join(REPO, "config", "ops.json")).get("buffer_h")
    return float(v) if isinstance(v, (int, float)) else None


def protected_windows(rec_dir: str) -> list[tuple[float, float]]:
    """Wall-clock windows a queued draft (status new) still needs from this recording."""
    latest: dict[str, dict] = {}
    try:
        for line in open(os.path.join(rec_dir, "queue.jsonl")):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            latest[r["id"]] = {**latest.get(r["id"], {}), **r}
    except OSError:
        return []
    out = []
    for r in latest.values():
        if r.get("status") != "new" or "start" not in r or "stop" not in r or r.get("scan"):
            continue
        a, b = r["start"] - 10, r["stop"] + 10
        src = r.get("src") or []                   # the transcribed handles a re-cut may extend into (W4)
        if len(src) == 2:
            a, b = min(a, float(src[0]) - 2), max(b, float(src[1]) + 2)
        out.append((a, b))
    return out


def segment_durations(rec_dir: str) -> dict[str, float]:
    """{segment file: its length in seconds} from index.jsonl (Twitch writes 2 s; YouTube / Kick can be longer)."""
    out = {}
    try:
        for line in open(os.path.join(rec_dir, "index.jsonl")):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("f"):
                out[r["f"]] = float(r.get("d") or 2.0)
    except OSError:
        pass
    return out


def is_protected(t: float, d: float, windows: list[tuple[float, float]]) -> bool:
    return any(t < b and t + d > a for a, b in windows)


# ---- recorder health --------------------------------------------------------------------------------

def gap_cause(fresh_start: bool, ads: int, reconnects: int, failed: int, prev_age_s: float = 0) -> str:
    """The likely reason the recording jumped (pure: the recorder counts what happened since its last
    saved segment)."""
    if fresh_start:
        return ("the stream was offline (or nothing was recording)" if prev_age_s > 1800 else
                "the recorder was restarted (sentinel restart or crash)")
    if ads:
        return "Twitch ad break (the stream is replaced by ads for viewers too)"
    if failed:
        return "segment downloads failed (network to the video host)"
    if reconnects:
        return "the stream dropped and the recorder reconnected"
    return "the stream's own timeline jumped (the streamer's connection)"


def recorder_stats(rec_dir: str, since: float, now: float | None = None) -> dict:
    """Segments, megabytes and gaps (> 3 s) recorded since `since` (the last cycle)."""
    now = now or time.time()
    segs = []
    try:
        for line in open(os.path.join(rec_dir, "index.jsonl")):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if "f" in r and r.get("t", 0) + r.get("d", 0) >= since:
                segs.append(r)
    except OSError:
        pass
    segs.sort(key=lambda r: r["t"])
    size = 0
    for r in segs:
        try:
            size += os.path.getsize(os.path.join(rec_dir, "segments", r["f"]))
        except OSError:
            pass
    gaps = [g for g in _gaps(rec_dir) if g["t"] + g["len"] >= since]
    return {"since": round(since), "segments": len(segs), "mb": round(size / 1e6, 1),
            "gaps": len(gaps), "gap_s": round(sum(g["len"] for g in gaps), 1),
            "last_gap": gaps[-1] if gaps else None,
            "last_segment_age_s": round(now - (segs[-1]["t"] + segs[-1]["d"]), 1) if segs else None}


def _gaps(rec_dir: str) -> list[dict]:
    """The holes the recorder wrote into index.jsonl, as {"t", "len", "cause"}."""
    out = []
    try:
        for line in open(os.path.join(rec_dir, "index.jsonl")):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if "gap" in r:
                out.append({"t": r["t"], "len": r["d"], "cause": r["gap"]})
    except OSError:
        pass
    return out


def holes_since(rec_dir: str, since: float) -> list[dict]:
    """Holes long enough to lose a moment (> 10 s), not counting ad breaks (unavoidable: the stream itself
    shows ads), that started after `since`."""
    return [g for g in _gaps(rec_dir) if g["t"] > since and g["len"] > HOLE_ALERT_S
            and not str(g.get("cause", "")).startswith("Twitch ad break")]


def _hm(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%H:%M:%S UTC")


def show_sentinels() -> list[dict]:
    """`clipengine sentinels`: the health lines (tier 2: full sentinels), plus one recording line each."""
    from . import sentinel as SN
    if any(os.path.isdir(d) for d in glob.glob(os.path.join(RUNS, "sentinel-*"))):
        try:
            from . import ops as _O
            _st = _O._state()
            _cap = _st.get("cap") or _O.config()["max_sentinels"]
            _why = _st.get("cap_why") or "steady"
        except Exception:
            _cap, _why = "?", ""
        print(f"tier 2  full sentinels (recording + audio + viewer clips) — up to {_cap} on this machine"
              + (f" ({_why})" if _why and _why != "steady" else "") + ":")
    rows = SN.health()
    since = _json(os.path.join(RUNS, ".cycle.json")).get("cycle_at") or time.time() - 600
    for s in rows:
        st = recorder_stats(s["dir"], since)
        g = st["last_gap"]
        print(f"      {s.get('login', '?'):<16} recording since last cycle: {st['segments']} segments, {st['mb']:.0f} MB, "
              f"{st['gaps']} gap(s)" + (f" ({st['gap_s']:.0f} s)" if st['gaps'] else "")
              + (f" — last {g['len']:.0f} s at {_hm(g['t'])}: {g.get('cause', '?')}" if g else ""))
    return rows


# ---- schedule ---------------------------------------------------------------------------------------

_VIDEOS = """query($login: String!) { user(login: $login) { stream { createdAt }
  videos(first: 30, type: ARCHIVE, sort: TIME) { edges { node { createdAt lengthSeconds } } } } }"""


def _epoch(iso: str) -> float:
    return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def usual_hours(sessions: list[tuple[float, float]], tz=None, now: float | None = None,
                share: float = SESSION_SHARE) -> dict:
    """From past sessions [(start, end) epoch], the hours of the local day they are usually live
    (pure). Returns {"days": n, "sessions": k, "share": {hour: fraction of days}, "windows": [[h0, h1, f]]}
    — a window runs from hour h0 to h1 (exclusive, may wrap past midnight), f = its average share."""
    now = now or time.time()
    tz = tz or dt.datetime.now().astimezone().tzinfo
    if not sessions:
        return {"days": 0, "sessions": 0, "share": {}, "windows": []}
    first = min(s for s, _ in sessions)
    days = max(1, min(30, round((now - first) / 86400 + 0.5)))
    hits: dict[int, set] = {h: set() for h in range(24)}
    for s, e in sessions:
        t = s - s % 3600
        while t < e:
            if t + 3600 > s:
                d = dt.datetime.fromtimestamp(t, tz)
                hits[d.hour].add(d.date())
            t += 3600
    sh = {h: min(1.0, len(v) / days) for h, v in hits.items()}
    on = [h for h in range(24) if sh[h] >= share]
    windows: list[list] = []
    if on and len(on) == 24:
        windows = [[0, 24, round(sum(sh.values()) / 24, 2)]]
    elif on:
        start = next(h for h in on if (h - 1) % 24 not in on)
        h, run = start, []
        for _ in range(24):
            if h in on:
                run.append(h)
            elif run:
                windows.append([run[0], (run[-1] + 1) % 24 or 24, round(sum(sh[x] for x in run) / len(run), 2)])
                run = []
            h = (h + 1) % 24
        if run:
            windows.append([run[0], (run[-1] + 1) % 24 or 24, round(sum(sh[x] for x in run) / len(run), 2)])
    return {"days": days, "sessions": len(sessions), "share": {h: round(v, 2) for h, v in sh.items() if v},
            "windows": windows}


def _in_window(hour_f: float, w: list) -> bool:
    a, b = w[0], w[1]
    return a <= hour_f < b if a < b else (hour_f >= a or hour_f < b)


def next_start(w: list, now: float, tz=None) -> float:
    """Epoch of the next time window w starts (now, if we're inside it)."""
    tz = tz or dt.datetime.now().astimezone().tzinfo
    d = dt.datetime.fromtimestamp(now, tz)
    if _in_window(d.hour + d.minute / 60, w):
        return now
    cand = d.replace(hour=w[0] % 24, minute=0, second=0, microsecond=0)
    if cand.timestamp() <= now:
        cand += dt.timedelta(days=1)
    return cand.timestamp()


def in_session(sched: dict, now: float | None = None, tz=None, lead_min: float = LEAD_MIN,
               tail_min: float = 30) -> bool:
    """Inside any roster creator's usual hours (from `lead_min` before to `tail_min` after). With nothing
    learned yet, always True: unknown hours must never mean "don't run"."""
    now = now or time.time()
    tz = tz or dt.datetime.now().astimezone().tzinfo
    windows = [w for c in (sched.get("creators") or {}).values() for w in c.get("windows", [])]
    if not windows:
        return True
    for off in (0, lead_min * 60, -tail_min * 60):
        d = dt.datetime.fromtimestamp(now + off, tz)
        if any(_in_window(d.hour + d.minute / 60, w) for w in windows):
            return True
    return False


def learn_schedule(now: float | None = None, write: bool = True) -> dict:
    from . import twitch as T
    from .publish import creators
    now = now or time.time()
    roster = creators()
    seen = _json(os.path.join(RUNS, ".cycle.json")).get("seen_live") or {}
    out = {"at": now, "creators": {}}
    for login, c in roster.items():
        if policy(login, roster) == "never" or login.startswith("yt"):
            continue
        sessions = []
        try:
            u = T.gql(_VIDEOS, {"login": login}).get("user") or {}
            for e in ((u.get("videos") or {}).get("edges") or []):
                n = e["node"]
                s = _epoch(n["createdAt"])
                if now - s < 30 * 86400:
                    sessions.append((s, s + float(n.get("lengthSeconds") or 0)))
            if u.get("stream") and u["stream"].get("createdAt") and not sessions:
                sessions.append((_epoch(u["stream"]["createdAt"]), now))
        except Exception as exc:
            out["creators"][login] = {"error": str(exc)[:80], "windows": []}
            continue
        for s, e in seen.get(login, []):              # sessions cycle saw itself (live-only creators)
            if not any(a - 600 <= s <= b for a, b in sessions):
                sessions.append((s, e))
        out["creators"][login] = {"name": c.get("name", login), **usual_hours(sessions, now=now)}
    if write:
        os.makedirs(RUNS, exist_ok=True)
        json.dump(out, open(SCHEDULE, "w"), indent=1)
    return out


def load_schedule(max_age_h: float = 12) -> dict:
    s = _json(SCHEDULE)
    if s and time.time() - s.get("at", 0) < max_age_h * 3600:
        return s
    try:
        return learn_schedule()
    except Exception:
        return s or {}


def show_schedule(refresh: bool = True) -> dict:
    s = learn_schedule() if refresh else load_schedule()
    now = time.time()
    tz = dt.datetime.now().astimezone().tzinfo
    rows = []
    for login, c in (s.get("creators") or {}).items():
        if c.get("error"):
            rows.append((1e18, f"{login:<16} couldn't read their broadcasts ({c['error']})"))
            continue
        if not c.get("windows"):
            rows.append((1e18 - 1, f"{login:<16} no usual hours yet ({c.get('sessions', 0)} broadcasts in "
                                    f"{c.get('days', 0)} days)"))
            continue
        nxt = min(c["windows"], key=lambda w: next_start(w, now, tz))
        at = next_start(nxt, now, tz)
        hours = ", ".join(f"{w[0]:02d}:00–{w[1] % 24:02d}:00 ({w[2] * 100:.0f}% of days)" for w in c["windows"])
        when = "NOW (usual hours)" if at <= now else \
            f"next {dt.datetime.fromtimestamp(at, tz):%a %H:%M} — start by {dt.datetime.fromtimestamp(at - LEAD_MIN * 60, tz):%H:%M}"
        rows.append((at, f"{login:<16} usually live {hours} · {when}"))
    tzname = dt.datetime.now().astimezone().strftime("%Z")
    print(f"SCHEDULE (local time, {tzname}) — learned from the last 30 days of broadcasts")
    for _, r in sorted(rows):
        print("  " + r)
    if not rows:
        print("  no roster creators (config/creators.json)")
    print("  autopilot only inside these hours: clipengine autopilot start --sessions")
    return s


# ---- catch-up ---------------------------------------------------------------------------------------

def catchup_targets(roster: dict, sentinel_cover: dict | None = None, restricted: set[str] | None = None) -> list[str]:
    """Roster creators whose saved streams can be scanned (VODs on and not subscriber-only, clipping allowed)."""
    from . import vodaccess as VA
    restricted = VA.restricted_set() if restricted is None else restricted
    return [l for l, c in roster.items() if c.get("vods") is not False and policy(l, roster) != "never"
            and not l.startswith("yt") and l.lower() not in restricted]


COVERAGE = os.path.join(RUNS, "coverage.json")
MAX_CATCHUP_H = 12.0


def _coverage() -> str:
    """runs/coverage.json under the CURRENT runs folder (tests and other roots move RUNS)."""
    return os.path.join(RUNS, "coverage.json")


def catchup_window(since_arg, until: float, sweep: bool = False, path: str | None = None) -> tuple[float, str]:
    """(since, how): an explicit --since, else the last completed coverage watermark (bounded to 12 h). No
    watermark and no --since is an error — never a silent guess."""
    if since_arg not in (None, ""):
        since = float(since_arg)
        how = "from --since"
    else:
        mark = _json(path or _coverage()).get("sweep_until" if sweep else "catchup_until")
        if not mark:
            raise ValueError("catchup: no coverage watermark yet — give --since (unix seconds) for the first run")
        since = max(float(mark), until - MAX_CATCHUP_H * 3600)
        how = "from the last completed coverage" + (" (capped at 12 h)" if since > float(mark) else "")
    if since >= until:
        raise ValueError(f"catchup: the window is empty (since {since:.0f} ≥ until {until:.0f}) — nothing to scan")
    if until - since > MAX_CATCHUP_H * 3600 * 2:
        raise ValueError(f"catchup: a {(until - since) / 3600:.0f} h window is too long — give a --since within 24 h")
    return since, how


def behind(sweep: bool = False, path: str | None = None) -> dict[str, float]:
    """Creators whose own coverage is behind the shared mark (their source failed): {login: covered up to}."""
    st = _json(path or _coverage())
    return {k: float(v) for k, v in ((st.get("by_creator") or {}).get("sweep" if sweep else "catchup") or {}).items()}


def mark_batch(since: float, until: float, outcome: dict, logins: list[str], sweep: bool = False,
               path: str | None = None) -> dict:
    """After a batch: the shared mark moves to `until`; a creator whose source failed keeps its OWN mark at the start
    of its first failed window, so only that creator is re-scanned next time. (Nov 22: one chronically failing creator
    held the single shared mark, and every sweep re-scanned the same window — "scanned from saved streams 0".)"""
    path = path or _coverage()
    st = _json(path)
    key, kind = ("sweep_until", "sweep") if sweep else ("catchup_until", "catchup")
    st[key] = max(float(st.get(key) or 0), until)
    by = st.setdefault("by_creator", {}).setdefault(kind, {})
    failed: dict[str, float] = {}
    for f in outcome.get("failed", []):
        failed[f["login"]] = min(failed.get(f["login"], until), max(since, float(f.get("from") or since)))
    for login in logins:
        if login in failed:
            by[login] = min(float(by.get(login, failed[login])), failed[login])
        else:
            by.pop(login, None)                            # caught up: the shared mark speaks for it again
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(st, open(path, "w"))
    return {"until": st[key], "behind": dict(by)}


def mark_covered(until: float, sweep: bool = False, path: str | None = None) -> None:
    path = path or _coverage()
    st = _json(path)
    key = "sweep_until" if sweep else "catchup_until"
    st[key] = max(float(st.get(key) or 0), until)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(st, open(path, "w"))


def maybe_catchup(state: dict, now: float, cfg: dict) -> str | None:
    """cycle: after a gap, start a background catch-up scan of the missed window (one line, or None)."""
    last = state.get("cycle_at")
    if not last or now - last <= cfg.get("catchup_gap_min", CATCHUP_GAP_S / 60) * 60:
        return None
    if _alive(_read_pid(CATCHUP_PID)):
        return "catch-up: still scanning the last gap"
    from .ops import _detach
    since = max(last, now - cfg.get("catchup_max_h", 6) * 3600)
    pid = _detach([sys.executable, "-m", "clipengine", "catchup", "--since", str(int(since - 120)),
                   "--until", str(int(now))], os.path.join(RUNS, "catchup.log"))
    os.makedirs(RUNS, exist_ok=True)
    open(CATCHUP_PID, "w").write(str(pid))
    return (f"catch-up: nothing ran for {(now - last) / 60:.0f} min — scanning the saved streams of that time "
            "in the background; moments land in the queue")


def vods_overlapping(login: str, since: float, until: float) -> list[dict]:
    from . import twitch as T
    u = T.gql(T._CHANNEL_VIDEOS, {"login": login}).get("user") or {}
    out = []
    for e in ((u.get("videos") or {}).get("edges") or []):
        n = e["node"]
        s = _epoch(n["createdAt"])
        e_ = s + float(n.get("lengthSeconds") or 0)
        if e_ > since and s < until:
            out.append({"id": n["id"], "created": s, "length_s": float(n.get("lengthSeconds") or 0),
                        "a": max(0.0, since - s), "b": min(e_, until) - s})
    return out


def _queue_rows(root: str = RUNS) -> list[dict]:
    from .sentinel import load_queue
    try:
        return load_queue(root)
    except Exception:
        return []


def _overlaps_any(login: str, s: float, e: float, rows: list[dict]) -> bool:
    for r in rows:
        if r.get("login") == login and "start" in r and "stop" in r and r.get("status") != "dropped":
            inter = min(e, r["stop"]) - max(s, r["start"])
            if inter > 0.5 * min(e - s, r["stop"] - r["start"]):
                return True
    return False


def queue_entry_from_candidate(c: dict, vod: dict, scan_dir: str, login: str, now: float | None = None) -> dict | None:
    """A probed scan candidate as a queue line `publish-queued` can publish (pure but for the grade config)."""
    from . import grade as G
    from . import judge as JG
    if not c.get("probed") or c.get("hard_fail"):
        return None
    created = _epoch(vod["created_at"])
    w = c.get("window") or {"start_s": c["clip_start_s"], "end_s": c["clip_end_s"]}
    from . import visual as VS
    seen = VS.assess_images(VS.thumb_images(c.get("thumbs"))) if c.get("thumbs") else {}
    if seen.get("flags"):
        c = {**c, "flags": (c.get("flags") or []) + seen["flags"]}
    seen.pop("flags", None)
    sig = G.signals_from_candidate(c, vod, dt.datetime.fromtimestamp(now or time.time(), dt.timezone.utc))
    eng = G.engine_score(sig)
    best = G.banger(eng["score"], None)["best_possible"]
    s, e = created + w["start_s"], created + w["end_s"]
    crowd = c.get("crowd") or {}
    return {"id": f"{login}-v{dt.datetime.fromtimestamp(s, dt.timezone.utc):%H%M%S}", "login": login,
            "rec": os.path.dirname(scan_dir), "start": round(s, 1), "stop": round(e, 1),
            "at": dt.datetime.fromtimestamp(created + c["peak_s"], dt.timezone.utc).isoformat(),
            "drafted_at": dt.datetime.now(dt.timezone.utc).isoformat(), "p": None,
            "engine": eng["score"], "best": best, "flags": c.get("flags") or [], "unsafe": sig.get("unsafe") or "",
            "chat": c.get("top_text", "") or (f"🔥 {crowd.get('views')} viewer-clip views" if crowd else ""),
            "said": (c.get("transcript_text") or "")[:140], "sheet": None, "status": "new",
            "via": "catch-up (saved stream)" + (" 🔥" if crowd else ""),
            "scan": scan_dir, "pick": c["rank"], "vod": vod["id"],
            "vod_start": w["start_s"], "vod_stop": w["end_s"], "thumbs": c.get("thumbs") or [],
            "liveliness": c.get("liveliness"), "frames_assessed": c.get("liveliness") is not None,
            "judgment": JG.score_snapshot(eng, sig), "titles": sig["titles"], **({"visual": seen} if seen else {})}


def _sheet(thumbs: list[str], out: str) -> str | None:
    """Join the scan's thumbnails into one contact sheet (what `queue` points the agent at)."""
    try:
        from PIL import Image
        import base64
        import io
        ims = []
        for t in thumbs:                              # the scan keeps thumbnails as data: URIs
            if t and t.startswith("data:image"):
                ims.append(Image.open(io.BytesIO(base64.b64decode(t.split(",", 1)[1]))).convert("RGB"))
            elif t and os.path.isfile(t):
                ims.append(Image.open(t).convert("RGB"))
        if not ims:
            return None
        h = 270
        ims = [im.resize((max(1, int(im.width * h / im.height)), h)) for im in ims]
        sheet = Image.new("RGB", (sum(i.width for i in ims), h))
        x = 0
        for im in ims:
            sheet.paste(im, (x, 0))
            x += im.width
        sheet.save(out, quality=85)
        return out
    except Exception:
        return None


def plan_batch(since: float, until: float, logins: list[str] | None = None, roster: dict | None = None,
               lister=None, behind_: dict[str, float] | None = None) -> dict:
    """Validate a catch-up batch BEFORE anything is scanned or queued (spec §2): every target is a known, allowed
    creator, and each one's saved streams in the window are listed now. Returns {"items": [{login, vod...}],
    "targets": {login: status}, "invalid": [why...]}. status: "ok" (N windows) · "no broadcast in the window" ·
    "subscriber-only" · "kick" (listed at scan time) · "source failed: …" (listing failed — counts as uncovered)."""
    from . import vodaccess as VA
    if roster is None:
        from .publish import creators
        roster = creators()
    lister = lister or vods_overlapping
    invalid = []
    if logins:
        for l in logins:
            if l not in roster:
                invalid.append(f"{l}: not on the roster (config/creators.json)")
            elif policy(l, roster) == "never":
                invalid.append(f"{l}: clip policy is \"never\"")
            elif l.startswith("yt"):
                invalid.append(f"{l}: YouTube has no saved-stream catch-up")
    targets = list(logins) if logins else catchup_targets(roster)
    items, status = [], {}
    if invalid:
        return {"items": [], "targets": {}, "invalid": invalid}
    for login in targets:
        if login.startswith("kick:"):
            status[login] = "kick"
            items.append({"login": login, "kick": True})
            continue
        if VA.restricted(login):
            status[login] = "subscriber-only"
            continue
        try:
            own = max(min(since, (behind_ or {}).get(login, since)), until - MAX_CATCHUP_H * 3600)
            vods = [v for v in lister(login, own, until) if v["b"] - v["a"] >= 60]   # a creator behind: from its mark
        except Exception as exc:
            status[login] = f"source failed: couldn't list broadcasts ({str(exc)[:60]})"
            continue
        status[login] = f"ok ({len(vods)} window(s))" if vods else "no broadcast in the window"
        items += [{"login": login, "vod": v} for v in vods]
    return {"items": items, "targets": status, "invalid": []}


BATCHES = "batches.jsonl"


def journal(rec: dict, root: str | None = None) -> None:
    """One line per finished batch in runs/heavy/batches.jsonl (`jobs` shows the last ones)."""
    from .admission import _dir
    d = _dir(root)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, BATCHES), "a") as fh:
        fh.write(json.dumps(rec) + "\n")


def batches(root: str | None = None, n: int = 5) -> list[dict]:
    from .admission import _dir
    try:
        rows = [json.loads(l) for l in open(os.path.join(_dir(root), BATCHES)) if l.strip()]
    except (OSError, ValueError):
        return []
    return rows[-n:]


def covered_until(since: float, until: float, outcome: dict) -> float:
    """How far the watermark may move: to `until` when every window was covered, else to the start of the
    earliest window whose source failed (the next run re-covers it; queue dedupe stops double drafts)."""
    failed = [f["from"] for f in outcome.get("failed", [])]
    return min([until] + [max(since, x) for x in failed])


def catchup(since: float, until: float, logins: list[str] | None = None, keep: int = 3, probe: int = 6,
            via: str | None = None, outcome: dict | None = None, plan: dict | None = None) -> list[dict]:
    """Scan the saved streams of [since, until] and queue the best moments. Returns the queued entries.
    `outcome` (a dict) is filled with what happened to each window: queued / nothing strong / source failed."""
    from . import pipeline as P
    from . import vodaccess as VA
    out = outcome if outcome is not None else {}
    out.setdefault("windows", []); out.setdefault("failed", [])
    plan = plan or plan_batch(since, until, logins)
    if plan["invalid"]:
        raise ValueError("catchup: " + "; ".join(plan["invalid"]))
    for login, st in plan["targets"].items():
        if st.startswith("source failed"):
            out["failed"].append({"login": login, "from": since, "why": st})
            AT.event("failure", f"catch-up couldn't list {login}'s broadcasts ({st[30:90]})",
                     key=f"catchup:{login}:{int(since)}")
    queued = []
    print(f"[catchup] {_hm(since)} → {_hm(until)}: {', '.join(plan['targets']) or 'no roster creators'}", flush=True)
    skip: set[str] = set()
    for it in plan["items"]:
        login = it["login"]
        if login in skip:
            continue
        if it.get("kick"):                                   # Kick: viewers' clips + the saved broadcast (kick.py)
            try:
                from . import kick as K
                got = K.catchup(login, since, until, keep=keep, via=via or "catch-up (saved stream)")
                queued += got
                out["windows"].append({"login": login, "result": f"queued {len(got)}" if got else "nothing strong"})
            except Exception as exc:
                out["failed"].append({"login": login, "from": since, "why": f"source failed: {str(exc)[:80]}"})
                print(f"[catchup] {login}: {str(exc)[:100]}", flush=True)
            continue
        v = it["vod"]
        d = os.path.join(RUNS, f"catchup-{login}")
        scan_dir = os.path.join(d, f"scan-{v['id']}-{int(v['a'])}")
        try:
            res = P.scan(v["id"], scan_dir, v["a"], v["b"], top=12, probe=probe)
        except Exception as exc:
            msg = str(exc)
            if VA.is_restricted_error(exc):        # subscriber-only VODs: remember, skip quietly from now on
                VA.mark(login, v["id"])
                print(f"[catchup] {login}: saved streams are subscriber-only — skipped from now on "
                      "(a full sentinel records them live)", flush=True)
                out["windows"].append({"login": login, "vod": v["id"], "result": "subscriber-only"})
                skip.add(login)
                continue
            if "no chat" in msg:                   # a real answer: nobody chatted — nothing to find
                out["windows"].append({"login": login, "vod": v["id"], "result": "nothing strong (no chat)"})
            else:
                AT.event("failure", f"catch-up scan of {login} failed ({msg[:60]})", key=f"catchup:{login}:{v['id']}")
                out["failed"].append({"login": login, "vod": v["id"], "from": v["created"] + v["a"],
                                      "why": f"source failed: {msg[:80]}"})
            print(f"[catchup] {login} {v['id']}: {msg[:100]}", flush=True)
            continue
        rows = _queue_rows()
        entries = []
        for c in res["candidates"]:
            q = queue_entry_from_candidate(c, res["vod"], scan_dir, login)
            if q and not q["unsafe"] and not _overlaps_any(login, q["start"], q["stop"], rows + entries):
                entries.append(q)
        entries.sort(key=lambda q: -q["best"])
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "queue.jsonl"), "a") as fh:
            for q in entries[:keep]:
                if via:
                    q["via"] = via + (" 🔥" if q["via"].endswith("🔥") else "")
                q["sheet"] = _sheet(q.pop("thumbs"), os.path.join(scan_dir, f"sheet-{q['pick']}.jpg"))
                fh.write(json.dumps(q) + "\n")
                queued.append(q)
                print(f"[catchup] queued {q['id']}: best {q['best']} {q['via']}", flush=True)
        n = min(len(entries), keep)
        out["windows"].append({"login": login, "vod": v["id"],
                               "result": f"queued {n}" if n else
                               f"nothing strong ({len(res['candidates'])} candidate(s), none new and safe)"})
    print(f"[catchup] done: {summary(out, len(queued))}", flush=True)
    return queued


def summary(outcome: dict, queued: int) -> str:
    """One line that never confuses "found nothing" with "couldn't look"."""
    w = outcome.get("windows", [])
    empty = sum(1 for x in w if x["result"].startswith("nothing"))
    fail = outcome.get("failed", [])
    parts = [f"{queued} moment(s) queued from {len(w)} window(s)"]
    if empty:
        parts.append(f"{empty} with nothing strong")
    if fail:
        parts.append(f"{len(fail)} NOT covered (source failed: {', '.join(sorted({f['login'] for f in fail}))}) — "
                     "the watermark stays before them, the next run retries")
    return " · ".join(parts)


# ---- the sweep: every 30 min, what the watchers missed ------------------------------------------------

SWEEP_EVERY_MIN = 30
SWEEP_LAG_S = 5 * 60              # the VOD's chat replay trails live by a few minutes


def maybe_sweep(state: dict, now: float, cfg: dict) -> str | None:
    """cycle: every `sweep_every_min` (30), scan the roster's recent saved streams in the background —
    chat + viewer clips only, no download — for moments the watchers missed. Off with "sweep": false."""
    if cfg.get("sweep") is False:
        return None
    every = float(cfg.get("sweep_every_min", SWEEP_EVERY_MIN)) * 60
    if now - state.get("sweep_at", 0) < every:
        return None
    if _alive(_read_pid(CATCHUP_PID)):
        return None                                   # one catch-up at a time: next cycle
    if not state.get("any_live", True) and state.get("sweep_until"):
        since_live = max((s[1] for v in (state.get("seen_live") or {}).values() for s in v), default=0)
        if since_live < state["sweep_until"]:
            state["sweep_at"] = now                   # nobody streamed since the last sweep
            return None
    from .ops import _detach
    until = now - SWEEP_LAG_S
    mark = _json(_coverage()).get("sweep_until") or state.get("sweep_until")      # completed coverage, not launches
    since = max(mark or until - every - 600, until - 3 * 3600)
    if until - since < 120:
        return None
    pid = _detach([sys.executable, "-m", "clipengine", "catchup", "--sweep", "--since", str(int(since)),
                   "--until", str(int(until))], os.path.join(RUNS, "catchup.log"))
    os.makedirs(RUNS, exist_ok=True)
    open(CATCHUP_PID, "w").write(str(pid))
    state["sweep_at"], state["sweep_until"] = now, until
    return None                                       # silent: what it finds lands in the queue


def light_moments(msgs: list[dict], crowd: list[dict], length: float = 62.0) -> list[dict]:
    """Moments in a VOD stretch from chat + viewer clips alone (pure): [{peak_s, start_s, end_s,
    chat_score, crowd}] — no audio, no video."""
    from . import rank as R
    cands, rows = R.find_candidates(msgs, top=12, length=length) if msgs else ([], [])
    out = [{"peak_s": c["peak_s"], "start_s": c["clip_start_s"], "end_s": c["clip_end_s"],
            "chat_score": c["chat_score"], "crowd": None} for c in cands]
    for m in crowd:
        hit = next((c for c in out if m["start"] - 20 <= c["peak_s"] <= m["end"] + 30), None)
        if hit:
            hit["crowd"] = m
            continue
        peak = m["end"] - 3
        out.append({"peak_s": peak, "start_s": max(0.0, peak - 30), "end_s": peak + 32, "chat_score": 0.0, "crowd": m})
    return out


def sweep_pick(cands: list[dict], vod_id: str, created: float, login: str, rows: list[dict], cfg: dict,
               clipped=None) -> list[dict]:
    """The strong ones not drafted or published yet, strongest first (pure but for `clipped`)."""
    from .publish import already_clipped
    clipped = clipped or already_clipped
    min_views = float(cfg.get("sweep_min_views", 25))
    min_chat = float(cfg.get("sweep_min_chat", 6.0))
    out = []
    for c in cands:
        views = (c.get("crowd") or {}).get("views", 0)
        if views < min_views and c["chat_score"] < min_chat:
            continue
        ws, we = created + c["start_s"], created + c["end_s"]
        if _overlaps_any(login, ws, we, rows) or clipped(vod_id, c["start_s"], c["end_s"]) or clipped(login, ws, we):
            continue
        out.append({**c, "wall": [ws, we], "strength": c["chat_score"] + (6.0 * math.log10(1 + views))})
    out.sort(key=lambda c: -c["strength"])
    return out[: int(cfg.get("sweep_keep", 2))]


def sweep(since: float, until: float, logins: list[str] | None = None, outcome: dict | None = None,
          behind_: dict[str, float] | None = None) -> list[dict]:
    """Scan the roster's saved streams of [since, until] cheaply (chat + viewer clips) and queue the strong
    moments nobody drafted, through the catch-up path. Returns the queued entries; `outcome` as catchup()."""
    out = outcome if outcome is not None else {}
    out.setdefault("windows", []); out.setdefault("failed", [])
    from . import twitch as T
    from .ops import config
    from .publish import creators
    cfg = config()
    roster = creators()
    targets = logins or catchup_targets(roster)
    queued = []
    print(f"[sweep] {_hm(since)} → {_hm(until)}: {len(targets)} creator(s)", flush=True)
    for login in targets:
        if login.startswith("kick:"):                        # Kick: no chat replay — its viewers' clips are the scan
            try:
                from . import kick as K
                got = K.catchup(login, since, until, keep=int(cfg.get("sweep_keep", 2)),
                                min_views=int(cfg.get("sweep_min_views", 25)), via="catch-up (sweep)")
                print(f"[sweep] {login}: {len(got)} viewer-clipped moment(s) drafted", flush=True)
                queued += got
            except Exception as exc:
                out["failed"].append({"login": login, "from": since, "why": f"source failed: {str(exc)[:80]}"})
                print(f"[sweep] {login}: couldn't read Kick ({str(exc)[:80]})", flush=True)
            continue
        try:
            vods = vods_overlapping(login, max(min(since, (behind_ or {}).get(login, since)), until - 3 * 3600), until)
        except Exception as exc:
            out["failed"].append({"login": login, "from": since, "why": f"source failed: {str(exc)[:80]}"})
            print(f"[sweep] {login}: couldn't list broadcasts ({str(exc)[:80]})", flush=True)
            continue
        for v in vods:
            if v["b"] - v["a"] < 60:
                continue
            try:
                vod = T.Vod.fetch(v["id"])
                msgs = T.download_chat(v["id"], v["a"], v["b"])
                try:
                    crowd = T.crowd_moments(vod, v["a"], v["b"])
                except Exception:
                    crowd = []
            except Exception as exc:
                out["failed"].append({"login": login, "vod": v["id"], "from": v["created"] + v["a"],
                                      "why": f"source failed: {str(exc)[:80]}"})
                print(f"[sweep] {login} {v['id']}: {str(exc)[:100]}", flush=True)
                continue
            picks = sweep_pick(light_moments(msgs, crowd), v["id"], v["created"], login, _queue_rows(), cfg)
            if not picks:
                out["windows"].append({"login": login, "vod": v["id"], "result": "nothing strong"})
            print(f"[sweep] {login} {v['id']}: {len(msgs)} messages, {len(crowd)} viewer-clipped moment(s) → "
                  f"{len(picks)} strong and new", flush=True)
            for c in picks:
                queued += catchup(c["wall"][0] - 30, c["wall"][1] + 30, [login], keep=1, probe=2,
                                  via="catch-up (sweep)", outcome=out)
    print(f"[sweep] done: {summary(out, len(queued))}", flush=True)
    return queued


def catchup_spikes(spikes: list[dict], pad_before: float = 60.0, pad_after: float = 60.0) -> list[dict]:
    """Tier-1 chat spikes no sentinel covered: draft each from the saved stream, the best moment in its window."""
    out = []
    for sp in spikes:
        print(f"[catchup] tier-1 spike on {sp['login']} at {_hm(sp['fire'])} (p {sp.get('p', 0):.2f})", flush=True)
        if sp["login"].startswith("kick:"):
            from . import kick as K
            try:
                out += K.catchup_spike(sp)
            except Exception as exc:
                print(f"[catchup] {sp['login']}: {str(exc)[:100]}", flush=True)
            continue
        out += catchup(sp["start"] - pad_before, sp["stop"] + pad_after, [sp["login"]], keep=1, probe=3,
                       via="catch-up (tier-1 chat spike)")
    return out


def publish_window_args(entry: dict, trim_start: float | None, trim_end: float | None) -> dict:
    """publish-queued for a catch-up (VOD) draft: the scan + pick, with the trims as VOD times (pure)."""
    a = entry["vod_start"] + (trim_start or 0)
    b = entry["vod_stop"] - (trim_end or 0)
    kw = {"scan_dir": entry["scan"], "pick": entry["pick"]}
    if trim_start or trim_end:
        kw.update(start=a, end=b)
    return kw


# ---- autopilot --------------------------------------------------------------------------------------

def _code_stamp() -> float:
    """The autopilot loop's OWN code. Each cycle already runs as a fresh process on whatever code is there,
    so only a change to the loop itself needs the autopilot to restart."""
    files = [os.path.join(REPO, "clipengine", f) for f in ("autopilot.py", "attention.py", "__main__.py")]
    return max((os.path.getmtime(f) for f in files if os.path.exists(f)), default=0)


def _new_code_starts() -> tuple[bool, str]:
    """Before the loop replaces itself: does the new code import and parse its own command line?"""
    try:
        r = subprocess.run([sys.executable, "-m", "clipengine", "autopilot", "status"], cwd=REPO,
                           capture_output=True, text=True, timeout=120)
        return r.returncode == 0, ((r.stderr or r.stdout).strip().splitlines() or [""])[-1][:120]
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)[:120]


def _root() -> str:
    return os.path.dirname(APSTATE)


def _leader() -> dict | None:
    """The live capture leader: whoever holds the lease (a pid file alone proves nothing — pids get reused).
    A loop from before the lease existed counts by its pid until it restarts onto this code."""
    lead = SV.holder(_root())
    if lead:
        return lead
    pid = _read_pid(PID)
    if not SV.ever_started(_root()) and _alive(pid):
        return {"pid": pid, "legacy": True}
    return None


def autopilot_status() -> dict:
    st = _json(APSTATE)
    lead = _leader()
    st["running"] = lead is not None
    st["pid"] = (lead or {}).get("pid") or _read_pid(PID)
    return st


def autopilot_start(every_min: float | None = None, sessions: bool = False, by: str = "manual") -> str:
    from .ops import config, _detach
    from . import migrate as MG
    ok, why = MG.may_lead()
    if not ok:
        return why
    st = autopilot_status()
    if st["running"]:
        return f"autopilot already running (pid {st['pid']}) — `clipengine autopilot stop` first to change it"
    SV.clear_stopped(_root())
    if SV.installed() and SV.service_start():
        return "autopilot started by its service (`clipengine supervisor status`)"
    every = float(every_min or config().get("autopilot_every_min", 5))
    if SV.settings(_root()):                               # installed, but no reachable service: the keeper runs it
        if every_min or sessions:
            SV.save_settings(_root(), every=every, sessions=bool(sessions))
        return SV.boot(_root())
    cmd = [sys.executable, "-m", "clipengine", "autopilot", "run", "--every", str(every)] + (["--sessions"] if sessions else [])
    pid = _detach(cmd, LOG, env={"CLIPENGINE_STARTED_BY": by})
    os.makedirs(RUNS, exist_ok=True)
    open(PID, "w").write(str(pid))
    return (f"autopilot started (pid {pid}): cycle every {every:g} min"
            + (" inside the usual stream hours (clipengine schedule)" if sessions else "")
            + f" — log {os.path.relpath(LOG, REPO)}; `clipengine attention` says what needs you")


def _log_tail(path: str, n: int = 40) -> list[str]:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 20000))
            return [l.decode("utf-8", "replace").rstrip() for l in fh.read().splitlines()[-n:]]
    except OSError:
        return []


def uptime(now: float | None = None, hours: float = 24.0, root: str | None = None) -> dict:
    """Autopilot deaths and dark periods observed in this report window."""
    now = time.time() if now is None else now
    st = _json(os.path.join(root, "autopilot.json") if root else APSTATE)
    since = now - hours * 3600
    recent = [d for d in st.get("deaths") or [] if since <= d.get("at", 0) <= now]
    gaps = [max(0.0, d["at"] - max(since, d.get("dark_from", d["at"]))) / 60 for d in recent]
    for inc in SV.incidents(root or _root(), since=since):
        end = inc.get("recovered_at") or now
        recent.append({"cause": inc.get("cause")})
        gaps.append(max(0.0, end - max(since, inc.get("dark_from", end))) / 60)
    running = _alive(_read_pid(os.path.join(root, "autopilot.pid"))) if root else autopilot_status()["running"]
    return {"deaths": len(recent), "dark_min": round(sum(gaps)), "running": running,
            "dark_periods": [round(minutes) for minutes in gaps if minutes > 15],
            "causes": [d.get("cause") or "unknown" for d in recent]}


def revive_supervised(by: str) -> str | None:
    """The next command after a reboot (no linger, no cron): an installed supervisor that nothing keeps alive is
    started again — the operator chose keep-alive at install, so no manual restart is needed."""
    if not SV.settings(_root()) or SV.stopped(_root()) or SV.keeping(_root()):
        return None
    msg = SV.boot(_root())
    AT.event("failure", f"supervisor was not running (host restarted?) — brought back by `{by}`: {msg[12:120]}",
             key=f"sv-boot:{int(time.time()) // 900}")
    return msg


def autopilot_died() -> bool:
    """Started and never stopped on purpose, but nobody holds the leader lease now."""
    if SV.stopped(_root()):
        return False
    return (SV.ever_started(_root()) or os.path.exists(PID)) and _leader() is None


def revive_if_dead(by: str, now: float | None = None, min_gap_s: float = 900) -> str | None:
    """Called by the always-running sentinels: if the autopilot died (not stopped), start it again with its
    last settings — at most once per 15 min, and only one sentinel does it (a lock file)."""
    if not autopilot_died() or SV.keeping(_root()):
        return None                                        # alive, or its service / keeper restarts it
    now = now or time.time()
    lock = os.path.join(RUNS, ".ap-revive.lock")
    try:
        try:
            last = float(open(lock).read().strip() or 0)
        except (OSError, ValueError):
            last = 0.0
        if now - last < min_gap_s:
            return None
        fd = os.open(lock + ".tmp", os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except OSError:
        return None                                        # another sentinel is on it
    try:
        open(lock, "w").write(str(now))
        if not autopilot_died():
            return None
        st = _json(APSTATE)
        try:
            os.remove(PID)                                 # so start() doesn't think it is still running
        except OSError:
            pass
        last = _log_tail(LOG, 40)
        cause = next((l for l in reversed(last) if any(k in l for k in ("Traceback", "Error", "error", "Killed",
                                                                          "restarting", "MemoryError"))), "")
        dark_from = float(st.get("last_cycle_at") or st.get("started_at") or now)
        if not SV.ever_started(_root()):                   # a pre-lease loop: the old death record
            deaths = (st.get("deaths") or [])[-19:] + [{"at": now, "dark_from": dark_from, "by": by,
                                                         "cause": cause[:200], "last": last[-5:]}]
            _write_ap(deaths=deaths)
        msg = autopilot_start(st.get("every"), bool(st.get("sessions")), by=f"revive:{by}")
        AT.event("failure", f"autopilot had stopped unexpectedly — restarted by the {by} sentinel after "
                 f"{(now - dark_from) / 60:.0f} min dark" + (f"; last: {cause[:100]}" if cause else ""),
                 key=f"ap-revive:{int(now) // 900}")
        return msg
    finally:
        try:
            os.remove(lock + ".tmp")
        except OSError:
            pass


def autopilot_stop() -> str:
    SV.mark_stopped("operator", _root())                   # first: the loop and its service both honour it
    SV.service_stop()
    pid = (_leader() or {}).get("pid") or _read_pid(PID)
    if not _alive(pid):
        try:
            os.remove(PID)
        except OSError:
            pass
        return "autopilot stopped (it stays stopped across restarts until `clipengine autopilot start`)"
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    for _ in range(40):
        if not _alive(pid):
            break
        time.sleep(0.25)
    try:
        os.remove(PID)
    except OSError:
        pass
    return ("autopilot stopped — it stays stopped across restarts until `clipengine autopilot start` "
            "(sentinels keep running; `clipengine stop --all` stops them too)")


def autopilot_status_line() -> str:
    st = autopilot_status()
    if not st["running"]:
        return "autopilot: not running — start it: clipengine autopilot start"
    last = st.get("last_cycle_at")
    ago = f"last cycle {(time.time() - last) / 60:.0f} min ago (exit {st.get('last_rc')})" if last else "no cycle yet"
    mode = " · stream hours only" if st.get("sessions") else ""
    idle = " · waiting for stream hours" if st.get("idle") else ""
    return f"autopilot: running (pid {st['pid']}) every {st.get('every', '?'):g} min{mode} · {ago}{idle}"


def _write_ap(**kw) -> None:
    st = {**_json(APSTATE), **kw}
    os.makedirs(RUNS, exist_ok=True)
    json.dump(st, open(APSTATE, "w"), indent=1)


def _maybe_restart(stamp: float, every_min: float, sessions: bool, t0: float, lease=None) -> float:
    """The loop's own code changed: restart on it — but only if the new code starts; else keep this loop."""
    if _code_stamp() == stamp:
        return stamp
    ok, why = _new_code_starts()
    if not ok:
        # never trade a working loop for one that won't start: keep going on this one and say so
        print(f"[autopilot] new version won't start ({why}) — staying on the running one", flush=True)
        AT.event("failure", f"autopilot couldn't restart on the new engine ({why}) — still running the "
                 "old loop; cycles already use the new code", key=f"ap-restart:{int(t0) // 3600}")
        return _code_stamp()
    print("[autopilot] engine updated — restarting on the new version", flush=True)
    if lease is not None:
        SV.record_exit(lease.generation, "update", 0, lease.root)   # the lock drops on exec; not an incident
    try:
        os.execv(sys.executable, [sys.executable, "-m", "clipengine", "autopilot", "run", "--every",
                                  str(every_min)] + (["--sessions"] if sessions else []))
    except OSError as exc:
        print(f"[autopilot] restart failed ({exc}) — staying on the running one", flush=True)
    return _code_stamp()


def autopilot_run(every_min: float, sessions: bool = False, rounds: int | None = None,
                  cycle_cmd: list[str] | None = None) -> None:
    """The loop itself (runs detached). Each round runs `cycle` as a child process, so a crash in one cycle
    never stops the loop, and re-execs itself when the engine's code changed."""
    stop = {"now": False, "sig": None}
    root = _root()
    by = os.environ.get("CLIPENGINE_STARTED_BY", "manual")
    if SV.stopped(root) and by in ("service", "keeper"):
        print("[autopilot] stopped on purpose (`clipengine autopilot start` resumes) — not starting", flush=True)
        return
    from . import migrate as MG
    ok, why = MG.may_lead()
    if not ok:
        print(f"[autopilot] {why} — not starting", flush=True)
        return
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, lambda n, _f: stop.update(now=True, sig=signal.Signals(n).name))
    lease = SV.Lease(root)
    if not lease.acquire(wait_s=1e12 if by in ("service", "keeper") else 0.0, poll_s=30.0,
                         should_stop=lambda: stop["now"] or bool(SV.stopped(root))):
        lead = SV.holder(root) or {}
        print(f"[autopilot] another leader holds the lease (pid {lead.get('pid')}) — not starting a second one",
              flush=True)
        return
    SV.reconcile(recovered_by=by, root=root)
    try:
        from .ops import config as _cfg
        ping = _cfg().get("heartbeat_url")
    except Exception:
        ping = None
    hb = SV.Heartbeat(lease, ping_url=ping)
    hb.start()
    try:
        _loop(every_min, sessions, rounds, cycle_cmd, stop, hb, lease, root)
    except BaseException as exc:
        if not isinstance(exc, SystemExit):
            SV.record_exit(lease.generation, f"crash: {type(exc).__name__}: {str(exc)[:160]}", SV.EXIT_RESTART, root)
        raise
    finally:
        hb.halt.set()
    if stop["sig"] and not SV.stopped(root):
        SV.record_exit(lease.generation, f"signal:{stop['sig']} (not an operator stop)", SV.EXIT_RESTART, root)
        lease.release()
        if rounds is None:
            sys.exit(SV.EXIT_RESTART)                         # a service restarts it
        return
    SV.record_exit(lease.generation, "stop", SV.EXIT_STOPPED, root)
    lease.release()


CYCLE_TIMEOUT_S = 1800


def run_cycle(cmd: list[str], timeout: float, out_path: str) -> tuple[str, int]:
    """One cycle in its own process group, output to a file (never a pipe: a grandchild that keeps a pipe open
    would hang the loop after a timeout). On timeout the whole group is killed. (output, exit code; -1 = hung)"""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w+") as fh:
        p = subprocess.Popen(cmd, cwd=REPO, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             start_new_session=True)
        try:
            rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(p.pid, sig)
                except OSError:
                    pass
                try:
                    p.wait(timeout=10)
                    break
                except subprocess.TimeoutExpired:
                    continue
            rc = -1
        fh.seek(0)
        out = fh.read()[-20000:].strip()
    return (out if rc != -1 else (out + f"\ncycle took over {timeout / 60:.0f} min — stopped it").strip()), rc


def _loop(every_min, sessions, rounds, cycle_cmd, stop, hb, lease, root) -> None:
    delay = SV.crash_loop_delay(root)
    if delay:
        print(f"[autopilot] crash loop — waiting {delay:.0f} s before cycling", flush=True)
        end = time.time() + delay
        while not stop["now"] and time.time() < end:
            time.sleep(1)
    stamp = _code_stamp()
    cmd = cycle_cmd or [sys.executable, "-m", "clipengine", "cycle"]
    _write_ap(pid=os.getpid(), every=every_min, sessions=sessions, started_at=time.time(),
              generation=lease.generation)
    n = 0
    print(f"[autopilot] {dt.datetime.now():%H:%M:%S} every {every_min:g} min{' (stream hours only)' if sessions else ''}",
          flush=True)
    while not stop["now"] and (rounds is None or n < rounds):
        t0 = time.time()
        if sessions and not in_session(load_schedule()) and not _json(os.path.join(RUNS, ".cycle.json")).get("any_live"):
            _write_ap(idle=True, checked_at=t0)
            hb.set_phase("idle")
        else:
            _write_ap(idle=False)
            hb.set_phase("cycle")
            out, rc = run_cycle(cmd, CYCLE_TIMEOUT_S, os.path.join(root, ".cycle.out"))
            if rc == -1:
                AT.event("failure", f"a cycle hung for {CYCLE_TIMEOUT_S // 60} min and was stopped",
                         key=f"cycle-hang:{int(t0)}")
            print(f"[autopilot] {dt.datetime.now():%H:%M:%S} cycle exit {rc}\n" + "\n".join(
                "  " + l for l in out.splitlines()[-12:]), flush=True)
            if rc not in (0,):
                AT.event("failure", f"cycle failed (exit {rc}): {(out.splitlines() or [''])[-1][:80]}",
                         key=f"cycle-fail:{int(t0) // 3600}")
            _write_ap(last_cycle_at=t0, last_rc=rc)
            hb.set_phase("sleep")
        n += 1
        if rounds is None and cycle_cmd is None:            # a loop with a custom cycle (a test) never re-execs
            stamp = _maybe_restart(stamp, every_min, sessions, t0, lease)
        while not stop["now"] and time.time() < t0 + every_min * 60 and (rounds is None or n < rounds):
            time.sleep(1)
    print("[autopilot] stopped", flush=True)


# ---- backups ----------------------------------------------------------------------------------------

BACKUP_PATHS = ("brand", "config", "data", "ledger.jsonl", os.path.join("local", "patches"))


def backup(to: str | None = None, root: str = REPO, now: float | None = None) -> str:
    import zipfile
    to = to or os.path.join(root, "runs", "backups")
    os.makedirs(to, exist_ok=True)
    name = os.path.join(to, f"backup-{dt.datetime.fromtimestamp(now or time.time()):%Y%m%d-%H%M%S}.zip")
    n = 0
    with zipfile.ZipFile(name, "w", zipfile.ZIP_DEFLATED) as z:
        for p in BACKUP_PATHS:
            full = os.path.join(root, p)
            files = [full] if os.path.isfile(full) else sorted(
                f for f in glob.glob(os.path.join(full, "**", "*"), recursive=True) if os.path.isfile(f))
            for f in files:
                if "__pycache__" in f or f.endswith(".pyc"):
                    continue
                z.write(f, os.path.relpath(f, root))
                n += 1
    print(f"backup: {n} files → {name}")
    return name


def restore(path: str, root: str = REPO) -> list[str]:
    """Put a backup back. Only the backed-up places are written; a backup of what's there now is made first.
    Everything is unpacked into a staging folder first; a restore killed part-way is finished (or reported) by the
    next run instead of leaving a silent mix."""
    import shutil
    import zipfile
    stage = os.path.join(root, "runs", "restore-staging")
    marker = os.path.join(root, "runs", "restore-in-progress.json")
    if _json(marker):
        print(f"restore: finishing an interrupted restore first ({_finish_restore(root)} file(s))")
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if not n.endswith("/") and n != "MANIFEST.json"]
        bad = [n for n in names if os.path.isabs(n) or ".." in n.replace("\\", "/").split("/")
               or not any(n == p or n.startswith(p.rstrip("/") + "/") for p in BACKUP_PATHS + ("outbox",))]
        if bad:
            raise SystemExit(f"RESTORE REFUSED: {path} has files outside the backed-up folders ({bad[0]})")
        before = backup(os.path.join(root, "runs", "backups"), root=root)
        shutil.rmtree(stage, ignore_errors=True)
        z.extractall(stage, names)                      # a kill here leaves the install untouched
    json.dump({"from": path, "names": names, "before": before, "at": time.time()}, open(marker, "w"))
    _finish_restore(root)
    print(f"restore: {len(names)} files from {path} (what was there before: {before})")
    return names


def _finish_restore(root: str = REPO) -> int:
    """Move a staged restore into place (also resumes one killed part-way). Returns files moved."""
    import shutil
    stage = os.path.join(root, "runs", "restore-staging")
    marker = os.path.join(root, "runs", "restore-in-progress.json")
    m = _json(marker)
    if not m:
        return 0
    n = 0
    for name in m["names"]:
        src, dest = os.path.join(stage, name), os.path.join(root, name)
        if os.path.exists(src):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            os.replace(src, dest)
            n += 1
    os.remove(marker)
    shutil.rmtree(stage, ignore_errors=True)
    return n


def restore_pending(root: str = REPO) -> str | None:
    m = _json(os.path.join(root, "runs", "restore-in-progress.json"))
    return f"a restore from {m['from']} was interrupted — `clipengine restore {m['from']}` finishes it" if m else None
