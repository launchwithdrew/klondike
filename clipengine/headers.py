"""v6 headers, generated never typed: '{streamer} {label}' from a CLOSED list
(a short, fixed list of moment types: "had to choose", nice and simple). No type fits -> no header -> drop or re-cut.

This module only renders the label so the one-command render can take --type CHOICE.
"""
from __future__ import annotations

TYPES = {
    "CHOICE": ("had to choose", "Can I see the options and pick one before the clip ends?"),
    "OFFER": ("had to take it or leave it", "Is an offer actually made on screen?"),
    "DECISION": ("made the call", "Does someone say what they decided?"),
    "VOTE": ("let chat decide", "Is the audience asked to decide?"),
    "FAIL": ("instantly regretted it", "Can I see what went wrong?"),
    "RANT": ("had enough", "Is someone actually ranting, not just annoyed for one line?"),
    "BAN": ("got banned on the spot", "Does a ban / removal happen or get threatened?"),
    "BLINDSIDE": ("didn't see it coming", "Can I see the moment they realise they were caught out?"),
    "ESCAPE": ("barely got away", "Is someone actively getting away?"),
    "HEIST": ("pulled the job", "Is a job being planned or pulled?"),
    "COMEBACK": ("came back from the dead", "Can I see the moment it turns around?"),
    "GAME": ("played all or nothing", "Are the rules and the stakes clear in the clip?"),
}

def short_name(login: str, display: str = "") -> str:
    try:
        from .publish import creators
        n = creators().get((login or "").lower(), {}).get("name")
        if n:
            return n
    except Exception:
        pass
    return display or login


def build(streamer: str, type_code: str) -> str:
    key = (type_code or "").strip().upper()
    if key not in TYPES:
        raise ValueError(f"'{type_code}' is not a closed moment type — pick one of "
                         f"{', '.join(TYPES)}; if none fits, the clip gets no header")
    h = f"{streamer.strip()} {TYPES[key][0]}"
    if len(h) > 32:
        raise ValueError(f"header '{h}' is {len(h)} chars (max 32)")
    return h
