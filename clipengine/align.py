"""Where a viewer clip really is in our recording: match the clip's audio against the recording's.

A viewer clip and our recording come from the same encode, so their waveforms line up: the true place correlates
around 0.8-1.0 and every other place stays under ~0.1 (U2, Nov 24, eval/eval_clip_offset.py — 12 of 15 clips matched,
the other 3 fell on stretches Twitch muted). That settles a clip's place to a fraction of a second, where its
saved-stream position is good to a few seconds and its createdAt is 5-150 s after the moment.

Costs one small download (the clip's lowest rendition, ~1-3 MB) and well under a second of numpy. Any failure — the
clip host unreachable, a muted stretch, a clip already deleted — returns None and the caller keeps its estimate.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import urllib.parse

from . import twitch as T

SR = 4000            # Hz: enough to line waveforms up, small enough to stay cheap
MIN_CORR = 0.3       # a real match is 0.8+; noise stays under ~0.1
MIN_CLIP_S = 3.0

_CLIP_SRC = """query($slug: ID!) { clip(slug: $slug) {
  playbackAccessToken(params: {platform: "web", playerBackend: "mediaplayer", playerType: "site"}) { signature value }
  videoQualities { quality sourceURL } } }"""


def clip_source(slug: str) -> str:
    """A signed URL for the clip's lowest rendition (audio is the same in every rendition)."""
    c = (T.gql(_CLIP_SRC, {"slug": slug}, playback=True) or {}).get("clip") or {}
    qs = [q for q in c.get("videoQualities") or [] if q.get("sourceURL")]
    tok = c.get("playbackAccessToken") or {}
    if not qs or not tok.get("value"):
        raise RuntimeError("clip has no playable source (deleted, or the clip list is stale)")
    q = min(qs, key=lambda q: int(q.get("quality") or 0) if str(q.get("quality") or "").isdigit() else 9999)
    return q["sourceURL"] + "?" + urllib.parse.urlencode({"sig": tok.get("signature", ""), "token": tok["value"]})


def pcm(path: str):
    """Mono float32 samples at SR."""
    import numpy as np
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
                       capture_output=True)
    return np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32)


def locate(clip, ref) -> tuple[float, float] | None:
    """(where the clip starts inside ref in seconds, normalised correlation 0-1) or None (pure).
    Places where ref is near-silent (a muted stretch) are skipped: their tiny energy would inflate noise."""
    import numpy as np
    a = clip - clip.mean()
    if len(a) < SR * MIN_CLIP_S or len(ref) <= len(a):
        return None
    ea = float((a ** 2).sum())
    if ea <= 0:
        return None
    b = ref.astype(np.float64)
    n = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    c = np.fft.irfft(np.fft.rfft(b, n) * np.conj(np.fft.rfft(a, n)), n)[: len(b) - len(a) + 1]
    cs = np.concatenate([[0.0], np.cumsum(b ** 2)])
    win = cs[len(a):] - cs[: len(b) - len(a) + 1]
    c = c / (np.sqrt(np.maximum(win, 1e-9)) * np.sqrt(ea))
    c[win < 0.05 * ea] = 0.0
    i = int(np.argmax(np.abs(c)))
    return i / SR, float(abs(c[i]))


def _decode_run(rec_dir: str, run: list[dict], tmp: str):
    """One unbroken run of segments decoded as one stream (seamless: decoding segments one by one shifts each by the
    codec's start-up delay, which blurs a waveform match)."""
    lst = os.path.join(tmp, "run.txt")
    with open(lst, "w") as fh:
        for r in run:
            fh.write("file '" + os.path.abspath(os.path.join(rec_dir, "segments", r["f"])).replace("'", "'\\''") + "'\n")
    p = subprocess.run(["ffmpeg", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst, "-vn", "-ac", "1",
                        "-ar", str(SR), "-f", "s16le", "-"], capture_output=True)
    import numpy as np
    return np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32)


def recording_audio(rec_dir: str, lo: float, hi: float):
    """(wall-clock time of sample 0, mono samples at SR) for [lo, hi] of a recording, every sample at its wall-clock
    place. Unbroken runs of segments decode as one stream; a run that comes back short (a segment that won't decode —
    Twitch's muted saved-stream stretches carry another audio layout — or a damaged download) is decoded segment by
    segment instead, so the bad one leaves silence where it was instead of sliding everything after it earlier.
    Holes stay silent."""
    import numpy as np
    from . import live as L
    idx = [r for r in L.load_index(rec_dir) if r["t"] + r["d"] > lo and r["t"] < hi]
    if not idx:
        raise RuntimeError("that stretch is not in the recording")
    t0 = idx[0]["t"]
    out = np.zeros(int((idx[-1]["t"] + idx[-1]["d"] - t0) * SR) + SR, dtype=np.float32)
    runs: list[list[dict]] = []
    for r in idx:
        if runs and abs(r["t"] - (runs[-1][-1]["t"] + runs[-1][-1]["d"])) <= 0.5:
            runs[-1].append(r)
        else:
            runs.append([r])

    def lay(t: float, x) -> None:
        i = int(round((t - t0) * SR))
        if i < len(out):
            out[i: i + len(x)] = x[: len(out) - i]
    with tempfile.TemporaryDirectory() as tmp:
        for run in runs:
            want = sum(r["d"] for r in run)
            x = _decode_run(rec_dir, run, tmp)
            if abs(len(x) / SR - want) <= 1.0:
                lay(run[0]["t"], x)
                continue
            for r in run:                              # something in this run won't decode: place each segment alone
                lay(r["t"], pcm(os.path.join(rec_dir, "segments", r["f"]))[: int(r["d"] * SR) + SR // 10])
    return t0, out


def in_recording(rec_dir: str, slug: str, lo: float, hi: float) -> dict | None:
    """The clip's place in our recording, searched within wall-clock [lo, hi]:
    {"start", "end", "corr"} (wall clock) or None when it can't be matched with confidence."""
    with tempfile.TemporaryDirectory() as tmp:
        clip_mp4 = os.path.join(tmp, "clip.mp4")
        try:
            first, ref = recording_audio(rec_dir, lo, hi)
            with open(clip_mp4, "wb") as fh:
                fh.write(T.http(clip_source(slug), attempts=2, timeout=30))
            clip = pcm(clip_mp4)
        except Exception:
            return None
        m = locate(clip, ref)
        if not m or m[1] < MIN_CORR:
            return None
        start = first + m[0]
        return {"start": round(start, 2), "end": round(start + len(clip) / SR, 2), "corr": round(m[1], 2)}
