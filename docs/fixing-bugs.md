# Fixing a bug yourself

You may fix the engine's code when something is clearly broken and `diagnose` has no answer. The user
never needs to be involved. Follow this exactly:

1. **Reproduce** it with the smallest command that shows it. Note the exact error.
2. **Write a failing test** in `tests/test_engine.py` that captures it.
3. **Make the smallest fix** in `clipengine/` (Python) or `engine/` (the poster, Node).
4. **Run every test:** `.venv/bin/python -m pytest tests -q`. All must pass. If anything else breaks,
   undo your change (`git checkout -- <file>`) and try a smaller fix.
5. **Keep it local.** Don't commit to the engine's history; `clipengine update` saves your change as a
   patch in `local/patches/`, pulls the new version, and re-applies it (or retires it when the new
   version already fixed it).
6. **Report it upstream:** `clipengine support --problem "..."` and send the bundle, so the fix reaches
   everyone.

**Never** edit `brand/`, `config/creators.json`, `data/`, the ledger or the signed-in Chrome profile to
"fix" something; never weaken the safety screen or the repeat guard; never store a password.

**Where things live:** `clipengine/` finds, cuts, checks and scores (`sentinel.py` live detection,
`radar.py` discovery, `render.py` the look, `grade.py` scores, `gate.py` distribution, `learn.py`
learning) · `engine/` posts (`post.mjs`, one file per platform) · `tools/drop.mjs` hands clips to the
poster · `docs/known-problems.json` is the catalog `diagnose` uses — add an entry when you solve a new one.
