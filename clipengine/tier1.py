"""Tier-1 watchers — cheap chat-only eyes on EVERY live roster creator.

Coverage in two tiers:
  tier 1  this module: ONE process, ONE anonymous IRC connection that joins every live roster creator's
          chat (no video, no audio, no viewer-clip polling). Each channel is judged with the sentinel's own
          chat model (same features, same 30-min baseline, warm-started from the stream's replay) every
          STEP_S seconds. It writes each channel's heat to runs/tier1/state.json and every closed spike
          to runs/tier1/spikes.jsonl.
  tier 2  a full sentinel (recorder + audio + viewer clips + drafts), on up to max_sentinels creators.

`cycle` keeps the channel list (runs/tier1/channels.json) in step with who is live, and:
  * promotes a creator whose tier-1 heat crosses the bar to tier 2 (plan_sentinels puts them ahead of
    the radar's picks; the coldest tier-2 gives up its slot) and holds them there for a while;
  * drafts a tier-1 spike on a creator with saved VODs from the VOD once it is published (the catch-up
    path) — even if they were never promoted. Spikes a sentinel already covered are skipped.

Kick sources ("kick:<slug>") are watched by their own chat reader behind the same interface (Kick refuses it to
outside clients — pusher 4001 — so on Kick tier 1 is in practice its viewers' clips), and CrowdWatch reads their
clips listing; their spikes are drafted from the live/saved broadcast (kick.catchup_spike).
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import os
import queue
import re
import threading
import time

from . import sentinel as SN

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIR = os.path.join(REPO, "runs", "tier1")
CHANNELS = os.path.join(DIR, "channels.json")
STATE = os.path.join(DIR, "state.json")
SPIKES = os.path.join(DIR, "spikes.jsonl")
PIDF = os.path.join(DIR, "tier1.pid")
HEAT_S = 120.0                      # heat = the highest probability in the last two minutes


# ---- chat: one IRC connection for many channels ------------------------------------------------

class MultiChat(threading.Thread):
    """Anonymous read-only Twitch chat for a changing set of channels over one websocket."""
    def __init__(self):
        super().__init__(daemon=True)
        self.want: set[str] = set()
        self.joined: set[str] = set()
        self.msgs: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self.lock = threading.Lock()
        self.connected, self.stop_flag, self.total = False, False, 0
        self.ws = None

    def set_channels(self, logins: set[str]) -> None:
        with self.lock:
            self.want = {l.lower() for l in logins}
            for gone in set(self.msgs) - self.want:
                self.msgs.pop(gone, None)

    def _sync(self) -> None:
        ws = self.ws
        if not ws:
            return
        with self.lock:
            add, drop = self.want - self.joined, self.joined - self.want
        for i in range(0, len(add), 20):                   # Twitch: JOIN takes a comma list; keep batches small
            batch = sorted(add)[i:i + 20]
            ws.send("JOIN " + ",".join(f"#{l}" for l in batch))
            self.joined |= set(batch)
        if drop:
            ws.send("PART " + ",".join(f"#{l}" for l in sorted(drop)))
            self.joined -= drop

    def run(self):
        import websocket
        delay = 2
        while not self.stop_flag:
            try:
                self.ws = ws = websocket.create_connection("wss://irc-ws.chat.twitch.tv:443", timeout=5,
                                                           **SN._ws_kwargs())
                ws.send("CAP REQ :twitch.tv/tags")
                ws.send("PASS SCHMOOPIIE")
                ws.send(f"NICK justinfan{int(time.time()) % 90000 + 10000}")
                self.joined = set()
                self.connected, delay = True, 2
                last_ping = time.time()
                while not self.stop_flag:
                    self._sync()
                    try:
                        data = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        if time.time() - last_ping > 60:
                            ws.send("PING :tmi.twitch.tv"); last_ping = time.time()
                        continue
                    self.ingest(data, time.time())
            except Exception as exc:
                self.connected, self.ws = False, None
                print(f"[tier1] chat reconnect in {delay}s ({str(exc)[:80]})", flush=True)
                time.sleep(delay)
                delay = min(60, delay * 2)

    def ingest(self, data: str, now: float) -> None:
        """Parse raw IRC lines (pure but for the buffers) — PRIVMSGs land in their channel's buffer."""
        for line in data.split("\r\n"):
            if line.startswith("PING"):
                if self.ws:
                    self.ws.send("PONG :tmi.twitch.tv")
                continue
            if " PRIVMSG #" not in line:
                continue
            tags = dict(kv.split("=", 1) for kv in line[1:].split(" ", 1)[0].split(";") if "=" in kv) \
                if line.startswith("@") else {}
            rest = line.split(" PRIVMSG #", 1)[1]
            chan, _, text = rest.partition(" :")
            chan = chan.strip().lower()
            user = tags.get("user-id") or tags.get("display-name") or line.split("!", 1)[0].lstrip("@:").split(" ")[-1]
            emotes = len(re.findall(r"\d+-\d+", tags.get("emotes", "")))
            with self.lock:
                if chan not in self.want:
                    continue
                q = self.msgs[chan]
                q.append({"t": now, "user": user, "text": text, "emotes": emotes})
                self.total += 1
                while q and q[0]["t"] < now - SN.WINDOW_S - 30:      # tier 1 keeps only what it judges
                    q.popleft()

    def window(self, login: str, t0: float, t1: float) -> list[dict]:
        with self.lock:
            return [m for m in self.msgs.get(login.lower(), ()) if t0 <= m["t"] < t1]


