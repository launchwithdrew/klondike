# Publishing

**Three ways a clip starts:**
- A sentinel draft in the queue (`clipengine queue` → `publish-queued`): the normal path.
- A hot moment from the radar line in `cycle` (it prints the exact `scan` command): run it, then
  `clipengine publish --scan runs/<dir> --pick N --title ... --quote ... --line ...`.
- A stream with no saved VOD: sentinels record it; use `publish-queued`, or
  `clipengine publish-live --rec runs/sentinel-<login> --start HH:MM:SS --end HH:MM:SS ...` (UTC).

**Every publish runs twice.** The first run renders and prints the engine score, captions and frames.
If it says `WON'T POST`, even a perfect rubric can't clear any platform's bar — skip it. The second run
adds `--frames-checked --rubric ... --taste yes --post` and drops it.

**Taste (`--taste yes|no`)** is one yes/no, separate from the score: would a fan of this streamer be glad to see this posted? Not cringe, not mean-spirited, not a dead moment, not a spoiler dump. `--taste no` refuses the
post and records a skip with reason `taste` (`data/taste.jsonl`); `learn` pushes down streamers whose clips keep
failing it. `--frames-checked` without `--taste` is refused.

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

**How drafts are judged:** payoff-first (setup 3–15 s + payoff + reaction ≤ 8 s, 15–35 s), only the
streamer's speech scores (game dialogue is tagged by voice + subtitles and can't post on its own; speech
under loud game audio that the voice filter cut is recovered by an unfiltered second pass), drops
keep their reason in `queue --all`. The picture counts too: `pic` (0–1) is how much moves on screen — menus,
maps, shop and settings screens, dark or still frames score low and are penalised, and a moment with nothing
happening (no streamer talk, reaction, chat or viewer clips, like plain driving) is cut again. Publish re-checks the render against the censor list and warns when
the headline's words aren't in the transcript or viewer titles.

**Never re-post:** the ledger refuses a slug or title that is already live on a platform.

See [checking the engine’s calls](judging.md) for headline evidence and skip reasons.

## Verify and review

- Run `clipengine doctor --accounts` in agent mode to check the public profiles configured in your brand pack.
- After posting in your browser, `clipengine posted` checks the live YouTube or TikTok author before recording it. For Instagram, or when YouTube or TikTok won't answer that check, open the post, compare its author, then add `--confirmed` (a different author is still refused). `posted` only records what the gate approved: a clip that went through `publish` / `publish-queued`, on a platform its drop lists. If your user has a clip posted anyway, record it with `--override "reason"` (plus `--login` and `--moment-at` when there was no drop, so the repeat guard knows the moment). The ledger keeps the reason, and `learn` leaves overrides out of its weights.
- `clipengine review --id ID [--title "…" --quote "…"]` writes a portable HTML review for the draft. It includes frames, a payoff marker, the transcript, the score, safety flags, and the headline check.
- `clipengine facecam-setup --channel LOGIN` proposes a refined webcam box and saves a check image. Approve a reviewed box with `--yes x,y,w,h`, or use `--no` for a creator with no webcam overlay.
- `clipengine import-posts --from FILE` accepts live ledger records with creator and moment metadata. It also accepts a JSON object or list with `slug`, `creator`, `moment_at`, and a `links` object keyed by platform.
