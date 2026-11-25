"""A trigger in shadow (OP_SPEC §1.4): it fires beside the live trigger, drafts nothing, and its live recall is shown
apart (`clipengine funnel`), so a new signal earns its place on real streams before it touches drafting.

The shadow model is config/shadow_model.json (written by eval/f1/trig_eval.py --save-shadow, from the held-out
evaluation): {"name", "features", "weights", "bias", "trigger", "release", "cooldown_s", "evaluated"}. Features are
the live chat features plus sound events from the recording: YAMNet (Google's AudioSet classifier, Apache 2.0) on each
new recorded segment — laughter, shouting/screaming, cheering, explosions/crashes, speech, loudness — each also
against the stream's own last 10 minutes. `clipengine shadow setup` downloads YAMNet once (pinned, checksummed).

Every fire goes to the sentinel's events.jsonl as {"ev": "fire", "trigger": "shadow:<name>"}; live fires are logged
there too ({"trigger": "live"}) so both are scored on the same viewer clips.
"""
from __future__ import annotations

import json
import math
import os
import threading
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL = os.path.join(REPO, "config", "shadow_model.json")
YAMNET_ID = "audiomagic/yamnet-onnx"                 # a straight tf2onnx export of google/yamnet v1 (Apache 2.0)
YAMNET_REV = "f25b741c2f0bdc6d7e6db24b5fddda23347dbafd"
YAMNET_FILES = {"yamnet.onnx": "d3835ffbbd4a1bb3e777f0ca217b5007907f5171dd5d17c4236b95b2af8f908e",
                "yamnet_class_map.csv": "cdf24d193e196d9e95912a2667051ae203e92a2ba09449218ccb40ef787c6df2"}
GROUPS = {"laugh": [13, 14, 15, 16, 17, 18], "shout": [6, 7, 8, 9, 10, 11], "cheer": [61, 62, 64],
          "boom": [420, 421, 430, 463], "speech": [0], "gasp": [39, 22, 33]}
STEP_S = 5.0
Z_SPAN, Z_WARM = 120, 24
FRAME_S = 0.48


def yamnet_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".cache", "clipengine", "yamnet")


def _sha(path: str) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def ready() -> bool:
    return all(os.path.exists(os.path.join(yamnet_dir(), f)) for f in YAMNET_FILES)


def setup(fetch=None, log=print) -> bool:
    os.makedirs(yamnet_dir(), exist_ok=True)
    fetch = fetch or (lambda url, dest: urllib.request.urlretrieve(url, dest))
    for name, sha in YAMNET_FILES.items():
        dest = os.path.join(yamnet_dir(), name)
        if os.path.exists(dest) and _sha(dest) == sha:
            continue
        log(f"shadow: downloading {name} …")
        part = dest + ".part"
        try:
            fetch(f"https://huggingface.co/{YAMNET_ID}/resolve/{YAMNET_REV}/{name}", part)
        except OSError as exc:
            log(f"shadow: download failed ({str(exc)[:80]}) — nothing changed")
            return False
        if _sha(part) != sha:
            os.remove(part)
            log(f"shadow: {name} failed its checksum — removed; nothing used")
            return False
        os.replace(part, dest)
    log(f"shadow: ready ({yamnet_dir()})")
    return True


def load_model(path: str = MODEL) -> dict | None:
    try:
        m = json.load(open(path))
    except (OSError, ValueError):
        return None
    return m if m.get("features") and m.get("weights") is not None and m.get("trigger") else None


class Sound:
    """YAMNet group scores for recorded audio, by wall-clock time (the recording index's `t`)."""

    def __init__(self, session=None):
        self.frames: list[tuple[float, dict, float]] = []        # (t, {group: score}, dB)
        self.lock = threading.Lock()
        self._s = session

    def session(self):
        if self._s is None:
            import onnxruntime as ort
            o = ort.SessionOptions()
            o.intra_op_num_threads = 1                           # never starve the recorder
            self._s = ort.InferenceSession(os.path.join(yamnet_dir(), "yamnet.onnx"), o)
        return self._s

    def add(self, t0: float, pcm, sr: int = 16000) -> None:
        import numpy as np
        if pcm is None or len(pcm) < sr:
            return
        sc = self.session().run(None, {"waveform": pcm.astype(np.float32)})[0]
        rows = []
        for k, row in enumerate(sc):
            a = int(k * FRAME_S * sr)
            c = pcm[a:a + int(2 * FRAME_S * sr)]
            db = float(20 * np.log10(np.sqrt(np.mean(np.square(c))) + 1e-9)) if len(c) else -90.0
            rows.append((t0 + (k + 1) * FRAME_S, {g: float(max(row[j] for j in ks)) for g, ks in GROUPS.items()}, db))
        with self.lock:
            self.frames = sorted(self.frames + rows)[-4000:]      # ~30 min

    def window(self, t0: float, t1: float) -> dict:
        with self.lock:
            fr = [f for f in self.frames if t0 < f[0] <= t1]
        if not fr:
            return {}
        out = {f"y_{g}": max(f[1][g] for f in fr) for g in GROUPS}
        out["y_db"] = max(f[2] for f in fr)
        return out

    def latest(self) -> float | None:
        with self.lock:
            return self.frames[-1][0] if self.frames else None


