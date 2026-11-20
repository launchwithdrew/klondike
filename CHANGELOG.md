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
