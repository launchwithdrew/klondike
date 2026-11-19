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
    r"\bso (fucking |f\*+ing )?hot\b", r"\b(that|her|them|dat) ass\b", r"\bass was\b", r"\bjiggl\w*", r"\bcondom\w*", r"\berect\w*", r"\bmoan\w*", r"\bgy+a+t+\b", r"\brizz\w*", r"\bgoon\w*",
    r"\bthongs?\b", r"\bbikini\w*", r"\bdump ?truck\w*", r"\bcaked?( up)?\b", r"\bbussy\b", r"\bthicc\w*",
    "\U0001F351", "\U0001F975", "\U0001F346", "\U0001F4A6",            # peach, hot face, eggplant, droplets
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
    # chat jokes about everything, so one word is noise — but several people on it is the moment's topic
    chat_msgs = sum(1 for c in chat_lines or [] if _RE.search(c or ""))
    chat_hot = len(chat) >= 3 or chat_msgs >= max(3, 0.03 * len(chat_lines or []))
    flagged = len(said) >= 2 or bool(named) or chat_hot
    why = []
    if said:
        why.append("said: " + ", ".join(said[:5]))
    if named:
        why.append("titled: " + ", ".join(named[:3]))
    if chat:
        why.append("chat: " + ", ".join(chat[:3]))
    return {"flagged": flagged, "why": "; ".join(why), "hits": said + named + chat,
            "chat_hits": chat, "chat_msgs": chat_msgs}


_STOP = set("""a an the and or but so of to in on at for with by from up down out over is are was were be been it its
this that these those he she they them his her their him i me my we our you your just like when what who how why
got get gets gone goes go going after before into off than then very really gta gta6 6 vi""".split())


def headline_support(title: str, transcript: str = "", titles: list[str] | None = None,
                     names: list[str] | None = None) -> list[str]:
    """Key words of our headline that appear in neither what was said nor the viewers' clip titles —
    a nudge to re-check it says what is ON SCREEN (rules/headlines.md). Empty = every key word is backed.
    Word stems match ("arrested" ~ "arrest"); emoji, numbers, stop words and names are skipped."""
    norm = lambda t: re.findall(r"[a-z']+", (t or "").lower())
    have = set(norm(transcript)) | set(norm(" ".join(titles or [])))
    stems = {w[:5] for w in have if len(w) >= 4}
    skip = {n.lower() for n in names or []}
    missing = []
    for w in norm(title):
        w = w.strip("'")
        if len(w) < 4 or w in _STOP or w in skip or w.endswith("'s") and w[:-2] in skip:
            continue
        if w in have or w[:5] in stems:
            continue
        if w not in missing:
            missing.append(w)
    return missing
