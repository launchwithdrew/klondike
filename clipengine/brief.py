"""Offline daily report measurements from the engine's local records."""

from __future__ import annotations

from collections import Counter
import datetime as dt
import json
from pathlib import Path
import statistics

from .moments import timestamp
from . import untrusted

DAY = 86400
PROGRAMS = {
    "youtube": (1000, 10_000_000, 90, "Owner: YouTube Studio → Earn, check qualified Shorts views and AdSense"),
    "tiktok": (10_000, 100_000, 30, "Owner: TikTok Studio → Creator Rewards, review account and video eligibility"),
}


def read(path: Path, fallback):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return fallback


def rows(path: Path) -> list[dict]:
    result = []
    try:
        with path.open() as stream:
            for line in stream:
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    result.append(value)
    except OSError:
        pass
    return result


def fmt_count(number) -> str:
    return f"{number:,}" if number is not None else "?"


def money(root: Path, now: float, ledger: list[dict], performance: list[dict]) -> tuple[str, list[str]]:
    follower_rows = rows(root / "data" / "followers.jsonl")
    posted = {(r["platform"], r["slug"]): timestamp(r.get("at")) for r in ledger
              if r.get("platform") and r.get("slug")}
    reads = {}
    for r in performance:
        at = timestamp(r.get("checked_at"))
        if r.get("views") is not None and at is not None and at <= now:
            reads.setdefault((r.get("platform"), r.get("slug")), []).append((at, int(r["views"])))
    for series in reads.values():
        series.sort()

    def views_since(platform, since):
        total = 0
        for key, series in reads.items():
            posted_at = posted.get(key)
            if key[0] != platform or posted_at is None or series[-1][0] < since:
                continue
            if posted_at >= since:
                total += series[-1][1]
            else:
                baseline = [views for at, views in series if at < since]
                if baseline:
                    total += max(0, series[-1][1] - baseline[-1])
        return total

    counts, tasks, estimates = {}, [], []
    for platform, (target, target_views, days, task) in PROGRAMS.items():
        change = sorted((at, int(r["followers"])) for r in follower_rows
                        if r.get("platform") == platform and r.get("followers") is not None
                        and (at := timestamp(r.get("at"))) is not None and at <= now)
        count = change[-1][1] if change else None
        views = views_since(platform, now - days * DAY)
        counts[platform] = (count, views)
        recent = [r for r in change if r[0] >= now - 14 * DAY]
        growth = 0.0
        if len(recent) >= 2 and recent[-1][0] - recent[0][0] >= 3 * DAY:
            growth = max(0.0, (recent[-1][1] - recent[0][1]) / ((recent[-1][0] - recent[0][0]) / DAY))
        view_pace = views_since(platform, now - 14 * DAY) / 14
        if count is None:
            continue
        for future in range(61):
            if count + future * growth < target:
                continue
            window_start = now - (days - future) * DAY
            remaining = views_since(platform, window_start) if window_start <= now else 0
            if remaining + min(future, days) * view_pace >= target_views:
                estimates.append(f"{platform} ~{future}d" if future else f"{platform} targets reached")
                tasks.append(task)
                break
    y, t = counts["youtube"], counts["tiktok"]
    line = (f"money: {fmt_count(y[0])}/1,000 subs + {fmt_count(y[1])}/10M Shorts (90d) · "
            f"TikTok {fmt_count(t[0])}/10k followers + {fmt_count(t[1])}/100k views (30d)")
    line += f" · {'; '.join(estimates)}" if estimates else " · enrolment parked"
    return line, tasks


