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
