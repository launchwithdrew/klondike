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
    try:
        with open(path) as stream:
            return _parse_rows(stream)
    except OSError:
        return []


def _parse_rows(lines) -> list[dict]:
    out = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
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


CALL_DEFAULTS = {"drop_below": 50.0, "ship_from": 70.0,
                 # OP_SPEC 4.1: the editor's drop reasons as named checks, each measured on Muse's verdicts
                 # (eval/eval_engine_call.py; Nov 24, 107 drafts: 2 ships, 105 drops)
                 "min_speech_s": 10.0,     # dud: under this much streamer speech and no viewer clip = chat + loudness
                 "death_peak": 0.6,        # death screen: the picture model's death read on any frame
                 "menu": 0.6,              # map / menu: the picture model's mean menu read
                 "still_action_peak": 0.4,  # cutscene / standing around: no frame reads as action
                 "adult_peak": 0.45}       # possible adult scene: look first


def _speech_s(draft: dict) -> float | None:
    j = draft.get("judgment") if isinstance(draft.get("judgment"), dict) else {}
    for v in (draft.get("speech_s"), j.get("speech_s"), (draft.get("speakers") or {}).get("streamer_s")):
        if v is not None:
            return float(v)
    return None


def _viewer_clipped(draft: dict) -> bool:
    parts = draft.get("parts") if isinstance(draft.get("parts"), dict) else (draft.get("judgment") or {}).get("parts") or {}
    return bool(draft.get("viewer_clip") or draft.get("titles") or (parts or {}).get("crowd")
                or "viewer clip" in str(draft.get("via") or "") or "crowd" in str(draft.get("via") or ""))


def drop_checks(draft: dict, cfg: dict | None = None) -> list[tuple[str, str]]:
    """The editor's named drop reasons that apply to this draft (OP_SPEC 4.1): [(effect, reason)], effect "drop"
    (never ships: a death screen where the channel penalises them) or "hold" (no ship call — look at the frames).
    Measured on Muse's 107 judged drafts (eval/eval_engine_call.py): each check touched no editor ship.
      dud        no viewer clip and under `min_speech_s` of the streamer's own speech: chat and loudness alone
                 (16 of the 20 wrong ship calls on Nov 24's replay). The streamer's laugh / volume burst does not
                 rescue it: all 5 wrong ship calls carrying a strong reaction were duds.
      death      the `death screen?` flag, or a frame the picture model reads as a death (`death_peak`): 3 of 4
                 death-screen drops, 0 of 71 other drafts with picture reads
      map/menu   the mean menu read (`menu`): 2 drops, 0 others
      still      no frame reads as action (`still_action_peak`): 12 drafts, all drops (3 of 3 cutscenes)
      adult      `adult_peak`: 6 drafts, all drops (the sexual one, 2 cutscenes, 3 duds)
    Picture checks need the picture model (`visual` on the draft); without it they don't apply."""
    from . import grade
    cfg = cfg if cfg is not None else grade.config()
    c = {**CALL_DEFAULTS, **(cfg.get("judge_call") or {})}
    out: list[tuple[str, str]] = []
    flags = " ".join(str(f) for f in draft.get("flags") or []).lower()
    v = draft.get("visual") if isinstance(draft.get("visual"), dict) else {}
    deaths_banned = float((cfg.get("penalties") or {}).get("death", 1.0)) < 1.0
    if deaths_banned and ("death screen?" in flags or (v.get("death_peak") or 0) >= c["death_peak"]):
        out.append(("drop", "death screen" + (f" (picture {v['death_peak']:.2f})" if (v.get("death_peak") or 0)
                                              >= c["death_peak"] else "?")))
    speech = _speech_s(draft)
    if speech is not None and speech < c["min_speech_s"] and not _viewer_clipped(draft):
        out.append(("hold", f"chat + loudness only: the streamer says little ({speech:.0f} s), no viewer clip"))
    if v:
        if (v.get("menu") or 0) >= c["menu"]:
            out.append(("hold", f"map/menu on screen (picture {v['menu']:.2f})"))
        if v.get("action_peak") is not None and v["action_peak"] < c["still_action_peak"]:
            out.append(("hold", f"little moves on screen — cutscene or standing around (action {v['action_peak']:.2f})"))
        if (v.get("adult_peak") or 0) >= c["adult_peak"]:
            out.append(("hold", f"possible adult scene (picture {v['adult_peak']:.2f})"))
    return out


def engine_call(draft: dict, cfg: dict | None = None) -> str | None:
    """The engine's own ship / hold / drop before an editor looks (J2 compares it with the editor's call)."""
    return engine_call_why(draft, cfg)[0]


