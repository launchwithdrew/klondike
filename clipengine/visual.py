"""Visual understanding (wishlist 1): what is ON SCREEN in a draft, read by CLIP, not guessed from chat and audio.

Why: editors' drops are visual (no payoff on screen, a static or dark picture, a menu, not the game, a death screen),
and the engine's signals — chat, loudness, speech — can't see any of it.

  model     CLIP ViT-B/32 as ONNX (Xenova/clip-vit-base-patch32, MIT), ~155 MB, run on onnxruntime + tokenizers
            (already installed for transcription — no torch). `clipengine visual setup` downloads it once into
            ~/.cache/clipengine/clip (CLIPENGINE_MODELS moves it), pinned to one revision and checked by sha256.
  read      each frame of a draft's contact sheet against short descriptions in five groups — action (a crash, a
            chase, a shootout), dull (plain driving, standing around), menu (pause / map / loading), off-game
            (webcam only, black screen, desktop), death (a death / game-over screen) and adult (a strip club,
            underwear, nudity); a frame's shares sum to 1 and a draft's are the mean (and for action, death and
            adult the peak) over its frames
  safety    adult peak >= 0.75 flags `adult scene on screen?` whatever the mode: demoted (the `adult_scene`
            penalty), never urgent — text can't see it; the editor's look still decides
  headline  how much better a headline describes the frames than a plain "a video game scene" does (advisory)
  eval      `clipengine visual eval` reads the frames editors judged (data/labels/<id>/<ship|hold|drop>/) and prints
            how well each measure separates their ships from their drops (AUC, 0.5 = chance). `--save` keeps the best
            one in data/visual.json — but only when it separates (AUC >= 0.70 on 8+ ships and 8+ drops)
  modes     config/grading.json "visual": {"mode": "advisory"} (default) shows `vis …` on the draft's queue line and
            changes nothing; "score" also flags drafts on the wrong side of the saved threshold, and the `visual`
            penalty applies — refused (stays advisory) until a saved eval says the measure separates
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ID = "Xenova/clip-vit-base-patch32"
REVISION = "6b7c523703ae26e39be4b03774966f8571c7f523"
FILES = {
    "onnx/vision_model_quantized.onnx": "583fd1110a514667812fee7d684952aaf82a99b959760c8d7dca7e0ab9839299",
    "onnx/text_model_quantized.onnx": "73baab855d406190da9faa498cfedf65f15cf309f4cc7385b7b032e6d08e5c3a",
    "tokenizer.json": "f7f3b7af117d467b58374797691a6438d3e6b9e9cef800dfd5dced7f697a90cd",
}
MEAN = (0.48145466, 0.4578275, 0.40821073)
STD = (0.26862954, 0.26130258, 0.27577711)
SIZE = 224
SCALE = 100.0           # CLIP's logit scale
MIN_AUC = 0.70
MIN_EACH = 8
ADULT_PEAK = 0.75      # 7 of 7 flagged sheets were real on 56 real GTA VI sheets (Nov 22); one at .53 missed

PROMPTS = {
    "action": ["a car crash in a video game", "an explosion in a video game", "a police chase in a video game",
               "a shootout in a video game", "a character fighting in a video game",
               "a car flipping over in a video game", "a character falling from a height in a video game"],
    "dull": ["a car driving down an empty road in a video game", "a character walking down a street in a video game",
             "a quiet video game scene where nothing happens", "a character standing still in a video game"],
    "menu": ["a video game pause menu", "a video game map screen", "a video game loading screen",
             "a video game settings menu", "a video game character customization screen",
             "a phone screen menu in a video game"],
    "offgame": ["a person talking to a webcam", "a black screen", "a computer desktop",
                "a stream starting soon screen", "a blurry dark image"],
    "death": ["a wasted death screen in a video game", "a game over screen in a video game"],
    # never decides: a text screen can't see a strip club or underwear, so this only tells the editor to LOOK
    "adult": ["a strip club in a video game", "a pole dancer in a video game", "a woman in underwear dancing",
              "a lap dance in a video game", "a nude body"],
}
GROUPS = tuple(PROMPTS)
# measures eval compares; "look" = action share minus everything an editor drops for
MEASURES = ("look", "action", "action_peak", "dull", "menu", "offgame", "death_peak")


def model_dir() -> str:
    return os.environ.get("CLIPENGINE_MODELS") or os.path.join(os.path.expanduser("~"), ".cache", "clipengine", "clip")


def _local(name: str) -> str:
    return os.path.join(model_dir(), os.path.basename(name))


def runtime_ok() -> tuple[bool, str]:
    try:
        import onnxruntime  # noqa: F401
        import tokenizers  # noqa: F401
    except ImportError as exc:
        return False, f"missing {exc.name} (pip install onnxruntime tokenizers)"
    return True, ""


def ready() -> bool:
    return runtime_ok()[0] and all(os.path.exists(_local(n)) for n in FILES)


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def setup(fetch=None, log=print) -> bool:
    """Download the pinned model files once (checked by sha256). fetch(url, dest) is injectable for tests."""
    ok, why = runtime_ok()
    if not ok:
        log(f"visual: {why}")
        return False
    os.makedirs(model_dir(), exist_ok=True)
    fetch = fetch or (lambda url, dest: urllib.request.urlretrieve(url, dest))
    for name, sha in FILES.items():
        dest = _local(name)
        if os.path.exists(dest) and _sha(dest) == sha:
            continue
        url = f"https://huggingface.co/{REPO_ID}/resolve/{REVISION}/{name}"
        log(f"visual: downloading {os.path.basename(name)} …")
        part = dest + ".part"
        try:
            fetch(url, part)
        except OSError as exc:
            log(f"visual: download failed ({str(exc)[:80]}) — nothing changed; run `clipengine visual setup` again")
            return False
        if _sha(part) != sha:
            os.remove(part)
            log(f"visual: {os.path.basename(name)} failed its checksum — removed; nothing used")
            return False
        os.replace(part, dest)
    log(f"visual: ready ({model_dir()})")
    return True


class Model:
    """The two ONNX sessions + tokenizer, loaded once per process."""
    _one = None

    def __init__(self):
        import onnxruntime as ort
        from tokenizers import Tokenizer
        so = ort.SessionOptions()
        so.intra_op_num_threads = 2          # never starve a recorder
        prov = ["CPUExecutionProvider"]
        self.vision = ort.InferenceSession(_local("onnx/vision_model_quantized.onnx"), so, providers=prov)
        self.text = ort.InferenceSession(_local("onnx/text_model_quantized.onnx"), so, providers=prov)
        self.tok = Tokenizer.from_file(_local("tokenizer.json"))
        self._texts: dict[str, object] = {}

    @classmethod
    def get(cls) -> "Model":
        if cls._one is None:
            cls._one = cls()
        return cls._one

    def embed_texts(self, texts: list[str]):
        import numpy as np
        out = []
        for t in texts:
            if t not in self._texts:
                ids = self.tok.encode(t).ids[:77]
                v = self.text.run(None, {"input_ids": np.array([ids], dtype=np.int64)})[0][0]
                self._texts[t] = v / (np.linalg.norm(v) or 1)
            out.append(self._texts[t])
        return np.vstack(out)

    def embed_images(self, images: list):
        import numpy as np
        if not images:
            return np.zeros((0, 512), dtype=np.float32)
        v = self.vision.run(None, {"pixel_values": np.stack([preprocess(im) for im in images])})[0]
        return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-8)


def preprocess(im):
    """CLIP's own preprocessing: shortest edge 224 (bicubic), centre crop, scale, normalise; CHW float32."""
    import numpy as np
    from PIL import Image
    im = im.convert("RGB")
    w, h = im.size
    s = SIZE / min(w, h)
    im = im.resize((max(SIZE, round(w * s)), max(SIZE, round(h * s))), Image.Resampling.BICUBIC)
    w, h = im.size
    left, top = (w - SIZE) // 2, (h - SIZE) // 2
    a = np.asarray(im.crop((left, top, left + SIZE, top + SIZE)), dtype=np.float32) / 255.0
    a = (a - np.array(MEAN, dtype=np.float32)) / np.array(STD, dtype=np.float32)
    return a.transpose(2, 0, 1).astype(np.float32)


