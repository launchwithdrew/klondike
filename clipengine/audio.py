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

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
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
        # Models are cached by setup; never re-check the hub on load — each
        # check hits the model CDN and fires a network-approval prompt.
        _MODELS[name] = WhisperModel(name, device="cpu", compute_type="int8",
                                     cpu_threads=os.cpu_count() or 4,
                                     local_files_only=True)
    return _MODELS[name]


# ---- vocabulary: names Whisper should spell right ---------------------------------------------------
GAME_TERMS = {   # by category (config/radar.json "game"); config/grading.json "vocabulary" can add more
    "Grand Theft Auto VI": ["GTA 6", "Jason", "Lucia", "Leonida", "Vice City", "Grassrivers", "Port Gellhorn",
                            "Ambrosia", "Mount Kalaga", "Leonida Keys", "Rockstar", "Cal Hampton", "Boobie Ike",
                            "Dre'Quan", "Real Dimez", "Brian Heder", "Raul Bautista"],
}
SLANG = ["bro", "chat", "W", "L", "rizz", "aura", "gyatt", "ohio", "sigma", "glazing", "crashout",
         "lock in", "no cap", "on God", "bussin", "deadass", "opps", "NPC", "speedrun", "goated"]


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def vocabulary() -> dict:
    """{"names": roster display names, "game": game terms, "slang": [...]} — defaults in code."""
    import json as _json
    names, game, slang = [], [], list(SLANG)
    try:
        cr = _json.load(open(os.path.join(REPO, "config", "creators.json"))).get("creators", {})
        names = sorted({c.get("name") for c in cr.values() if c.get("name")})
    except (OSError, ValueError):
        pass
    try:
        cat = _json.load(open(os.path.join(REPO, "config", "radar.json"))).get("game", "")
    except (OSError, ValueError):
        cat = ""
    game = list(GAME_TERMS.get(cat, []))
    try:
        extra = _json.load(open(os.path.join(REPO, "config", "grading.json"))).get("vocabulary") or {}
        game += [x for x in extra.get("game_terms", []) if x not in game]
        slang += [x for x in extra.get("slang", []) if x not in slang]
        names += [x for x in extra.get("names", []) if x not in names]
    except (OSError, ValueError):
        pass
    return {"names": names, "game": game, "slang": slang}


def creator_vocab(login: str | None) -> list[str]:
    if not login:
        return []
    import json
    try:
        with open(os.path.join(REPO, "config", "creators.json")) as stream:
            entries = json.load(stream).get("creators", {})
        terms = entries.get(login.casefold(), {}).get("vocab", [])
    except (OSError, ValueError):
        return []
    if not isinstance(terms, list):
        return []
    return list(dict.fromkeys(term.strip() for term in terms if isinstance(term, str) and term.strip()))


def vocab_prompt(max_chars: int = 600, login: str | None = None) -> str:
    """Whisper's initial prompt: spellings it should prefer (it biases, it does not force)."""
    v = vocabulary()
    text = ", ".join(dict.fromkeys(creator_vocab(login) + v["game"] + v["names"] + v["slang"]))
    return text[:max_chars].rsplit(",", 1)[0] + "." if len(text) > max_chars else text + "."