def engine_call_why(draft: dict, cfg: dict | None = None) -> tuple[str | None, list[str]]:
    """(call, reasons). The call:
    drop  unsafe, already dropped, a death screen the channel penalises, or an engine score under `drop_below`
          (Nov 21: the editor dropped 14 of 15 held drafts between 35 and 55)
    ship  an engine score from `ship_from` that no named check (`drop_checks`) or the game-dialogue rule holds back
    hold  in between: look at the frames
    Thresholds: config/grading.json "judge_call"."""
    from . import grade

    if draft.get("unsafe") or draft.get("status") == "dropped":
        return "drop", ["unsafe" if draft.get("unsafe") else "already dropped"]
    score = draft.get("engine")
    if score is None:
        return None, []
    cfg = cfg if cfg is not None else grade.config()
    calls = {**CALL_DEFAULTS, **(cfg.get("judge_call") or {})}
    checks = drop_checks(draft, cfg)
    drops = [r for e, r in checks if e == "drop"]
    if drops:
        return "drop", drops
    if score < calls["drop_below"]:
        return "drop", [f"engine {score:.0f} under {calls['drop_below']:g}"]
    if score < calls["ship_from"]:
        return "hold", []
    holds = [r for e, r in checks if e == "hold"]
    if game_dialogue_uncarried(draft, cfg):
        holds.append("game dialogue nobody reacted to")    # OP_SPEC 4.3: the game's lines never carry a ship call
    return ("hold", holds) if holds else ("ship", [])


def game_dialogue_uncarried(draft: dict, cfg: dict | None = None) -> bool:
    """Game dialogue is ≥ 40% of the speech (the flag, or the stored speaker share) and neither viewers (a viewer
    clip / the crowd part) nor the streamer's own reaction carries it (pure but for config)."""
    from . import grade
    from . import speaker
    cfg = {**grade.DEFAULTS, **(cfg or grade.config())}
    flags = " ".join(str(f) for f in draft.get("flags") or []).lower()
    share = (draft.get("speakers") or {}).get("game_share")
    if "game dialogue" not in flags and not (share is not None and share >= speaker.GAME_SHARE_FLAG):
        return False
    parts = draft.get("parts") if isinstance(draft.get("parts"), dict) else (draft.get("judgment") or {}).get("parts") or {}
    react = draft.get("reaction", (draft.get("judgment") or {}).get("reaction"))
    crowd = (parts or {}).get("crowd") or 0
    viewers = crowd >= cfg["carry_crowd"] or bool(draft.get("titles")) or draft.get("via") == "viewer clips"
    return not viewers and (react or 0) < cfg["carry_reaction"]


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


def _call_fields(draft: dict) -> dict:
    call, why = engine_call_why(draft)
    return {"engine_call": call, **({"call_why": why} if why else {})}


def _record(draft: dict, verdict: str, reason: str, *, frames=(), override: str | None = None,
            slug: str | None = None, artifact: str | None = None) -> dict:
    snapshot = draft.get("judgment") or draft
    speakers = draft.get("speakers") or {}
    row = {"kind": "judgment", "id": draft["id"], "login": draft.get("login"),
           "verdict": verdict, "reason": reason, **_call_fields(draft),
           "engine": snapshot.get("engine"), "parts": snapshot.get("parts"),
           "speech_s": snapshot.get("speech_s", speakers.get("streamer_s")),
           "duration_s": snapshot.get("duration_s") or (
               draft["stop"] - draft["start"] if draft.get("start") is not None and draft.get("stop") is not None else None),
           "reaction": snapshot.get("reaction"), "why": snapshot.get("why") or [],
           "flags": list(draft.get("flags") or []), "at": _now_iso()}
    if speakers.get("game_share") is not None:          # the replay can apply the game-dialogue rule (OP_SPEC 4.3)
        row["speakers"] = {"game_share": speakers["game_share"]}
    if draft.get("titles") or draft.get("via"):
        row["viewer_clip"] = bool(draft.get("titles")) or draft.get("via") == "viewer clips"
    if draft.get("visual"):
        row["visual"] = draft["visual"]
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
    from collections import Counter
    from . import untrusted

    if rows is None:
        import hashlib
        from pathlib import Path

        path = Path(_path("judged.jsonl"))
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            raw = b""
            print(f"verdict snapshot unavailable: {path}")
        else:
            print(f"verdict snapshot SHA-256: {hashlib.sha256(raw).hexdigest()}")
        lines = raw.decode("utf-8").splitlines()
        rows = _parse_rows(lines)
        raw_count = sum(bool(line.strip()) for line in lines)
    else:
        raw_count = len(rows)
    editorial = [r for r in rows if r.get("kind") == "judgment" and r.get("id")
                 and r.get("verdict") in ("ship", "hold", "drop")
                 and r.get("classification") not in ("technical", "fixture")]
    latest = {r["id"]: r for r in editorial}
    totals = Counter(r["verdict"] for r in latest.values())
    revisions = len({(r["id"], r.get("artifact")) for r in editorial})
    cohort = {"raw": raw_count, "editorial_events": len(editorial), "viewed_revisions": revisions,
              "effective": len(latest), "excluded": raw_count - len(editorial),
              "ship": totals["ship"], "hold": totals["hold"], "drop": totals["drop"]}
    print(f"verdict counts: {raw_count} raw · {len(editorial)} editorial events · {revisions} viewed revisions · "
          f"{len(latest)} effective ({totals['ship']} ship / {totals['hold']} hold / {totals['drop']} drop) · "
          f"{cohort['excluded']} excluded")
    judged = [r for r in latest.values() if r.get("engine_call")]
    low = [r for r in judged if r.get("speech_s") is not None and r.get("duration_s")
           and r["speech_s"] < 0.2 * r["duration_s"]]
    stats = {"cohort": cohort}
    for name, group in (("overall", judged), ("low speech", low)):
        correct = sum(r["engine_call"] == r["verdict"] for r in group)
        stats[name] = {"correct": correct, "count": len(group),
                       "agreement": 100 * correct / len(group) if group else None}
        result = f"{100 * correct / len(group):.0f}%" if group else "no scored examples"
        print(f"{name}: {correct}/{len(group)} agree ({result})")
    print(f"low-speech keepers: {sum(r['verdict'] == 'ship' for r in low)} · unscored judgments: {len(latest) - len(judged)}")
    # agreement alone rewards "drop everything" when editors drop most drafts: show what the calls actually do
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
    today = [(engine_call({k: r.get(k) for k in ("engine", "flags", "parts", "reaction", "speakers", "speech_s",
                                                 "visual", "viewer_clip")}), r["verdict"]) for r in judged
             if r.get("engine") is not None]
    if today:
        ok = sum(c == v for c, v in today)
        stats["today_rule"] = (ok, len(today))
        t_ships = [v for c, v in today if c == "ship"]
        stats["today_ship_precision"] = (sum(v == "ship" for v in t_ships), len(t_ships))
        print(f"today's rule, ship calls right: {stats['today_ship_precision'][0]}/{len(t_ships)} "
              f"(eval/eval_engine_call.py breaks it down by named check)")
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


