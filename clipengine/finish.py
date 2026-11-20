"""The approved finish — a channel's look, frozen once approved, so no edit or engine update changes it silently.

  approve   `clipengine finish approve --name V3` records the look as it is now, as a new version:
            - the look settings from the brand pack (layout, card, quote, captions, logo);
            - every file the look uses (fonts, logo), by content hash, with a copy of each file kept beside the
              record so it can be restored byte for byte (a card style's code is recorded too, not gated);
            - a reference still: one frame rendered through the real pipeline from a fixed synthetic clip.
            The records live next to the brand pack (`<brand dir>/finish/`), so they belong to the channel.
  check     the look now vs the approved version: which settings changed, which files changed bytes or went
            missing. `--still` re-renders the reference frame and compares pixels — that is how an engine update
            that changes the look is caught (once per engine version; `doctor` shows the result).
  receipt   every render writes `<clip>.finish.json`: the finish version, its key, the settings and file hashes
            it was rendered with, and the engine version. The drop carries it, so each post says which finish it
            has.
  gate      with an approved finish, a clip rendered with a different look is not dropped: the refusal names
            what changed. Re-approve if the change is wanted (`finish approve`), or put the approved version
            back (`finish restore`).
  cache     a pre-render made under a different finish key — any setting, or the BYTES of any font or logo — is
            stale and is made again.

Without an approved finish nothing is checked; the receipts are still written.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import inspect
import json
import os
import shutil
import subprocess
import tempfile
import time

from . import brand as B

LOOK = ("layout", "card", "quote", "captions", "logo")
STILL_MEAN_MAX = 3.0             # mean pixel delta allowed between reference stills (encoder noise)
STILL_SHARE_MAX = 0.035          # share of pixels allowed to differ visibly (> 24 levels)
FACECAM_BOX = [0.04, 0.06, 0.28, 0.29]


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _rel(path: str) -> str:
    try:
        r = os.path.relpath(path, B.REPO)
        return path if r.startswith("..") else r
    except ValueError:
        return path


def folder() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(B.path())), "finish")


def look(pack: dict | None = None) -> dict:
    pack = pack or B.load(fresh=True)
    return {k: pack.get(k) for k in LOOK}


def files(pack: dict | None = None) -> dict[str, str]:
    """role → the file the look uses (absolute path)."""
    pack = pack or B.load(fresh=True)
    card, quote, caps, logo = (pack.get(k) or {} for k in ("card", "quote", "captions", "logo"))
    out = {"card.font": B.font(card.get("font") or ""), "quote.font": B.font(quote.get("font") or ""),
           "captions.font_file": B.font(caps.get("font_file") or "")}
    if card.get("label_font"):
        out["card.label_font"] = B.font(card["label_font"])
    if logo.get("file"):
        out["logo.file"] = B.asset(logo["file"])
    return out


def code(pack: dict | None = None) -> dict[str, dict]:
    """The code that draws a registered card style — recorded for the record, not gated: code changes with every
    engine update, and the reference frame is what tells whether the look did."""
    pack = pack or B.load(fresh=True)
    style = (pack.get("card") or {}).get("style")
    try:
        from . import card as CD
        fn = CD.styles().get(style) if style else None
        src = inspect.getsourcefile(fn) if fn else None
        return {f"card.style:{style}": {"path": _rel(src), "sha": _sha(src)[:16]}} if src else {}
    except Exception:
        return {}


def fingerprint(pack: dict | None = None) -> dict:
    """The finish as it is: settings hash, each file's content hash, and one key over both."""
    pack = pack or B.load(fresh=True)
    lk = look(pack)
    profile = hashlib.sha256(json.dumps(lk, sort_keys=True, default=str).encode()).hexdigest()[:16]
    assets = {}
    for role, path in sorted(files(pack).items()):
        assets[role] = {"path": _rel(path), "sha": _sha(path)[:16] if os.path.isfile(path) else "missing"}
    key = hashlib.sha256(json.dumps([profile, assets], sort_keys=True).encode()).hexdigest()[:16]
    return {"key": key, "profile": profile, "assets": assets, "code": code(pack)}


