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
CREATORS = os.path.join(REPO, "config", "creators.json")
BANNED_TITLE = re.compile(r"\b(kill(s|ed|ing)?|gunpoint|murder\w*|shoot(s|ing)?|dead body)\b", re.I)


def creators() -> dict:
    try:
        return json.load(open(CREATORS))["creators"]
    except Exception:
        return {}


def hashtags(login: str) -> str:
    tag = creators().get(login.lower(), {}).get("tag") or f"#{re.sub(r'[^a-z0-9]', '', login.lower())}"
    from . import brand as B
    return B.hashtags(tag)


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
         schedule: dict | None = None, grade: dict | None = None) -> None:
    cmd = ["node", os.path.join(REPO, "tools", "drop.mjs"), "--file", file, "--slug", slug,
           "--title", title, "--description", description, "--moment-at", moment_at, "--brand-safe"]
    if platforms:
        cmd += ["--platforms", ",".join(platforms)]
    if schedule:
        cmd += ["--schedule", ",".join(f"{p}={t}" for p, t in schedule.items())]
    if grade:
        cmd += ["--grade", json.dumps(grade, separators=(",", ":"))]
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


def record_clipped(entry: dict, push: bool) -> None:
    os.makedirs(os.path.dirname(CLIPPED), exist_ok=True)
    with open(CLIPPED, "a") as fh:
        fh.write(json.dumps(entry) + "\n")
    from . import repo as RP
    if push and RP.sync():
        g = ["git", "-C", REPO]
        subprocess.run(g + ["add", CLIPPED], check=False)
        subprocess.run(g + ["-c", "user.name=clipengine", "-c", "user.email=clipengine@users.noreply.github.com",
                            "commit", "-qm", f"clipped: {entry['slug']}"], check=False)
        subprocess.run(g + ["push", "-q"], check=False)


def publish_scan(scan_dir: str, pick: int, title: str, line: str, quote: str | None = None,
                 slug: str | None = None, frames_checked: bool = False, post: bool = False,
                 push: bool = True, check_only: bool = False, rubric: list[str] | None = None,
                 **render_kw) -> dict:
    from . import pipeline as P
    res = json.load(open(os.path.join(scan_dir, "scan.json")))
    vod = res["vod"]
    c = next(x for x in res["candidates"] if x["rank"] == pick)
    problems = check_title(title)
    if problems:
        raise SystemExit("PUBLISH REFUSED: " + "; ".join(problems))
    rep = P.render_pick(scan_dir, pick, layout="stack", title=title, quote=quote, **render_kw)
    s, e = rep["window"]
    hit = already_clipped(vod["id"], s, e)
    if hit:
        raise SystemExit(f"PUBLISH REFUSED: this moment was already published as '{hit['slug']}'")
    moment = dt.datetime.fromisoformat(vod["created_at"].replace("Z", "+00:00")) + dt.timedelta(seconds=c["peak_s"])
    sig = G.signals_from_candidate(c, vod)
    return _finish(rep, vod["login"], vod["id"], s, e, moment, title, line, quote, slug, frames_checked, post, push,
                   check_only, sig, rubric)


def publish_window(rec_dir: str, start_ts: float, end_ts: float, title: str, line: str, quote: str | None = None,
                   slug: str | None = None, frames_checked: bool = False, post: bool = False,
                   push: bool = True, check_only: bool = False, rubric: list[str] | None = None,
                   chat_score: float | None = None, **kw) -> dict:
    """For a live recording (streams with no VOD): wall-clock window. A sentinel draft passes the chat
    strength its detector measured live (`chat_score`) — a recording keeps no chat of its own."""
    from . import live as L
    from . import render as V
    login = json.load(open(os.path.join(rec_dir, "recording.json")))["login"]
    problems = check_title(title)
    if problems:
        raise SystemExit("PUBLISH REFUSED: " + "; ".join(problems))
    hit = already_clipped(login, start_ts, end_ts)
    if hit:
        raise SystemExit(f"PUBLISH REFUSED: this moment was already published as '{hit['slug']}'")
    os.makedirs(os.path.join(rec_dir, "renders"), exist_ok=True)
    out = os.path.join(rec_dir, "renders", f"{login}_{int(start_ts)}.mp4")
    info = L.render_window(rec_dir, start_ts, end_ts, out, title, quote, watermark=V.WATERMARK, **kw)
    moment = dt.datetime.fromtimestamp((start_ts + end_ts) / 2, dt.timezone.utc)
    rep = {**info, "window": [start_ts, end_ts], "frames": V.frames(out, [1, (end_ts - start_ts) / 2, end_ts - start_ts - 1],
                                                                     os.path.join(rec_dir, "renders", "frames"))}
    tr = json.load(open(out.rsplit(".mp4", 1)[0] + ".transcript.json"))
    words = [w for seg in tr.get("segments", []) for w in seg.get("words", [])
             if start_ts <= w.get("start", 0) <= end_ts]
    sig = G.signals_from_live(rec_dir, login, start_ts, end_ts, words)
    if chat_score is not None:
        sig["chat_score"] = chat_score
    return _finish(rep, login, login, start_ts, end_ts, moment, title, line, quote, slug, frames_checked, post, push,
                   check_only, sig, rubric)


