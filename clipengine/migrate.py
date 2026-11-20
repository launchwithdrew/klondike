"""Moving the engine to another machine (spec §2, migration runbook) — without losing a clip, a post or a moment.

  clipengine migrate snapshot [--to DIR]     backup + finished clips + a checksum manifest (DIR: any folder, e.g. a
                                            mounted Synology share). Credentials never go in: they stay on the host
                                            they were authorised on.
  clipengine migrate verify FILE [--into D]  restore into an ISOLATED folder (never over this install) and check every
                                            checksum, that the ledger and the repeat guard read back with the same
                                            counts, the clips and the approved look are there
  clipengine migrate shadow                 on the NEW machine: capture and draft, but publishing is off here
  clipengine migrate release                on the machine that leads now: stop its autopilot, record the handoff
                                            and its last confirmed coverage watermark (data/migration.json, pushed)
  clipengine migrate takeover               on the new machine: becomes the one leader, publishing on, catch-up
                                            resumes from the released watermark
  clipengine migrate status                 this machine's role and the handoff record

Leadership moves once: after `release`, the old machine refuses to start its autopilot or publish until it takes
over again. **Rollback** is the same two steps the other way: `release` on the new machine, `takeover` on the old.
A machine with no migration record is the leader, as before.
"""
from __future__ import annotations

import datetime as dt
import glob
import hashlib
import json
import os
import shutil
import socket
import tempfile
import time
import uuid
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP_PATHS = ("brand", "config", "data", "ledger.jsonl", os.path.join("local", "patches"), "outbox")
SECRETS = ("chrome-profile", ".env", "cookies", "cookie", "credentials", "token", ".pem", ".key", "secret")
RECORD = os.path.join("data", "migration.json")       # shared (committed): who leads
ROLE = os.path.join("runs", "host-role.json")         # this install only: shadow or not
INSTALL = os.path.join("runs", "install-id")


def _r(root: str | None) -> str:
    return root or REPO


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _secret(rel: str) -> bool:
    low = rel.lower().replace("\\", "/")
    return any(s in low for s in SECRETS)


def install_id(root: str | None = None) -> str:
    """This install's identity (hostname + a random id kept in runs/, so two installs on one host differ)."""
    p = os.path.join(_r(root), INSTALL)
    try:
        return open(p).read().strip()
    except OSError:
        iid = f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write(iid)
        return iid


def _json(path: str) -> dict:
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return {}


def record(root: str | None = None) -> dict:
    return _json(os.path.join(_r(root), RECORD))


def role(root: str | None = None) -> str:
    """"leader" · "shadow" (captures, publishing off) · "released" (handed over: no autopilot, no publishing)."""
    if _json(os.path.join(_r(root), ROLE)).get("role") == "shadow":
        return "shadow"
    rec = record(root)
    if not rec:
        return "leader"
    me = install_id(root)
    if rec.get("leader") == me:
        return "leader"
    if rec.get("leader") is None and rec.get("released_by") != me:
        return "leader"                                 # released elsewhere, not yet taken: this one may take over
    return "released"


def may_publish(root: str | None = None) -> tuple[bool, str]:
    r = role(root)
    if r == "leader":
        return True, ""
    if r == "shadow":
        return False, "SHADOW HOST: publishing is off on this machine (`clipengine migrate takeover` makes it the leader)"
    rec = record(root)
    who = rec.get("leader") or "nobody yet"
    return False, (f"HANDED OVER: this machine released leadership ({who} leads) — publish there, or "
                   "`clipengine migrate takeover` here to roll back")


def may_lead(root: str | None = None) -> tuple[bool, str]:
    """The autopilot may run on the leader and on a shadow (it captures there); never on a released machine."""
    if role(root) != "released":
        return True, ""
    return False, may_publish(root)[1].replace("publish there", "its autopilot runs there")


def guard_publish(root: str | None = None) -> None:
    ok, why = may_publish(root)
    if not ok:
        raise SystemExit(why)


# ---- snapshot / verify --------------------------------------------------------------------------------

