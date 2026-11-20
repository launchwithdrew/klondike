# Customizing

Never change code to customize. Everything is in three files:

**The look — `brand/brand.json`** (preview any change with `clipengine preview`):
- `layout.kind`: `facecam` (the streamer's webcam on top, gameplay below — new channels' default),
  `stack` (zoomed action over the full frame) or `single`; `layout.split` (stack: 0.5–0.7 for the top
  panel; facecam: the most the webcam panel may take, up to 0.45). With `facecam`, a streamer whose
  webcam isn't found is cut in `stack` and the report says so. See where the engine thinks a webcam is
  with `clipengine facecam --channel LOGIN` (saves a check image); if it's wrong, set it per streamer in
  `config/creators.json`: `"facecam": [x, y, w, h]` (fractions 0–1 of the frame). A sentinel on a streamer
  with no `"facecam"` setting suggests a box itself after ~3 minutes of recording (one `attention` item a day
  with a check image) — look at it, then add the box (or `"facecam": false`); it never writes config.
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
`--channel yt:@handle` (live chat when YouTube serves it, audio-only otherwise; `clipengine sources` lists the
last 10 YouTube sentinel sessions — chat or audio-only, messages seen, drafts — from `data/sources.jsonl`). The radar also lists live
YouTube streams of the game (`"youtube": true`, `youtube_query`, `youtube_min_viewers` in `config/radar.json`),
from YouTube's live search page, or from the YouTube Data API when `YOUTUBE_API_KEY` is set in the environment
(asked at most every `youtube_every_min`, 20 by default, to stay inside the daily quota).

**The category — `config/radar.json`:** `game` (e.g. "Grand Theft Auto VI"), `languages`.
`"kick": true` also lists the category's live Kick streams (unverified; `clipengine kick-check` tests this
machine and says which route it uses; Kick's official API is used for live status and the listing when
`KICK_CLIENT_ID` / `KICK_CLIENT_SECRET` are set in the environment — it has no playback URL or chat, so recording
and chat still need kick.com's own channel API). Kick sources run on **audio + viewers' clips**: Kick refuses its
chat to outside clients (pusher 4001), so there are no chat spikes on Kick — `kick-check` says `READY (audio mode)`
when everything else works. Catch-up and the sweep draft Kick moments from the saved broadcast, found by the
viewers' clips made while it was live.

**Posting limits — `config/distribution.json`:** each platform's minimum score, daily cap and gap.
The daily `learn` moves the minimums by itself when results say so.

Start over completely: `clipengine init --force ...` (your old pack is kept as `brand/brand.json.bak`).

## A title style of your own
Your agent can design one with you: a Python function in `clipengine/house/` (your private folder — it
stays on your computer and is kept across updates), listed as `CARD_STYLES = {"mystyle": fn}` in
`clipengine/house/__init__.py`, then `"card": {"style": "mystyle"}` in `brand/brand.json`. See
`clipengine/card.py` (`styles()`) for what the function receives and returns.

## Freeze an approved look
Once the look is right, run `clipengine finish approve --name V1`. It records the look settings, every font and
logo by content hash with a copy of each, and a reference frame, all in `brand/finish/`. After that:
- a clip rendered with a different look isn't dropped, and the refusal says what changed;
- `finish restore` puts the approved version back byte for byte;
- `finish check --still` re-renders the reference frame and compares it, which catches an update that changes
  the look.