def _finish(rep, login, source, s, e, moment, title, line, quote, slug, frames_checked, post, push, check_only,
            sig: dict | None = None, rubric: list[str] | None = None):
    # transcript-level screen on what is actually in the cut
    tr = json.load(open(rep["out"].rsplit(".mp4", 1)[0] + ".transcript.json"))
    said = " ".join(seg["text"] for seg in tr["segments"])
    sf = SF.screen(said, headline=(title or "") + " " + (quote or ""))
    report = {"out": rep["out"], "frames": rep.get("frames"), "safety": sf, "moment_at": moment.isoformat(),
              "length_s": round(e - s, 1), "captions_preview": UT.quote(said, 400)}
    if sf["flagged"]:
        report["status"] = "REFUSED — sexual content: " + sf["why"]
        print(json.dumps(report, indent=2))
        raise SystemExit("PUBLISH REFUSED: " + report["status"])
    if not wm_ok(rep):
        raise SystemExit("PUBLISH REFUSED: no logo — copy assets/watermark.png into the repo (setup/setup.sh checks this)")
    # vision: cutscene / menu / loading screen, from the frames themselves
    from . import vision as VI
    vis = VI.check(rep["out"])
    report["vision"] = {k: vis[k] for k in ("cuts_per_min", "static", "dark", "flags")}
    sig = {**(sig or {}), "flags": list((sig or {}).get("flags") or []) + vis["flags"]}
    # banger score: engine signals now, operator rubric once the frames are checked
    eng = G.engine_score({**sig, "unsafe": sf["why"] if sf["flagged"] else ""})
    rub = G.rubric_score(rubric)
    final = G.banger(eng["score"], rub)
    report["grade"] = {"engine": eng, "rubric": rubric, **final}
    floor = min(r["min_score"] for r in GT.config()["platforms"].values())
    if not frames_checked:
        if final["best_possible"] < floor:
            report["status"] = (f"WON'T POST — engine score {eng['score']} means even 4/4 on the rubric reaches only "
                                f"{final['best_possible']}, under every platform's minimum ({floor}). Find a stronger moment.")
        else:
            report["status"] = ("RENDERED, NOT DROPPED — look at the frames and read the captions above, then re-run "
                                "with --frames-checked --rubric <the checks that pass: hook,context,payoff,share, "
                                "or none> (nudity / body close-ups / strip clubs are not in the transcript)")
        print(json.dumps(report, indent=2))
        return report
    if rub is None:
        raise SystemExit("PUBLISH REFUSED: with --frames-checked, also pass --rubric with the checks that pass "
                         "(hook,context,payoff,share) — or --rubric none")
    decision = GT.decide(final["score"], moment.timestamp(), login=login)
    report["gate"] = {p: {**d, "at": None if d["at"] is None else
                          dt.datetime.fromtimestamp(d["at"], dt.timezone.utc).isoformat()} for p, d in decision.items()}
    platforms = [p for p, d in decision.items() if d["action"] in ("now", "scheduled")]
    schedule = {p: report["gate"][p]["at"] for p, d in decision.items() if d["action"] == "scheduled"}
    if not platforms:
        report["status"] = f"NOT POSTED — banger score {final['score']} ({final['tier']}): {GT.summary(decision)}"
        print(json.dumps(report, indent=2))
        return report
    slug = slug or _slugify(login, title)
    description = f"{line.strip()} {hashtags(login)}"
    drop(rep["out"], slug, title, description, moment.isoformat(), post, push, check_only,
         platforms=platforms, schedule=schedule,
         grade={"score": final["score"], "tier": final["tier"], "engine": eng["score"], "rubric": rubric,
                "parts": eng["parts"]})
    if not check_only:
        record_clipped({"slug": slug, "source": source, "login": login, "start_s": s, "end_s": e,
                        "moment_at": moment.isoformat(), "title": title, "grade": final["score"],
                        "engine": eng["score"], "rubric": rubric, "platforms": platforms,
                        "explore": [p for p, d in decision.items() if d.get("explore")],
                        "at": dt.datetime.now(dt.timezone.utc).isoformat()}, push)
    report.update({"status": ("CHECKED (not dropped)" if check_only else ("DROPPED + POSTED" if post else "DROPPED"))
                   + f" — banger {final['score']} ({final['tier']}): {GT.summary(decision)}",
                   "slug": slug, "description": description})
    print(json.dumps(report, indent=2))
    return report


def wm_ok(rep: dict) -> bool:
    return bool(rep.get("watermark", True))
