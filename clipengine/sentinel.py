"""Live sentinel — catch a moment within ~30 s of its peak, not after the crowd.

One process per streamer (`clipengine sentinel --channel <login>`), started by
the operator from the radar and restarted by it if it dies. It runs three
listeners and one judge:

  chat      Twitch's live chat connection (IRC over websocket, anonymous,
            read-only) — every message as it's sent; the VOD chat replay lags
            minutes at the live edge and drops most messages there.
  recorder  its own recording of the stream (live.record), so the moment can
            be cut seconds after it happens, VOD or not.
  crowd     the channel's viewer clips, polled every minute: new clips appearing
            is the crowd confirming.
  judge     every STEP_S seconds: chat features for the last WINDOW_S seconds,
            compared with this streamer's own recent history (robust z-scores),
            plus audio loudness and new viewer clips -> a probability from the
            trained model (config/sentinel_model.json, written by `train`).
            Fires when it stays over the trigger; the moment ends when it falls
            back; then a cool-down.

On each moment it prepares a draft by itself: cut from the recording,
transcribe, safety + vision checks, engine score, a contact sheet, and ONE line
in <out>/queue.jsonl. `clipengine queue` lists the drafts worth a look;
`clipengine publish-queued` turns one into a post. Nothing posts on its own.
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import math
import os
import re
import statistics
import threading
import time
import urllib.parse

from . import signals as S

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL = os.path.join(REPO, "config", "sentinel_model.json")

WINDOW_S = 10.0          # chat window judged at each step
STEP_S = 5.0
HISTORY_S = 1800.0       # the streamer's own baseline: last 30 minutes
MIN_HISTORY = 24         # windows needed before judging (2 min)
CROWD_DRAFT_MIN_VIEWS = 10  # a viewer clip with this many views is drafted even without a live spike
REACTION_DELAY_S = 7.0   # chat reacts this long after the moment (stream delay + typing)
PRE_ROLL_S = 22.0        # setup before the reaction
POST_ROLL_S = 6.0
MAX_LEN_S = 75.0
COOLDOWN_S = 60.0

# Surges, not shares: what marks a moment is MORE people laughing / shocked / hyped than this
# streamer's chat normally has, and chat accelerating — measured against its own last 30 minutes.
FEATURES = ("z_users", "z_msgs", "z_laugh", "z_shock", "z_hype", "accel", "clip", "conv", "junk",
            "surge", "audio_z", "crowd_new")
HIST_KEYS = ("users", "msgs", "laugh_n", "shock_n", "hype_n")
# Memory: the same surge features as they were 10 s and 20 s ago, so the model can tell a moment
# STARTING (low a moment ago, high now) from chat that is still loud after one.
LAG_KEYS = ("z_users", "z_laugh", "z_shock")
LAGS = (2, 4)                      # steps back (STEP_S each): 10 s, 20 s
FEATURES = FEATURES + tuple(f"{k}@-{l * int(STEP_S)}" for l in LAGS for k in LAG_KEYS)

DEFAULT_MODEL = {
    "_about": "Logistic model over sentinel features. Chat weights are learned by `clipengine train` from "
              "past streams (label: viewers clipped it); audio_z and crowd_new are set by hand.",
    "bias": -4.0,
    "weights": {"z_users": 0.4, "z_msgs": 0.1, "z_laugh": 0.35, "z_shock": 0.25, "z_hype": 0.1, "accel": 0.5,
                "clip": 0.4, "conv": 0.5, "junk": -1.0, "surge": 0.5, "audio_z": 0.35, "crowd_new": 1.2},
    "trigger": 0.6, "release": 0.3,
}


def model() -> dict:
    m = json.loads(json.dumps(DEFAULT_MODEL))
    if os.path.exists(MODEL):
        user = json.load(open(MODEL))
        m.update({k: v for k, v in user.items() if k != "weights"})
        m["weights"].update(user.get("weights", {}))
    return m


# ---- features (pure: shared by the live judge and by training) ------------------------------

def window_raw(msgs: list[dict]) -> dict:
    """Raw chat measures for one window of messages ({t, user, text})."""
    n = len(msgs)
    if not n:
        return {"users": 0, "msgs": 0, "laugh_n": 0, "shock_n": 0, "hype_n": 0, "clip_n": 0, "conv": 0.0, "junk": 0.0}
    fam = collections.Counter()
    canon = collections.Counter()
    for m in msgs:
        f = S.classify(m)
        for k in f:
            fam[k] += 1
        canon[S._canon(m.get("text") or "")] += 1
    users = len({m.get("user") for m in msgs})
    top = canon.most_common(1)[0][1] if canon else 0
    junk = fam["command"] + fam["greet"] + fam["support"]
    return {"users": users, "msgs": n, "laugh_n": fam["laugh"], "shock_n": fam["shock"], "hype_n": fam["hype"],
            "clip_n": fam["clip"], "conv": (top / n) if top >= 3 else 0.0, "junk": junk / n}


def _robust_z(x: float, hist: list[float]) -> float:
    if len(hist) < 3:
        return 0.0
    med = statistics.median(hist)
    mad = statistics.median(abs(h - med) for h in hist)
    return max(-3.0, min(8.0, (x - med) / (1.4826 * mad + 1.0)))


def features(raw: dict, hist: dict[str, list[float]], audio_z: float = 0.0, crowd_new: float = 0.0) -> dict:
    """hist: this streamer's recent windows, {key: [values]} for HIST_KEYS (oldest first)."""
    recent = hist.get("users", [])[-4:]
    accel = raw["users"] / (sum(recent) / len(recent) + 1.0) - 1.0 if recent else 0.0
    return {"z_users": _robust_z(raw["users"], hist.get("users", [])),
            "z_msgs": _robust_z(raw["msgs"], hist.get("msgs", [])),
            "z_laugh": _robust_z(raw["laugh_n"], hist.get("laugh_n", [])),
            "z_shock": _robust_z(raw["shock_n"], hist.get("shock_n", [])),
            "z_hype": _robust_z(raw["hype_n"], hist.get("hype_n", [])),
            "accel": max(-1.0, min(5.0, accel)), "clip": min(5.0, float(raw["clip_n"])),
            "conv": raw["conv"], "junk": raw["junk"],
            # more people AND more of them reacting, at once: the shape of a real moment
            "surge": max(0.0, _robust_z(raw["users"], hist.get("users", []))) *
                     max(0.0, max(_robust_z(raw["laugh_n"], hist.get("laugh_n", [])),
                                   _robust_z(raw["shock_n"], hist.get("shock_n", [])))) / 8.0,
            "audio_z": audio_z, "crowd_new": crowd_new}