def misheard_names(words: list[dict], vocab: dict | None = None, cutoff: float = 0.72, *, login: str | None = None) -> list[tuple[str, str]]:
    """Capitalised words in a transcript that look like a known name but aren't it: [("Lucy a", "Lucia")].
    Shown on the draft so the operator can pass --fix "heard=>said"."""
    import difflib
    v = vocab or vocabulary()
    custom = creator_vocab(login)
    known = [n for n in custom + v["names"] + v["game"] if len(n) >= 4]
    custom_keys = {re.sub(r"[^a-z0-9 ]", "", n.lower()) for n in custom if len(n) >= 4}
    exact = {re.sub(r"[^a-z0-9 ]", "", n.lower()) for n in known}
    toks = [w["w"].strip() for w in words]
    out: list[tuple[str, str]] = []
    for i, t in enumerate(toks):
        core = re.sub(r"[^\w' ]", "", t)
        if not core or len(core) < 3 or (not core[0].isupper() and not custom_keys):
            continue
        if core.lower() in exact:
            continue                                   # said right ("Lucia a new haircut")
        nxt = re.sub(r"[^\w' ]", "", toks[i + 1]) if i + 1 < len(toks) else ""
        if nxt and (t.endswith((".", "?", "!", ",")) or not (custom_keys or nxt[0].isupper() or len(nxt) <= 2)):
            nxt = ""                                   # two-word names: "Vice Sitty", "Lucy a"
        for span in ((core + " " + nxt) if nxt else "", core):
            k = span.lower().strip()
            if not k:
                continue
            if k in exact or (" " not in k and any(k in e.split() for e in exact)):
                break
            choices = exact if core[0].isupper() else custom_keys
            m = difflib.get_close_matches(k, sorted(choices), n=1, cutoff=0.85 if len(k) <= 5 else cutoff)
            if m:
                right = next(n for n in known if re.sub(r"[^a-z0-9 ]", "", n.lower()) == m[0])
                if (span, right) not in out and span.lower() != right.lower():
                    out.append((span, right))
                break
    return out[:4]


def _prompt_echo(text: str, prompt: str) -> bool:
    """Whisper sometimes reads its prompt back over music/silence: 4+ prompt terms in a row."""
    terms = [t.strip().lower() for t in prompt.rstrip(".").split(",") if t.strip()]
    low = text.lower()
    return sum(1 for t in terms if len(t) > 3 and t in low) >= 4


# First pass: Silero VAD, gentler than faster-whisper's defaults (threshold 0.5, 2 s silence, 0.4 s pad),
# which cut a streamer talking under loud game audio. A clip that still comes back with too few words for
# how long it is loud gets a second pass without the filter (see needs_second_pass).
VAD_GENTLE = {"threshold": 0.3, "min_silence_duration_ms": 1000, "speech_pad_ms": 800}
LOUD_DB = -45.0              # a second louder than this is "loud" (game audio, voice, music — not silence)
SECOND_PASS_WPS = 0.3        # fewer real words than this per loud second → re-run without the VAD
SECOND_PASS_MIN_P = 0.5      # ... and keep that run only if its words are this confident on average


def _whisper(x: np.ndarray, model_name: str, offset: float, prompt: str | None, vad) -> dict:
    kw = {"vad_filter": vad is not False}
    if isinstance(vad, dict):
        kw["vad_parameters"] = vad
    segs, info = model(model_name).transcribe(
        x, language="en", word_timestamps=True, condition_on_previous_text=False,
        initial_prompt=prompt or None, **kw)
    out = []
    for s in segs:
        if prompt and _prompt_echo(s.text, prompt):
            continue
        words = [{"w": w.word, "start": round(w.start + offset, 2),
                  "end": round(w.end + offset, 2), "p": round(w.probability, 2)}
                 for w in (s.words or [])]
        out.append({"start": round(s.start + offset, 2), "end": round(s.end + offset, 2),
                    "text": s.text.strip(), "words": words})
    return {"model": model_name, "language": info.language,
            "language_probability": round(info.language_probability, 2),
            "segments": out}


def loud_seconds(x: np.ndarray) -> float:
    sec = frame_db(x, 1.0)
    return float((sec > LOUD_DB).sum()) if len(sec) else 0.0


def needs_second_pass(x: np.ndarray, tr: dict) -> bool:
    """Too few words for how long the clip is loud (or none): the VAD may have cut speech under game audio."""
    loud = loud_seconds(x)
    return loud >= 4 and len(real_words(tr)) < SECOND_PASS_WPS * loud


# what Whisper "hears" in noise without a VAD: YouTube outros
_OUTRO = re.compile(r"\b(subscrib\w*|like button|thanks? (you )?for watching|next video|see you (in the )?next)\b", re.I)