_FILLER = {"lol", "lmao", "bro", "omg", "chat", "clip", "clipped", "this", "that", "the", "a", "an", "is", "just", "so",
           "gta", "gta6", "vi", "6", "live", "stream", "moment", "funny", "lmfao", "xd", "kekw", "w", "l"}
_ACR = {"GTA", "NPC", "NPCS", "RP", "VI", "FBI", "CEO", "DLC", "USA", "TV", "OMG", "LOL", "W", "L"}


def headline_options(entry: dict, n: int = 3) -> dict:
    """Up to `n` distinct headline options from verified evidence (wishlist 7): each viewer-clip title — the crowd's
    own words for what happened — condensed to our card (2–5 words, ≤ 32 chars, the punch word in CAPS) and kept only
    if the headline checker passes it (no unsupported claim, no number or name the evidence lacks, no flagged word).
    The editor picks one. Fewer than `n`: `why_fewer` says why."""
    import re
    from . import safety
    transcript, titles = draft_evidence(entry)
    opts, rejected = [], []
    for raw in dict.fromkeys(t.strip() for t in titles if t and t.strip()):
        text = re.sub(r"https?://\S+|[@#]\w+", " ", raw)
        words = [w for w in re.findall(r"[A-Za-z][A-Za-z']*", text) if w.lower() not in _FILLER]
        if len(words) < 2:
            rejected.append(f"«{raw[:40]}»: too little to say")
            continue
        words = words[:5]
        while len(" ".join(words)) > 32 and len(words) > 2:
            words = words[:-1]
        punch = max((i for i, w in enumerate(words) if len(w) >= 3 and w.upper() not in _ACR), default=len(words) - 1)
        title = " ".join(w.upper() if i == punch else (w if w.isupper() and w in _ACR else w[:1].upper() + w[1:].lower()
                                                         if i == 0 else w.lower()) for i, w in enumerate(words))
        if any(o["title"].lower() == title.lower() for o in opts):
            continue
        chk = check_headline(entry, title)
        issues = chk["problems"] + [f"unsupported: {u}" for u in chk["unsupported"]]
        if safety.screen(headline=title)["flagged"] or safety.death_hint("", [title]):
            issues.append("safety / death-screen wording")
        if issues:
            rejected.append(f"{title}: {'; '.join(issues)[:80]}")
            continue
        opts.append({"title": title, "from": raw[:80]})
        if len(opts) >= n:
            break
    why = None
    if len(opts) < n:
        why = (f"{len(opts)} of {n}: " + ("no viewer-clip titles to work from — write it from the frames" if not titles
               else f"{len(titles)} viewer-clip title(s), {len(rejected)} failed the checker"
                    + (f" ({rejected[0]})" if rejected else "")))
    return {"options": opts, "why_fewer": why, "rejected": rejected}


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
