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


def moments_from_series(ser: list[dict], m: dict | None = None) -> list[dict]:
    """Trigger/release over a probability series -> [{start, peak, end, p}] in the series' clock."""
    m = m or model()
    out, cur, last_end = [], None, -1e9
    for row in ser:
        t, p = row["t"], row["p"]
        if cur is None:
            if p >= m["trigger"] and t - last_end >= COOLDOWN_S:
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


def run(login: str, out: str, buffer_h: float = 3.0, draft: bool = True, model_name: str = "small") -> None:
    """`login`: a Twitch login, or a YouTube source ("yt:@handle" / URL) — YouTube is audio-only."""
    from . import live as L
    source = login if L.is_youtube(login) else login.lower()
    youtube = L.is_youtube(source)
    login = L.source_name(source)
    os.makedirs(out, exist_ok=True)
    m = model()
    def supervised_record():
        # the stream can drop, go offline, or land on a host this machine can't reach: keep trying
        delay = 15
        while True:
            try:
                L.record(source, out, buffer_h * 3600)
                delay = 15
            except Exception as exc:
                print(f"[sentinel] recorder stopped ({str(exc)[:100]}) — retrying in {delay}s", flush=True)
            time.sleep(delay)
            delay = min(300, delay * 2)

    rec = threading.Thread(target=supervised_record, daemon=True)
    rec.start()
    if youtube:
        chat, crowd = _Silent(), _Silent()
    else:
        chat, crowd = Chat(login), Crowd(login)
        chat.start(); crowd.start()
    hist: dict[str, list[float]] = {}
    past: list[dict] = []
    hdb: list[float] = []
    keep = int(HISTORY_S / STEP_S)
    audio_seen: dict[str, float] = {}
    cur, last_end, fired = None, 0.0, 0
    status_path = os.path.join(out, "sentinel.json")
    print(f"[sentinel] {login}: listening (trigger p ≥ {m['trigger']}) -> {out}", flush=True)
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
        if youtube:                                  # no chat: a sustained loudness surge decides
            p = 1 / (1 + math.exp(-(-4.0 + 1.6 * audio_z))) if len(hdb) > 30 else 0.0
        else:
            p = prob(f, m) if len(hist.get("users", [])) >= MIN_HISTORY else 0.0
        _push(hist, raw, keep)
        if cur is None:
            if p >= m["trigger"] and now - last_end >= COOLDOWN_S:
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
                moment = {"start": start, "stop": stop, "p": round(cur["p"], 3), "fire": cur["fire"], "features": cur.get("features"),
                          "crowd_new": crowd.count(start, now + 60), "chat": cur["chat"]}
                last_end, cur, fired = now, None, fired + 1
                if draft:
                    threading.Thread(target=_draft_safe, args=(out, login, moment, model_name), daemon=True).start()
        if now - last_beat >= 30:
            last_beat = now
            json.dump({"login": login, "pid": os.getpid(), "beat": now, "chat_connected": chat.connected,
                       "chat_per_min": round(sum(hist.get("msgs", [])[-12:]) * (60 / (12 * WINDOW_S)), 1),
                       "recorded_min": round(len(L.load_index(out)) * 2 / 60, 1) if os.path.exists(os.path.join(out, "index.jsonl")) else 0,
                       "moments": fired, "p": round(p, 3)}, open(status_path, "w"))


def _draft_safe(out, login, moment, model_name):
    try:
        draft_moment(out, login, moment, model_name)
    except Exception as exc:
        print(f"[sentinel] draft failed: {exc}", flush=True)


