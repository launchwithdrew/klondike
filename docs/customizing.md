# Customizing

Never change code to customize. Everything is in three files:

**The look — `brand/brand.json`** (preview any change with `clipengine preview`):
- `layout.kind`: `stack` (zoomed action over the full frame) or `single`; `layout.split` (0.5–0.7)
- `card`: `position` (top, seam, bottom), `fill`, `text` (colors as #RRGGBB), `font`, `size`, `radius`
- `quote`: `show`, `place` (above, below), `fill`, `text`
- `captions`: `size`, `color`, `outline`, `highlight` (color of the word being said), `words_per_card`
  (1–3), `y` (0–1, height on screen), `uppercase`
- `logo`: `file`, `corner`, `width`, `opacity` · `hashtags` (`{creator}` = the streamer's tag)
- `accounts`: the three handles the poster must be signed in as

**Who to clip — `config/creators.json`:** add or remove streamers (Twitch logins). The radar still
suggests hot streamers outside the list in every `cycle`. YouTube streamers: start a sentinel with
`--channel yt:@handle` (audio-only).

**The category — `config/radar.json`:** `game` (e.g. "Grand Theft Auto VI"), `languages`.

**Posting limits — `config/distribution.json`:** each platform's minimum score, daily cap and gap.
The daily `learn` moves the minimums by itself when results say so.

Start over completely: `clipengine init --force ...` (your old pack is kept as `brand/brand.json.bak`).
