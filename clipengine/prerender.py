"""Pre-rendered drafts — so `publish-queued` only burns the headline card and posts.

Every queued draft that can clear some platform's bar gets its final render made in the background, as
soon as it is drafted: the brand layout (stack / facecam), captions with the game's lines left out,
bleeps, the logo — everything but the headline card, which needs the agent's words. The censor audit
(the render's own audio, transcribed again) and the vision check run on it then too.

`publish-queued` uses it when the publish asks for nothing the pre-render can't give (no --fix, no
--bleep, a trim inside the draft): it overlays the card, trimmed, in one short encode — seconds, not
minutes. Anything else falls back to the full render, as before.

  clipengine prerender --pending     every eligible draft that has none yet (cycle runs this in the background)
  clipengine prerender --id ID       one draft
"""
from __future__ import annotations

import hashlib
import json
import os
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _floor() -> float:
    from . import gate as GT
    return min(r["min_score"] for r in GT.config()["platforms"].values())


def brand_key() -> str:
    """A pre-render made under another look is stale: the brand pack changed, or the BYTES of a font, the logo or
    a card style's code did (finish.py)."""
    from . import brand as B
    from . import finish as FN
    try:
        fk = FN.fingerprint()["key"]
    except Exception:
        fk = ""
    return hashlib.sha1((json.dumps(B.load(fresh=True), sort_keys=True, default=str) + fk).encode()).hexdigest()[:12]


def paths(entry: dict) -> tuple[str, str]:
    d = os.path.join(entry["rec"], "drafts")
    return os.path.join(d, entry["id"] + ".pre.mp4"), os.path.join(d, entry["id"] + ".pre.json")


def eligible(entry: dict, floor: float | None = None, max_age_h: float = 6.0) -> bool:
    """A recording draft worth a look: new, safe, fresh, able to clear some platform's bar."""
    if entry.get("scan") or not entry.get("rec") or entry.get("payoff") is None:
        return False                                   # catch-up (VOD) drafts render through the scan path
    if entry.get("status") != "new" or entry.get("unsafe"):
        return False
    if time.time() - entry.get("stop", 0) > max_age_h * 3600:
        return False
    return entry.get("best", 0) >= (_floor() if floor is None else floor)


def load(entry: dict) -> dict | None:
    mp4, meta = paths(entry)
    try:
        m = json.load(open(meta))
    except (OSError, ValueError):
        return None
    return m if os.path.isfile(mp4) and m.get("ok") else None


