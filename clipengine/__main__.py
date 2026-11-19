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
    r.add_argument("--layout", choices=["single", "stack"], default="single",
                   help="stack: zoomed action on top, full frame + facecam below, title card on the seam")
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
    pb.add_argument("--slug", help="default: made from the creator + headline")
    pb.add_argument("--start", help="override window start (VOD time)")
    pb.add_argument("--end", help="override window end (VOD time)")
    pb.add_argument("--frames-checked", action="store_true",
                    help="you LOOKED at the frames + read the captions; without it the clip is rendered but not dropped")
    pb.add_argument("--post", action="store_true", help="run the poster on this machine right after the drop")
    pb.add_argument("--check", action="store_true", help="validate the drop without dropping")
    pb.add_argument("--no-push", action="store_true")
    for x in (pb,):
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
        x.add_argument("--slug")
        x.add_argument("--frames-checked", action="store_true")
        x.add_argument("--post", action="store_true")
        x.add_argument("--check", action="store_true")
        x.add_argument("--no-push", action="store_true")
        x.add_argument("--fix", action="append", default=[], metavar='"heard=>said"')
        x.add_argument("--bleep", action="append", default=[], metavar="WORD")
        x.add_argument("--rubric", metavar="CHECKS")
    sub.add_parser("creators", help="who is live right now, and whether their stream is saved as a VOD")
    sn = sub.add_parser("sentinel", help="watch one live streamer in real time: chat + audio + viewer clips -> drafts in the queue")
    sn.add_argument("--channel", required=True, help='Twitch login, or YouTube: "yt:@handle" or a live URL (audio-only)')
    sn.add_argument("--out", help="default runs/sentinel-<channel>")
    sn.add_argument("--buffer", type=float, default=1.0, help="hours of recording to keep")
    sn.add_argument("--no-draft", action="store_true", help="detect only; don't cut/transcribe drafts")
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
    dr = sub.add_parser("doctor", help="preflight: everything this machine needs, with the fix for each gap")
    dr.add_argument("--accounts", action="store_true", help="also check the three platform sign-ins (opens Chrome)")
    st = sub.add_parser("start", help="start a sentinel in the background (survives the shell)")
    st.add_argument("--channel", required=True)
    sp = sub.add_parser("stop", help="stop a sentinel")
    sp.add_argument("--channel", required=True)
    sub.add_parser("sentinels", help="one line per running sentinel (DOWN ones print the restart command)")
    qu = sub.add_parser("queue", help="sentinel drafts worth a look, best first (one line each)")
    qu.add_argument("--all", action="store_true", help="include skipped/unsafe/stale/low drafts")
    pq = sub.add_parser("publish-queued", help="publish a sentinel draft (same checks as publish-live)")
    pq.add_argument("--id", required=True)
    pq.add_argument("--title", required=True)
    pq.add_argument("--line", required=True)
    pq.add_argument("--quote")
    pq.add_argument("--slug")
    pq.add_argument("--start", type=float, help="trim: seconds to drop from the start")
    pq.add_argument("--end", type=float, help="trim: seconds to drop from the end")
    pq.add_argument("--frames-checked", action="store_true")
    pq.add_argument("--rubric", metavar="CHECKS")
    pq.add_argument("--post", action="store_true")
    pq.add_argument("--check", action="store_true")
    pq.add_argument("--no-push", action="store_true")
    pq.add_argument("--fix", action="append", default=[], metavar='"heard=>said"')
    pq.add_argument("--bleep", action="append", default=[], metavar="WORD")
    sk = sub.add_parser("skip", help="mark a sentinel draft as looked-at and not worth posting")
    sk.add_argument("--id", required=True)
    tn = sub.add_parser("train", help="train the sentinel on recent VODs, using viewer clips as labels")
    tn.add_argument("--channel", action="append", default=[], help="streamers to learn from (default: top live in the radar)")
    tn.add_argument("--vod", action="append", default=[])
    tn.add_argument("--hours", type=float, default=4.0)
    tn.add_argument("--dry", action="store_true")
    tn.add_argument("--out", help="write the model here instead of config/sentinel_model.json")
    ln = sub.add_parser("learn", help="turn view counts into streamer weights (data/weights.json) + a calibration report")
    ln.add_argument("--dry", action="store_true", help="report only, don't write data/weights.json")
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
    if getattr(a, "bleep", None):
        caption_kw["extra_censor"] = {w.lower() for w in a.bleep}
    if a.cmd == "publish":
        from . import publish as PB
        PB.publish_scan(a.scan, a.pick, a.title, a.line, a.quote, a.slug, a.frames_checked, a.post,
                        push=not a.no_push, check_only=a.check, start=T.parse_time(a.start), end=T.parse_time(a.end), **caption_kw)
        return 0
    if a.cmd == "publish-live":
        from . import publish as PB
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
        SN.run(a.channel, a.out or os.path.join("runs", f"sentinel-{L.source_name(a.channel)}"), a.buffer, not a.no_draft)
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
        else:
            print(O.stop(a.channel))
        return 0
    if a.cmd == "sentinels":
        from . import sentinel as SN
        if not SN.health():
            print("no sentinels running")
        return 0
    if a.cmd == "queue":
        from . import sentinel as SN
        if not SN.show_queue(all_=a.all):
            print("queue empty")
        return 0
    if a.cmd in ("publish-queued", "skip"):
        from . import sentinel as SN
        e = next((r for r in SN.load_queue() if r["id"] == a.id), None)
        if not e:
            ap.error(f"no queued draft {a.id}")
        if a.cmd == "skip":
            SN.set_status(e, "skipped")
            return 0
        from . import publish as PB
        rep = PB.publish_window(e["rec"], e["start"] + (a.start or 0), e["stop"] - (a.end or 0), a.title, a.line,
                                a.quote, a.slug, a.frames_checked, a.post, push=not a.no_push, check_only=a.check,
                                **caption_kw)
        if a.frames_checked and not a.check and str(rep.get("status", "")).startswith("DROPPED"):
            SN.set_status(e, "published", slug=rep.get("slug"))
        return 0
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
        L.record(a.channel, a.out, T.parse_time(a.buffer))
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