class _View:
    """One channel of a MultiChat, with the sentinel's Chat interface (connected, window)."""
    def __init__(self, multi: MultiChat, login: str):
        self.multi, self.login = multi, login

    @property
    def connected(self) -> bool:
        return self.multi.connected and self.login in self.multi.joined

    def window(self, t0: float, t1: float) -> list[dict]:
        return self.multi.window(self.login, t0, t1)


# ---- the judge, one per channel ----------------------------------------------------------------

class Judge:
    """The sentinel's chat judge for one channel, incremental: feed it a window per step."""
    def __init__(self, login: str, m: dict | None = None):
        self.login, self.m = login, m or SN.model()
        self.hist: dict[str, list[float]] = {}
        self.past: list[dict] = []
        self.keep = int(SN.HISTORY_S / SN.STEP_S)
        self.cur, self.last_end = None, 0.0
        self.p_recent: collections.deque = collections.deque(maxlen=int(HEAT_S / SN.STEP_S) + 1)
        self.p, self.spikes, self.warm = 0.0, 0, False

    def step(self, now: float, msgs: list[dict]) -> dict | None:
        """Judge one window; returns a closed spike (stream-time start/stop) or None."""
        raw = SN.window_raw(msgs)
        f = SN.with_lags(SN.features(raw, self.hist), self.past)
        self.past = (self.past + [f])[-max(SN.LAGS):]
        p = SN.prob(f, self.m) if len(self.hist.get("users", [])) >= SN.MIN_HISTORY else 0.0
        SN._push(self.hist, raw, self.keep)
        self.p = p
        self.p_recent.append((now, p))
        m = self.m
        if self.cur is None:
            if p >= m["trigger"] and now - self.last_end >= SN.cooldown(m):
                self.cur = {"fire": now, "peak": now, "p": p, "end": now}
            return None
        if p > self.cur["p"]:
            self.cur["p"], self.cur["peak"] = p, now
        if p >= m["release"]:
            self.cur["end"] = now
        if (p < m["release"] and now - self.cur["end"] >= 2 * SN.STEP_S) or now - self.cur["fire"] >= SN.MAX_LEN_S:
            c, self.cur, self.last_end = self.cur, None, now
            self.spikes += 1
            start = c["fire"] - SN.REACTION_DELAY_S - SN.PRE_ROLL_S
            stop = min(c["end"] - SN.REACTION_DELAY_S + SN.POST_ROLL_S, start + SN.MAX_LEN_S)
            return {"login": self.login, "fire": round(c["fire"], 1), "peak": round(c["peak"], 1),
                    "end": round(c["end"], 1), "p": round(c["p"], 3), "start": round(start, 1), "stop": round(stop, 1)}
        return None

    def heat(self, now: float) -> float:
        return max([p for t, p in self.p_recent if now - t <= HEAT_S] or [0.0])

    def chat_per_min(self) -> float:
        return round(sum(self.hist.get("msgs", [])[-12:]) * (60 / (12 * SN.WINDOW_S)), 1)