def approved() -> dict | None:
    try:
        return json.load(open(os.path.join(folder(), "approved.json")))
    except (OSError, ValueError):
        return None


def history() -> list[dict]:
    out = []
    try:
        with open(os.path.join(folder(), "history.jsonl")) as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def _engine_version() -> str:
    from . import __version__
    return __version__


def diff(a: dict, b: dict, look_a: dict | None = None, look_b: dict | None = None) -> list[str]:
    """Human lines: what differs between two fingerprints (a = approved, b = now)."""
    lines = []
    if a.get("profile") != b.get("profile"):
        if look_a is not None and look_b is not None:
            for k in LOOK:
                va, vb = look_a.get(k) or {}, look_b.get(k) or {}
                if va == vb:
                    continue
                if isinstance(va, dict) and isinstance(vb, dict):
                    keys = sorted(set(va) | set(vb), key=str)
                    ch = [f"{k}.{x} {va.get(x)!r}→{vb.get(x)!r}" for x in keys if va.get(x) != vb.get(x)]
                    lines += ch[:6]
                else:
                    lines.append(f"{k} changed")
        else:
            lines.append("look settings changed")
    ra, rb = a.get("assets") or {}, b.get("assets") or {}
    for role in sorted(set(ra) | set(rb)):
        x, y = ra.get(role), rb.get(role)
        if x == y:
            continue
        if y is None:
            lines.append(f"{role} no longer used (was {x['path']})")
        elif x is None:
            lines.append(f"{role} added ({y['path']})")
        elif y["sha"] == "missing":
            lines.append(f"{role} file missing ({y['path']})")
        elif x["path"] != y["path"]:
            lines.append(f"{role} is another file ({x['path']}→{y['path']})")
        else:
            lines.append(f"{role} bytes changed ({y['path']})")
    return lines


def check(pack: dict | None = None) -> dict:
    """{state: none | ok | drift, version, name, changes}"""
    ap = approved()
    if not ap:
        return {"state": "none", "version": None, "name": None, "changes": []}
    pack = pack or B.load(fresh=True)
    now = fingerprint(pack)
    if now["key"] == ap["fingerprint"]["key"]:
        return {"state": "ok", "version": ap["version"], "name": ap.get("name"), "changes": [], "key": now["key"]}
    return {"state": "drift", "version": ap["version"], "name": ap.get("name"), "key": now["key"],
            "changes": diff(ap["fingerprint"], now, ap.get("look"), look(pack))}


def label(ap: dict | None) -> str:
    if not ap:
        return "no approved finish"
    return f"v{ap['version']}" + (f" “{ap['name']}”" if ap.get("name") else "")


# ---- reference still --------------------------------------------------------------------------------------

def render_still(out_png: str, pack: dict | None = None) -> str:
    """One frame of the look, rendered through the real pipeline from a fixed synthetic 3 s clip."""
    from unittest import mock
    from . import render as V
    pack = pack or B.load(fresh=True)
    with tempfile.TemporaryDirectory(prefix="klondike-finish-") as tmp:
        src = os.path.join(tmp, "sample.mp4")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=3",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3", "-shortest",
                        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", src], check=True)
        mp4 = os.path.join(tmp, "still.mp4")
        kind = (pack.get("layout") or {}).get("kind", "stack")
        words = [{"w": " look", "start": 0.8, "end": 1.4}, {"w": " now", "start": 1.4, "end": 2.1}]
        with mock.patch.object(B, "load", return_value=pack), \
                mock.patch.object(V, "render_preset", return_value="ultrafast"):
            V._render(src, 0, 0, 3, words, mp4, layout=kind, facecam=FACECAM_BOX if kind == "facecam" else None,
                      title="They landed JUST IN TIME", quote="look now")
        os.makedirs(os.path.dirname(os.path.abspath(out_png)), exist_ok=True)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "1.5", "-i", mp4, "-frames:v", "1",
                        "-vf", "scale=270:480:flags=area", "-threads", "1", out_png], check=True)
    return out_png


