"""Results, updates and help — so nobody has to learn anything technical.

  report    what posted (links), views + shares/watch time, what was learned, pools, money thresholds — ~400 tokens
  update    pull the newest engine without losing the channel's own fixes: local code changes are
            saved as patches (local/patches/), the update is pulled, patches are re-applied (and
            dropped when the new version already contains them), then setup + doctor run
  support   a log bundle with secrets, emails, handles and home paths stripped, plus a filled-in
            issue text — ready to send upstream
"""
from __future__ import annotations

import datetime as dt
import glob
import io
import json
import os
from pathlib import Path
import re
import statistics as st
import subprocess
import sys
import time
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATCHES = os.path.join(REPO, "local", "patches")


def _rows(path: str) -> list[dict]:
    out = []
    if os.path.exists(path):
        for line in open(path):
            try:
                r = json.loads(line)
                if not any(str(k).startswith("_") for k in r):
                    out.append(r)
            except ValueError:
                pass
    return out


def _ts(iso):
    try:
        return dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ---- report ---------------------------------------------------------------------------------------

def money_line(now: float | None = None, repo: str | None = None) -> str:
    from pathlib import Path
    from . import brief

    root = Path(repo or REPO)
    ledger = [row for row in brief.rows(root / "ledger.jsonl") if row.get("status") == "live"]
    line, _ = brief.money(root, time.time() if now is None else now, ledger,
                          brief.rows(root / "data" / "performance.jsonl"))
    return line


