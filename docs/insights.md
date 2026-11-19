# Sharing anonymous results (opt-in)

Off by default. When the user agrees, the engine shares anonymous numbers that help retrain the
shared detector, so everyone's engine — including this one — catches moments better each release.

**What is shared:** which detector signals fired before moments that did well, banger scores and the
views they got as ratios to this channel's own median, which features and settings are used, and which
known problems `diagnose` matched. **Never:** handles, channel name, streamer names, clips, transcripts,
chat, passwords, or anything that identifies the user.

**Turn it on (with the user's yes):** `clipengine insights --enable`. See exactly what would be sent
first: `clipengine insights --show`. Turn it off any time: `clipengine insights --disable`.

**Right now nothing leaves this machine.** Sharing opens in a later release, through a channel where no
one can read or change anyone else's results. Until then each day's numbers are kept in
`local/insights/outbox/`, and turning it off stops that too.
