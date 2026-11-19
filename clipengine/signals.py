"""Chat signals: what chat is DOING in each window, not just how loud it is.

v1 ranked by one number — messages per 10 s divided by the trailing median.
The training reports list exactly how that number lies:

  * greetings at stream start ("yo" 24x, "hi", "o7")
  * command spam ("!play" 226x — chat playing a mini-game)
  * one-token emote floods ("wicked" 81x, "waga" 57x)
  * chat agreeing with a question ("yes" 59x) — not an event
  * low-baseline ratio inflation (median 5 -> 11x on 55 msgs)
  * high-baseline hours where nothing clears 2.5x (median 62)

So every window gets features — distinct chatters, laughter, hype, shock,
"clip it" calls, quotes repeated by many people (content), and the junk
families above — and a score built from them. The score uses distinct
CHATTERS (one spammer counts once) against a robust local baseline
(median + MAD of the previous 5 minutes), so a quiet chat and a firehose
chat are judged on the same scale without any threshold step-downs.
"""
from __future__ import annotations

import math
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field

WINDOW_S = 10
BASELINE_WINDOWS = 30        # 5 minutes of trailing context
MIN_BASELINE = 6

# --- lexicons (lower-case, whole tokens) --------------------------------------
LAUGH = {
    "lol", "lmao", "lmfao", "lmaoo", "lmaooo", "kekw", "kek", "omegalul", "lul", "lulw",
    "icant", "xd", "xdd", "haha", "hahaha", "ahaha", "rofl", "dead", "deadass", "om",
    "pepelaugh", "pepega", "😂", "🤣", "💀", "☠️", "😭", "kekl", "lmfaooo", "lolol",
    "ha", "hah", "jajaja", "kekwait",
}
HYPE = {
    "w", "ww", "www", "wwww", "pog", "pogg", "poggers", "pogchamp", "pogu", "letsgo",
    "lets", "letsgoo", "letsgooo", "hype", "goat", "🔥", "🐐", "insane", "clean",
    "clutch", "gg", "ez", "huge", "massive", "sheesh", "peak", "cinema", "kino",
    "banger", "fire", "elite", "valid", "based",
}
SHOCK = {
    "?", "??", "???", "????", "what", "wtf", "wth", "omg", "nah", "naw", "noway",
    "bro", "broo", "brooo", "holy", "monkas", "monkaw", "😳", "😱", "🤯", "wait",
    "huh", "yikes", "bruh", "ayo", "ayoo", "sus", "nooo", "noo", "no", "stop",
    "damn", "dang", "jesus", "oh", "ohh", "ohhh", "l", "ll", "ratio", "crazy",
    "unreal", "actually", "what?", "wtf?", "huhh",
}
CLIP = {"clip", "clipped", "clipit", "clipthat", "cliped", "clips"}
GREET = {
    "hi", "hii", "hiii", "hello", "hey", "heyy", "yo", "yoo", "yooo", "sup", "wassup",
    "gm", "morning", "gn", "goodnight", "night", "bye", "byee", "cya", "o7", "peace",
    "welcome", "wb", "back", "hiya", "howdy", "waddup", "first", "early", "late",
}
GREET_PHRASES = ("good morning", "good night", "we back", "im back", "i'm back",
                 "see you", "see ya", "have a good", "thanks for the stream",
                 "great stream", "good stream", "gn chat", "bye chat", "hi chat")
POLL = {"yes", "yeah", "yep", "no", "1", "2", "y", "n", "true", "real", "a", "b"}
SUPPORT = ("gifted", "gift sub", "subscribed", "thanks for the sub", "bits", "cheer",
           "prime", "resub", "hype train")

_TOKEN = re.compile(r"[\w']+|[?!]+|[^\w\s]", re.UNICODE)


def tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text)]


