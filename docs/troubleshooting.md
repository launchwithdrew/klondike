# Troubleshooting

Start with `clipengine diagnose` (known problems, from recent logs) and `clipengine doctor` (this
machine). Most problems are one of these:

| Symptom | Fix |
|---|---|
| Autopilot stops, nothing ran overnight, machine overloaded | `docs/reliability.md` |
| A sentinel shows DOWN | `clipengine cycle` restarts it; if it keeps dying, `diagnose` |
| "not signed in" / "wrong channel" / "logged in as @…" | The user signs in again: `cd engine && node post.mjs --login`, then `--check` |
| Posts stuck in `outbox/` | The poster isn't running: `cycle` starts it; or `cd engine && node post.mjs --once` |
| A clip was HELD | Its reason is in `logs/engine.log`; held clips are in `failed/` |
| Disk/quota full | `cycle` clears the oldest recording; hidden quota: `disk_quota_gb` in `config/ops.json` |
| Recording holes during ad breaks | Twitch shows ads to logged-out viewers. Optional: the user sets `TWITCH_AUTH_TOKEN` in the machine's environment (their own Twitch login token; never in a file or chat). With Turbo or a sub to that channel the recording is ad-free, and sub-only saved streams open. `doctor` says which mode is on |
| No moments from a streamer | They may be offline, or their stream isn't saved — sentinels record it live |
| Everything scores low | Normal at first; post the best, run `learn` daily, retrain weekly (`train`) |
| Twitch errors / `radar failed: IncompleteRead` | The network cut a response short. The engine retries and falls back to a smaller list; if it keeps happening, the machine's proxy is truncating downloads — run on a normal connection |
| `kick: unreachable (403)` in the radar | kick.com (behind Cloudflare) or this network refused the request. The radar carries on without Kick and tries again in 30 min. `clipengine kick-check` says which step fails |
| `youtube: bot check` in the radar | YouTube showed this machine its bot check. The radar carries on without YouTube rows and tries again in 30 min. Setting `YOUTUBE_API_KEY` (YouTube Data API) avoids the page entirely |
| `youtube: Data API 403 (quota or key refused)` | The API key is wrong, lacks the YouTube Data API, or the day's quota is used up. Raise `youtube_every_min` in `config/radar.json` |
| kick-check: `READY (audio mode)` / `chat ✓ refused by Kick (pusher 4001)` | Normal: Kick refuses its chat to outside clients. Kick sentinels judge on audio and on viewers' clips; nothing to fix |
| catch-up: `broadcast …: video unreadable here` | Kick's saved-broadcast video (stream.kick.com) is blocked from this machine. The moment isn't drafted; run Kick sources from a machine that reaches stream.kick.com |
| kick-check: `record ✗ … official API gives no playback URL` | Live status came from Kick's official API, but kick.com's channel API (the only source of the playback URL) refused this machine. Run Kick sentinels from a machine kick.com allows |
| "engine Chrome did not start" | The error shows what Chrome said. Root (Docker, cloud VM) and no screen (headless) are handled. "certificate" errors: give Chrome the machine's CA certificates. A proxy with a username/password: run a local forwarding proxy and point Chrome at it |
| Signing in on a computer with no screen (an agent's VM) | `post.mjs --login` can't show a window there. Sign in inside a browser you *can* see (e.g. your agent's own), start it with `--remote-debugging-port=9222`, and add `"browser_cdp_url": "http://127.0.0.1:9222"` to `config/ops.json` — the poster, stats and doctor then use that signed-in browser; check with `node post.mjs --check` |
| The signed-in browser is somewhere the engine can't reach (a cloud browser) | `"post_with": "agent"` in `config/ops.json`: the agent uploads each clip with its own browser from `clipengine to-post`, then runs `clipengine posted …` |
| "render came out N s short" | The recording has a hole in that window (the recorder lost the stream for a moment). Choose a moment inside the recording or trim with `--end` — the engine won't post a clip that lost its ending |
| `publish-queued` is quiet for minutes | Normal: transcribing takes a few minutes on a CPU; progress lines show each step |
| Sentinels still act like the old version | `clipengine update` restarts them on the new code; after fixing code yourself: `stop --all`, then `cycle` |

Still stuck: `clipengine support --problem "one or two sentences"` makes a scrubbed log bundle and an
issue text in `local/` — send those to the engine's maintainers. Then try `docs/fixing-bugs.md`.
