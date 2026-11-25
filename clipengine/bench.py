"""`clipengine bench` — a bounded benchmark of this machine on the real pipeline, and a prediction against the
speed targets, so a host's fitness is measured before a stream day rather than discovered during one.

It takes a heavy-job turn at the lowest priority (capture always goes first) and measures, each with a time cap:
  encode      x264 at the profile's preset, 10 s of 1080×1920 (realtime factor)
  render      the real render pipeline (this channel's layout, card, captions, logo) on a synthetic clip
  transcribe  the profile's Whisper model on recorded speech under game audio (skipped if the model isn't
              downloaded — `bash setup/setup.sh` fetches it)
Then it predicts the two speed targets (spec F2) for this host: a moment to a ready draft under 3 min, and a draft
to a finished render under 2 min (p50, excluding waits for a turn). `--profile lite|full` measures the other
profile's settings without changing the config. Results go to runs/bench/<host>-<time>.json;
`clipengine bench --compare A.json B.json` puts two hosts (the tester's and a candidate) side by side.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import tempfile
import time

from . import host as H

TARGET_DRAFT_S = 180.0          # F2: scan → draft p50
TARGET_RENDER_S = 120.0         # F2: draft → render p50
DRAFT_WINDOW_S = 45.0           # audio a live draft transcribes (setup + payoff + reaction + handles)
CLIP_S = 30.0                   # a typical finished clip
DRAFT_OVERHEAD_S = 20.0         # cut, vision timeline, speaker tags, scoring (measured order on small hosts)
STEP_CAP_S = 240.0              # no single step may run longer than this


def _repo() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _settings(profile: str | None) -> dict:
    from . import ops as O
    cfg = O.config()
    if profile:
        cfg = {**cfg, **O.PROFILES.get(profile, {})}
        if profile == "full":
            cfg["render_preset"], cfg["whisper_model"] = O.DEFAULTS["render_preset"], O.DEFAULTS["whisper_model"]
    return {"profile": profile or cfg.get("profile", "full"), "preset": cfg.get("render_preset") or "veryfast",
            "model": cfg.get("whisper_model") or "small"}


def _source(path: str, seconds: float) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    f"testsrc2=size=1920x1080:rate=30:duration={seconds}", "-f", "lavfi", "-i",
                    f"sine=frequency=440:sample_rate=48000:duration={seconds}", "-shortest", "-c:v", "libx264",
                    "-preset", "ultrafast", "-c:a", "aac", path], check=True, timeout=STEP_CAP_S)


def measure_render(preset: str, seconds: float) -> dict:
    """The real render pipeline on `seconds` of synthetic stream, timed."""
    from unittest import mock
    from . import brand as B, finish as FN, render as V
    pack = B.load(fresh=True)
    kind = (pack.get("layout") or {}).get("kind", "stack")
    words = [{"w": f" word{i}", "start": i * 0.5, "end": i * 0.5 + 0.4} for i in range(int(seconds * 2))]
    with tempfile.TemporaryDirectory(prefix="klondike-bench-") as tmp:
        src, out = os.path.join(tmp, "src.mp4"), os.path.join(tmp, "out.mp4")
        _source(src, seconds)
        t0 = time.monotonic()
        with mock.patch.object(V, "render_preset", return_value=preset):
            V._render(src, 0, 0, seconds, words, out, layout=kind,
                      facecam=FN.FACECAM_BOX if kind == "facecam" else None,
                      title="They landed JUST IN TIME", quote="look now")
        wall = time.monotonic() - t0
    return {"seconds": seconds, "wall_s": round(wall, 1), "realtime": round(seconds / wall, 2), "layout": kind}


def measure_transcribe(model_name: str, max_audio_s: float = 30.0) -> dict:
    """The profile's Whisper model on real speech; model load timed separately."""
    from . import audio as A
    fx = os.path.join(_repo(), "tests", "fixtures", "speech_under_game.ogg")
    if not os.path.exists(fx):
        return {"skipped": "no speech sample in this install (tests/fixtures)"}
    t0 = time.monotonic()
    try:
        A.model(model_name)
    except Exception as exc:
        return {"skipped": f"Whisper '{model_name}' isn't downloaded here ({str(exc)[:80]}) — bash setup/setup.sh"}
    load = time.monotonic() - t0
    x = A.decode_pcm(fx, 0, max_audio_s)
    secs = len(x) / A.SR
    t0 = time.monotonic()
    result = A._transcribe(x, model_name, 0.0, "")
    wall = time.monotonic() - t0
    measured = {"model": model_name, "device": result.get("device"), "audio_s": round(secs, 1),
                "load_s": round(load, 1), "wall_s": round(wall, 1),
                "realtime": round(secs / max(wall, 1e-3), 2),
                "words": A.real_words(result), "fallback": result.get("fallback")}
    if A.requested_device() == "cuda" and result.get("device") == "cuda":
        start = time.monotonic()
        cpu = A._whisper_once(x, model_name, 0.0, A.vocab_prompt(),
                              False if result["pass"] == "no-vad" else A.VAD_GENTLE, "cpu")
        measured["cpu_reference"] = {"infer_s": round(time.monotonic() - start, 2), "words": A.real_words(cpu)}
    return measured


