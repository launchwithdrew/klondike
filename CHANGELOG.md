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

## v0.4.6

- The supervisor now comes back after a reboot on hosts where the user systemd doesn't start by itself (no linger, no system bus) and in containers with no service manager.
  - The engine's keeper (`supervisor keep`) restarts the autopilot within 30 seconds of any exit that wasn't a stop, the same as launchd or systemd would.
  - `supervisor install` adds a `@reboot` line to your crontab where linger isn't available. The line is added once, and your other lines are kept.
  - With no cron either, the next engine command (`attention`, `doctor`, `supervisor status`) brings the supervisor back, with one attention line. A stop is still honoured across reboots.
  - `supervisor status` and `doctor` show an "after a reboot:" line saying which of these applies on your host.
- Installs from before this version are read back from their service file, so nothing needs reinstalling. Run `supervisor install` again only to add the boot line.
- New commands: `supervisor boot` (idempotent) and `supervisor keep`. `uninstall` also removes the keeper and the boot line.
- Containers are no longer DEGRADED: the keeper runs the autopilot there.

Export diff
- Added (0)
- Changed (9): `AGENTS.md`, `VERSION`, `clipengine/__init__.py`, `clipengine/__main__.py`, `clipengine/autopilot.py`, `clipengine/ops.py`, `clipengine/supervise.py`, `docs/reliability.md`, `tests/test_engine.py`
- Removed (0)

Leak scan: clean.

## v0.4.7

- Fix: `posted` can record a TikTok or YouTube post when the platform won't confirm its author from your host. TikTok often blocks automated reads.
  - Open the post, check that it plays as your account, and re-run with `--confirmed`. The ledger records it as `operator-confirmed`, with the reason.
  - A post that the platform says belongs to a different account is still refused. So is a TikTok link to another account's post.
  - Before this fix, such a post could not be recorded at all, so it was missing from the ledger and from view tracking.

Export diff
- Added (0)
- Changed (6): `VERSION`, `clipengine/__init__.py`, `clipengine/__main__.py`, `clipengine/verify.py`, `docs/publishing.md`, `tests/test_engine.py`
- Removed (0)

Leak scan: clean.

## v0.4.8

- Approved finish. `clipengine finish approve --name V3` freezes your channel's look as a numbered version, kept in `brand/finish/`. It records:
  - the look settings (layout, card, quote, captions, logo);
  - every font and logo file, by content hash, with a copy of each;
  - a reference frame rendered through the real pipeline.
- Every render now writes a receipt beside the clip (`<clip>.finish.json`), and the drop's `post.json` carries it. Each post records which approved version it has.
- With an approved finish, a clip rendered with a different look is not dropped. The refusal names the change, for example "logo.file bytes changed" or "card.fill '#111111'→'#00FF00'".
  - `finish restore [--version N]` puts the approved settings and files back byte for byte.
  - `finish approve` makes the new look the approved one.
- `finish check --still` re-renders the reference frame and compares pixels. This catches an engine update that changes how your look is drawn even when no setting changed. `doctor` and `cycle` run it once per engine version and say so.
- Pre-renders are invalidated when the bytes of a font or the logo change, not only when settings change.
- Without an approved finish nothing is enforced. Receipts are still written.

Export diff
- Added (1): `clipengine/finish.py`
- Changed (10): `VERSION`, `clipengine/__init__.py`, `clipengine/__main__.py`, `clipengine/ops.py`, `clipengine/prerender.py`, `clipengine/publish.py`, `clipengine/render.py`, `docs/customizing.md`, `tests/test_engine.py`, `tools/drop.mjs`
- Removed (0)

Leak scan: clean.

## v0.4.9

- The bank. `clipengine bank` lists every finished clip on each platform with one state:
  - waiting: scheduled, reserve inventory;
  - ready;
  - claimed;
  - uncertain;
  - live.
- Claim before uploading. `clipengine claim --slug S --platform P` takes the platform under a 30-minute lease (`claim_lease_min` in `config/ops.json`) and records the attempt before the clip goes to the browser.
  - Claims are atomic: two posters can't take the same platform.
  - The claim prints the file and the exact text to use.
- `posted` closes the attempt. Close an attempt that didn't post with `attempt --failed "why"`, or `attempt --release` if it never reached the browser. The platform is then ready again, and the failure count and reason are kept.
- Reconcile before retry. When a lease runs out with no outcome, the platform becomes uncertain. Nobody can claim it again until someone checks the account and records `posted` (if it is live) or `attempt --failed`. It shows as overdue on the attention line.
  - This prevents a double post after a session dies mid-upload.
