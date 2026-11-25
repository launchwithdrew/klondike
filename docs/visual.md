# Seeing the picture (`clipengine visual`)

Chat, loudness and speech can't see the screen. Most drafts an editor drops are dropped for what is ON screen:
a map or pause menu, the streamer's room instead of the game, a death screen, nothing happening, or a scene that
must never post (a strip club, underwear, nudity). The picture model reads each draft's contact sheet for these.

## Set it up once
```bash
clipengine visual setup      # downloads the picture model (CLIP ViT-B/32, MIT licence, ~155 MB) once
clipengine doctor            # "visual: advisory mode"
```
It runs on the same libraries as transcription (no GPU, about a second per draft) and lives in
`~/.cache/clipengine/clip` (`CLIPENGINE_MODELS` moves it). The download is pinned and checked by checksum; a
failed or tampered download changes nothing. Without it, the engine works exactly as before.

## What it does
- **Every new draft** gets a `vis …` note on its second `queue` line: `look` (action minus everything an editor
  drops for), `act` (crash, chase, shootout), and `menu`, `off-game`, `dull`, `death` when they're high.
- **A safety flag, always on:** `adult scene on screen? — look before posting` when a frame looks like a strip
  club, underwear or nudity. The draft is scored down, never marked urgent, and `publish` repeats the warning.
  A transcript never sees this. The flag doesn't refuse anything. You look at the frames and decide, as always.
- **Headline check at publish:** `visual_headline` prints how well the headline describes the frames, as a number
  only. Measured on 81 GTA VI viewer clips, the model scored a title on its own frames no better than on another clip
  of the same streamer (AUC 0.54; `eval/eval_headline_fit.py`), so it is not evidence: check the payoff is on screen.
- `clipengine visual read --id DRAFT [--title "…"]` prints the reads for one draft.

## Scoring with it (only on your evidence)
The model knows what a menu or a car crash looks like. It does not know what YOUR viewers want. On 56 real GTA VI
moments, the ones viewers clipped looked *less* like action than random stretches: viewers clip reactions,
webcams and IRL moments. So the `look` measure changes nothing until your own verdicts show it helps:
```bash
clipengine visual eval          # your judged drafts' frames (data/labels): how well each measure splits ships from drops
clipengine visual eval --save   # keeps the best measure, only if it separates (AUC ≥ 0.70, 8+ ships and 8+ drops)
```
Then set `"visual": {"mode": "score"}` in `config/grading.json`. Drafts on the drop side of the saved threshold get
`picture reads like a drop` and the `visual` penalty. Without a saved eval that separates, `score` stays advisory.
`"mode": "off"` turns all of it off, the safety flag included.