def report_text(days: float = 1.0, now: float | None = None) -> str:
    """Five sentences, followed by the measurements behind the day's work."""
    from pathlib import Path

    from . import __version__, autopilot, brief, campaigns, handpost, judge, learn, scouts, sentinel, untrusted

    now = time.time() if now is None else now
    since = now - days * brief.DAY
    root = Path(REPO)
    runs = root / "runs"
    roster = brief.read(root / "config" / "creators.json", {}).get("creators") or {}
    ledger = [row for row in brief.rows(root / "ledger.jsonl") if row.get("status") == "live"]
    perf = brief.rows(root / "data" / "performance.jsonl")
    recent = [row for row in ledger if since <= (_ts(row.get("at")) or 0) <= now]
    recent_keys = {(row.get("slug"), row.get("platform")): row for row in recent}
    latest = {}
    for r in perf:
        at = _ts(r.get("checked_at")) or 0
        key = r.get("slug"), r.get("platform")
        if key in recent_keys and r.get("views") is not None and at <= now and at >= latest.get(key, (0, {}))[0]:
            latest[key] = (at, r)
    snapshots = [r for _, r in latest.values()]
    first = "Nothing went live in this period"
    if not recent:
        first += " — " + pipeline_stop(root, since, now)
    if recent:
        count = len({row["slug"] for row in recent})
        platforms = len(recent_keys)
        first = f"Posted {count} clip{'s' if count != 1 else ''} across {platforms} platform post{'s' if platforms != 1 else ''}"
        if snapshots:
            first += f" with {sum(r['views'] for r in snapshots):,} measured views"
            shares = [r["shares"] for r in snapshots if r.get("shares") is not None]
            watch = [r["watch_s"] for r in snapshots if r.get("watch_s") is not None]
            first += (f", {sum(shares):,} shares" if shares else "") + (f", watch {st.median(watch):.1f} s" if watch else "")
            best = max(snapshots, key=lambda r: r["views"])
            link = recent_keys[(best["slug"], best["platform"])].get("url")
            first += f" ({link})" if link else ""
        else:
            first += " and analytics reads are pending"
    scout = scouts.cached_report(days * 24, now, str(runs), roster)
    miss = scout["best_miss"]
    if miss:
        why = untrusted.clean(miss["why"], 75)
        second = f"The biggest miss was a viewer clip from {miss['login']} with {miss['views']:,} views because {why}"
    elif scout["total"]:
        second = "Scouts caught every observed viewer-clip moment in this period"
    else:
        second = "No viewer-clip misses were observed in the sampled feed"
    weights = brief.read(root / "data" / "weights.json", {})
    learned = weights.get("summary") or next(iter(weights.get("report") or []), "")
    third = untrusted.clean(learned, 105) if learned else "Learning is waiting for measured posts"
    planned = brief.read(runs / ".cycle.json", {}).get("watch_today") or []
    fourth = (f"Today the scouts are watching {', '.join(planned[:3])}" if planned
              else "Today the scouts will watch the configured roster as creators go live")
    money, tasks = brief.money(root, now, ledger, perf)
    measure = handpost.to_measure(now, ledger=ledger, perf=perf, lookback_h=float("inf"))
    overdue = [post for post in measure if post["missing"]]
    if tasks:
        fifth = tasks[0]
    elif overdue:
        fifth = f"Please measure {overdue[0]['slug']} on {overdue[0]['platform']}"
    elif measure:
        fifth = f"Please record the next analytics read for {measure[0]['slug']}"
    else:
        fifth = "No owner action is due this morning"
    note = " ".join(sentence.rstrip(".!?") + "." for sentence in (first, second, third, fourth, fifth))

    up = autopilot.uptime(now, hours=days * 24, root=str(runs))
    periods = ", ".join(f"{n}m" for n in up["dark_periods"]) or "none"
    causes = ", ".join(untrusted.clean(cause, 64) for cause in up["causes"]) or "none"
    running = "running" if up["running"] else "stopped"
    window = f"{days * 24:g} h" if days < 2 else f"{days:g} days"
    # the window's total, never worded as one outage (it was read as "this run was 43 min dark")
    uptime = (f"engine v{__version__} · uptime: {running} · last {window}: {up['deaths']} outage(s), "
              f"{up['dark_min']}m dark in total, longest {up.get('longest_min', 0)}m · >15m: {periods} · causes: {causes}")
    lead = f"{100 * scout['first'] / scout['caught']:.0f}%" if scout["caught"] else "–"
    recall = f"{100 * scout['caught'] / scout['total']:.0f}%" if scout["total"] else "–"
    trained = brief.read(root / "config" / "sentinel_model.json", {}).get("eval") or {}
    held_out = trained.get("held_out_recall_top10")
    training = f"{100 * held_out:.0f}%" if held_out is not None else "–"
    fires = trained.get("false_fires_per_hour")
    train_rate = f" at {fires:g} false/h" if fires is not None else ""
    scout_line = (f"scouts: {scout['caught']}/{scout['total']} caught · first {lead} (target ≥50%) · "
                  f"live recall {recall} · train {training}{train_rate}")
    if scout["week_reason"]:
        scout_line += f" · week miss: {untrusted.clean(scout['week_reason'], 55)}"
    drafts = sentinel.load_queue(str(runs))
    speed, pipeline = brief.timing(drafts, ledger, since, now)
    lines = [note, uptime, scout_line, speed, pipeline, *brief.recording(runs, since, now), money]
    if tasks:
        lines.append("Enrolment tasks · " + " · ".join(tasks))
    recurring = judge.recurring_reasons(now)
    if recurring:
        lines.append("named detector test due: " + untrusted.clean(", ".join(recurring), 100))
    extras = [learn.forecast_summary(str(root / "data" / "predictions.jsonl"), now),
              scouts.category_shape(now, str(runs)), brief.series_line(root, ledger), brief.rights_line(roster, now)]
    if os.path.realpath(campaigns.REPO) == os.path.realpath(REPO):
        totals = campaigns.totals(since)
        if campaigns.pools() or totals["submitted_all"] or totals["paid"]:
            extras.append(f"pools: {totals['submitted']} submitted · ${totals['paid']:,.2f} paid")
    try:
        from . import facecam
        boxes = facecam.box_recheck(now, path=str(runs / "facecam_checks.jsonl"),
                                    creators=str(root / "config" / "creators.json"))
        if boxes["recheck"]:
            extras.append(facecam.recheck_line(boxes))
    except Exception:
        pass
    extras = [line for line in extras if line]
    remaining = 12 - len(lines)
    if remaining > 0 and extras:
        lines.extend(extras[:remaining - 1])
        lines.append(" · ".join(extras[remaining - 1:]))
    elif extras:
        lines[-1] += " · " + " · ".join(extras)
    return "\n".join(lines)


def pipeline_stop(root, since: float, now: float) -> str:
    """Zero posts: where the pipeline stopped — streams watched → drafts → over a bar → judged → dropped → posted."""
    import glob
    from . import brief, sentinel
    runs = os.path.join(str(root), "runs")
    seen = brief.read(Path(runs) / ".cycle.json", {}).get("seen_live") or {}
    live = sum(1 for ivs in seen.values() for s, e in ivs if e > since and s < now)
    rows = [r for r in sentinel.load_queue(runs) if since <= (_ts(r.get("drafted_at")) or _ts(r.get("at")) or 0) <= now]
    over = [r for r in rows if r.get("status") != "dropped"]
    judged = [r for r in rows if r.get("status") in ("published", "skipped") or r.get("judged_at")]
    drops = 0
    for p in glob.glob(os.path.join(str(root), "outbox", "*", "post.json")):
        t = _ts(brief.read(Path(p), {}).get("dropped_at"))
        drops += bool(t and since <= t <= now)
    funnel = (f"{live} stream session(s) watched → {len(rows)} draft(s) → {len(over)} over a bar → "
              f"{len(judged)} judged → {drops} dropped → 0 posted")
    if not live and not rows:
        where = "no roster creator was live (or no cycle ran)"
    elif not rows:
        where = "streams were watched but nothing was drafted"
    elif not over:
        where = "every draft fell under every platform's bar"
    elif not judged:
        where = f"{len(over)} draft(s) are waiting for a look"
    elif not drops:
        where = "the judged drafts were all skipped"
    else:
        where = f"{drops} clip(s) are dropped and waiting to be posted"
    return f"it stopped at: {where} ({funnel})"