# ---- files the cycle and the process share ----------------------------------------------------

def _json(path: str, default):
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return default


def want_channels() -> list[str]:
    return list(_json(CHANNELS, {}).get("channels") or [])


def write_channels(logins: list[str]) -> None:
    os.makedirs(DIR, exist_ok=True)
    tmp = CHANNELS + ".tmp"
    json.dump({"channels": sorted(set(logins)), "at": time.time()}, open(tmp, "w"))
    os.replace(tmp, CHANNELS)


def load_state() -> dict:
    return _json(STATE, {})


def load_spikes(since: float = 0.0, path: str | None = None) -> list[dict]:
    out = []
    try:
        for line in open(path or SPIKES):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("fire", 0) >= since:
                out.append(r)
    except OSError:
        pass
    return out


def sentinel_covers(login: str, now: float | None = None) -> bool:
    """A full sentinel is watching this creator right now (its spike is drafted by it, not by catch-up)."""
    from . import ops as O
    d = O.sentinel_dir(login)
    if not os.path.isdir(d):
        return False
    st = O.sentinel_status(d)
    return bool(st["alive"] and not st["stopped"])


# ---- the process ------------------------------------------------------------------------------

def _chat_for(login: str, multi: MultiChat):
    if login.startswith("kick:"):
        from . import kick as K
        c = K.KickChat(login[5:])
        c.start()
        return c
    return _View(multi, login)


CROWD_Q = """query($ls: [String!]) { users(logins: $ls) { login clips(first: 100, criteria: {period: LAST_DAY,
  sort: VIEWS_DESC}) { edges { node { slug title createdAt viewCount } } } } }"""
CROWD_MIN_VIEWS = 10        # a fresh viewer clip with this many views = a moment the crowd is reacting to
CROWD_FRESH_S = 1200        # only clips made in the last 20 min count as "happening now"
CROWD_HOT_S = 600           # after one, the creator counts as hot (promotion to a full sentinel) this long
CROWD_LATE_S = 2 * 3600     # a clip we saw under 10 views that passes them later (up to 2 h old) is still a moment
CROWD_PAGE = 100            # clips per creator per poll (24 Nov: at 20, 2 of 23 creators' lists were full at night)
CROWD_LOG = os.path.join(DIR, "crowd.jsonl")