def better_pass(first: dict, second: dict) -> bool:
    """Keep the unfiltered run only if it found clearly more words, confidently (not hallucinated noise)."""
    a, b = real_words(first), real_words(second)
    if any(_OUTRO.search(s["text"]) for s in second["segments"]):
        return False                     # one hallucinated outro means the rest is likely noise too
    if len(b) < len(a) + 3:
        return False
    return float(np.mean([w.get("p", 1) for w in b])) >= SECOND_PASS_MIN_P


def transcribe(x: np.ndarray, model_name: str = "small", offset: float = 0.0, prompt: str | None = "") -> dict:
    """Transcription is heavy work: it waits its turn for a heavy-job slot (admission.py) so recording keeps
    the machine first."""
    from .admission import slot
    with slot("transcribe"):
        return _transcribe(x, model_name, offset, prompt)


def _transcribe(x: np.ndarray, model_name: str = "small", offset: float = 0.0, prompt: str | None = "") -> dict:
    """faster-whisper on a PCM array. Times are shifted by `offset` so they
    can be expressed in VOD seconds. Same JSON shape as v1 caption.py
    (segments[].words[].w/start/end) so caption_logic and HyperFrames keep
    working. `pass` says which run the words came from: "vad" or "no-vad" (speech the filter cut)."""
    if prompt == "":
        prompt = vocab_prompt()
    tr = _whisper(x, model_name, offset, prompt, VAD_GENTLE)
    tr["pass"] = "vad"
    if needs_second_pass(x, tr):
        tr2 = _whisper(x, model_name, offset, prompt, False)
        if better_pass(tr, tr2):
            tr2["pass"] = "no-vad"
            tr2["first_pass_words"] = len(real_words(tr))
            return tr2
    return tr


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
    observation: str = "ok"          # ok | muted | payoff muted | no audio | flat audio — why ASR ran or didn't
    cached: bool = False             # the observation came from the probe cache (nothing fetched or decoded)

    def muted_in(self, a: float, b: float) -> float:
        """Share of [a, b) that is copyright-muted (digital silence)."""
        if not self.mute_map:
            return 0.0
        i0 = max(0, int(a - self.window_start))
        i1 = min(len(self.mute_map), int(b - self.window_start))
        if i1 <= i0:
            return 0.0
        return sum(self.mute_map[i0:i1]) / (i1 - i0)


PAYOFF_MUTE_SKIP = 0.8          # this much of the beat region copyright-muted: the payoff can't be heard


SKIP_OBS = ("no audio", "muted", "payoff muted")      # nothing to hear where the moment is: no Whisper
PROBE_CACHE_DAYS = 14                                 # a window found unhearable stays so (VOD mutes are permanent)


def skip_reason(obs: str) -> str | None:
    """The one-line refusal every route gives for an unhearable window (a rights review, not a crash)."""
    return {"payoff muted": "copyright-muted over the payoff (rights review: the beat can't be heard; not transcribed)",
            "muted": "copyright-muted (the whole window is digital silence; not transcribed)",
            "no audio": "no audio in the source window (missing or corrupt)"}.get(obs)


def early_observation(p: "Probe", peak_s: float, sec_db, no_audio: bool,
                      region: tuple[float, float] | None = None) -> str:
    """Before any transcription: is there anything to hear where the moment is? The beat region is the
    25 s before chat peaks to 5 s after (the moment usually comes 2–20 s before chat reacts); a route that
    already knows the payoff passes its own `region`."""
    if no_audio:
        return "no audio"
    if p.muted_frac > 0.9:
        return "muted"
    lo, hi = region or (peak_s - 25, peak_s + 5)
    if p.muted_in(lo, hi) >= PAYOFF_MUTE_SKIP:
        return "payoff muted"
    live = [v for v in sec_db if v > MUTE_DB]
    if len(live) >= 5 and float(np.std(live)) < 0.75:
        return "flat audio"                           # a constant bed (replacement tone / noise): transcribed, flagged
    return "ok"