def tiles(sheet_path: str) -> list:
    """The frames of a contact sheet (vision.contact_sheet: 4 per row, 16:9 tiles)."""
    from PIL import Image
    with Image.open(sheet_path) as sh:
        sh = sh.convert("RGB")
        tw = sh.width // 4
        if tw < 16:
            return []
        rows = max(1, round(sh.height / (tw * 9 / 16)))
        th = sh.height // rows
        out = []
        for r in range(rows):
            for c in range(4):
                t = sh.crop((c * tw, r * th, (c + 1) * tw, (r + 1) * th))
                if r < rows - 1 or max(hi for _, hi in t.getextrema()) > 8:   # a short last row is padded black
                    out.append(t)
        return out


def read(images: list, model: Model | None = None) -> dict:
    """Per-group shares of what is on screen (each frame sums to 1), averaged over the frames, + peaks + look."""
    import numpy as np
    if not images:
        return {}
    m = model or Model.get()
    prompts = [p for g in GROUPS for p in PROMPTS[g]]
    T = m.embed_texts(prompts)
    I = m.embed_images(images)
    logits = SCALE * I @ T.T
    P = np.exp(logits - logits.max(axis=1, keepdims=True))
    P /= P.sum(axis=1, keepdims=True)
    per, i = {}, 0
    for g in GROUPS:
        n = len(PROMPTS[g])
        per[g] = P[:, i:i + n].sum(axis=1)
        i += n
    out = {g: round(float(per[g].mean()), 3) for g in GROUPS}
    out["action_peak"] = round(float(per["action"].max()), 3)
    out["death_peak"] = round(float(per["death"].max()), 3)
    out["adult_peak"] = round(float(per["adult"].max()), 3)
    out["look"] = round(out["action"] - out["dull"] - out["menu"] - out["offgame"], 3)
    out["frames"] = len(images)
    return out


