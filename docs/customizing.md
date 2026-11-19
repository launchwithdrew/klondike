# Customizing

Never change code to customize. Everything is in three files:

**The look — `brand/brand.json`** (preview any change with `clipengine preview`):
- `layout.kind`: `facecam` (the streamer's webcam on top, gameplay below — new channels' default),
  `stack` (zoomed action over the full frame) or `single`; `layout.split` (stack: 0.5–0.7 for the top
  panel; facecam: the most the webcam panel may take, up to 0.45). With `facecam`, a streamer whose
  webcam isn't found is cut in `stack` and the report says so. See where the engine thinks a webcam is
  with `clipengine facecam --channel LOGIN` (saves a check image); if it's wrong, set it per streamer in
  `config/creators.json`: `"facecam": [x, y, w, h]` (fractions 0–1 of the frame).
- `card`: `position` (top, seam, bottom), `fill`, `text` (colors as #RRGGBB), `font`, `size`, `radius`
- `quote`: `show`, `place` (above, below), `fill`, `text`
- `captions`: `size`, `color`, `outline`, `highlight` (color of the word being said), `words_per_card`
  (1–3), `y` (0–1, height on screen), `uppercase`
- `logo`: `file`, `corner`, `width`, `opacity` · `hashtags` (`{creator}` = the streamer's tag; it always
  goes first) · `max_hashtags` (default 3) · `caption_style`: `line+question` (default: your one line, then
  the `--question` you pass, if any) or `line`
- `accounts`: the three handles the poster must be signed in as

**Who to clip — `config/creators.json`:** add or remove streamers (Twitch logins). The radar still
suggests hot streamers outside the list in every `cycle`. YouTube streamers: start a sentinel with
`--channel yt:@handle` (audio-only).

**The category — `config/radar.json`:** `game` (e.g. "Grand Theft Auto VI"), `languages`.

**Posting limits — `config/distribution.json`:** each platform's minimum score, daily cap and gap.
The daily `learn` moves the minimums by itself when results say so.

Start over completely: `clipengine init --force ...` (your old pack is kept as `brand/brand.json.bak`).
