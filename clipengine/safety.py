"""Brand-safety screen — sexual content, the thing platforms flag first.

Censoring swear words does not make a clip safe: a clip can bleep every curse
and still be about sex. This screen reads what the clip is ABOUT:
the transcript, the viewer clips' titles ("freaky", "Strip Club"), the
chat around the moment, and the headline we plan to put on it.

It flags, it does not decide: a flagged moment is pushed far down the ranking
with the reason shown, and render refuses a flagged clip unless overridden.
GTA VI also has strip clubs and nudity on screen — the thumbnails on the review
page are the only check for that; a person must look.
"""
from __future__ import annotations

import re

# whole-word / stem patterns — sexual topics, not just profanity
_SEXUAL = [
    r"\bsex\w*", r"\bfreaky\b", r"\bfreak\b", r"\bhorny\b", r"\bnudes?\b", r"\bnaked\b", r"\bstrip(per|pers|club)?\b",
    r"\bstrip club\b", r"\blap ?dance\w*", r"\bpole danc\w*", r"\bthot\w*", r"\bhoes?\b", r"\bslut\w*", r"\bwhore\w*",
    r"\bbooty\b", r"\bthick\b", r"\bdick\w*", r"\bcock\w*", r"\bpuss(y|ies)\b", r"\btits?\b", r"\btitties\b", r"\bboobs?\b",
    r"\bnut(ted|ting)?\b", r"\bsmash(ed|ing)?\b", r"\bhook ?up\w*", r"\bonlyfans\b", r"\bporn\w*", r"\bbang(ed|ing)\b",
    r"\bfucking each other\b", r"\bprison wife\b", r"\bmain bitch\b", r"\bside (bitch|chick|piece)\b",
    r"\bso (fucking |f\*+ing )?hot\b", r"\b(that|her|them|dat) ass\b", r"\bass was\b", r"\bjiggl\w*", r"\bcondom\w*", r"\berect\w*", r"\bmoan\w*", r"\bgyat+\b", r"\brizz\w*", r"\bgoon\w*",
]
_RE = re.compile("|".join(_SEXUAL), re.I)


def scan_text(*texts: str) -> list[str]:
    hits = []
    for t in texts:
        for m in _RE.finditer(t or ""):
            w = m.group(0).lower()
            if w not in hits:
                hits.append(w)
    return hits


def screen(transcript: str = "", titles: list[str] | None = None, chat_lines: list[str] | None = None,
           headline: str = "") -> dict:
    """{'flagged': bool, 'why': str, 'hits': [...]}. Two or more distinct sexual
    terms in what is SAID, or any in a viewer-clip title / our headline, flags it.
    Chat alone needs a majority-ish signal (chat jokes about everything)."""
    said = scan_text(transcript)
    named = scan_text(" ".join(titles or []), headline)
    chat = scan_text(" ".join(chat_lines or []))
    flagged = len(said) >= 2 or bool(named) or len(chat) >= 3
    why = []
    if said:
        why.append("said: " + ", ".join(said[:5]))
    if named:
        why.append("titled: " + ", ".join(named[:3]))
    if chat:
        why.append("chat: " + ", ".join(chat[:3]))
    return {"flagged": flagged, "why": "; ".join(why), "hits": said + named + chat}
