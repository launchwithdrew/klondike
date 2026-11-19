"""Speaker separation — who is talking: the streamer, someone else on stream, or the game.

Launch night scored GTA VI's own mission dialogue as the streamer talking (a boat-party mission and a
cops cutscene drafted at 85–92): Whisper hears speech, and speech density was the audio signal. This
module tags every transcribed word with `spk`:

  streamer  the voice present throughout the session (the person whose mic it is)
  other     a second voice that keeps coming back (a guest, a co-streamer on Discord)
  game      everything else that talks: cutscenes, mission dialogue, NPCs, radio hosts, TV

How:
  1. a speaker embedding per speech turn — a small CPU model (WeSpeaker ResNet34, ONNX, 26 MB,
     ~30 ms per 3 s on a laptop CPU; downloaded once by setup.sh). Without it, log-mel statistics
     (weaker, but needs nothing) — `backend()` says which ran.
  2. the session's voices: turns sampled across the whole session (a sentinel's recording, or
     every candidate of a VOD scan), clustered by cosine similarity. The streamer is the cluster
     present in the most parts of the session — game voices come in bursts (one mission, one
     cutscene), the streamer's mic is there all the time.
  3. hints for the game: the vision timeline (cutscene editing, on-screen subtitles at those
     seconds). A turn that doesn't sound like the streamer and sits in a cutscene is game.

Scoring uses STREAMER speech only (hook, speech density, "said"); a moment whose speech is mostly
game dialogue gets the flag "game dialogue" (grade.py caps it unless viewers or the streamer's own
reaction carry it).
"""
from __future__ import annotations

import json
import os
import time

import numpy as np

SR = 16000
STREAMER, OTHER, GAME = "streamer", "other", "game"
MODEL_REPO = "Wespeaker/wespeaker-voxceleb-resnet34-LM"
MODEL_FILE = "voxceleb_resnet34_LM.onnx"
# cosine similarity at which two turns count as the same voice, per backend
SAME = {"wespeaker": 0.42, "stats": 0.93}
TURN_GAP_S = 0.6          # words further apart than this start a new turn
TURN_MAX_S = 6.0
MIN_EMBED_S = 1.0         # shorter turns are embedded with a little audio either side
PROFILE_MAX_AGE_S = 600.0  # a sentinel's voice profile is rebuilt every 10 min (the session grows)
GAME_SHARE_FLAG = 0.4     # this share of the speech in a clip is game → "game dialogue" (launch week: game drafts 46–55%, streamer drafts ≤ 13%)

_SESS: dict = {}


# ---- embeddings ----------------------------------------------------------------------------------

def _wespeaker():
    if "ws" in _SESS:
        return _SESS["ws"]
    sess = None
    if os.environ.get("CLIPENGINE_SPEAKER", "") != "stats":
        try:
            import onnxruntime as ort
            from huggingface_hub import hf_hub_download
            os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
            try:
                path = hf_hub_download(MODEL_REPO, MODEL_FILE, local_files_only=True)
            except Exception:
                path = hf_hub_download(MODEL_REPO, MODEL_FILE)
            so = ort.SessionOptions()
            so.intra_op_num_threads = max(1, min(4, os.cpu_count() or 2))
            sess = ort.InferenceSession(path, so, providers=["CPUExecutionProvider"])
        except Exception as exc:                       # offline / not installed: the stats backend still works
            print(f"[speaker] embedding model unavailable ({str(exc)[:80]}) — using log-mel statistics", flush=True)
            sess = None
    _SESS["ws"] = sess
    return sess


def backend() -> str:
    return "wespeaker" if _wespeaker() is not None else "stats"


