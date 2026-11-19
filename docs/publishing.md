# Publishing

**Three ways a clip starts:**
- A sentinel draft in the queue (`clipengine queue` → `publish-queued`): the normal path.
- A hot moment from the radar line in `cycle` (it prints the exact `scan` command): run it, then
  `clipengine publish --scan runs/<dir> --pick N --title ... --quote ... --line ...`.
- A stream with no saved VOD: sentinels record it; use `publish-queued`, or
  `clipengine publish-live --rec runs/sentinel-<login> --start HH:MM:SS --end HH:MM:SS ...` (UTC).

**Every publish runs twice.** The first run renders and prints the engine score, captions and frames.
If it says `WON'T POST`, even a perfect rubric can't clear any platform's bar — skip it. The second run
adds `--frames-checked --rubric ... --post` and drops it.

**Headline (`--title`):** what happens on screen in this cut, true, under ~80 characters, 1–2 words in
CAPS, 1–2 emoji at the end. Never violence words (kill, shoot, murder) or sexual terms — publish refuses
them. **Quote (`--quote`):** the streamer's exact words, one short line. **Line (`--line`):** one plain,
true sentence; hashtags are added from the brand pack.

**Fixing captions before posting:**
- `--fix "heard=>said"` corrects a misheard word (repeatable).
- `--bleep word` censors one more word (repeatable).
- `--start N` / `--end N` (queue drafts: seconds off the start/end) or VOD seconds (`publish`) to trim.

**Where it goes:** the banger score (60% engine, 40% your rubric) meets each platform's bar in
`config/distribution.json` (TikTok/Instagram 75, YouTube 60 to start). Daily caps and minimum gaps
apply; a clip that misses a gap is scheduled and the poster posts it when the slot opens. New accounts
warm up: 1 post a day per platform at first, full speed by day 10.

**Never re-post:** the ledger refuses a slug or title that is already live on a platform.