def _push(hist: dict[str, list[float]], raw: dict, keep: int) -> None:
    for k in HIST_KEYS:
        hist.setdefault(k, []).append(raw[k])
        if len(hist[k]) > keep:
            del hist[k][: len(hist[k]) - keep]


def with_lags(f: dict, past: list[dict]) -> dict:
    """Add the lagged copies of LAG_KEYS from `past` (older feature dicts, newest last)."""
    out = dict(f)
    for l in LAGS:
        prev = past[-l] if len(past) >= l else {}
        for k in LAG_KEYS:
            out[f"{k}@-{l * int(STEP_S)}"] = prev.get(k, 0.0)
    return out


def prob(f: dict, m: dict) -> float:
    z = m["bias"] + sum(m["weights"].get(k, 0.0) * f.get(k, 0.0) for k in FEATURES)
    return 1 / (1 + math.exp(-max(-30, min(30, z))))


def series(msgs: list[dict], start: float, end: float, m: dict | None = None,
           step: float = STEP_S) -> list[dict]:
    """Slide the judge over a chat log (offsets in seconds) -> [{t, p, f}] (used by training/eval)."""
    m = m or model()
    msgs = sorted(msgs, key=lambda x: x["t"])
    out, hist = [], {}
    i0 = 0
    t = start + WINDOW_S
    keep = int(HISTORY_S / step)
    while t <= end:
        while i0 < len(msgs) and msgs[i0]["t"] < t - WINDOW_S:
            i0 += 1
        j = i0
        win = []
        while j < len(msgs) and msgs[j]["t"] < t:
            win.append(msgs[j]); j += 1
        raw = window_raw(win)
        f = with_lags(features(raw, hist), [r["f"] for r in out[-max(LAGS):]])
        n_hist = len(hist.get("users", []))
        out.append({"t": t, "p": prob(f, m) if n_hist >= MIN_HISTORY else 0.0, "f": f})
        _push(hist, raw, keep)
        t += step
    return out


def cooldown(m: dict) -> float:
    """Seconds after a moment ends before the next can fire. A trained model sets its own (`cooldown_s`): a long
    one plus a low release kept the trigger busy through the loud chat AFTER a moment, so the next real one in a
    busy stretch never fired (F1, Nov 20: re-arming quickly recovered ~a fifth of the missed moments)."""
    return float(m.get("cooldown_s", COOLDOWN_S))


def moments_from_series(ser: list[dict], m: dict | None = None) -> list[dict]:
    """Trigger/release over a probability series -> [{start, peak, end, p}] in the series' clock."""
    m = m or model()
    cool = cooldown(m)
    out, cur, last_end = [], None, -1e9
    for row in ser:
        t, p = row["t"], row["p"]
        if cur is None:
            if p >= m["trigger"] and t - last_end >= cool:
                cur = {"fire": t, "peak": t, "p": p, "end": t}
        else:
            if p > cur["p"]:
                cur["p"], cur["peak"] = p, t
            if p >= m["release"]:
                cur["end"] = t
            if p < m["release"] and t - cur["end"] >= STEP_S * 2 or t - cur["fire"] >= MAX_LEN_S:
                out.append(cur); last_end = cur["end"]; cur = None
    if cur:
        out.append(cur)
    for c in out:                                  # chat time -> stream time
        c["start"] = c["fire"] - REACTION_DELAY_S - PRE_ROLL_S
        c["stop"] = min(c["end"] - REACTION_DELAY_S + POST_ROLL_S, c["start"] + MAX_LEN_S)
    return out


# ---- live listeners ---------------------------------------------------------------------------

def _ws_kwargs() -> dict:
    p = urllib.parse.urlparse(os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "")
    kw: dict = {}
    if p.hostname:
        kw = {"http_proxy_host": p.hostname, "http_proxy_port": p.port, "proxy_type": "http"}
        if p.username:
            kw["http_proxy_auth"] = (urllib.parse.unquote(p.username), urllib.parse.unquote(p.password or ""))
    ca = os.environ.get("SSL_CERT_FILE") or ("/root/.ccr/ca-bundle.crt" if os.path.exists("/root/.ccr/ca-bundle.crt") else None)
    if ca:
        kw["sslopt"] = {"ca_certs": ca}
    return kw


class Chat(threading.Thread):
    """Anonymous read-only Twitch chat; keeps the last HISTORY_S seconds of messages."""
    def __init__(self, login: str):
        super().__init__(daemon=True)
        self.login, self.msgs, self.lock = login.lower(), collections.deque(), threading.Lock()
        self.connected, self.total, self.stop_flag = False, 0, False

    def run(self):
        import websocket
        delay = 2
        while not self.stop_flag:
            try:
                ws = websocket.create_connection("wss://irc-ws.chat.twitch.tv:443", timeout=15, **_ws_kwargs())
                ws.send("CAP REQ :twitch.tv/tags")
                ws.send("PASS SCHMOOPIIE")
                ws.send(f"NICK justinfan{int(time.time()) % 90000 + 10000}")
                ws.send(f"JOIN #{self.login}")
                self.connected, delay = True, 2
                while not self.stop_flag:
                    try:
                        data = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        ws.send("PING :tmi.twitch.tv")
                        continue
                    now = time.time()
                    for line in data.split("\r\n"):
                        if line.startswith("PING"):
                            ws.send("PONG :tmi.twitch.tv")
                        elif " PRIVMSG " in line:
                            tags = dict(kv.split("=", 1) for kv in line[1:].split(" ", 1)[0].split(";") if "=" in kv) \
                                if line.startswith("@") else {}
                            text = line.split(" PRIVMSG ", 1)[1].split(" :", 1)[-1]
                            user = tags.get("user-id") or tags.get("display-name") or \
                                line.split("!", 1)[0].lstrip("@:").split(" ")[-1]
                            emotes = len(re.findall(r"\d+-\d+", tags.get("emotes", "")))
                            with self.lock:
                                self.msgs.append({"t": now, "user": user, "text": text, "emotes": emotes})
                                self.total += 1
                                while self.msgs and self.msgs[0]["t"] < now - HISTORY_S - WINDOW_S:
                                    self.msgs.popleft()
            except Exception as exc:
                self.connected = False
                print(f"[sentinel] chat reconnect in {delay}s ({str(exc)[:80]})", flush=True)
                time.sleep(delay)
                delay = min(60, delay * 2)

    def window(self, t0: float, t1: float) -> list[dict]:
        with self.lock:
            return [m for m in self.msgs if t0 <= m["t"] < t1]