def _mel_banks(n_fft: int = 512, n_mels: int = 80, lo: float = 20.0, hi: float = SR / 2) -> np.ndarray:
    mel = lambda f: 1127.0 * np.log(1 + np.asarray(f) / 700.0)
    m_lo, m_hi = mel(lo), mel(hi)
    centers = np.linspace(m_lo, m_hi, n_mels + 2)
    freqs = mel(np.arange(n_fft // 2 + 1) * SR / n_fft)
    banks = np.zeros((n_mels, n_fft // 2 + 1), np.float32)
    for i in range(n_mels):
        l, c, r = centers[i], centers[i + 1], centers[i + 2]
        up = (freqs - l) / (c - l)
        down = (r - freqs) / (r - c)
        banks[i] = np.maximum(0, np.minimum(up, down))
    return banks


def fbank(x: np.ndarray) -> np.ndarray:
    """Kaldi-style 80-bin log-mel filterbank (25 ms / 10 ms, Hamming, pre-emphasis), mean-normalised —
    what the WeSpeaker model was trained on. x: float PCM at 16 kHz in [-1, 1]."""
    x = np.asarray(x, np.float32) * 32768.0
    fl, fs, n_fft = 400, 160, 512
    if len(x) < fl:
        x = np.pad(x, (0, fl - len(x)))
    n = 1 + (len(x) - fl) // fs
    idx = np.arange(fl)[None, :] + fs * np.arange(n)[:, None]
    fr = x[idx]
    fr = fr - fr.mean(axis=1, keepdims=True)
    fr = np.concatenate([fr[:, :1] * (1 - 0.97), fr[:, 1:] - 0.97 * fr[:, :-1]], axis=1)
    fr = fr * np.hamming(fl).astype(np.float32)
    spec = np.abs(np.fft.rfft(fr, n_fft)) ** 2
    if "banks" not in _SESS:
        _SESS["banks"] = _mel_banks(n_fft)
    feats = np.log(np.maximum(spec @ _SESS["banks"].T, 1.1920929e-07)).astype(np.float32)
    return feats - feats.mean(axis=0, keepdims=True)


def embed(x: np.ndarray) -> np.ndarray | None:
    """One L2-normalised voice vector for a stretch of speech (>= ~0.5 s), or None if too short."""
    if x is None or len(x) < SR // 2:
        return None
    feats = fbank(x)
    sess = _wespeaker()
    if sess is not None:
        v = sess.run(None, {sess.get_inputs()[0].name: feats[None, :, :]})[0][0]
    else:                                   # stats backend: mean + spread of the log-mel spectrum
        raw = feats + 0.0
        v = np.concatenate([raw.std(axis=0), np.percentile(raw, 90, axis=0) - np.percentile(raw, 10, axis=0)])
        v = v - v.mean()
    v = np.asarray(v, np.float32)
    nrm = float(np.linalg.norm(v))
    return v / nrm if nrm > 0 else None


def same_threshold() -> float:
    return SAME[backend()]


# ---- turns ----------------------------------------------------------------------------------------

def turns(words: list[dict], gap: float = TURN_GAP_S, max_len: float = TURN_MAX_S) -> list[dict]:
    """Group words into speech turns: [{start, end, idx: [word indices]}]. A pause or a long run splits."""
    out: list[dict] = []
    for i, w in enumerate(words):
        if out and w["start"] - out[-1]["end"] <= gap and w["end"] - out[-1]["start"] <= max_len:
            out[-1]["end"] = max(out[-1]["end"], w["end"])
            out[-1]["idx"].append(i)
        else:
            out.append({"start": w["start"], "end": w["end"], "idx": [i]})
    return out


def embed_turns(pcm: np.ndarray, t0: float, ts: list[dict]) -> list[np.ndarray | None]:
    """Embed each turn from `pcm` (whose first sample is time t0)."""
    out = []
    for t in ts:
        a, b = t["start"] - 0.1, t["end"] + 0.1
        if b - a < MIN_EMBED_S:
            pad = (MIN_EMBED_S - (b - a)) / 2
            a, b = a - pad, b + pad
        i0, i1 = max(0, int((a - t0) * SR)), min(len(pcm), int((b - t0) * SR))
        out.append(embed(pcm[i0:i1]) if i1 - i0 >= SR // 2 else None)
    return out


# ---- the session's voices ------------------------------------------------------------------------

def cluster(embs: list[np.ndarray], thr: float) -> list[int]:
    """Average-linkage agglomerative clustering on cosine similarity -> a label per embedding."""
    n = len(embs)
    if n == 0:
        return []
    X = np.stack(embs)
    S = X @ X.T
    groups = [[i] for i in range(n)]
    while len(groups) > 1:
        best, bi, bj = -2.0, -1, -1
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                s = float(S[np.ix_(groups[i], groups[j])].mean())
                if s > best:
                    best, bi, bj = s, i, j
        if best < thr:
            break
        groups[bi] += groups.pop(bj)
    labels = [0] * n
    for k, g in enumerate(groups):
        for i in g:
            labels[i] = k
    return labels


def profile_from_chunks(chunks: list[dict], bins: int = 6) -> dict | None:
    """chunks: [{"t": time, "emb": vec, "game": bool hint}] sampled across a session.
    -> {"streamer": centroid, "others": [centroids], "coverage": ..., "n": ...} or None (too little)."""
    chunks = [c for c in chunks if c.get("emb") is not None]
    if len(chunks) < 6:
        return None
    thr = same_threshold()
    labels = cluster([c["emb"] for c in chunks], thr)
    t_lo = min(c["t"] for c in chunks)
    t_hi = max(c["t"] for c in chunks)
    span = max(1e-6, t_hi - t_lo)
    stats = {}
    for c, k in zip(chunks, labels):
        b = min(bins - 1, int((c["t"] - t_lo) / span * bins))
        s = stats.setdefault(k, {"bins": set(), "n": 0, "game": 0, "embs": []})
        s["bins"].add(b); s["n"] += 1; s["game"] += bool(c.get("game")); s["embs"].append(c["emb"])
    # the streamer: present in the most parts of the session, and not mostly in cutscenes
    order = sorted(stats.items(), key=lambda kv: (len(kv[1]["bins"]) - 2 * (kv[1]["game"] > kv[1]["n"] / 2),
                                                  kv[1]["n"]), reverse=True)
    k0, s0 = order[0]
    if len(s0["bins"]) < min(3, bins) and span > 120:
        return None                                     # no voice runs through the session: don't guess
    cen = lambda es: (lambda m: m / (np.linalg.norm(m) or 1))(np.mean(np.stack(es), axis=0))
    others = [cen(s["embs"]) for k, s in order[1:]
              if len(s["bins"]) >= 3 and s["n"] >= 4 and s["game"] <= s["n"] / 2]
    return {"streamer": cen(s0["embs"]), "others": others, "backend": backend(), "thr": thr,
            "coverage": round(len(s0["bins"]) / bins, 2), "share": round(s0["n"] / len(chunks), 2),
            "n": len(chunks), "clusters": len(stats)}


def profile_to_json(p: dict | None) -> dict | None:
    if not p:
        return None
    return {**{k: v for k, v in p.items() if k not in ("streamer", "others")},
            "streamer": [round(float(x), 5) for x in p["streamer"]],
            "others": [[round(float(x), 5) for x in o] for o in p["others"]]}


def profile_from_json(d: dict | None) -> dict | None:
    if not d or d.get("backend") != backend():
        return None
    return {**d, "streamer": np.asarray(d["streamer"], np.float32),
            "others": [np.asarray(o, np.float32) for o in d.get("others", [])]}


def speech_chunks(pcm: np.ndarray, t0: float, max_s: float = 3.0, min_s: float = 1.0) -> list[tuple[float, float]]:
    """Speech regions (Silero VAD, shipped with faster-whisper) cut into <= max_s pieces."""
    try:
        from faster_whisper.vad import VadOptions, get_speech_timestamps
        ts = get_speech_timestamps(pcm, VadOptions(min_silence_duration_ms=300))
        regions = [(t0 + r["start"] / SR, t0 + r["end"] / SR) for r in ts]
    except Exception:
        regions = [(t0, t0 + len(pcm) / SR)]
    out = []
    for a, b in regions:
        while b - a >= min_s:
            out.append((a, min(b, a + max_s)))
            a += max_s
    return out


def recording_profile(rec_dir: str, spots: int = 24, spot_s: float = 8.0, force: bool = False,
                      hints: bool = True) -> dict | None:
    """The streamer's voice for a sentinel recording: speech sampled at `spots` points across what is
    recorded, clustered. Cached in <rec>/voice.json and rebuilt every PROFILE_MAX_AGE_S."""
    from . import audio as A
    from . import live as L
    cache = os.path.join(rec_dir, "voice.json")
    if not force and os.path.exists(cache) and time.time() - os.path.getmtime(cache) < PROFILE_MAX_AGE_S:
        try:
            p = profile_from_json(json.load(open(cache)))
            if p:
                return p
        except (OSError, ValueError):
            pass
    idx = L.load_index(rec_dir)
    if len(idx) < 30:                                   # under ~1 min recorded
        return None
    t_lo, t_hi = idx[0]["t"], idx[-1]["t"] + idx[-1]["d"] - spot_s
    chunks = []
    import threading
    tmp = os.path.join(rec_dir, f"_voice_spot_{os.getpid()}_{threading.get_ident()}.ts")
    for k in range(spots):
        a = t_lo + (t_hi - t_lo) * (k + 0.5) / spots
        try:
            first = L.cut_source(rec_dir, a, a + spot_s, tmp)
            x = A.decode_pcm(tmp, 0, None)
        except Exception:
            continue
        game_hint = None
        if hints:                       # a recurring game voice (a protagonist) is not a guest: subtitles say so
            from . import vision as VI
            try:
                game_hint = VI.game_hint_fn(VI.timeline(tmp), first)
            except Exception:
                game_hint = None
        for s, e in speech_chunks(x, first):
            v = embed(x[max(0, int((s - first) * SR)):int((e - first) * SR)])
            if v is not None:
                chunks.append({"t": s, "emb": v, "game": bool(game_hint and game_hint((s + e) / 2))})
    if os.path.exists(tmp):
        os.remove(tmp)
    p = profile_from_chunks(chunks)
    if p:
        json.dump(profile_to_json(p), open(cache, "w"))
    return p


# ---- tagging --------------------------------------------------------------------------------------

def classify(v: np.ndarray | None, prof: dict | None, game_hint: bool = False) -> tuple[str, float]:
    """(speaker, similarity to the streamer) for one turn."""
    if prof is None or v is None:
        return (GAME if game_hint else STREAMER), 0.0      # nothing to compare: only the hints speak
    thr = prof.get("thr", same_threshold())
    s = float(v @ prof["streamer"])
    # in a cutscene the streamer has to sound clearly like themself (they often talk over it)
    if s >= thr + (0.03 if game_hint else 0.0):
        return STREAMER, s
    if not game_hint and any(float(v @ o) >= thr for o in prof.get("others", [])):
        return OTHER, s
    return GAME, s


def tag_words(words: list[dict], pcm: np.ndarray, t0: float, prof: dict | None,
              game_at=None) -> dict:
    """Tag each word in place with "spk" (+ "sim"). game_at(t) -> bool: vision says cutscene/subtitles.
    Returns the summary scoring and the queue need."""
    ts = turns(words)
    embs = embed_turns(pcm, t0, ts) if prof is not None else [None] * len(ts)
    for t, v in zip(ts, embs):
        mid = (t["start"] + t["end"]) / 2
        spk, sim = classify(v, prof, bool(game_at and game_at(mid)))
        for i in t["idx"]:
            words[i]["spk"], words[i]["sim"] = spk, round(sim, 2)
    return summary(words, prof)


def summary(words: list[dict], prof: dict | None = None) -> dict:
    from .audio import speech_in
    by = {STREAMER: [], OTHER: [], GAME: []}
    for w in words:
        by[w.get("spk", STREAMER)].append(w)
    secs = {k: (speech_in(v, -1e18, 1e18)[1] if v else 0.0) for k, v in by.items()}
    total = sum(secs.values())
    game_share = secs[GAME] / total if total > 0 else 0.0
    return {"streamer_s": round(float(secs[STREAMER]), 1), "other_s": round(float(secs[OTHER]), 1), "game_s": round(float(secs[GAME]), 1),
            "game_share": round(float(game_share), 2), "profiled": prof is not None,
            "backend": (prof or {}).get("backend") or "hints", "game_dialogue": total >= 3 and game_share >= GAME_SHARE_FLAG}


def streamer_words(words: list[dict]) -> list[dict]:
    """Words the streamer said (untagged words count as the streamer — the old behaviour)."""
    return [w for w in words if w.get("spk", STREAMER) == STREAMER]
