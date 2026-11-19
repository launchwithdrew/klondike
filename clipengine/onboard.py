"""Onboarding — from nothing to a channel that's ready to post, with the user's Muse doing the work.

  init     writes the brand pack (brand/brand.json), the roster (config/creators.json), the radar's
           category and a headline-voice file; anything not answered gets a unique default seeded from
           the channel name, so two channels never come out looking alike. Also makes a profile kit
           in brand/kit/: logo, profile picture, YouTube banner, a bio per platform, and a settings
           checklist per platform.
  preview  renders a sample frame and a five-second clip in the pack's look (brand/preview.*).

Every question has a flag, so a Muse can run it in one command without a conversation.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import random
import subprocess

from PIL import Image, ImageDraw, ImageFont

from . import brand as B

REPO = B.REPO
BRAND_DIR = os.path.join(REPO, "brand")

# Curated palettes that read well on video: (card fill, card text, tag fill, tag text, caption highlight)
PALETTES = [
    ("#111827", "#F9FAFB", "#F59E0B", "#111827", "#F59E0B"),
    ("#FFFFFF", "#111111", "#2563EB", "#FFFFFF", "#60A5FA"),
    ("#0F172A", "#E2E8F0", "#22D3EE", "#0F172A", "#22D3EE"),
    ("#FDE047", "#111111", "#111111", "#FDE047", "#FDE047"),
    ("#1E1B4B", "#EDE9FE", "#A78BFA", "#1E1B4B", "#C4B5FD"),
    ("#052E16", "#DCFCE7", "#4ADE80", "#052E16", "#4ADE80"),
    ("#FFF7ED", "#7C2D12", "#EA580C", "#FFFFFF", "#FB923C"),
    ("#18181B", "#FAFAFA", "#F43F5E", "#FFFFFF", "#FB7185"),
    ("#ECFEFF", "#164E63", "#0891B2", "#FFFFFF", "#22D3EE"),
    ("#000000", "#FFFFFF", "#84CC16", "#000000", "#A3E635"),
]
FONTS_TITLE = ["InterDisplay-Bold.ttf", "InterDisplay-ExtraBold.ttf"]
OURS = ("#FFFFFF", "#0A0A0A", "#E51937")      # never hand out the look we use


def seeded_look(name: str) -> dict:
    """A complete, unique-by-default look derived from the channel name."""
    rnd = random.Random(int(hashlib.sha256(name.strip().lower().encode()).hexdigest(), 16))
    pal = [p for p in PALETTES if (p[0], p[1], p[2]) != OURS]
    card_fill, card_text, tag_fill, tag_text, hi = rnd.choice(pal)
    kind = rnd.choice(["stack", "stack", "single"])
    position = rnd.choice(["top", "bottom"] + (["seam"] if kind == "stack" else []))
    logo_corner = {"top": "bottom-right", "seam": rnd.choice(["top-left", "top-right"]),
                   "bottom": rnd.choice(["top-left", "top-right"])}[position]
    return {
        "layout": {"kind": kind, "split": round(rnd.uniform(0.55, 0.66), 3)},
        "card": {"show": True, "position": position, "fill": card_fill, "text": card_text,
                 "font": rnd.choice(FONTS_TITLE), "size": rnd.choice([52, 54, 56, 58]),
                 "radius": rnd.choice([0, 8, 12, 18, 26]), "margin": 60, "max_lines": 3},
        "quote": {"show": True, "place": rnd.choice(["above", "below"]), "fill": tag_fill, "text": tag_text,
                  "font": "InterDisplay-Bold.ttf", "size": 40, "radius": rnd.choice([6, 10, 14])},
        "captions": {"font": "Inter Display", "font_file": "InterDisplay-Bold.ttf", "size": rnd.choice([66, 72, 78]),
                     "color": "#FFFFFF", "outline": "#000000", "outline_width": rnd.choice([4, 5, 6]),
                     "highlight": hi, "words_per_card": rnd.choice([1, 2, 3]),
                     "y": round(rnd.uniform(0.5, 0.64) if position != "bottom" else rnd.uniform(0.4, 0.5), 3),
                     "uppercase": rnd.choice([True, False])},
        "logo": {"file": "brand/kit/logo.png", "corner": logo_corner, "width": rnd.choice([180, 200, 220]),
                 "opacity": 0.75, "margin": 32},
    }


def _font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(B.font("InterDisplay-ExtraBold.ttf" if bold else "InterDisplay-Bold.ttf"), size)


def _initials(name: str) -> str:
    words = [w for w in name.replace("_", " ").split() if w[:1].isalnum()]
    return ("".join(w[0] for w in words[:3]) or name[:2]).upper()


def make_kit(pack: dict, category: str, out_dir: str) -> list[str]:
    """Logo (transparent), profile picture, YouTube banner, bios and a settings checklist."""
    os.makedirs(out_dir, exist_ok=True)
    name, card = pack["name"], pack["card"]
    fill, text, accent = card["fill"], card["text"], pack["quote"]["fill"]
    made = []
    # logo: a ring in the accent color around the initials, transparent outside
    s = 800
    logo = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(logo)
    d.ellipse([20, 20, s - 20, s - 20], fill=B.rgba(fill), outline=B.rgba(accent), width=36)
    f = _font(300)
    ini = _initials(name)
    w = d.textlength(ini, font=f)
    d.text(((s - w) / 2, s / 2 - 190), ini, font=f, fill=B.rgba(text))
    p = os.path.join(out_dir, "logo.png"); logo.save(p); made.append(p)
    prof = Image.new("RGBA", (s, s), B.rgba(fill)); prof.alpha_composite(logo)
    p = os.path.join(out_dir, "profile.png"); prof.convert("RGB").save(p); made.append(p)
    # YouTube banner 2560x1440, text inside the 1546x423 safe area
    ban = Image.new("RGB", (2560, 1440), B.rgba(fill)[:3])
    d = ImageDraw.Draw(ban)
    d.rectangle([0, 1440 // 2 + 140, 2560, 1440 // 2 + 160], fill=B.rgba(accent)[:3])
    f = _font(150)
    w = d.textlength(name, font=f)
    d.text(((2560 - w) / 2, 1440 / 2 - 170), name, font=f, fill=B.rgba(text)[:3])
    f2 = _font(64, bold=False)
    tag = f"The best {category} moments, clipped fast"
    w = d.textlength(tag, font=f2)
    d.text(((2560 - w) / 2, 1440 / 2 + 10), tag, font=f2, fill=B.rgba(text)[:3])
    p = os.path.join(out_dir, "banner.png"); ban.save(p); made.append(p)
    bios = (f"YouTube:\n  {name} — the best {category} moments from the biggest streams, clipped while they're "
            f"still trending. New clips every day. Subscribe so you never miss one.\n\n"
            f"Instagram:\n  The best {category} moments, clipped fast. New clips daily.\n\n"
            f"TikTok:\n  {category} moments you missed. New clips daily.\n")
    p = os.path.join(out_dir, "bios.txt"); open(p, "w").write(bios); made.append(p)
    checklist = f"""# Account checklist for {name}

