"""test_stealth.py — quiet Neo. Double-press detection, the on/off state with
its TTL, the spoken switches, and the guarantees: nothing plays, nothing pops.

Run: python3 test_stealth.py
"""
# Suites live in tests/ but RUN from the repo root, so that the modules
# under test import and open("neo.py") still resolves. This makes the
# import work either way, so a suite can also be run directly.
import os as _bootstrap_os, sys as _bootstrap_sys
_bootstrap_sys.path.insert(0, _bootstrap_os.path.dirname(
    _bootstrap_os.path.dirname(_bootstrap_os.path.abspath(__file__))))

import os
import sys
import tempfile

sys.path.insert(0, ".")
import stealth

FAILED = []


def check(name, cond):
    print(("PASS - " if cond else "FAIL - ") + name)
    if not cond:
        FAILED.append(name)


# ---- the double-press ----
d = stealth.DoubleTap()
d.press(0.0); r1 = d.release(0.1)
d.press(0.3); r2 = d.release(0.4)
check("double: two quick taps make a double", r1 is False and r2 is True)
d = stealth.DoubleTap()
d.press(0.0); d.release(0.1); d.press(1.0)
check("double: two taps a second apart do not", d.release(1.1) is False)
d = stealth.DoubleTap()
d.press(0.0); d.release(0.1); d.press(0.3)
check("double: tap then HOLD (talking) is not a double", d.release(1.5) is False)
d = stealth.DoubleTap()
d.press(0.0); d.release(1.0); d.press(1.1)
check("double: a hold then a tap is not a double either", d.release(1.2) is False)
d = stealth.DoubleTap()
check("double: a release with no press is harmless", d.release(0.0) is False)
d = stealth.DoubleTap()
d.press(0.0); d.release(0.1); d.press(0.3); d.release(0.4); d.press(0.6)
check("double: the third tap starts a fresh count, not a second double", d.release(0.7) is False)

# ---- the mode ----
path = os.path.join(tempfile.mkdtemp(), "stealth.json")
m = stealth.Mode(path)
check("mode: off by default", m.active(0) is False)
check("mode: toggle turns it on and says so", m.toggle(100) is True and m.active(101) is True)
check("mode: survives a restart (persisted)", stealth.Mode(path).active(102) is True)
check("mode: expires after four hours", stealth.Mode(path).active(100 + 4 * 3600 + 1) is False)
m2 = stealth.Mode(path); m2.set(True, 0)
check("mode: set(False) really turns it off", (m2.set(False, 1), m2.active(2))[1] is False)

# ---- the words ----
check("words: 'go stealth' / 'stealth mode' turn it on", stealth.wants_on("go stealth") and stealth.wants_on("stealth mode please"))
check("words: 'normal mode' / 'voice back' turn it off", stealth.wants_off("normal mode") and stealth.wants_off("okay voice back"))
check("words: 'stealth off' is OFF, not on", stealth.wants_off("stealth off") and not stealth.wants_on("stealth off"))
check("words: ordinary talk is neither", not stealth.wants_on("what's the weather") and not stealth.wants_off("read my email"))

# ---- the box state ----
b = stealth._Box()
b.add("You", "hi"); b.add("Neo", "hello")
s = b.snapshot()
check("box: keeps the thread in order", [l["who"] for l in s["lines"]] == ["You", "Neo"])
b.set_working("Searching the web"); b.add("Neo", "found it")
check("box: a Neo line clears the working line", b.snapshot()["working"] == "")
_n = len(b.snapshot()["lines"])
b.flash("Stealth on.", 3)
s = b.snapshot()
check("box: a flash shows without focus and schedules its own hide", s["visible"] and not s["focus"] and b.hide_at > 0)
check("box: a flash is a banner, NOT a line in the thread", len(s["lines"]) == _n and s["notice"] == "Stealth on.")
b.show(); check("box: showing the box clears the banner", b.snapshot()["notice"] == "")
b.show(focus=True); s = b.snapshot()
check("box: show focuses once, then the flag clears", s["focus"] is True and b.snapshot()["focus"] is False)
for i in range(80):
    b.add("You", str(i))
check("box: the thread is capped", len(b.snapshot()["lines"]) <= 60)

# ---- the guarantees, in the code that has to keep them ----
src = open("neo.py").read()
check("neo: _speak in stealth prints to the box and returns before any audio",
      "if self._stealth_on():" in src.split("def _speak(self, text):")[1].split("text = clean_for_speech")[0]
      and "_stealth.box.add(\"Neo\", text)" in src)