def read_sheet(sheet_path: str, model: Model | None = None) -> dict:
    return read(tiles(sheet_path), model)


def headline_fit(title: str, images: list, model: Model | None = None) -> float | None:
    """How much better the headline describes the best frame than a plain 'a video game scene' does (cosine)."""
    import numpy as np
    if not title or not images:
        return None
    m = model or Model.get()
    T = m.embed_texts([f"a video game scene: {title}", "a video game scene"])
    I = m.embed_images(images)
    s = I @ T.T
    return round(float(np.max(s[:, 0] - s[:, 1])), 3)


def short(v: dict) -> str:
    """The queue's advisory: `vis look +.21 act .48 menu .05`."""
    if not v:
        return ""
    bits = [f"look {v['look']:+.2f}", f"act {v['action']:.2f}"]
    for g, label in (("menu", "menu"), ("offgame", "off-game"), ("dull", "dull")):
        if v.get(g, 0) >= 0.25:
            bits.append(f"{label} {v[g]:.2f}")
    if v.get("death_peak", 0) >= 0.4:
        bits.append(f"death {v['death_peak']:.2f}")
    if v.get("adult_peak", 0) >= ADULT_PEAK:
        bits.append(f"⛔adult {v['adult_peak']:.2f}")
    return "vis " + " ".join(bits)


# ---- modes + evidence ---------------------------------------------------------------------------------------

def _cfg() -> dict:
    try:
        from . import grade as G
        return G.config().get("visual") or {}
    except Exception:
        return {}


def saved(root: str | None = None) -> dict:
    try:
        return json.load(open(os.path.join(root or REPO, "data", "visual.json")))
    except (OSError, ValueError):
        return {}