def compare_stills(a: str, b: str) -> dict:
    import numpy as np
    from PIL import Image
    with Image.open(a) as ia, Image.open(b) as ib:
        x = np.asarray(ia.convert("RGB"), dtype=np.int16)
        y = np.asarray(ib.convert("RGB"), dtype=np.int16)
    if x.shape != y.shape:
        return {"same": False, "mean": None, "share": None, "why": "the frame's size changed"}
    d = np.abs(x - y)
    mean, share = float(d.mean()), float((d.max(axis=-1) > 24).mean())
    same = mean <= STILL_MEAN_MAX and share <= STILL_SHARE_MAX
    return {"same": same, "mean": round(mean, 2), "share": round(share, 4),
            "why": "" if same else f"{100 * share:.1f}% of the frame looks different (mean delta {mean:.1f})"}


def check_still(force: bool = False) -> dict | None:
    """Render the reference frame on this engine and compare it with the approved one. Remembered per engine
    version and finish key (runs once after each update unless forced). None without an approved still."""
    ap = approved()
    if not ap or not ap.get("still"):
        return None
    ref = os.path.join(folder(), ap["still"])
    if not os.path.isfile(ref):
        return {"same": False, "why": f"the approved still is missing ({ap['still']})"}
    from .admission import _dir
    memo_p = os.path.join(_dir(), "finish-verified.json")           # per machine (runs/heavy): never committed
    try:
        memo = json.load(open(memo_p))
    except (OSError, ValueError):
        memo = {}
    tag = f"{_engine_version()}:{ap['version']}:{fingerprint()['key']}"
    if not force and tag in memo:
        return memo[tag]
    with tempfile.TemporaryDirectory(prefix="klondike-finish-") as tmp:
        try:
            got = render_still(os.path.join(tmp, "now.png"))
            res = compare_stills(ref, got)
        except Exception as exc:                     # ffmpeg missing, a font gone: a finding, not a crash
            res = {"same": False, "why": f"could not render the reference frame ({str(exc)[:120]})"}
    res = {**res, "engine": _engine_version(), "at": time.time()}
    memo = {k: v for k, v in memo.items() if k.split(":")[1] == str(ap["version"])}
    memo[tag] = res
    try:
        os.makedirs(os.path.dirname(memo_p), exist_ok=True)
        with open(memo_p, "w") as fh:
            json.dump(memo, fh, indent=1)
    except OSError:
        pass
    return res


# ---- approve / restore -----------------------------------------------------------------------------------