class CrowdWatch(threading.Thread):
    """Viewers' clips on EVERY watched stream, once a minute, in one batched request. When a fresh clip
    passes CROWD_MIN_VIEWS the moment becomes a spike (kind "crowd") — drafted by catch-up when no sentinel
    has it — and the creator turns hot, so cycle promotes them to a full sentinel."""
    def __init__(self, every_s: float = 60.0):
        super().__init__(daemon=True)
        self.logins: list[str] = []
        self.every_s, self.stop_flag = every_s, False
        self.fired: dict[str, float] = {}           # slug -> when we fired on it
        self.below: dict[str, float] = {}           # slug -> when we last saw it under CROWD_MIN_VIEWS
        self.seen: dict[str, float] = {}            # slug -> when we first saw it (how soon after it was made: A1)
        self.hot: dict[str, float] = {}             # login -> hot until
        self.out: list[dict] = []
        self.lock = threading.Lock()

    def lists(self, logins: list[str], now: float) -> tuple[list[tuple[str, list[dict]]], str]:
        """[(login, clip nodes)] and where they came from: Helix (every clip of the last 2 h, by creation time) when
        there's a Twitch pass, else the public top-100-by-views list. Helix failing mid-poll falls back for that poll."""
        from . import helix as HX
        from . import twitch as T
        if logins and HX.token():
            try:
                ids = HX.user_ids(logins)
                return [(l, HX.recent_clips(ids[l], now - CROWD_LATE_S)) for l in logins if l in ids], "helix"
            except Exception:
                pass
        out = []
        for i in range(0, len(logins), 25):
            for u in T.gql(CROWD_Q, {"ls": logins[i:i + 25]}).get("users") or []:
                out.append(((u or {}).get("login", ""),
                            [e.get("node") or {} for e in ((u or {}).get("clips") or {}).get("edges") or []]))
        return out, "gql"

    def poll(self, now: float | None = None) -> list[dict]:
        now = now or time.time()
        logins = [l for l in self.logins if not l.startswith(("kick:", "yt"))]
        got, full, ages = [], [], []
        lists, src = self.lists(logins, now)
        for login, nodes in lists:
            if src == "gql" and saturated(nodes):
                full.append(login)
            for n in nodes:
                if n.get("slug") and n["slug"] not in self.seen:
                    self.seen[n["slug"]] = now
                    try:
                        age = now - dt.datetime.fromisoformat(n["createdAt"].replace("Z", "+00:00")).timestamp()
                        if 0 <= age <= CROWD_FRESH_S:
                            ages.append(round(age))
                    except (KeyError, ValueError, AttributeError):
                        pass
            for n in nodes:
                sp = crowd_spike(login, n, now, self.fired, self.below)
                if sp:
                    self.fired[n["slug"]] = now
                    self.below.pop(n["slug"], None)
                    if not sp.get("late"):              # an old moment says nothing about who's hot now
                        self.hot[sp["login"]] = now + CROWD_HOT_S
                    got.append(sp)
                elif n.get("slug") and int(n.get("viewCount") or 0) < CROWD_MIN_VIEWS:
                    self.below[n["slug"]] = now
        if logins:
            log_crowd(now, len(logins), full, src=src, ages=ages)
        for l in [l for l in self.logins if l.startswith("kick:")]:     # Kick: its own clips listing, one call each
            try:
                from . import kick as K
                for n in K.crowd_nodes(l, now):
                    sp = crowd_spike(l, n, now, self.fired)
                    if sp:
                        self.fired[n["slug"]] = now
                        self.hot[sp["login"]] = now + CROWD_HOT_S
                        got.append(sp)
            except Exception:
                pass                                        # kick.com refused / slow: Twitch's clips still count
        for slug in [k for k, t in self.fired.items() if now - t > 6 * 3600]:
            self.fired.pop(slug)
        for slug in [k for k, t in self.below.items() if now - t > CROWD_LATE_S]:
            self.below.pop(slug)
        for slug in [k for k, t in self.seen.items() if now - t > CROWD_LATE_S + 3600]:
            self.seen.pop(slug)
        return got

    def run(self):
        while not self.stop_flag:
            try:
                got = self.poll()
                if got:
                    with self.lock:
                        self.out += got
            except Exception:
                pass
            time.sleep(self.every_s)

    def take(self) -> list[dict]:
        with self.lock:
            got, self.out = self.out, []
        return got


def crowd_spike(login: str, n: dict, now: float, fired: dict, below: dict | None = None) -> dict | None:
    """A viewer clip -> a spike record, if it has views, wasn't fired on already, and is fresh — or passed
    CROWD_MIN_VIEWS late, after we saw it under them (`below`), within CROWD_LATE_S (marked "late") (pure).
    A clip first seen already old and over the bar is not a late pass: the sweep / catch-up own those."""
    try:
        t = dt.datetime.fromisoformat(n["createdAt"].replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError, AttributeError):
        return None
    if not login or n.get("slug") in fired or int(n.get("viewCount") or 0) < CROWD_MIN_VIEWS:
        return None
    age = now - t
    late = age > CROWD_FRESH_S
    if late and (age > CROWD_LATE_S or n.get("slug") not in (below or {})):
        return None
    # a clip is made 0.5-155 s after its moment ends (U2, p05-p95; 50 clips): the window reaches back 180 s + the clip
    sp = {"login": login.lower(), "kind": "crowd", "fire": round(t - 30, 1), "peak": round(t, 1), "end": round(t, 1),
          "start": round(t - 180 - float(n.get("durationSeconds") or 30), 1), "stop": round(t + 20, 1), "p": 1.0, "views": int(n["viewCount"]),
          "title": (n.get("title") or "")[:80]}
    if late:
        sp["late"] = round(age)
    return sp


