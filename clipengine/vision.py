"""Vision — cheap looks at the actual frames, before any person or agent spends a glance.

Text can't tell a moment from a cutscene, a menu or a loading screen; four
frames a second, 64 px wide, can. Each check returns a flag the banger score
penalises (config/grading.json):

  cutscene   the game's own cinematics — every clipper has them, nothing of ours in it.
             GTA VI cutscenes on stream aren't letterboxed; what gives them away is
             editing: hard shot cuts every few seconds (shot / reverse shot) with a
             mostly still camera between them, where gameplay is one moving camera.
             Measured on launch-week streams: cutscenes 12–23 cuts/min with median motion
             3.7–6.7; gameplay 1–6 cuts/min with motion ~12. Letterbox bars also count.
  static     almost nothing moves for most of the clip: a menu, map, phone or pause
  dark       mostly black: loading or "be right back" screens

It also makes the one contact sheet the operator looks at (8 frames, one image).
"""
from __future__ import annotations

import subprocess

import numpy as np

W, H = 64, 36


def frames(path: str, fps: float = 1.0, start: float = 0.0, dur: float | None = None) -> np.ndarray:
    """Grey 64x36 frames at `fps` → array (n, H, W) uint8."""
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{start:.2f}", "-i", path]
    if dur:
        cmd += ["-t", f"{dur:.2f}"]
    cmd += ["-vf", f"fps={fps},scale={W}:{H},format=gray", "-f", "rawvideo", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=False).stdout
    n = len(raw) // (W * H)
    return np.frombuffer(raw[: n * W * H], dtype=np.uint8).reshape(n, H, W) if n else np.zeros((0, H, W), np.uint8)


def shot_cuts(fr: np.ndarray, fps: float) -> tuple[float, float]:
    """(hard cuts per minute, median motion) on the centre of the frame, where overlays rarely sit."""
    if len(fr) < 3:
        return 0.0, 0.0
    c = fr[:, int(H * 0.2):int(H * 0.8), int(W * 0.2):int(W * 0.8)].astype(np.float32)
    d = np.abs(np.diff(c, axis=0)).mean(axis=(1, 2))
    h = np.array([np.histogram(x, bins=16, range=(0, 255))[0] / x.size for x in c])
    hd = np.abs(np.diff(h, axis=0)).sum(axis=1)
    med = float(np.median(d))
    cuts = int(((d > max(12.0, 4 * med)) & (hd > 0.35)).sum())
    return cuts / (len(fr) / fps) * 60, med


