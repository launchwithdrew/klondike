#!/usr/bin/env bash
# One command to make any machine ready to run the clip engine.
# Idempotent: safe to re-run after every `git pull`.
#   bash setup/setup.sh            # everything
#   bash setup/setup.sh --no-post  # clipping only (skip the poster's node deps / Chrome check)
set -euo pipefail
cd "$(dirname "$0")/.."
REPO=$(pwd)
ok() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
die() { printf '  \033[31m✗\033[0m %s\n' "$1"; exit 1; }

echo "Engine setup in $REPO"

# 1. ffmpeg / ffprobe (rendering, audio probe, frame checks)
if command -v ffmpeg >/dev/null && command -v ffprobe >/dev/null; then ok "ffmpeg $(ffmpeg -version | head -1 | cut -d' ' -f3)"
elif [ "$(uname)" = Darwin ] && command -v brew >/dev/null; then brew install ffmpeg && ok "ffmpeg installed"
elif command -v apt-get >/dev/null; then (sudo -n true 2>/dev/null && sudo apt-get install -y ffmpeg || apt-get install -y ffmpeg) && ok "ffmpeg installed"
else die "install ffmpeg (brew install ffmpeg / apt-get install ffmpeg) and re-run"; fi

# 2. Python venv + packages (faster-whisper, numpy, pillow, opencv-python-headless, pytest)
PY=$(command -v python3.12 || command -v python3.11 || command -v python3 || true)
[ -n "$PY" ] || die "python3 not found"
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
ok "python venv (.venv) with $(.venv/bin/python -c 'import faster_whisper,PIL,cv2;print("faster-whisper",faster_whisper.__version__,"/ pillow",PIL.__version__,"/ opencv",cv2.__version__,"(facecam layout)")')"

# 2b. A public copy only ever pulls engine releases (clipengine update) — never pushes to the engine's repo
if [ -f .public-build ]; then
  .venv/bin/python -c "from clipengine import repo; raise SystemExit(0 if repo.lock_push() else 1)" && ok "this copy only pulls engine updates (push disabled)"
fi
[ -d chrome-profile ] && chmod 700 chrome-profile

# 2c. The shared operation repo: git pushes with the GitHub CLI's login (`gh auth login` alone doesn't
#     connect git to it — `gh auth setup-git` does; there is no `setup-gitconfig`)
if [ ! -f .public-build ] && command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then
  if ! git push --dry-run -q origin HEAD:refs/heads/_setup_push_check >/dev/null 2>&1; then
    gh auth setup-git >/dev/null 2>&1 || true
  fi
  if git push --dry-run -q origin HEAD:refs/heads/_setup_push_check >/dev/null 2>&1; then ok "git can push (GitHub CLI login)"
  else warn "git can't push yet — run: gh auth setup-git  (needs gh auth login first)"; fi
fi

# 3. Whisper models (small = words, tiny = timing). First run downloads ~540 MB.
HF_HUB_DISABLE_XET=1 .venv/bin/python - <<'PY'
from faster_whisper import WhisperModel
for m in ("tiny", "small"):
    WhisperModel(m, device="cpu", compute_type="int8")
print("  \033[32m✓\033[0m whisper tiny + small cached")
PY
# 3b. Speaker model (who is talking: streamer / other / game) — WeSpeaker ResNet34, ONNX, 26 MB, CPU
HF_HUB_DISABLE_XET=1 .venv/bin/python - <<'PY' || echo "  ! speaker model not downloaded — speaker tags fall back to log-mel statistics"
from huggingface_hub import hf_hub_download
hf_hub_download("Wespeaker/wespeaker-voxceleb-resnet34-LM", "voxceleb_resnet34_LM.onnx")
print("  \033[32m✓\033[0m speaker model cached")
PY

# 4. Brand assets
if [ -f brand/brand.json ]; then ok "brand pack (brand/brand.json)"; else warn "no brand pack yet — next: .venv/bin/python -m clipengine init"; fi
for f in InterDisplay-Bold.ttf InterDisplay-ExtraBold.ttf NotoColorEmoji.ttf; do
  [ -f "assets/fonts/$f" ] || die "assets/fonts/$f missing"
done
ok "fonts (Inter Display, color emoji)"

# 5. Tests (offline, real ffmpeg)
# a failing test must not stop setup (the poster steps below still matter) — but it must be loud
if TEST_OUT="$(.venv/bin/python -m pytest tests -q 2>&1)"; then
  ok "tests: $(printf '%s\n' "$TEST_OUT" | tail -1)"
else
  printf '  \033[31m✗ tests FAILED: %s\033[0m\n' "$(printf '%s\n' "$TEST_OUT" | tail -1)"
  printf '%s\n' "$TEST_OUT" | grep -E '^(FAILED|ERROR)' | head -5 | sed 's/^/      /'
  printf '  \033[31m  next: .venv/bin/python -m pytest tests -q   (read the first failure; do not merge or post until it passes)\033[0m\n'
  SETUP_TESTS_FAILED=1
fi

# 6. Poster (posting from this machine)
if [ "${1:-}" != "--no-post" ]; then
  if command -v node >/dev/null; then
    (cd engine && npm ci --silent >/dev/null 2>&1 || npm install --silent >/dev/null 2>&1) && ok "poster deps (node $(node -v))"
  else warn "node not found — install Node 18+ to post from this machine (clipping still works)"; fi
  CHROME="${CLIPENGINE_CHROME:-}"
  for p in "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" /usr/bin/google-chrome /usr/bin/google-chrome-stable /usr/bin/chromium /usr/bin/chromium-browser; do
    [ -z "$CHROME" ] && [ -x "$p" ] && CHROME="$p"
  done
  if [ -n "$CHROME" ]; then ok "Chrome: $CHROME"
    if [ -d chrome-profile ]; then ok "poster Chrome profile exists — run: (cd engine && node post.mjs --check)"
    else warn "not signed in yet on this machine — run: (cd engine && node post.mjs --login), the channel owner signs in once"; fi
  else warn "Chrome not found — posting from this machine needs Google Chrome (or set CLIPENGINE_CHROME)"; fi
fi

if [ -n "${SETUP_TESTS_FAILED:-}" ]; then
  printf '\033[31mSet up, but the tests FAILED (see above) — fix them before running the operation.\033[0m\n'
  exit 1
fi
echo 'Ready. Next: .venv/bin/python -m clipengine doctor   (then read AGENTS.md: your rhythm is clipengine cycle)'