Do these once, on each account, before the engine posts. Your Muse can walk you through each one.

## All three
- Profile picture: brand/kit/profile.png · Bio: brand/kit/bios.txt
- Use the handles in brand/brand.json exactly (the poster checks them)
- New accounts: browse and like a few {category} videos for a day or two before the first post;
  the engine's warm-up then starts with 1–2 posts a day per platform

## YouTube
- Create the channel, set the handle, upload brand/kit/banner.png
- Settings → Channel → Advanced: audience "No, not made for kids"
- Verify the channel with a phone number (needed for longer titles and custom thumbnails)

## Instagram
- Switch to a Professional account (Creator) — free, gives you Reels insights
- Make the account public

## TikTok
- Switch to a Business or Creator account if offered; keep the account public
"""
    p = os.path.join(out_dir, "checklist.md"); open(p, "w").write(checklist); made.append(p)
    return made


def _suggest_roster(category: str, n_big: int = 3, n_rising: int = 3) -> list[dict]:
    from . import radar as R
    cfg = R.config()
    cfg["game"] = category
    streams = R.live_streams(cfg)
    big = streams[:n_big]
    rising = [s for s in streams[n_big:] if s["viewers"] < 30000][:n_rising]
    return big + rising


def init(name: str | None = None, youtube: str | None = None, instagram: str | None = None,
         tiktok: str | None = None, category: str | None = None, streamers: list[str] | None = None,
         hashtags: list[str] | None = None, force: bool = False, suggest: bool = True) -> dict:
    if os.path.exists(B.path()) and not force:
        raise SystemExit(f"{os.path.relpath(B.path(), REPO)} already exists — this channel is set up. "
                         "Use --force to start over (your current pack is kept as brand/brand.json.bak).")
    if os.path.exists(B.path()):
        os.replace(B.path(), B.path() + ".bak")
    name = (name or "My Clip Channel").strip()
    category = (category or "Just Chatting").strip()
    look = seeded_look(name)
    slugtag = "#" + "".join(ch for ch in name.lower() if ch.isalnum())
    pack = {"_about": "This channel's brand pack. Change anything here (or ask your Muse); run "
                      "`clipengine preview` to see it.",
            "name": name,
            "accounts": {"youtube": {"handle": youtube or "", "channelId": ""},
                         "instagram": {"username": (instagram or "").lstrip("@")},
                         "tiktok": {"username": (tiktok or "").lstrip("@")}},
            "hashtags": hashtags or ["{creator}", "#" + "".join(ch for ch in category.lower() if ch.isalnum()), slugtag],
            **look, "voice": "brand/voice.md", "warmup": True,
            "started": dt.date.today().isoformat()}
    os.makedirs(BRAND_DIR, exist_ok=True)
    kit = make_kit(pack, category, os.path.join(BRAND_DIR, "kit"))
    json.dump(pack, open(B.path(), "w"), indent=2)
    voice = f"""# Headline voice for {name}

