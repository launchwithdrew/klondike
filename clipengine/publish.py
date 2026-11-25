"""publish — one moment, all the way: render -> checks -> drop -> (optionally) post.

The front half (clipengine) and the back half (tools/drop.mjs + engine/post.mjs)
meet here, so whoever runs the op — the daily agent, or a backup machine —
does the whole thing with one command per clip:

  python -m clipengine publish --scan runs/scan-1530 --pick 2 \
      --title "Streamer is playing it like THE SIMS 😭💪" --quote "W workout, bro" \
      --line "The streamer makes his character work out, shower and go to bed like a life sim." \
      --frames-checked [--post]

What it guarantees:
  * the stack look, logo, captions, censor+bleep (render.py)
  * the sexual-content screen on transcript + headline (safety.py) — refuses if flagged
  * moment_at = when it happened LIVE (VOD start + offset, or the recording's wall clock),
    so the ledger's moment->live minutes are real
  * description = your one true line + the creator's hashtags (config/creators.json)
  * never re-clips a moment already published (data/clipped.jsonl)
  * --brand-safe is only passed to drop.mjs with --frames-checked: a person/agent must
    have LOOKED at the frames (nudity on screen is not in the transcript)
  * --post runs the poster on THIS machine right after the drop (needs its signed-in Chrome)
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import re
import subprocess

from . import safety as SF
from . import untrusted as UT
from . import twitch as T
from . import grade as G
from . import gate as GT

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIPPED = os.path.join(REPO, "data", "clipped.jsonl")
TASTE = os.path.join(REPO, "data", "taste.jsonl")       # explicit --taste no verdicts (learn.taste reads them)
TASTE_QUESTION = ("Would a fan of this streamer be glad to see this posted? "
                  "Not cringe, not mean-spirited, not a dead moment, not a spoiler dump")
CREATORS = os.path.join(REPO, "config", "creators.json")
BANNED_TITLE = re.compile(r"\b(kill(s|ed|ing)?|gunpoint|murder\w*|shoot(s|ing)?|dead body)\b", re.I)


def creators() -> dict:
    try:
        return json.load(open(CREATORS))["creators"]
    except Exception:
        return {}


def _tag(login: str) -> str:
    return creators().get(login.lower(), {}).get("tag") or f"#{re.sub(r'[^a-z0-9]', '', login.lower())}"


def hashtags(login: str) -> str:
    from . import brand as B
    return B.hashtags(_tag(login))


def caption(line: str, login: str, question: str | None = None) -> str:
    """What goes under the post: the one true line, the optional question, a few hashtags (brand pack:
    caption_style, max_hashtags)."""
    from . import brand as B
    return B.caption(line, _tag(login), question)


def check_question(question: str | None) -> list[str]:
    if not question or not question.strip():
        return []
    q = question.strip()
    problems = []
    if "\n" in q:
        problems.append("question must be one line")
    if len(q) > 120:
        problems.append(f"question is {len(q)} chars (max 120)")
    if not q.endswith("?"):
        problems.append("question must end with '?'")
    if BANNED_TITLE.search(q):
        problems.append(f"question uses '{BANNED_TITLE.search(q).group(0)}' — say it another way")
    if SF.screen(headline=q)["flagged"]:
        problems.append("question has sexual terms")
    return problems


def load_clipped() -> list[dict]:
    if not os.path.isfile(CLIPPED):
        return []
    return [json.loads(l) for l in open(CLIPPED) if l.strip()]


def already_clipped(vod_or_login: str, start_s: float, end_s: float) -> dict | None:
    """A published clip that overlaps this window by more than 10 s."""
    for r in load_clipped():
        if r.get("source") != vod_or_login:
            continue
        if min(end_s, r["end_s"]) - max(start_s, r["start_s"]) > 10:
            return r
    return None


def _slugify(login: str, title: str) -> str:
    words = re.findall(r"[a-z0-9]+", title.lower())
    stop = {"in", "gta", "6", "the", "a", "and", "his", "her", "it", "to", "of", "for", "s", "by", "at"}
    core = [w for w in words if w not in stop][1:5]
    name = creators().get(login.lower(), {}).get("name", login).lower()
    return re.sub(r"[^a-z0-9_]", "", f"{name}_{'_'.join(core)}")[:48] or f"{name}_clip"


def check_title(title: str) -> list[str]:
    problems = []
    if len(title) > 100:
        problems.append(f"title is {len(title)} chars (max 100)")
    if BANNED_TITLE.search(title):
        problems.append(f"title uses '{BANNED_TITLE.search(title).group(0)}' — rules/headlines.md: avoid kill/gunpoint "
                        "wording (posting was refused on it once); say what happened another way")
    hs = SF.screen(headline=title)
    if hs["flagged"]:
        problems.append("title has sexual terms: " + hs["why"])
    return problems


def drop(file: str, slug: str, title: str, description: str, moment_at: str, post: bool,
         push: bool = True, check_only: bool = False, platforms: list[str] | None = None,
         schedule: dict | None = None, grade: dict | None = None, moment_window: dict | None = None) -> None:
    from . import autopilot as AP
    from . import finish as FN
    from . import migrate as MG
    login = (moment_window or {}).get("login")
    roster = creators() if login else {}
    AP.refuse_never(login, roster)
    if post and not check_only:
        MG.guard_publish()                       # a shadow or handed-over machine never drops for posting
    receipt = FN.gate(file)                      # a clip with a look other than the approved finish never drops
    cmd = ["node", os.path.join(REPO, "tools", "drop.mjs"), "--file", file, "--slug", slug,
           "--title", title, "--description", description, "--moment-at", moment_at, "--brand-safe"]
    if platforms:
        cmd += ["--platforms", ",".join(platforms)]
    if schedule:
        cmd += ["--schedule", ",".join(f"{p}={t}" for p, t in schedule.items())]
    if grade:
        cmd += ["--grade", json.dumps(grade, separators=(",", ":"))]
    if moment_window:
        cmd += ["--moment-window", json.dumps(moment_window, separators=(",", ":"))]
    if login:
        creator = roster.get(login.lower()) or {}
        rights = {"policy": AP.policy(login, roster), "policy_source": creator.get("policy_source"),
                  "policy_checked": creator.get("policy_checked"),
                  "evaluated_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        cmd += ["--rights", json.dumps(rights, separators=(",", ":"))]
    if receipt:
        cmd += ["--finish", json.dumps({k: receipt[k] for k in ("key", "approved", "matches_approved", "engine",
                                                                  "stage") if k in receipt}, separators=(",", ":"))]
    if check_only:
        cmd.append("--check")
    elif not push:
        cmd.append("--no-push")
    subprocess.run(cmd, check=True)
    if post and not check_only:
        from . import handpost as HP
        if HP.enabled():
            print(f"ready for you to post: clipengine to-post  (then clipengine posted --slug {slug} --platform … --url …)")
        else:
            subprocess.run(["node", os.path.join(REPO, "engine", "post.mjs"), "--once"], check=True)


def _commit(path: str, msg: str, push: bool) -> None:
    from . import repo as RP
    if push and RP.sync():
        g = ["git", "-C", REPO]
        subprocess.run(g + ["add", path], check=False)
        subprocess.run(g + ["-c", "user.name=clipengine", "-c", "user.email=clipengine@users.noreply.github.com",
                            "commit", "-qm", msg], check=False)
        subprocess.run(g + ["push", "-q"], check=False)


def record_clipped(entry: dict, push: bool) -> None:
    os.makedirs(os.path.dirname(CLIPPED), exist_ok=True)
    with open(CLIPPED, "a") as fh:
        fh.write(json.dumps(entry) + "\n")
    _commit(CLIPPED, f"clipped: {entry['slug']}", push)


def parse_taste(v: str | None) -> bool | None:
    """--taste yes|no → True / False; not given → None."""
    if v is None:
        return None
    v = v.strip().lower()
    if v in ("yes", "y"):
        return True
    if v in ("no", "n"):
        return False
    raise ValueError(f"--taste takes yes or no, not {v!r}")


def taste_no(login: str, ref: str, push: bool = True, title: str | None = None,
             draft: dict | None = None) -> None:
    """--taste no: never post it. Recorded as a skip with reason "taste" (learn.taste pushes the streamer down)."""
    os.makedirs(os.path.dirname(TASTE), exist_ok=True)
    with open(TASTE, "a") as fh:
        fh.write(json.dumps({"login": (login or "").lower(), "ref": ref, "verdict": "no", "reason": "taste",
                             **({"title": title} if title else {}),
                             "at": dt.datetime.now(dt.timezone.utc).isoformat()}) + "\n")
    from . import judge as JG
    JG.record(draft or {"id": ref, "login": login}, "drop", "taste")
    _commit(TASTE, f"taste: skipped {ref}", push)
    raise SystemExit(f"NOT POSTED — taste: no. Skipped {ref} (reason: taste); learn counts it against {login or 'this streamer'}.")


def publish_scan(scan_dir: str, pick: int, title: str, line: str, quote: str | None = None,
                 slug: str | None = None, frames_checked: bool = False, post: bool = False,
                 push: bool = True, check_only: bool = False, rubric: list[str] | None = None,
                 question: str | None = None, taste: bool | None = None, truth_override: str | None = None,
                 draft: dict | None = None, **render_kw) -> dict:
    from . import pipeline as P
    from . import facecam as FC
    res = json.load(open(os.path.join(scan_dir, "scan.json")))
    vod = res["vod"]
    c = next(x for x in res["candidates"] if x["rank"] == pick)
    sig = G.signals_from_candidate(c, vod)
    if taste is False:
        from . import judge as JG
        ref = f"{vod['id']}#{c['peak_s']}"
        sample = JG.score_snapshot(G.engine_score(sig), sig)
        taste_no(vod.get("login", ""), ref, push, title,
                 draft=draft or {"id": ref, "login": vod["login"], "thumbs": c.get("thumbs"), **sample})
    problems = check_title(title) + check_question(question)
    if problems:
        raise SystemExit("PUBLISH REFUSED: " + "; ".join(problems))
    from .admission import track_draft
    with track_draft(draft), FC.creator(vod.get("login")):
        rep = P.render_pick(scan_dir, pick, layout="stack", title=title, quote=quote, **render_kw)
    s, e = rep["window"]
    hit = already_clipped(vod["id"], s, e)
    if hit:
        raise SystemExit(f"PUBLISH REFUSED: this moment was already published as '{hit['slug']}'")
    moment = dt.datetime.fromisoformat(vod["created_at"].replace("Z", "+00:00")) + dt.timedelta(seconds=c["peak_s"])
    if not draft:
        draft = {"id": f"{vod['id']}#{c['peak_s']}", "login": vod["login"], "thumbs": c.get("thumbs")}
    origin = dt.datetime.fromisoformat(vod["created_at"].replace("Z", "+00:00")).timestamp()
    return _finish(rep, vod["login"], vod["id"], s, e, moment, title, line, quote, slug, frames_checked, post, push,
                   check_only, sig, rubric, question, taste, truth_override=truth_override, draft=draft, clock_window=(origin + s, origin + e))


def source_vision(rec_dir: str, start_ts: float, end_ts: float) -> dict:
    """Frame checks + liveliness on the recording itself (the render's layout and card would skew them):
    {"flags": [...], "liveliness": 0–1}, or {} when the recording can't be read."""
    import tempfile
    from . import live as L
    from . import vision as VI
    try:
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src.ts")
            first = L.cut_source(rec_dir, start_ts, end_ts, src)
            vis = VI.check(src, max(0.0, start_ts - first), end_ts - start_ts)
        return {"flags": vis["flags"], "liveliness": vis["liveliness"]} if vis["frames"] else {}
    except Exception:
        return {}


def publish_window(rec_dir: str, start_ts: float, end_ts: float, title: str, line: str, quote: str | None = None,
                   slug: str | None = None, frames_checked: bool = False, post: bool = False,
                   push: bool = True, check_only: bool = False, rubric: list[str] | None = None,
                   chat_score: float | None = None, question: str | None = None, pre: dict | None = None,
                   taste: bool | None = None, truth_override: str | None = None, draft: dict | None = None, **kw) -> dict:
    """For a live recording (streams with no VOD): wall-clock window. A sentinel draft passes the chat
    strength its detector measured live (`chat_score`) — a recording keeps no chat of its own."""
    from . import live as L
    from . import render as V
    from . import facecam as FC
    login = json.load(open(os.path.join(rec_dir, "recording.json")))["login"]
    if taste is False:
        taste_no(login, f"{login}@{int(start_ts)}", push, title, draft=draft)
    problems = check_title(title) + check_question(question)
    if problems:
        raise SystemExit("PUBLISH REFUSED: " + "; ".join(problems))
    hit = already_clipped(login, start_ts, end_ts)
    if hit:
        raise SystemExit(f"PUBLISH REFUSED: this moment was already published as '{hit['slug']}'")
    os.makedirs(os.path.join(rec_dir, "renders"), exist_ok=True)
    out = os.path.join(rec_dir, "renders", f"{login}_{int(start_ts)}.mp4")
    from .admission import track_draft
    with track_draft(draft):
        if pre:                                     # pre-rendered draft: only the headline card is burned in
            info = from_prerender(pre, out, start_ts, end_ts, title, quote, payoff_ts=kw.get("payoff_ts"))
        else:
            with FC.creator(login):                 # the facecam layout looks up this streamer's webcam
                info = L.render_window(rec_dir, start_ts, end_ts, out, title, quote, watermark=V.WATERMARK, **kw)
    payoff = kw.get("payoff_ts")
    moment_ts = payoff if payoff is not None and start_ts <= payoff < end_ts else (start_ts + end_ts) / 2
    moment = dt.datetime.fromtimestamp(moment_ts, dt.timezone.utc)
    rep = {**info, "window": [start_ts, end_ts], "frames": V.frames(out, [1, (end_ts - start_ts) / 2, end_ts - start_ts - 1],
                                                                     os.path.join(rec_dir, "renders", "frames"))}
    tr = json.load(open(out.rsplit(".mp4", 1)[0] + ".transcript.json"))
    if kw.get("words"):                  # the draft's own words: wall-clock times and speaker tags
        words = [w for w in kw["words"] if start_ts <= w["start"] < end_ts]
    else:                                # caption cards are clip seconds: back to wall clock
        words = [{**w, "start": w["start"] + start_ts, "end": w["end"] + start_ts}
                 for seg in tr.get("segments", []) for w in seg.get("words", []) if w.get("start", 0) < end_ts - start_ts]
    sig = G.signals_from_live(rec_dir, login, start_ts, end_ts, words)
    sig["words_all"] = words
    sv = source_vision(rec_dir, start_ts, end_ts)
    sig["flags"] = G.merge_vision_flags(list(sig.get("flags") or []), sv.get("flags") or [])
    sig["liveliness"] = sv.get("liveliness")
    if chat_score is not None:
        sig["chat_score"] = chat_score
    if draft:
        sig["titles"] = list(dict.fromkeys([*(sig.get("titles") or []), *(draft.get("titles") or [])]))
    return _finish(rep, login, login, start_ts, end_ts, moment, title, line, quote, slug, frames_checked, post, push,
                   check_only, sig, rubric, question, taste, truth_override=truth_override, draft=draft, clock_window=(start_ts, end_ts))


def from_prerender(pre: dict, out: str, start_ts: float, end_ts: float, title: str, quote: str | None,
                   payoff_ts: float | None = None) -> dict:
    """Finish a pre-rendered draft: burn the card (and a trim) into it; its transcript follows the trim."""
    import sys as _sys
    from . import render as V
    trim = max(0.0, start_ts - pre["start"])
    dur = end_ts - start_ts
    print(f"[render] pre-rendered draft: burning the headline card{f' (trim {trim:.1f} s)' if trim else ''}…",
          file=_sys.stderr, flush=True)
    burned = V.burn_card(pre["mp4"], out, title, quote, pre.get("slot") or {}, trim_start=trim, dur=dur,
                         captions=pre.get("captions"),
                         payoff_s=(payoff_ts - start_ts) if payoff_ts is not None else None)
    tr = json.load(open(pre["mp4"].rsplit(".mp4", 1)[0] + ".transcript.json"))
    shift = lambda a, b: (round(a - trim, 2), round(b - trim, 2))
    segs = []
    for seg in tr.get("segments", []):
        if seg["end"] - trim <= 0 or seg["start"] - trim >= dur:
            continue
        a, b = shift(seg["start"], seg["end"])
        segs.append({**seg, "start": max(0.0, a), "end": min(dur, b),
                     "words": [{**w, "start": max(0.0, w["start"] - trim), "end": min(dur, w["end"] - trim)}
                               for w in seg.get("words", [])]})
    bleeps = [[max(0.0, a - trim), min(dur, b - trim)] for a, b in tr.get("bleep_intervals", [])
              if b - trim > 0 and a - trim < dur]
    json.dump({"clip_start_vod_s": start_ts, "clip_end_vod_s": end_ts, "segments": segs, "bleep_intervals": bleeps},
              open(out.rsplit(".mp4", 1)[0] + ".transcript.json", "w"), indent=2)
    return {"out": out, "cards": len(segs), "bleeps": bleeps, "watermark": pre.get("watermark", True),
            "layout": pre.get("layout"), "facecam": pre.get("facecam"), "title": title, "quote": quote,
            "prerendered": True, "pre_audit": pre.get("audit"), "pre_vision": pre.get("vision"),
            "audit": burned.get("audit")}


def _markers(draft: dict | None) -> list[str]:
    """The draft's confidence markers (sentinel.confidence_markers), carried into the publish report and the drop."""
    if not draft:
        return []
    from . import sentinel as SN
    try:
        return SN.confidence_markers(draft)
    except Exception:
        return []



def _headline_fit(title: str, draft: dict | None) -> float | None:
    """Advisory (visual.py): how much better the headline describes the draft's frames than a plain game scene."""
    sheet = (draft or {}).get("sheet")
    if not title or not sheet or not os.path.exists(sheet):
        return None
    try:
        from . import visual as VS
        if VS.mode() == "off":
            return None
        return VS.headline_fit(title, VS.tiles(sheet))
    except Exception:
        return None

def _finish(rep, login, source, s, e, moment, title, line, quote, slug, frames_checked, post, push, check_only,
            sig: dict | None = None, rubric: list[str] | None = None, question: str | None = None,
            taste: bool | None = None, *, truth_override: str | None = None, draft: dict | None = None,
            clock_window: tuple[float, float] | None = None):
    # transcript-level screen on what is actually in the cut
    tr = json.load(open(rep["out"].rsplit(".mp4", 1)[0] + ".transcript.json"))
    # what is in OUR cut (card times are clip seconds): a --start trim drops the words before it
    said = " ".join(seg["text"] for seg in tr["segments"] if 0 <= seg.get("start", 0) < (e - s))
    raw_words = (sig or {}).get("words_all") or []
    spoken = " ".join(w.get("w", "").strip() for w in raw_words)
    sf = SF.screen(said, headline=(title or "") + " " + (quote or ""))
    if not sf["flagged"]:
        audible = rep.get("speech_safety") or SF.screen(spoken)
        if audible["flagged"]:
            sf = audible
    report = {"out": rep["out"], "frames": rep.get("frames"), "safety": sf, "moment_at": moment.isoformat(),
              "layout": rep.get("layout"), **({"facecam_note": rep["facecam"]["note"]}
                                                if (rep.get("facecam") or {}).get("note") else {}),
              "caption": caption(line, login, question),
              "length_s": round(e - s, 1), "captions_preview": UT.quote(said, 400),
              **({"render_audit": rep["audit"]["line"]} if isinstance(rep.get("audit"), dict) else {})}
    # judge like an editor: the censor checked on the render itself, the headline checked against the moment
    from . import censor as CN
    if rep.get("prerendered") and (rep.get("pre_audit") or {}).get("ok"):
        report["prerendered"] = True                 # audited when it was pre-rendered; the card adds no audio
        if (rep.get("pre_audit") or {}).get("fixed"):
            report["censor"] = "; ".join(rep["pre_audit"]["fixed"])
    elif os.environ.get("CLIPENGINE_NO_AUDIT") != "1":
        aud = CN.ensure_clean(rep["out"])
        if aud.get("fixed"):
            report["censor"] = "; ".join(aud["fixed"])
        if not aud["ok"]:
            raise SystemExit("PUBLISH REFUSED: the render still has an uncensored word "
                             f"({', '.join(aud['captions'] + [w['w'] for w in aud['audio']])}) — re-run with --bleep WORD")
    spoken = ". ".join(part for part in (spoken, said) if part)
    missing = SF.headline_support(title, spoken, (sig or {}).get("titles"),
                                  names=[login, creators().get(login.lower(), {}).get("name", "")])
    if missing:
        report["warnings"] = [f"headline words not in the transcript or viewer titles: {', '.join(missing[:5])} — "
                              "make sure the cut SHOWS it (rules/headlines.md)"]
    adult = [f for f in (draft or {}).get("flags") or [] if "adult scene" in str(f)]
    if adult:
        report.setdefault("warnings", []).append(adult[0] + " — rules/safety.md: strip clubs, underwear and nudity "
                                                 "don't post; answer --frames-checked only after looking")
    fit = _headline_fit(title, draft)
    if fit is not None:
        # Not evidence either way (§4.2, 24 Nov, eval/eval_headline_fit.py): on 81 GTA VI viewer clips the picture
        # model scored a title on its own frames no better than on another clip of the same streamer (AUC 0.54).
        report["visual_headline"] = (f"{fit:+.3f} vs a plain 'video game scene' — the picture model can't tell a true "
                                     "headline from a false one: check the payoff is ON SCREEN yourself")
    if not sf["flagged"] and (draft or {}).get("unsafe"):
        sf = {**sf, "flagged": True, "why": draft["unsafe"]}
        report["safety"] = sf
    if sf["flagged"]:
        report["status"] = "REFUSED — sexual content: " + sf["why"]
        print(json.dumps(report, indent=2))
        raise SystemExit("PUBLISH REFUSED: " + report["status"])
    if frames_checked and missing and not (truth_override or "").strip():
        raise SystemExit("PUBLISH REFUSED: unsupported headline claims: " + ", ".join(missing) +
                         " — edit the headline or pass --truth-override with what you verified in the cut")
    if not wm_ok(rep):
        raise SystemExit("PUBLISH REFUSED: no logo — copy assets/watermark.png into the repo (setup/setup.sh checks this)")
    # vision: cutscene / menu / loading screen, from the frames themselves
    from . import vision as VI
    vis = rep.get("pre_vision") or VI.check(rep["out"])      # a pre-render was checked when it was made
    report["vision"] = {k: vis[k] for k in ("cuts_per_min", "static", "dark", "flags")}
    more = vis["flags"]
    if (sig or {}).get("liveliness") is not None:      # the source frames were checked: the render (layout, card,
        more = [f for f in more if "cutscene" in f.lower()]   # logo) adds only what it can see better
    sig = {**(sig or {}), "flags": G.merge_vision_flags(list((sig or {}).get("flags") or []), more)}
    # banger score: engine signals now, operator rubric once the frames are checked
    eng = G.engine_score({**sig, "unsafe": sf["why"] if sf["flagged"] else ""})
    rub = G.rubric_score(rubric)
    final = G.banger(eng["score"], rub)
    report["grade"] = {"engine": eng, "rubric": rubric, **final}
    report["confidence"] = _markers(draft) or ["every check ran"]
    floor = min(r["min_score"] for r in GT.config()["platforms"].values())
    if not frames_checked:
        if final["best_possible"] < floor:
            report["status"] = (f"WON'T POST — engine score {eng['score']} means even 4/4 on the rubric reaches only "
                                f"{final['best_possible']}, under every platform's minimum ({floor}). Find a stronger moment.")
        else:
            report["status"] = ("RENDERED, NOT DROPPED — look at the frames and read the captions above, then re-run "
                                "with --frames-checked --rubric <the checks that pass: hook,context,payoff,share, "
                                "or none> --taste yes|no (nudity / body close-ups / strip clubs are not in the "
                                "transcript). Taste: " + TASTE_QUESTION + ".")
        print(json.dumps(report, indent=2))
        return report
    if rub is None:
        raise SystemExit("PUBLISH REFUSED: with --frames-checked, also pass --rubric with the checks that pass "
                         "(hook,context,payoff,share) — or --rubric none")
    if taste is None:
        raise SystemExit("PUBLISH REFUSED: with --frames-checked, also answer --taste yes|no — " + TASTE_QUESTION + "?")
    decision = GT.decide(final["score"], moment.timestamp(), login=login,
                         clip={"title": title, "length_s": e - s, "layout": rep.get("layout") or "stack",
                               "caption_style": "line+question" if (question or "").strip() else "line"})
    report["gate"] = {p: {**d, "at": None if d["at"] is None else
                          dt.datetime.fromtimestamp(d["at"], dt.timezone.utc).isoformat()} for p, d in decision.items()}
    platforms = [p for p, d in decision.items() if d["action"] in ("now", "scheduled")]
    schedule = {p: report["gate"][p]["at"] for p, d in decision.items() if d["action"] == "scheduled"}
    from . import judge as JG
    sample = JG.score_snapshot(eng, sig)
    example = {"id": f"{source}@{s:g}", "login": login, "start": s, "stop": e, **(draft or {})}
    if not example.get("judgment"):
        example = {**sample, **example, "judgment": sample}
    accepted_override = truth_override.strip() if missing and truth_override else None
    if not platforms:
        if post and not check_only:
            JG.record(example, "ship", "operator approved; distribution gate held it",
                      frames=rep.get("frames"), override=accepted_override)
        report["status"] = f"NOT POSTED — banger score {final['score']} ({final['tier']}): {GT.summary(decision)}"
        print(json.dumps(report, indent=2))
        return report
    slug = slug or _slugify(login, title)
    description = caption(line, login, question)
    from . import handpost
    from . import moments

    bounds = clock_window or (moment.timestamp() - (e - s) / 2, moment.timestamp() + (e - s) / 2)
    moment_window = moments.metadata(login, *bounds)
    lock_path = os.path.join(REPO, "runs", "publish.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        records = load_clipped() + [row for row in handpost._ledger() if row.get("status") == "live"]
        duplicate = moments.match(login, *bounds, records)
        if duplicate:
            raise SystemExit(f"PUBLISH REFUSED: this moment was already posted as {duplicate['slug']}")
        drop(rep["out"], slug, title, description, moment.isoformat(), post, push, check_only,
             platforms=platforms, schedule=schedule,
             grade={"score": final["score"], "tier": final["tier"], "engine": eng["score"], "rubric": rubric,
                    "parts": eng["parts"], "confidence": _markers(draft)}, moment_window=moment_window)
        if not check_only:
            JG.record(example, "ship", "approved for posting", frames=rep.get("frames"), artifact=JG.artifact_id(example, rep.get("out")),
                      override=accepted_override, slug=slug)
            from .admission import draft_key
            record_clipped({"slug": slug, "draft_key": draft_key(draft), "source": source, "login": login,
                            "start_s": s, "end_s": e,
                            "moment_at": moment.isoformat(), **moment_window, "title": title, "grade": final["score"],
                            "engine": eng["score"], "rubric": rubric, "taste": "yes", "platforms": platforms,
                            "explore": [p for p, d in decision.items() if d.get("explore")],
                            "layout": rep.get("layout") or "stack",
                            "caption_style": "line+question" if (question or "").strip() else "line",
                            "parts": eng.get("parts"),
                            "at": dt.datetime.now(dt.timezone.utc).isoformat()}, push)
    report.update({"status": ("CHECKED (not dropped)" if check_only else ("DROPPED + POSTED" if post else "DROPPED"))
                   + f" — banger {final['score']} ({final['tier']}): {GT.summary(decision)}",
                   "slug": slug, "description": description})
    print(json.dumps(report, indent=2))
    return report


def wm_ok(rep: dict) -> bool:
    return bool(rep.get("watermark", True))
