# Moving to another machine

To move the engine to a bigger or always-on machine, follow these steps. You don't lose a clip, a post or a
moment, and only one machine posts at any time.

1. **Snapshot (old machine).** Run `clipengine migrate snapshot --to /path/to/folder`. The folder can be local
   or a mounted NAS share. The snapshot holds your brand and approved look, config, data, the ledger and the
   finished clips, with a checksum for every file. Passwords, cookies, tokens and the browser profile are never
   included. Sign in again on the new machine.
2. **Verify (new machine).** Run `clipengine migrate verify FILE`. It restores into a separate folder, never
   over an install, and checks the checksums. It also checks that the post ledger, the repeat guard, posting
   attempts, finished clips and the approved look read back with the same counts. Only a fully checked copy
   gets the final folder name.
3. **Shadow (new machine).** Install the engine as usual (setup, `doctor`), then run `clipengine migrate
   shadow`. It captures and drafts, but publishing is off there: `claim` and posting drops refuse. Compare
   its drafts with the old machine's for a stream or two.
4. **Hand over once.**
   - On the old machine, run `clipengine migrate release`. Its autopilot stops, and the handoff is recorded in
     `data/migration.json` with its last confirmed coverage. From then on it refuses to start the autopilot or
     post.
   - On the new machine, after `git pull`, run `clipengine migrate takeover`. It becomes the leader and turns
     publishing on. Catch-up resumes from the old machine's last confirmed coverage, so the handover minutes
     are scanned too.
5. **Roll back** if something is wrong: run `migrate release` on the new machine, then `migrate takeover` on the
   old one.

`clipengine migrate status` shows this machine's role (leader, shadow or released) and the handoff history.
`doctor` flags a machine that isn't the leader.

A plain `restore FILE` (from `backup`) unpacks into a staging folder first. If it's interrupted, `doctor` says
so and running the same restore again finishes it.
