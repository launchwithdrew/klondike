"""Censor list — copied VERBATIM in behaviour from v1 edit-pipeline/scripts/caption.py
(is_profane, incl. the "fag" and exact-match "ass" additions).

Rule unchanged: never post a clip with an uncensored slur/profanity in
captions or audio. Add a word here AND in caption.py until v1 is retired;
tests/test_censor.py pins the cases from the training reports.
"""
from __future__ import annotations

import re

_WORD_RE = re.compile(r"[A-Za-z']+")


def is_profane(core: str) -> bool:
    c = core.lower().strip("'")
    if not c:
        return False
    if "nigg" in c:
        return True
    if "fuck" in c or "shit" in c or "fag" in c or "bitch" in c:
        return True
    if c in ("cunt", "cunts", "dick", "dicks", "pussy", "tits", "tit"):
        return True
    if c in ("ass", "asses", "asshole", "assholes", "jackass", "jackasses",
             "dumbass", "dumbasses"):   # exact match only — never "class"/"pass"
        return True
    return False


def censor_text(text: str) -> str:
    return _WORD_RE.sub(lambda m: "***" if is_profane(m.group(0).strip("'").lower())
                        else m.group(0), text)


def censor_words(words: list[dict], pad: float = 0.05, extra: set[str] | None = None) -> tuple[list[dict], list[list[float]]]:
    """Return (censored copies of words, merged bleep intervals). `extra` =
    per-clip words to bleep on top of the list (e.g. "balls" in a clip about
    nothing sexual, where it still reads crude)."""
    out, iv = [], []
    EXTRA = {x.lower() for x in (extra or set())}
    for w in words:
        w2 = dict(w)
        m = _WORD_RE.search(w["w"])
        if m and (is_profane(m.group(0).strip("'").lower()) or m.group(0).strip("'").lower() in EXTRA):
            w2["w"] = _WORD_RE.sub("***", w["w"], count=1)
            iv.append([max(0.0, w["start"] - pad), w["end"] + pad])
        out.append(w2)
    merged: list[list[float]] = []
    for s, e in sorted(iv):
        if merged and s <= merged[-1][1] + 0.05:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return out, merged


def apply_fixes(words: list[dict], fixes: dict | None) -> list[dict]:
    """Correct Whisper mis-hearings in the captions: {"all equipped": "I'll equip"}.
    Matches whole words, case- and punctuation-insensitive; the replacement keeps the
    original timing (extra words share the last word's slot, fewer words drop the rest)."""
    import re as _re
    norm = lambda t: _re.sub(r"[^\w?']", "", t.strip().lower())
    out = list(words)
    for wrong, right in (fixes or {}).items():
        wa, ra = [norm(x) for x in wrong.split()], right.split()
        i = 0
        while i <= len(out) - len(wa):
            if wa and [norm(w["w"]) for w in out[i:i + len(wa)]] == wa:
                span = out[i:i + len(wa)]
                new = []
                for k, r in enumerate(ra):
                    base = span[min(k, len(span) - 1)]
                    new.append(dict(base, w=" " + r))
                if len(ra) > len(span):                  # squeeze extra words into the last slot
                    last = span[-1]; n = len(ra) - len(span) + 1
                    step = (last["end"] - last["start"]) / n
                    for j in range(n):
                        new[len(span) - 1 + j] = dict(new[len(span) - 1 + j], start=last["start"] + j * step,
                                                      end=last["start"] + (j + 1) * step)
                out[i:i + len(wa)] = new
                i += len(new)
            else:
                i += 1
    return out


# ---- completeness: check what was actually rendered -----------------------------------------------

def audit(mp4: str, listen: bool = True, model_name: str = "tiny") -> dict:
    """Check a rendered clip against the censor list, in both places a word can slip through:
      captions  every caption card (<clip>.transcript.json) must be clean
      audio     the clip's own audio, transcribed again: a profane word heard outside every bleep
                interval means the main transcript misheard it, so it was never bleeped.
    -> {"ok", "captions": [words], "audio": [{"w", "start", "end"}] (clip seconds), "bleeps": [...]}"""
    import json
    import os
    base = mp4.rsplit(".mp4", 1)[0]
    try:
        tr = json.load(open(base + ".transcript.json"))
    except (OSError, ValueError):
        tr = {"segments": [], "bleep_intervals": []}
    bleeps = [list(b) for b in tr.get("bleep_intervals", [])]
    caps = []
    for seg in tr.get("segments", []):
        for m in _WORD_RE.finditer(seg.get("text", "")):
            if is_profane(m.group(0)) and m.group(0) not in caps:
                caps.append(m.group(0))
    heard = []
    if listen and os.path.exists(mp4):
        from . import audio as A
        try:
            x = A.decode_pcm(mp4)
            for w in A.all_words(A.transcribe(x, model_name, prompt=None)):
                m = _WORD_RE.search(w["w"])
                if not m or not is_profane(m.group(0)):
                    continue
                if any(a - 0.2 <= w["start"] and w["end"] <= b + 0.2 for a, b in bleeps):
                    continue
                heard.append({"w": m.group(0), "start": round(w["start"], 2), "end": round(w["end"], 2)})
        except Exception as exc:
            return {"ok": not caps, "captions": caps, "audio": [], "bleeps": bleeps, "note": f"audio not checked: {exc}"}
    return {"ok": not caps and not heard, "captions": caps, "audio": heard, "bleeps": bleeps}


def bleep_file(mp4: str, intervals: list[list[float]], pad: float = 0.08) -> list[list[float]]:
    """Silence extra intervals (clip seconds) in a rendered clip — audio re-encoded, video copied — and
    record them in its transcript.json. Returns every bleep interval now in the clip."""
    import json
    import os
    import subprocess
    base = mp4.rsplit(".mp4", 1)[0]
    iv = [[max(0.0, a - pad), b + pad] for a, b in intervals]
    tmp = base + ".rebleep.mp4"
    af = ",".join(f"volume=0:enable='between(t,{a:.2f},{b:.2f})'" for a, b in iv)
    p = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", mp4, "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy",
                        "-af", af, "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", tmp],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError("re-bleep failed: " + p.stderr[-400:])
    os.replace(tmp, mp4)
    try:
        tr = json.load(open(base + ".transcript.json"))
    except (OSError, ValueError):
        tr = {"segments": []}
    allb = sorted([list(b) for b in tr.get("bleep_intervals", [])] + iv)
    tr["bleep_intervals"] = allb
    json.dump(tr, open(base + ".transcript.json", "w"), indent=2)
    return allb


def ensure_clean(mp4: str, rerender=None, listen: bool = True) -> dict:
    """audit -> fix -> audit again. Audio gaps are bleeped in place; caption gaps re-render through
    `rerender(extra_words)` when given. Returns the last audit plus what was fixed ("fixed")."""
    rep = audit(mp4, listen)
    fixed = []
    if rep["captions"] and rerender:
        rerender({w.lower() for w in rep["captions"]})
        fixed.append("captions re-rendered: " + ", ".join(rep["captions"]))
        rep = audit(mp4, listen)
    if rep["audio"]:
        bleep_file(mp4, [[w["start"], w["end"]] for w in rep["audio"]])
        fixed.append("audio re-bleeped at " + ", ".join(f"{w['start']:.1f}s" for w in rep["audio"]))
        rep = audit(mp4, listen)
    rep["fixed"] = fixed
    return rep