def mode(root: str | None = None) -> str:
    """off | advisory | score — score only with a saved eval that separates; else advisory."""
    want = (_cfg().get("mode") or "advisory").lower()
    if want == "off" or not ready():
        return "off"
    if want == "score":
        ev = saved(root)
        if (ev.get("auc") or 0) >= MIN_AUC and ev.get("measure") in MEASURES and ev.get("threshold") is not None:
            return "score"
    return "advisory"


def score_flag(v: dict, root: str | None = None) -> str | None:
    """In score mode: a flag when the saved measure is on the drop side of its threshold (the `visual` penalty)."""
    ev = saved(root)
    if not v or mode(root) != "score":
        return None
    x, thr = v.get(ev["measure"]), ev["threshold"]
    if x is None:
        return None
    bad = x < thr if ev.get("ships_high", True) else x > thr
    return f"picture reads like a drop (visual {ev['measure']} {x:.2f})" if bad else None


def adult_flag(v: dict) -> str | None:
    """A safety flag, on whenever the model is: a strip club / underwear / nudity on screen is what a transcript
    can't see (rules/safety.md). It demotes and stops `urgent`; the editor's look still decides."""
    p = (v or {}).get("adult_peak") or 0
    return f"adult scene on screen? (picture {p:.2f}) — look before posting" if p >= ADULT_PEAK else None


def assess_images(images: list, root: str | None = None) -> dict:
    """What a new draft gets: the reads + `flags` to add; {} when off / unreadable. Never raises."""
    if not images or mode(root) == "off":
        return {}
    try:
        v = read(images)
    except Exception:
        return {}
    v["flags"] = [f for f in (adult_flag(v), score_flag(v, root)) if f]
    return v


def assess(sheet_path: str | None, root: str | None = None) -> dict:
    if not sheet_path or not os.path.exists(sheet_path) or mode(root) == "off":
        return {}
    try:
        return assess_images(tiles(sheet_path), root)
    except Exception:
        return {}


def thumb_images(thumbs: list[str]) -> list:
    """A scan's thumbnails (data: URIs or paths) as images."""
    import base64
    import io
    from PIL import Image
    out = []
    for t in thumbs or []:
        try:
            if t and t.startswith("data:image"):
                out.append(Image.open(io.BytesIO(base64.b64decode(t.split(",", 1)[1]))).convert("RGB"))
            elif t and os.path.isfile(t):
                out.append(Image.open(t).convert("RGB"))
        except (OSError, ValueError):
            continue
    return out


# ---- eval on editors' labelled frames ------------------------------------------------------------------------

def auc(pos: list[float], neg: list[float]) -> float | None:
    """P(a random ship scores above a random drop); ties count half."""
    if not pos or not neg:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def labelled(labels_dir: str) -> list[dict]:
    """[{id, verdict, frames: [paths]}] from data/labels/<id>/<verdict>/frame-*.jpg (latest verdict folder wins)."""
    out = []
    if not os.path.isdir(labels_dir):
        return out
    for did in sorted(os.listdir(labels_dir)):
        d = os.path.join(labels_dir, did)
        if not os.path.isdir(d):
            continue
        best = None
        for verdict in ("ship", "hold", "drop"):
            vd = os.path.join(d, verdict)
            frames = sorted(os.path.join(vd, f) for f in os.listdir(vd) if f.endswith(".jpg")) if os.path.isdir(vd) else []
            if frames and (best is None or os.path.getmtime(vd) > best[2]):
                best = (verdict, frames, os.path.getmtime(vd))
        if best:
            out.append({"id": did, "verdict": best[0], "frames": best[1]})
    return out