def report(days: float = 1.0, now: float | None = None) -> str:
    out = report_text(days, now)
    print(out)
    return out


# ---- warm-up --------------------------------------------------------------------------------------

def warmup_cap(platform: str, full_cap: int, ledger: list[dict] | None = None, now: float | None = None,
               started: str | None = None) -> int:
    """New accounts post less at first: 1/day for days 0–2, 2 for 3–5, half the cap for 6–9, then full.
    Day 0 is the first post on that platform (or the pack's start date)."""
    now = now or time.time()
    ledger = ledger if ledger is not None else [r for r in _rows(os.path.join(REPO, "ledger.jsonl"))
                                                if r.get("status") == "live"]
    firsts = [(_ts(r.get("at")) or now) for r in ledger if r.get("platform") == platform]
    t0 = min(firsts) if firsts else (_ts(started) if started else now)
    day = (now - t0) / 86400
    if day < 3:
        return min(full_cap, 1)
    if day < 6:
        return min(full_cap, 2)
    if day < 10:
        return max(1, (full_cap + 1) // 2)
    return full_cap


# ---- update -----------------------------------------------------------------------------------------

def _git(*a: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", REPO, *a], capture_output=True, text=True, check=check)


def _tests_pass() -> tuple[bool, str]:
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", "tests", "-q", "-x"], cwd=REPO, capture_output=True,
                           text=True, timeout=1200)
    except subprocess.TimeoutExpired:
        return False, "tests timed out"
    return r.returncode == 0, ((r.stdout or "").strip().splitlines() or [""])[-1]


def _install_requirements(quiet: bool) -> None:
    """Add the packages a new version needs (pip only adds; the version that worked keeps working)."""
    pip = os.path.join(REPO, ".venv", "bin", "pip")
    if os.path.exists(pip):
        subprocess.run([pip, "install", "-q", "-r", os.path.join(REPO, "requirements.txt")], cwd=REPO,
                       capture_output=quiet)


def update(test: bool = True, quiet: bool = False) -> bool:
    """Pull the newest engine, keep this channel's fixes, and keep the new version only if it passes its
    tests on this machine — otherwise go back to the version that worked."""
    from . import repo as RP
    RP.lock_push()
    os.makedirs(PATCHES, exist_ok=True)
    old = _git("rev-parse", "HEAD").stdout.strip()
    diff = _git("diff", "HEAD", "--", ".", ":(exclude)brand", ":(exclude)config", ":(exclude)data",
                ":(exclude)local").stdout
    if diff.strip():
        name = os.path.join(PATCHES, f"local-{dt.datetime.now():%Y%m%d-%H%M%S}.patch")
        open(name, "w").write(diff)
        print(f"saved your local fixes: {os.path.relpath(name, REPO)}")
    # tracked changes are saved as patches above; untracked files stay where they are (never stashed and dropped)
    _git("stash", "push", "-q", "-m", "clipengine-update", "--", ".",
         ":(exclude)brand", ":(exclude)local", ":(exclude)runs", ":(exclude)data", ":(exclude)config")
    pull = _git("pull", "--ff-only", "-q")
    if pull.returncode != 0:
        _git("stash", "pop", "-q")
        raise SystemExit("update failed (couldn't pull): " + (pull.stderr or pull.stdout)[-200:])
    new = _git("rev-parse", "HEAD").stdout.strip()

    def apply_all() -> tuple[list, list, list]:
        applied, merged, conflict = [], [], []
        for p in sorted(glob.glob(os.path.join(PATCHES, "*.patch"))):
            if _git("apply", "--reverse", "--check", p).returncode == 0:
                merged.append(p)                                   # this version already has it
            elif _git("apply", "--check", p).returncode == 0 and _git("apply", p).returncode == 0:
                applied.append(p)                                  # clean apply only — never conflict markers
            else:
                conflict.append(p)
        return applied, merged, conflict

    applied, merged, conflict = apply_all()
    _git("stash", "drop", "-q")
    if new == old:
        print(f"already up to date ({_git('log', '-1', '--format=%h %s').stdout.strip()})")
        return True
    if _git("diff", "--quiet", old, new, "--", "requirements.txt").returncode != 0:
        _install_requirements(quiet)                       # new packages first, so the tests judge the code, not the venv
    if test:
        ok, line = _tests_pass()
        if not ok:
            _git("reset", "-q")                                    # this channel's fixes are saved as patches:
            _git("checkout", "-q", "--", ".", ":(exclude)brand", ":(exclude)config", ":(exclude)data", ":(exclude)local")
            _git("reset", "--keep", "-q", old)                     # back to the version that worked
            apply_all()                                            # and put this channel's fixes back on it
            print(f"UPDATE ROLLED BACK — the new version failed its tests on this machine ({line}). You're still on "
                  f"{_git('log', '-1', '--format=%h %s').stdout.strip()}, which worked. "
                  "Run `clipengine support --problem \"update failed its tests\"` and send the bundle to the maintainers.")
            return False
    for p in merged:
        os.rename(p, p + ".merged")
    print(f"updated to {_git('log', '-1', '--format=%h %s').stdout.strip()}" + (" (tests pass)" if test else ""))
    if applied:
        print(f"re-applied {len(applied)} local fix(es)")
    if merged:
        print(f"{len(merged)} local fix(es) are now part of the engine — retired")
    if conflict:
        print("these local fixes no longer apply (the code changed): "
              + ", ".join(os.path.basename(c) for c in conflict) + " — fix again with the bug runbook")
    subprocess.run(["bash", os.path.join(REPO, "setup", "setup.sh"), "--quiet"], cwd=REPO,
                   capture_output=quiet)          # an automatic update stays out of the cycle's few lines
    from .ops import doctor, restart_sentinels
    for line in restart_sentinels():               # running sentinels would keep the old code otherwise
        print(line)
    doctor()
    return True


# ---- support ----------------------------------------------------------------------------------------

_SCRUB = [
    (re.compile(r"(?i)\b(bearer|basic|token|oauth)\s+[A-Za-z0-9._~+/=-]{3,}"), r"\1 <redacted>"),
    (re.compile(r"(?i)(authorization|cookie|set-cookie|x-api-key|token|secret|password|sig|signature)([\"'=: ]+)[^\s\"'&,]+"), r"\1\2<redacted>"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "<email>"),
    (re.compile(r"(?i)(gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,})"), "<key>"),
    (re.compile(r"(/Users/|/home/)[^/\s]+"), r"\1<user>"),
    (re.compile(r"(?i)https?://\S*(token|sig|signature|auth)=\S+"), "<url-with-credentials>"),
]


def scrub(text: str, extra: list[str] | None = None) -> str:
    for rx, rep in _SCRUB:
        text = rx.sub(rep, text)
    for word in extra or []:
        if word and len(word) > 2:
            text = re.sub(re.escape(word), "<handle>", text, flags=re.I)
    return text


def support(problem: str = "") -> str:
    from . import brand as B
    acc = B.load().get("accounts", {})
    handles = [acc.get("youtube", {}).get("handle", ""), acc.get("instagram", {}).get("username", ""),
               acc.get("tiktok", {}).get("username", ""), B.load().get("name", "")]
    out_dir = os.path.join(REPO, "local")
    os.makedirs(out_dir, exist_ok=True)
    zpath = os.path.join(out_dir, f"support-{dt.datetime.now():%Y%m%d-%H%M%S}.zip")
    buf = io.StringIO()
    from contextlib import redirect_stdout
    with redirect_stdout(buf):
        try:
            from .ops import doctor
            doctor()
        except Exception as exc:
            print(f"doctor failed: {exc}")
    diag = ""
    try:
        from .diagnose import diagnose
        with redirect_stdout(io.StringIO()) as d2:
            diagnose()
        diag = d2.getvalue()
    except Exception:
        pass
    from . import __version__
    version = f"v{__version__} " + _git("log", "-1", "--format=%h %cs").stdout.strip()
    logs = sorted(glob.glob(os.path.join(REPO, "logs", "*.log")) + glob.glob(os.path.join(REPO, "runs", "*.log")),
                  key=os.path.getmtime)[-8:]
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("doctor.txt", scrub(buf.getvalue(), handles))
        z.writestr("diagnose.txt", scrub(diag, handles))
        z.writestr("version.txt", f"engine {version}\npython {sys.version.split()[0]}\nplatform {sys.platform}\n")
        for lg in logs:
            tail = open(lg, errors="replace").read()[-20000:]
            z.writestr("logs/" + os.path.basename(lg), scrub(tail, handles))
    issue = (f"### What happened\n{problem or '(describe in one or two sentences)'}\n\n"
             f"### Engine version\n{version} on {sys.platform}\n\n"
             f"### What diagnose found\n{scrub(diag, handles)[:1200] or 'nothing matched a known problem'}\n\n"
             f"### Attached\n{os.path.basename(zpath)} (secrets, emails, handles and home paths removed)\n")
    open(zpath.replace(".zip", ".issue.md"), "w").write(issue)
    print(f"support bundle: {os.path.relpath(zpath, REPO)}\nissue text: {os.path.relpath(zpath.replace('.zip', '.issue.md'), REPO)}")
    return zpath