def timing(drafts: list[dict], ledger: list[dict], since: float, now: float) -> tuple[str, str]:
    from . import prerender

    posted = {}
    for row in ledger:
        if row.get("slug") and (at := timestamp(row.get("at"))) is not None:
            slug = row["slug"]
            posted[slug] = min(at, posted.get(slug, at))
    windows = [[], [], [], []]
    marks = Counter()
    for row in drafts:
        moment = timestamp(row.get("at")) or timestamp(row.get("payoff")) or timestamp(row.get("start"))
        created = timestamp(row.get("drafted_at"))
        pre = None
        if row.get("pre") and row.get("rec"):
            pre = timestamp(read(Path(prerender.paths(row)[1]), {}).get("at"))
        live = timestamp(row.get("posted_at")) or posted.get(row.get("slug"))
        for name in ("seen_at", "judged_at"):
            at = timestamp(row.get(name))
            if at is not None and since <= at <= now:
                marks[name] += 1
        if not any(at is not None and since <= at <= now for at in (moment, created, pre, live)):
            continue
        for bucket, a, b in zip(windows, (moment, created, pre, moment), (created, pre, live, live)):
            if a is not None and b is not None and a <= b <= now:
                bucket.append((b - a) / 60)
    med = [f"{statistics.median(bucket):.1f}m" if bucket else "–" for bucket in windows]
    posted_count = sum(since <= at <= now for at in posted.values())
    return (
        f"speed median: moment→draft {med[0]} · draft→render {med[1]} · render→live {med[2]} · moment→live {med[3]}",
        f"pipeline: {marks['seen_at']} seen · {marks['judged_at']} judged · {posted_count} posted",
    )


def recording(root: Path, since: float, now: float) -> list[str]:
    from . import attention
    from . import autopilot

    holes, oldest = [], None
    for rec in sorted(root.glob("*/index.jsonl")):
        gaps = autopilot._gaps(str(rec.parent))
        for gap in gaps:
            if since <= gap["t"] <= now and gap["len"] > 10:
                label = rec.parent.name.removeprefix("sentinel-")
                holes.append(f"{label} {gap['len']:.0f}s ({untrusted.clean(gap.get('cause') or 'unknown', 45)})")
        for row in rows(rec):
            chunk = row.get("f")
            at = timestamp(row.get("t"))
            if chunk and at is not None and (rec.parent / "segments" / chunk).is_file():
                oldest = at if oldest is None else min(at, oldest)
    latest = {e["key"]: e for e in attention._events(since, str(root)) if e.get("key", "").startswith("missed:")}
    late = [untrusted.clean(e["text"], 115) for e in latest.values() if e["kind"] != "resolved"]
    buffer = f"oldest re-cuttable {dt.datetime.fromtimestamp(oldest, dt.timezone.utc):%m-%d %H:%M} UTC" if oldest is not None else "buffer unavailable"
    headline = f"recording: {len(holes)} holes >10s · {len(late)} live-only late starts · {buffer} · VOD fallback when available"
    lines = [headline]
    if holes:
        lines.append("holes: " + " · ".join(holes))
    if late:
        lines.append("late: " + " · ".join(late))
    return lines


def series_line(root: Path, ledger: list[dict]) -> str | None:
    from . import learn

    live = {r.get("slug") for r in ledger}
    shapes = Counter()
    seen = set()
    for row in rows(root / "data" / "clipped.jsonl"):
        login, title, slug = row.get("login"), row.get("title"), row.get("slug")
        if not login or not title or slug not in live or slug in seen:
            continue
        duration = row.get("end_s", 0) - row.get("start_s", 0)
        if duration <= 0:
            continue
        seen.add(slug)
        features = learn.headline_features(title)
        hook = "number" if features["number"] == "has a number" else "quote" if features["quote"] == "has a quote" else "reaction"
        shapes[(login, hook, learn.length_bucket(duration))] += 1
    if not shapes:
        return None
    (login, hook, length), wins = shapes.most_common(1)[0]
    return f"forming: {login} {hook} clips ({length}, {wins} winners)" if wins >= 2 else None


def rights_line(roster: dict, now: float) -> str | None:
    stale = []
    for login, settings in roster.items():
        if settings.get("policy") == "never":
            continue
        checked = timestamp(settings.get("policy_checked"))
        if checked is None and isinstance(settings.get("policy_checked"), str):
            try:
                checked = dt.datetime.combine(dt.date.fromisoformat(settings["policy_checked"]), dt.time(), dt.timezone.utc).timestamp()
            except ValueError:
                pass
        if not settings.get("policy_source") or checked is None or not now - 90 * DAY <= checked <= now:
            stale.append(login)
    if not stale:
        return None
    example = ", ".join(stale[:3])
    return f"rights evidence: {len(stale)} missing or stale (>90d) · {untrusted.clean(example, 65)}"
