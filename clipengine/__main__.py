"""clipengine — one command per step.

  python -m clipengine scan   --vod <vod id> --from 8h --to 9h --out runs/scan-8h
  python -m clipengine render --scan runs/scan-8h --pick 1 --type CHOICE
  python -m clipengine run    --vod <vod id> --from 8h --to 9h --out runs/scan-8h --render-top 3

`run` = scan, then render the top N clean candidates WITHOUT headers (a
header needs a person to pick its type). Everything it makes is a DRAFT.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import headers as H
from . import untrusted as UT
from . import pipeline as P
from . import twitch as T


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="clipengine", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def scan_args(p):
        p.add_argument("--vod", help="VOD id or URL")
        p.add_argument("--channel", help="instead of --vod: the channel's newest VOD (works while live)")
        p.add_argument("--last", help="with --channel: only the last N of it (e.g. 2h) — launch-day mode")
        p.add_argument("--from", dest="start", default=None, help="start (e.g. 2h, 1:30:00, 5400)")
        p.add_argument("--to", dest="end", default=None, help="end")
        p.add_argument("--out", required=True, help="output folder")
        p.add_argument("--top", type=int, default=12, help="candidates from chat (default 12)")
        p.add_argument("--probe", type=int, default=8, help="how many to audio-probe (default 8)")
        p.add_argument("--model", default="small", help="whisper model for the probe (small|tiny)")
        p.add_argument("--length", type=float, default=62.0, help="clip length (default 62)")
        p.add_argument("--chat-file", help="use a chat file you already have (TwitchDownloader JSON)")

    s = sub.add_parser("scan", help="rank moments and write review.html (no video download)")
    scan_args(s)

    r = sub.add_parser("render", help="render a picked candidate (single pass)")
    r.add_argument("--scan", required=True, help="the scan folder")
    r.add_argument("--pick", type=int, required=True, help="candidate rank from review.html")
    r.add_argument("--type", help="header type from the closed list: " + ", ".join(H.TYPES))
    r.add_argument("--streamer", help="name in the header (default: per-creator short name)")
    r.add_argument("--hook", help="optional hook text, first 2 s (<= ~18 chars)")
    r.add_argument("--layout", choices=["single", "stack", "facecam"], default="single",
                   help="stack: zoomed action on top, full frame + facecam below, title card on the seam; "
                        "facecam: the streamer's webcam on top, gameplay below (falls back to stack)")
    r.add_argument("--title", help="stack layout: the contextual headline, e.g. 'Streamer got CAUGHT by his in-game girlfriend 😭👀'")
    r.add_argument("--quote", help="stack layout: the streamer's own line, shown in the red tag")
    r.add_argument("--allow-flagged", action="store_true", help="render a moment flagged for sexual content (a person decided)")
    r.add_argument("--crop", choices=["centre", "blur", "zoom"], help="override the chapter-based crop")
    r.add_argument("--start", help="override window start (VOD time)")
    r.add_argument("--end", help="override window end (VOD time)")
    r.add_argument("--mute-audio", action="store_true", help="drop all audio (radio / music)")
    r.add_argument("--cut", choices=["tight", "full"], default="tight",
                   help="tight (default): setup -> moment -> reaction, 12-60 s, from the viewer-clip "
                        "window or the beat finder. full: the 62 s window")
    r.add_argument("--no-retime", action="store_true", help="skip the tiny-model timing pass")
    r.add_argument("--no-watermark", action="store_true")
    r.add_argument("--watermark", help="watermark png (default assets/watermark.png)")

    x = sub.add_parser("run", help="scan, then render the top N clean candidates (no header)")
    scan_args(x)
    x.add_argument("--render-top", type=int, default=3)

    w = sub.add_parser("watch", help="launch day: re-scan channels' newest VODs on a timer, one ranked queue")
    w.add_argument("--channel", action="append", required=True, help="repeat for several channels")
    w.add_argument("--out", required=True)
    w.add_argument("--every", default="20m", help="how often (default 20m)")
    w.add_argument("--last", default="30m", help="how much of the newest VOD each round (default 30m)")
    w.add_argument("--rounds", type=int, help="stop after N rounds (default: run until stopped)")
    w.add_argument("--probe", type=int, default=6)
    w.add_argument("--model", default="small")

    lr = sub.add_parser("record", help="record a live stream that is NOT archived (no public VOD) into a rolling buffer")
    lr.add_argument("--channel", required=True)
    lr.add_argument("--out", required=True)
    lr.add_argument("--buffer", default="2h")
    lr.add_argument("--supervised", action="store_true", help="keep recording through drops (what `start` runs)")
    lc = sub.add_parser("live-clips", help="cut the most-viewed viewer-clipped moments out of a recording")
    lc.add_argument("--rec", required=True, help="the --out folder of `record`")
    lc.add_argument("--top", type=int, default=3)
    lc.add_argument("--watermark")

    pb = sub.add_parser("publish", help="render a picked moment, run the checks, drop it (and --post it)")
    pb.add_argument("--scan", required=True)
    pb.add_argument("--pick", type=int, required=True)
    pb.add_argument("--title", required=True, help="the headline (rules/headlines.md)")
    pb.add_argument("--line", required=True, help="one true sentence for the caption; hashtags are added")
    pb.add_argument("--quote", help="the streamer's own words for the red tag")
    pb.add_argument("--question", help="optional second caption line: a question for viewers (rules/headlines.md)")
    pb.add_argument("--slug", help="default: made from the creator + headline")
    pb.add_argument("--start", help="override window start (VOD time)")
    pb.add_argument("--end", help="override window end (VOD time)")
    pb.add_argument("--frames-checked", action="store_true",
                    help="you LOOKED at the frames + read the captions; without it the clip is rendered but not dropped")
    pb.add_argument("--post", action="store_true", help="run the poster on this machine right after the drop")
    pb.add_argument("--check", action="store_true", help="validate the drop without dropping")
    pb.add_argument("--no-push", action="store_true")
    for x in (pb,):
        x.add_argument("--truth-override", metavar="REASON", help="confirm an unsupported headline claim seen in the cut")
        x.add_argument("--taste", metavar="yes|no", help="yes|no — would a fan of this streamer be glad to see this posted? (no = skip, reason taste)")
        x.add_argument("--rubric", metavar="CHECKS",
                       help="with --frames-checked: the checks that pass, from hook,context,payoff,share (or none)")
        x.add_argument("--fix", action="append", default=[], metavar='"heard=>said"',
                       help='correct a misheard caption, e.g. --fix "all equipped=>I\'ll equip" (repeatable)')
        x.add_argument("--bleep", action="append", default=[], metavar="WORD",
                       help="also censor this word (*** + muted), e.g. --bleep balls (repeatable)")
    pl = sub.add_parser("publish-live", help="same as publish, for a window of a live recording (no VOD)")
    pl.add_argument("--rec", required=True)
    pl.add_argument("--start", required=True, help="wall-clock start: unix seconds or HH:MM:SS UTC today")
    pl.add_argument("--end", required=True)
    for x in (pl,):
        x.add_argument("--title", required=True)
        x.add_argument("--line", required=True)
        x.add_argument("--quote")
        x.add_argument("--question", help="optional second caption line: a question for viewers")
        x.add_argument("--slug")
        x.add_argument("--frames-checked", action="store_true")
        x.add_argument("--post", action="store_true")
        x.add_argument("--check", action="store_true")
        x.add_argument("--no-push", action="store_true")
        x.add_argument("--fix", action="append", default=[], metavar='"heard=>said"')
        x.add_argument("--bleep", action="append", default=[], metavar="WORD")
        x.add_argument("--rubric", metavar="CHECKS")
        x.add_argument("--truth-override", metavar="REASON", help="confirm an unsupported headline claim seen in the cut")
        x.add_argument("--taste", metavar="yes|no", help="yes|no — would a fan of this streamer be glad to see this posted? (no = skip, reason taste)")
    sub.add_parser("creators", help="who is live right now, and whether their stream is saved as a VOD")
    sn = sub.add_parser("sentinel", help="watch one live streamer in real time: chat + audio + viewer clips -> drafts in the queue")
    sn.add_argument("--channel", required=True, help='Twitch login, or YouTube: "yt:@handle" or a live URL (audio-only)')
    sn.add_argument("--out", help="default runs/sentinel-<channel>")
    sn.add_argument("--buffer", type=float, default=1.0, help="hours of recording to keep")
    sn.add_argument("--no-draft", action="store_true", help="detect only; don't cut/transcribe drafts")
    sn.add_argument("--no-record", action="store_true", help="the recorder runs as its own process (what `start` does)")
    ap_ = sub.add_parser("autopilot", help="run `cycle` every few minutes in the background, no agent needed")
    ap_.add_argument("action", choices=["start", "stop", "status", "run"])
    ap_.add_argument("--every", type=float, help="minutes between cycles (default config/ops.json autopilot_every_min, 5)")
    ap_.add_argument("--sessions", action="store_true", help="only inside the roster's usual stream hours (clipengine schedule)")
    ap_.add_argument("--rounds", type=int, help=argparse.SUPPRESS)
    tr_ = sub.add_parser("triage", help="route every new draft (preview / review / repair / archive) — cycle runs this")
    tr_.add_argument("--keep", metavar="ID", help="keep an old draft as evergreen (needs --why)")
    tr_.add_argument("--why", help="the editorial reason it is still worth posting")
    sub.add_parser("jobs", help="heavy jobs (transcription, encodes): who runs, who waits and why, 24 h timings")
    sv_ = sub.add_parser("supervisor", help="the native service that keeps the autopilot alive (install/status/stop/resume/uninstall)")
    sv_.add_argument("action", choices=["install", "status", "stop", "resume", "uninstall", "boot", "keep"])
    sv_.add_argument("--every", type=float, help="minutes between cycles (default config/ops.json autopilot_every_min, 5)")
    sv_.add_argument("--sessions", action="store_true", help="only inside the roster's usual stream hours")
    at_ = sub.add_parser("attention", help="ONE line of what needs you now (nothing when nothing does)")
    at_.add_argument("--seen", action="store_true", help="mark what it shows as read")
    sc_ = sub.add_parser("schedule", help="each roster creator's usual stream hours and the next session (local time)")
    sc_.add_argument("--cached", action="store_true", help="don't re-read their broadcasts")
    cu_ = sub.add_parser("catchup", help="scan saved streams for a window nothing was running in; queue the best")
    cu_.add_argument("--since", help="unix seconds")
    cu_.add_argument("--spikes", help=argparse.SUPPRESS)          # tier-1 spikes to draft from saved streams
    cu_.add_argument("--sweep", action="store_true", help="cheap: chat + viewer clips only, queue the strong new moments")
    cu_.add_argument("--until", help="unix seconds (default now)")
    cu_.add_argument("--channel", action="append", help="only these roster creators")
    bk_ = sub.add_parser("backup", help="zip brand/, config/, data/, ledger.jsonl, local/patches")
    bk_.add_argument("--to", help="folder for the zip (default runs/backups)")
    rs_ = sub.add_parser("restore", help="put a backup zip back (backs up what's there first)")
    rs_.add_argument("file")
    it = sub.add_parser("init", help="set up this channel: brand pack, profile kit, roster (unique look by default)")
    it.add_argument("--name"); it.add_argument("--youtube", help="@handle"); it.add_argument("--instagram")
    it.add_argument("--tiktok"); it.add_argument("--category", help='game or category, e.g. "Grand Theft Auto VI"')
    it.add_argument("--streamers", help="comma-separated Twitch logins (default: suggested from what's live)")
    it.add_argument("--hashtags", help='comma-separated; "{creator}" = the streamer\'s tag')
    it.add_argument("--force", action="store_true", help="start over (keeps a .bak of the current pack)")
    it.add_argument("--accept-risks", action="store_true", help="the user heard the risks (printed without this) and said yes")
    pv = sub.add_parser("preview", help="render a sample frame + 5 s clip in this channel's look")
    pv.add_argument("--clip", help="use a real .ts/.mp4 instead of the neutral background")
    pv.add_argument("--title"); pv.add_argument("--quote")
    rp = sub.add_parser("report", help="what posted, views, best clip, what's improving (short)")
    rp.add_argument("--days", type=float, default=1.0)
    sub.add_parser("update", help="get the newest engine, keep this channel's fixes and settings")
    su = sub.add_parser("support", help="scrubbed log bundle + a filled-in issue, ready to send")
    su.add_argument("--problem", default="", help="one or two sentences: what went wrong")
    ins = sub.add_parser("insights", help="opt-in anonymous results that improve the shared detector")
    g = ins.add_mutually_exclusive_group(required=True)
    g.add_argument("--show", action="store_true"); g.add_argument("--enable", action="store_true")
    g.add_argument("--disable", action="store_true"); g.add_argument("--submit", action="store_true")
    ins.add_argument("--repo", help="with --enable: the submission repo URL the maintainers gave you")
    sub.add_parser("diagnose", help="match recent logs to known problems; prints only the matches and fixes")
    cy = sub.add_parser("cycle", help="the whole rhythm in one idempotent command — run every 5-10 min")
    cy.add_argument("--top", type=int, help="sentinels to keep running (default config/ops.json)")
    cy.add_argument("--no-radar", action="store_true", help="don't start/stop sentinels from the radar")
    fc_setup = sub.add_parser("facecam-setup", help="review a refined webcam box and approve it")
    fc_setup.add_argument("--channel", required=True)
    answer = fc_setup.add_mutually_exclusive_group()
    answer.add_argument("--yes", metavar="BOX", help="approve a reviewed x,y,width,height box")
    answer.add_argument("--no", action="store_true", help="confirm the creator has no webcam box")
    dr = sub.add_parser("doctor", help="preflight: everything this machine needs, with the fix for each gap")
    dr.add_argument("--accounts", action="store_true", help="check public handles in agent mode or platform sign-ins in Chrome")
    st = sub.add_parser("start", help="start a sentinel in the background (survives the shell)")
    st.add_argument("--channel", required=True)
    sp = sub.add_parser("stop", help="stop a sentinel (--all: every sentinel and the poster)")
    sp.add_argument("--channel")
    sp.add_argument("--all", action="store_true")
    t1_ = sub.add_parser("tier1", help="the chat-only watcher on every live roster creator (cycle runs it)")
    t1_.add_argument("--seconds", type=float, help=argparse.SUPPRESS)
    sub.add_parser("sentinels", help="one line per running sentinel (DOWN ones print the restart command)")
    qu = sub.add_parser("queue", help="sentinel drafts worth a look, best first (one line each)")
    qu.add_argument("--all", action="store_true", help="include skipped/unsafe/stale/low drafts")
    pq = sub.add_parser("publish-queued", help="publish a sentinel draft (same checks as publish-live)")
    pq.add_argument("--id", required=True)
    pq.add_argument("--title", required=True)
    pq.add_argument("--line", required=True)
    pq.add_argument("--quote")
    pq.add_argument("--question", help="optional second caption line: a question for viewers")
    pq.add_argument("--slug")
    pq.add_argument("--start", type=float, help="trim: seconds to drop from the start")
    pq.add_argument("--end", type=float, help="trim: seconds to drop from the end")
    pq.add_argument("--frames-checked", action="store_true")
    pq.add_argument("--rubric", metavar="CHECKS")
    pq.add_argument("--truth-override", metavar="REASON")
    pq.add_argument("--taste", metavar="yes|no", help="yes|no — would a fan of this streamer be glad to see this posted? (no = skip, reason taste)")
    pq.add_argument("--post", action="store_true")
    pq.add_argument("--check", action="store_true")
    pq.add_argument("--no-push", action="store_true")
    pq.add_argument("--fix", action="append", default=[], metavar='"heard=>said"')
    pq.add_argument("--bleep", action="append", default=[], metavar="WORD")
    pr_ = sub.add_parser("prerender", help="final-render queued drafts (all but the headline card) so publish-queued takes seconds")
    prg = pr_.add_mutually_exclusive_group(required=True)
    prg.add_argument("--pending", action="store_true", help="every eligible draft without one (what cycle runs)")
    prg.add_argument("--id", action="append", help="this draft (repeatable); re-renders it")
    sub.add_parser("to-post", help="(post_with: agent) clips due now: the file and the exact text per platform")
    pd = sub.add_parser("posted", help="(post_with: agent) record a clip you posted with your own browser")
    pd.add_argument("--slug", required=True); pd.add_argument("--platform", required=True); pd.add_argument("--url", required=True)
    pd.add_argument("--confirmed", action="store_true", help="you opened the post and saw it plays as our account (Instagram always; TikTok/YouTube when the platform won't confirm the author from this host)")
    pd.add_argument("--override", metavar="REASON", help="the owner had it posted outside the gate (no drop, or a "
                    "platform the drop didn't list): recorded with this reason, left out of learning")
    pd.add_argument("--login", help="with --override and no drop: the creator")
    pd.add_argument("--moment-at", help="with --override and no drop: the moment's UTC time (ISO), for the repeat guard")
    ip = sub.add_parser("import-posts", help="import live posts from another engine for the repeat guard")
    ip.add_argument("--from", dest="file", required=True)
    vw = sub.add_parser("views", help="(post_with: agent) record a view count you read off a post")
    vw.add_argument("--slug", required=True); vw.add_argument("--platform", required=True); vw.add_argument("--views", type=int, required=True)
    vw.add_argument("--watch-s", type=float, help="average watch time in seconds (from the post's analytics)")
    vw.add_argument("--skip", type=float, help="skip / swipe-away rate, %%")
    for k in ("shares", "saves", "follows", "likes", "comments"):
        vw.add_argument(f"--{k}", type=int)
    sub.add_parser("to-measure", help="posts due a 1 h / 24 h / 72 h analytics read, with links")
    fo = sub.add_parser("followers", help="record an account's follower count (report: payout thresholds)")
    fo.add_argument("--platform", required=True); fo.add_argument("--count", type=int, required=True)
    sub.add_parser("to-submit", help="posted clips that match a clipping pool (config/campaigns.json), with links")
    sb = sub.add_parser("submitted", help="record a clip submitted to a clipping pool")
    sb.add_argument("--slug", required=True); sb.add_argument("--pool", required=True); sb.add_argument("--url", required=True)
    sb.add_argument("--platform")
    po = sub.add_parser("payout", help="record money a clipping pool paid")
    po.add_argument("--pool", required=True); po.add_argument("--amount", type=float, required=True); po.add_argument("--slug")
    au = sub.add_parser("audit", help="humans check the engine: `audit speakers` (tags right/wrong)")
    au.add_argument("what", choices=["speakers", "words"])
    au.add_argument("--channel", help="creator vocab pack to audit")
    au.add_argument("--reference", help="JSON of manually verified draft transcript text, keyed by ID")
    au.add_argument("--last", type=int, default=20)
    au.add_argument("--mark", nargs=2, metavar=("ID", "right|wrong"))
    au.add_argument("--note")
    sk = sub.add_parser("skip", help="mark a sentinel draft as looked-at and not worth posting")
    sk.add_argument("--id", required=True)
    sk.add_argument("--reason", default="other", help="menu/map/loading/driving/dark/static/cutscene/sexual/dud/other, or your own reason")
    sub.add_parser("eval-judge", help="compare the engine calls to human decisions")
    ch = sub.add_parser("check-headline", help="check a title against a queued draft")
    ch.add_argument("--id", required=True)
    ch.add_argument("--title", required=True)
    ch.add_argument("--quote")
    rv = sub.add_parser("review", help="write a self-contained review page for a draft")
    rv.add_argument("--id", required=True)
    rv.add_argument("--title")
    rv.add_argument("--quote")
    tn = sub.add_parser("train", help="train the sentinel on recent VODs, using viewer clips as labels")
    tn.add_argument("--channel", action="append", default=[], help="streamers to learn from (default: top live in the radar)")
    tn.add_argument("--vod", action="append", default=[])
    tn.add_argument("--hours", type=float, default=4.0)
    tn.add_argument("--dry", action="store_true")
    tn.add_argument("--out", help="write the model here instead of config/sentinel_model.json")
    ln = sub.add_parser("learn", help="turn view counts into streamer weights (data/weights.json) + a calibration report")
    ln.add_argument("--dry", action="store_true", help="report only, don't write data/weights.json")
    fc = sub.add_parser("facecam", help="find a streamer's webcam box (for the facecam layout); saves a check image")
    fcg = fc.add_mutually_exclusive_group(required=True)
    fcg.add_argument("--rec", help="a `record` / sentinel folder")
    fcg.add_argument("--channel", help="Twitch login: looks at the last minutes of the newest VOD")
    fc.add_argument("--out", help="check image path")
    fc.add_argument("--refine", action="store_true",
                    help="snap the streamer's box (config or detected) to the webcam's real border: a clean cut")
    sc = sub.add_parser("scouts", help="were we first? every moment viewers clipped on the roster vs our drafts")
    sc.add_argument("--hours", type=float, default=6.0)
    sc.add_argument("--min-views", type=int, default=25)
    so = sub.add_parser("sources", help="the last 10 yt: sentinel sessions: when, how long, chat or audio-only, drafts")
    so.add_argument("-n", type=int, default=10)
    kc = sub.add_parser("kick-check", help="one line: does Kick work from this machine (site, GTA VI listing, recording, chat)")
    kc.add_argument("--channel", help="kick:<slug> to try (default: the top live Kick stream in the category)")
    kc.add_argument("--seconds", type=float, default=60.0, help="how long to read chat")
    rd = sub.add_parser("radar", help="score every live stream in the category and list the hottest moments")
    rd.add_argument("--hours", type=float, help="trending window in hours (default from config/radar.json)")
    rd.add_argument("--out", help="also save the full result as JSON")
    rd.add_argument("--show", type=int, default=15)

    a = ap.parse_args(argv)
    caption_kw = {}
    if getattr(a, "fix", None):
        bad = [f for f in a.fix if "=>" not in f]
        if bad:
            ap.error(f'--fix needs "heard=>said": {bad[0]}')
        caption_kw["fixes"] = dict(f.split("=>", 1) for f in a.fix)
    if getattr(a, "rubric", None) is not None:
        from . import grade as G
        try:
            checks = G.parse_rubric(a.rubric)
            G.rubric_score(checks)                      # validates the names
            caption_kw["rubric"] = checks
        except ValueError as exc:
            ap.error(str(exc))
    if getattr(a, "taste", None) is not None:
        from . import publish as PB
        try:
            caption_kw["taste"] = PB.parse_taste(a.taste)
        except ValueError as exc:
            ap.error(str(exc))
    if getattr(a, "question", None):
        caption_kw["question"] = a.question
    if getattr(a, "truth_override", None) is not None:
        caption_kw["truth_override"] = a.truth_override.strip()
    if getattr(a, "bleep", None):
        caption_kw["extra_censor"] = {w.lower() for w in a.bleep}
    if a.cmd == "triage":
        from . import sentinel as SN, triage as TR
        if a.keep:
            if not (a.why or "").strip():
                ap.error("triage --keep needs --why \"the editorial reason\"")
            hit = [r for r in SN.load_queue() if r["id"] == a.keep]
            if not hit:
                ap.error(f"no draft {a.keep}")
            TR.keep(hit[0], a.why)
            print(f"{a.keep}: kept as evergreen — {a.why.strip()}")
        else:
            print(TR.apply() or "triage: nothing new")
        return 0
    if a.cmd == "jobs":
        from . import admission as ADM
        print("\n".join(ADM.show()))
        return 0
    if a.cmd == "supervisor":
        from . import autopilot as AP, ops as O, supervise as SV
        if a.action == "install":
            print(SV.install(a.every or O.config().get("autopilot_every_min", 5), a.sessions))
        elif a.action == "uninstall":
            print(SV.uninstall())
        elif a.action == "stop":
            print(AP.autopilot_stop())
        elif a.action == "resume":
            print(AP.autopilot_start(a.every, a.sessions))
        elif a.action == "boot":
            print(SV.boot(AP._root()))
        elif a.action == "keep":
            print(SV.keep(a.every or O.config().get("autopilot_every_min", 5), a.sessions, AP._root()), flush=True)
        else:
            msg = AP.revive_supervised("supervisor status")
            if msg:
                print(msg)
            if AP.autopilot_died():
                SV.reconcile(None, root=AP._root())
            print("\n".join(SV.status_lines(AP._root())))
        return 0
    if a.cmd in ("autopilot", "attention", "schedule", "catchup", "backup", "restore"):
        from . import autopilot as AP
        if a.cmd == "autopilot":
            if a.action == "start":
                print(AP.autopilot_start(a.every, a.sessions))
            elif a.action == "stop":
                print(AP.autopilot_stop())
            elif a.action == "status":
                print(AP.autopilot_status_line())
            else:
                from . import ops as O
                AP.autopilot_run(a.every or O.config().get("autopilot_every_min", 5), a.sessions, a.rounds)
        elif a.cmd == "attention":
            from . import attention as AT
            items = AT.pending()
            AP.revive_supervised("attention")
            if AP.autopilot_died():
                from . import supervise as SV
                SV.reconcile(None, root=AP._root())         # records the outage once (and alerts once)
                items = [{"kind": "failure", "text": "autopilot stopped unexpectedly → clipengine autopilot start"}] + AT.pending()
            text = AT.line(items)
            if text:
                print(text)
            if a.seen:
                print(f"marked {AT.mark_seen()} item(s) read")
        elif a.cmd == "schedule":
            AP.show_schedule(refresh=not a.cached)
        elif a.cmd == "catchup":
            import time as _t
            if a.spikes:
                AP.catchup_spikes(json.load(open(a.spikes))["spikes"])
            else:
                until = float(a.until) if a.until else _t.time()
                try:
                    since, how = AP.catchup_window(a.since, until, sweep=a.sweep)
                except ValueError as exc:
                    ap.error(str(exc))
                print(f"catch-up window: {_t.strftime('%Y-%m-%d %H:%M', _t.localtime(since))} → "
                      f"{_t.strftime('%H:%M', _t.localtime(until))} ({(until - since) / 60:.0f} min, {how})", flush=True)
                if a.sweep:
                    AP.sweep(since, until, a.channel)
                else:
                    AP.catchup(since, until, a.channel)
                if not a.channel:
                    AP.mark_covered(until, sweep=a.sweep)          # only completed coverage moves the watermark
        elif a.cmd == "backup":
            AP.backup(a.to)
        else:
            AP.restore(a.file)
        return 0
    if a.cmd == "publish":
        from . import publish as PB
        from . import autopilot as AP
        AP.refuse_never(json.load(open(os.path.join(a.scan, "scan.json")))["vod"].get("login", ""))
        PB.publish_scan(a.scan, a.pick, a.title, a.line, a.quote, a.slug, a.frames_checked, a.post,
                        push=not a.no_push, check_only=a.check, start=T.parse_time(a.start), end=T.parse_time(a.end), **caption_kw)
        return 0
    if a.cmd == "publish-live":
        from . import publish as PB
        from . import autopilot as AP
        AP.refuse_never(json.load(open(os.path.join(a.rec, "recording.json"))).get("login", ""))
        def wall(v):
            if ":" in v:
                import datetime as dt
                h, m, sec = v.split(":")
                now = dt.datetime.now(dt.timezone.utc)
                return now.replace(hour=int(h), minute=int(m), second=0, microsecond=0).timestamp() + float(sec)
            return float(v)
        PB.publish_window(a.rec, wall(a.start), wall(a.end), a.title, a.line, a.quote, a.slug,
                          a.frames_checked, a.post, push=not a.no_push, check_only=a.check, **caption_kw)
        return 0
    if a.cmd == "sentinel":
        from . import sentinel as SN
        from . import live as L
        SN.run(a.channel, a.out or os.path.join("runs", f"sentinel-{L.source_name(a.channel)}"), a.buffer, not a.no_draft,
               record=not a.no_record)
        return 0
    if a.cmd == "scouts":
        from . import scouts as SC
        SC.show(a.hours, a.min_views)
        return 0
    if a.cmd == "import-posts":
        from . import moments
        try:
            count = moments.import_posts(a.file)
        except (OSError, ValueError) as exc:
            ap.error(f"import-posts: {exc}")
        print(f"imported {count} live post record(s)")
        return 0
    if a.cmd == "facecam-setup":
        from . import facecam
        facecam.setup_channel(a.channel, a.yes, a.no)
        return 0
    if a.cmd == "facecam":
        from . import facecam as FC
        FC.cli(rec=a.rec, channel=a.channel, out=a.out, refine_it=a.refine)
        return 0
    if a.cmd == "init":
        from . import onboard as OB
        OB.init(a.name, a.youtube, a.instagram, a.tiktok, a.category,
                a.streamers.split(",") if a.streamers else None,
                [h.strip() for h in a.hashtags.split(",")] if a.hashtags else None, a.force,
                accept_risks=a.accept_risks)
        return 0
    if a.cmd == "preview":
        from . import onboard as OB
        kw = {k: v for k, v in (("title", a.title), ("quote", a.quote)) if v}
        OB.preview(a.clip, **kw)
        return 0
    if a.cmd in ("report", "update", "support"):
        from . import support as SP
        if a.cmd == "report":
            SP.report(a.days)
        elif a.cmd == "update":
            SP.update()
        else:
            SP.support(a.problem)
        return 0
    if a.cmd == "insights":
        from . import insights as IN
        if a.show:
            IN.show()
        elif a.enable:
            IN.enable(a.repo)
        elif a.disable:
            IN.disable()
        else:
            print(IN.submit())
        return 0
    if a.cmd == "diagnose":
        from .diagnose import diagnose
        return 1 if diagnose() else 0
    if a.cmd in ("cycle", "doctor", "start", "stop"):
        from . import ops as O
        if a.cmd == "cycle":
            O.cycle(a.top, radar=not a.no_radar)
        elif a.cmd == "doctor":
            return 0 if O.doctor(a.accounts) else 1
        elif a.cmd == "start":
            print(O.start(a.channel))
        elif a.all:
            print("\n".join(O.stop_all()))
        elif a.channel:
            print(O.stop(a.channel))
        else:
            print("stop --channel LOGIN, or stop --all")
            return 2
        return 0
    if a.cmd in ("to-post", "posted", "views", "to-measure", "followers"):
        from . import handpost as HP
        if a.cmd == "to-post":
            HP.show()
        elif a.cmd == "posted":
            print(HP.mark(a.slug, a.platform, a.url, a.confirmed, a.override, a.login, a.moment_at))
        elif a.cmd == "to-measure":
            HP.show_measure()
        elif a.cmd == "followers":
            print(HP.followers(a.platform, a.count))
        else:
            print(HP.views(a.slug, a.platform, a.views, watch_s=a.watch_s, skip=a.skip, shares=a.shares,
                           saves=a.saves, follows=a.follows, likes=a.likes, comments=a.comments))
        return 0
    if a.cmd in ("to-submit", "submitted", "payout"):
        from . import campaigns as CP
        if a.cmd == "to-submit":
            CP.show()
        elif a.cmd == "submitted":
            print(CP.submitted(a.slug, a.pool, a.url, a.platform))
        else:
            print(CP.payout(a.pool, a.amount, a.slug))
        return 0
    if a.cmd == "tier1":
        from . import tier1 as TR
        TR.run(a.seconds)
        return 0
    if a.cmd == "sentinels":
        from . import autopilot as AP
        from . import tier1 as TR
        TR.show()
        if not AP.show_sentinels():
            print("tier 2  no sentinels running")
        return 0
    if a.cmd == "audit":
        from . import judge as JG
        if a.what == "speakers":
            if a.mark:
                JG.mark_speakers(a.mark[0], a.mark[1], a.note)
            else:
                JG.audit_speakers(a.last)
        else:
            JG.audit_words(a.last, login=a.channel, reference=a.reference)
        return 0
    if a.cmd == "eval-judge":
        from . import judge as JG
        JG.eval_judge()
        return 0
    if a.cmd == "queue":
        from . import sentinel as SN
        if not SN.show_queue(all_=a.all):
            print("queue empty")
        return 0
    if a.cmd in ("publish-queued", "skip", "check-headline", "review"):
        from . import sentinel as SN
        e = next((r for r in SN.load_queue() if r["id"] == a.id), None)
        if not e:
            ap.error(f"no queued draft {a.id}")
        from . import judge as JG
        if a.cmd == "review":
            from . import review
            print(review.draft_page(e, a.title, a.quote))
            return 0
        if a.cmd == "check-headline":
            preview = JG.check_headline(e, a.title, a.quote)
            print(json.dumps(preview, indent=2))
            return 1 if preview["problems"] or preview["unsupported"] else 0
        if a.cmd == "skip":
            SN.mark_seen(e)
            reason = " ".join(a.reason.split()) or "other"
            JG.record(e, "drop", reason)
            SN.set_status(e, "skipped", reason=reason)
            print(f"skipped {e['id']}")
            return 0
        from . import publish as PB
        from . import autopilot as AP
        AP.refuse_never(e.get("login", ""))
        if a.frames_checked or caption_kw.get("taste") is False:
            SN.mark_seen(e)
        if caption_kw.get("taste") is False:      # a fan wouldn't want it: skip the draft, learn from it
            SN.set_status(e, "skipped", reason="taste")
            PB.taste_no(e.get("login", ""), e["id"], push=not a.no_push, title=a.title, draft=e)
        if e.get("scan"):                       # a catch-up draft from a saved stream: the normal VOD path
            kw = AP.publish_window_args(e, a.start, a.end)
            rep = PB.publish_scan(kw.pop("scan_dir"), kw.pop("pick"), a.title, a.line, a.quote, a.slug, a.frames_checked,
                                  a.post, push=not a.no_push, check_only=a.check, draft=e, **kw, **caption_kw)
            if a.frames_checked and not a.check and str(rep.get("status", "")).startswith("DROPPED"):
                SN.set_status(e, "published", slug=rep.get("slug"))
            return 0
        s_, e_ = e["start"] + (a.start or 0), e["stop"] - (a.end or 0)
        cut = SN.check_trim(e, s_, e_)           # a trim must not cut the payoff
        if cut:
            raise SystemExit("PUBLISH REFUSED: " + cut)
        words = SN.draft_words(e, s_, e_)
        from . import prerender as PR
        pre = PR.usable(e, s_, e_, caption_kw.get("fixes"), caption_kw.get("extra_censor"))
        rep = PB.publish_window(e["rec"], s_, e_, a.title, a.line,
                                a.quote, a.slug, a.frames_checked, a.post, push=not a.no_push, check_only=a.check,
                                chat_score=SN.chat_score_from_p(e["p"]) if e.get("p") is not None else None,
                                words=words, drop_captions=SN.game_spans(words) or None,   # captions: people, not the game
                                pre=pre, payoff_ts=e.get("payoff"), draft=e, **caption_kw)
        if a.frames_checked and not a.check and str(rep.get("status", "")).startswith("DROPPED"):
            SN.set_status(e, "published", slug=rep.get("slug"))
        return 0
    if a.cmd == "prerender":
        from . import prerender as PR
        out = PR.run_pending(ids=a.id) if a.id else PR.run_pending()
        if not out:
            print("prerender: nothing pending")
        return 0
    if a.cmd == "sources":
        from . import youtube as YT
        YT.show_sources(a.n)
        return 0
    if a.cmd == "kick-check":
        from . import kick as K
        from . import radar as R
        from . import ops as O
        cfg = R.config()
        res = K.check(cfg["game"], cfg["languages"], cfg.get("kick_category"), a.channel, a.seconds,
                      creds=K.credentials(O.config()))
        print(K.check_line(res))
        return 0 if res["ok"] else 1
    if a.cmd == "train":
        from . import train as TR
        TR.run(a.channel or None, a.vod or None, a.hours, write=not a.dry, out=a.out)
        return 0
    if a.cmd == "learn":
        from . import learn as LN
        LN.run(write=not a.dry)
        return 0
    if a.cmd == "radar":
        from . import radar as R
        R.run(a.out, a.hours, a.show)
        return 0
    if a.cmd == "creators":
        from . import publish as PB
        for login, c in sorted(PB.creators().items(), key=lambda kv: kv[1].get("priority", 9)):
            try:
                u = T.gql('query($l:String!){user(login:$l){stream{game{displayName}} videos(first:1,type:ARCHIVE,sort:TIME)'
                          '{edges{node{id createdAt status}}}}}', {"l": login}).get("user") or {}
                live = u.get("stream")
                v = ((u.get("videos") or {}).get("edges") or [{}])[0].get("node") or {}
                vod_now = live and v.get("status") == "RECORDING"
                how = ("scan --channel" if vod_now else "record (live, no VOD)") if live else "-"
                print(f"{login:15} {'LIVE ' + ((live.get('game') or {}).get('displayName') or '') if live else 'offline':32} "
                      f"newest VOD {v.get('createdAt', '-')[:16]}  -> {how}")
            except Exception as exc:
                print(f"{login:15} error: {exc}")
        return 0
    if a.cmd == "record":
        from . import live as L
        (L.record_supervised if a.supervised else L.record)(a.channel, a.out, T.parse_time(a.buffer))
        return 0
    if a.cmd == "live-clips":
        from . import live as L
        L.render_live(a.rec, a.top, watermark=a.watermark)
        return 0
    if a.cmd == "watch":
        P.watch(a.channel, a.out, T.parse_time(a.every), T.parse_time(a.last), a.rounds,
                probe=a.probe, model=a.model)
        return 0
    if a.cmd in ("scan", "run"):
        if not a.vod and not a.channel:
            ap.error("give --vod or --channel")
        if a.channel:
            v = T.latest_vod(a.channel)
            a.vod = v["id"]
            print(f"{a.channel}: newest VOD {v['id']} {UT.quote(v['title'], 60)} "
                  f"{T.fmt(v['length_s'])}{' (LIVE — still recording)' if v['live'] else ''}")
            if a.last:
                a.start = str(max(0, v["length_s"] - T.parse_time(a.last)))
                a.end = str(v["length_s"])
        res = P.scan(a.vod, a.out, T.parse_time(a.start), T.parse_time(a.end), top=a.top,
                     probe=a.probe, model=a.model, length=a.length, chat_file=a.chat_file)
        if a.cmd == "run":
            clean = [c for c in res["candidates"] if c.get("probed") and not c.get("hard_fail")]
            for c in clean[: a.render_top]:
                P.render_pick(a.out, c["rank"])
        return 0
    if a.cmd == "render":
        res = json.load(open(os.path.join(a.scan, "scan.json")))
        header = None
        if a.type:
            name = a.streamer or H.short_name(res["vod"]["login"], res["vod"]["display_name"])
            header = H.build(name, a.type)
            print(f"header (DRAFT until a person approves it): {header}")
        wm = None if a.no_watermark else (a.watermark or P.V.WATERMARK)
        from . import facecam as FC
        with FC.creator(res["vod"].get("login")):
            rep = P.render_pick(a.scan, a.pick, header=header, hook=a.hook, crop=a.crop,
                              start=T.parse_time(a.start), end=T.parse_time(a.end),
                              retime=not a.no_retime, watermark=wm, mute_audio=a.mute_audio, cut=a.cut,
                              layout=a.layout, title=a.title, quote=a.quote, allow_flagged=a.allow_flagged)
        print(json.dumps({k: rep[k] for k in ("out", "cards", "censored", "bleeps_verified_silent",
                                               "timing_matched", "timing_s", "status")}, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
