"""Audio-first probe: decide BEFORE downloading video.

v1 order:  chat spike -> download 60 s of 1080p (2.5-6 min) -> encode ->
           transcribe (small) -> transcribe again (tiny, for timing) ->
           discover the clip is muted / silent / a radio song -> drop.
v2 order:  chat spike -> download ~85 s of the audio_only stream (~3 MB,
           under a second) -> loudness + mute map + one transcript ->
           drop, or pick the best 62 s window and only then fetch video.

Everything the training reports learned to check by hand is measured
here, per candidate, in seconds:
  * copyright mute (-91 dB digital silence)          (found in evaluation)
  * < 10 s of speech = hard fail                     (found in evaluation)
  * dead air at the open ("first2s" from WORD times,
    never segment ranges)                            kai00_3060 (15 s of boat)
  * a car radio song as the whole clip               kai08_30430
  * scattered speech with a dead middle              (found in evaluation)
and the window is slid so the clip opens on speech instead of silence.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field

import numpy as np

SR = 16000
MUTE_DB = -60.0          # Twitch copyright mute is digital silence (~-91 dB)
FRAME_S = 0.1


def decode_pcm(path: str, start: float = 0.0, length: float | None = None) -> np.ndarray:
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-i", path]
    if length:
        cmd += ["-t", f"{length:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def frame_db(x: np.ndarray, frame_s: float = FRAME_S) -> np.ndarray:
    n = int(SR * frame_s)
    if len(x) < n:
        return np.array([-120.0])
    k = len(x) // n
    fr = x[: k * n].reshape(k, n)
    rms = np.sqrt((fr ** 2).mean(axis=1) + 1e-12)
    return 20 * np.log10(rms + 1e-12)


def write_wav(x: np.ndarray, path: str) -> None:
    import wave
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())


# --------------------------------------------------------------------------
# Transcription (one model instance per process; v1 reloaded per call)
# --------------------------------------------------------------------------
_MODELS: dict = {}


def model(name: str):
    if name not in _MODELS:
        os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
        from faster_whisper import WhisperModel
        _MODELS[name] = WhisperModel(name, device="cpu", compute_type="int8",
                                     cpu_threads=os.cpu_count() or 4)
    return _MODELS[name]


def transcribe(x: np.ndarray, model_name: str = "small", offset: float = 0.0) -> dict:
    """faster-whisper on a PCM array. Times are shifted by `offset` so they
    can be expressed in VOD seconds. Same JSON shape as v1 caption.py
    (segments[].words[].w/start/end) so caption_logic and HyperFrames keep
    working."""
    segs, info = model(model_name).transcribe(
        x, language="en", word_timestamps=True, vad_filter=True,
        condition_on_previous_text=False)
    out = []
    for s in segs:
        words = [{"w": w.word, "start": round(w.start + offset, 2),
                  "end": round(w.end + offset, 2), "p": round(w.probability, 2)}
                 for w in (s.words or [])]
        out.append({"start": round(s.start + offset, 2), "end": round(s.end + offset, 2),
                    "text": s.text.strip(), "words": words})
    return {"model": model_name, "language": info.language,
            "language_probability": round(info.language_probability, 2),
            "segments": out}


def all_words(tr: dict) -> list[dict]:
    return [w for s in tr["segments"] for w in s["words"]]


_HALLUCINATION = re.compile(
    r"^(thank you\.?|thanks for watching!?|you|bye\.?|\.+|♪+|\[music\]|music)$", re.I)


def real_words(tr: dict) -> list[dict]:
    """Drop Whisper's silence hallucinations ("Thank you." over nothing, ♪)
    and near-zero-confidence tokens."""
    ws = []
    for s in tr["segments"]:
        if _HALLUCINATION.match(s["text"].strip()):
            continue
        ws.extend(w for w in s["words"] if w.get("p", 1) >= 0.15)
    return ws


# --------------------------------------------------------------------------
# Probe
# --------------------------------------------------------------------------
@dataclass
class Probe:
    window_start: float
    window_end: float
    mean_db: float
    muted_frac: float                 # share of the window at digital silence
    words: list = field(default_factory=list)
    transcript: dict = field(default_factory=dict)
    music_suspect: bool = False
    music_note: str = ""
    loud_peak_s: float | None = None  # loudest second near the chat peak (scream/laugh)
    excitement_db: float = 0.0        # that second vs the window median
    mute_map: list = field(default_factory=list)   # per-second: True = digital silence
    repeated_lines: int = 0

    def muted_in(self, a: float, b: float) -> float:
        """Share of [a, b) that is copyright-muted (digital silence)."""
        if not self.mute_map:
            return 0.0
        i0 = max(0, int(a - self.window_start))
        i1 = min(len(self.mute_map), int(b - self.window_start))
        if i1 <= i0:
            return 0.0
        return sum(self.mute_map[i0:i1]) / (i1 - i0)


def probe_audio(audio_path: str, file_t0: float, win_start: float, win_end: float,
                peak_s: float, model_name: str = "small", transcript: dict | None = None) -> Probe:
    """Measure + transcribe [win_start, win_end] (VOD seconds) of an audio
    file whose first sample is VOD second file_t0. Pass `transcript` to
    reuse an earlier one (re-scoring without re-running Whisper)."""
    x = decode_pcm(audio_path, win_start - file_t0, win_end - win_start)
    db = frame_db(x)
    muted = float((db < MUTE_DB).mean()) if len(db) else 1.0
    active = db[db > MUTE_DB]
    mean_db = float(10 * np.log10(np.mean(10 ** (active / 10)))) if len(active) else -120.0
    p = Probe(win_start, win_end, round(mean_db, 1), round(muted, 2))
    sec_db = frame_db(x, 1.0)
    p.mute_map = [bool(v < MUTE_DB) for v in sec_db]
    if muted > 0.9:
        return p
    tr = transcript or transcribe(x, model_name, offset=win_start)
    p.transcript = tr
    p.words = real_words(tr)
    # excitement: loudest 1 s around the chat peak (moment is usually
    # 2-20 s before chat reacts) relative to the window's median second
    sec = frame_db(x, 1.0)
    if len(sec) > 5:
        med = float(np.median(sec[sec > MUTE_DB])) if (sec > MUTE_DB).any() else -60
        lo = max(0, int(peak_s - 25 - win_start))
        hi = min(len(sec), int(peak_s + 5 - win_start))
        if hi > lo:
            j = lo + int(np.argmax(sec[lo:hi]))
            p.loud_peak_s = win_start + j
            p.excitement_db = round(float(sec[j] - med), 1)
    # music / radio suspicion: lots of loud, steady audio with few words, or
    # Whisper reading lyrics (repeated lines, ♪)
    text = " ".join(s["text"] for s in tr["segments"])
    loud = float((sec > (np.median(sec) - 3)).mean()) if len(sec) else 0
    lines = [re.sub(r"[^a-z0-9 ]", "", s["text"].strip().lower()) for s in tr["segments"]
             if len(s["text"].split()) >= 2]
    repeated = len(lines) - len(set(lines))
    p.repeated_lines = repeated
    if "♪" in text or repeated >= 2:
        # a phrase read out 4x by a screen reader (text-to-speech) looked like speech
        # reading his desktop — a loop / TTS / lyrics, not the streamer
        p.music_suspect, p.music_note = True, (
            f"{repeated} repeated lines — lyrics, text-to-speech or a loop, not the streamer?")
    elif len(p.words) < 25 and loud > 0.8 and mean_db > -30:
        p.music_suspect, p.music_note = True, "steady loud audio with few words (radio / music bed?)"
    return p


# --------------------------------------------------------------------------
# Window choice
# --------------------------------------------------------------------------
RUN_GAP = 1.0   # words closer than this are one continuous run of speech


def speech_in(words: list[dict], a: float, b: float) -> tuple[int, float]:
    """(words, seconds of speech) in [a, b). Speech = continuous RUNS of
    words (gaps < 1 s joined), clipped to the window. Summing bare word
    durations under-counts (Whisper words are short, the pauses inside a
    sentence are speech); v1's segment spans over-count (one segment
    once spanned 4.6-29 s for a single line — kai00_2050)."""
    ws = [w for w in words if a <= w["start"] < b]
    if not ws:
        return 0, 0.0
    total, rs, re_ = 0.0, ws[0]["start"], ws[0]["end"]
    for w in ws[1:]:
        if w["start"] - re_ <= RUN_GAP:
            re_ = max(re_, w["end"])
        else:
            total += min(re_, b) - rs
            rs, re_ = w["start"], w["end"]
    total += min(re_, b) - rs
    return len(ws), total


def gaps(words: list[dict], a: float, b: float) -> tuple[float, float]:
    """(seconds before first word, longest silence inside) for [a, b)."""
    ws = [w for w in words if a <= w["start"] < b]
    if not ws:
        return b - a, b - a
    longest, prev = 0.0, ws[0]["end"]
    for w in ws[1:]:
        longest = max(longest, w["start"] - prev)
        prev = max(prev, w["end"])
    longest = max(longest, b - prev)
    return ws[0]["start"] - a, longest


def choose_window(p: Probe, peak_s: float, length: float = 62.0,
                  min_pre: float = 3.0, max_pre: float = 40.0) -> dict:
    """Pick the clip start in [peak-max_pre, peak-min_pre] that

      * opens on speech (penalise every second of silence before the first word),
      * keeps the most speech inside,
      * avoids a long dead middle,
      * keeps the chat peak inside the clip (the moment chat reacted to),
      * prefers starting at a sentence boundary (a Whisper segment start).

    Returns the chosen start/end and the measurements a human gate needs.
    """
    words = p.words
    seg_starts = sorted({round(s["start"], 1) for s in p.transcript.get("segments", [])})
    best = None
    lo, hi = peak_s - max_pre, peak_s - min_pre
    lo = max(lo, p.window_start)
    hi = min(hi, p.window_end - length)
    cands = [lo + 0.5 * i for i in range(int(max(0, hi - lo) / 0.5) + 1)] or [max(p.window_start, peak_s - 20)]
    for s in cands:
        e = s + length
        n, sp = speech_in(words, s, e)
        lead, mid = gaps(words, s, e)
        snap = min((abs(s - (x - 0.3)) for x in seg_starts), default=9)
        mute = p.muted_in(s, e) * length
        score = (sp - 2.5 * min(lead, 10) - 0.6 * max(0.0, mid - 8) - (0.8 if snap > 1.0 else 0)
                 - 1.5 * mute)
        if best is None or score > best[0]:
            best = (score, s, e, n, sp, lead, mid)
    _, s, e, n, sp, lead, mid = best
    # snap the start to just before the first word if that word is close
    first = next((w["start"] for w in words if s <= w["start"] < e), None)
    if first is not None and 0 < first - s <= 3.0 and first - 0.25 >= p.window_start:
        shift = first - 0.25 - s
        s, e = s + shift, e + shift
        n, sp = speech_in(words, s, e)
        lead, mid = gaps(words, s, e)
    return {"start_s": round(s, 2), "end_s": round(e, 2), "words": n,
            "speech_s": round(sp, 1), "first_word_at": round(lead, 1),
            "longest_gap": round(mid, 1), "muted_frac": round(p.muted_in(s, e), 2)}


_CTA = re.compile(
    r"\b(say|type|spam|drop|put|hit|smash|give me|let me see)\b[^.?!]{0,40}"
    r"\b(chat|in the chat|yes|no|w'?s|ws|l'?s|ones?|twos?|1|2|f|like button|follow)\b"
    r"|\bchat,? (is|are|should|do|did|would|have|has|was)\b[^.?!]{0,60}\?"
    r"|\b(yes or no|be honest)\b", re.I)


def asked_chat(words: list[dict], peak_s: float, before: float = 30.0) -> str | None:
    """Did the streamer ASK chat to type something right before the spike?
    Then the spike is obedience, not a reaction ("spam the W's in
    the chat", "first time seeing this? say yes")."""
    text = " ".join(w["w"].strip() for w in words if peak_s - before <= w["start"] <= peak_s + 2)
    m = _CTA.search(text)
    return m.group(0) if m else None


def assess(p: Probe, win: dict, peak_s: float | None = None) -> dict:
    """Pre-cut audio verdict (the parts the human gate used to measure by hand)."""
    flags, hard = [], None
    cta = asked_chat(p.words, peak_s) if peak_s is not None else None
    if cta:
        flags.append(f'chat was asked to respond ("{cta[:40]}") — spike may be a poll, not a reaction')
    muted = win.get("muted_frac", p.muted_frac)
    if muted > 0.5 or (p.muted_frac > 0.9 and not p.words):
        hard = f"copyright-muted ({int(max(muted, p.muted_frac) * 100)}% digital silence)"
    elif win["words"] < 12 or win["speech_s"] < 6:
        hard = f"only {win['words']} words / {win['speech_s']}s of speech — nothing to caption"
    if not hard and win["speech_s"] < 15:
        flags.append(f"thin speech ({win['speech_s']}s)")
    if muted > 0.05 and not hard:
        flags.append(f"partly muted ({int(muted * 100)}% of the clip)")
    if win["first_word_at"] > 2.0:
        flags.append(f"first word at {win['first_word_at']}s (dead open)")
    if win["longest_gap"] > 12:
        flags.append(f"{win['longest_gap']}s silent stretch")
    if p.music_suspect:
        flags.append("music? " + p.music_note)
    speech_f = min(1.25, max(0.2, win["speech_s"] / 30.0))
    open_f = 1.0 if win["first_word_at"] <= 2.0 else max(0.5, 1 - 0.05 * win["first_word_at"])
    gap_f = 1.0 if win["longest_gap"] <= 12 else max(0.5, 1 - 0.03 * (win["longest_gap"] - 12))
    music_f = (0.35 if p.repeated_lines >= 3 else 0.6) if p.music_suspect else 1.0
    mute_f = 1.0 - muted
    excite_f = 1.0 + min(0.25, max(0.0, p.excitement_db) / 40)
    cta_f = 0.5 if cta else 1.0
    factor = 0.0 if hard else speech_f * open_f * gap_f * music_f * excite_f * mute_f * cta_f
    # Floor (from review): with >= 20 s of real speech and no hard
    # fail, penalties can demote a moment but never bury it. B1 Darko — a
    # taste SHIP with a slow open and repeated in-game lines — scored 0.17,
    # below a DROP. In-game dialogue repeats too; "repeated lines" is a flag
    # for a human, not proof of a loop.
    if not hard and win["speech_s"] >= 20:
        factor = max(factor, 0.5)
    return {"audio_factor": round(factor, 3), "hard_fail": hard, "flags": flags}
