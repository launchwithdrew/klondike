"""Facecam detection — where the streamer's webcam box sits in the frame.

The look top clip pages post is the streamer's FACECAM on top and the gameplay below. To cut it we
need the webcam overlay's rectangle. It is a fixed region with a face in it, while gameplay faces
(cutscenes, NPCs) move around. So:

  1. sample frames spread over the footage (a recording, a VOD window, or the clip being rendered)
  2. find faces with OpenCV's bundled Haar cascades (CPU, no model download)
  3. the webcam face = the cluster of detections that stays in the same place in many frames
  4. the webcam box = the persistent edges around that face (the overlay's border, or the boundary
     between two unrelated images), falling back to the frame edge or a face-proportional box
  5. confidence = how often the face is there × how many sides of the box were actually found

Results are normalised [x, y, w, h] (0–1 of the frame). Per creator:
  * manual override — `"facecam": [x, y, w, h]` on the creator in config/creators.json (always wins);
    `"facecam": false` = this creator has no webcam box (full-room camera, avatar): always the stack layout
  * cache           — data/facecam.json (written at runtime by `clipengine facecam` and by renders)

`clipengine facecam --rec DIR` / `--channel LOGIN` prints what it found and saves a check image.
"""
from __future__ import annotations

import concurrent.futures as cf
import contextlib
import contextvars
import datetime as dt
import json
import os
import subprocess
import tempfile

import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.environ.get("CLIPENGINE_FACECAM_CACHE") or os.path.join(REPO, "data", "facecam.json")
CREATORS = os.path.join(REPO, "config", "creators.json")
GOOD = 0.6            # confidence needed to cut the facecam layout from a box
WORK_W, WORK_H = 960, 540

_login: contextvars.ContextVar[str | None] = contextvars.ContextVar("facecam_login", default=None)


@contextlib.contextmanager
def creator(login: str | None):
    """Tell render() whose stream it is cutting (it looks up that creator's facecam)."""
    tok = _login.set((login or "").lower() or None)
    try:
        yield
    finally:
        _login.reset(tok)


def current_login() -> str | None:
    return _login.get()


def available() -> bool:
    try:
        import cv2  # noqa: F401
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- frames

def media_info(path: str) -> dict:
    p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height:format=duration", "-of", "json", path], capture_output=True, text=True)
    try:
        j = json.loads(p.stdout or "{}")
        st = (j.get("streams") or [{}])[0]
        return {"width": int(st.get("width") or 0), "height": int(st.get("height") or 0),
                "duration": float((j.get("format") or {}).get("duration") or 0)}
    except (ValueError, TypeError):
        return {"width": 0, "height": 0, "duration": 0.0}


def _grab(path: str, t: float) -> np.ndarray | None:
    p = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0.0, t):.2f}", "-i", path, "-frames:v", "1",
                        "-vf", f"scale={WORK_W}:{WORK_H}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                       capture_output=True)
    if p.returncode != 0 or len(p.stdout) != WORK_W * WORK_H * 3:
        return None
    return np.frombuffer(p.stdout, np.uint8).reshape(WORK_H, WORK_W, 3)


def sample_frames(path: str, n: int = 16, start: float = 0.0, end: float | None = None) -> list[np.ndarray]:
    """n frames spread evenly over [start, end] of a video file (BGR, WORK_W x WORK_H)."""
    if end is None:
        end = media_info(path)["duration"] or (start + 10)
    span = max(0.5, end - start)
    times = [start + span * (i + 0.5) / n for i in range(n)]
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        out = list(ex.map(lambda t: _grab(path, t), times))
    return [f for f in out if f is not None]


# ---------------------------------------------------------------- detection

def _cascades():
    import cv2
    root = cv2.data.haarcascades
    return [cv2.CascadeClassifier(os.path.join(root, f)) for f in
            ("haarcascade_frontalface_alt2.xml", "haarcascade_frontalface_default.xml", "haarcascade_profileface.xml")]


