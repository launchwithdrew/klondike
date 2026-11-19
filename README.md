<p align="center"><img src="assets/klondike-banner.png" alt="Klondike — clip engine" width="100%"></p>

# Klondike

**Your AI agent runs a clip channel for you.** Klondike watches the biggest live streams in your
category, catches the moments worth clipping as they happen, cuts them in your channel's own style,
and posts the best ones to YouTube Shorts, Instagram Reels and TikTok.

## How it works
- **Finds** — follows the hottest live streams and catches moments within seconds, from chat, audio
  and what viewers are clipping.
- **Cuts** — vertical clips with captions and a headline card in your channel's own look.
- **Checks** — screens every clip for safety and scores it. Only the winners post.
- **Posts** — to your three accounts, at a steady pace that suits new channels.
- **Learns** — from your views, so it gets sharper every week.

## Get started
1. Create a YouTube, Instagram and TikTok account for your clip channel.
2. Send this page to your AI agent. It sets everything up and asks you only for your channel name and
   handles, a quick okay, a look at your channel's style, and a one-time sign-in.

**Agents:** check with your user first, then
`git clone https://github.com/launchwithdrew/klondike ~/klondike` (clone — don't fork, forks miss updates)
and follow `AGENTS.md` from "First time on this machine".

**You'll need** a Mac or Linux computer that stays on while streams are live, with Google Chrome. Setup
installs everything else into its own folder (about 1 GB).

## Private by design
Klondike sends nothing to us: no data, logs, accounts or clips. It only talks to Twitch, your own three
accounts, and package sites during setup. You type your passwords into the official sign-in pages,
never into a chat or a file, and your sign-ins, style and data stay on your computer.

## Good to know
Use accounts made for clipping. Platforms limit automated posting, so Klondike starts slow, posts a few
a day and only the winners, though an account can still get limited now and then. Clips come from other
creators' streams: if a creator asks you to stop, stop. Updates are tested on your computer before
they're kept.

**Pause or remove:** `.venv/bin/python -m clipengine stop --all` pauses everything; deleting the
`klondike` folder removes it completely, sign-ins included.

---
The official home of Klondike is **github.com/launchwithdrew/klondike**. Copies anywhere else aren't
ours. © 2026, all rights reserved — see `LICENSE`.