class Crowd(threading.Thread):
    """New viewer clips for the channel, by creation time (polled every minute)."""
    def __init__(self, login: str):
        super().__init__(daemon=True)
        self.login, self.seen, self.new, self.stop_flag = login.lower(), None, [], False

    def run(self):
        from . import twitch as T
        q = """query($l: String!) { user(login: $l) { clips(first: 100, criteria: {period: LAST_DAY, sort: VIEWS_DESC}) {
          edges { node { slug title createdAt viewCount } } } } }"""
        while not self.stop_flag:
            try:
                edges = (((T.gql(q, {"l": self.login}).get("user") or {}).get("clips") or {}).get("edges") or [])
                nodes = [e["node"] for e in edges]
                slugs = {n["slug"] for n in nodes}
                if self.seen is not None:
                    for n in nodes:
                        if n["slug"] not in self.seen:
                            try:
                                ts = dt.datetime.fromisoformat(n["createdAt"].replace("Z", "+00:00")).timestamp()
                            except ValueError:
                                ts = time.time()
                            self.new.append({"t": ts, "title": n.get("title") or "", "views": n.get("viewCount", 0)})
                self.seen = (self.seen or set()) | slugs
            except Exception:
                pass
            time.sleep(60)

    def count(self, t0: float, t1: float) -> int:
        return sum(1 for c in self.new if t0 <= c["t"] < t1)


def _audio_db(path: str) -> float | None:
    try:
        from . import audio as A
        x = A.decode_pcm(path, 0, None)
        if x is None or len(x) == 0:
            return None
        import numpy as np
        return float(20 * np.log10(np.sqrt(np.mean(np.square(x))) + 1e-9))
    except Exception:
        return None


# ---- the judge ---------------------------------------------------------------------------------

class _Silent:
    """Stand-in for chat/crowd on a source that has neither (YouTube): audio decides alone."""
    connected = True

    def window(self, t0, t1):
        return []

    def count(self, t0, t1):
        return 0