def log_probe(p: "Probe", seconds: float, skipped: bool, route: str = "vod", cached: bool = False) -> None:
    """Count what the probe saved: each window it stopped before Whisper (runs/heavy/probes.jsonl)."""
    try:
        from .admission import _dir
        d = _dir()
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "probes.jsonl"), "a") as fh:
            fh.write(json.dumps({"at": time.time(), "route": route, "obs": p.observation, "skipped_asr": skipped,
                                 "cached": cached, "asr_s_saved": round(seconds, 1) if skipped else 0.0}) + "\n")
    except OSError:
        pass


# Probe cache: a window already found unhearable is never fetched, decoded or transcribed again (sweeps,
# catch-up retries, pre-render and publish revisit the same moments). Keyed by the media's identity — a VOD id
# and window, or the decoded audio's own hash — in runs/heavy/probe_cache.jsonl (append-only, last line wins).
def _cache_file() -> str:
    from .admission import _dir
    return os.path.join(_dir(), "probe_cache.jsonl")


def media_key(x: np.ndarray, region: tuple[float, float]) -> str:
    """Fingerprint of decoded audio + the region judged (the same sound judged the same way)."""
    h = hashlib.sha1(np.ascontiguousarray(x, dtype=np.float32).tobytes())
    h.update(f"{region[0]:.1f}:{region[1]:.1f}".encode())
    return "pcm:" + h.hexdigest()


def cache_get(key: str | None) -> dict | None:
    if not key:
        return None
    hit, cutoff = None, time.time() - PROBE_CACHE_DAYS * 86400
    try:
        with open(_cache_file()) as fh:
            for line in fh:
                if key in line:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("k") == key and r.get("at", 0) >= cutoff:
                        hit = r
    except OSError:
        return None
    return hit


def cache_put(key: str | None, p: "Probe") -> None:
    if not key or p.observation not in SKIP_OBS:
        return
    try:
        f = _cache_file()
        os.makedirs(os.path.dirname(f), exist_ok=True)
        with open(f, "a") as fh:
            fh.write(json.dumps({"k": key, "at": time.time(), "obs": p.observation, "muted_frac": p.muted_frac,
                                 "mean_db": p.mean_db}) + "\n")
    except OSError:
        pass


def from_cache(key: str | None, win_start: float, win_end: float, route: str = "vod") -> "Probe | None":
    """A Probe straight from the cache when this window is already known to be unhearable (no fetch, no decode)."""
    r = cache_get(key)
    if not r or r.get("obs") not in SKIP_OBS:
        return None
    p = Probe(win_start, win_end, r.get("mean_db", -120.0), r.get("muted_frac", 1.0))
    p.observation, p.cached = r["obs"], True
    log_probe(p, win_end - win_start, skipped=True, route=route, cached=True)
    return p


def measure(x: np.ndarray, win_start: float, win_end: float, peak_s: float,
            region: tuple[float, float] | None = None) -> tuple["Probe", np.ndarray]:
    """Levels, the per-second mute map and the early observation of decoded audio — no transcription."""
    db = frame_db(x) if len(x) else np.zeros(0)
    muted = float((db < MUTE_DB).mean()) if len(db) else 1.0
    active = db[db > MUTE_DB]
    mean_db = float(10 * np.log10(np.mean(10 ** (active / 10)))) if len(active) else -120.0
    p = Probe(win_start, win_end, round(mean_db, 1), round(muted, 2))
    sec_db = frame_db(x, 1.0) if len(x) else np.zeros(0)
    p.mute_map = [bool(v < MUTE_DB) for v in sec_db]
    p.observation = early_observation(p, peak_s, sec_db, len(x) == 0, region)
    return p, sec_db


def gate(x: np.ndarray, t0: float, payoff: float | None = None, route: str = "live",
         region: tuple[float, float] | None = None) -> "Probe":
    """The same early probe for routes that already hold the audio (live buffer, re-cut, pre-render, publish):
    x starts at stream time t0. The beat region is around the known payoff (10 s before to 5 s after), else
    the whole window. Check `.observation in SKIP_OBS` before transcribing; `skip_reason()` says why."""
    end = t0 + len(x) / SR
    if region is None:
        region = (payoff - 10, payoff + 5) if payoff is not None else (t0, end)
    key = media_key(x, region) if len(x) else None
    hit = from_cache(key, t0, end, route)
    if hit:
        return hit
    p, _ = measure(x, t0, end, region[1] - 5, region)
    log_probe(p, end - t0, skipped=p.observation in SKIP_OBS, route=route)
    cache_put(key, p)
    return p