check("neo: no live conversation opens in stealth", "and not self._stealth_on())" in src.split("def _live_default")[1].split("def _audio_loop")[0])
check("neo: fillers never play in stealth", "if self._stealth_on():\n                return" in src.split("def _ack_after")[1].split("def _ack_done")[0])
check("neo: a typed line enters the same chain as a spoken one",
      'elif kind == "text":' in src and "self._handle_text(text, typed=True)" in src.split('elif kind == "text":')[1][:1600] and "def _handle_text(self, text, typed=False):" in src)
check("neo: the held path calls the same chain", "self._handle_text(text)" in src.split("def _handle(self, audio):")[1].split("def _handle_text")[0])
check("neo: a whisper in stealth gets the bedtime mic gain", "or (getattr(self, \"stealth\", None) is not None and self.stealth.active())" in src)
check("neo: the HUD tab stays down in stealth", "or self._stealth_on():" in src.split("def _arm_turn_hud")[1].split("def _live_step")[0])
check("neo: 'go stealth' out loud is obeyed on the transcript, muted from the model",
      "_st.wants_on(text) and not self._stealth_on()" in src and "session.mute_turn()" in src.split("_st.wants_on(text)")[1][:400])
check("neo: the box is built on the main thread like the panel", "_stealth_mod.Chat()" in src)
_press = src.split("if self._stealth_on():\n            # A press in stealth opens the box")[1].split("return")[0]
check("neo: a press in stealth opens the box and NEVER opens the mic",
      "_stealth.box.show(focus=True)" in _press and '("start", None)' not in _press
      and "_stealth_hold_check" not in src)
check("neo: a release in stealth records nothing", "return                       # the box is up; nothing was recorded" in src)
check("neo: the island is not lit for a typed turn",
      'self.state.set("thinking")' not in src.split('elif kind == "text":')[1].split("else:\n                        self._handle(payload)")[0])

# ---- adding to a question in flight, with a bare Neo ----
import queue as _q, threading as _th, neo as _neo
n = _neo.Neo.__new__(_neo.Neo)
n._jobs = _q.Queue(); n.state = _neo.State(); n._stealth_turn = None
n.stealth = stealth.Mode(os.path.join(tempfile.mkdtemp(), "s.json")); n.stealth.set(True)
stealth.box.lines.clear()
n._stealth_send("what's apple at")
check("inflight: a line with nothing running is simply queued", n._jobs.get_nowait() == ("text", "what's apple at"))
n._stealth_turn = {"text": "what's apple at", "superseded": False, "done": False}
n._stealth_send("and microsoft")
check("inflight: a line typed while thinking supersedes the running turn and is queued",
      n._stealth_turn["superseded"] and n._jobs.get_nowait() == ("text", "and microsoft"))
_before = len(stealth.box.lines)
n._speak("Apple is at $332.")
check("inflight: the stale reply is dropped, not shown", len(stealth.box.lines) == _before)
n._stealth_turn = {"text": "what's apple at", "superseded": False, "done": False}
n._stealth_send("stop")
check("inflight: 'stop' cancels and queues nothing", n._stealth_turn.get("cancelled") and n._jobs.empty())
n._stealth_turn = {"text": "x", "superseded": False, "done": True}
n._speak("Done.")
check("inflight: a finished turn's reply shows normally", stealth.box.lines[-1]["text"] == "Done." and stealth.box.lines[-1]["who"] == "Neo")
check("worker: superseded text is carried into the next question", 'carry = ([prev["text"]] if prev and prev.get("superseded")' in src)
check("neo: replies in stealth are asked for in writing (digits, no filler)",
      "[STEALTH: they are reading your reply" in src and "$332.68" in src)
check("box: Esc works from any app (global key monitor), not just the field",
      "addGlobalMonitorForEventsMatchingMask_handler_" in open("stealth.py").read())
web = open("webdrive.py").read()
check("browser: never pops a sign-in window in stealth", "if _stealth_on():" in web.split("def show_for_login")[1][:300])
html = stealth._HTML
check("box: return sends, shift-return doesn't, escape closes",
      "e.key==='Enter' && !e.shiftKey" in html and "e.key==='Escape'" in html)
check("box: top-right, glass, small", "sf.size.width - W - 6" in open("stealth.py").read() and "backdrop-filter:blur" in html)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED:")
    for f in FAILED:
        print("  -", f)
    sys.exit(1)
print("Stealth clean.")

check("neo: a typed line skips the keyword shortcuts and goes to the model with tools",
      "def _handle_text(self, text, typed=False):" in src and "if not typed:" in src
      and "self._handle_text(text, typed=True)" in src)
check("rules: the model is told, up front, that other people's calendars are out of reach",
      "OTHER PEOPLE'S calendars" in open("context.py").read())
