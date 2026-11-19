"""Find the beat — the second the moment happens — and cut tight around it.

A 62 s window around a chat spike is mostly dead weight: the moment is a few
seconds, the setup a few more, the reaction a few after. This module finds:

  beat   the sharpest rise in the streamer's audio (a shout, a laugh, a
         scream, a burst of talking) in the stretch chat is reacting to —
         chat reacts LAG seconds after the beat (stream delay + typing),
         so we look in [chat_peak - lag_max, chat_peak - lag_min]
  start  the start of the sentence that sets the beat up (4-15 s before it),
         never on silence
  end    when the reaction dies down: the first pause > 1.4 s after the
         beat + 3 s, capped at beat + 22 s

and returns a 12-40 s "tight" window next to the 62 s one. The lag range is
measured from Twitch viewer clips (eval/eval_clips.py --lag), not guessed.
"""
from __future__ import annotations

import numpy as np

ENV_STEP = 0.25          # seconds per envelope frame
LAG_MIN, LAG_MAX = 2.0, 22.0


def envelope(x: np.ndarray, sr: int = 16000, step: float = ENV_STEP) -> np.ndarray:
    n = int(sr * step)
    k = len(x) // n
    if k == 0:
        return np.array([-120.0])
    fr = x[: k * n].reshape(k, n)
    return 20 * np.log10(np.sqrt((fr ** 2).mean(axis=1) + 1e-12) + 1e-12)


def find_beat(env: np.ndarray, env_t0: float, words: list[dict], chat_peak: float,
              lag_min: float = LAG_MIN, lag_max: float = LAG_MAX) -> dict:
    """Most likely beat time (VOD seconds) and why."""
    t = env_t0 + np.arange(len(env)) * ENV_STEP
    live = env > -60
    if not live.any():
        return {"beat_s": chat_peak - 10, "how": "silent — fell back to chat peak - 10 s", "jump_db": 0.0}
    base = float(np.median(env[live]))
    # 1 s smoothed loudness and its rise over the previous 1.5 s
    k = int(1.0 / ENV_STEP)
    sm = np.convolve(env, np.ones(k) / k, mode="same")
    lagk = int(1.5 / ENV_STEP)
    rise = np.zeros_like(sm)
    rise[lagk:] = sm[lagk:] - sm[:-lagk]
    # word density (words per second) — a burst of talking is a beat too
    dens = np.zeros_like(sm)
    for w in words:
        i = int((w["start"] - env_t0) / ENV_STEP)
        if 0 <= i < len(dens):
            dens[i] += 1
    dens = np.convolve(dens, np.ones(int(2.0 / ENV_STEP)), mode="same") / 2.0  # words/s over 2 s
    # candidates: inside the lag range before the chat peak
    sel = (t >= chat_peak - lag_max) & (t <= chat_peak - lag_min)
    if not sel.any():
        sel = t <= chat_peak
    loud = np.clip(sm - base, 0, 25)
    score = 0.6 * np.clip(rise, 0, 20) + 0.4 * loud + 1.5 * np.clip(dens - 2.0, 0, 4)
    # mild preference for beats nearer the typical lag (~8 s): chat reacts fast
    prox = 1.0 - np.clip(np.abs((chat_peak - t) - 8.0) / 20.0, 0, 0.5)
    score = np.where(sel, score * prox, -1)
    i = int(np.argmax(score))
    return {"beat_s": round(float(t[i]), 2), "jump_db": round(float(rise[i]), 1),
            "loud_db": round(float(sm[i] - base), 1), "words_per_s": round(float(dens[i]), 1),
            "how": "loudness rise" if rise[i] >= 6 else ("talk burst" if dens[i] > 3 else "loudest point")}


def tight_window(words: list[dict], segments: list[dict], beat_s: float,
                 setup_min: float = 4.0, setup_max: float = 15.0, tail_max: float = 22.0,
                 min_len: float = 12.0, max_len: float = 40.0, pause: float = 1.4) -> dict:
    """Setup -> beat -> reaction, nothing else."""
    # start: a sentence (segment) start 4-15 s before the beat; else first word in range
    starts = [s["start"] for s in segments if beat_s - setup_max <= s["start"] <= beat_s - setup_min]
    if starts:
        start = min(starts, key=lambda x: abs((beat_s - x) - 8.0)) - 0.2
    else:
        ws = [w["start"] for w in words if beat_s - setup_max <= w["start"] <= beat_s - setup_min]
        start = (ws[0] - 0.2) if ws else beat_s - 6.0
    # end: first pause > `pause` after beat + 3 s; cap at beat + tail_max
    after = [w for w in words if w["start"] >= beat_s + 3.0]
    end = beat_s + 6.0
    prev = None
    for w in after:
        if prev is not None and w["start"] - prev > pause:
            break
        end = w["end"] + 1.0
        prev = w["end"]
        if w["start"] > beat_s + tail_max:
            break
    end = min(max(end, beat_s + 6.0), beat_s + tail_max)
    if end - start < min_len:
        end = start + min_len
    if end - start > max_len:
        start = end - max_len
    return {"start_s": round(start, 2), "end_s": round(end, 2), "length_s": round(end - start, 1),
            "beat_s": round(beat_s, 2), "beat_at": round(beat_s - start, 1)}
