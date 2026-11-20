# Reliability — keeping capture alive

## The supervisor
`clipengine supervisor install` (once per machine) runs the autopilot as a native service: a launchd LaunchAgent
on macOS (starts at login) or a systemd user service on Linux (starts at login; run `loginctl enable-linger`
once to start at boot). The system restarts it within 30 seconds of any exit that wasn't `clipengine autopilot
stop`. A stop stays in force across reboots until `clipengine autopilot start`. On a host with neither (a
container, an ephemeral machine), `supervisor status` says DEGRADED: the autopilot runs detached and a death is
only noticed by a sentinel or the next command.

There is one capture leader per installation: an OS lock that a killed process releases at once. A service, a
manual start and a sentinel's revival can't run two loops. A heartbeat every 30 seconds runs beside the cycle, so
a long scan can't hide a dead loop. A cycle with no progress for 40 minutes exits so it is restarted cleanly.

## Incidents
Every outage is one incident with:
- when it went dark, and the last thing it was doing;
- its cause: `signal` (killed by someone else), `crash`, `stalled`, `reboot`, `suspension` (the host slept), or
  `unknown` (killed without warning: the out-of-memory killer or `kill -9`; the engine never guesses);
- when it came back, and what brought it back.

The attention line shows an open incident once and its recovery once. Three deaths in an hour is a crash loop: it
says so, and restarts back off (30 s, 60 s … up to 10 min). `clipengine supervisor status` lists the open
incidents and the last 7 days, and `clipengine diagnose` prints them too.

A sleeping or dead host can't alert you itself. Set `"heartbeat_url"` in `config/ops.json` to a dead-man's-switch
monitor (a service that alerts when its URL stops being called). The autopilot calls it every minute.

## Heavy work takes turns
Transcription and video encodes go through one admission gate: at most `heavy_slots` at once (lite profile 1,
full 2). The order is a live moment first, then your publish, catch-up, the sweep and pre-renders, then
historical scans. Nothing heavy starts while free memory is under `heavy_min_free_gb` (0.8 GB), so the
recorders keep going. `clipengine jobs` shows what runs, what waits and why, and 24-hour wait and run times.

## Recorders
A recorder that is alive but has written no media for 6 minutes (longer than an ad break) is restarted by
itself. The sentinel and the other streams carry on. Holes over 10 seconds go on the attention line with their
cause.

## Catch-up
After a gap, `cycle` scans the saved streams of the missing time once. By hand, `clipengine catchup` without
`--since` resumes from the last completed coverage (at most 12 hours back). With no coverage on record it asks for
`--since` instead of guessing.
