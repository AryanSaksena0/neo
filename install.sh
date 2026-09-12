#!/usr/bin/env bash
# install.sh — Neo, in one line.
#
#   curl -fsSL https://raw.githubusercontent.com/aryansaksena2010-web/neo/main/install.sh | bash
#
# What it does, in order, saying each step once and nothing else:
#   1. checks this is a Mac with Apple Silicon
#   2. installs Homebrew if it is missing (Apple's own developer tools come too)
#   3. gets Neo into ~/neo (a fresh clone, or a pull if it is already there)
#   4. runs setup.sh  — Python, the audio libraries, the speech models
#   5. runs make_app.sh — builds Neo.app and the agent that keeps it running
#   6. opens Neo, which walks the person through permissions and the key
#
# Re-runnable. Nothing here asks a question it can answer itself.
set -euo pipefail

REPO="${NEO_REPO:-https://github.com/aryansaksena2010-web/neo.git}"
DIR="${NEO_DIR:-$HOME/neo}"

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
note() { printf '   %s\n' "$*"; }
die()  { printf '\n\033[31m%s\033[0m\n' "$*"; exit 1; }

say "Neo"
echo

# 1 ------------------------------------------------------------------------
[ "$(uname -s)" = "Darwin" ] || die "Neo runs on macOS."
if [ "$(uname -m)" != "arm64" ]; then
  die "Neo needs an Apple Silicon Mac (M1 or later). This one is $(uname -m)."
fi
note "macOS $(sw_vers -productVersion), Apple Silicon"

# 2 ------------------------------------------------------------------------
if ! command -v brew >/dev/null 2>&1; then
  say "Installing Homebrew (this asks for your password once)"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  # Apple Silicon puts it here; make it visible to the rest of this script.
  eval "$(/opt/homebrew/bin/brew shellenv)"
fi
note "Homebrew ready"

if ! command -v git >/dev/null 2>&1; then
  say "Installing git"
  brew install git
fi

# 3 ------------------------------------------------------------------------
if [ -d "$DIR/.git" ]; then
  say "Updating Neo in $DIR"
  git -C "$DIR" pull --ff-only
else
  say "Getting Neo into $DIR"
  git clone --depth 1 "$REPO" "$DIR"
fi
cd "$DIR"

# 4 ------------------------------------------------------------------------
say "Setting up (Python, audio, speech models — a few minutes the first time)"
./setup.sh

# 5 ------------------------------------------------------------------------
say "Building Neo.app"
./make_app.sh

# 6 ------------------------------------------------------------------------
echo
say "Done. Neo is opening."
note "It will walk you through permissions and a free Gemini key — about three minutes."
note "Then: hold fn, say the thing, let go."
echo
note "Later:   ~/neo/go.sh          restart + self-test"
note "         python3 ~/neo/neo.py --doctor   if anything seems off"
