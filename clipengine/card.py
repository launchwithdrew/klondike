"""Title card for the "stack" layout — the look top clip pages post:

    [ red pill:  "the streamer's own line" ]
    [ white rounded box, bold black headline that says what happened 😭👀 ]

Drawn with Pillow as one transparent PNG (rounded corners, color emoji —
libass can't do either) and overlaid on the seam between the two video
panels. Headline words in CAPS are kept as written: emphasis is the
writer's choice ("Streamer got ARRESTED after stealing ...").
"""
from __future__ import annotations

import os
import re

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
FONTS = os.path.join(os.path.dirname(HERE), "assets", "fonts")
TITLE_FONT = os.path.join(FONTS, "InterDisplay-ExtraBold.ttf")
if not os.path.isfile(TITLE_FONT):
    TITLE_FONT = os.path.join(FONTS, "InterDisplay-Bold.ttf")
QUOTE_FONT = os.path.join(FONTS, "InterDisplay-Bold.ttf")
EMOJI_FONT = os.path.join(FONTS, "NotoColorEmoji.ttf")
RED = (229, 25, 55, 255)

_EMOJI = re.compile(
    "([\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF\U0001F900-\U0001F9FF]"
    "[\U0000FE0F\U0001F3FB-\U0001F3FF‍\U0001F300-\U0001FAFF]*)")


def _runs(text: str) -> list[tuple[str, bool]]:
    out, pos = [], 0
    for m in _EMOJI.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], False))
        out.append((m.group(0), True))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], False))
    return out


class _Writer:
    def __init__(self, size: int, font_path: str):
        self.size = size
        self.font = ImageFont.truetype(font_path, size)
        self.efont = ImageFont.truetype(EMOJI_FONT, 109) if os.path.isfile(EMOJI_FONT) else None

    def width(self, text: str) -> int:
        w = 0
        for t, emo in _runs(text):
            if emo and self.efont:
                w += int(self.size * 1.15) * len(t.replace("️", "").replace("‍", "")) // max(1, len(t.replace("️", "").replace("‍", "")))
                w += int(self.size * 1.15) * (len([c for c in t if c not in "️‍"]) - 1) if len(t) > 1 else 0
            else:
                w += int(self.font.getlength(t))
        return w

    def draw(self, img: Image.Image, xy: tuple[int, int], text: str, fill) -> None:
        x, y = xy
        d = ImageDraw.Draw(img)
        asc, desc = self.font.getmetrics()
        for t, emo in _runs(text):
            if emo and self.efont:
                for ch in [c for c in t if c not in "️‍"]:
                    g = Image.new("RGBA", (136, 128), (0, 0, 0, 0))
                    ImageDraw.Draw(g).text((0, 0), ch, font=self.efont, embedded_color=True)
                    bbox = g.getbbox()
                    if bbox:
                        g = g.crop(bbox)
                    s = int(self.size * 1.05)
                    g = g.resize((s, int(s * g.height / max(1, g.width))), Image.LANCZOS)
                    img.alpha_composite(g, (int(x + self.size * 0.05), int(y + asc - g.height * 0.88)))
                    x += int(self.size * 1.15)
            else:
                d.text((x, y), t, font=self.font, fill=fill)
                x += int(self.font.getlength(t))


def _wrap(text: str, w: _Writer, max_w: int) -> list[str]:
    words = []
    for word in text.split():            # keep emoji glued to the word before them
        if words and _EMOJI.fullmatch(word):
            words[-1] += " " + word
        else:
            words.append(word)
    lines, cur = [], ""
    for word in words:
        cand = (cur + " " + word).strip()
        if w.width(cand) <= max_w or not cur:
            cur = cand
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def styles() -> dict:
    """Title styles beyond the built-in card, from the channel's own private folder `clipengine/house/`
    (never part of the engine's releases: a channel's look stays between it and its agent). A style is
    `fn(title, quote, out_png, card, quote_style, width) -> (png, height, mid_y)`, listed in
    `clipengine/house/__init__.py` as CARD_STYLES = {"name": fn}; brand.json picks it with card.style."""
    try:
        from . import house                          # noqa: F401  (absent in a fresh copy)
        return dict(getattr(house, "CARD_STYLES", {}) or {})
    except ImportError:
        return {}


def make_card(title: str, quote: str | None, out_png: str, width: int = 1080,
              title_size: int | None = None, quote_size: int | None = None,
              style: dict | None = None) -> tuple[str, int, int]:
    """Render the headline card in the brand pack's style (brand.json "card" + "quote").
    Returns (path, height, y of the title box centre)."""
    from . import brand as B
    b = B.load()
    card = {**b["card"], **((style or {}).get("card") or {})}
    qs = {**b["quote"], **((style or {}).get("quote") or {})}
    style_fn = styles().get(card.get("style") or "")
    if style_fn:                                    # a title style the channel brought (see styles())
        return style_fn(title, quote, out_png, card, qs, width=width)
    title_size = title_size or card["size"]
    quote_size = quote_size or qs["size"]
    tw = _Writer(title_size, B.font(card["font"]))
    qw = _Writer(quote_size, B.font(qs["font"]))
    pad_x, pad_y, line_gap = 34, 22, int(title_size * 0.18)
    max_text = width - 2 * card["margin"] - 2 * pad_x
    lines = _wrap(title.strip(), tw, max_text)
    if len(lines) > card["max_lines"]:
        raise ValueError(f"headline wraps to {len(lines)} lines — keep it under ~{card['max_lines'] - 1} lines")
    asc, desc = tw.font.getmetrics()
    lh = asc + desc + line_gap
    box_w = max(tw.width(l) for l in lines) + 2 * pad_x
    box_h = len(lines) * lh - line_gap + 2 * pad_y
    q_h, q_w = 0, 0
    qtext = None
    if quote and qs.get("show", True):
        qtext = f"“{quote.strip().strip(chr(34))}”"
        qa, qd = qw.font.getmetrics()
        q_h = qa + qd + 2 * 16
        q_w = qw.width(qtext) + 2 * 26
        if q_w > width - 80:
            raise ValueError("quote too long for one line — shorten it")
    gap = -14 if qtext else 0              # the tag overlaps the box edge a little
    above = qs.get("place", "above") != "below"
    H = q_h + gap + box_h + 8
    img = Image.new("RGBA", (width, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    by = (q_h + gap) if (qtext and above) else 0
    bx = (width - box_w) // 2
    d.rounded_rectangle([bx, by, bx + box_w, by + box_h], radius=card["radius"], fill=B.rgba(card["fill"]))
    y = by + pad_y
    for line in lines:
        lx = (width - tw.width(line)) // 2
        tw.draw(img, (lx, y), line, B.rgba(card["text"]))
        y += lh
    if qtext:
        qx = (width - q_w) // 2
        qy = 0 if above else by + box_h + gap
        d.rounded_rectangle([qx, qy, qx + q_w, qy + q_h], radius=qs["radius"], fill=B.rgba(qs["fill"]))
        qw.draw(img, (qx + 26, qy + 14), qtext, B.rgba(qs["text"]))
    img.save(out_png)
    return out_png, H, by + box_h // 2