def live_features(fr: np.ndarray, fps: float = 4.0) -> dict:
    """Raw liveliness measures of grey 64x36 frames (see liveliness() for the 0–1 score):
      motion      median frame-to-frame change in the centre (gameplay ~8–15, menus/maps ~0–2)
      busy        share of frame pairs with real change (> 3 grey levels)
      still       share of frame pairs where nothing moves (< 2): a menu, a map, a shop screen. Launch week:
                  real moments ≤ 0.33, menus / maps / shop screens 0.41–0.97
      burst       90th-percentile change ÷ median: action is bursty, plain driving is even
      cuts_pm     hard cuts per minute that are REAL scene changes — a cut undone by another within a
                  second (a menu opening and closing, a flash) is flicker
      flicker_pm  those flicker cuts per minute
      bright      mean brightness (0–255); detail = mean edge strength (blurry/dark frames are low)
      flat        share of frames whose centre is mostly flat colour: menus, maps, title cards, loading"""
    n = len(fr)
    if n < 3:
        return {"n": int(n), "motion": 0.0, "busy": 0.0, "still": 1.0 if n else 0.0, "burst": 1.0, "cuts_pm": 0.0, "flicker_pm": 0.0,
                "bright": float(fr.mean()) if n else 0.0, "detail": 0.0, "flat": 1.0 if n else 0.0}
    f = fr.astype(np.float32)
    c = f[:, int(H * 0.2):int(H * 0.8), int(W * 0.2):int(W * 0.8)]
    d = np.abs(np.diff(c, axis=0)).mean(axis=(1, 2))
    med = float(np.median(d))
    h = np.array([np.histogram(x, bins=16, range=(0, 255))[0] / x.size for x in c])
    hd = np.abs(np.diff(h, axis=0)).sum(axis=1)
    cut_at = np.where((d > max(12.0, 4 * med)) & (hd > 0.35))[0]
    k = max(1, int(round(fps)))
    real = flick = 0
    cuts, skip = set(int(i) for i in cut_at), set()
    for i in sorted(cuts):                 # frames i -> i+1 is the cut
        if i in skip:
            continue
        # flicker: another cut within a second brings back a picture closer to the one before the first cut
        # (a menu or phone opening and closing, a flash) — not a change of scene
        back = next((j for j in range(i + 1, min(n - 1, i + 1 + k)) if j in cuts and
                     np.abs(c[j + 1] - c[i]).mean() < np.abs(c[j + 1] - c[i + 1]).mean()), None)
        if back is not None:
            flick += 1
            skip.add(back)
        else:
            real += 1
    mins = n / fps / 60
    gx = np.abs(np.diff(f, axis=2)).mean()
    gy = np.abs(np.diff(f, axis=1)).mean()
    ch, cw = (c.shape[1] // 4) * 4, (c.shape[2] // 4) * 4
    blocks = c[:, :ch, :cw].reshape(n, ch // 4, 4, cw // 4, 4).std(axis=(2, 4))
    flat = float(((blocks < 2.5).mean(axis=(1, 2)) >= 0.6).mean())
    return {"n": int(n), "motion": round(med, 2), "busy": round(float((d > 3).mean()), 2),
            "still": round(float((d < 2).mean()), 2),
            "burst": round(float(np.percentile(d, 90) / max(0.5, med)), 2),
            "cuts_pm": round(real / mins, 1), "flicker_pm": round(flick / mins, 1),
            "bright": round(float(f.mean()), 1), "detail": round(float(gx + gy) / 2, 2), "flat": round(flat, 2)}


# liveliness: how much is HAPPENING on screen (0–1). Measured on launch-week VODs (eval/eval_dull.py):
# gameplay moves 9–24 grey levels/frame at 4 fps with busy ≥ 0.8; menus, settings and web pages 0–1.5;
# map screens and title cards are flat colour; a dark scene sits under brightness ~30.
LIVE_MOTION = (1.5, 9.5)  # median change: at or under the first nothing moves, at the second fully lively
DULL_BELOW = 0.5          # liveliness under this: "dull picture" (menus, maps, standing at a counter)
FLAT_MENU = 0.4           # share of flat (menu/map/title) frames that flags the clip
STILL_MENU = 0.4          # share of still frame pairs that flags it static (real moments ≤ 0.33 at launch)


def liveliness(lf: dict) -> float:
    if not lf or not lf.get("n"):
        return 0.0
    lo, hi = LIVE_MOTION
    move = 0.75 * min(1.0, max(0.0, (lf["motion"] - lo) / (hi - lo))) + 0.25 * lf["busy"]
    flat = 1.0 - 0.8 * lf.get("flat", 0.0)
    still = 1.0 - lf.get("still", 0.0)
    dark = min(1.0, max(0.0, (lf["bright"] - 12) / 25))
    return round(max(0.0, min(1.0, move * flat * still * dark)), 2)


def analyse_live(lf: dict, static: float = 0.0, dark: float = 0.0) -> dict:
    """Flags + liveliness from live_features() (and analyse()'s static / dark shares)."""
    L = liveliness(lf)
    flags = []
    if lf.get("n"):
        if lf.get("flat", 0.0) >= FLAT_MENU:
            flags.append(f"menu/map screens ({int(lf['flat'] * 100)}% of frames)")
        elif static >= 0.5 or lf.get("still", 0.0) >= STILL_MENU or (lf["motion"] < 1.5 and lf["busy"] < 0.3):
            flags.append("static (menu/map/pause?)")
        if dark >= 0.4 or lf["bright"] < 25:
            flags.append("dark (loading/BRB screen?)")
        if L < DULL_BELOW:
            flags.append(f"dull picture (liveliness {L:.2f})")
    return {"liveliness": L, "flags": flags}


def analyse(fr: np.ndarray, fps: float = 1.0) -> dict:
    if len(fr) == 0:
        return {"frames": 0, "letterbox": 0.0, "static": 0.0, "dark": 0.0, "cuts_per_min": 0.0, "flags": []}
    cpm, motion = shot_cuts(fr, fps) if fps >= 2 else (0.0, 0.0)
    f = fr.astype(np.float32)
    band = max(2, H // 9)
    top, bot, mid = f[:, :band, :].mean(axis=(1, 2)), f[:, -band:, :].mean(axis=(1, 2)), f[:, band:-band, :].mean(axis=(1, 2))
    letter = ((top < 14) & (bot < 14) & (mid > 30)).mean()
    dark = (f.mean(axis=(1, 2)) < 18).mean()
    if len(f) > 1:
        diff = np.abs(np.diff(f, axis=0)).mean(axis=(1, 2))
        static = (diff < (1.5 if fps <= 1 else 0.8)).mean()
    else:
        static = 0.0
    flags = []
    if letter >= 0.6:
        flags.append("cutscene? (letterboxed)")
    elif cpm >= 10 and motion < 8:
        flags.append(f"cutscene? ({cpm:.0f} shot cuts/min)")
    if static >= 0.7 and dark < 0.5:
        flags.append("static (menu/map/pause?)")
    if dark >= 0.5:
        flags.append("dark (loading/BRB screen?)")
    return {"frames": int(len(f)), "letterbox": round(float(letter), 2), "static": round(float(static), 2),
            "dark": round(float(dark), 2), "cuts_per_min": round(cpm, 1), "motion": round(motion, 1), "flags": flags}


SUB_W, SUB_H = 480, 270
SUB_EDGE = 0.03          # share of strong horizontal edges in the subtitle band that means text is there


def subtitle_score(f: np.ndarray) -> float:
    """Game subtitles: white text, bottom centre of the game frame (GTA VI: ~89% down). Measured on launch
    week: 0.04–0.10 strong-edge share while a character talks, ~0 in gameplay, maps and menus."""
    h, w = f.shape
    band = f[int(h * 0.855):int(h * 0.935), int(w * 0.22):int(w * 0.78)].astype(np.int16)
    return float((np.abs(np.diff(band, axis=1)) > 60).mean()) if band.size else 0.0


def timeline(path: str, start: float = 0.0, dur: float | None = None) -> list[dict]:
    """Per second of the clip: {"sub": subtitles on screen, "cut": inside cutscene-style editing}.
    The game hints speaker.py uses: speech in those seconds is likely the game's."""
    big = frames_sized(path, 1.0, start, dur, SUB_W, SUB_H)
    small = frames(path, 4.0, start, dur)
    n = max(len(big), len(small) // 4)
    out = []
    for i in range(n):
        sub = subtitle_score(big[i]) >= SUB_EDGE if i < len(big) else False
        a, b = max(0, (i - 5) * 4), min(len(small), (i + 6) * 4)
        cpm, motion = shot_cuts(small[a:b], 4.0) if b - a >= 12 else (0.0, 99.0)
        out.append({"sub": bool(sub), "cut": bool(cpm >= 10 and motion < 8)})
    return out


def frames_sized(path: str, fps: float, start: float, dur: float | None, w: int, h: int) -> np.ndarray:
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{start:.2f}", "-i", path]
    if dur:
        cmd += ["-t", f"{dur:.2f}"]
    cmd += ["-vf", f"fps={fps},scale={w}:{h},format=gray", "-f", "rawvideo", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=False).stdout
    n = len(raw) // (w * h)
    return np.frombuffer(raw[: n * w * h], dtype=np.uint8).reshape(n, h, w) if n else np.zeros((0, h, w), np.uint8)


def game_hint_fn(tl: list[dict], t0: float):
    """timeline -> game_at(t) for stream time t (the timeline's second 0 is stream time t0)."""
    def game_at(t: float) -> bool:
        i = int(t - t0)
        if any(tl[j]["sub"] for j in (i - 1, i, i + 1) if 0 <= j < len(tl)):
            return True
        # cutscene-style editing alone misfires on maps and fast gameplay: only next to subtitles
        return bool(0 <= i < len(tl) and tl[i]["cut"] and
                    any(tl[j]["sub"] for j in range(max(0, i - 5), min(len(tl), i + 6))))
    return game_at


def combine(an_flags: list[str], live: dict) -> list[str]:
    """analyse()'s flags (cutscene) + analyse_live()'s (menu/map, static, dark, dull picture) — the live
    measures replace analyse()'s own static / dark checks."""
    keep = [f for f in an_flags if not f.startswith(("static (", "dark ("))]
    return keep + [f for f in live["flags"] if f not in keep]


def check(path: str, start: float = 0.0, dur: float | None = None, fps: float = 4.0) -> dict:
    """The frame checks + liveliness of [start, start+dur] of a video: analyse()'s fields, plus
    "live" (live_features) and "liveliness" (0–1); "flags" is the combined list."""
    fr = frames(path, fps, start, dur)
    out = analyse(fr, fps)
    lf = live_features(fr, fps)
    la = analyse_live(lf, out["static"], out["dark"])
    return {**out, "live": lf, "liveliness": la["liveliness"], "flags": combine(out["flags"], la)}


def contact_sheet(path: str, out: str, n: int = 8, width: int = 320, start: float = 0.0,
                  dur: float | None = None) -> str:
    """One image of `n` evenly spaced frames (4 per row) — the only picture the operator needs.
    start/dur: just the draft's window of a longer source."""
    d = dur
    if not d:
        try:
            d = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                                     capture_output=True, text=True).stdout.strip() or 0) - start
        except ValueError:
            d = 0
    fps = n / d if d and d > 0 else 1
    cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{max(0.0, start):.2f}", "-i", path]
    if dur:
        cmd += ["-t", f"{dur:.2f}"]
    subprocess.run(cmd + ["-vf", f"fps={fps:.4f},scale={width}:-2,tile=4x{max(1, (n + 3) // 4)}", "-frames:v", "1", out],
                   check=False)
    return out
