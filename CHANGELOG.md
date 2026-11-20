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

## v0.4.4

- Automatic triage. `cycle` routes every new draft before anything renders, so you never have to choose which draft to render.
  - Fresh, plausible drafts are pre-rendered and show when they expire.
  - Quiet visual moments and cuts that couldn't be repaired go to review, with what's missing.
  - A clip cut mid-sentence is re-cut once on speech boundaries, keeping the payoff.
  - Drafts whose moment is over 6 hours old are archived with the reason, and so are drafts with chat laughter but no reaction, no viewer clips and a dull picture. Freshness is measured from the moment itself, so re-queuing an old draft doesn't make it fresh.
  - `clipengine triage --keep ID --why "…"` keeps an old moment on purpose.
- Saved-stream windows are checked before transcription. Missing audio, a fully muted window, or a copyright-muted payoff is refused with a rights-review reason instead of spending a transcription. A constant, flat audio bed is flagged. The compute saved is counted.

Export diff
- Added (1): `clipengine/triage.py`
- Changed (8): `VERSION`, `clipengine/__init__.py`, `clipengine/__main__.py`, `clipengine/audio.py`, `clipengine/ops.py`, `clipengine/sentinel.py`, `docs/judging.md`, `tests/test_engine.py`
- Removed (0)

Leak scan: clean.

## v0.4.5

- The audio check before transcription now runs on every route, not only saved streams.
  - Covered: live-recording drafts and their re-cut, pre-renders without a saved transcript, `publish-live`, and viewer-clip renders from a recording.
  - A window with no audio, full digital silence, or silence over the payoff is refused with one reason line. No transcription is spent on it.
- Probe cache. A saved-stream window found muted is remembered for 14 days, keyed by stream and window.
  - Later sweeps and catch-up retries skip the download, the decode and the transcription.
  - A failed download is never cached, so it is tried again.
- `clipengine jobs` shows what the early check saved in the last 24 hours: windows stopped, seconds of audio, how many came from the cache, and the split by route.
- Fix: a fully muted window now gets the copyright-muted reason. It used to be reported as "only 0 words — nothing to caption".

Export diff
- Added (0)
- Changed (10): `VERSION`, `clipengine/__init__.py`, `clipengine/admission.py`, `clipengine/audio.py`, `clipengine/live.py`, `clipengine/pipeline.py`, `clipengine/prerender.py`, `clipengine/sentinel.py`, `docs/judging.md`, `tests/test_engine.py`
- Removed (0)

Leak scan: clean.