- `to-post` shows each due platform's state and the command for its next step.
- Every step is one line in `data/attempts.jsonl`, committed like the ledger.

Export diff
- Added (1): `clipengine/bank.py`
- Changed (9): `AGENTS.md`, `VERSION`, `clipengine/__init__.py`, `clipengine/__main__.py`, `clipengine/handpost.py`, `clipengine/ops.py`, `clipengine/verify.py`, `docs/reliability.md`, `tests/test_engine.py`
- Removed (0)

Leak scan: clean.

## v0.4.10

- Sizing by real limits. In a container, the engine now reads the CPU quota and memory limit it actually runs under (cgroup v1 and v2, plus CPU affinity) instead of the host's numbers. That corrects:
  - the adaptive scout cap, which now uses this container's share of its own CPUs rather than the host's load;
  - the heavy-job memory floor, which now uses the container's headroom so it no longer lets in a job the container's out-of-memory killer will end;
  - Whisper's thread count, which now matches the real CPUs (a 1.5-CPU container on a 16-core host gets 2 threads, not 16);
  - the profile suggestion.
- `doctor` prints the effective CPUs and memory limit, with where each comes from. It also measures the x264 encode speed at your preset and lists the hardware encoders that actually work here (each one is tried). The result is cached for a day.
- `clipengine bench` times the real pipeline in about a minute at the lowest priority, so capture still goes first. It measures encode, the full render of your look, and Whisper on recorded speech, then predicts the two speed targets: moment to draft under 3 min, and draft to render under 2 min.
  - `--profile lite|full` measures the other profile without changing the config.
  - `bench --compare A.json B.json` puts two hosts side by side.

Export diff
- Added (2): `clipengine/bench.py`, `clipengine/host.py`
- Changed (8): `VERSION`, `clipengine/__init__.py`, `clipengine/__main__.py`, `clipengine/admission.py`, `clipengine/audio.py`, `clipengine/ops.py`, `docs/reliability.md`, `tests/test_engine.py`
- Removed (0)

Leak scan: clean.

## v0.4.11

- A render killed mid-way recovers by itself and never leaves a half-made clip that looks ready.
  - Burning the card onto a pre-rendered clip now writes to a temporary file and renames it only when finished, like the main render already did. A kill mid-encode leaves no finished-looking clip.
  - Temporary encode files are per process, so an encoder orphaned by a killed engine can't overwrite a retry's output. A retry clears the leftovers of dead runs.
  - A pre-render whose owner was killed is taken over at once, instead of being blocked for 15 minutes by the lock it left behind.
  - `publish-live` saves the transcript as soon as it has one. A run killed after transcribing resumes without transcribing again, as long as the window is the same.
- Drills: a real pre-render process is killed mid-encode (SIGKILL, encoder orphaned). Nothing looks ready, the retry takes over immediately, and the finished clip has the right length.

Export diff
- Added (0)
- Changed (6): `VERSION`, `clipengine/__init__.py`, `clipengine/live.py`, `clipengine/prerender.py`, `clipengine/render.py`, `tests/test_engine.py`
- Removed (0)

Leak scan: clean.

## v0.4.12

- `train` is stricter and catches more moments.
  - Held-out scores now hold out a whole streamer at a time, so the number you see is for streamers the model has never seen.
  - It trains on each streamer's 2 newest broadcasts of the game (`--per N`), using the 4 hours with the most viewer clips, not the last 4 hours.
  - Viewer-clip labels read the neighbouring clip periods too, because Twitch lists only a channel's top 100 clips per period. That finds 2–5× more labelled moments on a busy channel.
  - Time near a clip with too few views to count is left out of training. It is no longer treated as "nothing happened".
- The trigger re-arms. A trained model now carries its own release and cooldown, and `train` tunes them together with the trigger. The old trigger stayed busy through the loud chat after a moment, then waited 60 s, so the next moment in a busy stretch never fired. On 61 held-out streams, re-arming caught about a fifth more top moments at the same false-fire rate. The live sentinel and the chat-only watcher both use it.
- `train` writes a new model only if it does at least as well as the one in use on the same streams. It prints both. `--force` writes it anyway.
- Fitting standardises features first, so small-scale signals aren't under-fitted.

Export diff
- Added (0)
- Changed (8): `VERSION`, `clipengine/__init__.py`, `clipengine/__main__.py`, `clipengine/sentinel.py`, `clipengine/tier1.py`, `clipengine/train.py`, `clipengine/twitch.py`, `tests/test_engine.py`
- Removed (0)

Leak scan: clean.

## v0.4.13