def draft_moment(out: str, login: str, moment: dict, model_name: str = "small", wait_s: float = 90) -> dict:
    """Cut + check one moment from the recording and append ONE line to queue.jsonl."""
    from . import audio as A
    from . import live as L
    from . import grade as G
    from . import safety as SF
    from . import vision as VI
    if not os.path.exists(os.path.join(out, "index.jsonl")):
        print(f"[sentinel] {login}: moment at {dt.datetime.fromtimestamp(moment['fire'], dt.timezone.utc):%H:%M:%S} "
              "UTC not drafted — nothing recorded yet (the recorder can't reach the stream; see its retries above)",
              flush=True)
        return {}
    t_wait = time.time() + wait_s
    while time.time() < t_wait:                      # wait for the recording to cover the end
        idx = L.load_index(out)
        if idx and idx[-1]["t"] + idx[-1]["d"] >= moment["stop"]:
            break
        time.sleep(3)
    ddir = os.path.join(out, "drafts")
    os.makedirs(ddir, exist_ok=True)
    mid = f"{login}-{dt.datetime.fromtimestamp(moment['start'], dt.timezone.utc):%H%M%S}"
    src = os.path.join(ddir, mid + ".ts")
    first = L.cut_source(out, moment["start"], moment["stop"], src)
    x = A.decode_pcm(src, 0, None)
    words = A.real_words(A.transcribe(x, model_name, offset=first))
    words = [w for w in words if moment["start"] <= w["start"] <= moment["stop"]]
    said = " ".join(w["w"].strip() for w in words)
    chat_lines = [c["text"] for c in moment.get("chat", [])]
    sf = SF.screen(said, chat_lines=chat_lines)
    vis = VI.check(src)
    sig = G.signals_from_live(out, login, moment["start"], moment["stop"], words)
    sig["chat_score"] = -6 * math.log(1 - min(0.99, moment["p"]))
    if moment.get("crowd_new"):                     # viewers already clipping it: strong early crowd signal
        sig["crowd_vpm"] = (sig.get("crowd_vpm") or 0) + 5 * moment["crowd_new"]
    sig["flags"] = (sig.get("flags") or []) + vis["flags"]
    sig["unsafe"] = sf["why"] if sf["flagged"] else ""
    eng = G.engine_score(sig)
    best = G.banger(eng["score"], None)["best_possible"]
    sheet = os.path.join(ddir, mid + ".jpg")
    VI.contact_sheet(src, sheet)
    os.remove(src)
    top = collections.Counter(w for c in chat_lines for w in S.tokens(c) if len(w) > 1 or w in "💀😭").most_common(4)
    entry = {"id": mid, "login": login, "rec": out, "start": round(moment["start"], 1), "stop": round(moment["stop"], 1),
             "at": dt.datetime.fromtimestamp(moment["fire"], dt.timezone.utc).isoformat(), "p": moment["p"],
             "engine": eng["score"], "best": best, "flags": sig["flags"], "unsafe": sig["unsafe"],
             "chat": " ".join(f"{w}×{n}" for w, n in top), "said": said[:140], "sheet": sheet, "status": "new",
             "features": moment.get("features")}
    with open(os.path.join(out, "queue.jsonl"), "a") as fh:
        fh.write(json.dumps(entry) + "\n")
    print(f"[sentinel] queued {mid}: engine {eng['score']} (best {best}) {sf['why'] or ''}", flush=True)
    return entry


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


def set_status(entry: dict, status: str, **extra) -> None:
    with open(os.path.join(entry["rec"], "queue.jsonl"), "a") as fh:
        fh.write(json.dumps({"id": entry["id"], "status": status, **extra}) + "\n")


def show_queue(root: str = os.path.join(REPO, "runs"), all_: bool = False, max_age_h: float = 6.0) -> list[dict]:
    """One line per draft worth a look: new, safe, fresh, and able to clear some platform's bar."""
    from . import gate as GT
    floor = min(r["min_score"] for r in GT.config()["platforms"].values())
    now = time.time()
    rows = []
    for r in load_queue(root):
        age_h = (now - r["stop"]) / 3600
        if not all_ and (r.get("status") != "new" or r.get("unsafe") or r.get("best", 0) < floor or age_h > max_age_h):
            continue
        rows.append({**r, "age_min": round(age_h * 60)})
    rows.sort(key=lambda r: -r.get("best", 0))
    for r in rows:
        mark = f" ⛔{r['unsafe']}" if r.get("unsafe") else ""
        print(f"{r['id']:<22} best {r.get('best', 0):>4.0f}  {r['age_min']:>3}m ago  chat: {r.get('chat', '')[:40]:<40} "
              f"said: \"{r.get('said', '')[:70]}\"{mark}")
        print(f"{'':<22} sheet {os.path.relpath(r['sheet'], REPO) if r.get('sheet') else '-'}"
              f"{'  flags: ' + ', '.join(r['flags']) if r.get('flags') else ''}")
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
        alive = age < 120
        out.append({**s, "alive": alive, "dir": os.path.dirname(sp)})
        if not alive or not s.get("chat_connected"):
            print(f"DOWN  {s['login']:<16} last beat {age / 60:.0f} min ago — restart: "
                  f".venv/bin/python -m clipengine start --channel {s['login']}  (or just run `cycle`)")
        else:
            print(f"ok    {s['login']:<16} chat {s.get('chat_per_min', 0):>6.0f}/min  recorded {s.get('recorded_min', 0):>5.0f} min  "
                  f"moments {s.get('moments', 0)}")
    return out
