#!/usr/bin/env bash
# go.sh — the only command. Reinstall, verify, commit.
#
# Claude can write files into this folder but cannot run macOS binaries
# (.venv/bin/python, launchctl) — the desktop bridge grants terminals
# click-only access. So it edits, and this runs. One command, every time.
set -uo pipefail
cd "$(dirname "$0")" || exit 1

echo "── restarting neo ─────────────────────────────"
./make_app.sh --stop >/dev/null 2>&1
./make_app.sh --start 2>&1 | tail -3

echo
echo "── waiting for boot ───────────────────────────"
for i in $(seq 1 40); do
  grep -q "Ready\. Hold fn" <(tail -30 neo.log 2>/dev/null) && break
  sleep 1
done
tail -4 neo.log

echo
echo "── tests ──────────────────────────────────────"
# Every suite's LAST LINE is its verdict, so tail is what gets shown — but a
# suite that dies mid-run also has a last line, and it looks like a traceback
# instead of a verdict. That is exactly what happened: test_neo.py crashed on a
# typo'd constant, roughly half the file stopped running, and what showed up
# here was the tail of a stack trace where a pass line should have been.
# So the exit code decides, and a failure prints the whole thing.
fail=0
# Checks that touch the real machine — opening files, switching apps, writing
# the clipboard, drawing on screen — are OFF here on purpose. go.sh runs after
# every change, and every run used to steal focus and open documents while
# the owner was working. Run them deliberately instead:
#     NEO_LIVE_TESTS=1 NEO_LIVE_CLICK=1 .venv/bin/python test_overnight.py
unset NEO_LIVE_TESTS NEO_LIVE_CLICK
# NEO_NO_AUDIO=1 for every suite. Importing sounddevice starts a CoreAudio
# I/O thread, and in a test process that thread faulted with a hard SIGSEGV
# about twenty seconds in — no stream ever opened, no Python frame on the
# crashing thread — which put a "Python quit unexpectedly" dialog on screen
# after almost every run. The tests check routing, parsing and wiring; not one
# of them needs a real speaker. See the stub at the top of neo.py.
run_suite() {
  local name="$1" out rc short
  short="$(basename "$name")"
  out=$(NEO_NO_AUDIO=1 .venv/bin/python "$name" 2>&1); rc=$?
  if [ $rc -eq 0 ]; then
    printf "  %-22s %s\n" "$short" "$(echo "$out" | tail -1)"
  else
    fail=1
    printf "  %-22s FAILED (exit %d)\n" "$short" "$rc"
    echo "$out" | grep -E "^(FAIL|MISROUTE|Traceback|[A-Za-z]*Error)" | head -12
    echo "$out" | tail -4
  fi
}
# DISCOVERED, not listed. The hardcoded list here drifted: test_chrome.py was
# written, committed, and then never run by anything for weeks, because nobody
# remembered to add it to a line in a shell script. A suite that the runner
# does not know about is not a test. Anything matching test_*.py runs.
#
# The adversarial ones — test_deck_hard, test_highlight — are in here too:
# every check in them came from a real defect that was actually found.
#
# SKIP is only for suites that need something this machine may not have (a
# logged-in Claude CLI, a live model). Each one names why.
# Suites live in tests/ so the repo root stays readable on GitHub (a hundred
# files pushed the README below the fold). They still RUN from the root, which
# is what keeps open("neo.py") and sys.path.insert(0, ".") working inside them.
SKIP="test_claude_live.py test_live_tools.py"   # need a logged-in claude CLI + live API
for suite in tests/test_*.py; do
  base="$(basename "$suite")"
  case " $SKIP " in *" $base "*) printf "  %-22s skipped (needs a live account)\n" "$base"; continue;; esac
  run_suite "$suite"
done

echo
echo "── committing ─────────────────────────────────"
if [ "$fail" -ne 0 ]; then
  echo "a suite failed — NOT committing. Fix it, then run ./go.sh again."
  exit 1
fi
git add -A >/dev/null 2>&1
if git diff --cached --quiet; then
  echo "nothing to commit"
else
  git commit -m "${1:-neo: update}" 2>&1 | head -2
fi

echo
echo "── done. hold fn and talk. ────────────────────"
echo "if it misbehaves:  tail -40 neo.log   and   cat neo.err"