- Catch-up batches are checked before they start, and the coverage mark moves only over minutes that were actually covered.
  - `clipengine catchup` checks the whole batch first. An unknown channel, or one whose clip policy is "never", stops it before anything is scanned or queued (exit 2). It then prints the plan: each creator's windows, or why it has none.
  - Each window ends as queued, nothing strong, or source failed. The summary keeps "found nothing" apart from "couldn't look".
  - A source that failed holds the coverage mark before its window, so the next run retries it and those minutes are never silently skipped (exit 3). The 30-minute sweep starts from completed coverage too, not from its last launch.
  - Running the same batch twice queues nothing new. `jobs` shows the last batches and anything they couldn't cover.
- Moving to another machine: `clipengine migrate` (`docs/moving.md`).
  - `snapshot` saves your brand and approved look, config, data, the ledger and finished clips, with checksums. Credentials and the browser profile are never included.
  - `verify` restores into a separate folder and checks every checksum, plus the post ledger, repeat guard, attempts and approved look.
  - `shadow` makes the new machine capture without posting.
  - `release` on the old machine and `takeover` on the new one hand over posting and the autopilot exactly once, and catch-up resumes from the old machine's coverage. Rollback is the same two steps the other way.
- `restore` unpacks into a staging folder first. An interrupted restore is reported by `doctor`, and running the same restore again finishes it.

Export diff
- Added (2): `clipengine/migrate.py`, `docs/moving.md`
- Changed (11): `AGENTS.md`, `VERSION`, `clipengine/__init__.py`, `clipengine/__main__.py`, `clipengine/admission.py`, `clipengine/autopilot.py`, `clipengine/bank.py`, `clipengine/ops.py`, `clipengine/publish.py`, `docs/reliability.md`, `tests/test_engine.py`
- Removed (0)

Leak scan: clean.

## v0.4.14

- `clipengine metrics`: the weekly numbers that show what changed. Each number comes with its time window, host profile and sample count, or the reason it isn't available (never a silent zero).
  - Coverage: recorded vs live minutes, minutes covered by catch-up, unknown minutes, uptime, longest outage, holes over 10 s (explained and ad breaks), and whether an off-host heartbeat monitor is set.
  - Overnight (local 00–08): live minutes, live vs saved-stream drafts, approved winners, muted payoffs per probed window by route, or "no stream".
  - Cost per 100 drafts: transcriptions and their audio minutes, final encodes, heavy-job time, early and late rejects. The early filter stays advisory until held-out evidence meets the rollout gate.
  - Speed: p50/p90 for moment→draft, draft→review, draft→pre-render and moment→live, reserve stock (hours under two ready clips), daily yield, claim→live.
  - Publishing: approved moments per day, verified links per platform, missed slots by reason, 1 h/24 h/72 h reads, missing reads kept apart from zero views, overrides left out.
  - Recommendations: posting hours from measured performance (timezone, observations, date range), else the configured prime hours, plus the score of last week's predictions.
- `--save` keeps a week. `--compare A B` puts two saved weeks side by side, with absolute numbers and ratios.
- New records behind it: the audio minutes of each transcription and the video seconds of each encode (`runs/heavy/work.jsonl`), and one reserve-stock sample per cycle (`runs/metrics/stock.jsonl`).

Export diff
- Added (1): `clipengine/metrics.py`
- Changed (10): `AGENTS.md`, `VERSION`, `clipengine/__init__.py`, `clipengine/__main__.py`, `clipengine/admission.py`, `clipengine/audio.py`, `clipengine/ops.py`, `clipengine/render.py`, `docs/earning.md`, `tests/test_engine.py`
- Removed (0)

Leak scan: clean.

## v0.4.15

- The engine's own Chrome poster never posts from a copy that can't update. If `git pull` fails on the machine running it (lost git access, an expired key), it stops posting and says why once an hour. It used to keep trying to post from its out-of-date outbox and post ledger, which risked re-uploading clips already posted elsewhere.
- A poster error that repeats now alerts once an hour instead of every minute.
- `metrics` coverage counts minutes scanned from saved streams by the 30-minute sweep (chat and viewer clips), not only recorded minutes. It shows recorded, scanned, total covered and truly unknown minutes. moment→draft is also split into live drafts and catch-up drafts, so slow saved-stream drafts don't hide the live speed.
- A timing-sensitive keeper test now waits long enough on a busy machine.

Export diff
- Added (0)
- Changed (6): `VERSION`, `clipengine/__init__.py`, `clipengine/metrics.py`, `engine/lib.mjs`, `engine/post.mjs`, `tests/test_engine.py`
- Removed (0)

Leak scan: clean.

## v0.4.16

