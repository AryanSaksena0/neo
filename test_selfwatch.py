"""test_selfwatch.py — Neo reading its own log.

The engine is only worth having if it is quiet when things are fine and
specific when they are not. Both halves are tested here against real lines
lifted out of this repo's neo.log, because a signature that has never actually
appeared is a guess.

Run: python3 test_selfwatch.py
"""
import sys
import time

sys.path.insert(0, ".")
import selfwatch as sw

FAILED = []


def check(name, cond):
    print(("PASS - " if cond else "FAIL - ") + name)
    if not cond:
        FAILED.append(name)


def keys(findings):
    return {f["key"] for f in findings}


# =========================================================================== #
# 1. Silence when nothing is wrong
# =========================================================================== #
ORDINARY = [
    "[09:12:01] (holding — opening a conversation)",
    "[09:12:02] [live] connected (gemini-3.1-flash-live-preview) — just talk.",
    "[09:12:06] (let go — thinking)",
    "[09:12:06] You: what's the weather",
    "[09:12:07] Neo: Sixty-one and clear.",
    "[09:12:40] [live] quiet for a while — closing the session.",
]
check("quiet: an ordinary conversation raises nothing at all",
      sw.scan(ORDINARY) == [])
check("quiet: and the spoken summary says so plainly",
      "nothing" in sw.summary(ORDINARY).lower())


# =========================================================================== #
# 2. The real faults, in the words they actually appeared in
# =========================================================================== #
# 6 Sept — the tap that reported itself enabled for eighty minutes.
check("finds: a deaf key listener",
      "tap-deaf" in keys(sw.scan([
          "[17:57:02] WATCHDOG: the fn tap says it is enabled but 41 key "
          "events went past it without being delivered — that is what "
          "sleep/wake does to a tap. Rebuilding it."])))

# 8 Sept — the device reset that invalidated the mic mid-session.
check("finds: two holds in a row that captured nothing",
      "nothing-said" in keys(sw.scan([
          "[21:19:28] (let go, but nothing was said — ignoring)",
          "[21:19:31] (let go, but nothing was said — ignoring)"])))

check("finds: the speaker refusing to open",
      "speaker-open" in keys(sw.scan([
          "[01:29:38] [live] could not open audio (Error opening "
          "RawOutputStream: Internal PortAudio error [PaErrorCode -9986])"])))

check("finds: a runaway turn",
      "runaway-turn" in keys(sw.scan([
          "[02:11:57] WATCHDOG: a turn has been running 5304s"])))

check("finds: a skill that stopped loading",
      "skill-skipped" in keys(sw.scan([
          "[23:20:29] [skills] skipped highlight_on_screen.py: NAME is "
          "already a built-in tool"])))

check("finds: the day's cloud voice running out",
      "tts-dry" in keys(sw.scan([
          "[22:08:49] [voice] the day's TTS allowance is gone"])))

check("finds: an unhandled exception",
      "traceback" in keys(sw.scan(["Traceback (most recent call last):"])))


# =========================================================================== #
# 3. One is life, two is a fault
# =========================================================================== #
# A single swallowed hold happens. Two in a row is the microphone not working,
# and that distinction is the difference between a useful card and a nag.
ONE_LOST = ["[21:19:28] (let go, but nothing was said — ignoring)"]
check("threshold: one lost hold is not worth a card",
      "nothing-said" not in keys(sw.scan(ONE_LOST)))
check("threshold: two in a row is",
      "nothing-said" in keys(sw.scan(ONE_LOST * 2)))


# =========================================================================== #
# 4. Ranking and repetition
# =========================================================================== #
MIXED = [
    "[01:00:00] [voice] the day's TTS allowance is gone",
    "[01:00:01] WATCHDOG: a turn has been running 900s",
]
check("rank: the serious fault is reported before the cosmetic one",
      sw.scan(MIXED)[0]["key"] == "runaway-turn")

seen = {}
check("repeat: a fault raised now is not raised again immediately",
      sw.due("tap-deaf", seen, now=1000.0)
      and not sw.due("tap-deaf", {"tap-deaf": 1000.0}, now=1100.0))
check("repeat: ...but it is raised again hours later",
      sw.due("tap-deaf", {"tap-deaf": 1000.0}, now=1000.0 + sw.REPEAT_S + 1))


# =========================================================================== #
# 5. It must never speak, and never edit anything
# =========================================================================== #
_src = open("selfwatch.py").read()
check("safety: the watcher cannot talk",
      "say(" not in _src and "_speak" not in _src)
check("safety: the watcher cannot write to anything",
      'open(' in _src and '"w"' not in _src and "'w'" not in _src)