def predict(render: dict | None, trans: dict | None) -> dict:
    """Stage times (p50, no waiting) for the two speed targets, from the measured realtime factors."""
    out: dict = {}
    if trans and trans.get("realtime"):
        draft = DRAFT_WINDOW_S / trans["realtime"] + DRAFT_OVERHEAD_S
        out["draft_s"] = round(draft)
        out["draft_ok"] = draft <= TARGET_DRAFT_S
    if render and render.get("realtime"):
        rend = CLIP_S / render["realtime"]
        out["render_s"] = round(rend)
        out["render_ok"] = rend <= TARGET_RENDER_S
    return out


def run(profile: str | None = None, seconds: float = 10.0, say=print) -> dict:
    from .admission import slot
    s = _settings(profile)
    res: dict = {"host": H.summary(), "settings": s, "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    from . import __version__
    res["engine"] = __version__
    say(f"bench: {res['host']['host']} — {res['host']['cpus']:g} CPUs ({res['host']['cpus_from']}), "
        f"{res['host']['mem_total_gb']:g} GB, profile {s['profile']} (x264 {s['preset']}, Whisper {s['model']})")
    with slot("bench", kind="scan", say=lambda m: say(f"bench: {m}")):          # capture always goes first
        say("bench: encode…")
        res["encode"] = {"preset": s["preset"], "realtime": H._encode(
            ["-c:v", "libx264", "-preset", s["preset"], "-threads", str(H.threads()), "-pix_fmt", "yuv420p"],
            seconds, timeout=STEP_CAP_S)}
        res["hardware"] = H.hardware_encoders()
        rt = res["encode"]["realtime"]
        if rt and rt * STEP_CAP_S >= seconds * 3:                      # the full render fits the cap
            say("bench: render (the real pipeline)…")
            try:
                res["render"] = measure_render(s["preset"], seconds)
            except Exception as exc:
                res["render"] = {"error": str(exc)[:200]}
        else:
            res["render"] = {"skipped": "encoding is too slow to render within the time cap"}
        say("bench: transcribe…")
        try:
            res["transcribe"] = measure_transcribe(s["model"])
        except Exception as exc:
            res["transcribe"] = {"error": str(exc)[:200]}
    res["predict"] = predict(res.get("render"), res.get("transcribe"))
    d = os.path.join(_repo(), "runs", "bench")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{res['host']['host']}-{dt.datetime.now():%Y%m%d-%H%M%S}.json")
    with open(path, "w") as fh:
        json.dump(res, fh, indent=1)
    res["path"] = path
    for line in lines(res):
        say(line)
    return res


def _fmt_ok(ok: bool | None) -> str:
    return "" if ok is None else (" ✓" if ok else " ✗ over target")


def lines(res: dict) -> list[str]:
    p, r, t = res.get("predict") or {}, res.get("render") or {}, res.get("transcribe") or {}
    hw = [k for k, ok in (res.get("hardware") or {}).items() if ok]
    out = [f"  encode   x264 {res['settings']['preset']}: {res['encode'].get('realtime') or '–'}× realtime"
           + (f" · hardware that works: {', '.join(hw)}" if hw else "")]
    out.append(f"  render   {r['realtime']}× realtime ({r['layout']}) → a {CLIP_S:.0f} s clip ≈ {p.get('render_s')} s"
               f"{_fmt_ok(p.get('render_ok'))} (target {TARGET_RENDER_S:.0f} s)" if r.get("realtime") else
               f"  render   {r.get('skipped') or r.get('error')}")
    out.append(f"  whisper  {t['model']} ({t.get('device') or 'device unrecorded'}): {t['realtime']}× realtime (load {t['load_s']} s) → a draft ≈ {p.get('draft_s')} s"
               f"{_fmt_ok(p.get('draft_ok'))} (target {TARGET_DRAFT_S:.0f} s)" if t.get("realtime") else
               f"  whisper  {t.get('skipped') or t.get('error')}")
    if t.get("fallback"):
        out.append(f"  whisper  CUDA fell back to CPU: {t['fallback']}")
    measured = [p[k] for k in ("draft_ok", "render_ok") if k in p]
    if not measured:
        out.append("  verdict  speed targets unmeasured")
    elif not all(measured):
        out.append("  verdict  this host misses a speed target on this profile — try `bench --profile lite` or a bigger host")
    elif len(measured) < 2:
        out.append("  verdict  one speed target met; the other is unmeasured")
    else:
        out.append("  verdict  this host meets both speed targets on this profile")
    return out


def compare(a_path: str, b_path: str) -> list[str]:
    a, b = json.load(open(a_path)), json.load(open(b_path))
    row = lambda name, f: f"  {name:<22} {f(a)!s:>16} {f(b)!s:>16}"
    g = lambda *ks: (lambda r: _dig(r, ks))
    return [f"  {'':<22} {a['host']['host'][:16]:>16} {b['host']['host'][:16]:>16}",
            row("CPUs", g("host", "cpus")), row("memory GB", g("host", "mem_total_gb")),
            row("profile", g("settings", "profile")),
            row("encode × realtime", g("encode", "realtime")), row("render × realtime", g("render", "realtime")),
            row("whisper × realtime", g("transcribe", "realtime")),
            row("draft s (≤180)", g("predict", "draft_s")), row("render s (≤120)", g("predict", "render_s"))]


def _dig(r: dict, ks: tuple) -> object:
    for k in ks:
        r = (r or {}).get(k) if isinstance(r, dict) else None
    return "–" if r is None else r