def _files(root: str | None) -> list[str]:
    out = []
    for p in SNAP_PATHS:
        full = os.path.join(_r(root), p)
        files = [full] if os.path.isfile(full) else sorted(
            f for f in glob.glob(os.path.join(full, "**", "*"), recursive=True) if os.path.isfile(f))
        for f in files:
            rel = os.path.relpath(f, _r(root))
            if "__pycache__" in rel or rel.endswith(".pyc") or _secret(rel):
                continue
            out.append(rel)
    return out


def _counts(root: str | None) -> dict:
    """What must read back the same after a restore: posts, published moments, attempts, finished clips."""
    def lines(p):
        try:
            return [json.loads(l) for l in open(os.path.join(_r(root), p)) if l.strip()]
        except (OSError, ValueError):
            return None
    led, clip, att = lines("ledger.jsonl"), lines(os.path.join("data", "clipped.jsonl")), \
        lines(os.path.join("data", "attempts.jsonl"))
    keys = {(c.get("login"), c.get("moment_at") or c.get("slug")) for c in clip or []}
    return {"ledger_posts": None if led is None else len(led), "clipped_moments": None if clip is None else len(keys),
            "attempts": None if att is None else len(att),
            "finished_clips": len(glob.glob(os.path.join(_r(root), "outbox", "*", "*.mp4"))),
            "approved_finish": os.path.exists(os.path.join(_r(root), "brand", "finish", "approved.json"))}


def snapshot(to: str | None = None, root: str | None = None, now: float | None = None) -> str:
    from . import __version__
    to = to or os.path.join(_r(root), "runs", "backups")
    os.makedirs(to, exist_ok=True)
    stamp = dt.datetime.fromtimestamp(now or time.time()).strftime("%Y%m%d-%H%M%S")
    name = os.path.join(to, f"migrate-{stamp}.zip")
    files = _files(root)
    cov = _json(os.path.join(_r(root), "runs", "coverage.json"))
    manifest = {"engine": __version__, "from": install_id(root), "at": now or time.time(),
                "files": {f: _sha(os.path.join(_r(root), f)) for f in files}, "counts": _counts(root),
                "coverage": cov, "note": "credentials are never included — sign in again on the new machine"}
    tmp = name + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(os.path.join(_r(root), f), f)
        z.writestr("MANIFEST.json", json.dumps(manifest, indent=1))
    os.replace(tmp, name)                               # a killed snapshot never looks complete
    print(f"migrate snapshot: {len(files)} files ({manifest['counts']['finished_clips']} finished clips, "
          f"{manifest['counts']['ledger_posts']} posts) → {name}")
    return name


def verify(path: str, into: str | None = None) -> dict:
    """Restore `path` into an isolated folder and check it. Returns {"ok", "checks": [(name, ok, detail)], "dir"}."""
    with zipfile.ZipFile(path) as z:
        man = json.loads(z.read("MANIFEST.json"))
        names = [n for n in z.namelist() if n != "MANIFEST.json" and not n.endswith("/")]
        bad = [n for n in names if os.path.isabs(n) or ".." in n.replace("\\", "/").split("/")]
        if bad:
            raise SystemExit(f"VERIFY REFUSED: {path} has a file outside the snapshot ({bad[0]})")
        dest = into or tempfile.mkdtemp(prefix="klondike-verify-")
        if os.path.realpath(dest) == os.path.realpath(REPO):
            raise SystemExit("VERIFY REFUSED: verify restores into an isolated folder, never over this install")
        stage = dest.rstrip("/") + ".staging"
        shutil.rmtree(stage, ignore_errors=True)
        z.extractall(stage, names)
    checks = []
    missing = [f for f in man["files"] if not os.path.exists(os.path.join(stage, f))]
    wrong = [f for f, h in man["files"].items() if f not in missing and _sha(os.path.join(stage, f)) != h]
    checks.append(("checksums", not missing and not wrong,
                   f"{len(man['files'])} files" + (f", {len(missing)} missing" if missing else "")
                   + (f", {len(wrong)} changed (e.g. {wrong[0]})" if wrong else "")))
    secrets = [n for n in names if _secret(n)]
    checks.append(("no credentials", not secrets, secrets[0] if secrets else "none in the snapshot"))
    got, want = _counts(stage), man["counts"]
    for k in ("ledger_posts", "clipped_moments", "attempts", "finished_clips", "approved_finish"):
        checks.append((k.replace("_", " "), got[k] == want[k], f"{got[k]} (source {want[k]})"))
    ok = all(c[1] for c in checks)
    if ok:                                              # only a fully checked restore takes the final name
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(stage, dest)
    return {"ok": ok, "checks": checks, "dir": dest if ok else stage, "manifest": man}


