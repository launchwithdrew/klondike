"""Single-pass render: source segments -> finished vertical clip, ONE encode.

v1 encoded every clip twice (recut.py: crop -> x264 CRF 20; caption.py:
captions/header/watermark -> x264 CRF 20 again) and transcribed it twice
(small for words, tiny for timing). v2 cuts, crops, captions, heads,
bleeps and watermarks in one ffmpeg pass straight from the downloaded
.ts, using the transcript the probe already made. Half the encode time,
no generation loss.

Look is v1's locked spec, unchanged:
  captions  Inter Display Bold 84, white, black outline 5, one word per
            card at its own start time, MarginV 330 (clear of the UI)
  header    persistent, plain white, no box/pill, no glow, y = 0.13h,
            96px one line / 72px two-line fallback, refuse > 2 lines
  hook      optional, 96px, dark box, y = 0.26h, first 2 s
  watermark bottom-right, 260px, 65% opacity, 32px margin
  bleep     each censored word muted (+-0.05 s), "***" on screen
  output    1080x1920, H.264 CRF 20 veryfast, 30 fps, AAC 128k, +faststart
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile

from . import censor as C

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(os.path.dirname(HERE), "assets")
FONT_DIR = os.path.join(ASSETS, "fonts")
FONT_FILE = os.path.join(FONT_DIR, "InterDisplay-Bold.ttf")
ASS_FONT = "Inter Display"
WATERMARK = os.path.join(ASSETS, "watermark.png")

CROPS = {
    # talking head: 9:16 centre crop
    "centre": "crop=ih*9/16:ih,scale=1080:1920,setsar=1",
    # gameplay: full frame over a blurred fill — HUD, minimap, subtitles kept
    "blur": ("split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,"
             "crop=1080:1920,gblur=sigma=25[bg];[b]scale=1080:-2[fg];"
             "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1"),
    "zoom": "crop=ih*9/16/1.2:ih/1.2,scale=1080:1920,setsar=1",
}


def crop_for_game(game: str) -> str:
    """v1 rule: gameplay = blur, talking-head = centre. Twitch's chapter
    tells us which one this is."""
    g = (game or "").lower()
    if g in ("just chatting", "talk shows & podcasts", "irl", "asmr", "pools, hot tubs, and beaches"):
        return "centre"
    return "blur"


def _ass_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    return f"{h}:{m:02d}:{t % 60:05.2f}"


MIN_CARD = 0.18   # seconds a word stays on screen at minimum


def word_cards(words: list[dict], clip_start: float, clip_end: float, uppercase: bool | None = None) -> list[dict]:
    """One card per word, each at its own start (v1 v5 rule: never show a
    word before it is said), each visible for at least MIN_CARD.

    Bug fixed: re-timing can give several words the SAME start; the
    old code then ended a card at the next word's start = its own start, a
    zero-length card that never appeared (SRT "09,290 --> 09,290"). Now a
    run of words sharing a start is spread across the time until the next
    distinct start, and every card gets at least MIN_CARD.
    """
    if uppercase is None:
        from . import brand as B
        uppercase = B.load()["captions"]["uppercase"]
    ws = [w for w in words if clip_start <= w["start"] < clip_end and w["w"].strip()]
    dur = clip_end - clip_start
    starts = [w["start"] - clip_start for w in ws]
    # spread ties: words with identical starts share the gap to the next start
    i = 0
    while i < len(starts):
        j = i
        while j + 1 < len(starts) and starts[j + 1] - starts[i] < 0.06:
            j += 1
        if j > i:
            nxt = starts[j + 1] if j + 1 < len(starts) else min(dur, starts[i] + 0.3 * (j - i + 1))
            step = max(MIN_CARD, (nxt - starts[i]) / (j - i + 1))
            for k in range(i, j + 1):
                starts[k] = starts[i] + (k - i) * step
        i = j + 1
    cards = []
    for i, w in enumerate(ws):
        s = starts[i]
        e = max(w["end"] - clip_start, s + MIN_CARD)
        if i + 1 < len(ws):
            e = min(e, max(starts[i + 1], s + MIN_CARD))
        e = min(e, dur)
        if e - s < 0.05:
            continue
        text = w["w"].strip()
        text = (text.upper() if uppercase else text).replace("\\", "").replace("{", "(").replace("}", ")")
        cards.append({"start": round(s, 2), "end": round(e, 2), "text": text})
    # no overlaps: a card ends before the next one starts
    for a, b in zip(cards, cards[1:]):
        if a["end"] > b["start"]:
            b["start"] = a["end"]
            b["end"] = max(b["end"], b["start"] + MIN_CARD)
    return [c for c in cards if c["end"] - c["start"] >= 0.05]


def write_ass(cards: list[dict], path: str, margin_v: int = 330, size: int = 84, style: dict | None = None) -> None:
    """Captions in the brand pack's style. words_per_card > 1 shows a short phrase with the word being
    said in the highlight color; 1 shows one word at a time."""
    from . import brand as B
    cs = {**B.load()["captions"], **(style or {})}
    size = cs.get("size") or size
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{cs["font"]},{size},{B.ass_color(cs["color"])},&H000019FF,{B.ass_color(cs["outline"])},&H80000000,-1,0,0,0,100,100,0,0,1,{cs["outline_width"]},0,2,80,80,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    n = max(1, int(cs.get("words_per_card") or 1))
    events = []
    if n == 1:
        events = [(c["start"], c["end"], c["text"]) for c in cards]
    else:
        hi, base = B.ass_color(cs.get("highlight") or cs["color"]), B.ass_color(cs["color"])
        for g in range(0, len(cards), n):
            group = cards[g:g + n]
            for i, c in enumerate(group):
                end = group[i + 1]["start"] if i + 1 < len(group) else c["end"]
                text = " ".join((f"{{\\c{hi}&}}{w['text']}{{\\c{base}&}}" if j == i else w["text"])
                                for j, w in enumerate(group))
                events.append((c["start"], max(end, c["start"] + 0.05), text))
    lines = [head] + [f"Dialogue: 0,{_ass_time(a)},{_ass_time(b)},Cap,,0,0,0,,{t}\n" for a, b, t in events]
    open(path, "w", encoding="utf-8").write("".join(lines))


def write_srt(cards: list[dict], path: str) -> None:
    def t(x):
        return f"{int(x // 3600):02d}:{int(x % 3600 // 60):02d}:{x % 60:06.3f}".replace(".", ",")
    open(path, "w", encoding="utf-8").write(
        "\n".join(f"{i}\n{t(c['start'])} --> {t(c['end'])}\n{c['text']}\n"
                  for i, c in enumerate(cards, 1)))


def _drawtext_safe(text: str) -> str:
    return (text.replace("\\", "\\\\").replace("'", "’")
            .replace(":", "\\:").replace("%", "\\%"))


def wrap(title: str, max_chars: int) -> list[str]:
    lines, cur = [], ""
    for w in title.split():
        cand = (cur + " " + w).strip()
        if len(cand) <= max_chars:
            cur = cand
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def header_filters(header: str) -> str:
    """v7 persistent header: 96px one line (~19 chars), 72px two-line
    fallback (~26/line); refuse anything longer instead of shrinking."""
    for size, max_chars in ((96, 19), (72, 26)):
        lines = wrap(header, max_chars)
        if len(lines) <= 2:
            break
    if len(lines) > 2:
        raise ValueError(f"header needs {len(lines)} lines — shorten it (max 32 chars)")
    out = ""
    for i, line in enumerate(lines):
        out += (f",drawtext=fontfile='{FONT_FILE}':text='{_drawtext_safe(line)}':fontsize={size}:"
                f"fontcolor=white:borderw=0:x=(w-text_w)/2:y=h*0.13+{i * int(size * 1.4)}")
    return out


STACK_TOP = 1312          # default stack split (brand pack "layout.split" overrides): zoom panel height


def render(source: str, source_t0: float, start_s: float, end_s: float,
           words: list[dict], out: str, crop: str = "blur", header: str | None = None,
           hook: str | None = None, hook_s: float = 2.0, watermark: str | None = WATERMARK,
           censor: bool = True, mute_audio: bool = False, layout: str = "single",
           title: str | None = None, quote: str | None = None, extra_censor: set[str] | None = None,
           drop_captions: list[tuple[float, float]] | None = None) -> dict:
    """Render [start_s, end_s] (VOD seconds) of `source` (a .ts whose first
    frame is VOD second source_t0). `words` are VOD-time word dicts."""
    if crop not in CROPS:
        raise ValueError(f"crop must be one of {list(CROPS)}")
    base = out.rsplit(".mp4", 1)[0]
    clip_words = [dict(w) for w in words if start_s <= w["start"] < end_s]
    bleeps: list[list[float]] = []
    if censor:
        clip_words, bleeps = C.censor_words(clip_words, extra=extra_censor)
    if drop_captions:   # no captions in these VOD-time spans (garbled transcription) — audio untouched
        clip_words = [w for w in clip_words if not any(a <= w["start"] < b for a, b in drop_captions)]
    from . import brand as B
    bp = B.load()
    if layout == "stack" and bp["layout"].get("kind") == "single":
        layout = "single"                                   # the channel's pack chose the full-frame look
    stack_top = int(round(1920 * float(bp["layout"].get("split", STACK_TOP / 1920)) / 2)) * 2
    caps = bp["captions"]
    cards = word_cards(clip_words, start_s, end_s, caps.get("uppercase", True))
    ass = base + ".ass"
    write_ass(cards, ass, margin_v=int(round(1920 * (1 - float(caps.get("y", 0.56))))),
              size=caps.get("size") or (72 if layout == "stack" else 84))
    cap_font_dir = os.path.dirname(B.font(caps.get("font_file", "InterDisplay-Bold.ttf")))
    write_srt(cards, base + ".srt")
    rel_bleeps = [[round(max(0.0, a - start_s), 2), round(b - start_s, 2)] for a, b in bleeps]
    dur = end_s - start_s

    card_png = None
    if layout == "stack":
        # top: the action, zoomed (centre of the frame); bottom: the full frame
        # with the facecam and HUD; the title card sits on the seam
        zw = int(1080 * 1080 / stack_top) // 2 * 2
        vf = (f"[0:v]split[s1][s2];[s1]crop={zw}:ih:(iw-{zw})/2:0,scale=1080:{stack_top},setsar=1[top];"
              f"[s2]scale=1080:{1920 - stack_top},setsar=1[bot];[top][bot]vstack=2,"
              f"subtitles='{ass}':fontsdir='{cap_font_dir}'")
    else:
        vf = f"[0:v]{CROPS[crop]},subtitles='{ass}':fontsdir='{cap_font_dir}'"
    if title and bp["card"].get("show", True):
        from .card import make_card
        t = C.censor_text(title) if censor else title
        q = (C.censor_text(quote) if censor else quote) if quote else None
        card_png, card_h, card_mid = make_card(t, q, base + ".card.png")
        pos = bp["card"].get("position", "top")
        if pos == "seam" and layout == "stack":
            card_y = stack_top - card_mid
        elif pos == "bottom":
            card_y = 1920 - card_h - 380                    # above the platforms' caption/buttons area
        else:
            card_y = 150                                    # below the platforms' top bar
    if header:
        h = C.censor_text(header) if censor else header
        if censor and h != header:
            raise ValueError("header contains profanity — rewrite it clean")
        vf += header_filters(h)
    if hook:
        hk = C.censor_text(hook) if censor else hook
        vf += (f",drawtext=fontfile='{FONT_FILE}':text='{_drawtext_safe(hk)}':fontsize=96:"
               f"fontcolor=white:borderw=6:bordercolor=black:x=(w-text_w)/2:y=h*0.26:"
               f"box=1:boxcolor=black@0.55:boxborderw=28:enable='between(t,0,{hook_s})'")
    logo = bp["logo"]
    if watermark == WATERMARK:
        watermark = B.asset(logo.get("file", "assets/watermark.png"))
    use_wm = bool(watermark) and os.path.isfile(watermark)
    extra_inputs, chain, n = [], vf + "[v0]", 1
    last = "v0"
    if card_png:
        # bounded loop: an endless looped image can stall ffmpeg's frame sync on some sources
        extra_inputs += ["-loop", "1", "-t", f"{dur + 2:.3f}", "-framerate", "30", "-i", card_png]
        chain += f";[v0][{n}:v]overlay=0:{card_y}:shortest=1[v1]"
        last, n = "v1", n + 1
    if use_wm:
        extra_inputs += ["-i", watermark]
        m = int(logo.get("margin", 32))
        corner = logo.get("corner", "bottom-right")
        ox = f"W-w-{m}" if "right" in corner else f"{m}"
        oy = f"H-h-{m}" if "bottom" in corner else f"{m}"
        chain += (f";[{n}:v]format=rgba,colorchannelmixer=aa={float(logo.get('opacity', 0.65))},"
                  f"scale={int(logo.get('width', 260))}:-1[wm];[{last}][wm]overlay={ox}:{oy}[v2]")
        last, n = "v2", n + 1
    fc = chain + f";[{last}]format=yuv420p[vout]"
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start_s - source_t0:.3f}", "-i", source] + extra_inputs
    cmd += ["-filter_complex", fc, "-map", "[vout]"]
    if mute_audio:
        cmd += ["-an"]
    else:
        cmd += ["-map", "0:a:0"]
        if rel_bleeps:
            cmd += ["-af", ",".join(f"volume=0:enable='between(t,{a:.2f},{b:.2f})'"
                                    for a, b in rel_bleeps)]
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    # -t as an OUTPUT option: as an input option it is ignored for .ts
    # sources once a filter graph is attached (test caught 17.4 s for 10 s)
    cmd += ["-t", f"{dur:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-r", "30",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", out]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + p.stderr[-1500:])
    transcript = {"clip_start_vod_s": start_s, "clip_end_vod_s": end_s,
                  "segments": [{"start": c["start"], "end": c["end"], "text": c["text"],
                                "words": [{"w": c["text"], "start": c["start"], "end": c["end"]}]}
                               for c in cards],
                  "bleep_intervals": rel_bleeps}
    json.dump(transcript, open(base + ".transcript.json", "w"), indent=2)
    return {"out": out, "cards": len(cards), "censored": sum("***" in c["text"] for c in cards),
            "bleeps": rel_bleeps, "watermark": use_wm, "crop": crop if layout == "single" else "stack",
            "title": title, "quote": quote}


def ffprobe(path: str) -> dict:
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "stream=codec_type,codec_name,width,height:format=duration,size",
                        "-of", "json", path], capture_output=True, text=True)
    return json.loads(p.stdout or "{}")


def frames(path: str, times: list[float], out_dir: str, width: int = 270) -> list[str]:
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(path))[0]
    outs = []
    for t in times:
        o = os.path.join(out_dir, f"{stem}_{t:05.1f}s.jpg")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", path,
                        "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", o])
        if os.path.isfile(o):
            outs.append(o)
    return outs


def silences(path: str, start: float, length: float) -> list[tuple[float, float]]:
    """silencedetect at -v info (v1 lesson: it prints nothing at -v error)."""
    p = subprocess.run(["ffmpeg", "-v", "info", "-ss", str(start), "-t", str(length), "-i", path,
                        "-af", "silencedetect=noise=-50dB:d=0.1", "-f", "null", "-"],
                       capture_output=True, text=True)
    st, out = None, []
    for line in p.stderr.splitlines():
        if "silence_start" in line:
            st = float(line.rsplit(":", 1)[1])
        elif "silence_end" in line and st is not None:
            out.append((start + st, start + float(line.split("silence_end:")[1].split("|")[0])))
            st = None
    return out