def prerender(entry: dict, listen: bool = True, force: bool = False) -> dict | None:
    """Render the draft's window in the channel's look without the card. Returns the meta (None when it
    is already being made elsewhere)."""
    from . import live as L
    from . import render as V
    from . import facecam as FC
    from . import censor as CN
    from . import vision as VI
    from . import sentinel as SN
    mp4, meta_path = paths(entry)
    have = load(entry)
    key = brand_key()
    if have and not force and have.get("brand") == key:
        return have
    lock = meta_path + ".lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode()); os.close(fd)
    except FileExistsError:
        if time.time() - os.path.getmtime(lock) < 900:
            return None                                # another process is on it
        os.remove(lock)
        return prerender(entry, listen, force)
    t0 = time.time()
    try:
        s, e = float(entry["start"]), float(entry["stop"])
        words = SN.draft_words(entry, s, e)
        src = mp4 + ".src.ts"
        first = L.cut_source(entry["rec"], s - 3, e + 3, src)
        if words is None:                              # no saved transcript: transcribe like a publish would
            from . import audio as A
            from . import ops as O
            x = A.decode_pcm(src, 0, None)
            early = A.gate(x, first, entry.get("payoff"), route="prerender")
            if early.observation in A.SKIP_OBS:
                raise RuntimeError(A.skip_reason(early.observation))
            words = A.real_words(A.transcribe(x, O.whisper_model(), offset=first))
        tmp = mp4 + ".part.mp4"
        try:
            with FC.creator(entry.get("login")):
                info = V.render(src, first, s, e, words, tmp, crop="blur", layout="stack",
                                watermark=V.WATERMARK, drop_captions=SN.game_spans(words) or None,
                                captions_later=True)
        finally:
            try:
                os.remove(src)
            except OSError:
                pass
        got = L.media_duration(tmp)
        if got and got < (e - s) - 1.5:
            raise RuntimeError(f"pre-render came out {(e - s) - got:.1f} s short")
        os.replace(tmp, mp4)
        os.replace(tmp.rsplit(".mp4", 1)[0] + ".transcript.json", mp4.rsplit(".mp4", 1)[0] + ".transcript.json")
        caps_json = None
        if info.get("captions_later") and os.path.exists(info["captions_later"]):
            caps_json = mp4.rsplit(".mp4", 1)[0] + ".captions.json"     # burned with the card at publish
            os.replace(info["captions_later"], caps_json)
        for ext in (".ass", ".srt"):
            try:
                os.remove(tmp.rsplit(".mp4", 1)[0] + ext)
            except OSError:
                pass
        audit = {"ok": True, "fixed": [], "skipped": True}
        if listen and os.environ.get("CLIPENGINE_NO_AUDIT") != "1":
            a = CN.ensure_clean(mp4)
            audit = {"ok": bool(a["ok"]), "fixed": a.get("fixed", []), "captions": a.get("captions", []),
                     "audio": [w.get("w") for w in a.get("audio", [])]}
        vis = VI.check(mp4)
        meta = {"id": entry["id"], "mp4": mp4, "start": s, "stop": e, "slot": info.get("card_slot") or {},
                "captions": caps_json,
                "layout": info.get("layout"), "facecam": info.get("facecam"), "watermark": info.get("watermark"),
                "bleeps": info.get("bleeps"), "audit": audit,
                "vision": {k: vis[k] for k in ("cuts_per_min", "static", "dark", "flags")},
                "brand": key, "version": _version(), "secs": round(time.time() - t0, 1),
                "at": time.time(), "ok": True}
        json.dump(meta, open(meta_path, "w"), indent=1)
        SN.note(entry, pre=mp4)
        return meta
    except Exception as exc:
        json.dump({"id": entry["id"], "ok": False, "error": str(exc)[:300], "at": time.time()}, open(meta_path, "w"))
        raise
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass


def _version() -> str:
    from . import __version__
    return __version__


def usable(entry: dict, start: float, stop: float, fixes: dict | None = None,
           extra_censor: set | None = None) -> dict | None:
    """The pre-render, when this publish can use it: same look, nothing re-captioned, window inside it."""
    if fixes or extra_censor:
        return None                                    # a caption fix / extra bleep changes the render itself
    m = load(entry)
    if not m or m.get("brand") != brand_key() or not (m.get("audit") or {}).get("ok"):
        return None
    if start < m["start"] - 0.05 or stop > m["stop"] + 0.05 or stop - start < 3:
        return None
    return m


def pending(root: str | None = None) -> list[dict]:
    from . import sentinel as SN
    floor = _floor()
    rows = SN.load_queue(root) if root else SN.load_queue()
    return [r for r in rows if eligible(r, floor) and not load(r) and not _failed_recently(r)]


def _failed_recently(entry: dict) -> bool:
    try:
        m = json.load(open(paths(entry)[1]))
    except (OSError, ValueError):
        return False
    return not m.get("ok") and time.time() - m.get("at", 0) < 1800


def run_pending(root: str | None = None, ids: list[str] | None = None) -> list[str]:
    from . import sentinel as SN
    rows = [r for r in SN.load_queue() if r["id"] in ids] if ids else pending(root)
    out = []
    for r in rows:
        try:
            m = prerender(r, force=bool(ids))
            out.append(f"{r['id']}: " + (f"pre-rendered in {m['secs']} s" if m else "already being made"))
        except Exception as exc:
            out.append(f"{r['id']}: pre-render failed ({str(exc)[:100]}) — publish-queued renders it in full")
        print(out[-1], flush=True)
    return out