- A draft with a high score built only on chat and loudness is no longer marked 🔥URGENT. Both signals max out on any busy stretch: a walk through the woods and a car tipping over scored 90 and 88 with nothing else behind them, and an editor skipped both on the frames.
  - URGENT now means viewers are clipping it, or a high score where the streamer's own reaction backs it up.
  - Other high scorers stay in the queue, marked "unconfirmed: chat + loudness only", for a look at the frames. The score itself is unchanged, so a real winner still posts once its frames are checked.

Export diff
- Added (0)
- Changed (5): `AGENTS.md`, `VERSION`, `clipengine/__init__.py`, `clipengine/sentinel.py`, `tests/test_engine.py`
- Removed (0)

Leak scan: clean.

## v0.4.17

- Confidence markers look the same everywhere. A draft's markers appear identically in `queue`, on its review page and in the drop's grade record:
  - frames unassessed;
  - thin speech;
  - speaker tags uncertain;
  - unconfirmed: chat + loudness only.

  A clean draft says "every check ran", so a qualified 75 never reads like a clean one.
- One verdict per viewed artifact. A verdict records which artifact it was given on: the rendered clip, or the contact sheet the review page shows (with its id). Repeating the same verdict on the same artifact records it once. A changed mind or a new render counts as a new decision.
- Series memory: `clipengine series`.
  - It suggests a creator's recurring bit when two or more of your live winners share a headline punch word (their first CAPS word), with receipts: the slugs, dates and live links. Owner overrides never count.
  - `series --confirm LOGIN:WORD --name "…"` confirms it. After that, a new draft from that creator whose own evidence carries the word is labelled `series: …` in `queue`. Other creators and other bits aren't.
- The daily brief on a day with no posts says where the pipeline stopped, with the counts: streams watched → drafts → over a bar → judged → dropped → posted.
- The category-shapes line names its source (last 24 h, the number of viewer clips and creators). With fewer than 5 clips or 2 creators it says "sparse" instead of claiming a pattern. It reports aggregated words only, never a copied title.
- A triage reason in `queue` is no longer cut off at 50 characters: a failed re-cut now shows why it failed. A test traces one refusal per path (the gate bar, a muted payoff, safety, stale, no payoff, a failed repair, never-clip policy) back to its reason after a restart.

Export diff
- Added (1): `clipengine/series.py`
- Changed (11): `VERSION`, `clipengine/__init__.py`, `clipengine/__main__.py`, `clipengine/judge.py`, `clipengine/publish.py`, `clipengine/review.py`, `clipengine/scouts.py`, `clipengine/sentinel.py`, `clipengine/support.py`, `docs/judging.md`, `tests/test_engine.py`
- Removed (0)

Leak scan: clean.

## v0.4.18

- Running the test suite no longer writes into your real judging records. Some tests used to append fixture verdicts to `data/judged.jsonl` and `data/censor_caught.jsonl` on every run, including the run `update` does before installing a release, and `eval-judge` then counted them. Every test now gets its own temporary judging folder.
  - To clean an install that already ran the suite, remove the fixture rows ("StreamerA@1000", "streamera-1"):

    ```
    python - <<'PY'
    import json, os
    p = "data/judged.jsonl"
    if os.path.exists(p):
        rows = [l for l in open(p) if l.strip() and json.loads(l).get("id") not in ("StreamerA@1000", "streamera-1")]
        open(p, "w").writelines(rows)
    PY
    ```

Export diff
- Added (1): `tests/conftest.py`
- Changed (2): `VERSION`, `clipengine/__init__.py`
- Removed (0)

Leak scan: clean.

## v0.4.19

- Possible death screens are flagged. The engine can't see the screen, so it reads the words around a moment: "he died", a viewer clip titled "wasted", "F in chat", RIPs, blood, a corpse.
  - Chat's "dead" and 💀, which mean laughing, never count.
  - A flagged draft is never URGENT and shows `possible death screen — check the frames`.
  - The score is unchanged unless your channel opts in: `"penalties": {"death": 0.5}` in `config/grading.json` halves it.

Export diff
- Added (0)
- Changed (8): `VERSION`, `clipengine/__init__.py`, `clipengine/grade.py`, `clipengine/pipeline.py`, `clipengine/safety.py`, `clipengine/sentinel.py`, `rules/safety.md`, `tests/test_engine.py`
- Removed (0)

Leak scan: clean.

## v0.4.20

- The engine's own ship / hold / drop call (the one `eval-judge` compares with yours) is recalibrated. It used to assume a perfect editor rubric, so it held nearly everything and never dropped.
  - It now drops a draft under engine 50, ships from 70 and holds in between. Both thresholds are in `config/grading.json` under `"judge_call"`.
  - It drops a possible death screen when your channel penalises them.
