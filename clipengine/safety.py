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
    r"\btwe{2,}k(?:ing|in)\b", r"\bthongs?\b", r"\bbikini\w*", r"\bdump ?truck\w*", r"\bcaked?( up)?\b", r"\bbussy\b", r"\bthicc\w*",
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
    flagged = "prison wife" in said or len(said) >= 2 or bool(named) or chat_hot
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


_NUMBERS = {word: number for number, word in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen".split())}
_NUMBERS.update({word: number * 10 for number, word in enumerate(
    "twenty thirty forty fifty sixty seventy eighty ninety".split(), 2)})
_SCALES = {"hundred": 100, "thousand": 1000, "million": 1000000, "billion": 1000000000}
_DIGIT = re.compile(r"(?<![a-z0-9])\d+(?:,\d{3})*(?:\.\d+)?(?![a-z0-9])", re.I)


def _spoken_numbers(text: str) -> set[str]:
    """Normalize digit and spoken cardinal amounts without accepting a substring of a larger amount."""
    tokens = re.findall(r"\d+(?:,\d{3})*(?:\.\d+)?|[a-z]+|[^\s-]", text.lower())
    values = set()
    index = 0
    while index < len(tokens):
        word = tokens[index]
        index += 1
        if word[0].isdigit():
            values.add(word.replace(",", ""))
            continue
        if word not in _NUMBERS:
            continue
        subtotal, total = _NUMBERS[word], 0
        previous = word
        while index < len(tokens):
            word = tokens[index]
            if word == "and" and previous in _SCALES and index + 1 < len(tokens) and tokens[index + 1] in _NUMBERS:
                index += 1
                continue
            if word in _SCALES:
                if word == "hundred":
                    subtotal *= 100
                else:
                    total += subtotal * _SCALES[word]
                    subtotal = 0
            elif word in _NUMBERS and (previous in _SCALES or _NUMBERS[previous] >= 20 > _NUMBERS[word]):
                subtotal += _NUMBERS[word]
            else:
                break
            previous = word
            index += 1
        values.add(str(total + subtotal))
    return values


def headline_support(title: str, transcript: str = "", titles: list[str] | None = None,
                     names: list[str] | None = None) -> list[str]:
    """Claim words and exact digit amounts missing from speech or viewer-clip titles."""
    def words(text):
        return re.findall(r"[a-z']+", (text or "").lower())

    evidence = [transcript or "", *(titles or [])]
    have = {w.strip("'") for text in evidence for w in words(text)}
    stems = {w[:5] for w in have if len(w) >= 4}
    skip = {word for name in names or [] for word in words(name)}
    missing = []
    non_claims = {"all", "any", "did", "doe", "new", "now", "not", "one", "see", "too", "try", "wow", "yes", "you"}
    for word in words(title):
        word = word.strip("'")
        if len(word) < 3 or word in _STOP or word in non_claims or word in skip or word.removesuffix("'s") in skip:
            continue
        if word in have or word[:5] in stems or word + "s" in have or word.removesuffix("s") in have:
            continue
        if word not in missing:
            missing.append(word)
    amounts = set().union(*map(_spoken_numbers, evidence))
    amount_title = re.sub(r"\bgta\s*6\b", "", title, flags=re.I)
    for m in _DIGIT.finditer(amount_title):
        amount = m.group()
        if amount.replace(",", "") not in amounts and amount not in missing:
            missing.append(amount)
    return missing
