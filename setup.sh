#!/bin/bash
# setup.sh — one-time setup for Neo. Run from the neo folder:  ./setup.sh
set -e

cd "$(dirname "$0")"
echo "==> Setting up Neo"

# 1. System libraries Neo needs (audio + speech phonemizer) + a compatible Python.
#    Kokoro (the voice) needs Python 3.10-3.12, so we use Python 3.12 specifically.
if command -v brew >/dev/null 2>&1; then
  echo "==> Installing system libs via Homebrew (portaudio, espeak-ng, python@3.12)"
  brew list portaudio >/dev/null 2>&1 || brew install portaudio
  brew list espeak-ng >/dev/null 2>&1 || brew install espeak-ng
  brew list python@3.12 >/dev/null 2>&1 || brew install python@3.12
else
  echo "!! Homebrew not found. Install it from https://brew.sh then re-run."
  echo "   Neo needs:  brew install portaudio espeak-ng python@3.12"
  exit 1
fi

# 2. Find a Kokoro-compatible Python (3.10-3.12). Prefer 3.12.
PYBIN=""
for cand in python3.12 python3.11 python3.10; do
  if command -v "$cand" >/dev/null 2>&1; then PYBIN="$cand"; break; fi
done
if [ -z "$PYBIN" ]; then
  PYBIN="$(brew --prefix)/bin/python3.12"
fi
echo "==> Using $($PYBIN --version) at $PYBIN"

# 3. (Re)create the virtual environment with that Python.
#    If an old .venv exists on the wrong Python, rebuild it.
if [ -d ".venv" ]; then
  VENV_OK=$(.venv/bin/python -c 'import sys; print(1 if (3,10)<=sys.version_info<(3,13) else 0)' 2>/dev/null || echo 0)
  if [ "$VENV_OK" != "1" ]; then
    echo "==> Rebuilding .venv on a compatible Python"
    rm -rf .venv
  fi
fi
if [ ! -d ".venv" ]; then
  "$PYBIN" -m venv .venv
fi
source .venv/bin/activate

echo "==> Installing Python packages (this can take a few minutes)"
pip install --upgrade pip >/dev/null
pip install -r requirements.txt

# 4. Pre-download the speech models so the first real run is fast.
echo "==> Downloading speech models"
python - <<'PY'
from faster_whisper import WhisperModel
WhisperModel("base.en", device="cpu", compute_type="int8")
from kokoro import KPipeline
KPipeline(lang_code="a")
print("models cached.")
PY

echo ""
echo "==> Setup complete."
echo ""
# Deliberately does NOT tell you to run neo.py from a terminal first.
# macOS attaches Microphone / Accessibility / Screen Recording permission to the
# BINARY that asks, so granting them to Terminal's python grants them to a
# different identity than Neo.app — you end up approving everything twice and
# wondering why the app still cannot hear you. ./make_app.sh builds the bundle
# and everything is granted once, to Neo.
echo "Next:  ./make_app.sh"
echo "       Builds Neo.app, starts it at login, and keeps it running."
echo "       Neo then walks you through permissions and the key, in its own voice."
echo ""
echo "Tip: in System Settings > Keyboard, set 'Press fn key to' = Do Nothing,"
echo "so holding fn doesn't pop up emoji or dictation."
