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


def check(path: str, start: float = 0.0, dur: float | None = None, fps: float = 4.0) -> dict:
    return analyse(frames(path, fps, start, dur), fps)


def contact_sheet(path: str, out: str, n: int = 8, width: int = 320) -> str:
    """One image of `n` evenly spaced frames (4 per row) — the only picture the operator needs."""
    try:
        d = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                                 capture_output=True, text=True).stdout.strip() or 0)
    except ValueError:
        d = 0
    fps = n / d if d > 0 else 1
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-vf",
                    f"fps={fps:.4f},scale={width}:-2,tile=4x{max(1, (n + 3) // 4)}", "-frames:v", "1", out],
                   check=False)
    return out