def faces_in(gray: np.ndarray, cascades=None) -> list[tuple[int, int, int, int]]:
    cascades = cascades or _cascades()
    h, w = gray.shape
    found = []
    for i, c in enumerate(cascades):
        for flip in ((False, True) if i == 2 else (False,)):       # profile cascade only sees one side
            g = gray[:, ::-1] if flip else gray
            got = c.detectMultiScale(g, scaleFactor=1.1, minNeighbors=4, minSize=(20, 20))
            for (x, y, fw, fh) in (got if len(got) else []):
                if flip:
                    x = w - x - fw
                found.append((int(x), int(y), int(fw), int(fh)))
    # merge overlapping detections from the two cascades
    out: list[tuple[int, int, int, int]] = []
    for b in sorted(found, key=lambda b: -b[2] * b[3]):
        if not any(_iou(b, o) > 0.3 for o in out):
            out.append(b)
    return out


def _iou(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    return inter / float(aw * ah + bw * bh - inter or 1)


def fixed_face(per_frame: list[list[tuple]], n_frames: int) -> tuple[tuple[int, int, int, int], float] | None:
    """The face that stays put: cluster detections by centre; return (median box, share of frames)."""
    clusters: list[dict] = []
    for fi, boxes in enumerate(per_frame):
        for b in boxes:
            cx, cy = b[0] + b[2] / 2, b[1] + b[3] / 2
            for c in clusters:
                if abs(cx - c["cx"]) < 0.5 * c["s"] and abs(cy - c["cy"]) < 0.5 * c["s"]:
                    c["boxes"].append(b)
                    c["frames"].add(fi)
                    c["cx"] = np.mean([x + w / 2 for x, _, w, _ in c["boxes"]])
                    c["cy"] = np.mean([y + h / 2 for _, y, _, h in c["boxes"]])
                    c["s"] = float(np.median([w for _, _, w, _ in c["boxes"]]))
                    break
            else:
                clusters.append({"cx": cx, "cy": cy, "s": float(b[2]), "boxes": [b], "frames": {fi}})
    if not clusters:
        return None
    best = max(clusters, key=lambda c: (len(c["frames"]), -c["s"]))
    share = len(best["frames"]) / max(1, n_frames)
    if len(best["frames"]) < 2:
        return None
    med = tuple(int(np.median([b[k] for b in best["boxes"]])) for k in range(4))
    return med, share


def persistent_edges(grays: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    """(vertical-line map, horizontal-line map), 0/1 per pixel. A border is where the brightness jumps
    in EVERY frame: its average |gradient| over time is far above the frame's typical level. That holds
    for a drawn overlay border and for the seam between two unrelated pictures (a webcam over dark
    gameplay), while gameplay edges move and average out. Static things inside the webcam (the
    streamer's room) persist too, which is why the box is chosen as a whole rectangle below."""
    import cv2
    gx = np.zeros(grays[0].shape, np.float32)
    gy = np.zeros(grays[0].shape, np.float32)
    for g in grays:
        gx += np.abs(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3))
        gy += np.abs(cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3))
    gx /= len(grays)
    gy /= len(grays)
    level = 4.0 * max(1.0, float(np.median(np.maximum(gx, gy))))
    return (gx > level).astype(np.uint8), (gy > level).astype(np.uint8)


