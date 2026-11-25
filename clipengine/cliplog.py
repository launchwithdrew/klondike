"""The sentinel's event log: what happened to every viewer clip it saw, so a miss has a cause, not a guess.

`runs/<sentinel>/events.jsonl`, one JSON line per event:
  {"ev": "poll", "t", "ran", "why"?, "n", "more", "min_views", "newest_age_s", "rec": [start, end]}
      each crowd check (once a minute): did it run, how big the clip list was (top-N by views, more pages?),
      its lowest view count (the cutoff a fresh clip must pass to be visible at all), the newest clip's age
  {"ev": "clip", "t", "slug", "views", "created", "start", "end", "state", "draft"?}
      a clip, the first time it's seen and whenever its outcome changes (or its views cross 10 / 25 / double):
      state = drafted | below 10 views | overlaps a draft | spike open | replay clip | before the recording |
      not recorded yet
`clipengine scouts --explain` reads these to give each "recorded, didn't fire" miss one concrete cause.
"""
from __future__ import annotations

import json
import os
import time

MAX_BYTES = 20 * 1024 * 1024          # then events.jsonl → events.1.jsonl (one old file kept)
NAME = "events.jsonl"


class ClipLog:
    def __init__(self, out: str):
        self.path = os.path.join(out, NAME)
        self.state: dict[str, tuple[str, int]] = {}

    def _write(self, row: dict) -> None:
        try:
            if os.path.exists(self.path) and os.path.getsize(self.path) > MAX_BYTES:
                os.replace(self.path, self.path[:-len(".jsonl")] + ".1.jsonl")
            with open(self.path, "a") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass                                         # a full disk must never stop the sentinel

    def poll(self, ran: bool, info: dict | None = None, why: str | None = None, now: float | None = None) -> None:
        row = {"ev": "poll", "t": round(now or time.time(), 1), "ran": ran}
        if why:
            row["why"] = why[:160]
        row.update({k: v for k, v in (info or {}).items() if k in ("n", "more", "min_views", "newest_age_s", "rec")})
        self._write(row)

    def clip(self, c: dict, state: str, draft: str | None = None, now: float | None = None) -> bool:
        """Log a clip when it's new, its outcome changed, or its views crossed 10 / 25 / doubled. True = written."""
        views = int(c.get("views") or 0)
        last = self.state.get(c["slug"])
        if last is not None:
            s0, v0 = last
            crossed = any(v0 < k <= views for k in (10, 25)) or (v0 > 0 and views >= 2 * v0)
            if s0 == state and not crossed:
                return False
        self.state[c["slug"]] = (state, views)
        row = {"ev": "clip", "t": round(now or time.time(), 1), "slug": c["slug"], "views": views,
               "created": c.get("created"), "start": c.get("start"), "end": c.get("end"), "state": state}
        if draft:
            row["draft"] = draft
        self._write(row)
        return True


def load(rec_dir: str, since: float = 0.0) -> list[dict]:
    """Events at or after `since` from a sentinel folder (the rotated file first)."""
    out = []
    for name in ("events.1.jsonl", NAME):
        try:
            for line in open(os.path.join(rec_dir, name)):
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("t", 0) >= since:
                    out.append(r)
        except OSError:
            continue
    return out


def explain(clip: dict, events: list[dict], recorded: bool) -> str | None:
    """One concrete cause for a viewer clip we didn't draft, from the sentinel's own log (pure).
    `clip` has slug and t (the moment, wall clock). None = the log has nothing to say (no sentinel log there)."""
    t = clip["t"]
    polls = [e for e in events if e.get("ev") == "poll" and t - 300 <= e["t"] <= t + 3600]
    seen = [e for e in events if e.get("ev") == "clip" and e.get("slug") == clip.get("slug")]
    if seen:
        last = seen[-1]
        st, peak = last.get("state", ""), max(int(e.get("views") or 0) for e in seen)
        if st == "drafted":
            return f"drafted ({last.get('draft')}) but no queue line: the draft thread hung or died"
        if st == "below 10 views":
            return f"stayed under 10 views at every check (most seen: {peak}) — it passed them after we stopped looking"
        if st == "overlaps a draft":
            return f"folded into the overlapping draft {last.get('draft') or ''} (judged as that moment)".replace("  ", " ")
        if st == "spike open":
            return "a live spike was still open over it when the sentinel stopped checking"
        if st == "replay clip":
            return "clipped from the replay, not live (catch-up's job)"
        if st == "before the recording":
            return "its window starts before the recording did (older code didn't hand it to catch-up)"
        if st == "pruned before we saw it":
            return "recorded, but the buffer pruned it before the sentinel saw the clip (disk-sized buffer too short)"
        if st == "handed to catch-up":
            return "not in our recording: handed to catch-up, which didn't queue it (check `jobs` for that batch)"
        if st == "not recorded yet":
            return "its window was never in the recording (recorder behind or stopped)"
        return f"last seen as {st!r}"
    if not polls:
        if not events:
            return None
        return ("the sentinel wasn't running then (the recorder was)" if recorded
                else "the sentinel wasn't running then")
    ran = [p for p in polls if p.get("ran")]
    if not ran:
        why = next((p.get("why") for p in reversed(polls) if p.get("why")), "")
        return f"the crowd check didn't run ({why or 'no new clip in the list'})"
    cut = max((p.get("min_views") or 0) for p in ran)
    more = any(p.get("more") for p in ran)
    n = max((p.get("n") or 0) for p in ran)
    return (f"never in the sentinel's clip list while it watched (top {n} by views{', more pages' if more else ''}, "
            f"cutoff up to {cut} views): the list hides fresh clips (A1)")
