"""Absolute recording windows shared by the VOD, live and imported-post paths."""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import math
import re
from pathlib import Path

from . import handpost
from . import verify

TOLERANCE_S = 60


def timestamp(value) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed.timestamp()
        except ValueError:
            pass
    return None


def iso(value: float) -> str:
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat()


def metadata(login: str, start: float, end: float) -> dict:
    return {"login": login.casefold(), "moment_start_at": iso(start), "moment_end_at": iso(end)}


def window(row: dict) -> tuple[float, float] | None:
    start, end = timestamp(row.get("moment_start_at")), timestamp(row.get("moment_end_at"))
    if start is not None and end is not None and start <= end:
        return start, end
    at = timestamp(row.get("moment_at"))
    if at is None:
        return None
    a, b = row.get("start_s"), row.get("end_s")
    if isinstance(a, (float, int)) and isinstance(b, (float, int)) and a <= b:
        if row.get("source") == row.get("login") and a > 1_000_000_000:
            return a, b
        duration = b - a
        return at - duration / 2, at + duration / 2
    return at, at


def match(login: str, start: float, end: float, rows: list[dict]) -> dict | None:
    login = login.casefold()
    for row in rows:
        who = row.get("login") or row.get("creator")
        if not isinstance(who, str) or who.casefold() != login or not row.get("slug"):
            continue
        bounds = window(row)
        if bounds and start <= bounds[1] + TOLERANCE_S and bounds[0] <= end + TOLERANCE_S:
            return row
    return None


def _json_rows(path: Path) -> list[dict]:
    text = path.read_text().strip()
    if not text:
        return []
    try:
        rows = json.loads(text)
    except ValueError:
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    return rows if isinstance(rows, list) else [rows]


def _import_rows(path: Path) -> list[dict]:
    rows = _json_rows(path)
    sidecar = path.parent / "data" / "clipped.jsonl"
    by_slug = {r.get("slug"): r for r in _json_rows(sidecar)} if sidecar.is_file() else {}
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each import row must be an object")
        if row.get("status", "live") != "live":
            continue
        slug = row.get("slug", "")
        if not isinstance(slug, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", slug):
            raise ValueError("each imported post needs a valid slug")
        row = {**by_slug.get(slug, {}), **row}
        login = row.get("login") or row.get("creator")
        bounds = window(row)
        if not isinstance(login, str) or not login or not bounds:
            raise ValueError(f"{slug}: creator and moment time are required (or include the source data/clipped.jsonl)")
        links = row.get("links") or {p: row[p] for p in handpost.PLATFORMS if row.get(p)}
        if not links and row.get("platform"):
            links = {row["platform"]: row.get("url")}
        if not isinstance(links, dict) or not links:
            raise ValueError(f"{slug}: include at least one platform link")
        for platform, url in links.items():
            if platform not in handpost.PLATFORMS or not isinstance(url, str):
                raise ValueError(f"{slug}: invalid platform link")
            verify.live_url(platform, url)
            result.append({"slug": slug, "platform": platform, "url": url, "status": "live", "imported": True,
                           "title": row.get("title", slug), "sha256": row.get("sha256"),
                           "at": row.get("at") or iso(dt.datetime.now(dt.timezone.utc).timestamp()),
                           "moment_at": row.get("moment_at"), **metadata(login, *bounds)})
    return result


def import_posts(path: str) -> int:
    """Import another engine's live ledger or explicit post records, with idempotent retries."""
    rows = _import_rows(Path(path))
    if not rows:
        return 0
    ledger_path = handpost._dirs()[2]
    with open(ledger_path, "a+") as ledger:
        fcntl.flock(ledger, fcntl.LOCK_EX)
        existing = handpost._ledger()
        keys = {(r.get("slug"), r.get("platform"), r.get("url")) for r in existing if r.get("status") == "live"
                and window(r) and (r.get("login") or r.get("creator"))}
        added = []
        for row in rows:
            key = row["slug"], row["platform"], row["url"]
            if key not in keys:
                added.append(row)
                keys.add(key)
        ledger.write("".join(json.dumps(r) + "\n" for r in added))
    if added:
        handpost._sync([ledger_path], f"import {len(added)} live post records")
    return len(added)