- Say what happens on screen in the clip, true to the cut. No clickbait the clip doesn't pay off.
- Who + what happened + the twist, in under ~80 characters; 1–2 words in CAPS; 1–2 emoji at the end.
- The quote tag is the streamer's own words, short enough for one line.
- Never: violence wording (kill, shoot, murder), sexual terms, slurs.
"""
    if not os.path.exists(os.path.join(BRAND_DIR, "voice.md")):
        open(os.path.join(BRAND_DIR, "voice.md"), "w").write(voice)
    # roster + radar category
    roster_path = os.path.join(REPO, "config", "creators.json")
    roster = {}
    picked = [s.strip().lower() for s in (streamers or []) if s.strip()]
    suggestions = []
    if not picked and suggest:
        try:
            suggestions = _suggest_roster(category)
            picked = [s["login"] for s in suggestions]
        except Exception as exc:
            print(f"(couldn't suggest streamers right now: {str(exc)[:80]} — add them later)")
    for i, login in enumerate(picked):
        roster[login] = {"name": next((s["name"] for s in suggestions if s["login"] == login), login),
                         "tag": "#" + login, "vods": None, "priority": 1 if i < 3 else 2}
    json.dump({"_about": "Who this channel clips. vods: null = checked automatically.", "creators": roster},
              open(roster_path, "w"), indent=2)
    radar_path = os.path.join(REPO, "config", "radar.json")
    try:
        rc = json.load(open(radar_path))
    except (OSError, ValueError):
        rc = {}
    rc["game"] = category
    json.dump(rc, open(radar_path, "w"), indent=2)
    B.load(fresh=True)
    print(f"✓ brand pack: {os.path.relpath(B.path(), REPO)} ({look['layout']['kind']} layout, "
          f"card {look['card']['position']}, {look['captions']['words_per_card']} word(s) per caption)")
    print(f"✓ profile kit: brand/kit/ ({', '.join(os.path.basename(k) for k in kit)})")
    print(f"✓ roster: {', '.join(picked) or 'empty — add streamers to config/creators.json'} · category: {category}")
    missing = [p for p, v in (("YouTube handle", youtube), ("Instagram username", instagram), ("TikTok username", tiktok)) if not v]
    if missing:
        print("! still needed: " + ", ".join(missing) + " — re-run init with them, or edit brand/brand.json")
    print("next: clipengine preview · then sign in once: cd engine && node post.mjs --login · then clipengine doctor")
    return pack


def _sample_source(path: str, seconds: float = 6.0) -> str:
    """A neutral moving background to preview the look on (no copyrighted footage needed)."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"gradients=size=1920x1080:rate=30:duration={seconds}:speed=0.02",
                    "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo", "-t", str(seconds),
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "30", "-bf", "0", "-c:a", "aac",
                    "-f", "mpegts", path], check=True)
    return path


def preview(clip: str | None = None, title: str = "Streamer found a SECRET room and lost it 😭🔥",
            quote: str = "no way this is real") -> dict:
    from . import render as V
    os.makedirs(BRAND_DIR, exist_ok=True)
    src = clip or _sample_source(os.path.join(BRAND_DIR, ".preview_src.ts"))
    words = [{"w": f" {w}", "start": 0.8 + i * 0.45, "end": 1.15 + i * 0.45}
             for i, w in enumerate("no way bro look at this right now".split())]
    out = os.path.join(BRAND_DIR, "preview.mp4")
    V.render(src, 0.0, 0.0, 5.0, words, out, layout="stack", title=title, quote=quote)
    png = os.path.join(BRAND_DIR, "preview.png")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "2.2", "-i", out, "-frames:v", "1", png], check=True)
    if not clip:
        try:
            os.remove(src)
        except OSError:
            pass
    print(f"preview: {os.path.relpath(png, REPO)} (frame) · {os.path.relpath(out, REPO)} (5 s clip)")
    return {"png": png, "mp4": out}
