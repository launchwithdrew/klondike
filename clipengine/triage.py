"""Triage — the engine decides what each new draft needs, so the operator is never asked "which should I render?".

Every new draft gets one route, a one-line reason and an expiry:

  preview   fresh and plausible: it is pre-rendered in the background (through the heavy-job gate) so a
            contact sheet and the finished cut are ready when the operator looks; expires when the moment
            stops being news (breaking window, then the evergreen limit)
  review    worth a human look but not a clean case: a quiet visual moment with no words, a cut that couldn't
            be repaired, uncertain speakers — shown with what is missing
  repair    cut mid-sentence: one bounded re-cut into the recorded handles (speech boundaries, payoff kept,
            render audit after). It returns once; a second failure goes to review with the reason
  archive   a stale fragment (the MOMENT is older than the limit — a restart or re-queue never makes it
            fresh), or chat noise with no corroborated payoff (plain driving with chat laughing): kept in
            `queue --all` with the reason, never rendered

Age is always measured from the moment itself (`at`, else the payoff), never from when it was drafted.
Defaults (config/grading.json "triage"): breaking 2 h, evergreen 6 h. An operator can keep an old moment with
an explicit evergreen reason (`clipengine triage --keep ID --why "…"`).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import time

DEFAULTS = {"breaking_h": 2.0, "evergreen_h": 6.0, "max_repairs": 1}
SENTENCE_END = (".", "?", "!", "…")


def config() -> dict:
    try:
        from . import grade as G
        return {**DEFAULTS, **(G.config().get("triage") or {})}
    except Exception:
        return dict(DEFAULTS)


def moment_time(entry: dict) -> float:
    at = entry.get("at")
    if isinstance(at, str):
        try:
            return dt.datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    for k in ("payoff", "stop", "start"):
        if isinstance(entry.get(k), (int, float)) and entry[k] > 1e9:
            return float(entry[k])
    return time.time()


def _parts(entry: dict) -> dict:
    j = entry.get("judgment") or {}
    return (j.get("parts") if isinstance(j, dict) else None) or {}


def _flags(entry: dict) -> list[str]:
    f = entry.get("flags") or []
    return [f] if isinstance(f, str) else list(f)


def _has_viewer_clip(entry: dict) -> bool:
    return bool((entry.get("crowd") or _parts(entry).get("crowd") or 0) > 0 or entry.get("titles"))


def render_review_reasons(entry: dict) -> list[str]:
    """Evidence that warrants a look before encoding. Missing evidence leaves that check undecided."""
    flags = " ".join(_flags(entry)).lower()
    reasons = []
    if "silent stretch" in flags or any(float(s) > 12 for s in re.findall(r"first word at (\d+(?:\.\d+)?)s", flags)):
        reasons.append("over 12 s without speech")
    j = entry.get("judgment") or {}
    speech, dur = j.get("speech_s"), j.get("duration_s")
    if speech is not None and dur and speech < 0.2 * dur and not _has_viewer_clip(entry):
        reasons.append("under 20% streamer speech with no viewer clip")
    picture = entry.get("visual") or {}
    if "menu/map screens" in flags or (picture.get("menu") or 0) >= 0.65:
        reasons.append("menu-heavy frames")
    if "recording from " in flags or (picture.get("offgame") or 0) >= 0.65:
        reasons.append("off-game frames")
    fit = picture.get("viewer_title_fit")
    if fit is not None and fit <= -0.01:
        reasons.append("sampled frames don't support the viewer clip title")
    return reasons


def corroboration(entry: dict) -> list[str]:
    """Evidence that something actually happens, beyond chat noise."""
    parts, ev = _parts(entry), []
    if _has_viewer_clip(entry):
        ev.append("viewers clipped it")
    if (parts.get("reaction") or 0) >= 0.3:
        ev.append("streamer reaction")
    speech = (entry.get("judgment") or {}).get("speech_s") if isinstance(entry.get("judgment"), dict) else None
    dur = max(1.0, float(entry.get("stop", 0)) - float(entry.get("start", 0)))
    if speech is not None and speech >= 0.2 * dur:
        ev.append("the streamer talks through it")
    live = entry.get("liveliness")
    if live is not None and live >= 0.6:
        ev.append(f"lively picture ({live:.2f})")
    if (parts.get("audio") or 0) >= 0.5:
        ev.append("loud audio")
    return ev


def _words(entry: dict) -> tuple[list[dict], float, float] | None:
    try:
        d = json.load(open(entry["words"]))
        return d.get("words") or [], float(d["start"]), float(d["stop"])
    except (KeyError, OSError, ValueError, TypeError):
        return None


def cut_mid_sentence(words: list[dict], start: float, stop: float) -> str | None:
    """Which edge of [start, stop] cuts speech: a word straddles it, or speech runs straight across it."""
    ws = sorted(words, key=lambda w: w["start"])
    for edge, name in ((stop, "end"), (start, "start")):
        if any(w["start"] < edge - 0.05 and w["end"] > edge + 0.05 for w in ws):
            return name
        before = max((w for w in ws if w["end"] <= edge + 0.05), key=lambda w: w["end"], default=None)
        after = min((w for w in ws if w["start"] >= edge - 0.05), key=lambda w: w["start"], default=None)
        if before and after and after is not before and after["start"] - before["end"] < 0.3 \
                and not str(before.get("w", "")).strip().endswith(SENTENCE_END):
            return name
    return None


def assess(entry: dict, now: float | None = None, cfg: dict | None = None, floor: float = 60.0) -> dict:
    """{route, reason, expires_at} for one draft (route "none" for drafts already decided)."""
    now = time.time() if now is None else now
    cfg = cfg or config()
    if entry.get("status") not in (None, "new") or entry.get("unsafe"):
        return {"route": "none", "reason": "", "expires_at": None}
    t = moment_time(entry)
    age_h = (now - t) / 3600
    limit_h = float(cfg["evergreen_h"])
    expires = t + (float(cfg["breaking_h"]) if age_h <= cfg["breaking_h"] else limit_h) * 3600
    if age_h > limit_h and not entry.get("evergreen"):
        return {"route": "archive", "reason": f"stale: the moment is {age_h:.0f} h old (limit {limit_h:g} h) — "
                f"`triage --keep {entry['id']} --why …` to keep it as evergreen", "expires_at": None}
    if entry.get("evergreen"):
        expires = now + 24 * 3600
    ev = corroboration(entry)
    flags = " ".join(_flags(entry))
    quiet = "nothing happening" in flags or (entry.get("liveliness") is not None and entry["liveliness"] < 0.35)
    if not ev and (quiet or "dull picture" in flags):
        live = entry.get("liveliness")
        return {"route": "archive", "reason": "no corroborated payoff: chat alone"
                + (f" (picture {live:.2f}" if live is not None else " (")
                + ", no reaction, no viewer clips)", "expires_at": None}
    if entry.get("best", 0) < floor and "viewers clipped it" not in ev:
        return {"route": "none", "reason": "below every bar", "expires_at": expires}
    review = render_review_reasons(entry)
    if review:
        return {"route": "review", "reason": "; ".join(review), "expires_at": expires}
    w = _words(entry)
    if w is not None and entry.get("payoff") is not None:
        edge = cut_mid_sentence(w[0], float(entry["start"]), float(entry["stop"]))
        if edge:
            if int(entry.get("repairs", 0)) < int(cfg["max_repairs"]):
                return {"route": "repair", "reason": f"cut mid-sentence at the {edge}", "expires_at": expires}
            return {"route": "review", "reason": f"cut mid-sentence at the {edge}; repair failed: "
                    + (entry.get("repair_failed") or "no clean boundary in the recorded handles"), "expires_at": expires}
    speech = (entry.get("judgment") or {}).get("speech_s") if isinstance(entry.get("judgment"), dict) else None
    if not entry.get("said") and (speech or 0) == 0 and ev:
        return {"route": "review", "reason": "quiet visual moment (" + ", ".join(ev) + "): no words to judge — look at it",
                "expires_at": expires}
    return {"route": "preview", "reason": ", ".join(ev) or "plausible", "expires_at": expires}


def repair(entry: dict) -> dict | None:
    """One bounded re-cut into the recorded handles, on speech boundaries, keeping the payoff."""
    from . import sentinel as SN
    w = _words(entry)
    if w is None or entry.get("payoff") is None:
        SN.note(entry, repairs=int(entry.get("repairs", 0)) + 1, repair_failed="no transcript handles to re-cut into")
        return None
    words, lo, hi = w
    payoff = float(entry["payoff"])
    if entry.get("rec") and os.path.exists(os.path.join(entry["rec"], "index.jsonl")):
        have = SN.covered(entry["rec"], lo, hi, payoff)
        if have is None:
            SN.note(entry, repairs=int(entry.get("repairs", 0)) + 1, repair_failed="the payoff is in a recording hole")
            return None
        lo, hi = max(lo, have[0]), min(hi, have[1])
    win = SN.payoff_window(words, payoff, lo, hi)
    ok = win["start"] < payoff < win["stop"] and not cut_mid_sentence(words, win["start"], win["stop"])
    if not ok:
        SN.note(entry, repairs=int(entry.get("repairs", 0)) + 1,
                repair_failed=f"no clean boundary within the recorded {hi - lo:.0f} s around the payoff")
        return None
    SN.note(entry, repairs=int(entry.get("repairs", 0)) + 1, start=win["start"], stop=win["stop"],
            repaired_from=[entry["start"], entry["stop"]], prerender=None)
    return {"start": win["start"], "stop": win["stop"]}


def keep(entry: dict, why: str) -> None:
    from . import sentinel as SN
    SN.note(entry, evergreen=why.strip(), status="new", reason=None)


def apply(root: str | None = None, now: float | None = None, floor: float | None = None) -> str | None:
    """Route every new draft; archive what can't be news or has no payoff; repair once. One summary line."""
    from . import gate as GT
    from . import sentinel as SN
    now = time.time() if now is None else now
    if floor is None:
        floor = min(r["min_score"] for r in GT.config()["platforms"].values())
    rows = SN.load_queue(root) if root else SN.load_queue()
    counts: dict[str, int] = {}
    for r in rows:
        if r.get("status") not in (None, "new") or not r.get("rec"):
            continue
        a = assess(r, now, floor=floor)
        if a["route"] == "repair":
            fixed = repair(r)
            key = "repaired" if fixed else "repair failed"
            counts[key] = counts.get(key, 0) + 1
            r = {**r, **(fixed or {}), "repairs": int(r.get("repairs", 0)) + 1}
            a = assess(r, now, floor=floor)
        if a["route"] == "archive":
            SN.set_status(r, "archived", reason=a["reason"], triage="archive")
            counts["archived"] = counts.get("archived", 0) + 1
        elif a["route"] in ("preview", "review") and (r.get("triage") != a["route"] or r.get("triage_reason") != a["reason"]):
            SN.note(r, triage=a["route"], triage_reason=a["reason"], expires_at=a["expires_at"])
            counts[a["route"]] = counts.get(a["route"], 0) + 1
    if not counts:
        return None
    return "triage: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