def saturated(nodes: list[dict], page: int = CROWD_PAGE) -> bool:
    """The clip list is full and its lowest clip already has CROWD_MIN_VIEWS: a fresh clip with that many can't
    show (pure)."""
    return len(nodes) >= page and int(nodes[-1].get("viewCount") or 0) >= CROWD_MIN_VIEWS


def log_crowd(now: float, n: int, full: list[str], path: str | None = None, src: str = "gql",
              ages: list[int] | None = None) -> None:
    """One line per clip poll: how many creators, whose list was too full to show a fresh clip, where the clips came
    from (helix | gql) and how many seconds after it was made each clip first seen this poll was. Keeps ~8 days."""
    path = path or CROWD_LOG
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as fh:
            fh.write(json.dumps({"t": round(now, 1), "n": n, "full": sorted(l for l in full if l), "src": src,
                                 **({"ages": ages} if ages else {})}) + "\n")
        if os.path.getsize(path) > 3_000_000:
            keep = [r for r in _jsonl(path) if r.get("t", 0) >= now - PLAN_KEEP_S]
            with open(path + ".tmp", "w") as fh:
                fh.writelines(json.dumps(r) + "\n" for r in keep)
            os.replace(path + ".tmp", path)
    except OSError:
        pass


def run(stop_after_s: float | None = None) -> None:
    """The tier-1 process: follows runs/tier1/channels.json, judges every channel every STEP_S."""
    os.makedirs(DIR, exist_ok=True)
    open(PIDF, "w").write(str(os.getpid()))
    multi = MultiChat()
    multi.start()
    m = SN.model()
    judges: dict[str, Judge] = {}
    chats: dict[str, object] = {}
    warm_q: queue.Queue = queue.Queue()

    def warmer():                                   # baseline from each stream's replay, one at a time
        while True:
            j = warm_q.get()
            try:
                if not j.login.startswith("kick:") and SN.warm_history(j.login, j.hist, j.keep):
                    j.warm = True
            except Exception:
                pass
    threading.Thread(target=warmer, daemon=True).start()
    crowd = CrowdWatch()
    crowd.start()
    t0 = time.time()
    t_next, last_cfg, last_state = time.time() + SN.STEP_S, 0.0, 0.0
    previously_connected: set[str] = set()
    print(f"[tier1] chat-only watcher started (trigger p ≥ {m['trigger']})", flush=True)
    while stop_after_s is None or time.time() - t0 < stop_after_s:
        time.sleep(max(0.0, t_next - time.time()))
        now, t_next = time.time(), t_next + SN.STEP_S
        if now - last_cfg >= 15:
            last_cfg = now
            want = set(want_channels())
            for l in want - set(judges):
                judges[l] = Judge(l, m)
                chats[l] = _chat_for(l, multi)
                warm_q.put(judges[l])
            for l in set(judges) - want:
                judges.pop(l, None)
                c = chats.pop(l, None)
                if hasattr(c, "stop_flag"):
                    c.stop_flag = True
            multi.set_channels({l for l in want if not l.startswith("kick:")})
            crowd.logins = sorted(want)
        spikes = []
        for l, j in judges.items():
            sp = j.step(now, chats[l].window(now - SN.WINDOW_S, now))
            if sp:
                sp["covered"] = sentinel_covers(l)
                sp["at"] = now
                spikes.append(sp)
                print(f"[tier1] {l}: spike p={sp['p']:.2f}" + (" (a sentinel has it)" if sp["covered"] else ""),
                      flush=True)
        for sp in crowd.take():                          # viewers are clipping something right now
            if any(o["login"] == sp["login"] and abs(o["fire"] - sp["fire"]) < 120 for o in spikes):
                continue
            sp["covered"] = sentinel_covers(sp["login"])
            sp["at"] = now
            spikes.append(sp)
            print(f"[tier1] {sp['login']}: viewers clipping ({sp['views']} views)"
                  + (" (a sentinel has it)" if sp["covered"] else ""), flush=True)
        if spikes:
            with open(SPIKES, "a") as fh:
                for sp in spikes:
                    fh.write(json.dumps(sp) + "\n")
        if now - last_state >= 15:
            connected = {l for l, chat in chats.items() if getattr(chat, "connected", False)}
            overlap = sorted(connected & previously_connected)
            if overlap and now - last_state <= 30:
                try:
                    with open(os.path.join(os.path.dirname(STATE), "coverage.jsonl"), "a") as stream:
                        stream.write(json.dumps({"start": last_state, "end": now, "channels": overlap}) + "\n")
                except OSError as exc:
                    print(f"[tier1] could not save coverage sample: {exc}", flush=True)
            previously_connected = connected
            last_state = now
            st = {"pid": os.getpid(), "beat": now, "connected": multi.connected, "msgs_total": multi.total,
                  "channels": {l: {"p": round(j.p, 3), "chat_per_min": j.chat_per_min(),
                                   "heat": round(max(j.heat(now), 1.0 if crowd.hot.get(l, 0) > now else 0.0), 3),
                                   "crowd_hot": crowd.hot.get(l, 0) > now,
                                   "connected": bool(getattr(chats[l], "connected", False)), "warm": j.warm,
                                   "ready": len(j.hist.get("users", [])) >= SN.MIN_HISTORY, "spikes": j.spikes}
                               for l, j in judges.items()}}
            tmp = STATE + ".tmp"
            json.dump(st, open(tmp, "w"))
            os.replace(tmp, STATE)
    multi.stop_flag = True


