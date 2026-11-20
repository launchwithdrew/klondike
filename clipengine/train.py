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

It reports, on STREAMERS held out from training (each streamer's streams are
scored by a model that never saw that streamer), how many of each stream's
top-10 crowd moments the sentinel would have fired on (recall) and how many
times it fires on nothing per hour. It picks the trigger, release and cooldown
with the best recall inside the false-fire budget, scores the model already in
use on the same streams, and writes config/sentinel_model.json only when the
new one is at least as good there (or with force=True).

Which stretch of a stream: the <= `hours` of GTA VI play with the most viewer
clips (labels are densest there); a stream with no 1 h GTA VI stretch is
skipped. Labels read the neighbouring clip periods too (twitch.crowd_moments
wide=True), since Twitch lists only a channel's top 100 clips per period.
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


GAME = "Grand Theft Auto VI"


def window(vod: T.Vod, crowd: list[dict], hours: float, game: str = GAME, min_views: int = 10,
           min_span_s: float = 3600) -> tuple[int, float, float] | None:
    """(clipped moments, start, end): the <= `hours` stretch of `game` play holding the most viewer clips with
    >= min_views, away from the live edge. None when the stream has no `min_span_s` stretch of that game."""
    spans = [(c.start_s, min(c.end_s, vod.length_s - 600)) for c in vod.chapters if game in c.game]
    if not spans and game in vod.game:
        spans = [(0.0, vod.length_s - 600.0)]
    best = None
    for a, b in spans:
        if b - a < min_span_s:
            continue
        span = min(hours * 3600, b - a)
        for s0 in [a] + [m["start"] - 300 for m in crowd]:
            s0 = max(a, min(s0, b - span))
            e0 = s0 + span
            n = sum(1 for m in crowd if m["views"] >= min_views and s0 + 120 <= m["end"] <= e0)
            if best is None or n > best[0]:
                best = (n, s0, e0)
    return best


def vod_dataset(vod_id: str, hours: float, cache_dir: str, min_views: int = 10, game: str = GAME) -> dict | None:
    vod = T.Vod.fetch(vod_id)
    every = T.crowd_moments(vod, wide=True)
    w = window(vod, every, hours, game, min_views)
    if w is None or w[0] < 3:
        return None
    _, start, end = w
    os.makedirs(cache_dir, exist_ok=True)
    cache = os.path.join(cache_dir, f"chat-{vod_id}-{int(start)}-{int(end)}.json")
    msgs = T.download_chat(vod_id, start, end, cache=cache)
    if len(msgs) < 500:
        return None
    near = [m for m in every if start - 60 <= m["start"] < end]
    crowd = [m for m in near if m["views"] >= min_views]
    ser = SN.series(msgs, start, end, step=SN.STEP_S)
    X, y = design(ser, crowd, near)
    return {"vod": vod_id, "login": vod.login, "start": start, "end": end, "n_msgs": len(msgs), "crowd": crowd,
            "crowd_all": near, "ser": ser, "X": X, "y": y}


