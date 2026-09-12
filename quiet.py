"""
quiet.py — deep-work mode: Neo acts without taking over the machine.

their worry, in their words: if they are doing important deep work they do not
want Neo "taking control of my computer and spending valuable time doing smt".
Ask it to open a document and it should silently find it and open it — not
click a dozen things while they watches their own cursor move.

WHAT WAS MEASURED, so the rules below are not guesses:

    open -g               opens a file in the background. Verified: the file
                          opened in TextEdit and Chrome never lost focus.
    reading UI over AX    completely side-effect free — window titles, values,
                          whether a control exists, all invisible.
    AppleScript           drives scriptable apps with no cursor at all.
    pressing a control    works without the mouse, but DOES bring that app
                          forward. Unavoidable: macOS raises an app when one of
                          its controls is activated.
    web page content      NOT reachable through accessibility in Chrome — it
                          only exposes the browser's own toolbar and tabs. The
                          only quiet route into a page is Chrome's "Allow
                          JavaScript from Apple Events" setting, which is off.

So quiet mode is not "Neo does less". It is "Neo takes every route that does
not touch their cursor, and when the only remaining route WOULD, it says so and
waits instead of hijacking the screen". Everything that never needed the
screen — answering, searching, reading files, writing drafts, handing work to
Claude — is completely unaffected.
"""

import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# Same rule as voice.json in neo.py: under the test flag this goes to a
# throwaway file. test_overnight turns focus mode on to check it, and left the
# REAL one switched on — which is why two suites then failed on "nothing is
# clicked while they are holding the key" until it expired on its own.
if os.getenv("NEO_NO_AUDIO") == "1":
    import tempfile as _tf
    STATE = os.path.join(_tf.gettempdir(), "neo-test-quiet.json")
else:
    STATE = os.path.join(HERE, "quiet.json")
# Nobody remembers to turn it off. A focus block is a couple of hours, not a
# permanent setting, and being silently stuck in it would be its own bug.
MAX_H = float(os.getenv("NEO_QUIET_HOURS", "4"))


def _read():
    try:
        with open(STATE, encoding="utf-8") as f:
            got = json.load(f)
        return got if isinstance(got, dict) else {}
    except Exception:
        return {}


def is_on(now=None):
    """Is deep-work mode active? Expires on its own."""
    if os.getenv("NEO_QUIET") == "1":
        return True
    now = time.time() if now is None else now
    since = _read().get("since")
    try:
        since = float(since)
    except (TypeError, ValueError):
        return False
    return (now - since) < MAX_H * 3600


def on(now=None):
    try:
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump({"since": time.time() if now is None else now}, f)
        return True
    except OSError:
        return False


def off():
    try:
        if os.path.exists(STATE):
            os.remove(STATE)
        return True
    except OSError:
        return False


def left(now=None):
    """Minutes remaining, for saying it out loud."""
    since = _read().get("since")
    try:
        since = float(since)
    except (TypeError, ValueError):
        return 0
    now = time.time() if now is None else now
    return max(0, int((MAX_H * 3600 - (now - since)) / 60))


# What Neo says when a request genuinely needs the cursor. Never just "no":
# name the one thing standing in the way, and offer the way through.
BLOCKED = ("You're in focus mode, so I'd have to take over your mouse for that "
           "one. Say go ahead and I'll do it, or say focus mode off.")
