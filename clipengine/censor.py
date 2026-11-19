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