# ---- cycle side ---------------------------------------------------------------------------------

def running() -> bool:
    from . import ops as O
    try:
        return O._alive(int(open(PIDF).read().strip()))
    except (OSError, ValueError):
        return False


def ensure(channels: list[str]) -> str | None:
    """cycle: the channel list + the process (started when there is someone to watch). One note or None."""
    from . import ops as O
    import sys
    write_channels(channels)
    if not channels:
        return None
    if running():
        return None
    os.makedirs(DIR, exist_ok=True)
    pid = O._detach([sys.executable, "-m", "clipengine", "tier1"], os.path.join(REPO, "runs", "tier1.log"))
    open(PIDF, "w").write(str(pid))
    return f"tier 1 started on {len(channels)} creator(s)"


def stop() -> bool:
    import signal
    try:
        os.kill(int(open(PIDF).read().strip()), signal.SIGTERM)
        return True
    except (OSError, ValueError):
        return False


def tier1_channels(roster: dict, live: set[str], cfg: dict) -> list[str]:
    """Every live roster creator whose clipping is allowed (pure). YouTube sources stay audio-only (tier 2)."""
    return coverage_plan(roster, live, cfg)[0]


PLAN = os.path.join(DIR, "plan.jsonl")
COVERAGE = os.path.join(DIR, "coverage.jsonl")
PLAN_KEEP_S = 8 * 86400
NO_CYCLE_S = 900            # a moment more than this after the last cycle's plan: no cycle was running