def probe_audio(audio_path: str, file_t0: float, win_start: float, win_end: float,
                peak_s: float, model_name: str = "small", transcript: dict | None = None,
                login: str | None = None, key: str | None = None) -> Probe:
    """Measure + transcribe [win_start, win_end] (VOD seconds) of an audio
    file whose first sample is VOD second file_t0. Pass `transcript` to
    reuse an earlier one (re-scoring without re-running Whisper). `key` names
    the media (e.g. a VOD id + window) for the probe cache."""
    hit = from_cache(key, win_start, win_end)
    if hit:
        return hit
    try:
        x = decode_pcm(audio_path, win_start - file_t0, win_end - win_start)
    except Exception:                                    # missing / corrupt audio: an observation, not a crash
        x = np.zeros(0, dtype=np.float32)
    p, sec_db = measure(x, win_start, win_end, peak_s)
    if p.observation in SKIP_OBS:
        log_probe(p, win_end - win_start, skipped=True)
        if p.observation != "no audio":                  # a failed download may succeed next time: never cached
            cache_put(key, p)
        return p
    log_probe(p, win_end - win_start, skipped=False)
    tr = transcript or transcribe(x, model_name, offset=win_start, prompt=vocab_prompt(login=login))
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
    obs = getattr(p, "observation", "ok")
    if not hard and obs in ("payoff muted", "no audio"):
        hard = skip_reason(obs)
    if obs == "flat audio":
        flags.append("flat audio (a constant bed — replaced or muted sound?)")
    elif not hard and (win["words"] < 12 or win["speech_s"] < 6):     # never overwrites a mute / no-audio reason
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


# --------------------------------------------------------------------------
# Reactions: what the streamer DID when there is little to read
# --------------------------------------------------------------------------
_LAUGH = re.compile(r"\b(ha(ha)+|he(he)+|lol|lmao+|(laugh\w*))\b|\[laugh|\(laugh", re.I)


def reaction(x: np.ndarray, t0: float, words: list[dict], at: float, before: float = 6.0,
             after: float = 6.0) -> dict:
    """{"score": 0–1, "why": "...", "onset": time | None} around `at` (stream time; x starts at t0):
    a volume burst (loudest second vs the clip's median) and laughter in the streamer's words."""
    sec = frame_db(x, 1.0)
    vol, onset = 0.0, None
    act = sec[sec > MUTE_DB]
    if len(act) > 5:
        med = float(np.median(act))
        lo, hi = max(0, int(at - before - t0)), min(len(sec), int(at + after - t0))
        if hi > lo:
            j = lo + int(np.argmax(sec[lo:hi]))
            jump = float(sec[j] - med)
            vol = max(0.0, min(1.0, (jump - 4.0) / 8.0))
            # onset: the first second of the burst (louder than the one before by 4 dB+)
            k = j
            while k > lo and sec[k - 1] > sec[j] - 4:
                k -= 1
            onset = t0 + k
    near = [w for w in words if at - before <= w["start"] <= at + after and w.get("spk", "streamer") == "streamer"]
    # loud is only the streamer's reaction when the streamer is the one making noise (a cutscene's
    # gunshot or score is loud too): their voice within a second of the burst, when speakers are tagged
    if vol and onset is not None and any("spk" in w for w in words) and \
            not any(onset - 1.0 <= w["start"] <= onset + 2.0 for w in near):
        vol = 0.0
    n_laugh = sum(1 for w in near if _LAUGH.search(w["w"]))
    laugh = 0.0 if not n_laugh else (1.0 if n_laugh >= 2 else 0.6)
    why = []
    if vol >= 0.25:
        why.append(f"volume +{4 + vol * 8:.0f} dB")
    if laugh:
        why.append("laughing")
    return {"score": round(max(vol, laugh), 2), "why": ", ".join(why), "onset": onset}
