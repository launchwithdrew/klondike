"""Turn per-window chat scores into ranked moment candidates.

Fixes v1's ranking bug: spike_finder.py deduped by ratio, then SORTED BY
TIME and only then kept --top N — so it returned the EARLIEST N spikes,
not the strongest (in one evaluated hour: 13 events, the 5 "candidates" were
all in the first 29 minutes of the hour). Here the cut is by score and
time order is only applied for display.
"""
from __future__ import annotations

from . import signals as S
from .twitch import fmt

DEFAULT_LEAD = 20.0       # chat reacts ~5-20 s after the moment
DEFAULT_LENGTH = 62.0     # v1's hedge for the >60 s Creator Rewards rule
MIN_GAP = 90.0            # seconds between candidate peaks


def find_candidates(msgs: list[dict], top: int = 10, min_score: float = 1.0,
                    lead: float = DEFAULT_LEAD, length: float = DEFAULT_LENGTH,
                    min_gap: float = MIN_GAP, start_s: float | None = None,
                    end_s: float | None = None) -> tuple[list[dict], list[dict]]:
    """Returns (candidates best-first, all window rows)."""
    ws = S.windows(msgs, start_s, end_s)
    rows = S.score_windows(ws)
    if not rows:
        return [], rows
    sc = [r["score"] for r in rows]
    # sustain: a moment chat keeps reacting to for 20-30 s beats a 1-window blip
    sustain = [sum(sc[max(0, i - 1): i + 3]) for i in range(len(sc))]
    order = sorted(range(len(rows)), key=lambda i: -(sc[i] + 0.35 * sustain[i]))
    picked: list[int] = []
    for i in order:
        if sc[i] < min_score:
            break
        t = rows[i]["start_s"]
        if all(abs(t - rows[j]["start_s"]) >= min_gap for j in picked):
            picked.append(i)
        if len(picked) >= top:
            break
    cands = []
    for rank, i in enumerate(picked, 1):
        r = rows[i]
        near = rows[max(0, i - 1): i + 3]
        peak = r["start_s"]
        start = max(0.0, peak - lead)
        quotes = []
        for x in near:
            for q in x["quotes"]:
                if q not in quotes:
                    quotes.append(q)
        cands.append({
            "rank": rank,
            "peak_s": peak, "peak": fmt(peak),
            "clip_start_s": start, "clip_start": fmt(start),
            "clip_end_s": start + length, "clip_end": fmt(start + length),
            "chat_score": round(sc[i] + 0.35 * sustain[i], 2),
            "lift": r["lift"], "react": r["react"], "junk": r["junk"],
            "msgs_10s": r["msgs"], "users_10s": r["users"],
            "baseline_users": r["baseline_users"],
            "ratio": round(r["users"] / r["baseline_users"], 2) if r["baseline_users"] else None,
            "reaction": S.reaction_label(near),
            "chat_quotes": [q[0] for q in sorted(quotes, key=lambda q: -q[1])[:5]],
            "top_text": r["top_text"],
        })
    return cands, rows


def chat_near(msgs: list[dict], t: float, before: float = 10, after: float = 30,
              limit: int = 12) -> list[tuple[str, int]]:
    """Most repeated chat lines around a moment (what chat was saying)."""
    from collections import Counter
    c = Counter(S._canon(m["text"]) for m in msgs
                if t - before <= m["t"] <= t + after and m.get("text"))
    return c.most_common(limit)