def coverage_plan(roster: dict, live: set[str], cfg: dict, status: dict | None = None,
                  check_error: str | None = None, game: str = "") -> tuple[list[str], dict[str, str]]:
    """(channels the chat scout watches, {login: why it doesn't}) for the roster (pure). `status` is the roster's
    own live check (twitch.live_status); creators it shows offline are simply absent from both."""
    from . import autopilot as AP
    cap = int(cfg.get("tier1_max", 60))
    watch, why = [], {}
    try:
        from . import yields as YL
        y = YL.load()
    except Exception:
        y = {}
    order = sorted(roster, key=lambda l: -y.get(l, 0.5)) if y else list(roster)   # over the cap: best yield first
    for l in order:
        if l.startswith("yt"):
            continue                                   # YouTube: audio-only (tier 2), no chat scout
        if AP.policy(l, roster) == "never":
            continue
        if l in live:
            if len(watch) < cap:
                watch.append(l)
            else:
                why[l] = f"over the chat-scout cap (tier1_max {cap})"
            continue
        if ":" in l:
            continue                                   # Kick: its own live check, nothing to explain here
        st = (status or {}).get(l.lower()) if status is not None else None
        if status is None:
            why[l] = ("not in the radar's top streams; the roster live check failed"
                      + (f" ({check_error[:40]})" if check_error else ""))
        elif st and game and (st.get("game") or "").lower() != game.lower():
            why[l] = f"live, playing {st.get('game') or 'no category'}"
    return watch, why


def log_plan(now: float, watched: list[str], left_out: dict[str, str], path: str | None = None) -> None:
    """One line per cycle: who the chat scout was told to watch and why anyone live wasn't (scouts reads it to
    say WHY a viewer-clipped moment had no scout). Keeps 8 days."""
    path = path or PLAN
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps({"at": now, "watched": sorted(watched), "left_out": left_out}) + "\n")
    try:
        if os.path.getsize(path) > 2_000_000:
            keep = [r for r in _jsonl(path) if r.get("at", 0) >= now - PLAN_KEEP_S]
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                fh.writelines(json.dumps(r) + "\n" for r in keep)
            os.replace(tmp, path)
    except OSError:
        pass


def _jsonl(path: str) -> list[dict]:
    out = []
    try:
        for line in open(path):
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return out


def scout_records(since: float, root: str | None = None) -> tuple[list[dict], dict[str, list[tuple[float, float]]]]:
    """(cycle plans since `since` minus a margin, {login: [(start, end)] the chat scout was connected})."""
    d = os.path.join(root, "tier1") if root else DIR
    every = sorted((r for r in _jsonl(os.path.join(d, "plan.jsonl")) if isinstance(r.get("at"), (int, float))),
                   key=lambda r: r["at"])
    # the window's plans, plus the last one before it: a moment after a long silence still reads "no cycle ran",
    # while a moment before the FIRST plan on record has no records to say (plans began 24 Nov; Muse's 3-day funnel
    # counted 204 pre-record moments as "autopilot down")
    plans = [r for r in every if r["at"] >= since - NO_CYCLE_S]
    older = [r for r in every if r["at"] < since - NO_CYCLE_S]
    plans = older[-1:] + plans
    spans: dict[str, list[tuple[float, float]]] = {}
    for r in _jsonl(os.path.join(d, "coverage.jsonl")):
        try:
            if r["end"] < since - 60:
                continue
            for l in r.get("channels") or []:
                spans.setdefault(l, []).append((float(r["start"]), float(r["end"])))
        except (KeyError, TypeError, ValueError):
            continue
    return plans, spans


def scout_reason(login: str, t: float, plans: list[dict], spans: dict) -> str | None:
    """Was a chat scout on `login` at moment `t`? "watched", else why not (from the cycle plan in force then), else
    None when there are no records to say (an older install). Pure."""
    if any(a - 20 <= t <= b + 20 for a, b in spans.get(login, ())):
        return "watched"
    if not plans:
        return None
    import bisect
    i = bisect.bisect_right([p["at"] for p in plans], t) - 1
    if i < 0:
        return None                                    # before the first plan on record: nothing to say
    if t - plans[i]["at"] > NO_CYCLE_S:
        return "no cycle ran in the 15 min before (autopilot down?)"
    p = plans[i]
    if login in (p.get("watched") or ()):
        return "on the chat scout's list, but it wasn't connected then"
    if login in (p.get("left_out") or {}):
        return p["left_out"][login]
    return "offline at the last cycle's check — the stream started between cycles"


