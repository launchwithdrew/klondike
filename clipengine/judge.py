"""Judging the judge — humans mark the engine's calls, the engine counts how often it was right.

  audit speakers   each recent draft's speaker tags (streamer / other / game seconds) with a transcript
                   excerpt per tagged stretch, so a person can mark the tags right or wrong
                   (data/audits.jsonl) — and the running accuracy.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(REPO, "data")


def _path(name: str) -> str:
    return os.path.join(DATA, name)


def _rows(path: str) -> list[dict]:
    out = []
    try:
        for line in open(path):
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return out


def _append(path: str, rec: dict) -> dict:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    return rec


def _now_iso(now: float | None = None) -> str:
    return dt.datetime.fromtimestamp(now or time.time(), dt.timezone.utc).isoformat()


# ---- J1: speaker audit ------------------------------------------------------------------------------

def stretches(words: list[dict], gap: float = 1.5) -> list[dict]:
    """Consecutive words with the same speaker tag → [{spk, start, end, text}] (untagged = streamer)."""
    out: list[dict] = []
    for w in words:
        spk = w.get("spk", "streamer")
        if out and out[-1]["spk"] == spk and w["start"] - out[-1]["end"] <= gap:
            out[-1]["end"] = w["end"]
            out[-1]["text"] += " " + w["w"].strip()
        else:
            out.append({"spk": spk, "start": w["start"], "end": w["end"], "text": w["w"].strip()})
    return out


def _draft_words(entry: dict) -> list[dict]:
    try:
        d = json.load(open(entry["words"]))
    except (OSError, ValueError, KeyError, TypeError):
        return []
    return [w for w in d.get("words", []) if entry["start"] <= w["start"] < entry["stop"]]


def speaker_accuracy(audits: list[dict] | None = None) -> tuple[int, int]:
    """(right, marked) over the latest verdict per draft."""
    latest: dict[str, str] = {}
    for a in audits if audits is not None else _rows(_path("audits.jsonl")):
        if a.get("kind") == "speakers":
            latest[a["id"]] = a["verdict"]
    return sum(1 for v in latest.values() if v == "right"), len(latest)


def accuracy_line(audits: list[dict] | None = None) -> str:
    right, n = speaker_accuracy(audits)
    return (f"speaker tags: {right}/{n} drafts marked right ({100 * right / n:.0f}%)" if n
            else "speaker tags: none marked yet")


def audit_speakers(last: int = 20, root: str | None = None, show: bool = True) -> list[dict]:
    """The last `last` drafts with tagged words: tags + one excerpt per stretch. Returns them."""
    from . import sentinel as SN
    from . import untrusted as UT
    rows = [r for r in SN.load_queue(root or os.path.join(REPO, "runs")) if r.get("words") and r.get("speakers")]
    rows.sort(key=lambda r: r.get("at", ""), reverse=True)
    marked = {a["id"]: a["verdict"] for a in _rows(_path("audits.jsonl")) if a.get("kind") == "speakers"}
    out = []
    for r in rows[:last]:
        sp = r["speakers"]
        st = stretches(_draft_words(r))
        out.append({**r, "stretches": st})
        if not show:
            continue
        mark = f" [marked {marked[r['id']]}]" if r["id"] in marked else ""
        print(f"{r['id']:<24} streamer {sp.get('streamer_s', 0):>4.1f}s · other {sp.get('other_s', 0):>4.1f}s · "
              f"game {sp.get('game_s', 0):>4.1f}s ({sp.get('backend', '?')}){mark}")
        for s in st[:8]:
            print(f"    {s['spk']:<8} {s['end'] - s['start']:>4.1f}s {UT.quote(s['text'], 70)}")
        if len(st) > 8:
            print(f"    … +{len(st) - 8} more stretches")
    if show:
        print(("" if out else "no drafts with speaker tags yet\n")
              + "mark each: clipengine audit speakers --mark ID right|wrong · " + accuracy_line())
    return out


def mark_speakers(draft_id: str, verdict: str, note: str | None = None, root: str | None = None,
                  now: float | None = None) -> dict:
    from . import sentinel as SN
    if verdict not in ("right", "wrong"):
        raise SystemExit("audit speakers --mark: verdict is right or wrong")
    e = next((r for r in SN.load_queue(root or os.path.join(REPO, "runs")) if r["id"] == draft_id), None)
    if not e:
        raise SystemExit(f"audit speakers: no draft {draft_id}")
    sp = e.get("speakers") or {}
    rec = _append(_path("audits.jsonl"), {"kind": "speakers", "id": draft_id, "login": e.get("login"),
                                          "verdict": verdict, "backend": sp.get("backend"),
                                          "speakers": {k: sp.get(k) for k in ("streamer_s", "other_s", "game_s")},
                                          **({"note": note} if note else {}), "at": _now_iso(now)})
    print(f"marked {draft_id} {verdict} · {accuracy_line()}")
    return rec


def score_snapshot(engine: dict, signals: dict) -> dict:
    speech = signals.get("speech") or {}
    return {"engine": engine["score"], "parts": engine["parts"], "speech_s": speech.get("streamer_s"),
            "duration_s": speech.get("dur"), "reaction": signals.get("reaction"), "why": engine["why"]}


CALL_DEFAULTS = {"drop_below": 50.0, "ship_from": 70.0}


def engine_call(draft: dict) -> str | None:
    """The engine's own ship / hold / drop before an editor looks (J2 compares it with the editor's call).

    drop  unsafe, already dropped, a possible death screen the channel penalises, or an engine score under
          `drop_below` (the old rule assumed a PERFECT rubric, so it held everything from 33 up — Nov 21: the editor
          dropped 14 of 15 held drafts between 35 and 55)
    ship  an engine score from `ship_from`, not flagged
    hold  in between: look at the frames
    Thresholds: config/grading.json "judge_call" (defaults 50 / 70)."""
    from . import grade

    if draft.get("unsafe") or draft.get("status") == "dropped":
        return "drop"
    score = draft.get("engine")
    if score is None:
        return None
    cfg = grade.config()
    calls = {**CALL_DEFAULTS, **(cfg.get("judge_call") or {})}
    flags = " ".join(str(f) for f in draft.get("flags") or []).lower()
    if "death screen?" in flags and float((cfg.get("penalties") or {}).get("death", 1.0)) < 1.0:
        return "drop"
    if score < calls["drop_below"]:
        return "drop"
    return "ship" if score >= calls["ship_from"] else "hold"


def artifact_id(draft: dict, path: str | None = None) -> str | None:
    """What the verdict was given on: the rendered clip when there is one, else the draft's contact sheet (the
    review page embeds it). sha1, short. None when nothing viewable exists."""
    import hashlib
    for p in (path, draft.get("sheet")):
        if p and os.path.isfile(p):
            h = hashlib.sha1()
            with open(p, "rb") as fh:
                for b in iter(lambda: fh.read(1 << 20), b""):
                    h.update(b)
            return h.hexdigest()[:12]
    return None


def record(draft: dict, verdict: str, reason: str, *, frames=(), override: str | None = None,
           slug: str | None = None, artifact: str | None = None) -> dict:
    """One verdict per decision (wishlist 8): the verdict is tied to the artifact that was viewed, and repeating the
    same verdict on the same artifact (a retried click, a re-run command) records nothing new — it returns the
    verdict already on file. A different verdict, or the same one on a new artifact, is a real decision and is kept."""
    import fcntl
    art = artifact or artifact_id(draft)
    path = _path("judged.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        last = next((r for r in reversed(_rows(path)) if r.get("kind") == "judgment" and r.get("id") == draft["id"]), None)
        if last and last.get("verdict") == verdict and last.get("artifact") == art:
            return {**last, "repeat": True}
        return _record(draft, verdict, reason, frames=frames, override=override, slug=slug, artifact=art)


def _record(draft: dict, verdict: str, reason: str, *, frames=(), override: str | None = None,
            slug: str | None = None, artifact: str | None = None) -> dict:
    snapshot = draft.get("judgment") or draft
    speakers = draft.get("speakers") or {}
    row = {"kind": "judgment", "id": draft["id"], "login": draft.get("login"),
           "verdict": verdict, "reason": reason, "engine_call": engine_call(draft),
           "engine": snapshot.get("engine"), "parts": snapshot.get("parts"),
           "speech_s": snapshot.get("speech_s", speakers.get("streamer_s")),
           "duration_s": snapshot.get("duration_s") or (
               draft["stop"] - draft["start"] if draft.get("start") is not None and draft.get("stop") is not None else None),
           "reaction": snapshot.get("reaction"), "why": snapshot.get("why") or [],
           "flags": list(draft.get("flags") or []), "at": _now_iso()}
    if override:
        row["truth_override"] = override
    if slug:
        row["slug"] = slug
    if artifact:
        row["artifact"] = artifact
    paths = label_frames(draft, verdict, frames) if verdict in ("ship", "drop") else []
    if paths:
        row["frames"] = paths
    return _append(_path("judged.jsonl"), row)


def label_frames(draft: dict, verdict: str, frames=()) -> list[str]:
    """Store three real stills when the draft or render has visual evidence."""
    import base64
    import io
    import re
    from pathlib import Path

    from PIL import Image
    from PIL import ImageOps

    stills = []
    try:
        thumbs = draft.get("thumbs") or []
        if len(thumbs) >= 3:
            indices = (0, len(thumbs) // 2, len(thumbs) - 1)
            stills = [Image.open(io.BytesIO(base64.b64decode(thumbs[i].split(",", 1)[1]))).convert("RGB")
                      for i in indices]
        elif draft.get("sheet"):
            with Image.open(draft["sheet"]) as sheet:
                if sheet.width % 4 or sheet.height % 2:
                    return []
                width, height = sheet.width // 4, sheet.height // 2
                stills = [sheet.crop((i % 4 * width, i // 4 * height, (i % 4 + 1) * width,
                                      (i // 4 + 1) * height)).convert("RGB") for i in (0, 3, 6)]
        elif len(frames or ()) >= 3:
            stills = [Image.open(p).convert("RGB") for p in frames[:3]]
    except (OSError, ValueError, IndexError):
        return []
    if len(stills) != 3:
        return []
    safe_id = re.sub(r"[^a-zA-Z0-9_.-]", "_", draft["id"]).strip(".") or "draft"
    folder = Path(_path("labels")) / safe_id / verdict
    paths = []
    try:
        folder.mkdir(parents=True, exist_ok=True)
        for index, still in enumerate(stills, 1):
            path = folder / f"frame-{index}.jpg"
            ImageOps.contain(still, (426, 240), Image.Resampling.LANCZOS).save(path, quality=85)
            paths.append(str(path.relative_to(DATA)))
        (folder / "verdict.json").write_text(json.dumps({"id": draft["id"], "verdict": verdict}) + "\n")
    except OSError:
        return []
    return paths


def eval_judge(rows: list[dict] | None = None) -> dict:
    from . import untrusted

    rows = rows if rows is not None else _rows(_path("judged.jsonl"))
    latest = {r["id"]: r for r in rows if r.get("kind") == "judgment" and r.get("verdict") in ("ship", "hold", "drop")}
    judged = [r for r in latest.values() if r.get("engine_call")]
    low = [r for r in judged if r.get("speech_s") is not None and r.get("duration_s")
           and r["speech_s"] < 0.2 * r["duration_s"]]
    stats = {}
    for name, group in (("overall", judged), ("low speech", low)):
        correct = sum(r["engine_call"] == r["verdict"] for r in group)
        stats[name] = {"correct": correct, "count": len(group),
                       "agreement": 100 * correct / len(group) if group else None}
        result = f"{100 * correct / len(group):.0f}%" if group else "no scored examples"
        print(f"{name}: {correct}/{len(group)} agree ({result})")
    print(f"low-speech keepers: {sum(r['verdict'] == 'ship' for r in low)} · unscored judgments: {len(latest) - len(judged)}")
    # agreement alone rewards "drop everything" when editors drop most drafts: show what the calls actually do
    from collections import Counter
    cm = Counter((r["engine_call"], r["verdict"]) for r in judged)
    order = ("ship", "hold", "drop")
    print("engine \\ editor   " + "  ".join(f"{v:>5}" for v in order))
    for e in order:
        print(f"  {e:<15} " + "  ".join(f"{cm[(e, v)]:>5}" for v in order))
    ships = [r for r in judged if r["engine_call"] == "ship"]
    kept = [r for r in judged if r["verdict"] == "ship"]
    base = max((sum(r["verdict"] == v for r in judged) for v in order), default=0)
    stats["ship_precision"] = (sum(r["verdict"] == "ship" for r in ships), len(ships))
    stats["keepers_caught"] = (sum(r["engine_call"] == "ship" for r in kept), len(kept))
    print(f"ship calls right: {stats['ship_precision'][0]}/{stats['ship_precision'][1]} · editor's keepers the engine "
          f"shipped: {stats['keepers_caught'][0]}/{stats['keepers_caught'][1]} · always-the-commonest-call baseline: "
          f"{base}/{len(judged)}")
    today = [(engine_call({"engine": r.get("engine"), "flags": r.get("flags") or []}), r["verdict"]) for r in judged
             if r.get("engine") is not None]
    if today:
        ok = sum(c == v for c, v in today)
        stats["today_rule"] = (ok, len(today))
        print(f"today's rule on the same drafts: {ok}/{len(today)} agree ({100 * ok / len(today):.0f}%) "
              f"(calls above are as made at the time)")
    for r in judged:
        if r["engine_call"] == r["verdict"]:
            continue
        parts = r.get("parts") or {}
        if not isinstance(parts, dict):
            parts = {}
        components = {"speech_s": r.get("speech_s"), "chat": parts.get("chat"), "audio": parts.get("audio"),
                      "reaction": r.get("reaction"), "crowd": parts.get("crowd")}
        detail = " · ".join(f"{key} {value if value is not None else 'unknown'}" for key, value in components.items())
        why = "; ".join(r.get("why") or [])
        print(f"{r['id']}: engine {r['engine_call']} / human {r['verdict']} · {detail} · "
              f"{untrusted.quote(r.get('reason') or '', 90)} {untrusted.quote(why, 140)}")
    return stats


def recurring_reasons(now: float | None = None) -> list[str]:
    from collections import Counter

    now = time.time() if now is None else now
    reasons = {}
    for row in _rows(_path("judged.jsonl")):
        if row.get("verdict") != "drop" or row.get("reason") in (None, "", "taste", "other"):
            continue
        try:
            at = dt.datetime.fromisoformat(row["at"]).timestamp()
        except (KeyError, TypeError, ValueError):
            continue
        if now - 7 * 86400 <= at <= now:
            reasons[row["id"]] = row["reason"].strip().casefold()
    return sorted(reason for reason, count in Counter(reasons.values()).items() if count >= 3)


def headline_preview(title: str, quote: str | None, transcript: str, titles: list[str], names: list[str]) -> dict:
    import re
    from itertools import groupby

    from . import publish
    from . import safety

    problems = publish.check_title(title)
    word_count = len(re.findall(r"[\w]+(?:['’-][\w]+)*", title))
    if not 2 <= word_count <= 5:
        problems.append(f"headline card has {word_count} words (target 2–5)")
    if len(title) > 32:
        problems.append(f"headline card is {len(title)} chars (target 32 or fewer)")
    if quote and safety.screen(headline=quote)["flagged"]:
        problems.append("quote has sexual terms")
    acronyms = {"GTA", "NPC", "NPCS", "RP", "VI", "FBI", "CEO", "DLC", "USA", "TV", "OMG", "LOL", "W", "L"}

    def marked(token):
        letters = "".join(letter for letter in token if letter.isalpha())
        return len(letters) >= 2 and letters.isupper() and letters not in acronyms
    caps = next((" ".join(run) for emph, run in groupby(title.split(), marked) if emph), "")
    return {"problems": problems, "unsupported": safety.headline_support(title, transcript, titles, names),
            "first_caps_run": caps}


def draft_evidence(entry: dict) -> tuple[str, list[str]]:
    from pathlib import Path

    if entry.get("scan"):
        scan_path = Path(entry["scan"]) / "scan.json"
        try:
            with scan_path.open() as stream:
                scan = json.load(stream)
            candidate = next(c for c in scan["candidates"] if c["rank"] == entry["pick"])
            return candidate.get("transcript_text", ""), (candidate.get("crowd") or {}).get("titles") or []
        except (OSError, ValueError, KeyError, StopIteration):
            pass
    words = _draft_words(entry)
    return (" ".join(w["w"].strip() for w in words) or entry.get("said", ""), entry.get("titles") or [])


def check_headline(entry: dict, title: str, quote: str | None = None) -> dict:
    from . import publish

    transcript, titles = draft_evidence(entry)
    login = entry.get("login") or ""
    return headline_preview(title, quote, transcript, titles,
                            [login, publish.creators().get(login.lower(), {}).get("name", "")])


def audit_words(last: int = 20, login: str | None = None, reference: str | None = None,
                root: str | None = None) -> dict:
    """Measure substitutions/misses against a human reference; without one, report estimated matches."""
    import re
    from collections import Counter
    from pathlib import Path

    from . import audio
    from . import sentinel

    refs = json.loads(Path(reference).read_text()) if reference else {}
    if not isinstance(refs, dict):
        raise SystemExit("audit words: reference must map draft IDs to verified transcript strings")
    queue = sentinel.load_queue(root or os.path.join(REPO, "runs"))
    queue = [row for row in queue if row.get("words") and (not login or row.get("login") == login.casefold())]
    queue.sort(key=lambda row: row.get("at", ""), reverse=True)

    def occurrences(text, terms):
        tokens = " ".join(re.findall(r"[a-z0-9']+", text.casefold()))
        return Counter({term: len(re.findall(rf"(?<!\w){re.escape(term)}(?!\w)", tokens)) for term in terms})

    stats = {"drafts": 0, "reference_words": 0, "errors": 0, "estimated_mishearings": 0}
    for entry in queue[:last]:
        who = entry.get("login")
        pack = audio.creator_vocab(who)
        if not pack:
            continue
        words = _draft_words(entry)
        if not words:
            continue
        text = " ".join(word["w"].strip() for word in words)
        stats["drafts"] += 1
        stats["estimated_mishearings"] += sum(right in pack for _, right in audio.misheard_names(words, login=who))
        if isinstance(refs.get(entry["id"]), str):
            terms = {" ".join(re.findall(r"[a-z0-9']+", word.casefold())) for word in pack}
            terms.discard("")
            expected = occurrences(refs[entry["id"]], terms)
            got = occurrences(text, terms)
            stats["reference_words"] += sum(expected.values())
            stats["errors"] += sum(abs(expected[term] - got[term]) for term in terms)
    total = stats["reference_words"]
    rate = 100 * stats["errors"] / total if total else None
    if rate is None:
        print(f"vocab audit: {stats['drafts']} drafts; {stats['estimated_mishearings']} possible mishearings; "
              "use --reference for a measured name/slang error rate")
    else:
        print(f"vocab audit: {stats['errors']} errors / {total} reference terms ({rate:.1f}%)")
    return {**stats, "error_rate": rate}
