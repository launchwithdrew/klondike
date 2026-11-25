# Operate: the rhythm

**Once per machine:** `clipengine doctor` (every check, with the fix for each ✗), then `clipengine autopilot start`.
The autopilot runs `cycle` every 5 minutes by itself: it updates the engine, starts and stops watchers on live
streamers, records, drafts moments, renders and learns. No agent is needed for any of that.

**Your loop while streams are live (every 5 minutes):**
1. `clipengine next` prints one line: what to do now and the command. `--json` gives the same as an object.
2. Do it. The job's page tells you how: [judge-a-draft.md](judge-a-draft.md), [post-a-clip.md](post-a-clip.md) or
   [measure.md](measure.md).
3. Repeat until it says `nothing needs you`.

**Order of importance** (the order `next` uses):
1. A post whose upload lease ran out with no outcome: reconcile it first and never upload blind.
2. URGENT drafts: judge them the moment they're queued. A fresh moment is worth most.
3. Clips due on a platform.
4. Other queued drafts.
5. Analytics reads that are due.

**What needs you:** `clipengine attention` prints one line, or nothing at all. `attention --seen` marks it read.
Tell your owner about hot drafts and failures from that line.

**Never:**
- post outside `publish-queued` / `claim` / `posted`;
- claim you checked frames you didn't look at;
- edit or delete live posts;
- write a password anywhere. Your owner types passwords into the browser.