def promoted(state: dict, t1: dict, cfg: dict, now: float, live: set[str] | None = None) -> list[str]:
    """Creators to run as tier 2 because their tier-1 chat is hot: heat ≥ the bar now, held for a while
    after (no flapping). Updates state["promoted"]; returns the hottest first (pure but for state)."""
    bar = float(cfg.get("tier1_promote_p") or SN.model()["trigger"])
    hold = float(cfg.get("tier1_hold_min", 20)) * 60
    prom = state.setdefault("promoted", {})
    fresh = now - (t1.get("beat") or 0) < 120
    heat = {}
    for l, c in ((t1.get("channels") or {}).items() if fresh else ()):
        if c.get("heat", 0) >= bar:
            prom[l] = {"until": now + hold, "heat": c["heat"]}
        heat[l] = c.get("heat", 0)
    for l in list(prom):
        if prom[l]["until"] < now or (live is not None and l not in live):
            prom.pop(l)
    return sorted(prom, key=lambda l: -max(heat.get(l, 0), prom[l].get("heat", 0)))


def spike_catchups(state: dict, roster: dict, now: float, cfg: dict) -> list[dict]:
    """Tier-1 spikes no sentinel covered, on creators with saved VODs, old enough that the VOD has them
    (published + its chat replay caught up): what the catch-up path should draft now (pure but for files)."""
    from . import vodaccess as VA
    wait = float(cfg.get("tier1_vod_wait_min", 4)) * 60
    done = set(state.get("spikes_done") or [])
    restricted = VA.restricted_set(now)
    out = []
    for sp in load_spikes(now - 6 * 3600):
        c = roster.get(sp["login"]) or {}
        if spike_key(sp) in done or sp.get("covered") or c.get("vods") is False or sp["login"].startswith("yt") \
                or sp["login"].lower() in restricted:        # subscriber-only VODs: nothing to draft from
            continue
        if now - sp["end"] < wait:
            continue
        out.append(sp)
    return out


def spike_key(sp: dict) -> str:
    return f"{sp['login']}@{int(sp['fire'])}"


def start_spike_catchup(state: dict, spikes: list[dict]) -> str | None:
    """One background catch-up for these spikes' windows (each creator, the spike ± a minute). A busy
    catch-up leaves them for the next cycle; started ones are marked done in state."""
    from . import autopilot as AP
    from . import ops as O
    import sys
    if not spikes:
        return None
    if AP._alive(AP._read_pid(AP.CATCHUP_PID)):
        return None                                      # busy: next cycle
    os.makedirs(DIR, exist_ok=True)
    qf = os.path.join(DIR, "spike_queue.json")
    json.dump({"spikes": spikes}, open(qf, "w"))
    pid = O._detach([sys.executable, "-m", "clipengine", "catchup", "--spikes", qf],
                    os.path.join(REPO, "runs", "catchup.log"))
    open(AP.CATCHUP_PID, "w").write(str(pid))
    state["spikes_done"] = (list(state.get("spikes_done") or []) + [spike_key(s) for s in spikes])[-500:]
    names = sorted({s["login"] for s in spikes})
    return f"tier 1: {len(spikes)} chat spike(s) on {', '.join(names)} — drafting from the saved stream (catch-up)"


def show() -> dict:
    """`clipengine sentinels`: the tier-1 line."""
    st = load_state()
    ch = st.get("channels") or {}
    fresh = time.time() - (st.get("beat") or 0) < 120
    if not ch and not want_channels():
        return st
    if not fresh or not running():
        print(f"tier 1  DOWN — chat-only watcher not running ({len(want_channels())} creator(s) wanted) — `cycle` starts it")
        return st
    hot = sorted(ch.items(), key=lambda kv: -kv[1].get("heat", 0))
    print(f"tier 1  chat only, {len(ch)} creator(s), {'connected' if st.get('connected') else 'CONNECTING'}: "
          + ", ".join(f"{l} {c.get('heat', 0):.2f}{'' if c.get('ready') else '…'}" for l, c in hot[:12])
          + (f" +{len(hot) - 12} more" if len(hot) > 12 else ""))
    return st