class _Lines:
    """Fast 'how much of this segment is a persistent edge' via prefix sums (±1 px tolerance)."""

    def __init__(self, maps: tuple[np.ndarray, np.ndarray]):
        vm, hm = maps
        v = np.maximum(vm, np.maximum(np.roll(vm, 1, 1), np.roll(vm, -1, 1)))   # vertical lines: ±1 col
        h = np.maximum(hm, np.maximum(np.roll(hm, 1, 0), np.roll(hm, -1, 0)))   # horizontal lines: ±1 row
        self.vc = np.vstack([np.zeros((1, v.shape[1]), np.int32), np.cumsum(v, 0, dtype=np.int32)])
        self.hc = np.hstack([np.zeros((h.shape[0], 1), np.int32), np.cumsum(h, 1, dtype=np.int32)])
        self.H, self.W = v.shape

    def vert(self, x: int, y0: int, y1: int) -> float:
        if x <= 0 or x >= self.W - 1:
            return 0.9                    # the frame's own edge is a border
        y0, y1 = max(0, y0), min(self.H, y1)
        return float(self.vc[y1, x] - self.vc[y0, x]) / max(1, y1 - y0)

    def horiz(self, y: int, x0: int, x1: int) -> float:
        if y <= 0 or y >= self.H - 1:
            return 0.9
        x0, x1 = max(0, x0), min(self.W, x1)
        return float(self.hc[y, x1] - self.hc[y, x0]) / max(1, x1 - x0)


def _candidates(scores: dict[int, float], keep: int = 8) -> list[int]:
    """Local maxima of a line-score profile, strongest first."""
    items = sorted(scores.items())
    peaks = [p for i, (p, s) in enumerate(items)
             if s >= 0.25 and all(s >= items[j][1] for j in range(max(0, i - 3), min(len(items), i + 4)))]
    return sorted(peaks, key=lambda p: -scores[p])[:keep]


