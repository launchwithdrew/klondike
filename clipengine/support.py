"""Results, updates and help — so nobody has to learn anything technical.

  report    what posted, views per platform, the best clip, the trend — about 400 tokens
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

def report(days: float = 1.0, now: float | None = None) -> str:
    now = now or time.time()
    since, before = now - days * 86400, now - 2 * days * 86400
    ledger = [r for r in _rows(os.path.join(REPO, "ledger.jsonl")) if r.get("status") == "live"]
    perf = _rows(os.path.join(REPO, "data", "performance.jsonl"))
    latest: dict[tuple, dict] = {}
    for r in perf:
        if r.get("views") is None:
            continue
        k = (r["slug"], r["platform"])
        if k not in latest or (_ts(r.get("checked_at")) or 0) >= (_ts(latest[k].get("checked_at")) or 0):
            latest[k] = r
    titles = {r["slug"]: r.get("title", r["slug"]) for r in ledger}
    lines = [f"REPORT — last {days:g} day(s)"]
    for plat in ("youtube", "instagram", "tiktok"):
        now_posts = [r for r in ledger if r["platform"] == plat and (_ts(r.get("at")) or 0) >= since]
        prev_posts = [r for r in ledger if r["platform"] == plat and before <= (_ts(r.get("at")) or 0) < since]
        views = [latest[(r["slug"], plat)]["views"] for r in now_posts if (r["slug"], plat) in latest]
        all_views = [v["views"] for (s, p), v in latest.items() if p == plat]
        total = sum(views)
        med = st.median(all_views) if all_views else 0
        lines.append(f"  {plat:<10} {len(now_posts)} posts (prev {len(prev_posts)}) · {total:,} views on them"
                     + (f" · typical post {med:,.0f}" if all_views else ""))
    recent = [(v["views"], s, p) for (s, p), v in latest.items()
              if any(r["slug"] == s and (_ts(r.get("at")) or 0) >= since for r in ledger)]
    if recent:
        v, s, p = max(recent)
        lines.append(f"  best: \"{titles.get(s, s)[:60]}\" — {v:,} views on {p}")
    all_time = sorted(((v["views"], s, p) for (s, p), v in latest.items()), reverse=True)[:1]
    if all_time:
        v, s, p = all_time[0]
        lines.append(f"  best ever: \"{titles.get(s, s)[:60]}\" — {v:,} views on {p}")
    try:
        w = json.load(open(os.path.join(REPO, "data", "weights.json")))
        top = sorted((w.get("streamers") or {}).items(), key=lambda x: -x[1])[:3]
        if top:
            lines.append("  streamers doing best for you: " + ", ".join(f"{l} {f:.1f}×" for l, f in top))
        for r in (w.get("report") or [])[:2]:
            lines.append(f"  learned: {r}")
    except (OSError, ValueError):
        pass
    try:
        from .sentinel import load_queue
        q = [r for r in load_queue() if r.get("status") == "new"]
        if q:
            lines.append(f"  waiting in the queue: {len(q)} draft(s) — run `clipengine queue`")
    except Exception:
        pass
    if not ledger:
        lines.append("  nothing posted yet — the first clips are the hardest; run `clipengine cycle`")
    out = "\n".join(lines)
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
    version = _git("log", "-1", "--format=%h %cs").stdout.strip()
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
