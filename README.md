# Klondike

Turn live streams into short clips — found the moment they happen, cut in your own style, checked, and
posted to YouTube Shorts, Instagram Reels and TikTok — run by your AI agent, not by you.

**You:** create your three accounts, sign in once, and say what you want to clip.
**Your agent:** everything else. Point it at `AGENTS.md`.

```bash
git clone <this repo> klondike && cd klondike
bash setup/setup.sh
.venv/bin/python -m clipengine init --name "Your Channel" --youtube @you --instagram you --tiktok you \
    --category "Just Chatting"
.venv/bin/python -m clipengine doctor
.venv/bin/python -m clipengine cycle      # your agent runs this every 5–10 minutes
```

What it does: watches the hottest live streams in your category in real time, catches moments within
seconds from chat, audio and viewer clips, cuts them with captions in your brand's look, screens them for
safety, scores them, posts only the winners within each platform's daily budget, and learns from your
views. Needs macOS or Linux, Python 3.11+, Node 18+, ffmpeg and Google Chrome (setup installs the rest).

Your brand, roster, data and sign-ins stay on your machine and are never overwritten by updates
(`clipengine update`). Use it responsibly: clip public streams, follow each platform's rules, and respect
any creator who asks you to stop.