def evaluate(labels_dir: str | None = None, reader=None, save: bool = False, root: str | None = None) -> dict:
    """Each measure's AUC for the editor's ships vs drops; --save keeps the best when it separates."""
    from PIL import Image
    root = root or REPO
    labels_dir = labels_dir or os.path.join(root, "data", "labels")
    rows = labelled(labels_dir)
    reader = reader or (lambda paths: read([Image.open(p).convert("RGB") for p in paths]))
    scored = []
    for r in rows:
        try:
            scored.append({**r, "v": reader(r["frames"])})
        except Exception:
            continue
    ships = [r for r in scored if r["verdict"] == "ship"]
    drops = [r for r in scored if r["verdict"] == "drop"]
    res = {"ships": len(ships), "drops": len(drops), "holds": sum(r["verdict"] == "hold" for r in scored),
           "measures": {}}
    print(f"visual eval: {len(ships)} ships · {len(drops)} drops · {res['holds']} holds (holds left out) "
          f"from {labels_dir}")
    if not ships or not drops:
        print("  need both ships and drops with frames — judged drafts store 3 stills each (data/labels)")
        return res
    best = None
    for m in MEASURES:
        a = auc([r["v"][m] for r in ships], [r["v"][m] for r in drops])
        hi = a >= 0.5
        sep = a if hi else 1 - a
        vals = sorted({r["v"][m] for r in ships + drops})
        # threshold: the cut that gets the most drafts right (ships on the high side when hi)
        thr, right = None, -1
        for t in vals:
            ok = sum((r["v"][m] >= t) == hi for r in ships) + sum((r["v"][m] >= t) != hi for r in drops)
            if ok > right:
                thr, right = t, ok
        res["measures"][m] = {"auc": round(sep, 3), "ships_high": hi, "threshold": thr,
                              "right": right, "of": len(ships) + len(drops),
                              "ship_mean": round(sum(r["v"][m] for r in ships) / len(ships), 3),
                              "drop_mean": round(sum(r["v"][m] for r in drops) / len(drops), 3)}
        print(f"  {m:<12} AUC {sep:.2f} ({'ships higher' if hi else 'ships lower'})  ship mean "
              f"{res['measures'][m]['ship_mean']:+.2f} · drop mean {res['measures'][m]['drop_mean']:+.2f} · "
              f"best cut {thr:+.2f} gets {right}/{len(ships) + len(drops)} right")
        if best is None or sep > res["measures"][best]["auc"]:
            best = m
    b = res["measures"][best]
    enough = len(ships) >= MIN_EACH and len(drops) >= MIN_EACH
    res["best"] = best
    res["separates"] = enough and b["auc"] >= MIN_AUC
    verdict = ("SEPARATES" if res["separates"] else
               f"not enough labels yet (need {MIN_EACH}+ ships and {MIN_EACH}+ drops)" if not enough else
               f"does not separate (best AUC {b['auc']:.2f} < {MIN_AUC})")
    print(f"  best: {best} AUC {b['auc']:.2f} — {verdict}"
          + (" · with 7 measures on few drafts, re-check after more labels before scoring with it" if enough else ""))
    if save:
        if res["separates"]:
            ev = {"measure": best, "auc": b["auc"], "ships_high": b["ships_high"], "threshold": b["threshold"],
                  "ships": len(ships), "drops": len(drops), "revision": REVISION,
                  "at": dt.datetime.now(dt.timezone.utc).isoformat()}
            os.makedirs(os.path.join(root, "data"), exist_ok=True)
            json.dump(ev, open(os.path.join(root, "data", "visual.json"), "w"), indent=1)
            print(f"  saved data/visual.json — set config/grading.json \"visual\": {{\"mode\": \"score\"}} to use it")
        else:
            print("  not saved: only a measure that separates is kept")
    return res


def doctor_line() -> str:
    ok, why = runtime_ok()
    if not ok:
        return f"✗ visual: {why}"
    if not ready():
        return "· visual: not set up (optional) — `clipengine visual setup` downloads the 155 MB picture model once"
    ev = saved()
    return (f"✓ visual: {mode()} mode" + (f" · saved eval {ev['measure']} AUC {ev['auc']:.2f}" if ev else
                                       " · no saved eval yet (`clipengine visual eval`)"))
