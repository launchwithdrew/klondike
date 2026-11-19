"""Train the sentinel's detector — the crowd labels every past stream for us.

Wherever viewers clipped heavily, something clip-worthy happened. So for
recent VODs of big streamers:

  labels    viewer-clip moments (twitch.crowd_moments): chat-time t is positive
            from 15 s before a clip's end (when someone pressed "clip") to 25 s
            after — the reaction, not the setup; negative when it is more than
            60 s from ANY viewer clip; ambiguous time is left out.
  features  exactly what the live sentinel computes (sentinel.series): distinct
            chatters and message rate vs that streamer's own recent history,
            laugh / hype / shock / "clip it" shares, chat converging on one
            message, junk share.
  model     logistic regression (numpy, class-balanced, L2), trained on chat
            only — audio and live viewer-clip weights stay as set by hand.

It reports, on VODs held out from training, how many of each stream's top-10
crowd moments the sentinel would have fired on (recall) and how many times it
fires on nothing per hour, picks the trigger that keeps false fires near the
target, and writes config/sentinel_model.json.
"""
from __future__ import annotations

import datetime as dt
import json
import os

import numpy as np

from . import sentinel as SN
from . import twitch as T

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHAT_FEATURES = [f for f in SN.FEATURES if f not in ("audio_z", "crowd_new")]


def label(t: float, moments: list[dict]) -> int | None:
    # a viewer clip ends when someone pressed "clip" — chat's reaction is around then, not at
    # the clip's start (that's the setup): positive from 15 s before its end to 25 s after
    for m in moments:
        if m["end"] - 15 <= t <= m["end"] + 25:
            return 1
    for m in moments:
        if m["start"] - 60 <= t <= m["end"] + 60:
            return None
    return 0


def vod_dataset(vod_id: str, hours: float, cache_dir: str, min_views: int = 10) -> dict | None:
    vod = T.Vod.fetch(vod_id)
    end = max(0.0, vod.length_s - 600)                     # skip the live edge (replay incomplete there)
    start = max(0.0, end - hours * 3600)
    os.makedirs(cache_dir, exist_ok=True)
    cache = os.path.join(cache_dir, f"chat-{vod_id}-{int(start)}-{int(end)}.json")
    msgs = T.download_chat(vod_id, start, end, cache=cache)
    if len(msgs) < 500:
        return None
    crowd = [m for m in T.crowd_moments(vod, start, end) if m["views"] >= min_views]
    if len(crowd) < 3:
        return None
    ser = SN.series(msgs, start, end, step=SN.STEP_S)
    X, y, ts = [], [], []
    for row in ser[SN.MIN_HISTORY:]:
        lab = label(row["t"], crowd)
        if lab is None:
            continue
        X.append([row["f"][k] for k in CHAT_FEATURES]); y.append(lab); ts.append(row["t"])
    return {"vod": vod_id, "login": vod.login, "start": start, "end": end, "n_msgs": len(msgs), "crowd": crowd,
            "ser": ser, "X": np.array(X, dtype=np.float64), "y": np.array(y), "t": np.array(ts)}


def fit(X: np.ndarray, y: np.ndarray, l2: float = 0.01, iters: int = 3000, lr: float = 0.1) -> tuple[float, np.ndarray]:
    w = np.zeros(X.shape[1]); b = 0.0
    pos = max(1, y.sum()); neg = max(1, len(y) - y.sum())
    sw = np.where(y == 1, len(y) / (2 * pos), len(y) / (2 * neg))
    for _ in range(iters):
        z = np.clip(X @ w + b, -30, 30)
        p = 1 / (1 + np.exp(-z))
        g = (p - y) * sw
        w -= lr * (X.T @ g / len(y) + l2 * w)
        b -= lr * g.mean()
    return float(b), w


