"""review.html — the human gate in one page, no downloads needed.

Each candidate card has everything v1's operator had to assemble by hand:
the score and why, a Twitch link that opens the VOD at the exact second,
what chat said, what was said on stream, the probe's flags, the game from
Twitch's chapter, the chosen window, and the exact render command with
the 12 header types to pick from. Self-contained HTML, no scripts.
"""
from __future__ import annotations

import html
import os

from . import headers as H
from . import untrusted as UT
from .twitch import fmt


def _twitch_link(vod_id: str, t: float) -> str:
    t = max(0, int(t))
    return f"https://www.twitch.tv/videos/{vod_id}?t={t // 3600}h{t % 3600 // 60}m{t % 60}s"


def _spark(timeline: list[dict], cands: list[dict], w: int = 900, h: int = 70) -> str:
    if not timeline:
        return ""
    t0, t1 = timeline[0]["t"], timeline[-1]["t"] or 1
    mx = max(p["s"] for p in timeline) or 1
    span = max(1, t1 - t0)
    pts = " ".join(f"{(p['t'] - t0) / span * w:.1f},{h - p['s'] / mx * (h - 6):.1f}" for p in timeline)
    marks = "".join(
        f'<line x1="{(c["peak_s"] - t0) / span * w:.1f}" x2="{(c["peak_s"] - t0) / span * w:.1f}" '
        f'y1="0" y2="{h}" class="{"m-bad" if c.get("hard_fail") else "m"}"/>'
        f'<text x="{(c["peak_s"] - t0) / span * w + 2:.1f}" y="10" class="mt">{c["rank"]}</text>'
        for c in cands)
    return (f'<svg viewBox="0 0 {w} {h}" class="spark" preserveAspectRatio="none">'
            f'<polyline points="{pts}" class="line"/>{marks}</svg>')


