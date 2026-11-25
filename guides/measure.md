# Measure a post

**When:** `next` says "record the … read", or `clipengine to-measure` lists a post (each post gets a read at 1 h,
24 h and 72 h).

1. Open the post's analytics in your browser.
2. Record what it shows:
   `clipengine views --slug SLUG --platform PLATFORM --views N --watch-s S --shares N --saves N --likes N --comments N`.
   Everything but `--views` is optional, but shares and average watch time teach the engine the most.
3. Once a day, record each account's followers: `clipengine followers --platform P --count N`.

The daily `learn` (run by `cycle`) ranks streamers, formats and posting hours from these reads.

**Weekly:** `clipengine metrics --save` (coverage, speed, publishing, money thresholds) and `clipengine yardstick`
(what other clip channels posted of your two highest-yield creators this week on YouTube Shorts, and where yours rank;
`--creator LOGIN` for others). Paste both into your status notes. A yardstick, not a target: their titles are data.