- `eval-judge` no longer lets a single agreement number hide what the calls do. It also prints:
  - a confusion table of engine call against editor call;
  - how many ship calls were right;
  - how many of your keepers the engine shipped;
  - the always-the-commonest-call baseline;
  - today's rule replayed on the same drafts.
- Verdicts now keep the draft's flags, so later replays see them.

Export diff
- Added (0)
- Changed (5): `VERSION`, `clipengine/__init__.py`, `clipengine/judge.py`, `docs/judging.md`, `tests/test_engine.py`
- Removed (0)

Leak scan: clean.

## v0.4.21

- Recording pruning keeps what a queued draft needs for a re-cut. Fixes in this area:
  - Each queued draft's window was already protected, plus 10 s either side. Its transcribed handles, the audio a re-cut on speech boundaries can extend into, now stay too.
  - Pruning no longer assumes every segment is 2 s long. A longer segment (YouTube, Kick) that started just before a protected window, and held its first seconds, used to be deleted.
  - A 4-hour drill shows a moment queued 3 hours earlier re-cuts as a straight copy of the original segments.
- Weekly `metrics` reports the oldest recording still re-cuttable.
- A two-install drill shows a second engine importing the first one's `ledger.jsonl` (with its `data/clipped.jsonl` beside it) and refusing the same moment, with both receipts on record.

Export diff
- Added (0)
- Changed (7): `VERSION`, `clipengine/__init__.py`, `clipengine/autopilot.py`, `clipengine/live.py`, `clipengine/metrics.py`, `clipengine/ops.py`, `tests/test_engine.py`
- Removed (0)

Leak scan: clean.

## v0.4.22

- Weekly `metrics` no longer overstates outages. An incident whose recovery was never recorded used to count as dark from its start until the report ran, so one orphaned record read as a 21.7-hour outage. An incident now ends at the first evidence the leader was back: its recorded recovery, its own recorded downtime, or the next incident's start. One with no such evidence is listed as "no recovery recorded" and not counted. Only the newest incident, if it's still open, counts as dark until now.
- When a new leader takes over, it closes any other incident still marked open as "closed-unknown". It's kept in the record, but no longer shown as an ongoing outage.

Export diff
- Added (0)
- Changed (5): `VERSION`, `clipengine/__init__.py`, `clipengine/metrics.py`, `clipengine/supervise.py`, `tests/test_engine.py`
- Removed (0)

Leak scan: clean.

## v0.5.0

**v0.5: finding where winners are lost, and stopping the biggest leak.**

- **`clipengine funnel [--days 3]` shows where moments are lost.** It reports every route (viewer clips, live spike, streamer reaction, tier-1 chat spike, sweep, catch-up) as drafts → dropped by the engine → archived by triage → waiting → skipped by the editor → published, with the top reason at each loss. It also shows the moments viewers clipped (25+ views) that we caught or missed (and why), and recording holes by cause.
- **A moment our recording missed is drafted from the saved stream instead of being dropped.**
  - A moment whose payoff wasn't in our recording used to be dropped as "recording gap (ad break)", whatever the real reason: a long hole while nothing was recording, before the recording started, or segments pruned. One install lost every viewer-clipped moment this way (90 of 90).
  - Such a moment from a creator with saved streams now goes to the saved-stream route. It waits a few minutes when the VOD's copy hasn't caught up yet.
  - The drop line says the real reason and that the hand-off happened. A dropped line no longer blocks the re-draft.
- **Coverage marks per creator.** One creator whose saved streams keep failing no longer freezes the catch-up / sweep coverage mark for everyone. The shared mark moves, that creator keeps its own mark and is re-scanned from it (up to 12 h back), and weekly metrics read each creator's own mark.
- **Crowd-first queue.** Drafts backed by viewer clips or the streamer's own reaction come first. Drafts on chat and loudness alone are listed last under "maybe".
- **Headline options.** `clipengine headlines --id ID` and the review page offer up to three headlines condensed from viewers' clip titles. Each one is checked first (2–5 words, ≤ 32 characters, a CAPS punch word, no claim or number the evidence lacks, no flagged wording). Fewer than three comes with the reason. You pick.

Export diff
- Added (0)
- Changed (10): `VERSION`, `clipengine/__init__.py`, `clipengine/__main__.py`, `clipengine/autopilot.py`, `clipengine/judge.py`, `clipengine/metrics.py`, `clipengine/review.py`, `clipengine/sentinel.py`, `docs/judging.md`, `tests/test_engine.py`
- Removed (0)

Leak scan: clean.