def run(login: str, out: str, buffer_h: float = 3.0, draft: bool = True, model_name: str | None = None,
        record: bool = True) -> None:
    """`login`: a Twitch login, or a YouTube source ("yt:@handle" / URL) — YouTube is audio-only."""
    from . import live as L
    from . import ops as O
    model_name = model_name or O.whisper_model()          # the lite profile drafts with Whisper tiny
    source = login if L.is_youtube(login) else login.lower()
    youtube = L.is_youtube(source)
    kick = source.startswith("kick:")
    login = L.source_name(source)
    os.makedirs(out, exist_ok=True)
    m = model()
    if record:      # record=False: `cycle`/`start` run the recorder as its own process, which outlives sentinel restarts
        threading.Thread(target=L.record_supervised, args=(source, out, buffer_h * 3600), daemon=True).start()
    if youtube:                                      # live chat when it can be read; audio-only otherwise
        from . import youtube as YT
        chat, crowd = YT.YouTubeChat(source), _Silent()
        chat.start()
    elif kick:                                       # Kick: its Pusher chat behind the same interface
        from . import kick as K
        chat, crowd = K.KickChat(source), _Silent()
        chat.start()
    else:
        chat, crowd = Chat(login), Crowd(login)
        chat.start(); crowd.start()
    hist: dict[str, list[float]] = {}
    past: list[dict] = []
    hdb: list[float] = []
    keep = int(HISTORY_S / STEP_S)
    if not youtube and not kick:
        try:
            filled = warm_history(login, hist, keep)
            if filled:
                print(f"[sentinel] {login}: warm start — {filled * STEP_S / 60:.0f} min of chat history from the "
                      "stream's replay", flush=True)
        except Exception as exc:
            print(f"[sentinel] {login}: no warm start ({str(exc)[:80]}) — building the baseline live", flush=True)
    p_hist: collections.deque = collections.deque(maxlen=int(3600 / STEP_S))   # (t, p) for crowd drafts
    drafted: list[tuple] = []
    crowd_checked = 0.0
    audio_seen: dict[str, float] = {}
    cur, last_end, fired = None, 0.0, 0
    status_path = os.path.join(out, "sentinel.json")
    print(f"[sentinel] {login}: listening (trigger p ≥ {m['trigger']}) -> {out}", flush=True)
    threading.Thread(target=_facecam_suggest, args=(login, out, time.time()), daemon=True).start()
    t_next = time.time() + STEP_S
    last_beat = 0.0
    while True:
        time.sleep(max(0.0, t_next - time.time()))
        now, t_next = time.time(), t_next + STEP_S
        # audio: loudness of recorded segments not measured yet
        try:
            for r in L.load_index(out)[-6:]:
                if r["f"] not in audio_seen:
                    db = _audio_db(os.path.join(out, "segments", r["f"]))
                    if db is not None:
                        audio_seen[r["f"]] = db
                        hdb.append(db)
                        hdb = hdb[-keep:]
        except Exception:
            pass
        audio_z = _robust_z(hdb[-1], hdb[:-1]) if len(hdb) > 10 else 0.0
        raw = window_raw(chat.window(now - WINDOW_S, now))
        f = with_lags(features(raw, hist, audio_z, crowd.count(now - 120, now)), past)
        past = (past + [f])[-max(LAGS):]
        if youtube:                                  # a sustained loudness surge, and the chat model once chat is in
            p = 1 / (1 + math.exp(-(-4.0 + 1.6 * audio_z))) if len(hdb) > 30 else 0.0
            if chat.connected and len(hist.get("users", [])) >= MIN_HISTORY and sum(hist.get("msgs", [])[-24:]) > 0:
                p = max(p, prob(f, m))
        else:
            p = prob(f, m) if len(hist.get("users", [])) >= MIN_HISTORY else 0.0
        _push(hist, raw, keep)
        p_hist.append((now, p))
        # moments viewers already clipped while we were recording: draft them now, no live spike needed
        if draft and not youtube and not kick and crowd.new and now - crowd_checked >= 60:
            crowd_checked = now
            try:
                for cm in L.live_moments(out):
                    w = (cm["start"] - 3, cm["end"] + 3)
                    if cm["views"] < CROWD_DRAFT_MIN_VIEWS or _overlaps(w, drafted) or (cur and w[1] >= cur["fire"] - 60):
                        continue
                    drafted.append(w)
                    pm = max([q for tq, q in p_hist if w[0] - 10 <= tq <= w[1] + REACTION_DELAY_S] or [0.0])
                    moment = {"start": w[0], "stop": w[1], "p": round(max(pm, 0.5), 3), "fire": cm["end"],
                              "crowd_end": cm["end"], "features": None, "crowd_new": cm["clips"], "chat": chat.window(w[0], w[1] + 20),
                              "source": "viewer clips"}
                    print(f"[sentinel] {login}: viewers clipped a moment ({cm['views']} views) — drafting it", flush=True)
                    threading.Thread(target=_draft_safe, args=(out, login, moment, model_name), daemon=True).start()
            except Exception as exc:
                print(f"[sentinel] crowd draft check failed ({str(exc)[:80]})", flush=True)
        if cur is None:
            if p >= m["trigger"] and now - last_end >= cooldown(m):
                cur = {"fire": now, "peak": now, "p": p, "end": now, "chat": chat.window(now - 20, now),
                       "features": {k: round(v, 3) for k, v in f.items()}}
                print(f"[sentinel] {login}: FIRED p={p:.2f} {json.dumps({k: round(v, 2) for k, v in f.items()})}", flush=True)
        else:
            if p > cur["p"]:
                cur["p"], cur["peak"] = p, now
            if p >= m["release"]:
                cur["end"] = now
                cur["chat"] = (cur["chat"] + chat.window(now - STEP_S, now))[-400:]
            if (p < m["release"] and now - cur["end"] >= 2 * STEP_S) or now - cur["fire"] >= MAX_LEN_S:
                start = cur["fire"] - REACTION_DELAY_S - PRE_ROLL_S
                stop = min(cur["end"] - REACTION_DELAY_S + POST_ROLL_S, start + MAX_LEN_S)
                moment = {"start": start, "stop": stop, "p": round(cur["p"], 3), "fire": cur["fire"], "peak": cur["peak"],
                          "features": cur.get("features"),
                          "crowd_new": crowd.count(start, now + 60), "chat": cur["chat"]}
                last_end, cur, fired = now, None, fired + 1
                drafted.append((start, stop))
                if draft:
                    threading.Thread(target=_draft_safe, args=(out, login, moment, model_name), daemon=True).start()
        if now - last_beat >= 30:
            if int(now // 30) % 20 == 0:                   # every ~10 min: is the autopilot still alive?
                try:
                    from . import autopilot as AP
                    AP.revive_if_dead(login)
                except Exception:
                    pass
            last_beat = now
            json.dump({"login": login, "pid": os.getpid(), "beat": now,
                       "chat_connected": bool(chat.connected or getattr(chat, "fallback", False)),
                       "chat_mode": ("audio-only" if getattr(chat, "fallback", False) else "live chat") if youtube else "chat",
                       "chat_per_min": round(sum(hist.get("msgs", [])[-12:]) * (60 / (12 * WINDOW_S)), 1),
                       "recorded_min": round(len(L.load_index(out)) * 2 / 60, 1) if os.path.exists(os.path.join(out, "index.jsonl")) else 0,
                       "moments": fired, "p": round(p, 3), "rec": _rec_health(out)}, open(status_path, "w"))


def _facecam_suggest(login: str, out: str, started: float) -> None:
    """A creator with no "facecam" setting: once ~3 min are recorded, suggest a checked webcam box on the
    attention line (once per creator per day; never writes config)."""
    from . import attention as AT
    from . import facecam as FC
    from . import live as L
    try:
        if not FC.available() or not FC.unchecked(login):
            return
        deadline = started + 3600
        while time.time() < deadline:
            idx = L.load_index(out) if os.path.exists(os.path.join(out, "index.jsonl")) else []
            if idx and sum(r["d"] for r in idx if r["t"] + r["d"] >= started - 60) >= FC.SUGGEST_AFTER_S:
                break
            time.sleep(20)
        else:
            return
        sug = FC.suggest(login, out)
        if sug:
            AT.event("setup", sug["text"], key=f"facecam:{login}:{time.strftime('%Y-%m-%d', time.gmtime())}")
            print(f"[sentinel] {login}: {sug['text']}", flush=True)
    except Exception as exc:
        print(f"[sentinel] {login}: webcam suggestion skipped ({str(exc)[:80]})", flush=True)


def _rec_health(out: str) -> dict | None:
    """Recording since the last cycle: segments, MB, gaps > 3 s (with the last one's cause)."""
    try:
        from . import autopilot as AP
        since = (json.load(open(os.path.join(REPO, "runs", ".cycle.json"))).get("cycle_at") or 0) \
            if os.path.exists(os.path.join(REPO, "runs", ".cycle.json")) else 0
        return AP.recorder_stats(out, since or time.time() - 600)
    except Exception:
        return None


def warm_history(login: str, hist: dict, keep: int, minutes: float = 30.0) -> int:
    """Fill the chat baseline from the last `minutes` of the stream's own replay (a streamer whose VOD is
    being written while they're live), so a new sentinel judges spikes against real history from its first
    minute instead of a noisy two-minute baseline. Returns how many windows it filled (0 = no replay)."""
    from . import twitch as T
    v = T.latest_vod(login)
    if not v.get("live") or v.get("length_s", 0) < 300:
        return 0
    end = max(0.0, v["length_s"] - 30)                   # the replay lags the live edge a little
    start = max(0.0, end - minutes * 60)
    msgs = sorted(T.download_chat(v["id"], start, end), key=lambda m: m["t"])
    if len(msgs) < 50:
        return 0
    n, i, j, t = 0, 0, 0, start + WINDOW_S
    while t <= end:
        while j < len(msgs) and msgs[j]["t"] < t:
            j += 1
        while i < j and msgs[i]["t"] < t - WINDOW_S:
            i += 1
        _push(hist, window_raw(msgs[i:j]), keep)
        n += 1
        t += STEP_S
    return n


def _overlaps(a: tuple, drafted: list, frac: float = 0.5) -> bool:
    for b in drafted:
        inter = min(a[1], b[1]) - max(a[0], b[0])
        if inter > frac * min(a[1] - a[0], b[1] - b[0]):
            return True
    return False


def draft_id(login: str, start: float) -> str:
    return f"{login}-{dt.datetime.fromtimestamp(start, dt.timezone.utc):%H%M%S}"


def _draft_safe(out, login, moment, model_name):
    try:
        draft_moment(out, login, moment, model_name)
    except Exception as exc:
        print(f"[sentinel] draft failed: {exc}", flush=True)
        try:                                       # dropped, with its reason: the queue keeps a path back
            log_dropped(out, login, moment, f"draft failed: {str(exc)[:120]}")
        except Exception:
            pass
        try:
            from . import attention as AT
            at = dt.datetime.fromtimestamp(moment.get("fire") or time.time(), dt.timezone.utc)
            AT.event("failure", f"{login}: a moment at {at:%H:%M:%S} UTC couldn't be drafted ({str(exc)[:90]})",
                     key=f"draft:{login}:{int(moment.get('fire') or 0)}")
        except Exception:
            pass


def log_dropped(out: str, login: str, moment: dict, reason: str, **extra) -> dict:
    """A moment that won't be drafted still gets ONE queue line: status "dropped" and why."""
    entry = {"id": draft_id(login, moment["start"]), "login": login, "rec": out, "start": round(moment["start"], 1),
             "stop": round(moment["stop"], 1), "at": dt.datetime.fromtimestamp(moment.get("fire") or moment["start"],
                                                                                dt.timezone.utc).isoformat(),
             "p": moment.get("p"), "engine": 0, "best": 0, "flags": [], "unsafe": "", "chat": "", "said": "",
             "sheet": None, "status": "dropped", "reason": reason, "via": moment.get("source", "live spike"), **extra}
    with open(os.path.join(out, "queue.jsonl"), "a") as fh:
        fh.write(json.dumps(entry) + "\n")
    print(f"[sentinel] dropped {entry['id']}: {reason}", flush=True)
    return entry


def draft_words(entry: dict, start: float, stop: float) -> list[dict] | None:
    """The words the draft already transcribed, when they cover [start, stop] — so publishing doesn't
    transcribe the same moment again (the slow part). None = transcribe as usual."""
    try:
        d = json.load(open(entry["words"]))
    except (KeyError, OSError, ValueError, TypeError):
        return None
    if d.get("start", 1e18) > start + 0.5 or d.get("stop", -1) < stop - 0.5:
        return None
    return [w for w in d.get("words", []) if start - 3 <= w["start"] <= stop + 3]


def game_spans(words: list[dict] | None) -> list[tuple[float, float]]:
    """Stretches the game is talking (speaker tags): captions skip them — the game burns in its own
    subtitles, and a character's line is not our streamer's. Audio (and bleeps) are untouched."""
    out: list[tuple[float, float]] = []
    for w in words or []:
        if w.get("spk") == "game":
            if out and w["start"] - out[-1][1] < 0.8:
                out[-1] = (out[-1][0], w["end"] + 0.05)
            else:
                out.append((w["start"] - 0.05, w["end"] + 0.05))
    return [(round(a, 2), round(b, 2)) for a, b in out]


def check_trim(entry: dict, start: float, stop: float) -> str | None:
    """A plain refusal when a trim would cut the moment itself (the payoff and 1 s of reaction)."""
    pay = entry.get("payoff")
    if pay is None:
        return None
    if start > pay - 1.0:
        return (f"that --start cuts the payoff (it happens {pay - entry['start']:.1f} s into the draft) — "
                f"use --start {max(0.0, pay - entry['start'] - 3):.0f} or less")
    if stop < pay + 1.0:
        return (f"that --end cuts the payoff (it happens {entry['stop'] - pay:.1f} s before the draft's end) — "
                f"use --end {max(0.0, entry['stop'] - pay - 2):.0f} or less")
    return None


def chat_score_from_p(p: float) -> float:
    """The detector's live probability as the banger score's chat signal (same scale as a VOD scan)."""
    return -6 * math.log(1 - min(0.99, max(0.0, float(p or 0))))


# ---- the moment window: payoff first ------------------------------------------------------------

SETUP_S = (3.0, 15.0)     # before the payoff, starting on a sentence boundary
REACT_MAX_S = 8.0         # after it


def estimate_payoff(moment: dict) -> float:
    """Where the moment itself happened (stream time), before listening: viewers click right after the
    moment (a viewer clip ends ~3 s after it); chat reacts REACTION_DELAY_S after it."""
    if moment.get("crowd_end"):
        return moment["crowd_end"] - 3.0
    return (moment.get("fire") or moment["stop"]) - REACTION_DELAY_S


def boundaries(words: list[dict], lo: float, hi: float, step: float = 0.25) -> list[tuple[float, float]]:
    """(time, quality) for cut points in [lo, hi]: never inside a word. 1.0 = after a sentence end or
    in silence (gap >= 0.7 s), 0.5 = a pause inside speech (>= 0.3 s), 0.1 = between words."""
    out = []
    ws = sorted(words, key=lambda w: w["start"])
    t = lo
    while t <= hi + 1e-6:
        inside = any(w["start"] - 0.08 < t < w["end"] + 0.08 for w in ws)
        if not inside:
            prev = max((w for w in ws if w["end"] <= t), key=lambda w: w["end"], default=None)
            nxt = min((w for w in ws if w["start"] >= t), key=lambda w: w["start"], default=None)
            gap = (nxt["start"] if nxt else 1e9) - (prev["end"] if prev else -1e9)
            q = 1.0 if gap >= 0.7 or (prev and prev["w"].strip().endswith((".", "?", "!"))) else \
                0.5 if gap >= 0.3 else 0.1
            out.append((round(t, 2), q))
        t += step
    return out


def payoff_window(words: list[dict], payoff: float, lo: float, hi: float,
                  target: tuple[float, float] | None = None) -> dict:
    """Setup (3–15 s, from a sentence boundary) + payoff + reaction (<= 8 s, to a sentence boundary),
    inside [lo, hi] (what is recorded), aiming at `target` length (grading "target_len", 15–35 s).
    Never cuts a word; prefers sentence ends. Returns {start, stop, payoff, setup, react, clean}."""
    from . import grade as G
    tlo, thi = target or tuple(G.config().get("target_len", [15, 35]))
    starts = boundaries(words, max(lo, payoff - SETUP_S[1]), max(lo, payoff - SETUP_S[0]))
    stops = boundaries(words, min(hi, payoff + 1.0), min(hi, payoff + REACT_MAX_S))
    clean = True
    if not starts:                                       # one sentence runs through the whole setup range
        starts = boundaries(words, max(lo, payoff - 25), max(lo, payoff - SETUP_S[0])) or \
            [(max(lo, payoff - SETUP_S[1]), 0.0)]
        clean = starts[0][1] > 0
    if not stops:                                        # still talking 8 s later: finish the sentence (<= 14 s)
        stops = boundaries(words, min(hi, payoff + REACT_MAX_S), min(hi, payoff + 14)) or \
            [(min(hi, payoff + REACT_MAX_S), 0.0)]
        clean = clean and stops[0][1] > 0
    # the streamer's reaction: their words right after the payoff, up to the first real pause after +8 s
    reacting = [w for w in words if payoff - 0.5 <= w["start"] <= payoff + 12 and w.get("spk", "streamer") != "game"]
    if reacting and any(w["start"] <= payoff + REACT_MAX_S < w["end"] + 0.7 for w in reacting):
        stops += [x for x in boundaries(words, payoff + REACT_MAX_S, min(hi, payoff + 12)) if x[1] >= 0.5]
    best = None
    for s, qs in starts:
        for e, qe in stops:
            L = e - s
            fit = 0.0 if tlo <= L <= thi else -0.4 * (tlo - L if L < tlo else L - thi)
            cover = (sum(1 for w in reacting if w["end"] <= e) / len(reacting)) if reacting else 0.0
            score = 2 * qs + 2 * qe + fit - 0.05 * abs((payoff - s) - 8) + 3 * cover - 0.1 * max(0, e - payoff - REACT_MAX_S)
            if best is None or score > best[0]:
                best = (score, s, e, qs, qe)
    _, s, e, qs, qe = best
    return {"start": s, "stop": e, "payoff": round(payoff, 2), "setup": round(payoff - s, 1),
            "react": round(e - payoff, 1), "clean": clean and qs >= 0.5 and qe >= 0.5}


def covered(rec_dir: str, a: float, b: float, payoff: float) -> tuple[float, float] | None:
    """The stretch of [a, b] around the payoff the recording really has (ad breaks / restarts leave
    holes): None when the payoff itself is in a hole."""
    from . import live as L
    holes = [(g["t"], g["t"] + g["d"]) for g in L.gaps_in(rec_dir, a, b)]
    idx = [r for r in L.load_index(rec_dir) if r["t"] + r["d"] > a and r["t"] < b]
    prev = a
    for r in idx:                                        # holes the index shows (no gap line written)
        if r["t"] > prev + 3.0:
            holes.append((prev, r["t"]))
        prev = max(prev, r["t"] + r["d"])
    if prev < b - 1.0:
        holes.append((prev, b))
    s, e = a, b
    for h0, h1 in holes:
        if h0 - 1.0 <= payoff <= h1 + 1.0:
            return None
        if h1 <= payoff:
            s = max(s, h1)
        else:
            e = min(e, h0)
    return (s, e)


_GAME_CACHE: dict = {}


def stream_game(login: str) -> str | None:
    """The game the stream shows right now (Twitch category) — the live 'game at fire time'."""
    hit = _GAME_CACHE.get(login)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    try:
        from . import twitch as T
        u = T.gql('query($l:String!){user(login:$l){stream{game{displayName}}}}', {"l": login}).get("user") or {}
        g = ((u.get("stream") or {}).get("game") or {}).get("displayName")
    except Exception:
        g = None
    _GAME_CACHE[login] = (time.time(), g)
    return g


def target_game() -> str:
    try:
        return json.load(open(os.path.join(REPO, "config", "radar.json"))).get("game") or ""
    except (OSError, ValueError):
        return ""


def wrong_game(game: str | None) -> str:
    """'' when the moment is from the category we clip (or unknown); else the game it really is."""
    want = target_game()
    if not game or not want:
        return ""
    return "" if game.strip().lower() == want.strip().lower() else game


def draft_moment(out: str, login: str, moment: dict, model_name: str = "small", wait_s: float = 90,
                 _recut: str | None = None) -> dict:
    """Judge one moment like an editor and append ONE line to queue.jsonl:
    find the payoff, cut setup + payoff + reaction on sentence boundaries, tag who is talking (streamer /
    other / game), score from the streamer's speech and reactions, re-cut once if the cut is fixable,
    and log a drop with its reason instead of losing it."""
    from . import audio as A
    from . import live as L
    from . import grade as G
    from . import safety as SF
    from . import speaker as SPK
    from . import vision as VI
    from . import gate as GT
    if not os.path.exists(os.path.join(out, "index.jsonl")):
        print(f"[sentinel] {login}: moment at {dt.datetime.fromtimestamp(moment['fire'], dt.timezone.utc):%H:%M:%S} "
              "UTC not drafted — nothing recorded yet (the recorder can't reach the stream; see its retries above)",
              flush=True)
        return log_dropped(out, login, moment, "nothing recorded yet")
    game = moment.get("game") or stream_game(login)
    bad_game = wrong_game(game)
    payoff = estimate_payoff(moment)
    a, b = payoff - SETUP_S[1] - 10, payoff + REACT_MAX_S + 6
    t_wait = time.time() + wait_s
    while time.time() < t_wait:                      # wait for the recording to cover the reaction
        idx = L.load_index(out)
        if idx and idx[-1]["t"] + idx[-1]["d"] >= b:
            break
        time.sleep(3)
    cov = covered(out, a, b, payoff)
    if cov is None or cov[1] - cov[0] < 8:
        return log_dropped(out, login, moment, "recording gap (ad break) over the moment", payoff=round(payoff, 1))
    a, b = cov
    ddir = os.path.join(out, "drafts")
    os.makedirs(ddir, exist_ok=True)
    mid = draft_id(login, moment["start"])
    src = os.path.join(ddir, mid + ".ts")
    first = L.cut_source(out, a, b, src)
    x = A.decode_pcm(src, 0, None)
    early = A.gate(x, first, payoff, route="live-recut" if _recut else "live")
    if early.observation in A.SKIP_OBS:              # nothing to hear at the payoff: no Whisper, one queue line
        os.remove(src)
        return log_dropped(out, login, moment, A.skip_reason(early.observation), payoff=round(payoff, 1),
                           muted_frac=early.muted_frac)
    words = A.real_words(A.transcribe(x, model_name, offset=first, prompt=A.vocab_prompt(login=login)))
    words = [w for w in words if a <= w["start"] <= b]
    # who is talking: the session's voice profile + what the screen shows (subtitles, cutscene editing)
    tl = VI.timeline(src)
    hint = VI.game_hint_fn(tl, first)
    try:
        prof = SPK.recording_profile(out)
    except Exception as exc:
        print(f"[sentinel] voice profile unavailable ({str(exc)[:80]})", flush=True)
        prof = None
    SPK.tag_words(words, x, first, prof, hint)
    json.dump({"start": a, "stop": b, "model": model_name, "words": words},
              open(os.path.join(ddir, mid + ".words.json"), "w"))            # publish reuses it: no second transcription
    # the payoff: the streamer's reaction onset near where chat / viewers place the moment
    re0 = A.reaction(x, first, words, payoff, before=8.0, after=4.0)
    how = "viewer clip end" if moment.get("crowd_end") else "chat reaction − delay"
    if re0["onset"] is not None and re0["score"] >= 0.25 and not moment.get("crowd_end"):
        payoff, how = re0["onset"] - 0.5, "streamer reaction onset"
    win = payoff_window(words, payoff, a, b)
    recut = _recut
    # hold & re-cut once: a dead first 2 s, or the payoff jammed against an edge
    first_word = next((w["start"] for w in words if w["start"] >= win["start"]), None)
    if not recut and first_word is not None and first_word - win["start"] > 2.0 and payoff - first_word >= SETUP_S[0]:
        win = payoff_window(words, payoff, max(a, first_word - 0.4), b)
        recut = "dead open → started on the first word"
    if not recut and win["react"] < 2.0 and b < payoff + REACT_MAX_S:
        recut = "payoff at the edge → waited for the reaction"
        if True:
            t_wait = time.time() + 45
            while time.time() < t_wait:
                idx = L.load_index(out)
                if idx and idx[-1]["t"] + idx[-1]["d"] >= payoff + REACT_MAX_S + 2:
                    break
                time.sleep(3)
            os.remove(src)
            return draft_moment(out, login, {**moment, "game": game}, model_name, wait_s=0, _recut=recut)
    s, e = win["start"], win["stop"]
    in_win = [w for w in words if s <= w["start"] < e]
    rx = A.reaction(x, first, in_win, payoff, before=2.0, after=min(REACT_MAX_S, e - payoff))
    human = [w for w in in_win if w.get("spk", "streamer") != "game"]
    said = " ".join(w["w"].strip() for w in human)
    chat_lines = [c["text"] for c in moment.get("chat", [])]
    vis = VI.check(src, s - first, e - s)
    sig = G.signals_from_live(out, login, s, e, in_win)
    sf = SF.screen(said, titles=sig.get("titles"), chat_lines=chat_lines)
    sig["chat_score"] = chat_score_from_p(moment["p"])
    if moment.get("crowd_new"):                     # viewers already clipping it: strong early crowd signal
        sig["crowd_vpm"] = (sig.get("crowd_vpm") or 0) + 5 * moment["crowd_new"]
    sig["reaction"], sig["reaction_why"] = rx["score"], rx["why"]
    sig["liveliness"] = vis.get("liveliness")
    misheard = A.misheard_names(in_win, login=login)
    sig["flags"] = (sig.get("flags") or []) + vis["flags"] + \
        ([f"recording from {game}, not {target_game()}"] if bad_game else []) + \
        ([f"names? {', '.join(f'{h}→{r}' for h, r in misheard)}"] if misheard else []) + \
        ([] if win["clean"] else ["cut mid-sentence (no pause near the payoff)"])
    sig["unsafe"] = sf["why"] if sf["flagged"] else ""
    sig["wrong_game"] = bad_game
    eng = G.engine_score(sig)
    best = G.banger(eng["score"], None)["best_possible"]
    sheet = os.path.join(ddir, mid + ".jpg")
    VI.contact_sheet(src, sheet, start=s - first, dur=e - s)
    os.remove(src)
    top = collections.Counter(w for c in chat_lines for w in S.tokens(c) if len(w) > 1 or w in "💀😭").most_common(4)
    after = [w for w in human if w.get("spk", "streamer") == "streamer" and w["start"] >= payoff - 1]
    quote = " ".join(w["w"].strip() for w in after[:9])
    floor = min(r["min_score"] for r in GT.config()["platforms"].values())
    reason = ("wrong game: " + bad_game if bad_game else "unsafe: " + sig["unsafe"] if sig["unsafe"] else
              f"best {best} under every platform's bar ({floor}): " + "; ".join(eng["why"][:2])
              if best < floor else "")
    from . import judge as JG
    spk = SPK.summary(in_win, prof)
    entry = {"id": mid, "login": login, "rec": out, "start": round(s, 2), "stop": round(e, 2),
             "payoff": round(payoff, 2), "payoff_how": how, "src": [round(a, 1), round(b, 1)],
             "at": dt.datetime.fromtimestamp(moment["fire"], dt.timezone.utc).isoformat(),
             "drafted_at": dt.datetime.now(dt.timezone.utc).isoformat(), "p": moment["p"],
             "engine": eng["score"], "best": best, "parts": G.parts_line(eng, sig), "why": eng["why"],
             "liveliness": vis.get("liveliness"), "frames_assessed": bool(vis.get("frames")),
             "judgment": JG.score_snapshot(eng, sig), "titles": sig.get("titles") or [],
             "flags": sig["flags"], "unsafe": sig["unsafe"], "chat_unsafe": sf.get("chat_hits", []),
             "chat": " ".join(f"{w}×{n}" for w, n in top), "said": said[:140], "quote_hint": quote[:60],
             "speakers": {k: spk[k] for k in ("streamer_s", "other_s", "game_s", "game_share", "backend")},
             "game": game, "sheet": sheet, "status": "dropped" if reason else "new", "reason": reason or None,
             "recut": recut, "features": moment.get("features"), "via": moment.get("source", "live spike"),
             "words": os.path.join(ddir, mid + ".words.json")}
    with open(os.path.join(out, "queue.jsonl"), "a") as fh:
        fh.write(json.dumps(entry) + "\n")
    print(f"[sentinel] {'dropped' if reason else 'queued'} {mid}: {e - s:.0f}s, payoff +{payoff - s:.0f}s ({how}), "
          f"engine {eng['score']} (best {best}) [{entry['parts']}] {reason or sf['why'] or ''}", flush=True)
    if not reason and prerender_on():
        _prerender_safe(entry)
    return entry


def prerender_on() -> bool:
    try:
        from . import ops as O
        return O.config().get("prerender", True) is not False
    except Exception:
        return True


def _prerender_safe(entry: dict) -> None:
    """The draft's final render (all but the headline card), so publishing it takes seconds."""
    try:
        from . import prerender as PR
        if PR.eligible(entry):
            m = PR.prerender(entry)
            if m:
                print(f"[sentinel] pre-rendered {entry['id']} in {m['secs']} s — publish-queued only burns the card",
                      flush=True)
    except Exception as exc:
        print(f"[sentinel] pre-render of {entry['id']} failed ({str(exc)[:100]}) — publish renders it in full",
              flush=True)


# ---- operator commands --------------------------------------------------------------------------

def _queue_files(root: str) -> list[str]:
    import glob
    return sorted(glob.glob(os.path.join(root, "*", "queue.jsonl")))


def load_queue(root: str = os.path.join(REPO, "runs")) -> list[dict]:
    rows = []
    for qf in _queue_files(root):
        latest: dict[str, dict] = {}
        for line in open(qf):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            latest[r["id"]] = {**latest.get(r["id"], {}), **r}       # later lines update status
        rows += latest.values()
    return rows


def note(entry: dict, **extra) -> None:
    """Append a queue update, even if an older writer omitted the final newline."""
    import fcntl

    with open(os.path.join(entry["rec"], "queue.jsonl"), "a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0, os.SEEK_END)
        if stream.tell():
            stream.seek(-1, os.SEEK_END)
            if stream.read(1) != b"\n":
                stream.write(b"\n")
        stream.write((json.dumps({"id": entry["id"], **extra}) + "\n").encode())


def mark_seen(entry: dict, now: float | None = None) -> str | None:
    seen = entry.get("seen_at")
    if not seen and entry.get("rec") and os.path.isdir(entry["rec"]):
        seen = dt.datetime.fromtimestamp(time.time() if now is None else now, dt.timezone.utc).isoformat()
        note(entry, seen_at=seen)
        entry["seen_at"] = seen
    return seen


def set_status(entry: dict, status: str, **extra) -> None:
    if status in ("published", "skipped"):
        extra.setdefault("judged_at", dt.datetime.now(dt.timezone.utc).isoformat())
    note(entry, status=status, **extra)


URGENT_BEST = 75.0          # a draft that can clear TikTok/Instagram's bar…


def is_urgent(r: dict) -> bool:
    """…or one viewers are already clipping: a race to be first — it goes to the top and pings the phone."""
    if r.get("unsafe") or r.get("status") not in (None, "new"):
        return False
    m = re.search(r"\bcr ([0-9.]+)", str(r.get("parts") or ""))
    crowd = float(m.group(1)) if m else float((r.get("features") or {}).get("crowd_new") or 0)
    return float(r.get("best") or 0) >= URGENT_BEST or crowd > 0 or "crowd" in str(r.get("via") or "")


def show_queue(root: str = os.path.join(REPO, "runs"), all_: bool = False, max_age_h: float = 6.0, seen: bool = True) -> list[dict]:
    """One line per draft worth a look: new, safe, fresh, and able to clear some platform's bar."""
    from . import gate as GT
    from . import untrusted as UT
    floor = min(r["min_score"] for r in GT.config()["platforms"].values())
    now = time.time()
    rows = []
    from . import triage as TR
    for r in load_queue(root):
        age_h = (now - TR.moment_time(r)) / 3600         # the moment's age — a re-queue never makes it fresh
        if not all_ and (r.get("status") != "new" or r.get("unsafe") or r.get("best", 0) < floor or age_h > max_age_h):
            continue
        rows.append({**r, "age_min": round(age_h * 60)})
    if not all_ and len(rows) > 1:
        from . import events, publish, scouts
        rows = scouts.group_angles(rows, publish.creators(), events.load())
    rows.sort(key=lambda r: (not (is_urgent(r) or r.get("angle_urgent")), -r.get("best", 0)))
    for r in rows:
        if seen and r.get("status") == "new":
            mark_seen(r, now)
        mark = f" ⛔{UT.quote(r['unsafe'], 30)}" if r.get("unsafe") else \
            f" ⚠chat: {UT.quote(', '.join(r['chat_unsafe'][:2]), 20)}" if r.get("chat_unsafe") else ""
        if is_urgent(r) or r.get("angle_urgent"):
            mark += " 🔥URGENT"
        if r.get("status") not in (None, "new"):
            reason = r.get("reason") or ("no reason saved" if r.get("status") in ("dropped", "skipped") else "")
            mark += f" [{r['status']}{': ' + UT.quote(reason, 50) if reason else ''}]"
        confidence = []
        if not r.get("frames_assessed", r.get("liveliness") is not None):
            confidence.append("frames unassessed")
        speakers = r.get("speakers") or {}
        speech = (r.get("judgment") or {}).get("speech_s", speakers.get("streamer_s"))
        if "thin speech" in (r.get("flags") or []) or (speech is not None and speech < 0.2 * (r["stop"] - r["start"])):
            confidence.append("thin speech")
        if speakers.get("backend") in {"stats", "hints"}:
            confidence.append("speaker tags uncertain")
        if confidence:
            mark += " · " + " · ".join(confidence)
        if r.get("triage") in ("preview", "review") and r.get("status") == "new":
            exp = r.get("expires_at")
            mark += (f" · {r['triage']}: {UT.quote(r.get('triage_reason') or '', 50)}"
                     + (f" · expires {time.strftime('%H:%M', time.localtime(exp))}" if exp else ""))
        if r.get("repaired_from"):
            mark += " · re-cut on speech boundaries"
        if r.get("angles"):
            mark += f" · same moment, {len(r['angles'])} angles"
        if r.get("seen_at"):
            mark += f" · seen {r['seen_at'][11:16]} UTC"
        summary = (f"{r['id']:<22} best {r.get('best', 0):>4.0f}  {r['age_min']:>3}m ago  "
                   f"chat: {UT.quote(r.get('chat', ''), 30):<32} said: ")
        excerpt = UT.quote(r.get("said", ""), max(0, min(60, 218 - len(summary) - len(mark))))
        print(summary + excerpt + mark)
        work = r.get("parts") or ""
        if r.get("payoff") is not None:
            work = f"{r['stop'] - r['start']:.0f}s payoff +{r['payoff'] - r['start']:.0f}s · " + work
        flags = ", ".join(r.get("flags") or [])
        line2 = (f"{'':<22} {work}{' | ' + flags if flags else ''}")[:140]
        alternate = " · angles: " + ", ".join(r["angles"][1:]) if r.get("angles") else ""
        print(f"{line2}  sheet {os.path.relpath(r['sheet'], REPO) if r.get('sheet') else '-'}{alternate}")
    return rows


def health(root: str = os.path.join(REPO, "runs")) -> list[dict]:
    """One line per sentinel; silent-ish when all is well."""
    import glob
    out = []
    for sp in sorted(glob.glob(os.path.join(root, "*", "sentinel.json"))):
        try:
            s = json.load(open(sp))
        except (OSError, ValueError):
            continue
        age = time.time() - s.get("beat", 0)
        d = os.path.dirname(sp)
        pidf = os.path.join(d, "sentinel.pid")
        try:
            pid = int(open(pidf).read().split()[0])
            os.kill(pid, 0)
            running = True
        except (OSError, ValueError, IndexError):
            running = False
        stopped = os.path.exists(os.path.join(d, "STOPPED"))
        alive = age < 120 and running and not stopped       # a fresh heartbeat alone isn't proof: the pid must run
        out.append({**s, "alive": alive, "stopped": stopped, "dir": d})
        just_started = os.path.exists(pidf) and time.time() - os.path.getmtime(pidf) < 120
        if stopped:
            why = (open(os.path.join(d, "STOPPED")).read().strip() or "stopped")[:40]
            print(f"stop  {s['login']:<16} {why} — not running (cycle starts it again when it's wanted)")
        elif alive and not s.get("chat_connected") and just_started:
            print(f"start {s['login']:<16} connecting to chat — check again next cycle")
        elif not alive or not s.get("chat_connected"):
            print(f"DOWN  {s['login']:<16} last beat {age / 60:.0f} min ago — restart: "
                  f".venv/bin/python -m clipengine start --channel {s['login']}  (or just run `cycle`)")
        else:
            mode = " (audio only)" if s.get("chat_mode") == "audio-only" else ""
            print(f"ok    {s['login']:<16} chat {s.get('chat_per_min', 0):>6.0f}/min{mode}  recorded {s.get('recorded_min', 0):>5.0f} min  "
                  f"moments {s.get('moments', 0)}")
    return out
