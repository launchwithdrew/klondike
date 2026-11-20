# Klondike changelog

Each entry records the shipped engine version, what changed, the public export diff, and the leak scan result.

## v0.4.2

- Review drafts with confidence indicators, score components, skip reasons, and a self-contained contact sheet page. Headline claims now need evidence or a recorded operator override.
- Verify posting links before ledger entry, import another engine's posts for moment-level repeat protection, and approve refined facecam boxes from a check image.
- See a five-sentence morning brief with recording gaps, scout recall, stage timings, overdue analytics, and evidence behind roster changes. Weekly predictions are scored against subsequent reads.
- Added YouTube discovery, improved Kick coverage, adaptive scout capacity, resumable saved-stream renders, and render audits.

Export diff
- Added (13): `clipengine/brief.py`, `clipengine/judge.py`, `clipengine/moments.py`, `clipengine/scouts.py`, `clipengine/verify.py`, `clipengine/vodaccess.py`, `docs/judging.md`, `tests/fixtures/make_speech_fixture.py`, `tests/fixtures/speech_under_game.ogg`, `tests/golden/facecam.png`, `tests/golden/single.png`, `tests/golden/stack.png`, `tools/golden.py`
- Changed (36): `.gitignore`, `AGENTS.md`, `VERSION`, `clipengine/__init__.py`, `clipengine/__main__.py`, `clipengine/attention.py`, `clipengine/audio.py`, `clipengine/autopilot.py`, `clipengine/censor.py`, `clipengine/facecam.py`, `clipengine/grade.py`, `clipengine/handpost.py`, `clipengine/insights.py`, `clipengine/kick.py`, `clipengine/learn.py`, `clipengine/live.py`, `clipengine/ops.py`, `clipengine/pipeline.py`, `clipengine/publish.py`, `clipengine/radar.py`, `clipengine/render.py`, `clipengine/review.py`, `clipengine/safety.py`, `clipengine/sentinel.py`, `clipengine/support.py`, `clipengine/tier1.py`, `clipengine/twitch.py`, `clipengine/vision.py`, `clipengine/youtube.py`, `docs/customizing.md`, `docs/earning.md`, `docs/publishing.md`, `docs/troubleshooting.md`, `engine/post.mjs`, `tests/test_engine.py`, `tools/drop.mjs`
- Removed (0)

Leak scan: clean.

## v0.4.3

- The autopilot can now run as a native service (`clipengine supervisor install`: launchd on macOS, the user's systemd on Linux). It restarts within 30 seconds of any exit that wasn't `autopilot stop`, and a stop now survives reboots. Where no service manager exists, status says the host is degraded.
- There is exactly one capture leader per installation, held by an OS lock that a killed process releases immediately. Service starts, manual starts and sentinel revivals no longer start a second loop. A 30-second heartbeat runs beside the cycle, so a long scan cannot hide a dead loop. A cycle phase with no progress for 40 minutes exits for a clean restart.
- Every outage becomes one incident with its cause (signal, crash, stalled, reboot, host sleep, unknown) and its recovery. The attention line alerts once when it opens and once when it recovers. Three deaths in an hour is reported as a crash loop and restarts back off. `clipengine supervisor status` and `diagnose` show the incidents.
- A recorder that stays alive without producing media for 6 minutes is restarted by itself. A hung cycle can no longer freeze the loop through an inherited output pipe. Notifications that fail to send (while offline) are retried on the next cycle instead of being dropped.
- Heavy work (transcription and encodes) takes turns through one admission gate: one job at a time on the lite profile, two on full. A live moment goes first, then your publish, catch-up, the sweep and pre-renders, then historical scans. Nothing heavy starts when free memory is low, so recording keeps going. `clipengine jobs` shows who runs, who waits and why, plus 24-hour wait and run timings.
- `catchup` without `--since` resumes from the last completed coverage, capped at 12 hours, and refuses to guess when there is none. `posted` records only what the gate approved. An owner's explicit `--override "reason"` is kept in the ledger, still protected against repeats, and left out of learning.
- Optional `"heartbeat_url"` in `config/ops.json`: the autopilot pings it every minute, so an off-host monitor can alert you when a host goes dark.

Export diff
- Added (3): `clipengine/admission.py`, `clipengine/supervise.py`, `docs/reliability.md`
- Changed (15): `AGENTS.md`, `VERSION`, `clipengine/__init__.py`, `clipengine/__main__.py`, `clipengine/attention.py`, `clipengine/audio.py`, `clipengine/autopilot.py`, `clipengine/diagnose.py`, `clipengine/handpost.py`, `clipengine/learn.py`, `clipengine/ops.py`, `clipengine/render.py`, `docs/publishing.md`, `docs/troubleshooting.md`, `tests/test_engine.py`
- Removed (0)

Leak scan: clean.
