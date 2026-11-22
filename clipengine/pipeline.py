"""The whole loop, VOD -> ranked, probed candidates -> rendered drafts.

scan:   chat (parallel, cached) -> chat ranking -> audio probe per candidate
        (mute map, one transcript, music flag, best window) -> combined
        score -> scan.json + review.html.  No video is downloaded.
render: for the candidates a person picks: parallel video segment fetch
        for the chosen window -> timing re-check (tiny) -> single-pass
        render -> frame strip -> checks (ffprobe, censor, bleep silence).

Nothing posts, submits or signs in. A rendered clip is a DRAFT until a
person approves its header and watches it (v1's human gates stand).
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import time

from . import audio as A
from . import rank as R
from . import render as V
from . import twitch as T


def log(msg: str) -> None:
    print(f"[engine] {msg}", flush=True)


def _plain(o):
    """numpy scalars (np.bool_, np.float32 …) in a scan result: write them as plain JSON values."""
    if hasattr(o, "item"):
        return o.item()
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


def scan(vod_id: str, out_dir: str, start_s: float | None = None, end_s: float | None = None,
         top: int = 12, probe: int = 8, model: str = "small", length: float = 62.0,
         chat_file: str | None = None) -> dict:
    t_all = time.time()
    os.makedirs(out_dir, exist_ok=True)
    vod = T.Vod.fetch(vod_id)
    a = start_s if start_s is not None else 0.0
    b = end_s if end_s is not None else float(vod.length_s)
    log(f"{vod.display_name} — {vod.title[:70]} — {T.fmt(vod.length_s)} — "
        f"chapters: {', '.join(f'{T.fmt(c.start_s)} {c.game}' for c in vod.chapters) or vod.game}")
    t0 = time.time()
    if chat_file:
        msgs = [m for m in T.load_chat_file(chat_file) if a <= m["t"] < b]
    else:
        msgs = T.download_chat(vod.id, a, b, cache=os.path.join(out_dir, f"chat-{vod.id}-{int(a)}-{int(b)}.json"))
    t_chat = time.time() - t0
    if not msgs:
        raise RuntimeError("no chat in that range — chat disabled, or the range is empty")
    cands, rows = R.find_candidates(msgs, top=top, length=length)
    log(f"chat: {len(msgs):,} messages, {len(cands)} candidates by chat score")
    # ---- the crowd: where viewers already clipped this VOD -----------------
    crowd = []
    try:
        crowd = T.crowd_moments(vod, a, b)
    except Exception as exc:
        log(f"viewer clips unavailable: {exc}")
    n_new = 0
    for m in crowd:
        hit = next((c for c in cands if m["start"] - 20 <= c["peak_s"] <= m["end"] + 30), None)
        if hit:
            hit.setdefault("crowd", m)
            continue
        # a moment viewers clipped that chat did not flag: add it. Viewers
        # click right after the moment, so the clip END sits near the beat.
        peak = m["end"] - 3
        row = min(rows, key=lambda r: abs(r["start_s"] - peak)) if rows else {}
        cands.append({
            "peak_s": peak, "peak": T.fmt(peak), "clip_start_s": max(0.0, peak - 30),
            "clip_start": T.fmt(max(0.0, peak - 30)), "clip_end_s": peak + 32, "clip_end": T.fmt(peak + 32),
            "chat_score": round(row.get("score", 0.0), 2), "lift": row.get("lift", 0), "react": row.get("react", 0),
            "junk": row.get("junk", 0), "msgs_10s": row.get("msgs", 0), "users_10s": row.get("users", 0),
            "baseline_users": row.get("baseline_users", 0), "ratio": None, "reaction": "crowd-clipped",
            "chat_quotes": [], "top_text": row.get("top_text", ""), "crowd": m})
        n_new += 1
    if crowd:
        log(f"crowd: {len(crowd)} moments viewers clipped ({sum(m['views'] for m in crowd):,} views); "
            f"{n_new} that chat alone missed")
    # probe crowd-clipped moments first (most-viewed first), then chat's best
    cands.sort(key=lambda c: (-(c.get("crowd") or {}).get("views", -1), -c["chat_score"]))

    # ---- audio-first probe ------------------------------------------------
    t0 = time.time()
    pls = T.playlists(vod.id)
    audio_pl = pls["audio"] or pls["video"]
    tmp_dir = os.path.join(out_dir, "probe")
    os.makedirs(tmp_dir, exist_ok=True)

    def key(c):                                  # the probe cache: this VOD's window, judged before
        return f"vod:{vod.id}:{int(c['peak_s'] - 45)}:{int(c['peak_s'] + 40)}"

    def fetch_audio(c):
        ws, we = c["peak_s"] - 45, c["peak_s"] + 40
        path = os.path.join(tmp_dir, f"audio_{int(c['peak_s'])}.ts")
        if A.cache_get(key(c)):                  # already known unhearable: no download
            return c, path, ws, ws, we
        first = T.download_window(audio_pl, ws, we, path, pad_s=1)
        return c, path, first, ws, we

    to_probe = cands[:probe]
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        fetched = list(ex.map(fetch_audio, to_probe))
    probes = [(c, A.probe_audio(path, first, ws, we, c["peak_s"], model, login=vod.login, key=key(c)), path, first, ws)
              for c, path, first, ws, we in fetched]   # ASR is CPU-bound: one at a time, all cores
    n_cached = sum(1 for _, p, *_ in probes if p.cached)
    if n_cached:
        log(f"probe cache: {n_cached} window(s) already known unhearable — not downloaded or transcribed")
    prof = None
    try:                                     # who is talking: the session's voices across every candidate
        prof, tagged = tag_speakers(probes)
        log(f"speakers: {tagged}")
    except Exception as exc:
        log(f"speaker tags skipped: {exc}")
    from . import speaker as SPK
    from . import sentinel as SN
    for c, p, path, first, ws in probes:
        all_words = list(p.words)
        p.words = SPK.streamer_words(p.words)     # hook, speech density and "said": the streamer only
        win = A.choose_window(p, c["peak_s"], length=length) if p.words else {
            "start_s": c["clip_start_s"], "end_s": c["clip_end_s"], "words": 0,
            "speech_s": 0.0, "first_word_at": length, "longest_gap": length}
        verdict = A.assess(p, win, c["peak_s"])
        game = vod.game_at(win["start_s"])
        in_win = [w for w in all_words if win["start_s"] <= w["start"] < win["end_s"]]
        spk = SPK.summary(in_win, prof)
        c["speech"] = {"streamer_s": win["speech_s"], "dur": round(win["end_s"] - win["start_s"], 1),
                       "game_share": spk["game_share"], "game_dialogue": spk["game_dialogue"]}
        if spk["game_dialogue"]:
            verdict["flags"].append(f"game dialogue ({int(spk['game_share'] * 100)}% of speech)")
            verdict["audio_factor"] = round(verdict["audio_factor"] * 0.5, 3)
        bad = SN.wrong_game(game)
        if bad:                                 # fake "GTA 6" from GTA V footage: never ours
            c["wrong_game"] = bad
            verdict["flags"].append(f"recording from {bad}, not {SN.target_game()}")
            verdict["hard_fail"] = verdict["hard_fail"] or f"wrong game: {bad}"
        misheard = A.misheard_names(in_win)
        if misheard:
            verdict["flags"].append("names? " + ", ".join(f"{h}→{r}" for h, r in misheard))
        c.update({
            "probed": True, "game": game, "crop": V.crop_for_game(game),
            "window": win, "mean_db": p.mean_db, "muted_frac": p.muted_frac,
            "music_suspect": p.music_suspect, "excitement_db": p.excitement_db,
            "audio_factor": verdict["audio_factor"], "hard_fail": verdict["hard_fail"],
            "flags": verdict["flags"],
            "score": round(c["chat_score"] * verdict["audio_factor"]
                           + crowd_bonus(c.get("crowd")) * (1.0 if not verdict["hard_fail"] else 0.0), 2),
            "chat_context": R.chat_near(msgs, c["peak_s"], 10, 30, 10),
            "transcript_text": " ".join(w["w"].strip() for w in in_win if w.get("spk", "streamer") != "game"),
        })
        from . import safety as SF
        sf = SF.screen(c["transcript_text"], (c.get("crowd") or {}).get("titles"),
                       [t for t, n in c["chat_context"]])
        c["safety"] = sf
        dh = SF.death_hint(c["transcript_text"], (c.get("crowd") or {}).get("titles"), [t for t, n in c["chat_context"]])
        if dh:
            c["flags"] = c["flags"] + [f"death screen? ({dh})"]
        if sf["flagged"]:
            c["flags"] = ["SEXUAL CONTENT — " + sf["why"]] + c["flags"]
            c["score"] = round(c["score"] * 0.15, 2)
        json.dump(p.transcript, open(os.path.join(tmp_dir, f"words_{int(c['peak_s'])}.json"), "w"))
        log(f"  {c['peak']} chat {c['chat_score']:.1f} x audio {verdict['audio_factor']:.2f} "
            f"-> {c['score']:.1f}  {verdict['hard_fail'] or ', '.join(verdict['flags']) or 'clean'}")
    # thumbnails from the lowest-quality stream: start, peak, middle, end of
    # the chosen window — the pause menus / settings screens / loading
    # screens a transcript can't see (e.g. an on-screen mode change)
    thumb_pl = None
    try:
        thumb_pl = T.load_playlist(pls["thumb_url"])

        def thumbs(c):
            if not c.get("probed"):
                return
            w = c["window"]
            pts = [w["start_s"] + 1, c["peak_s"], (w["start_s"] + w["end_s"]) / 2, w["end_s"] - 1]
            c["thumbs"] = grab_thumbs(thumb_pl, pts, os.path.join(tmp_dir, f"thumb_{int(c['peak_s'])}"))
        with cf.ThreadPoolExecutor(max_workers=6) as ex:
            list(ex.map(thumbs, to_probe))
    except Exception as exc:  # thumbnails are a convenience, never a blocker
        log(f"thumbnails skipped: {exc}")
        thumb_pl = None
    # the picture: menus, maps, static or dark screens and how much moves (liveliness), from the lowest
    # quality stream of each probed window — the same frame checks a sentinel draft gets
    if thumb_pl is not None:
        from . import grade as G
        from . import vision as VI

        def look(c):
            if not c.get("probed"):
                return
            w = c["window"]
            path = os.path.join(tmp_dir, f"pic_{int(c['peak_s'])}.ts")
            try:
                first = T.download_window(thumb_pl, w["start_s"], w["end_s"], path, pad_s=1)
                vis = VI.check(path, max(0.0, w["start_s"] - first), w["end_s"] - w["start_s"])
            except Exception as exc:
                log(f"  {c['peak']}: frame check skipped ({str(exc)[:60]})")
                return
            finally:
                if os.path.exists(path):
                    os.remove(path)
            if vis["frames"]:
                c["liveliness"] = vis["liveliness"]
                c["flags"] = G.merge_vision_flags(c.get("flags") or [], vis["flags"])
        with cf.ThreadPoolExecutor(max_workers=6) as ex:
            list(ex.map(look, to_probe))
    for c in cands[probe:]:
        c.update({"probed": False, "score": 0.0, "game": vod.game_at(c["peak_s"])})
    # never re-clip a moment that is already published (data/clipped.jsonl)
    try:
        from .publish import already_clipped
        for c in cands:
            w = c.get("window") or {"start_s": c["clip_start_s"], "end_s": c["clip_end_s"]}
            hit = already_clipped(vod.id, w["start_s"], w["end_s"])
            if hit:
                c["hard_fail"] = f"already published as '{hit['slug']}'"
                c["score"] = 0.0
    except Exception as exc:
        log(f"clipped-check skipped: {exc}")
    t_probe = time.time() - t0
    ranked = sorted(cands, key=lambda c: (c.get("hard_fail") is None and c["probed"], c["score"]),
                    reverse=True)
    for i, c in enumerate(ranked, 1):
        c["rank"] = i
    result = {"vod": vod.to_dict(), "range": [a, b], "messages": len(msgs),
              "timing_s": {"chat": round(t_chat, 1), "probe": round(t_probe, 1),
                           "total": round(time.time() - t_all, 1)},
              "candidates": ranked,
              "timeline": [{"t": r["start_s"], "s": r["score"], "u": r["users"]} for r in rows]}
    json.dump(result, open(os.path.join(out_dir, "scan.json"), "w"), indent=2, default=_plain)
    from .review import write_review
    write_review(result, os.path.join(out_dir, "review.html"))
    log(f"scan done in {result['timing_s']['total']}s -> {out_dir}/review.html")
    return result


def tight_cut(scan_dir: str, c: dict) -> dict | None:
    """Setup -> moment -> reaction, no dead weight.

    Crowd-clipped moment: viewers already chose the window — use it, snapped
    back to the start of the sentence that sets it up (<= 6 s earlier) and
    forward to the end of the last sentence (<= 4 s later).
    Otherwise: the beat finder (beat.py) on the probe's audio + words.
    """
    from . import beat as BT
    wpath = os.path.join(scan_dir, "probe", f"words_{int(c['peak_s'])}.json")
    apath = os.path.join(scan_dir, "probe", f"audio_{int(c['peak_s'])}.ts")
    if not os.path.isfile(wpath):
        return None
    tr = json.load(open(wpath))
    words = A.real_words(tr) if tr.get("segments") else []
    segs = tr.get("segments", [])
    m = c.get("crowd")
    have_audio = bool(words) and os.path.isfile(apath)
    if m:
        best = m.get("best") or {"start": m["start"], "end": m["end"], "views": m["views"]}
        s0, e0 = best["start"], best["end"]
        # Trust the viewers' window. Re-picking a "beat" inside it chased the
        # loudest audio — on GTA VI launch day that was the game's cutscene,
        # not the streamer (seen in evaluation). Only trim dead air at the ends.
        if have_audio:
            inside = [w for w in words if s0 <= w["start"] <= e0]
            if inside:
                s0 = max(s0, inside[0]["start"] - 1.0)
                e0 = min(e0, inside[-1]["end"] + 1.5)
        seg_starts = [sg["start"] for sg in segs if s0 - 6 <= sg["start"] <= s0 + 1]
        s1 = (min(seg_starts) - 0.2) if seg_starts else s0
        seg_ends = [sg["end"] for sg in segs if e0 - 1 <= sg["end"] <= e0 + 4]
        e1 = (max(seg_ends) + 0.6) if seg_ends else e0
        e1 = max(e1, s1 + 12)
        if e1 - s1 > 60:
            s1 = e1 - 60
        return {"start_s": round(s1, 2), "end_s": round(e1, 2), "how": f"viewer clip window ({best['views']:,} views)"}
    if not have_audio:
        return None
    ws = c["peak_s"] - 45
    x = A.decode_pcm(apath, 0, 86)   # probe file starts ~1 s before ws (pad); close enough for the envelope
    bt = BT.find_beat(BT.envelope(x), ws - 1, words, c["peak_s"])
    tw = BT.tight_window(words, segs, bt["beat_s"])
    return {"start_s": tw["start_s"], "end_s": tw["end_s"], "how": f"beat at +{tw['beat_at']}s ({bt['how']})"}


def tag_speakers(probes: list) -> tuple[dict | None, str]:
    """Speaker tags for a VOD scan: speech chunks from every probed candidate (spread across the scanned
    range — the session) give the streamer's voice; each candidate's words are tagged in place."""
    from . import speaker as SPK
    chunks, pcm = [], {}
    for c, p, path, first, ws in probes:
        if not p.words:
            continue
        x = A.decode_pcm(path, 0, None)
        pcm[id(p)] = (x, first)
        for a, b in SPK.speech_chunks(x, first):
            v = SPK.embed(x[max(0, int((a - first) * A.SR)):int((b - first) * A.SR)])
            if v is not None:
                chunks.append({"t": a, "emb": v})
    prof = SPK.profile_from_chunks(chunks, bins=max(3, min(6, len(pcm))))
    out = {"streamer_s": 0.0, "other_s": 0.0, "game_s": 0.0}
    for c, p, path, first, ws in probes:
        if id(p) in pcm:
            x, t0 = pcm[id(p)]
            sm = SPK.tag_words(p.words, x, t0, prof)
            for k in out:
                out[k] += sm[k]
    return prof, ("no voice runs through the scan — untagged" if prof is None else
                  ", ".join(f"{k[:-2]} {v:.0f}s" for k, v in out.items()))


def crowd_bonus(m: dict | None) -> float:
    """Viewer clips outrank any guess: 6 points per 10x views (1k views = 18),
    plus a little for several people clipping the same moment."""
    import math
    if not m:
        return 0.0
    return 6.0 * math.log10(1 + m["views"]) + 1.5 * min(4, m["clips"] - 1)


def grab_thumbs(pl, times: list[float], stem: str) -> list[str]:
    """Small JPEGs (base64) at VOD times, from a low-res playlist."""
    import base64
    import subprocess
    out = []
    for i, t in enumerate(times):
        ts = f"{stem}_{i}.ts"
        jpg = f"{stem}_{i}.jpg"
        try:
            first = T.download_window(pl, t, t + 0.5, ts, pad_s=0)
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{max(0.0, t - first):.2f}", "-i", ts,
                            "-frames:v", "1", "-vf", "scale=240:-2", "-q:v", "6", jpg], check=True,
                           capture_output=True)
            out.append("data:image/jpeg;base64," + base64.b64encode(open(jpg, "rb").read()).decode())
        except Exception:
            out.append("")
        finally:
            for f in (ts, jpg):
                if os.path.exists(f):
                    os.remove(f)
    return out


def _checkpoint(path: str, vod_id: str, s: float, e: float) -> dict:
    """The stages a render already finished for exactly this VOD window (else a fresh checkpoint)."""
    try:
        ck = json.load(open(path))
        if ck.get("vod") == vod_id and abs(ck.get("s", -1) - s) < 0.01 and abs(ck.get("e", -1) - e) < 0.01:
            return ck
    except (OSError, ValueError):
        pass
    return {"vod": vod_id, "s": s, "e": e}


def render_pick(scan_dir: str, pick: int, out_dir: str | None = None, header: str | None = None,
                hook: str | None = None, crop: str | None = None, start: float | None = None,
                end: float | None = None, retime: bool = True, watermark: str | None = V.WATERMARK,
                mute_audio: bool = False, cut: str = "tight", layout: str = "single",
                title: str | None = None, quote: str | None = None, allow_flagged: bool = False,
                fixes: dict | None = None, extra_censor: set[str] | None = None) -> dict:
    t_all = time.time()
    res = json.load(open(os.path.join(scan_dir, "scan.json")))
    c = next(x for x in res["candidates"] if x["rank"] == pick)
    if (c.get("safety") or {}).get("flagged") and not allow_flagged:
        raise RuntimeError(f"#{pick} is flagged for sexual content ({c['safety']['why']}) — platforms flag "
                           "this; pick another moment, or pass --allow-flagged after a person decides")
    if c.get("hard_fail") and not mute_audio:
        log(f"WARNING: #{pick} hard-failed the probe ({c['hard_fail']}) — rendering anyway on request")
    vod_id = res["vod"]["id"]
    win = c["window"]
    tight = None
    if cut == "tight" and start is None and end is None:
        tight = tight_cut(scan_dir, c)
    if tight:
        win = tight
        log(f"tight cut: {T.fmt(win['start_s'])} +{win['end_s'] - win['start_s']:.0f}s ({win['how']})")
    s = start if start is not None else win["start_s"]
    e = end if end is not None else win["end_s"]
    out_dir = out_dir or os.path.join(scan_dir, "renders")
    os.makedirs(out_dir, exist_ok=True)
    name = f"{res['vod']['login']}_{T.fmt(s).replace(':', '')}"
    t0 = time.time()
    src = os.path.join(out_dir, name + ".src.ts")
    ck_path = os.path.join(out_dir, name + ".checkpoint.json")
    ck = _checkpoint(ck_path, vod_id, s, e)              # a killed render resumes from its last finished stage
    if ck.get("first") is not None and os.path.exists(src):
        first = ck["first"]
        log("resuming: the video window is already downloaded")
    else:
        pls = T.playlists(vod_id)
        first = T.download_window(pls["video"], s, e, src + ".part", pad_s=2)
        os.replace(src + ".part", src)
        ck = {**ck, "first": first}
        json.dump(ck, open(ck_path, "w"))
    t_dl = time.time() - t0
    words = A.all_words(json.load(open(os.path.join(scan_dir, "probe", f"words_{int(c['peak_s'])}.json"))))
    t0 = time.time()
    timing = None
    if retime and words and ck.get("words") is not None:
        words, timing = ck["words"], ck.get("timing")
        log("resuming: words already re-timed")
    elif retime and words:
        # v1 v5 lesson: small's word times run 0.2-1.2 s early on in-game
        # dialogue; tiny's land on the audio bursts. Keep small's words,
        # take tiny's times where the tokens agree.
        x = A.decode_pcm(src, s - first, e - s)
        tiny = A.all_words(A.transcribe(x, "tiny", offset=s, prompt=A.vocab_prompt(login=res["vod"]["login"])))
        clip_words = [w for w in words if s <= w["start"] < e]
        new, n_ok, n = retime_words(clip_words, tiny)
        if n and n_ok >= 0.5 * n:
            words = [w for w in words if not (s <= w["start"] < e)] + new
            words.sort(key=lambda w: w["start"])
        timing = f"{n_ok}/{n}"
        json.dump({**ck, "words": words, "timing": timing}, open(ck_path, "w"))
    t_retime = time.time() - t0
    t0 = time.time()
    out = os.path.join(out_dir, name + ".mp4")
    if title:
        from . import safety as SF
        hs = SF.screen(headline=(title or "") + " " + (quote or ""))
        if hs["flagged"] and not allow_flagged:
            raise RuntimeError(f"headline/quote has sexual terms ({hs['why']}) — rewrite it")
    if not crop:
        # re-check the game at render time (scan-time chapters of a live VOD can be stale)
        try:
            crop = V.crop_for_game(T.Vod.fetch(vod_id).game_at(s))
        except Exception:
            crop = c.get("crop", "blur")
    if fixes:
        from . import censor as C
        words = C.apply_fixes(words, fixes)
    info = V.render(src, first, s, e, words, out, crop=crop, layout=layout, title=title, quote=quote,
                    header=header, hook=hook, watermark=watermark, mute_audio=mute_audio,
                    extra_censor=extra_censor)
    t_render = time.time() - t0
    probe = V.ffprobe(out)
    strip = V.frames(out, [1, (e - s) / 2, e - s - 1], os.path.join(out_dir, "frames"))
    # bleep verification: every bleep interval must read as silence
    bleep_ok = None
    if info["bleeps"] and not mute_audio:
        sil = V.silences(out, 0, e - s)
        bleep_ok = all(any(a0 - 0.1 <= a and b <= b0 + 0.1 for a0, b0 in sil) for a, b in info["bleeps"])
    os.remove(src)
    try:
        os.remove(ck_path)                       # done: nothing left to resume
    except OSError:
        pass
    report = {**info, "pick": pick, "window": [s, e], "header": header, "hook": hook,
              "timing_matched": timing, "bleeps_verified_silent": bleep_ok,
              "ffprobe": probe, "frames": strip,
              "timing_s": {"video_download": round(t_dl, 1), "retime": round(t_retime, 1),
                           "render": round(t_render, 1), "total": round(time.time() - t_all, 1)},
              "status": "DRAFT — a person must watch it and approve the header before posting"}
    json.dump(report, open(os.path.join(out_dir, name + ".report.json"), "w"), indent=2)
    log(f"rendered {out} in {report['timing_s']['total']}s "
        f"(download {t_dl:.1f}s, retime {t_retime:.1f}s, render {t_render:.1f}s)")
    return report


def retime_words(words_model: list[dict], words_timing: list[dict]) -> tuple[list[dict], int, int]:
    """v1 caption.retime_words, unchanged in behaviour: align by normalised
    token, take the timing model's start/end where tokens match, interpolate
    the rest, keep starts monotonic."""
    import difflib
    import re

    nums = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
            "seven": "7", "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13",
            "fourteen": "14", "fifteen": "15", "sixteen": "16", "seventeen": "17", "eighteen": "18", "nineteen": "19",
            "twenty": "20", "thirty": "30", "forty": "40", "fifty": "50", "sixty": "60", "seventy": "70",
            "eighty": "80", "ninety": "90", "hundred": "100", "thousand": "1000"}

    def norm(w, side, i):
        t = re.sub(r"[^a-z0-9']", "", w.lower().strip())
        t = nums.get(t, t)                       # "Six" and "6" are the same word (Nov 19: captions 9.5 s early)
        # a token with nothing left (an emoji, punctuation) must never "match" another one somewhere else
        return t if t else f"\x00{side}{i}"
    a = [norm(w["w"], "a", i) for i, w in enumerate(words_model)]
    b = [norm(w["w"], "b", i) for i, w in enumerate(words_timing)]
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    aligned = [None] * len(words_model)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                aligned[i1 + k] = (words_timing[j1 + k]["start"], words_timing[j1 + k]["end"])
    out = [dict(w) for w in words_model]
    idx = [i for i, x in enumerate(aligned) if x]
    if not idx:
        return out, 0, len(out)
    for i, w in enumerate(out):
        if aligned[i]:
            w["start"], w["end"] = round(aligned[i][0], 2), round(aligned[i][1], 2)
        else:
            prev = max((j for j in idx if j < i), default=None)
            nxt = min((j for j in idx if j > i), default=None)
            if prev is not None and nxt is not None:
                w["start"] = round(aligned[prev][0] + (i - prev) / (nxt - prev) * (aligned[nxt][0] - aligned[prev][0]), 2)
            elif prev is not None:
                w["start"] = round(aligned[prev][1] + 0.05, 2)
            else:
                w["start"] = round(max(0.0, aligned[nxt][0] - 0.25), 2)
            w["end"] = round(w["start"] + 0.2, 2)
    for i in range(1, len(out)):
        if out[i]["start"] < out[i - 1]["start"]:
            out[i]["start"] = out[i - 1]["start"]
        if out[i]["end"] < out[i]["start"]:
            out[i]["end"] = round(out[i]["start"] + 0.12, 2)
    return out, len(idx), len(out)


def watch(channels: list[str], out_root: str, every_s: float = 1200, last_s: float = 1800,
          rounds: int | None = None, **scan_kw) -> None:
    """Launch-day loop: every `every_s`, scan the newest `last_s` of each
    channel's newest VOD (it grows while they are live) and keep one ranked
    queue in <out_root>/index.html. Overlapping windows are deduped by
    VOD + peak time. Reads only — nothing is rendered, posted or submitted."""
    import datetime
    seen: dict[str, dict] = {}
    idx_path = os.path.join(out_root, "queue.json")
    if os.path.isfile(idx_path):
        seen = json.load(open(idx_path))
    n = 0
    while rounds is None or n < rounds:
        n += 1
        for ch in channels:
            try:
                v = T.latest_vod(ch)
                end = float(v["length_s"])
                start = max(0.0, end - last_s)
                stamp = datetime.datetime.now().strftime("%m%d-%H%M")
                d = os.path.join(out_root, ch, f"{v['id']}-{stamp}")
                res = scan(v["id"], d, start, end, **scan_kw)
                for c in res["candidates"]:
                    if not c.get("probed"):
                        continue
                    key = f"{v['id']}:{int(c['peak_s'] // 60)}"
                    if key not in seen or c["score"] > seen[key]["score"]:
                        seen[key] = {"channel": ch, "vod": v["id"], "live": v["live"],
                                     "peak": c["peak"], "peak_s": c["peak_s"], "score": c["score"],
                                     "hard_fail": c.get("hard_fail"), "flags": c.get("flags", []),
                                     "game": c.get("game"), "window": c["window"],
                                     "scan_dir": d, "rank_in_scan": c["rank"],
                                     "thumb": (c.get("thumbs") or [""])[1] if c.get("thumbs") else "",
                                     "found": stamp,
                                     "crowd_views": (c.get("crowd") or {}).get("views", 0),
                                     "crowd_title": ((c.get("crowd") or {}).get("titles") or [""])[0]}
            except Exception as exc:
                log(f"{ch}: skipped this round — {exc}")
        json.dump(seen, open(idx_path, "w"), indent=2)
        write_queue(seen, os.path.join(out_root, "index.html"))
        log(f"round {n}: queue has {sum(1 for x in seen.values() if not x['hard_fail'])} live candidates "
            f"-> {out_root}/index.html")
        if rounds is None or n < rounds:
            time.sleep(every_s)


def write_queue(seen: dict, path: str) -> None:
    import html
    rows = sorted(seen.values(), key=lambda x: (x["hard_fail"] is None, x["score"]), reverse=True)
    e = html.escape
    items = "".join(
        f"<tr class='{'bad' if r['hard_fail'] else ''}'><td>{('<img src="' + r['thumb'] + '" alt="">') if r['thumb'] else ''}</td>"
        f"<td><b>{e(r['channel'])}</b><br>{e(r['game'] or '')}</td><td>{r['score']}</td>"
        f"<td>{T.fmt(r['window']['start_s'])}<br><small>found {r['found']}</small></td>"
        f"<td>{('🔥 ' + format(r.get('crowd_views', 0), ',') + ' views — “' + e(r.get('crowd_title', '')[:50]) + '”<br>') if r.get('crowd_views') else ''}"
        f"{e(r['hard_fail'] or '; '.join(r['flags']) or 'clean')}</td>"
        f"<td><a href='{os.path.relpath(r['scan_dir'], os.path.dirname(path))}/review.html'>review #{r['rank_in_scan']}</a><br>"
        f"<a href='https://www.twitch.tv/videos/{r['vod']}?t={int(r['window']['start_s'])}s'>twitch</a></td></tr>"
        for r in rows)
    open(path, "w", encoding="utf-8").write(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Clip queue</title>
<meta http-equiv="refresh" content="120">
<style>:root{{--bg:#0f1115;--fg:#e8e8ea;--mut:#9aa0aa;--line:#2a2f3a;--acc:#8ab4ff}}
@media (prefers-color-scheme: light){{:root{{--bg:#f6f6f4;--fg:#1b1d22;--mut:#5d636e;--line:#dcdde1;--acc:#2456c7}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,sans-serif}}
main{{max-width:1000px;margin:0 auto;padding:16px}} table{{width:100%;border-collapse:collapse}}
td{{border-bottom:1px solid var(--line);padding:6px;vertical-align:top}} img{{width:160px;border-radius:6px}}
a{{color:var(--acc)}} tr.bad{{opacity:.5}} small{{color:var(--mut)}}
@media (max-width:640px){{img{{width:90px}}}}</style></head><body><main>
<h1>Clip queue</h1><p><small>Best first across every scan. Refreshes every 2 min. Nothing here is posted or approved.</small></p>
<table>{items}</table></main></body></html>""")