class Shadow:
    """The shadow trigger for one sentinel. step() once per 5 s with the live chat features."""

    def __init__(self, model: dict, sound: Sound, log=None):
        self.m, self.sound, self.log = model, sound, log
        self.hist: dict[str, list[float]] = {}
        self.cur: dict | None = None
        self.last_end = -1e9

    def features(self, chat: dict) -> dict | None:
        end = self.sound.latest()
        if end is None:
            return None
        y = self.sound.window(end - STEP_S, end)
        if not y:
            return None
        f = dict(chat)
        for k, v in y.items():
            h = self.hist.setdefault(k, [])
            if len(h) >= Z_WARM:
                mu = sum(h) / len(h)
                sd = math.sqrt(sum((x - mu) ** 2 for x in h) / len(h)) + 1e-6
                f[k + "_z"] = max(-5.0, min(8.0, (v - mu) / sd))
            else:
                f[k + "_z"] = 0.0
            f[k] = v
            h.append(v)
            del h[:-Z_SPAN]
        return f

    def prob(self, f: dict) -> float:
        z = float(self.m.get("bias", 0.0)) + sum(float(w) * float(f.get(k, 0.0)) for k, w in self.m["weights"].items())
        return 1 / (1 + math.exp(-max(-30.0, min(30.0, z))))

    def step(self, now: float, chat: dict) -> dict | None:
        """Returns the fire (once, when it starts) or None."""
        f = self.features(chat)
        if f is None:
            return None
        p = self.prob(f)
        trig, rel = float(self.m["trigger"]), float(self.m.get("release", 0.9 * float(self.m["trigger"])))
        if self.cur is None:
            if p >= trig and now - self.last_end >= float(self.m.get("cooldown_s", 30)):
                self.cur = {"fire": now, "p": p}
                ev = {"ev": "fire", "t": round(now, 1), "trigger": f"shadow:{self.m.get('name', 'model')}", "p": round(p, 3)}
                if self.log:
                    self.log(ev)
                return ev
        elif p < rel:
            self.cur, self.last_end = None, now
        return None


def start(out: str, log, segments_dir: str | None = None):
    """A Shadow fed by a thread that runs YAMNet on each new recorded segment; None when there is no model or no
    YAMNet (the sentinel runs exactly as before)."""
    m = load_model()
    if not m or not ready():
        return None
    snd = Sound()
    sh = Shadow(m, snd, log)

    def feed():
        import time
        from . import audio as A
        from . import live as L
        seen: set[str] = set()
        while True:
            try:
                for r in L.load_index(out)[-8:]:
                    if r["f"] in seen:
                        continue
                    seen.add(r["f"])
                    x = A.decode_pcm(os.path.join(segments_dir or os.path.join(out, "segments"), r["f"]), 0, None)
                    snd.add(float(r["t"]), x)
                if len(seen) > 5000:
                    seen = set(list(seen)[-1000:])
            except Exception:
                pass
            time.sleep(2.0)

    threading.Thread(target=feed, daemon=True).start()
    return sh


# ---- live recall, shown apart (funnel) ------------------------------------------------------------------------

def live_recall(events: list[dict], min_views: int = 25) -> dict:
    """Per trigger: recorded viewer-clipped moments (25+ views) it fired on, and fires per recorded hour that no
    viewer clip explains. From one or more sentinels' events.jsonl rows."""
    clips: dict[str, dict] = {}
    for e in events:
        if e.get("ev") == "clip" and e.get("start") is not None and e.get("end") is not None:
            clips[e["slug"]] = {**clips.get(e["slug"], {}), **e}
    recorded = [c for c in clips.values() if (c.get("views") or 0) >= min_views
                and c.get("state") not in ("before the recording", "not recorded yet", "replay clip")]
    fires: dict[str, list[float]] = {}
    for e in events:
        if e.get("ev") == "fire":
            fires.setdefault(e.get("trigger", "live"), []).append(float(e["t"]))
    ts = [float(e["t"]) for e in events if e.get("ev") == "poll"]
    hours = (max(ts) - min(ts)) / 3600 if len(ts) > 1 else 0.0
    out = {"moments": len(recorded), "hours": round(hours, 1), "triggers": {}}
    for name, fs in fires.items():
        hit = sum(1 for c in recorded if any(c["start"] - 10 <= f <= c["end"] + 40 for f in fs))
        false = sum(1 for f in fs if not any(c["start"] - 60 <= f <= c["end"] + 60 for c in clips.values()))
        out["triggers"][name] = {"caught": hit, "fires": len(fs),
                                 "false_per_h": round(false / hours, 1) if hours else None}
    return out


def recall_line(r: dict) -> str | None:
    if not r["triggers"] or not any(k.startswith("shadow:") for k in r["triggers"]):
        return None
    parts = [f"{k} {v['caught']}/{r['moments']}" + (f" at {v['false_per_h']} unexplained fires/h" if v["false_per_h"]
             is not None else "") for k, v in sorted(r["triggers"].items())]
    return f"shadow trigger (drafts nothing), {r['hours']} recorded h: " + " · ".join(parts)
