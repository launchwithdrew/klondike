# Judge a draft

**When:** `next` says "judge draft …", or `queue` lists one. Judge it now. Waiting only makes it staler.

1. `clipengine queue` (or `queue --json`) lists the drafts, crowd-backed first. 🔥URGENT means viewers are clipping
   it, or the streamer clearly reacted. "maybe" means chat and loudness only: judge it on the frames.
2. **Look at the frames:** open the draft's contact sheet (the `sheet` path). The checks can't see nudity, strip
   clubs or a misheard word. If you see any of those, skip it.
3. **Not worth it:** `clipengine skip --id ID --reason "dud"` (or menu / map / cutscene / driving / sexual / your
   own words).
4. **Worth it:** write a headline of 2–5 words (≤ 32 characters) about what happens **on screen**, with the punch
   word in CAPS. Add the streamer's own words as the quote and one true sentence as the caption:
   `clipengine publish-queued --id ID --title "…" --quote "…" --line "…"`. This renders the clip and prints the
   score and captions. `WON'T POST` means move on.
5. Look at the render's frames and read its captions, then answer honestly:
   - the rubric (`hook`: something happens in the first 2 s; `context`: makes sense to a stranger; `payoff`: the
     headline is visible; `share`: a fan would send it);
   - taste: would a fan be glad to see it?

   Re-run with `--frames-checked --rubric hook,payoff --taste yes --post`.

The engine refuses repeats, checks safety and decides which platforms and when. An honest "no" teaches it. An
inflated "yes" posts weak clips and teaches it the wrong thing.
