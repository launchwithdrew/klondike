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
| Twitch errors / `radar failed: IncompleteRead` | The network cut a response short. The engine retries and falls back to a smaller list; if it keeps happening, the machine's proxy is truncating downloads — run on a normal connection |
| "engine Chrome did not start" | The error now shows what Chrome said. Running as root (Docker, cloud VM) is handled; no screen → it runs headless. "certificate" errors: give Chrome the machine's CA certificates. A proxy that needs a username/password: Chrome can't use one directly — run a small local forwarding proxy and point Chrome at it |
| Signing in on a computer with no screen (an agent's VM) | `post.mjs --login` can't show a window there. Sign in inside a browser you *can* see (e.g. your agent's own), start it with `--remote-debugging-port=9222`, and add `"browser_cdp_url": "http://127.0.0.1:9222"` to `config/ops.json` — the poster, stats and doctor then use that signed-in browser; check with `node post.mjs --check` |
| The signed-in browser is somewhere the engine can't reach (a cloud browser) | `"post_with": "agent"` in `config/ops.json`: the agent uploads each clip with its own browser from `clipengine to-post`, then runs `clipengine posted …` |
| "render came out N s short" | The recording has a hole in that window (the recorder lost the stream for a moment). Choose a moment inside the recording or trim with `--end` — the engine won't post a clip that lost its ending |
| `publish-queued` is quiet for minutes | Normal: transcribing takes a few minutes on a CPU; progress lines show each step |
| Sentinels still act like the old version | `clipengine update` restarts them on the new code; after fixing code yourself: `stop --all`, then `cycle` |

Still stuck: `clipengine support --problem "one or two sentences"` makes a scrubbed log bundle and an
issue text in `local/` — send those to the engine's maintainers. Then try `docs/fixing-bugs.md`.