def find_box(grays: list[np.ndarray], face: tuple[int, int, int, int]) -> tuple[list[int], int]:
    """The webcam rectangle around a fixed face: of all rectangles built from candidate lines (and the frame
    edges) that contain the face, the one whose four sides are most completely persistent edges; when
    several have four solid sides, the tightest (a chat panel stacked on the webcam shares its border;
    edges of the room inside the webcam are rarely solid all the way along).
    Returns ([x, y, w, h] in work pixels, sides that are real lines)."""
    H, W = grays[0].shape
    L = _Lines(persistent_edges(grays))
    fx, fy, fw, fh = face
    ys0, ys1 = max(0, fy - fh), min(H, fy + 2 * fh)
    xs0, xs1 = max(0, fx - fw), min(W, fx + 2 * fw)
    left = {x: L.vert(x, ys0, ys1) for x in range(max(0, fx - 6 * fw), max(1, fx - fw // 4))}
    right = {x: L.vert(x, ys0, ys1) for x in range(min(W - 1, fx + fw + fw // 4), min(W, fx + 7 * fw) + 1)}
    top = {y: L.horiz(y, xs0, xs1) for y in range(max(0, fy - 4 * fh), max(1, fy - fh // 4))}
    bot = {y: L.horiz(y, xs0, xs1) for y in range(min(H - 1, fy + fh + fh // 4), min(H, fy + 5 * fh) + 1)}
    cl, cr, ct, cb = (_candidates(d) for d in (left, right, top, bot))
    for c, edge, rng in ((cl, 0, left), (cr, W, right), (ct, 0, top), (cb, H, bot)):
        if (edge in rng or edge - 1 in rng) and edge not in c:
            c.append(edge)
    best, best_s = None, -1.0
    for l in cl:
        for r in cr:
            w = r - l
            if w < 2 * fw:
                continue
            for t in ct:
                for b in cb:
                    h = b - t
                    if h < 1.5 * fh or not (0.6 <= w / h <= 2.6) or w * h > 0.45 * W * H:
                        continue
                    sides = (L.vert(l, t, b), L.vert(r, t, b), L.horiz(t, l, r), L.horiz(b, l, r))
                    s = min(sides) * 0.5 + sum(sides) / 8
                    if min(sides) >= 0.85:
                        s = 1.0       # all four sides are solid borders: the tightest such box wins
                    if s > best_s + 0.01 or (s > best_s - 0.01 and best and w * h < best[2] * best[3]):
                        best, best_s, best_sides = [l, t, w, h], max(s, best_s), sides
    if best is None or best_s < 0.45:
        cx, cy = fx + fw / 2, fy + fh / 2          # no clear border: a face-proportional guess
        w, h = 3.8 * fw, 3.8 * fw * 9 / 16 * 1.3
        x, y = max(0, cx - w / 2), max(0, cy - 0.45 * h)
        return [int(x), int(y), int(min(w, W - x)), int(min(h, H - y))], 0
    return best, sum(s >= 0.6 for s in best_sides)


def detect_frames(frames: list[np.ndarray]) -> dict:
    """Facecam from already-decoded BGR frames (WORK_W x WORK_H)."""
    import cv2
    if len(frames) < 3:
        return {"box": None, "confidence": 0.0, "why": f"only {len(frames)} frames readable"}
    grays = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in frames]
    cas = _cascades()
    per = [faces_in(cv2.equalizeHist(g), cas) for g in grays]
    ff = fixed_face(per, len(frames))
    if not ff:
        return {"box": None, "confidence": 0.0, "why": "no face stays in one place (no webcam, or it is hidden)",
                "faces_seen": sum(len(p) for p in per)}
    face, share = ff
    if face[3] > 0.45 * WORK_H:
        return {"box": None, "confidence": 0.0, "face": face,
                "why": "the face fills the frame — a full-camera (just chatting) shot, not a facecam over gameplay"}
    box, sides = find_box(grays, face)
    x, y, w, h = box
    area = w * h / float(WORK_W * WORK_H)
    seen = round(share * len(frames))
    # a face seen in the same spot at several separate moments is strong evidence even if the streamer
    # often looks away (Haar only sees fairly frontal faces); the box's real borders carry the rest
    presence = min(1.0, seen / max(3.0, 0.2 * len(frames)))
    conf = presence * (0.3 + 0.175 * sides)
    why = f"face in {share:.0%} of frames, {sides}/4 box sides found"
    if area > 0.45:
        conf, why = min(conf, 0.3), why + "; box is almost the whole frame (full camera?)"
    if not (0.5 <= w / max(1, h) <= 3.0):
        conf, why = min(conf, 0.4), why + f"; odd shape {w}x{h}"
    return {"box": [round(x / WORK_W, 4), round(y / WORK_H, 4), round(w / WORK_W, 4), round(h / WORK_H, 4)],
            "confidence": round(conf, 2), "why": why, "face": list(face), "frames": len(frames)}


def detect_file(path: str, n: int = 16, start: float = 0.0, end: float | None = None) -> dict:
    if not available():
        return {"box": None, "confidence": 0.0, "why": "opencv not installed (bash setup/setup.sh)"}
    return detect_frames(sample_frames(path, n, start, end))


# ---------------------------------------------------------------- per creator

def _load(p: str) -> dict:
    try:
        return json.load(open(p))
    except (OSError, ValueError):
        return {}


def override(login: str) -> list[float] | None:
    c = (_load(CREATORS).get("creators") or {}).get((login or "").lower()) or {}
    box = c.get("facecam")
    if isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) for v in box):
        return [float(v) for v in box]
    return None


def disabled(login: str | None) -> bool:
    """`"facecam": false` on a creator: no webcam box (a full-room camera, an avatar) — always the stack layout."""
    c = (_load(CREATORS).get("creators") or {}).get((login or "").lower()) or {}
    return c.get("facecam") is False


def cached(login: str) -> dict | None:
    return _load(CACHE).get((login or "").lower())


def save(login: str, res: dict, source: str) -> None:
    if not login or not res.get("box"):
        return
    data = _load(CACHE)
    data[login.lower()] = {"box": res["box"], "confidence": res["confidence"], "source": source,
                           "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    tmp = CACHE + ".tmp"
    json.dump(data, open(tmp, "w"), indent=2)
    os.replace(tmp, CACHE)


def known(login: str | None) -> dict | None:
    """What we know for this creator without looking at video: the override, else a confident cache."""
    if not login or disabled(login):
        return None
    ov = override(login)
    if ov:
        return {"box": ov, "confidence": 1.0, "source": "config/creators.json"}
    c = cached(login)
    if c and c.get("confidence", 0) >= GOOD:
        return {**c, "source": "data/facecam.json"}
    return None


def cam_present(source: str, box: list[float], start: float = 0.0, end: float | None = None, n: int = 8) -> bool:
    """Is the streamer actually in that box during this clip? A face in any of n frames is enough (a headset
    hides a face from the detector most of the time); none at all = the webcam isn't on screen (the streamer
    went full-screen game or switched scene), and a facecam cut would show a slice of gameplay instead."""
    import cv2
    frames = sample_frames(source, n, start, end)
    if not frames:
        return True                                     # can't look: trust the checked box
    cas = _cascades()
    x, y, w, h = box
    for f in frames:
        H, W = f.shape[:2]
        crop = f[int(y * H):int((y + h) * H), int(x * W):int((x + w) * W)]
        if crop.size == 0:
            continue
        g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        if g.shape[1] < 320:                             # small webcam: enlarge so a face is big enough to find
            k = 320 / g.shape[1]
            g = cv2.resize(g, (320, int(g.shape[0] * k)))
        if faces_in(g, cas):
            return True
    return False


def for_clip(source: str, login: str | None, start: float = 0.0, end: float | None = None,
             detect: bool = True) -> dict:
    """The facecam to use for one clip: the creator's override, else what this clip itself shows (and it
    updates the cache), else a confident cached box. {'box': None} = cut the stack layout instead.
    detect=False (brand layout "facecam_detect": false): only checked boxes — no override, no facecam."""
    if login and override(login):
        box = override(login)
        if available() and not cam_present(source, box, start, end):
            return {"box": None, "confidence": 0.0, "source": "config/creators.json",
                    "why": "the webcam isn't on screen in this clip (full-screen game or another scene)"}
        return {"box": box, "confidence": 1.0, "source": "config/creators.json"}
    if disabled(login):
        return {"box": None, "confidence": 0.0, "source": "config/creators.json",
                "why": "no webcam box for this creator (\"facecam\": false)"}
    if not detect:
        return {"box": None, "confidence": 0.0, "source": "none",
                "why": "no checked webcam box for this creator (set \"facecam\" in config/creators.json)"}
    res = detect_file(source, n=20, start=start, end=end)
    if res.get("box") and res["confidence"] >= GOOD:
        save(login, res, "render")
        return {**res, "source": "this clip"}
    kn = known(login)
    if kn and res.get("why", "").startswith("opencv"):
        return kn                       # can't look — trust what we learned before
    return {**res, "box": None, "source": "none", "why": res.get("why", "")}


# ---------------------------------------------------------------- suggestions for unchecked creators

SUGGEST_AFTER_S = 180          # recording needed before a sentinel looks for the webcam
SUGGEST_STATE = os.path.join(REPO, "runs", "facecam_suggest.json")


def unchecked(login: str | None) -> bool:
    """No "facecam" setting at all on this creator in config/creators.json (a box, or false)."""
    c = (_load(CREATORS).get("creators") or {}).get((login or "").lower())
    return c is not None and "facecam" not in c


def suggest(login: str, rec_dir: str, now: float | None = None, state_path: str | None = None,
            detect=None) -> dict | None:
    """A webcam box for a creator nobody has checked: detect + refine on the recording's first minutes and
    a check image. At most once per creator per day; never writes config (the agent looks, then adds it).
    Returns {"box", "confidence", "check_image", "text"} or None (already suggested today / nothing found)."""
    import time as _time
    now = now or _time.time()
    day = dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime("%Y-%m-%d")
    path = state_path or SUGGEST_STATE
    st = _load(path)
    if st.get(login.lower()) == day:
        return None
    res, _, frames = (detect or from_recording)(rec_dir, SUGGEST_AFTER_S / 60, 24)
    st[login.lower()] = day                                  # one try a day, found or not
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(st, open(path, "w"), indent=1)
    if not res.get("box") or res.get("confidence", 0) < GOOD or len(frames) < 2:
        return None
    box = [round(float(v), 3) for v in refine(res["box"], frames)]
    img = os.path.join(rec_dir, "facecam-suggest.jpg")
    try:
        check_image(frames[len(frames) // 2], {"box": box, "confidence": res["confidence"]}, img)
    except Exception:
        return None
    shown = os.path.relpath(img, REPO) if os.path.abspath(img).startswith(REPO) else img
    text = (f"{login}: suggested webcam box [{', '.join(f'{v:g}' for v in box)}] — look at {shown}, "
            "then add it to config/creators.json")
    return {"box": box, "confidence": res["confidence"], "check_image": img, "text": text}


# ---------------------------------------------------------------- CLI helpers

def check_image(frame: np.ndarray, res: dict, out: str) -> str:
    import cv2
    img = frame.copy()
    if res.get("box"):
        x, y, w, h = res["box"]
        cv2.rectangle(img, (int(x * WORK_W), int(y * WORK_H)), (int((x + w) * WORK_W), int((y + h) * WORK_H)),
                      (0, 255, 0), 3)
    if res.get("face"):
        fx, fy, fw, fh = res["face"]
        cv2.rectangle(img, (fx, fy), (fx + fw, fy + fh), (0, 200, 255), 2)
    label = f"facecam {res.get('confidence', 0):.2f}" if res.get("box") else "no facecam"
    cv2.putText(img, label, (12, 34), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 0), 2)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    cv2.imwrite(out, img)
    return out


def from_recording(rec_dir: str, minutes: float = 5.0, n: int = 32) -> tuple[dict, str | None, list]:
    """Sample frames from the last `minutes` of a `clipengine record` folder."""
    from . import live as L
    idx = L.load_index(rec_dir)
    if not idx:
        return {"box": None, "confidence": 0.0, "why": "recording is empty"}, None, []
    login = _load(os.path.join(rec_dir, "recording.json")).get("login")
    t1 = idx[-1]["t"] + idx[-1]["d"]
    t0 = max(idx[0]["t"], t1 - minutes * 60)
    frames = []
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(n):
            t = t0 + (t1 - t0) * (i + 0.5) / n
            ts = os.path.join(tmp, f"{i}.ts")
            try:
                first = L.cut_source(rec_dir, t, t + 1.0, ts)
            except RuntimeError:
                continue
            f = _grab(ts, max(0.0, t - first))
            if f is None:
                f = _grab(ts, 0.0)
            if f is not None:
                frames.append(f)
    return detect_frames(frames) if available() else {"box": None, "confidence": 0.0, "why": "opencv missing"}, login, frames


def from_channel(login: str, minutes: float = 5.0, n: int = 32) -> tuple[dict, list]:
    """Sample frames from the last minutes of the channel's newest VOD (live VODs included)."""
    from . import twitch as T
    v = T.latest_vod(login)
    pls = T.playlists(v["id"])
    pl = pls["video"]
    end = pl.length_s - 5
    start = max(0.0, end - minutes * 60)
    frames = []
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(n):
            t = start + (end - start) * (i + 0.5) / n
            ts = os.path.join(tmp, f"{i}.ts")
            try:
                first = T.download_window(pl, t, t + 0.5, ts, pad_s=0)
            except Exception:
                continue
            f = _grab(ts, max(0.0, t - first))
            if f is not None:
                frames.append(f)
    return detect_frames(frames), frames


def refine(box: list[float], frames: list, search: float = 0.035, inset_px: int = 3) -> list[float]:
    """A clean cut of the webcam: each side of a rough box snaps to the overlay's real border nearby — the line
    that is a sharp edge in EVERY frame (the picture inside changes, the border doesn't) — then moves in a few
    pixels so no border line or game sliver shows. Sides on the frame's edge stay there."""
    gs = [np.asarray(f, dtype=np.float32) for f in frames]
    gs = [g.mean(axis=2) if g.ndim == 3 else g for g in gs]
    if len(gs) < 2:
        return list(box)
    H, W = gs[0].shape
    gx = [np.abs(np.diff(g, axis=1)) for g in gs]
    gy = [np.abs(np.diff(g, axis=0)) for g in gs]
    x0, y0 = box[0] * W, box[1] * H
    x1, y1 = (box[0] + box[2]) * W, (box[1] + box[3]) * H
    sx = (int(y0 + 0.15 * (y1 - y0)), int(y1 - 0.15 * (y1 - y0)))
    sy = (int(x0 + 0.15 * (x1 - x0)), int(x1 - 0.15 * (x1 - x0)))

    def score(i: int, grads: list, span: tuple[int, int], axis: int) -> float:
        return min(float((g[span[0]:span[1], i] if axis == 1 else g[i, span[0]:span[1]]).mean()) for g in grads)

    def snap(c: float, size: int, grads: list, span: tuple[int, int], axis: int, inward: int) -> float:
        reach0 = max(2, int(round(14 * size / (1920 if axis == 1 else 1080))))
        if c <= 2 or c >= size - 2:
            # on the frame's edge: a coloured frame around the webcam still has an inner edge to cut inside
            start = 0 if c <= 2 else size - 2
            for d in range(reach0, 0, -1):
                j = start + inward * d
                if 0 <= j < size - 1 and score(j, grads, span, axis) > 12:
                    return j + 0.5
            return c
        lo, hi = int(max(0, c - search * size)), int(min(size - 2, c + search * size))
        best, at = 0.0, None
        for i in range(lo, hi + 1):
            sc = score(i, grads, span, axis)
            if sc > best:
                best, at = sc, i
        if at is None or best <= 6:
            return c
        # a border is a band: its inner edge (a few px further in) is where the picture starts
        reach = max(2, int(round(14 * size / (1920 if axis == 1 else 1080))))
        for d in range(reach, 0, -1):
            j = at + inward * d
            if 0 <= j < size - 1 and score(j, grads, span, axis) >= 0.35 * best:
                at = j
                break
        return at + 0.5

    L, R = snap(x0, W, gx, sx, 1, +1), snap(x1, W, gx, sx, 1, -1)
    T, B = snap(y0, H, gy, sy, 0, +1), snap(y1, H, gy, sy, 0, -1)
    k = inset_px * W / 1920
    L, R = (L + k if L > 2 else 0.0), (R - k if R < W - 2 else float(W))
    T, B = (T + k if T > 2 else 0.0), (B - k if B < H - 2 else float(H))
    return [round(L / W, 4), round(T / H, 4), round((R - L) / W, 4), round((B - T) / H, 4)]


def cli(rec: str | None = None, channel: str | None = None, out: str | None = None, save_it: bool = True,
        refine_it: bool = False) -> dict:
    if not available():
        raise SystemExit("facecam: opencv is not installed — run bash setup/setup.sh")
    if rec:
        res, login, frames = from_recording(rec)
        source = f"recording {os.path.basename(os.path.normpath(rec))}"
        out = out or os.path.join(rec, "facecam-check.jpg")
    elif channel:
        login = channel.lower()
        res, frames = from_channel(login)
        source = "latest VOD"
        out = out or os.path.join(REPO, "runs", f"facecam-{login}.jpg")
    else:
        raise SystemExit("facecam: pass --rec DIR or --channel LOGIN")
    ov = override(login) if login else None
    if refine_it:
        base = ov or res.get("box")
        if not base or len(frames) < 2:
            raise SystemExit("facecam --refine: no box to refine (set one in config/creators.json first)")
        nb = refine(base, frames)
        check_image(frames[len(frames) // 2], {"box": nb, "confidence": 1.0}, out)
        print(f"facecam {login}: clean cut {nb} (from {base}) — look at the check image: {out}\n"
              f"  if it's right, put it in config/creators.json as \"facecam\": {nb}")
        return {"box": nb, "from": base, "login": login, "check_image": out}
    if frames:
        check_image(frames[len(frames) // 2], res, out)
    good = bool(res.get("box")) and res["confidence"] >= GOOD
    if res.get("box"):
        x, y, w, h = res["box"]
        print(f"facecam {login or ''}: [{x}, {y}, {w}, {h}]  confidence {res['confidence']:.2f} "
              f"({'good' if good else 'too low — renders fall back to stack'}) — {res['why']}")
    else:
        print(f"facecam {login or ''}: none — {res.get('why')}")
    if ov:
        print(f"  note: config/creators.json overrides this with {ov}")
    if save_it and login and res.get("box"):
        save(login, res, source)
        print(f"  saved to {os.path.relpath(CACHE, REPO) if CACHE.startswith(REPO) else CACHE} · check image: "
              f"{os.path.relpath(out, REPO) if out.startswith(REPO) else out}")
    elif frames:
        print(f"  check image: {out}")
    return {**res, "login": login, "check_image": out if frames else None}


def setup_channel(channel: str, approved: str | None = None, no_camera: bool = False) -> bool:
    """Show a refined box and persist it only after an explicit answer."""
    import re
    import sys

    login = channel.strip().casefold()
    if not re.fullmatch(r"(?:[\w-]+:)?@?[\w.-]+", login):
        raise SystemExit("facecam-setup: invalid channel")
    box = False if no_camera else None
    if approved is not None:
        try:
            parsed = json.loads(approved if approved.startswith("[") else f"[{approved}]")
            x, y, w, h = map(float, parsed)
        except (ValueError, TypeError):
            raise SystemExit("facecam-setup: --yes expects x,y,width,height") from None
        if not (x >= 0 and y >= 0 and w > 0 and h > 0 and x + w <= 1.00001 and y + h <= 1.00001):
            raise SystemExit("facecam-setup: the box must fit inside the frame (coordinates 0–1)")
        box = [x, y, w, h]
    if box is None:
        if not available():
            raise SystemExit("facecam-setup: OpenCV is missing. Run bash setup/setup.sh")
        info, frames = from_channel(login)
        proposed = info.get("box")
        if proposed and len(frames) >= 2:
            proposed = refine(proposed, frames)
        if frames:
            name = re.sub(r"[^a-z0-9_-]", "-", login)
            image = os.path.join(REPO, "runs", f"facecam-setup-{name}.jpg")
            os.makedirs(os.path.dirname(image), exist_ok=True)
            check_image(frames[len(frames) // 2], {**info, "box": proposed}, image)
            print(f"facecam {login}: {proposed or 'no box found'} · check image: {image}")
        if not sys.stdin.isatty():
            if proposed:
                print(f"confirm: clipengine facecam-setup --channel {login} --yes {','.join(map(str, proposed))}")
            print(f"no webcam: clipengine facecam-setup --channel {login} --no")
            return False
        if not proposed:
            print("No box found. Use --no if this channel has no webcam.")
            return False
        try:
            answer = input("Use this box? [yes/no, Enter leaves it unchanged] ").strip().lower()
        except EOFError:
            return False
        if answer not in ("y", "yes", "n", "no"):
            return False
        box = proposed if answer in ("y", "yes") else False
    try:
        with open(CREATORS) as stream:
            config = json.load(stream)
    except FileNotFoundError:
        config = {}
    except ValueError:
        raise SystemExit("facecam-setup: fix invalid JSON in config/creators.json before saving") from None
    if not isinstance(config, dict) or not isinstance(config.get("creators", {}), dict):
        raise SystemExit("facecam-setup: config/creators.json must contain a creators object")
    roster = config.setdefault("creators", {})
    roster.setdefault(login, {})["facecam"] = box
    parent = os.path.dirname(CREATORS)
    os.makedirs(parent, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=parent, prefix=".creators-", delete=False) as temp:
        json.dump(config, temp, indent=2)
        temp.write("\n")
    os.replace(temp.name, CREATORS)
    print(f"saved facecam {login}: {box if box else 'no webcam'}")
    return True
