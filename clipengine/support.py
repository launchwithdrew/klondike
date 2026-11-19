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

YT_PARTNER = {"subs": 1000, "shorts_views_90d": 10_000_000}
TIKTOK_PROGRAM = {"followers": 10_000}
MAX_LINES, MAX_LINE = 12, 130          # ~400 tokens (test_budget_report_queue_and_diagnose)


def _n(x: float) -> str:
    x = float(x)
    return f"{x / 1e6:.1f}M" if x >= 1e6 else f"{x / 1e3:.1f}k" if x >= 1e4 else f"{x:,.0f}"


def money_line(now: float | None = None, repo: str | None = None) -> str:
    """Progress to YouTube Partner (1,000 subs + 10M Shorts views in 90 days) and TikTok's creator
    program (about 10k followers), from follower counts the agent records and our view reads."""
    repo = repo or REPO
    now = now or time.time()
    fol: dict[str, tuple[float, int]] = {}
    for r in _rows(os.path.join(repo, "data", "followers.jsonl")):
        t = _ts(r.get("at")) or 0
        if r.get("platform") and r.get("followers") is not None and t >= fol.get(r["platform"], (-1, 0))[0]:
            fol[r["platform"]] = (t, int(r["followers"]))
    live = {r["slug"]: _ts(r.get("at")) or 0 for r in _rows(os.path.join(repo, "ledger.jsonl"))
            if r.get("status") == "live" and r.get("platform") == "youtube"}
    yt_views: dict[str, tuple[float, int]] = {}
    for r in _rows(os.path.join(repo, "data", "performance.jsonl")):
        if r.get("platform") == "youtube" and r.get("views") is not None and now - live.get(r.get("slug"), 0) <= 90 * 86400:
            t = _ts(r.get("checked_at")) or 0
            if t >= yt_views.get(r["slug"], (-1, 0))[0]:
                yt_views[r["slug"]] = (t, int(r["views"]))
    shorts = sum(v for _, v in yt_views.values())
    if not fol:
        return (f"money: Shorts {_n(shorts)}/10M views (90 d) — record followers: "
                "`clipengine followers --platform P --count N`")
    yt = fol.get("youtube", (0, None))[1]
    tk = fol.get("tiktok", (0, None))[1]
    parts = [f"YouTube Partner {'?' if yt is None else _n(yt)}/1,000 subs + {_n(shorts)}/10M Shorts views (90 d)"
             + (" ✓" if (yt or 0) >= YT_PARTNER["subs"] and shorts >= YT_PARTNER["shorts_views_90d"] else "")]
    parts.append(f"TikTok {'?' if tk is None else _n(tk)}/10k followers" + (" ✓" if (tk or 0) >= TIKTOK_PROGRAM["followers"] else ""))
    if fol.get("instagram"):
        parts.append(f"IG {_n(fol['instagram'][1])}")
    return "money: " + " · ".join(parts)


