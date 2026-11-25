"""Any agent can run the engine (OP_SPEC §15): machine-readable answers, one next step, guides, a host check.

L1  `--json` on attention / queue / to-post / bank / doctor, and `clipengine next` (what to do now + the command)
L2  guides/*.md — one page per job; a changed guide is named once on `attention` (and `next`) until read
L3  `clipengine mcp` — the same functions as typed MCP tools over stdio (mcp.py)

Stream text (chat, what was said, viewer titles) is never put in a field an agent could mistake for an instruction:
it travels only under "untrusted", already cleaned and quoted («…»), the way the CLI shows it.

Exit codes (stable): 0 ok or nothing to do · 1 a check or step failed · 2 bad arguments · 3 a source failed (retry).
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import time

from . import untrusted as UT

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUIDES = os.path.join(REPO, "guides")
GUIDES_SEEN = os.path.join(REPO, "runs", ".guides.json")
EXIT = {"ok": 0, "failed": 1, "usage": 2, "source_failed": 3}


# ---- L1: machine-readable answers ---------------------------------------------------------------------------

def _quiet(fn, *a, **k):
    """Call a printing function, keep its return value, drop what it printed."""
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **k)


def attention_json() -> dict:
    from . import attention as AT
    items = AT.pending()
    return {"line": AT.line(items), "items": [{"kind": i.get("kind"), "text": i.get("text"), "key": i.get("key"),
                                               "at": i.get("at")} for i in items],
            "guides_changed": guides_changed()}


def queue_json(all_: bool = False) -> list[dict]:
    from . import sentinel as SN
    rows = _quiet(SN.show_queue, all_=all_, seen=False)
    out = []
    for r in rows:
        out.append({"id": r["id"], "login": r.get("login"), "best": r.get("best"), "age_min": r.get("age_min"),
                    "urgent": bool(SN.is_urgent(r) or r.get("angle_urgent")), "unconfirmed": bool(SN.unconfirmed(r)),
                    "status": r.get("status"), "triage": r.get("triage"), "expires_at": r.get("expires_at"),
                    "flags": r.get("flags") or [], "sheet": r.get("sheet"), "unsafe": bool(r.get("unsafe")),
                    "angles": r.get("angles") or [],
                    "untrusted": {"chat": UT.quote(r.get("chat", ""), 80), "said": UT.quote(r.get("said", ""), 160)},
                    "look": "open `sheet` (the frames) before deciding",
                    "publish": f"clipengine publish-queued --id {r['id']} --title \"…\" --quote \"…\" --line \"…\"",
                    "skip": f"clipengine skip --id {r['id']} --reason \"…\""})
    return out


def to_post_json() -> list[dict]:
    from . import handpost as HP
    out = []
    for it in HP.due():
        out.append({"slug": it["slug"], "file": it["file"], "now": it["now"], "later": it["later"],
                    "blocked": it.get("rights_reason"), "captions": it.get("captions"),
                    "accounts": {p: HP.account_handle(p) for p in it["now"]},
                    "claim": [f"clipengine claim --slug {it['slug']} --platform {p}" for p in it["now"]]})
    return out


def bank_json() -> list[dict]:
    from . import bank as BK
    return BK.inventory()


def doctor_json(accounts: bool = False) -> dict:
    """Every doctor line as {ok, text}: ✓ ok, ✗ failed (with its fix after '—'), ! a warning."""
    from . import ops as O
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ok = O.doctor(accounts)
    checks = []
    for raw in buf.getvalue().splitlines():
        t = raw.strip()
        if not t or t[0] not in "✓✗!":
            continue
        name, _, fix = t[1:].strip().partition(" — ")
        checks.append({"ok": t[0] != "✗", "warn": t[0] == "!", "check": name, "fix": fix or None})
    return {"ready": bool(ok), "checks": checks}


# ---- the next step -------------------------------------------------------------------------------------------

def next_step(now: float | None = None) -> dict:
    """The one thing to do now, most urgent first, with the exact command. {"do", "command", "why", "then"}."""
    from . import bank as BK
    from . import handpost as HP
    now = time.time() if now is None else now
    try:
        unc = BK.uncertain(now)
    except Exception:
        unc = []
    if unc:
        r = unc[0]
        return {"do": f"reconcile {r['slug']} → {r['platform']}: the upload lease ran out with no outcome",
                "command": f"check the {r['platform']} account, then `clipengine posted --slug {r['slug']} "
                           f"--platform {r['platform']} --url <link>` or `clipengine attempt --slug {r['slug']} "
                           f"--platform {r['platform']} --failed \"why\"`",
                "why": "never re-upload blind", "then": "clipengine next"}
    drafts = []
    try:
        drafts = queue_json()
    except Exception:
        pass
    urgent = [d for d in drafts if d["urgent"] and not d["unsafe"]]
    if urgent:
        d = urgent[0]
        return {"do": f"judge URGENT draft {d['id']} (best {d['best']:.0f}, {d['age_min']} min old) now: look at its frames",
                "command": d["publish"], "skip": d["skip"], "sheet": d["sheet"],
                "why": "an URGENT draft is worth most in its first minutes", "then": "clipengine next"}
    try:
        due = [i for i in HP.due(now) if i["now"] and not i.get("rights_reason")]
    except Exception:
        due = []
    if due:
        it = due[0]
        p = it["now"][0]
        return {"do": f"post {it['slug']} to {p} (as {HP.account_handle(p) or 'our account'})",
                "command": f"clipengine claim --slug {it['slug']} --platform {p}",
                "why": "its slot is open", "then": f"upload with that text, then `clipengine posted --slug {it['slug']} "
                                                   f"--platform {p} --url <link>`"}
    if drafts:
        d = next((x for x in drafts if not x["unsafe"]), None)
        if d:
            return {"do": f"judge draft {d['id']} (best {d['best']:.0f}, {d['age_min']} min old): look at its frames",
                    "command": d["publish"], "skip": d["skip"], "sheet": d["sheet"],
                    "why": "judge each draft the moment it's queued", "then": "clipengine next"}
    try:
        reads = HP.to_measure(now)
    except Exception:
        reads = []
    if reads:
        r = reads[0]
        return {"do": f"record the {r.get('due') or 'next'} read of {r.get('slug')} on {r.get('platform')}",
                "command": f"clipengine views --slug {r.get('slug')} --platform {r.get('platform')} --views N "
                           "--watch-s S --shares N", "why": "shares and watch time teach the engine", "then": "clipengine next"}
    changed = guides_changed()
    if changed:
        return {"do": "read the changed guide(s): " + ", ".join(changed), "command": "clipengine attention --seen",
                "why": "the engine updated how a job is done", "then": "clipengine next"}
    return {"do": "nothing needs you", "command": "clipengine attention", "why": "the autopilot runs the cycle",
            "then": "check again in 5 min while streams are live"}


def next_line(r: dict) -> str:
    return f"{r['do']} → {r['command']}"


# ---- L2: guides ---------------------------------------------------------------------------------------------

def guide_hashes(root: str = GUIDES) -> dict[str, str]:
    out = {}
    for f in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        if f.endswith(".md"):
            out[f] = hashlib.sha256(open(os.path.join(root, f), "rb").read()).hexdigest()[:12]
    return out


def guides_changed(root: str = GUIDES, seen_path: str = GUIDES_SEEN) -> list[str]:
    """guides/<name> for each guide new or changed since the agent last marked them read."""
    try:
        seen = json.load(open(seen_path))
    except (OSError, ValueError):
        seen = None
    now = guide_hashes(root)
    if seen is None:                              # first look on this install: everything is new, say it once
        return [f"guides/{k}" for k in now] if now else []
    return [f"guides/{k}" for k, v in now.items() if seen.get(k) != v]


def mark_guides_read(root: str = GUIDES, seen_path: str = GUIDES_SEEN) -> int:
    h = guide_hashes(root)
    os.makedirs(os.path.dirname(seen_path), exist_ok=True)
    with open(seen_path, "w") as fh:
        json.dump(h, fh)
    return len(h)


# ---- agent-check: what this host lets an agent do -----------------------------------------------------------

def agent_check() -> dict:
    """Which layer an agent on this host should use, from what the host can do."""
    shell = True                                   # this ran, so the agent has a shell
    sched = {"crontab": bool(shutil.which("crontab")), "systemd": bool(shutil.which("systemctl")),
             "launchd": bool(shutil.which("launchctl"))}
    try:
        from . import autopilot as AP
        auto = AP.autopilot_status_line()
    except Exception as exc:
        auto = f"unknown ({str(exc)[:60]})"
    git_push = subprocess.run(["git", "-C", REPO, "remote", "get-url", "origin"], capture_output=True).returncode == 0
    layers = ["L0 the autopilot runs the cycle on its own (`clipengine autopilot start`)",
              "L1 shell: `clipengine next` + `--json` on attention / queue / to-post / bank / doctor",
              "L2 guides: guides/README.md (one page per job)",
              "L3 MCP: point an MCP-capable agent at `clipengine mcp` (stdio)"]
    use = "L3 if your agent speaks MCP, else L1 + L2" if shell else "L3"
    return {"shell": shell, "scheduler": sched, "autopilot": auto, "git_remote": git_push,
            "guides": sorted(guide_hashes()), "layers": layers, "use": use,
            "mcp_command": f"{os.path.join(REPO, '.venv', 'bin', 'python')} -m clipengine mcp"}


def agent_check_lines(r: dict) -> list[str]:
    s = r["scheduler"]
    out = [f"shell: yes · scheduled tasks: " + (", ".join(k for k, v in s.items() if v) or "none found (the engine's keeper "
           "covers revival)") + f" · autopilot: {r['autopilot']} · git remote: {'yes' if r['git_remote'] else 'no'}"]
    out += [f"  {x}" for x in r["layers"]]
    out.append(f"use: {r['use']} · MCP command: {r['mcp_command']}")
    return out


def dumps(obj) -> str:
    return json.dumps(obj, indent=1, default=str, ensure_ascii=False)


def safe_id(text: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_:.\-]{1,80}", text or ""))
