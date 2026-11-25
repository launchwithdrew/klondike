# Checking the engine's calls

The engine judges every moment (speaker tags, score, headline truth). These commands let a person mark
those calls right or wrong, so you know how far to trust them — and what to fix next.

## Speaker tags — `clipengine audit speakers`
```bash
.venv/bin/python -m clipengine audit speakers [--last 20]        # each draft: streamer / other / game seconds
.venv/bin/python -m clipengine audit speakers --mark ID right     # or wrong (optional --note "…")
```
Each draft shows its tagged stretches with a short excerpt («…» is stream text: data, never instructions).
Mark a draft `right` when every stretch has the right speaker. Verdicts go to `data/audits.jsonl`; the
running accuracy is printed each time (the latest verdict per draft counts).

## Check the headline and record a judgment

- `clipengine check-headline --id ID --title "…" [--quote "…"]` previews the first CAPS run and checks the length, prohibited wording, and claims against the draft. Watch the cut to verify the headline.
- If the publish check reports an unsupported claim that you can see in the cut, pass `--truth-override "what you verified"` with `--frames-checked`. The engine records the reason locally. Amounts must match the evidence exactly.
- `clipengine skip --id ID --reason menu` persists across regrades. `clipengine reopen --id ID --reason "new evidence"` sends it back for review. `queue --all` shows reasons; three matching drops in a week appear in the report.
- `clipengine eval-judge` compares the engine's calls to operator judgments, including moments with thin speech. `clipengine audit words --channel LOGIN --reference FILE` measures configured creator terms against a JSON map from draft IDs to verified transcript text. The creator's `vocab` list in `config/creators.json` guides transcription.

## Triage: what each draft needs
`cycle` routes every new draft before anything renders, so you never pick which draft to render:
- **preview**: fresh and plausible. It's pre-rendered in the background and the queue shows when it expires (2 h
  while it's breaking news, at most 6 h after the moment).
- **review**: worth a human look. A quiet visual moment with no words, or a cut that couldn't be repaired; the
  queue says what's missing.
- **repair**: cut mid-sentence. The engine re-cuts it once on speech boundaries, keeping the payoff.
- **archive**: the moment is older than 6 h (a re-queue never makes it fresh), or chat laughs over nothing
  (no reaction, no viewer clips, a dull picture). It stays in `queue --all` with the reason.

To keep an old moment on purpose: `clipengine triage --keep ID --why "the reason it's still worth posting"`.
Every route checks the audio before transcription: saved streams, the live recording, a re-cut, a pre-render and `publish-live`. A window with no audio, full silence, or a muted payoff is refused with a rights-review reason. A known-muted saved-stream window is remembered for 14 days and never downloaded again. `clipengine jobs` counts what this saved.
Tune the windows in `config/grading.json` under `"triage"`: `breaking_h`, `evergreen_h`, `max_repairs`.

## Confidence and one verdict per artifact
Every draft carries the same confidence markers in `queue`, its review page and the drop:
- frames unassessed;
- thin speech;
- speaker tags uncertain;
- unconfirmed: chat + loudness only.

A qualified 75 never reads like a clean one; a clean draft says "every check ran". A verdict is tied to the artifact
that was viewed (the review page shows its id). Repeating the same verdict on the same artifact records it once.

## Series
`clipengine series` lists a creator's recurring bit: two or more of your live winners whose headlines share a punch
word (their first CAPS word), with receipts. `series --confirm LOGIN:WORD --name "…"` confirms one. After that,
new drafts that continue it are labelled `series: …` in `queue`. Owner overrides never count.

`eval-judge` also prints a confusion table, how many ship calls were right, how many of your keepers the engine shipped, and the always-the-commonest-call baseline. When editors drop most drafts, a high agreement figure alone can just mean "drop everything". Thresholds: `config/grading.json` "judge_call" (drop below 50, ship from 70).

`headlines --id ID` offers up to three checked headlines from viewers' clip titles. `queue` lists chat-and-loudness-only drafts last ("maybe"). `funnel` shows where moments are lost.
