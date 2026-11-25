# Fix something

1. `clipengine diagnose` matches recent logs to known problems and prints the fix.
2. `clipengine doctor` (or `--json`) runs every check and prints the fix for each one that fails.
3. Common ones:
   - **autopilot stopped:** `clipengine autopilot start`. `supervisor status` says why it stopped.
   - **a watcher is DOWN:** `clipengine sentinels` prints the restart command. `cycle` also revives it.
   - **disk low:** `cycle` clears old drafts and recordings by itself, but never a queued draft's stretch.
   - **a recording hole:** `clipengine sentinels` names each hole's cause (an ad break, a reconnect, a restart).
4. If it's still broken, tell your owner the `attention` line and the `diagnose` output. Don't change config or
   account settings without their yes.