def approve(name: str | None = None, note: str | None = None, still: bool = True) -> dict:
    pack = B.load(fresh=True)
    fp = fingerprint(pack)
    missing = [r for r, a in fp["assets"].items() if a["sha"] == "missing"]
    if missing:
        raise SystemExit("finish not approved: these files are missing — " + ", ".join(
            f"{r} ({fp['assets'][r]['path']})" for r in missing))
    d = folder()
    os.makedirs(os.path.join(d, "assets"), exist_ok=True)
    version = max([h.get("version", 0) for h in history()] + [0]) + 1
    kept = {}
    for role, path in files(pack).items():
        full = _sha(path)
        ext = os.path.splitext(path)[1]
        dest = os.path.join(d, "assets", full[:24] + ext)
        if not os.path.exists(dest):
            shutil.copy2(path, dest)
        kept[role] = os.path.relpath(dest, d)
    rec = {"version": version, "name": (name or "").strip() or None, "note": (note or "").strip() or None,
           "at": dt.datetime.now(dt.timezone.utc).isoformat(), "engine": _engine_version(),
           "fingerprint": fp, "look": look(pack), "kept": kept, "still": None}
    if still:
        rel = f"v{version}.png"
        try:
            render_still(os.path.join(d, rel), pack)
            rec["still"] = rel
        except Exception as exc:
            rec["still_error"] = str(exc)[:200]
    with open(os.path.join(d, "history.jsonl"), "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    tmp = os.path.join(d, "approved.json.tmp")
    with open(tmp, "w") as fh:
        json.dump(rec, fh, indent=1)
    os.replace(tmp, os.path.join(d, "approved.json"))
    return rec


def restore(version: int | None = None) -> list[str]:
    """Put an approved finish back: its look settings into the brand pack, and every file byte for byte.
    The version restored becomes the approved one again. Returns what was changed."""
    hs = history()
    rec = next((h for h in reversed(hs) if version is None or h.get("version") == version), None)
    if version is None:
        rec = approved() or rec
    if not rec:
        raise SystemExit("finish: nothing approved to restore" if version is None else f"finish: no v{version}")
    d, done = folder(), []
    for role, kept in (rec.get("kept") or {}).items():
        dest = rec["fingerprint"]["assets"][role]["path"]
        dest = dest if os.path.isabs(dest) else os.path.join(B.REPO, dest)
        src = os.path.join(d, kept)
        if os.path.isfile(dest) and _sha(dest)[:16] == rec["fingerprint"]["assets"][role]["sha"]:
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(src, dest)
        done.append(f"{role}: restored {_rel(dest)}")
    p = B.path()
    try:
        user = json.load(open(p))
    except (OSError, ValueError):
        user = {}
    changed = [k for k in LOOK if user.get(k) != rec["look"].get(k)]
    if changed:
        shutil.copy2(p, p + ".before-restore") if os.path.exists(p) else None
        user.update({k: rec["look"][k] for k in LOOK})
        with open(p + ".tmp", "w") as fh:
            json.dump(user, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(p + ".tmp", p)
        done.append("brand pack: " + ", ".join(changed) + " put back (previous pack saved as "
                    + os.path.basename(p) + ".before-restore)")
    if approved() is None or approved().get("version") != rec["version"]:
        with open(os.path.join(d, "approved.json"), "w") as fh:
            json.dump(rec, fh, indent=1)
        done.append(f"approved finish is {label(rec)} again")
    return done


# ---- receipts and the drop gate -----------------------------------------------------------------------

def receipt_path(clip: str) -> str:
    return clip.rsplit(".mp4", 1)[0] + ".finish.json"


def write_receipt(clip: str, stage: str = "render") -> dict | None:
    """What finish this clip was rendered with (written beside it)."""
    try:
        fp = fingerprint()
        ap = approved()
        rec = {"clip": os.path.basename(clip), "stage": stage, "key": fp["key"], "profile": fp["profile"],
               "assets": fp["assets"], "engine": _engine_version(), "at": time.time(),
               "approved": label(ap) if ap else None,
               "matches_approved": (ap["fingerprint"]["key"] == fp["key"]) if ap else None}
        with open(receipt_path(clip), "w") as fh:
            json.dump(rec, fh, indent=1)
        return rec
    except Exception:
        return None


def receipt(clip: str) -> dict | None:
    try:
        return json.load(open(receipt_path(clip)))
    except (OSError, ValueError):
        return None


def gate(clip: str) -> dict | None:
    """Before a drop: refuse a clip rendered with a look other than the approved finish. Returns the receipt."""
    ap = approved()
    rec = receipt(clip) or write_receipt(clip, stage="drop")
    if not ap or not rec:
        return rec
    if rec["key"] != ap["fingerprint"]["key"]:
        lines = diff(ap["fingerprint"], rec)
        raise SystemExit(f"NOT DROPPED: this clip was rendered with a finish other than the approved {label(ap)} — "
                         + "; ".join(lines[:4] or ["the finish key differs"])
                         + ". `clipengine finish check` shows it; `finish restore` puts the approved one back, "
                         "`finish approve` makes this the new one; then render again.")
    return rec


def lines() -> list[str]:
    """For doctor / finish check: the state in a line or two."""
    ap = approved()
    if not ap:
        return ["finish: none approved — `clipengine finish approve --name …` freezes the current look"]
    c = check()
    out = [f"finish: approved {label(ap)} ({ap['at'][:10]}, {len(ap['fingerprint']['assets'])} files)"
           + (" — matches" if c["state"] == "ok" else " — DRIFT: " + "; ".join(c["changes"][:3]))]
    return out