def write_review(res: dict, path: str) -> None:
    vod = res["vod"]
    e = html.escape
    name = H.short_name(vod["login"], vod["display_name"])
    cards = []
    for c in res["candidates"]:
        win = c.get("window") or {"start_s": c["clip_start_s"], "end_s": c["clip_end_s"]}
        status = ("drop" if c.get("hard_fail") else
                  "warn" if c.get("flags") else "ok") if c.get("probed") else "unprobed"
        badge = {"drop": "AUTO-DROP", "warn": "CHECK", "ok": "CLEAN", "unprobed": "NOT PROBED"}[status]
        why = c.get("hard_fail") or "; ".join(c.get("flags") or []) or "no audio problems found"
        chat = "".join(f"<li><b>{n}×</b> {e(t[:80])}</li>" for t, n in (c.get("chat_context") or [])[:8])
        types = " ".join(f'<span class="ty" title="{e(q)}">{k}</span>' for k, (lab, q) in H.TYPES.items())
        cr = c.get("crowd")
        crowd_line = (f'<p class="crowd">🔥 Viewers clipped this: <b>{cr["clips"]} clip{"s" if cr["clips"] > 1 else ""}, '
                      f'{cr["views"]:,} views</b> ({cr["views_per_min"]}/min) — '
                      + " · ".join(e(UT.quote(t, 60)) for t in cr["titles"]) + "</p>") if cr else ""
        labels = ("start", "chat peak", "middle", "end")
        thumbs = ('<div class="thumbs">' + "".join(
            f'<figure><img src="{u}" alt="{lab} frame"><figcaption>{lab}</figcaption></figure>'
            for u, lab in zip(c.get("thumbs") or [], labels) if u) + "</div>") if c.get("thumbs") else ""
        cmd = (f"python -m clipengine render --scan {e(os.path.dirname(path))} --pick {c['rank']} "
               f"--type TYPE")
        cards.append(f"""
<section class="card {status}">
  <header><span class="rank">#{c['rank']}</span>
    <span class="badge {status}">{badge}</span>
    <span class="t">{fmt(win['start_s'])} → {fmt(win['end_s'])}</span>
    <span class="g">{e(c.get('game') or '')} · {e(c.get('crop', ''))}</span>
    <a class="watch" href="{_twitch_link(vod['id'], win['start_s'])}" target="_blank" rel="noopener">▶ watch on Twitch</a>
  </header>
  {crowd_line}
  <p class="why">{e(why)}</p>
  {thumbs}
  <div class="grid">
    <div><h4>Score {c.get('score', 0)}</h4>
      <ul class="kv">
        <li>chat {c['chat_score']} <small>(lift {c['lift']}, react {c['react']}, junk {c['junk']}, {c['reaction']})</small></li>
        <li>audio ×{c.get('audio_factor', '–')} <small>speech {win.get('speech_s', '–')}s, {win.get('words', '–')} words, first word +{win.get('first_word_at', '–')}s, longest gap {win.get('longest_gap', '–')}s</small></li>
        <li>loudness {c.get('mean_db', '–')} dB, muted {int((c.get('muted_frac') or 0) * 100)}%, excitement +{c.get('excitement_db', 0)} dB</li>
        <li>chat peak {c['peak']} ({c['users_10s']} chatters/10s vs {c['baseline_users']} usual)</li>
      </ul></div>
    <div><h4>Chat said</h4><ul class="chat">{chat or '<li>–</li>'}</ul></div>
  </div>
  <h4>On stream</h4><p class="tr">{e((c.get('transcript_text') or '(not probed)')[:900])}</p>
  <details><summary>Render it</summary>
    {('<p class="why"><b>AUTO-DROP: ' + e(c['hard_fail']) + '.</b> Rendering it anyway is an override — say why in the tracker.</p>') if c.get('hard_fail') else ''}
    <p>Pick the type whose question is a clear YES for this clip — header becomes “{e(name)} &lt;label&gt;”. No type fits → no header → skip it.</p>
    <p class="types">{types}</p>
    <code>{cmd}</code></details>
</section>""")
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Clip review · {e(name)}</title>
<style>
:root{{--bg:#0f1115;--fg:#e8e8ea;--mut:#9aa0aa;--card:#171a21;--line:#2a2f3a;--ok:#3fb27f;--warn:#e0a83e;--bad:#e05a5a;--acc:#8ab4ff}}
@media (prefers-color-scheme: light){{:root{{--bg:#f6f6f4;--fg:#1b1d22;--mut:#5d636e;--card:#fff;--line:#dcdde1;--acc:#2456c7}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{max-width:960px;margin:0 auto;padding:20px 16px 60px}}
h1{{font-size:22px;margin:0 0 4px}} .sub{{color:var(--mut);margin:0 0 16px}}
.spark{{width:100%;height:70px;background:var(--card);border:1px solid var(--line);border-radius:8px}}
.line{{fill:none;stroke:var(--acc);stroke-width:1}} .m{{stroke:var(--ok);stroke-width:1.5}} .m-bad{{stroke:var(--bad);stroke-dasharray:3 3}}
.mt{{fill:var(--mut);font-size:9px}}
.card{{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--line);border-radius:10px;padding:14px 16px;margin:14px 0}}
.card.ok{{border-left-color:var(--ok)}} .card.warn{{border-left-color:var(--warn)}} .card.drop{{border-left-color:var(--bad);opacity:.72}}
header{{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center}}
.rank{{font-weight:700;font-size:18px}} .t{{font-variant-numeric:tabular-nums}} .g{{color:var(--mut)}}
.badge{{font-size:11px;font-weight:700;padding:2px 8px;border-radius:99px;border:1px solid}}
.badge.ok{{color:var(--ok)}} .badge.warn{{color:var(--warn)}} .badge.drop{{color:var(--bad)}} .badge.unprobed{{color:var(--mut)}}
.watch{{margin-left:auto;color:var(--acc);text-decoration:none;font-weight:600}}
.why{{color:var(--mut);margin:6px 0}}
.crowd{{margin:6px 0;color:var(--warn)}}
.thumbs{{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin:8px 0}}
.thumbs figure{{margin:0}} .thumbs img{{width:100%;border-radius:6px;display:block;background:#000}}
.thumbs figcaption{{font-size:11px;color:var(--mut);text-align:center}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px}} @media (max-width:640px){{.grid{{grid-template-columns:1fr}}}}
h4{{margin:10px 0 4px;font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut)}}
ul{{margin:0;padding-left:18px}} .kv small{{color:var(--mut)}}
.tr{{margin:0;font-style:italic}}
.ty{{display:inline-block;border:1px solid var(--line);border-radius:6px;padding:1px 6px;margin:2px;font-size:12px}}
code{{display:block;overflow-x:auto;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:8px;font-size:12px}}
</style></head><body><main>
<h1>{e(name)} — {e(UT.quote(vod['title'], 90))}</h1>
<p class="sub">VOD {vod['id']} · {fmt(res['range'][0])}–{fmt(res['range'][1])} · {res['messages']:,} chat messages ·
scan {res['timing_s']['total']}s (chat {res['timing_s']['chat']}s, audio probe {res['timing_s']['probe']}s) ·
chapters: {e(', '.join(f"{fmt(c['start_s'])} {c['game']}" for c in vod['chapters']))}</p>
{_spark(res.get('timeline', []), res['candidates'])}
<p class="sub">Ranked by chat reaction × audio. A score finds candidates; it is not the verdict — watch each one.
Nothing here is posted or approved.</p>
{''.join(cards)}
</main></body></html>"""
    open(path, "w", encoding="utf-8").write(doc)


def draft_page(entry: dict, title: str | None = None, quote: str | None = None) -> str:
    """Build a self-contained local review beside the draft, using escaped evidence."""
    import base64
    import html
    import re
    from pathlib import Path

    from . import judge
    from . import sentinel
    from . import untrusted

    def text(value):
        return html.escape(str(value))

    def evidence(value, limit=160):
        return text(untrusted.quote(str(value), limit))

    tags = []
    path = entry.get("sheet")
    if path and Path(path).is_file():
        data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
        tags.append(f'<img src="data:image/jpeg;base64,{data}" alt="Draft contact sheet">')
    elif entry.get("thumbs"):
        for source in entry["thumbs"]:
            if re.fullmatch(r"data:image/(?:jpeg|png|webp);base64,[A-Za-z0-9+/=]+", source):
                tags.append(f'<img src="{source}" alt="Draft frame">')
    if not tags:
        tags.append("<p>Frames unavailable</p>")
    start = entry["start"]
    duration = max(0.0, entry["stop"] - start)
    payoff = entry.get("payoff")
    timeline = "Payoff timestamp unavailable"
    if payoff is not None and duration > 0:
        offset = min(duration, max(0, payoff - start))
        timeline = (f'<meter min="0" max="{duration:.2f}" value="{offset:.2f}"></meter>'
                    f'<p>Payoff +{offset:.1f}s of {duration:.1f}s</p>')
    words = judge._draft_words(entry)
    stretches = judge.stretches(words)
    if stretches:
        transcript = "".join(f"<p>+{max(0, row['start'] - start):.1f}s · {text(row['spk'])} "
                             f"{evidence(row['text'], 500)}</p>" for row in stretches)
    else:
        spoken, _ = judge.draft_evidence(entry)
        transcript = f"<p>{evidence(spoken, 650) if spoken else 'Transcript unavailable'}</p>"
    check = "<p>Pass --title to check a proposed headline.</p>"
    if title:
        preview = judge.check_headline(entry, title, quote)
        issues = preview["problems"] + [f"unsupported claim: {term}" for term in preview["unsupported"]]
        headline_result = '; '.join(issues) or 'Supported by the draft evidence'
        check = (f"<p>{text(title)}</p><p>{text(headline_result)}</p>"
                 f"<p>First CAPS run: {text(preview['first_caps_run'] or 'none')}</p>")
    marks = sentinel.confidence_markers(entry)
    safety = evidence(entry["unsafe"]) if entry.get("unsafe") else "No text flag recorded"
    if "frames unassessed" in marks:
        safety += " · frames unassessed"
    flags = "; ".join(entry.get("flags") or [])
    confidence = ("Confidence: " + text("; ".join(marks)) if marks else "Confidence: every check ran") + \
        (" · flags: " + evidence(flags) if flags else "")
    title_html = text(entry["id"])
    ident = re.sub(r"[^a-zA-Z0-9_.-]", "_", entry["id"]).strip(".") or "draft"
    target = Path(entry["rec"]) / "drafts" / f"{ident}.review.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"""<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Draft {title_html}</title>
<style>body{{font:16px/1.5 system-ui;margin:auto;max-width:44rem;padding:1rem;background:#f7f8fa;color:#151923}}
section{{background:white;border:1px solid #d9dde5;border-radius:.6rem;padding:.8rem;margin:1rem 0}}
h1,h2,p{{margin:.4rem 0}}img,meter{{width:100%;height:auto}}img{{margin:.25rem 0}}meter{{height:1rem}}</style>
<h1>{title_html}</h1><section><h2>Frames</h2>{''.join(tags)}{timeline}</section>
<section><h2>Score and safety</h2><p>Reviewing artifact {text(judge.artifact_id(entry) or 'unavailable')} — a verdict is recorded once per artifact</p><p>Engine {text(entry.get('engine', 'unknown'))} · best {text(entry.get('best', 'unknown'))}</p>
<p>{text(entry.get('parts') or (entry.get('judgment') or {}).get('parts') or 'Score parts unavailable')}</p>
<p>{safety}</p><p>{confidence}</p></section>
<section><h2>Transcript</h2>{transcript}</section><section><h2>Headline check</h2>{check}</section>
""", encoding="utf-8")
    sentinel.mark_seen(entry)
    return str(target)
