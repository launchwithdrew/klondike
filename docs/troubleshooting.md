# Troubleshooting

Start with `clipengine diagnose` (known problems, from recent logs) and `clipengine doctor` (this
machine). Most problems are one of these:

| Symptom | Fix |
|---|---|
| A sentinel shows DOWN | `clipengine cycle` restarts it; if it keeps dying, `diagnose` |
| "not signed in" / "wrong channel" / "logged in as @…" | The user signs in again: `cd engine && node post.mjs --login`, then `--check` |
| Posts stuck in `outbox/` | The poster isn't running: `cycle` starts it; or `cd engine && node post.mjs --once` |
| A clip was HELD | Its reason is in `logs/engine.log`; held clips are in `failed/` |
| Disk nearly full | `cycle` clears old drafts; lower `buffer_h` or `max_sentinels` in `config/ops.json` |
| No moments from a streamer | They may be offline, or their stream isn't saved — sentinels record it live |
| Everything scores low | Normal at first; post the best, run `learn` daily, retrain weekly (`train`) |
| Twitch errors | Usually temporary; the next `cycle` retries |

Still stuck: `clipengine support --problem "one or two sentences"` makes a scrubbed log bundle and an
issue text in `local/` — send those to the engine's maintainers. Then try `docs/fixing-bugs.md`.