def classify(msg: dict) -> set[str]:
    """Families a single message belongs to (it can be several)."""
    text = (msg.get("text") or "").strip()
    low = text.lower()
    fam: set[str] = set()
    if not text:
        return {"empty"}
    if low.startswith("!"):
        return {"command"}
    toks = tokens(text)
    words = [t for t in toks if re.match(r"\w", t)]
    tset = set(toks)
    # collapse elongations: "lmaooooo" -> "lmaoo", "Wwwww" -> "www"
    norm = {re.sub(r"(.)\1{2,}", r"\1\1", t) for t in tset}
    norm |= tset
    if norm & LAUGH or re.search(r"(ha){2,}|lmf?ao+|😂|🤣|💀", low):
        fam.add("laugh")
    if norm & HYPE or re.fullmatch(r"w+", low) or "lets go" in low or "let's go" in low:
        fam.add("hype")
    if norm & SHOCK or re.fullmatch(r"[?!. ]+", low) or "no way" in low or "oh my" in low:
        fam.add("shock")
    if norm & CLIP or "clip it" in low or "clip that" in low:
        fam.add("clip")
    if any(p in low for p in SUPPORT):
        fam.add("support")
    # greetings only when the message is SHORT and mostly greeting
    if (len(words) <= 4 and set(words) & GREET) or any(p in low for p in GREET_PHRASES):
        fam.add("greet")
    if msg.get("emotes", 0) and len(words) <= msg.get("emotes", 0):
        fam.add("emote_only")
    if len(words) >= 3 and not fam - {"emote_only"}:
        fam.add("talk")
    return fam or {"other"}


def _canon(text: str) -> str:
    """Canonical form for repetition counting: lower, de-elongated, de-spaced."""
    t = re.sub(r"(.)\1{2,}", r"\1\1", text.lower().strip())
    return re.sub(r"\s+", " ", t)


@dataclass
class Window:
    start_s: float
    n: int = 0
    users: int = 0
    fam: Counter = field(default_factory=Counter)
    top_text: str = ""
    top_share: float = 0.0       # share of messages that are the single most common text
    top_users: int = 0           # distinct users who sent the top text
    top_is_emote: bool = False   # the top text is an emote (Twitch marks emote fragments)
    quotes: list = field(default_factory=list)  # (text, distinct users) repeated phrases
    text_users: dict = field(default_factory=dict)  # canonical text -> distinct users (top 25)
    texts: list = field(default_factory=list)


