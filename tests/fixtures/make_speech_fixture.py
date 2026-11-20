#!/usr/bin/env python3
"""Builds speech_under_game.ogg (opus): synthetic speech (espeak-ng, no recorded voice) under a loud synthetic
"game" bed (engine rumble, gunfire bursts, a music pad) — nothing copyrighted. Not run by the tests; the .ogg
.ogg is committed. Needs: pip install espeakng-loader numpy, and a speech wav made with espeak-ng.

  python make_speech_fixture.py speech.wav speech_under_game.ogg
"""
import subprocess
import sys
import wave

import numpy as np

SR = 16000


def load(path: str) -> np.ndarray:
    w = wave.open(path)
    x = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
    n = int(len(x) * SR / w.getframerate())
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


def game_bed(n: int, rng: np.random.Generator) -> np.ndarray:
    t = np.arange(n) / SR
    rumble = np.cumsum(rng.normal(0, 1, n)).astype(np.float32)
    rumble -= np.convolve(rumble, np.ones(800) / 800, "same")
    rumble /= np.abs(rumble).max() + 1e-9
    engine = 0.6 * np.sign(np.sin(2 * np.pi * (70 + 15 * np.sin(2 * np.pi * 0.3 * t)) * t))
    pad = sum(0.25 * np.sin(2 * np.pi * f * t) for f in (220, 277.2, 329.6, 440))
    pad *= 0.6 + 0.4 * np.sin(2 * np.pi * 0.5 * t)
    shots = np.zeros(n, np.float32)
    for s in rng.uniform(0, n / SR - 0.3, 14):
        i = int(s * SR)
        k = np.arange(int(0.25 * SR))
        shots[i:i + len(k)] += (rng.normal(0, 1, len(k)) * np.exp(-k / (0.04 * SR)))[: n - i]
    return (0.5 * rumble + 0.35 * engine + 0.35 * pad + 0.9 * shots).astype(np.float32)


def main(speech_wav: str, out: str) -> None:
    rng = np.random.default_rng(7)
    sp = load(speech_wav)
    n = int(SR * (len(sp) / SR + 6))
    bed = game_bed(n, rng)
    x = bed / np.sqrt((bed ** 2).mean()) * 0.25            # game: loud
    s = np.zeros(n, np.float32)
    s[3 * SR: 3 * SR + len(sp)] = sp
    s = s / np.sqrt((sp ** 2).mean()) * 0.25 * 10 ** (-9 / 20)   # speech 9 dB under the game
    mix = np.clip(x + s, -1, 1)
    pcm = (mix * 32767).astype(np.int16).tobytes()
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "s16le", "-ar", str(SR), "-ac", "1", "-i", "-", "-c:a", "libopus", "-b:a", "32k", out],
                   input=pcm, check=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
