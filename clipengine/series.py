"""Series memory (wishlist 14): a creator's recurring bit, found from our own winners, confirmed by an editor, and
watched for in the next drafts.

  key         creator + the headline's punch word (its first CAPS word: "Streamer A got WASTED" → streamera:WASTED)
  suggestion  two or more of our live winners (gate-approved, not owner overrides) share a key → a suggestion with
              receipts: each winner's slug, title, date and live link
  confirm     `clipengine series --confirm streamera:WASTED --name "A gets wasted"` (data/series.json): an editor
              says it is a series
  watch       a new draft from that creator whose own evidence (what was said, viewers' clip titles, chat) carries the
              punch word is labelled `series: <name>` in `queue` — an unrelated hit is not

The punch word comes from OUR headlines, so a series is our editorial call; stream text only has to contain it.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIN_WINS = 2
STOP = {"GTA", "VI", "NPC", "LOL", "OMG", "WTF", "THE", "AND"}


def _rows(path: str) -> list[dict]:
    try:
        return [json.loads(l) for l in open(path) if l.strip()]
    except (OSError, ValueError):
        return []


def punch(title: str) -> str | None:
    """The headline's first CAPS word (3+ letters), the bit's name."""
    for w in re.findall(r"[A-Za-z']+", title or ""):
        if len(w) >= 3 and w.isupper() and w not in STOP:
            return w
    return None


def key(login: str, title: str) -> str | None:
    p = punch(title)
    return f"{login.lower()}:{p}" if login and p else None


def _path(root: str | None) -> str:
    return os.path.join(root or REPO, "data", "series.json")


def confirmed(root: str | None = None) -> dict:
    try:
        return json.load(open(_path(root)))
    except (OSError, ValueError):
        return {}


def suggestions(root: str | None = None) -> list[dict]:
    """Keys with >= MIN_WINS live winners, each with its receipts. Owner overrides never count."""
    root = root or REPO
    live = {}
    for r in _rows(os.path.join(root, "ledger.jsonl")):
        if r.get("status") == "live" and not r.get("override") and r.get("slug"):
            live.setdefault(r["slug"], []).append(r)
    groups: dict[str, list[dict]] = {}
    for c in _rows(os.path.join(root, "data", "clipped.jsonl")):
        k = key(c.get("login") or "", c.get("title") or "")
        if not k or c.get("slug") not in live:
            continue
        posts = live[c["slug"]]
        groups.setdefault(k, []).append({"slug": c["slug"], "title": c.get("title"), "moment_at": c.get("moment_at"),
                                         "links": [p.get("url") for p in posts if p.get("url")]})
    done = confirmed(root)
    out = []
    for k, wins in sorted(groups.items()):
        uniq = list({w["slug"]: w for w in wins}.values())
        if len(uniq) >= MIN_WINS:
            out.append({"key": k, "wins": uniq, "confirmed": k in done, "name": (done.get(k) or {}).get("name")})
    return out


def confirm(k: str, name: str, root: str | None = None, by: str = "editor") -> dict:
    sug = {s["key"]: s for s in suggestions(root)}
    if k not in sug:
        raise SystemExit(f"series: {k} has fewer than {MIN_WINS} live winners — nothing to confirm "
                         "(`clipengine series` lists the suggestions)")
    data = confirmed(root)
    data[k] = {"name": name.strip(), "by": by, "at": dt.datetime.now(dt.timezone.utc).isoformat(),
               "receipts": [w["slug"] for w in sug[k]["wins"]]}
    os.makedirs(os.path.dirname(_path(root)), exist_ok=True)
    json.dump(data, open(_path(root), "w"), indent=1)
    return data[k]


def match(draft: dict, root: str | None = None, sugg: list[dict] | None = None) -> dict | None:
    """The series a new draft continues: same creator, and its own evidence carries the bit's punch word."""
    login = (draft.get("login") or "").lower()
    if not login:
        return None
    text = " ".join([str(draft.get("said") or ""), " ".join(draft.get("titles") or []), str(draft.get("chat") or ""),
                     str(draft.get("quote_hint") or "")]).lower()
    for s in sugg if sugg is not None else suggestions(root):
        who, word = s["key"].split(":", 1)
        if who == login and re.search(rf"\b{re.escape(word.lower())}\b", text):
            return {"key": s["key"], "name": s.get("name") or s["key"], "confirmed": s["confirmed"],
                    "wins": len(s["wins"])}
    return None


def lines(root: str | None = None) -> list[str]:
    out = []
    for s in suggestions(root):
        state = f"CONFIRMED «{s['name']}»" if s["confirmed"] else "suggested"
        out.append(f"{s['key']}: {state} — {len(s['wins'])} winners")
        for w in s["wins"]:
            out.append(f"   {w['slug']}  {(w['moment_at'] or '')[:10]}  {w['title']}  {' '.join(w['links'])}")
        if not s["confirmed"]:
            out.append(f"   confirm: clipengine series --confirm {s['key']} --name \"…\"")
    return out or [f"series: no creator has {MIN_WINS}+ live winners with the same punch word yet"]
