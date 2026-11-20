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
- Use `clipengine skip --id ID --reason menu` when you drop a draft. Other reasons include map, loading, driving, dark, static, cutscene, sexual, dud, and other. `queue --all` shows the reasons. Three matching reasons in a week appear in the report.
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
A saved-stream window whose payoff is copyright-muted is refused before transcription, with a rights-review reason.
Tune the windows in `config/grading.json` under `"triage"`: `breaking_h`, `evergreen_h`, `max_repairs`.