def windows(msgs: list[dict], start_s: float | None = None, end_s: float | None = None,
            window: int = WINDOW_S) -> list[Window]:
    if not msgs:
        return []
    lo = start_s if start_s is not None else (msgs[0]["t"] // window) * window
    hi = end_s if end_s is not None else msgs[-1]["t"] + window
    n = max(1, math.ceil((hi - lo) / window))
    ws = [Window(lo + i * window) for i in range(n)]
    buckets: list[list[dict]] = [[] for _ in range(n)]
    for m in msgs:
        i = int((m["t"] - lo) // window)
        if 0 <= i < n:
            buckets[i].append(m)
    for w, b in zip(ws, buckets):
        w.n = len(b)
        if not b:
            continue
        w.users = len({m.get("user") or m["id"] for m in b})
        texts: dict[str, set] = {}
        emote_texts: set[str] = set()
        for m in b:
            for f in classify(m):
                w.fam[f] += 1
            key = _canon(m.get("text") or "")
            texts.setdefault(key, set()).add(m.get("user") or m["id"])
            if m.get("emotes"):
                emote_texts.add(key)
        ranked = sorted(texts.items(), key=lambda kv: -len(kv[1]))
        if ranked:
            w.top_text, us = ranked[0]
            w.top_users = len(us)
            w.top_share = len(us) / max(1, w.users)
            w.top_is_emote = w.top_text in emote_texts
        w.quotes = [(t, len(u)) for t, u in ranked[:6] if len(u) >= 3]
        w.text_users = {t: len(u) for t, u in ranked[:25]}
        w.texts = [m.get("text") or "" for m in b][:400]
    return ws


def _robust(prior: list[float]) -> tuple[float, float]:
    med = statistics.median(prior)
    mad = statistics.median([abs(x - med) for x in prior])
    return med, mad


def score_windows(ws: list[Window]) -> list[dict]:
    """Per-window score. Returns dicts with the parts, for transparency.

    lift   = how far distinct-chatter count rises above the local baseline,
             in robust standard units (median + 1.4826*MAD, with a floor so
             a near-silent chat cannot produce a giant z on 8 messages).
    react  = share of messages that are laughter / hype / shock / clip calls
             (a reaction to SOMETHING), or quotes repeated by many people
             (chat repeating a line = content).
    junk   = share that is greetings, commands, support spam, or one
             non-reaction token flooded by many users ("wicked" x81).
    score  = lift * (0.35 + react) * (1 - junk)
    """
    users = [float(w.users) for w in ws]
    df: Counter = Counter()
    for w in ws:
        for t, k in w.text_users.items():
            if k >= 1:
                df[t] += 1
    out = []
    for i, w in enumerate(ws):
        prior = users[max(0, i - BASELINE_WINDOWS):i]
        if len(prior) < MIN_BASELINE:
            prior = users[: max(MIN_BASELINE, BASELINE_WINDOWS)]
        med, mad = _robust(prior) if prior else (0.0, 0.0)
        scale = max(1.4826 * mad, 0.25 * med, 3.0)
        lift = (w.users - med) / scale
        n = max(1, w.n)
        f = w.fam
        react_msgs = f["laugh"] + f["hype"] + f["shock"] + f["clip"] * 3
        react = min(1.0, react_msgs / n)
        top_is_react = bool(classify({"text": w.top_text}) & {"laugh", "hype", "shock", "clip"})
        # Channel vocabulary: how many windows of THIS stream contain each text.
        # A token the chat spams all night ("om", "o", "waga", "wicked" — often
        # 7TV/BTTV emotes Twitch does not mark) is culture, not news. A text
        # that floods one window but is rare in the stream ("kill", "revenge",
        # "reallymad", "don't take it") is chat naming what just happened.
        quote_bonus = 0.0
        flood_common = 0.0
        lex_react = LAUGH | HYPE | SHOCK | CLIP
        for text, k in w.text_users.items():
            if k < 3:
                continue
            words = re.findall(r"\w+", text)
            if not words:
                continue
            share = k / max(1, w.users)
            rarity = 1.0 - min(1.0, df.get(text, 0) / max(1.0, 0.05 * len(ws)))
            if set(words) <= (GREET | POLL):
                continue
            if set(words) <= lex_react:
                continue                      # already counted as reaction
            if rarity > 0.5 and not (text == w.top_text and w.top_is_emote):
                quote_bonus = max(quote_bonus, min(0.45, 1.5 * share * rarity))
            elif rarity <= 0.5:
                flood_common = max(flood_common, share)
        react = min(1.0, react + quote_bonus)
        junk = (f["greet"] + f["command"] + f["support"]) / n
        if flood_common >= 0.2:
            junk += flood_common              # chat-culture emote flood
        elif w.top_share >= 0.25 and w.top_is_emote and not top_is_react:
            junk += w.top_share               # a Twitch emote flood
        if w.top_text.strip() in POLL:
            # chat answering a question asked on stream: a poll, not an event
            junk += 0.5 * w.top_share
        junk = min(0.9, junk)
        score = max(0.0, lift) * (0.35 + react) * (1 - junk)
        out.append({
            "start_s": w.start_s, "msgs": w.n, "users": w.users,
            "baseline_users": round(med, 1), "lift": round(lift, 2),
            "react": round(react, 2), "junk": round(junk, 2), "score": round(score, 3),
            "laugh": f["laugh"], "hype": f["hype"], "shock": f["shock"], "clip": f["clip"],
            "greet": f["greet"], "command": f["command"], "talk": f["talk"],
            "top_text": w.top_text[:60], "top_users": w.top_users,
            "quotes": w.quotes[:4],
        })
    return out


def reaction_label(rows: list[dict]) -> str:
    """Dominant reaction across a few windows: laugh / hype / shock / clip / junk."""
    tot = Counter()
    for r in rows:
        for k in ("laugh", "hype", "shock", "clip", "greet", "command"):
            tot[k] += r[k]
    if not tot:
        return "quiet"
    k, v = tot.most_common(1)[0]
    if k in ("greet", "command"):
        return "junk:" + k
    return k