# ---- shadow / release / takeover -------------------------------------------------------------------------

def _save_record(rec: dict, root: str, msg: str, push: bool) -> None:
    p = os.path.join(_r(root), RECORD)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump(rec, open(p, "w"), indent=1)
    if push:
        from .publish import _commit
        _commit(p, msg, True)


def shadow(root: str | None = None) -> str:
    p = os.path.join(_r(root), ROLE)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump({"role": "shadow", "since": time.time()}, open(p, "w"))
    return (f"this machine ({install_id(root)}) is a SHADOW: it captures and drafts, publishing is off here. "
            "Compare its drafts with the leader's, then `migrate release` there and `migrate takeover` here.")


def release(root: str | None = None, push: bool = True, stop=None) -> str:
    if role(root) != "leader":
        raise SystemExit(f"migrate release: this machine isn't the leader ({role(root)})")
    if stop is None:
        from . import autopilot as AP
        stop = AP.autopilot_stop
    stopped = stop()
    cov = _json(os.path.join(_r(root), "runs", "coverage.json"))
    rec = record(root)
    hist = rec.get("history", [])
    hist.append({"released_by": install_id(root), "at": time.time(), "coverage": cov})
    _save_record({"leader": None, "released_by": install_id(root), "released_at": time.time(), "coverage": cov,
                  "history": hist[-20:]}, root, f"migrate: {install_id(root)} released leadership", push)
    return (f"released: {stopped}; handoff recorded with coverage up to "
            f"{_hm(cov.get('catchup_until'))} — run `clipengine migrate takeover` on the new machine")


def takeover(root: str | None = None, push: bool = True, start=None) -> str:
    rec = record(root)
    me = install_id(root)
    if rec.get("leader") == me and role(root) == "leader":
        return f"already the leader ({me})"
    if rec.get("leader") not in (None, me):
        raise SystemExit(f"migrate takeover: {rec['leader']} still leads — run `clipengine migrate release` there first")
    if not rec:
        raise SystemExit("migrate takeover: no handoff on record — `clipengine migrate release` on the leading machine "
                         "first (and `git pull` here)")
    try:
        os.remove(os.path.join(_r(root), ROLE))             # no longer a shadow
    except OSError:
        pass
    cov = rec.get("coverage") or {}
    cp = os.path.join(_r(root), "runs", "coverage.json")
    mine = _json(cp)
    # resume from the RELEASED machine's last confirmed watermark — never skip what it didn't cover
    merged = {k: cov.get(k) or mine.get(k) for k in ("catchup_until", "sweep_until")}
    os.makedirs(os.path.dirname(cp), exist_ok=True)
    json.dump({k: v for k, v in merged.items() if v}, open(cp, "w"))
    hist = rec.get("history", [])
    hist.append({"leader": me, "at": time.time()})
    _save_record({"leader": me, "since": time.time(), "coverage": cov, "history": hist[-20:]}, root,
                 f"migrate: {me} took over leadership", push)
    if start is None:
        from . import autopilot as AP
        start = AP.autopilot_start
    started = start()
    return (f"{me} is the leader: publishing on, catch-up resumes from {_hm(merged.get('catchup_until'))}; {started}")


def _hm(t) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(t))) if t else "no watermark (catch-up asks for --since)"


def status_lines(root: str | None = None) -> list[str]:
    rec = record(root)
    out = [f"this machine: {install_id(root)} — {role(root)}"]
    if rec:
        out.append(f"leader on record: {rec.get('leader') or 'none (released by ' + str(rec.get('released_by')) + ')'}")
        for h in rec.get("history", [])[-4:]:
            what = f"{h['leader']} took over" if h.get("leader") else f"{h.get('released_by')} released"
            out.append(f"  {time.strftime('%m-%d %H:%M', time.localtime(h['at']))} {what}")
    else:
        out.append("no migration on record (this machine leads)")
    return out