def design(ser: list[dict], crowd: list[dict], every: list[dict] | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Training rows: positives around the crowd moments; time near ANY viewer clip (even one under the views
    bar) is left out rather than called a negative."""
    X, y = [], []
    for row in ser[SN.MIN_HISTORY:]:
        lab = label(row["t"], crowd)
        if lab == 0 and every and label(row["t"], every) is None:
            lab = None
        if lab is None:
            continue
        X.append([row["f"][k] for k in CHAT_FEATURES]); y.append(lab)
    return np.array(X, dtype=np.float64).reshape(-1, len(CHAT_FEATURES)), np.array(y)


def fit(X: np.ndarray, y: np.ndarray, l2: float = 0.01, iters: int = 3000, lr: float = 0.1) -> tuple[float, np.ndarray]:
    """Class-balanced L2 logistic regression. Fitted on standardised features (features differ in scale by 10×:
    plain gradient descent under-fits the small ones), returned in raw-feature units."""
    mu, sd = X.mean(0), X.std(0) + 1e-6
    Z = (X - mu) / sd
    w = np.zeros(X.shape[1]); b = 0.0
    pos = max(1, y.sum()); neg = max(1, len(y) - y.sum())
    sw = np.where(y == 1, len(y) / (2 * pos), len(y) / (2 * neg))
    for _ in range(iters):
        z = np.clip(Z @ w + b, -30, 30)
        p = 1 / (1 + np.exp(-z))
        g = (p - y) * sw
        w -= lr * (Z.T @ g / len(y) + l2 * w)
        b -= lr * g.mean()
    w = w / sd
    return float(b - mu @ w), w


def scored(ds: dict, model: dict) -> list[dict]:
    """The model's probability at every step (trigger-independent, so a sweep scores each stream once)."""
    return [{"t": r["t"], "p": SN.prob(r["f"], model) if i >= SN.MIN_HISTORY else 0.0}
            for i, r in enumerate(ds["ser"])]                 # features are model-independent: re-score only


def evaluate(ds: dict, model: dict, top: int = 10, ser: list[dict] | None = None) -> dict:
    fires = SN.moments_from_series(ser if ser is not None else scored(ds, model), model)
    best = sorted(ds["crowd"], key=lambda m: -m["views"])[:top]
    hit = 0
    for m in best:
        if any(m["start"] - 10 <= f["fire"] <= m["end"] + 40 for f in fires):
            hit += 1
    false = sum(1 for f in fires if not any(m["start"] - 60 <= f["fire"] <= m["end"] + 60
                                             for m in ds.get("crowd_all") or ds["crowd"]))
    hours = (ds["end"] - ds["start"]) / 3600
    return {"vod": ds["vod"], "login": ds["login"], "recall": hit / max(1, len(best)), "top": len(best),
            "fires": len(fires), "false_per_h": round(false / hours, 2)}


def pick_vods(logins: list[str], per: int = 2, game: str = GAME) -> list[str]:
    """Each streamer's newest finished broadcasts with an hour or more of `game` (default 2 per streamer)."""
    out = []
    for l in logins:
        try:
            u = T.gql(_RECENT, {"login": l.lower()}).get("user") or {}
        except Exception as exc:
            print(f"[train] {l}: {exc}")
            continue
        got = 0
        for e in ((u.get("videos") or {}).get("edges") or []):
            n = e["node"]
            if got >= per or n.get("status") == "RECORDING":             # still live: clips not all in
                continue
            if int(n["lengthSeconds"]) > 3600 and (game in ((n.get("game") or {}).get("displayName") or "")
                                                  or any(game in (((c["node"].get("details") or {}).get("game") or {})
                                                                  .get("displayName") or "")
                                                         for c in ((n.get("moments") or {}).get("edges") or []))):
                out.append(n["id"]); got += 1
    return out


_RECENT = """query($login: String!) { user(login: $login) { login
  videos(first: 8, type: ARCHIVE, sort: TIME) { edges { node { id lengthSeconds status game { displayName }
    moments(momentRequestType: VIDEO_CHAPTER_MARKERS) { edges { node { details {
      ... on GameChangeMomentDetails { game { displayName } } } } } } } } } } }"""

TRIGGERS = tuple(float(round(x, 2)) for x in np.arange(0.5, 0.97, 0.02))
RELEASES = (0.5, 0.9, 0.95)           # release = trigger × this
COOLDOWNS = (20.0, 30.0, 60.0)


def held_out(sets: list[dict], l2: float = 0.01) -> list[tuple[float, np.ndarray]]:
    """(bias, weights) per dataset from a model that never saw that dataset's STREAMER (leave-one-streamer-out;
    leave-one-stream-out when there are fewer than 3 streamers)."""
    group = [s["login"] for s in sets] if len({s["login"] for s in sets}) >= 3 else [s["vod"] for s in sets]
    fits: dict[str, tuple[float, np.ndarray]] = {}
    for g in dict.fromkeys(group):
        tr = [s for s, k in zip(sets, group) if k != g]
        fits[g] = fit(np.vstack([s["X"] for s in tr]), np.concatenate([s["y"] for s in tr]), l2=l2)
    return [fits[k] for k in group]


def model_from(base: dict, b: float, w: np.ndarray, trigger: float, release: float, cool: float) -> dict:
    m = json.loads(json.dumps(base))
    m["bias"] = round(b, 4)
    for k, v in zip(CHAT_FEATURES, w):
        m["weights"][k] = round(float(v), 4)
    m["trigger"], m["release"], m["cooldown_s"] = trigger, round(release, 3), cool
    return m


def sweep(sets: list[dict], models: list[dict], target: float) -> dict:
    """Best (trigger, release ratio, cooldown) for the given per-dataset models: the highest mean recall with
    mean false fires/h <= target (else the fewest false fires)."""
    best = None
    sers = [scored(ds, m) for ds, m in zip(sets, models)]
    for rr in RELEASES:
        for cool in COOLDOWNS:
            for trig in TRIGGERS:
                res = [evaluate(ds, {**m, "trigger": trig, "release": trig * rr, "cooldown_s": cool}, ser=sr)
                       for ds, m, sr in zip(sets, models, sers)]
                rec = float(np.mean([r["recall"] for r in res]))
                fph = float(np.mean([r["false_per_h"] for r in res]))
                key = (fph <= target, rec if fph <= target else -fph)
                if best is None or key > best["key"]:
                    best = {"key": key, "trigger": trig, "release_ratio": rr, "cooldown_s": cool, "recall": rec,
                            "fph": fph, "per_vod": res}
    return best


def run(logins: list[str] | None = None, vods: list[str] | None = None, hours: float = 4.0,
        target_false_per_h: float = 4.0, write: bool = True, out: str | None = None, force: bool = False,
        per: int = 2) -> dict:
    if not vods:
        if not logins:
            from . import radar as R
            cfg = R.config()
            logins = [s["login"] for s in R.live_streams(cfg)[:14]]
        vods = pick_vods(logins, per)
    cache = os.path.join(REPO, "runs", "train")
    sets = []
    for v in vods:
        try:
            ds = vod_dataset(v, hours, cache)
        except Exception as exc:
            print(f"[train] {v}: skipped ({str(exc)[:80]})")
            continue
        if ds is None:
            print(f"[train] {v}: skipped (no 1 h GTA VI stretch with 3+ viewer-clipped moments, or too little chat)")
            continue
        print(f"[train] {ds['login']:<16} {v}: {ds['n_msgs']:>6} msgs, {len(ds['crowd'])} crowd moments, "
              f"{int(ds['y'].sum())} positive / {len(ds['y'])} steps")
        sets.append(ds)
    if len(sets) < 3:
        raise SystemExit("need at least 3 usable VODs to train")
    base = SN.model()
    streamers = len({s["login"] for s in sets})
    held = held_out(sets)
    new = sweep(sets, [model_from(base, b, w, 0.7, 0.35, SN.COOLDOWN_S) for b, w in held], target_false_per_h)
    old = sweep(sets, [base] * len(sets), target_false_per_h)
    cur = [evaluate(ds, base) for ds in sets]
    cur_rec, cur_fph = float(np.mean([r["recall"] for r in cur])), float(np.mean([r["false_per_h"] for r in cur]))
    how = "held-out streamers" if streamers >= 3 else "held-out streams"
    print(f"[train] {len(sets)} streams, {streamers} streamers, {sum(len(s['crowd']) for s in sets)} crowd moments")
    print(f"[train] model in use, as set:   recall {cur_rec:.2f} of top-10 crowd moments, {cur_fph:.1f} false fires/h")
    print(f"[train] model in use, retuned:  recall {old['recall']:.2f}, {old['fph']:.1f}/h")
    print(f"[train] new model ({how}): recall {new['recall']:.2f}, {new['fph']:.1f}/h — trigger {new['trigger']}, "
          f"release ×{new['release_ratio']}, cooldown {new['cooldown_s']:.0f} s")
    b, w = fit(np.vstack([s["X"] for s in sets]), np.concatenate([s["y"] for s in sets]))
    m = model_from(base, b, w, new["trigger"], new["trigger"] * new["release_ratio"], new["cooldown_s"])
    m["eval"] = {"trained": dt.datetime.now(dt.timezone.utc).isoformat(), "vods": [s["vod"] for s in sets],
                 "streamers": sorted({s["login"] for s in sets}), "crowd_moments": sum(len(s["crowd"]) for s in sets),
                 "held_out": how, "held_out_recall_top10": round(new["recall"], 3),
                 "false_fires_per_hour": round(new["fph"], 2), "target_false_per_h": target_false_per_h,
                 "in_use": {"recall": round(cur_rec, 3), "false_per_h": round(cur_fph, 2),
                            "retuned_recall": round(old["recall"], 3), "retuned_false_per_h": round(old["fph"], 2)},
                 "per_vod": new["per_vod"],
                 "note": "a 'false' fire is one no viewer clipped — some are real moments the crowd missed"}
    m["_about"] = SN.DEFAULT_MODEL["_about"]
    print("[train] weights: " + ", ".join(f"{k} {m['weights'][k]:+.2f}" for k in CHAT_FEATURES) + f", bias {m['bias']:+.2f}")
    worse = new["recall"] + 1e-9 < old["recall"] and old["fph"] <= target_false_per_h
    if worse and not force:
        print(f"[train] not written: the model in use, retuned, does better on these streams ({old['recall']:.2f} vs "
              f"{new['recall']:.2f}) — `train --force` writes it anyway")
        m["written"] = False
        return m
    if write:
        dest = out or SN.MODEL
        os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
        json.dump(m, open(dest, "w"), indent=1)
        print(f"[train] wrote {os.path.relpath(dest, REPO)}")
    m["written"] = bool(write)
    return m