def _cards_are_silent():
    """_deliver_insight CAN speak — but only for a finished Claude job, and
    only with a live session open. Everything else is card-only.

    Worth pinning precisely rather than loosely: the first version of this
    check asserted the whole function never speaks, which was simply false and
    would have hidden the real question, which is whether a "warn" card can
    reach that branch. It cannot: the speech sits inside the kind == "claude"
    block, and selfwatch only ever emits "warn".
    """
    body = open("neo.py").read().split("def _deliver_insight")[1]
    body = body.split("\n    def ")[0]
    if 'insight.get("kind") == "claude"' not in body:
        return False
    after_claude = body.split('insight.get("kind") == "claude"')[1]
    before_claude = body.split('insight.get("kind") == "claude"')[0]
    # no speech before the claude branch, and selfwatch never sends that kind
    return ("self.say(" not in before_claude
            and "self.say(" in after_claude
            and '"kind": "warn"' in open("selfwatch.py").read())
check("safety: only a finished Claude job speaks; a fault card never does",
      _cards_are_silent())


# =========================================================================== #
# 6. It survives a broken or missing log
# =========================================================================== #
check("robust: a missing log is empty, not an exception",
      sw.read_tail("/no/such/file.log") == [])
check("robust: scanning nothing finds nothing",
      sw.scan([]) == [] and sw.scan(None if False else []) == [])


def _check_raises_cards_and_dedupes():
    """A card must be about something happening NOW.

    The first version scanned the last 200 KB on every pass, so its very first
    boot reported four days of history — including 134 rebuilds from a bug that
    had been fixed an hour earlier. A watcher that reports faults you already
    fixed is one you learn to ignore.
    """
    import os, tempfile
    fd, path = tempfile.mkstemp(suffix=".log")
    os.write(fd, b"[01:00:00] WATCHDOG: a turn has been running 4000s\n")
    os.close(fd)
    raised = []
    w = sw.SelfWatch(notify=raised.append, log=lambda *a: None, path=path)
    before = w.check(now=1000.0)              # history: silence
    with open(path, "a") as f:                # a NEW fault arrives
        f.write("[01:05:00] WATCHDOG: a turn has been running 900s\n")
    during = w.check(now=1000.0)
    again = w.check(now=1000.0)               # same fault: deduped
    os.unlink(path)
    return (before == [] and len(during) == 1
            and during[0]["key"] == "runaway-turn" and again == []
            and len(raised) == 1)
check("engine: history is never reported, only what happens from now on",
      _check_raises_cards_and_dedupes())

def _rotation_is_survived():
    import os, tempfile
    fd, path = tempfile.mkstemp(suffix=".log")
    os.write(fd, b"x" * 5000)
    os.close(fd)
    w = sw.SelfWatch(notify=lambda c: None, log=lambda *a: None, path=path)
    with open(path, "w") as f:                # truncated underneath it
        f.write("[02:00:00] STARTUP STALLED\n")
    found = w.check(now=1000.0)
    os.unlink(path)
    return len(found) == 1 and found[0]["key"] == "boot-stalled"
check("engine: a rotated or cleared log resets instead of reading garbage",
      _rotation_is_survived())


# =========================================================================== #
# 7. Wired into Neo, and reachable by voice
# =========================================================================== #
import agent
check("wired: 'what's been going wrong' is a tool the model can call",
      any(t.__name__ == "self_check" for t in agent.TOOLS))

_neo = open("neo.py").read()
check("wired: the watcher starts once the models are up",
      "selfwatch.SelfWatch(" in _neo and "self._selfwatch.start()" in _neo)
check("wired: it reports through the silent card channel",
      "notify=self._deliver_insight" in _neo)


if FAILED:
    print(f"\n{len(FAILED)} FAILED:")
    for n in FAILED:
        print("  - " + n)
    sys.exit(1)
print("\nSelf-watch clean.")


# ---- a double-press of fn is not a deaf microphone ----
_tap_log = [
    "[18:21:13] (let go, but nothing was said — ignoring)",
    "[18:21:14] (let go, but nothing was said — ignoring)",
    "[18:21:15] [stealth] on",
]
check("double-press: the two empty taps before '[stealth] on' are not a deaf mic",
      not any(f["key"] == "nothing-said" for f in sw.scan(_tap_log)))
_real = ["[10:00:01] (let go, but nothing was said — ignoring)",
         "[10:00:09] (let go, but nothing was said — ignoring)",
         "[10:00:20] You: hello"]
check("double-press: two empty holds with NO stealth toggle still count",
      any(f["key"] == "nothing-said" for f in sw.scan(_real)))