def report_text(days: float = 1.0, now: float | None = None) -> str:
    """The report as text, nothing printed — what a daily-report trigger sends (ops calls this)."""
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
    urls = {(r["slug"], r["platform"]): r.get("url", "") for r in ledger}
    from . import __version__
    lines = [f"REPORT — last {days:g} day(s) · engine v{__version__}"]
    for plat in ("youtube", "instagram", "tiktok"):
        now_posts = [r for r in ledger if r["platform"] == plat and (_ts(r.get("at")) or 0) >= since]
        prev_posts = [r for r in ledger if r["platform"] == plat and before <= (_ts(r.get("at")) or 0) < since]
        reads = [latest[(r["slug"], plat)] for r in now_posts if (r["slug"], plat) in latest]
        all_views = [v["views"] for (s, p), v in latest.items() if p == plat]
        med = st.median(all_views) if all_views else 0
        shares = [x.get("shares") for x in reads if x.get("shares") is not None]
        watch = [x.get("watch_s") for x in reads if x.get("watch_s") is not None]
        lines.append(f"  {plat:<10} {len(now_posts)} posts (prev {len(prev_posts)}) · {sum(x['views'] for x in reads):,} views on them"
                     + (f" · typical post {med:,.0f}" if all_views else "")
                     + (f" · {sum(shares):,} shares" if shares else "")
                     + (f" · watch {st.median(watch):.1f} s" if watch else ""))
    recent = [(v["views"], s, p) for (s, p), v in latest.items()
              if any(r["slug"] == s and (_ts(r.get("at")) or 0) >= since for r in ledger)]
    pick = max(recent) if recent else max(((v["views"], s, p) for (s, p), v in latest.items()), default=None)
    if pick:
        v, s, p = pick
        lines.append(f"  {'best' if recent else 'best ever'}: \"{titles.get(s, s)[:50]}\" — {v:,} views on {p} {urls.get((s, p), '')}")
    new_posts = sorted((r for r in ledger if (_ts(r.get("at")) or 0) >= since), key=lambda r: -(_ts(r.get("at")) or 0))
    if new_posts:
        lines.append("  posted: " + " · ".join(f"{r['platform'][:2]} {r.get('url') or r['slug']}" for r in new_posts[:3])
                     + (f" · +{len(new_posts) - 3} more" if len(new_posts) > 3 else ""))
    try:
        w = json.load(open(os.path.join(REPO, "data", "weights.json")))
        learned = w.get("summary") or next(iter(w.get("report") or []), "")
        if learned:
            lines.append("  " + (learned if learned.startswith("learned") else "learned: " + learned))
        top = sorted((w.get("streamers") or {}).items(), key=lambda x: -x[1])[:3]
        if top:
            lines.append("  streamers doing best for you: " + ", ".join(f"{l} {f:.1f}×" for l, f in top))
    except (OSError, ValueError):
        pass
    try:
        from . import campaigns as CP
        if os.path.realpath(CP.REPO) == os.path.realpath(REPO):
            t = CP.totals(since)
            if CP.pools() or t["submitted_all"] or t["paid"]:
                lines.append(f"  pools: {t['submitted']} submitted (all-time {t['submitted_all']}) · "
                             f"${t['paid']:,.2f} paid (${t['paid_since']:,.2f} this period)")
    except Exception:
        pass
    lines.append("  " + money_line(now))
    todo = []
    try:
        from .sentinel import load_queue
        rows = load_queue(os.path.join(REPO, "runs"))
        period = [r for r in rows if (r.get("stop") or 0) >= since]
        skipped = sum(1 for r in period if r.get("status") == "skipped")
        unsafe = sum(1 for r in period if r.get("unsafe"))
        if skipped or unsafe:
            lines.append(f"  dropped: {skipped} draft(s) skipped after a look, {unsafe} unsafe")
        q = [r for r in rows if r.get("status") == "new"]
        if q:
            todo.append(f"{len(q)} draft(s) in `clipengine queue`")
    except Exception:
        pass
    try:
        from .handpost import to_measure
        m = to_measure(now, ledger=ledger, perf=perf)
        if m:
            todo.append(f"{len(m)} post(s) to measure (`to-measure`)")
    except Exception:
        pass
    try:
        from . import campaigns as CP
        if os.path.realpath(CP.REPO) == os.path.realpath(REPO):
            n = len(CP.to_submit(now))
            if n:
                todo.append(f"{n} to submit to pools (`to-submit`)")
    except Exception:
        pass
    if todo:
        lines.append("  to do: " + " · ".join(todo))
    if not ledger:
        lines.append("  nothing posted yet — the first clips are the hardest; run `clipengine cycle`")
    lines = [l if len(l) <= MAX_LINE else l[:MAX_LINE - 1] + "…" for l in lines]
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES - 1] + [lines[-1]]
    return "\n".join(lines)


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