def evaluate(ds: dict, model: dict, top: int = 10) -> dict:
    ser = [{"t": r["t"], "p": SN.prob(r["f"], model) if i >= SN.MIN_HISTORY else 0.0}
           for i, r in enumerate(ds["ser"])]                 # features are model-independent: re-score only
    fires = SN.moments_from_series(ser, model)
    best = sorted(ds["crowd"], key=lambda m: -m["views"])[:top]
    hit = 0
    for m in best:
        if any(m["start"] - 10 <= f["fire"] <= m["end"] + 40 for f in fires):
            hit += 1
    false = sum(1 for f in fires if not any(m["start"] - 60 <= f["fire"] <= m["end"] + 60 for m in ds["crowd"]))
    hours = (ds["end"] - ds["start"]) / 3600
    return {"vod": ds["vod"], "login": ds["login"], "recall": hit / max(1, len(best)), "top": len(best),
            "fires": len(fires), "false_per_h": round(false / hours, 2)}


def pick_vods(logins: list[str]) -> list[str]:
    out = []
    for l in logins:
        try:
            v = T.latest_vod(l)
            if v["length_s"] > 3600:
                out.append(v["id"])
        except Exception as exc:
            print(f"[train] {l}: {exc}")
    return out


def run(logins: list[str] | None = None, vods: list[str] | None = None, hours: float = 4.0,
        target_false_per_h: float = 4.0, write: bool = True, out: str | None = None) -> dict:
    if not vods:
        if not logins:
            from . import radar as R
            cfg = R.config()
            logins = [s["login"] for s in R.live_streams(cfg)[:14]]
        vods = pick_vods(logins)
    cache = os.path.join(REPO, "runs", "train")
    sets = []
    for v in vods:
        try:
            ds = vod_dataset(v, hours, cache)
        except Exception as exc:
            print(f"[train] {v}: skipped ({str(exc)[:80]})")
            continue
        if ds is None:
            print(f"[train] {v}: skipped (too little chat or too few viewer clips)")
            continue
        print(f"[train] {ds['login']:<16} {v}: {ds['n_msgs']:>6} msgs, {len(ds['crowd'])} crowd moments, "
              f"{int(ds['y'].sum())} positive / {len(ds['y'])} steps")
        sets.append(ds)
    if len(sets) < 3:
        raise SystemExit("need at least 3 usable VODs to train")
    base = SN.model()

    def model_from(b, w, trigger):
        m = json.loads(json.dumps(base))
        m["bias"] = round(b, 4)
        for k, v in zip(CHAT_FEATURES, w):
            m["weights"][k] = round(float(v), 4)
        m["trigger"], m["release"] = trigger, round(trigger * 0.5, 3)
        return m

    # held-out evaluation: leave each VOD out once
    held = []
    for i, ds in enumerate(sets):
        tr = [s for j, s in enumerate(sets) if j != i]
        b, w = fit(np.vstack([s["X"] for s in tr]), np.concatenate([s["y"] for s in tr]))
        held.append((ds, b, w))
    best = None
    for trig in (0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95):
        res = [evaluate(ds, model_from(b, w, trig)) for ds, b, w in held]
        rec = float(np.mean([r["recall"] for r in res]))
        fph = float(np.mean([r["false_per_h"] for r in res]))
        print(f"[train] trigger {trig:.2f}: held-out recall {rec:.2f} of top-10 crowd moments, {fph:.1f} false fires/h")
        score = rec - 0.05 * max(0.0, fph - target_false_per_h)
        if best is None or score > best[0]:
            best = (score, trig, rec, fph, res)
    _, trig, rec, fph, res = best
    b, w = fit(np.vstack([s["X"] for s in sets]), np.concatenate([s["y"] for s in sets]))
    m = model_from(b, w, trig)
    m["eval"] = {"trained": dt.datetime.now(dt.timezone.utc).isoformat(), "vods": [s["vod"] for s in sets],
                 "held_out_recall_top10": round(rec, 3), "false_fires_per_hour": round(fph, 2),
                 "per_vod": res}
    m["_about"] = SN.DEFAULT_MODEL["_about"]
    print(f"[train] chosen trigger {trig}: recall {rec:.2f}, {fph:.1f} false fires/h (held out)")
    print("[train] weights: " + ", ".join(f"{k} {m['weights'][k]:+.2f}" for k in CHAT_FEATURES) + f", bias {m['bias']:+.2f}")
    if write:
        dest = out or SN.MODEL
        os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
        json.dump(m, open(dest, "w"), indent=1)
        print(f"[train] wrote {os.path.relpath(dest, REPO)}")
    return m
