"""Brand pack — everything about how a channel looks and who it posts as, outside the code.

One file, `brand/brand.json` (override with CLIPENGINE_BRAND), plus the logo and fonts it
names. The engine's built-in defaults are a plain, neutral look; every channel's own pack sets
its colors, placement, fonts, captions, logo, accounts and hashtags. `clipengine init` writes a
pack (unique by default); `clipengine preview` shows it before anything posts.

  name        channel name (used in profile kit and logs)
  accounts    youtube {handle, channelId?} · instagram {username} · tiktok {username}
  hashtags    list; "{creator}" is replaced by the streamer's tag (config/creators.json)
  layout      kind: "stack" (zoomed action over the full frame) | "single" (full frame on blur)
              split: fraction of the height the top panel takes in "stack"
  card        headline card: show, position (seam | top | bottom), fill, text, font, size,
              radius, margin (side margin), max_lines
  quote       the streamer's-words tag: show, place (above | below), fill, text, font, size, radius
  captions    font (family name) + font_file, size, color, outline, outline_width, highlight
              (color of the word being said when words_per_card > 1), words_per_card,
              y (0..1, where captions sit), uppercase
  logo        file, corner (top-left | top-right | bottom-left | bottom-right), width, opacity, margin
  voice       file with the channel's headline rules
"""
from __future__ import annotations

import copy
import json
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONTS = os.path.join(REPO, "assets", "fonts")

DEFAULTS: dict = {
    "name": "My Clip Channel",
    "accounts": {"youtube": {"handle": "", "channelId": ""}, "instagram": {"username": ""}, "tiktok": {"username": ""}},
    "hashtags": ["{creator}"],
    "layout": {"kind": "stack", "split": 0.6},
    "card": {"show": True, "position": "top", "fill": "#111111", "text": "#FFFFFF", "font": "InterDisplay-Bold.ttf",
             "size": 56, "radius": 12, "margin": 60, "max_lines": 3},
    "quote": {"show": True, "place": "below", "fill": "#FFD400", "text": "#111111", "font": "InterDisplay-Bold.ttf",
              "size": 40, "radius": 10},
    "captions": {"font": "Inter Display", "font_file": "InterDisplay-Bold.ttf", "size": 72, "color": "#FFFFFF",
                 "outline": "#000000", "outline_width": 5, "highlight": "#FFD400", "words_per_card": 1,
                 "y": 0.56, "uppercase": True},
    "logo": {"file": "brand/kit/logo.png", "corner": "bottom-right", "width": 220, "opacity": 0.7, "margin": 32},
    "voice": "rules/headlines.md",
}


def path() -> str:
    return os.environ.get("CLIPENGINE_BRAND") or os.path.join(REPO, "brand", "brand.json")


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if k.startswith("_"):
            continue
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


_cache: dict | None = None


def load(fresh: bool = False) -> dict:
    global _cache
    if _cache is None or fresh:
        p = path()
        user = json.load(open(p)) if os.path.exists(p) else {}
        _cache = _merge(DEFAULTS, user)
    return _cache


def font(name: str) -> str:
    """A font file from the pack: absolute, relative to the repo, or a name in assets/fonts."""
    for cand in (name, os.path.join(REPO, name), os.path.join(FONTS, name)):
        if cand and os.path.isfile(cand):
            return cand
    return os.path.join(FONTS, "InterDisplay-Bold.ttf")


def asset(rel: str) -> str:
    return rel if os.path.isabs(rel) else os.path.join(REPO, rel)


def rgba(color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    c = color.lstrip("#")
    if len(c) == 8:
        return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), int(c[6:8], 16)
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), alpha


def ass_color(color: str) -> str:
    """#RRGGBB → ASS &H00BBGGRR (opaque)."""
    r, g, b, _ = rgba(color)
    return f"&H00{b:02X}{g:02X}{r:02X}"


def hashtags(tag: str) -> str:
    """The pack's hashtags with {creator} → the streamer's tag."""
    return " ".join(h.replace("{creator}", tag) for h in load()["hashtags"] if h.replace("{creator}", tag).strip())
