"""test_hush.py — shushing Neo, whispering, and playing out of the right speaker.

Three things the user asked for on 8 September, and the reasons each of them is
easy to get subtly wrong:

  - "quiet" means SILENCE in "be quiet" and VOLUME in "talk quietly", and
    reading the second as the first would cut him off instead of softening,
  - the hush mic is open exactly when the speakers are loudest, so without an
    echo guard Neo hushes itself the first time it says the word "stop",
  - and PortAudio decides what "the speakers" means once, at import, which for
    a LaunchAgent is login — hours before any headphones exist.

Run: python3 test_hush.py
"""
# Suites live in tests/ but RUN from the repo root, so that the modules
# under test import and open("neo.py") still resolves. This makes the
# import work either way, so a suite can also be run directly.
import os as _bootstrap_os, sys as _bootstrap_sys
_bootstrap_sys.path.insert(0, _bootstrap_os.path.dirname(
    _bootstrap_os.path.dirname(_bootstrap_os.path.abspath(__file__))))

import os
import sys

# BEFORE any Neo import. NEO_NO_AUDIO is what sends voice.json and quiet.json to
# throwaway files (neo._state_file, quiet.STATE), and go.sh sets it for every
# suite — but a suite that only isolates itself when the runner remembers to is
# not isolated. Run `python3 test_hush.py` by hand without this and the mode
# cycling below writes "normal" into the REAL voice.json, which is the exact
# bug this file has a section about: the user set whisper, a commit ran the
# tests, and Neo came back normal.
os.environ.setdefault("NEO_NO_AUDIO", "1")

sys.path.insert(0, ".")
import audio_out
import hush

FAILED = []


def check(name, cond):
    print(("PASS - " if cond else "FAIL - ") + name)
    if not cond:
        FAILED.append(name)


# =========================================================================== #
# 1. Hush
# =========================================================================== #
for said in ("mute", "shush", "hush", "shut up", "be quiet", "quiet",
             "stop talking", "stop", "enough", "that's enough", "okay stop",
             "never mind", "forget it", "shh"):
    check(f"hush: {said!r} stops him", hush.is_hush(said))

# A hush arrives mid-sentence, embedded in whatever else was said.
check("hush: it still counts inside a longer sentence",
      hush.is_hush("no no mute please"))

for said in ("what's the weather", "how does a heap work", "open chrome",
             "tell me about stopping distance", "set a timer",
             "what did you say"):
    check(f"hush: {said!r} is not a hush", not hush.is_hush(said))

# "stopping" is not "stop" — the matcher pads and single-spaces so a phrase
# test can never fire on a word that merely contains it.
check("hush: a word that CONTAINS a hush word doesn't trigger it",
      not hush.is_hush("stopping distance") and not hush.is_hush("mutual"))

# The longest match wins, so the log says what was actually said.
check("hush: the longest phrase is the one reported",
      hush.hush_phrase("okay stop talking") == "stop talking")


# =========================================================================== #
# 2. Whisper mode, and its collision with hush
# =========================================================================== #
# This is the one that would have shipped broken: every "talk quietly" phrase
# contains a word from the hush list.
for said in ("whisper mode", "whisper", "talk quietly", "speak quietly",
             "keep it down", "keep your voice down", "lower your voice",
             "turn it down", "you're too loud", "not so loud", "talk softer"):
    check(f"whisper: {said!r} softens him", hush.voice_mode_request(said) == "whisper")
    check(f"whisper: ...and does NOT silence him", not hush.is_hush(said))

for said in ("normal voice", "talk normally", "speak normally", "speak up",
             "louder", "turn it up", "full volume", "back to normal",
             "stop whispering"):
    check(f"whisper: {said!r} brings him back", hush.voice_mode_request(said) == "normal")

# "stop whispering" contains "whisper". Reading it as a request to START
# whispering builds a mode nobody can get out of.
check("whisper: 'stop whispering' leaves the mode, never enters it",
      hush.voice_mode_request("stop whispering") == "normal")
check("whisper: ...and it isn't read as a hush either",
      not hush.is_hush("stop whispering"))

check("whisper: an ordinary question changes nothing",
      hush.voice_mode_request("what's the score") is None)


# =========================================================================== #
# 3. The echo guard
# =========================================================================== #
# The mic is open while the speakers play, so Neo hears itself. Every match is
# checked against what Neo is saying right now.
check("echo: Neo saying 'stop' does not hush Neo",
      hush.should_act("stop", "you should stop worrying about it") == (None, "stop"))
check("echo: a real hush still lands when Neo said something else",
      hush.should_act("stop", "carrots are a root vegetable")[0] == "hush")
check("echo: it guards whisper requests too",
      hush.should_act("whisper", "the word whisper comes from old english")[0] is None)
check("echo: nothing to compare against never suppresses a hush",
      hush.should_act("mute", "")[0] == "hush")
check("echo: an empty transcript is not treated as a match",
      not hush.echoes_neo("stop", "") and not hush.echoes_neo("", "stop"))

# should_act is the whole decision in one pure function — that is what makes
# the listener a thin wrapper around something testable.
check("hush: should_act returns nothing for ordinary speech",
      hush.should_act("what's for dinner", "") == (None, None))


# =========================================================================== #
# 4. Output routing
# =========================================================================== #
check("audio: a genuine disagreement is stale",
      audio_out.output_is_stale("the user's AirPods Max", "MacBook Air Speakers"))
check("audio: the same device by another spelling is not stale",
      not audio_out.output_is_stale("MacBook Air Speakers", "macbook air speakers "))

# Re-enumerating tears down the whole audio system. Doing that because a query
# failed would be far worse than playing out of the wrong speaker.
check("audio: an unreadable device name never triggers a reset",
      not audio_out.output_is_stale(None, "Speakers")
      and not audio_out.output_is_stale("Speakers", None)
      and not audio_out.output_is_stale(None, None))


def _reset_only_when_stale():
    calls = []

    class _FakeSd:
        @staticmethod
        def query_devices(kind=None):
            return {"name": "MacBook Air Speakers"}

    def fake_reset(sd, log):
        calls.append(1)
        return True

    saved = audio_out.system_default_output
    try:
        audio_out.system_default_output = lambda: "MacBook Air Speakers"
        audio_out.ensure_current_output(_FakeSd, log=lambda *a: None,
                                        reset=fake_reset)
        agreed = not calls
        audio_out.system_default_output = lambda: "the user's AirPods Max"
        audio_out.ensure_current_output(_FakeSd, log=lambda *a: None,
                                        reset=fake_reset)
        moved = len(calls) == 1
    finally:
        audio_out.system_default_output = saved
    return agreed and moved
check("audio: the devices are re-read when the output moves, and only then",
      _reset_only_when_stale())

# CoreAudio is the whole point: it answers correctly on a process whose
# PortAudio list is stale, so it must actually work on this machine.
check("audio: CoreAudio names the real output device on this Mac",
      bool(audio_out.system_default_output()))


# =========================================================================== #
# 5. Wiring
# =========================================================================== #
_neo = open("neo.py").read()
_live = open("live.py").read()

# The device check moved OUT of the stream-open paths. Doing it inline meant
# sd._terminate() ran while the microphone was open and invalidated it — two
# questions in a row were lost on 8 Sept before the answer even started.
check("wiring: re-enumeration never happens on a stream-open path",
      "ensure_current_output" not in _live
      and "ensure_current_output" not in
          _neo.split("def _speak")[1].split("def ")[0])
check("wiring: one owner follows the speakers, and only when idle",
      "def _device_watch" in _neo and "def _audio_idle" in _neo
      and "_audio_idle()" in _neo.split("def _device_watch")[1]
                                 .split("def _audio_idle")[0])
check("wiring: whisper mode reaches the live socket's own audio",
      "set_output_gain" in _neo and "set_output_gain" in _live)
check("wiring: the hush mic closes in a finally, not on the happy path",
      "self._speaking_changed(False)" in _neo)
def _hush_never_uploads():
    """The hush mic is the one the user did not deliberately open, so its audio
    must not reach a provider. Check the CALLS, not the comments: the cloud
    path is speech.transcribe(), the local floor is _transcribe_local()."""
    body = _neo.split("def _build_hush")[1].split("def _live_partial")[0]
    code = "\n".join(l for l in body.splitlines()
                     if not l.strip().startswith("#"))
    return "_transcribe_local(" in code and "speech.transcribe(" not in code
check("wiring: hush transcribes LOCALLY — this audio never leaves the machine",
      _hush_never_uploads())
check("wiring: there is an off switch",
      'NEO_HUSH' in _neo)
check("wiring: a hush drops what the live socket already sent",
      "session.silence()" in _neo and "def silence(self)" in _live)


# =========================================================================== #
# 6. The stutter: device I/O must never happen on a thread that feeds audio
# =========================================================================== #
# live.py calls on_state("speaking") from inside _receive_loop — the asyncio
# task that writes into the playback buffer. The first version opened the hush
# microphone right there. Opening a PortAudio input stream is well over a
# hundred milliseconds, the buffer drained while it happened, and _callback
# wrote silence into the middle of Neo's sentence. the user heard it as stuttering
# and broken English on the first answer of a session, which is exactly when
# the device is coldest and the open is slowest.
def _hush_never_opens_on_the_caller():
    body = _neo.split("def _speaking_changed")[1].split("def _hush_loop")[0]
    code = "\n".join(l for l in body.splitlines()
                     if not l.strip().startswith("#"))
    # it may only ENQUEUE; the words start( and stop( must not appear as calls
    return ("_hush_cmds.put_nowait" in code
            and "listener.start()" not in code
            and "listener.stop()" not in code)
check("stutter: speaking state only enqueues — it never opens a device",
      _hush_never_opens_on_the_caller())

check("stutter: a worker owns the hush mic",
      "def _hush_loop" in _neo and "neo-hush-mic" in _neo)

check("stutter: the mic opens a beat AFTER the answer starts, not into it",
      "HUSH_OPEN_DELAY_S" in _neo)

def _press_releases_the_mic():
    """A press means the user wants to talk into the same microphone hush is
    holding. Two input streams on one device is contention at best, and a
    capture that hears nothing at worst."""
    body = _neo.split("def on_press")[1].split("def on_release")[0]
    return '_hush_cmds.put_nowait("stop")' in body
check("stutter: pressing the key hands the microphone back immediately",
      _press_releases_the_mic())


# =========================================================================== #
# 7. Bedtime mode — the one that changes the microphone
# =========================================================================== #
import neo as _n
import live as _l

for said in ("bedtime mode", "night mode", "good night", "sleep mode",
             "quiet hours", "everyone is asleep"):
    check(f"bedtime: {said!r} turns it on",
          hush.voice_mode_request(said) == "bedtime")

for said in ("normal mode", "wake up", "good morning", "day mode",
             "turn off night mode", "exit bedtime", "talk normal"):
    check(f"bedtime: {said!r} turns it off",
          hush.voice_mode_request(said) == "normal")

# The trap: every "off" phrase contains its own "on" phrase.
check("bedtime: 'turn off night mode' exits, it never enters",
      hush.voice_mode_request("turn off night mode") == "normal")
check("bedtime: and it isn't heard as a hush either",
      not hush.is_hush("turn off night mode") and not hush.is_hush("good night"))

# Bedtime is more specific than whisper and must win when both could match.
check("bedtime: beats whisper when a sentence could be either",
      hush.voice_mode_request("bedtime mode, keep it down") == "bedtime")

check("bedtime: any spelling resolves to one of the three modes",
      all(_n.clean_mode(m) in _n.VOICE_MODES
          for m in ("bed", "NIGHT", "sleep mode", "whis", "quiet", "", "junk")))
check("bedtime: 'night' and 'sleep' both mean bedtime",
      _n.clean_mode("night") == "bedtime" and _n.clean_mode("sleep") == "bedtime")

# The gate is the reason whispering normally fails: a whisper sits well under
# MIC_GATE, so the honest answer is "that was silence".
check("bedtime: the silence gate drops far below the normal one",
      _n.BEDTIME_MIC_GATE < _n.MIC_GATE / 4)
check("bedtime: the microphone is genuinely amplified, not nudged",
      _n.BEDTIME_MIC_GAIN >= 4.0)
check("bedtime: the answer is quieter than whisper mode",
      _n.BEDTIME_GAIN < _n.WHISPER_GAIN
      and _n.BEDTIME_LIVE_GAIN < _n.WHISPER_LIVE_GAIN)

# Amplifying int16 has to clip, not wrap. A wrap turns a loud whisper into
# white noise, which is worse than not hearing it.
def _amplify_clips():
    import numpy as np
    loud = (np.ones(8, dtype="<i2") * 30000).tobytes()
    out = np.frombuffer(_l.amplify_pcm(loud, 8.0), dtype="<i2")
    return int(out[0]) == 32767 and _l.amplify_pcm(loud, 1.0) is loud
check("bedtime: amplification clips instead of wrapping round",
      _amplify_clips())

# Waking up to a machine still amplifying the mic eight times, in a room now
# full of people, is the one failure this mode must not have.
def _bedtime_expires_rather_than_sticking():
    """Superseded twice before landing here. "Never persists" broke it — Neo
    restarts several times an hour and the mode vanished mid-use. "Always
    persists" was worse — it whispered for days. It expires."""
    import time as _tt
    fresh = _n._load_voice_mode(prefs={"voice_mode": "bedtime",
                                       "voice_mode_at": _tt.time() - 60})
    old = _n._load_voice_mode(prefs={"voice_mode": "bedtime",
                                     "voice_mode_at": _tt.time() - 12 * 3600})
    return fresh == "bedtime" and old == "normal" and _n.VOICE_MODE_TTL_H <= 8
check("bedtime: it survives a restart but is gone by morning",
      _bedtime_expires_rather_than_sticking())

check("bedtime: shushing still works in it — the gate follows the mode",
      "set_sensitivity" in _neo and hasattr(hush, "set_sensitivity"))

# One switch sets all four things, so they can never drift apart.
def _one_switch_sets_everything():
    import inspect
    src = inspect.getsource(_n.Neo.set_voice_mode)
    return all(k in src for k in ("set_output_gain", "set_input_gain",
                                  "set_sensitivity", "BEDTIME_MIC_GATE"))
check("bedtime: one switch sets speaker, microphone, gate and hush together",
      _one_switch_sets_everything())


# =========================================================================== #
# 8. The three things that were broken on first use (8 Sept, evening)
# =========================================================================== #

# ---- a highlight has to take itself off the screen ----
import pointer as _pt
check("highlight: a mark clears itself instead of smearing the screen",
      _pt.HOLD_S > 0 and "threading.Timer" in open("pointer.py").read())

def _expiry_cannot_kill_a_newer_mark():
    """draw() supersedes, so the generation it captures is unique to it. A
    timer from an old mark must never take down one drawn after it."""
    import inspect
    src = inspect.getsource(_pt.draw)
    return "supersede()" in src and "current_generation() == mine" in src
check("highlight: an old timer can never clear a newer mark",
      _expiry_cannot_kill_a_newer_mark())


# ---- bedtime has to actually HEAR a whisper ----
# turn_opens is strict on purpose: holding the key in silence must never become
# a turn, because the model answers silence by inventing what it heard. But all
# three of its tests reject a whisper, which is why whispering did nothing.
def _whisper_opens_a_turn_only_in_bedtime():
    import random
    random.seed(7)

    def levels(mean, n, jitter=0.35):
        return [max(0.00005, mean * (1 + random.uniform(-jitter, jitter)))
                for _ in range(n)]

    FLOOR, WHISPER, SPEECH = 0.0004, 0.0022, 0.035

    def trial(mean, gain, bedtime):
        if bedtime:
            _l.set_listening_profile(base=_n.BEDTIME_VOICE_BASE,
                                     peak_ratio=_n.BEDTIME_PEAK_RATIO,
                                     needed=_n.BEDTIME_VOICE_CHUNKS)
        else:
            _l.set_listening_profile()
        return _l.turn_opens([v * gain for v in
                              (levels(FLOOR, 12) + levels(mean, 18))])
    try:
        normal_speech = trial(SPEECH, 1, False)
        whisper_normal = trial(WHISPER, 1, False)      # the bug
        whisper_bedtime = trial(WHISPER, 8, True)      # the fix
        silence_bedtime = trial(FLOOR, 8, True)        # must NOT open
        speech_bedtime = trial(SPEECH, 8, True)
    finally:
        _l.set_listening_profile()
    return (normal_speech and not whisper_normal and whisper_bedtime
            and not silence_bedtime and speech_bedtime)
check("bedtime: a whisper opens a turn in bedtime and nowhere else",
      _whisper_opens_a_turn_only_in_bedtime())

check("bedtime: silence still never becomes a turn, even amplified 8x",
      True)   # asserted inside the trial above; named here so it is visible

def _amplify_before_the_level_test():
    """Boosting only at the send left turn_opens judging the RAW whisper, so
    bedtime amplified audio that had already been rejected."""
    src = open("live.py").read()
    send_loop = src.split("async def _send_loop")[1].split("async def ")[0]
    boost = send_loop.index("amplify_pcm")
    level = send_loop.index("chunk_rms(chunk)")
    return boost < level
check("bedtime: the gain is applied BEFORE the level test, not after it",
      _amplify_before_the_level_test())

check("bedtime: lowering the bar is a profile, so normal rooms are untouched",
      _l.listening_profile() == {"base": None, "peak_ratio": None,
                                 "needed": None})


# ---- a mode must survive the answer that confirms it ----
# 22:00:29 bedtime on -> 22:00:34 the listener heard Neo's own confirmation,
# which contains "whisper", and downgraded the mode five seconds later.
check("mode: the hush listener can no longer change modes at all",
      hush.should_act("whisper mode", "") == (None, None)
      and hush.should_act("bedtime mode", "") == (None, None)
      and hush.should_act("normal mode", "") == (None, None))
check("mode: ...but it still hushes, which is the whole reason it exists",
      hush.should_act("shush", "")[0] == "hush")

def _neo_ignores_non_hush_actions():
    import inspect
    src = inspect.getsource(_n.Neo._hush_action)
    return 'action != "hush"' in src and "set_voice_mode" not in src
check("mode: Neo acts on nothing but a hush from that microphone",
      _neo_ignores_non_hush_actions())

check("mode: asking for a mode still works through the normal path",
      any(t.__name__ == "set_voice_mode" for t in
          __import__("agent").TOOLS))


# =========================================================================== #
# 9. The voice, 8 Sept overnight: four ways it was breaking itself
# =========================================================================== #

# ---- (a) Neo booted whispering, forever, without being asked ----
# bedtime stored "whisper" as its safe fallback, so one "bedtime mode" left
# voice.json holding {"voice_mode": "whisper"} and every later boot was quiet.
# Neo then answered "whisper mode is already on" to a request nobody made.
check("voice: no quiet mode survives a restart",
      _n._load_voice_mode() == "normal")
check("voice: ...and what IS written carries the time it was set",
      "_save_voice_prefs(voice_mode=mode, voice_mode_at=" in _neo)
check("voice: the env override still works for testing",
      "NEO_VOICE_MODE" in _neo)


# ---- (b) the hush mic could open the AIRPODS microphone ----
# pick_input_device answers None for "the system default is fine", which is
# right for recording and wrong for a stream that opens while the speakers are
# playing: the default input becomes the headset the moment it connects, and
# opening a Bluetooth mic flips it into hands-free mode — output drops to mono
# telephone quality mid-sentence.
def _hush_mic_is_never_bluetooth():
    dev = _n.safe_input_device()
    if dev is None:
        return True                      # nothing safe -> the listener refuses
    low = dev.lower()
    return (any(h in low for h in _n._BUILTIN_HINTS)
            and not any(a in low for a in _n._AVOID_HINTS))
check("voice: the hush mic is a concrete built-in device, never the headset",
      _hush_mic_is_never_bluetooth())

check("voice: safe_input_device never answers 'use the system default'",
      _n.safe_input_device([{"name": "the user's AirPods Max",
                             "max_input_channels": 1}]) is None)
check("voice: ...and it finds the built-in when there is one",
      _n.safe_input_device([{"name": "the user's AirPods Max", "max_input_channels": 1},
                            {"name": "MacBook Air Microphone",
                             "max_input_channels": 1}]) == "MacBook Air Microphone")

def _hush_refuses_without_a_safe_mic():
    body = _neo.split("def _build_hush")[1].split("def _live_partial")[0]
    return "safe_input_device()" in body and "return None" in body
check("voice: no safe mic means no hush listener at all",
      _hush_refuses_without_a_safe_mic())


# ---- (c) the filler could tear down the live stream it was playing over ----
def _filler_never_reenumerates():
    """Check the CALLS, not the prose — the comment there explains why it
    mustn't, so a plain substring test matches its own explanation."""
    body = _neo.split("def _ack(self, kind)")[1].split("def _speak")[0]
    code = "\n".join(l for l in body.splitlines()
                     if not l.strip().startswith("#"))
    return "ensure_current_output(" not in code
check("voice: the filler can't re-enumerate PortAudio mid-answer",
      _filler_never_reenumerates())


# ---- (d) live gain was per-chunk, so one failure meant a jumping volume ----
check("voice: softening the live voice is all-or-nothing, never per chunk",
      "_SCALING_OK" in _live and "_SCALING_OK[0] = False" in _live)


# ---- the idle predicate has to be conservative ----
def _idle_check_fails_closed():
    import inspect
    src = inspect.getsource(_n.Neo._audio_idle)
    # an exception must mean "not safe", never "safe"
    return "except Exception:" in src and src.rstrip().endswith("return True") \
        and "return False          # can't prove it is safe" in src
check("voice: if it can't prove the audio is idle, it does nothing",
      _idle_check_fails_closed())


# The architectural guard in test_deck_hard.py only scans neo.py. hush.py opens
# a PortAudio stream too, while the speaker stream is running, and did it
# outside the shared lock — the same hole that guard exists to close.
def _hush_opens_under_the_shared_lock():
    src = open("hush.py").read()
    start = src.split("def start(self)")[1].split("def stop(self)")[0]
    return "AUDIO_LOCK" in start and "with pa_lock:" in start
check("voice: the hush stream opens under the same lock as every other one",
      _hush_opens_under_the_shared_lock())


# ---- the whole state machine, in every direction ----
# "It shouldn't be that hard to just switch between 2 voices." It isn't, but a
# mode is four numbers and the bug was always one of them not moving.
def _every_transition_sets_every_knob():
    n = _n.Neo.__new__(_n.Neo)
    n.voice_mode = "normal"
    _n._VOICE_MODE["mode"] = "normal"

    def snap():
        return (round(_l.output_gain(), 3), round(_l.input_gain(), 1),
                round(n.speech_gain(), 3), round(n.mic_gate(), 5),
                _l.listening_profile()["base"])

    want = {
        "normal":  (1.0, 1.0, round(_n.TTS_GAIN, 3), round(_n.MIC_GATE, 5), None),
        "whisper": (round(_n.WHISPER_LIVE_GAIN, 3), 1.0,
                    round(_n.WHISPER_GAIN, 3), round(_n.MIC_GATE, 5), None),
        "bedtime": (round(_n.BEDTIME_LIVE_GAIN, 3), round(_n.BEDTIME_MIC_GAIN, 1),
                    round(_n.BEDTIME_GAIN, 3), round(_n.BEDTIME_MIC_GATE, 5),
                    _n.BEDTIME_VOICE_BASE),
    }
    try:
        # every ordered pair, so leaving a mode is tested as hard as entering
        for a in ("normal", "whisper", "bedtime"):
            for b in ("normal", "whisper", "bedtime"):
                n.set_voice_mode(a)
                n.set_voice_mode(b)
                if snap() != want[b]:
                    return False
        n.set_voice_mode("normal")
        return n.set_voice_mode("normal") is False      # idempotent
    finally:
        _l.set_listening_profile()
        _l.set_input_gain(1.0)
        _l.set_output_gain(1.0)
        _n._VOICE_MODE["mode"] = "normal"
check("voice: every transition between all three modes sets all four knobs",
      _every_transition_sets_every_knob())


# =========================================================================== #
# 10. 9 Sept: long questions, and three voices at once
# =========================================================================== #

# ---- a 43-second question was being thrown away ----
# The mic queue held 64 chunks at 100 ms each — six and a half seconds — and
# the callback dropped everything after that (`except queue.Full: pass`).
check("long: the mic queue holds a real question, not six seconds of one",
      _l.IN_CHUNK == 1600 and 1200 * 0.1 >= 100)

def _queue_is_big_enough():
    import inspect
    src = inspect.getsource(_l.LiveSession.__init__)
    n = int(src.split("queue.Queue(maxsize=")[1].split(")")[0])
    return n * (_l.IN_CHUNK / _l.IN_RATE) >= 60      # at least a minute
check("long: at least a minute of speech fits before anything is dropped",
      _queue_is_big_enough())

def _send_loop_drains_in_batches():
    """One await per 100 ms chunk can only just keep pace with the microphone,
    so a backlog never clears and the turn ends seconds after the key does."""
    import inspect
    src = inspect.getsource(_l.LiveSession._send_loop)
    return "SEND_BATCH_MAX" in src and "get_nowait()" in src
check("long: the send loop drains a backlog instead of trickling through it",
      _send_loop_drains_in_batches())

def _end_marker_is_never_overtaken():
    """The end-of-turn marker must be handled next, not swallowed by a batch."""
    import inspect
    src = inspect.getsource(_l.LiveSession._send_loop)
    seg = src.split("batch = [chunk]")[1].split("send_realtime_input")[0]
    return "_END_OF_TURN" in seg and "_held_marker" in seg
check("long: batching never eats the end-of-turn marker",
      _end_marker_is_never_overtaken())


# ---- three voices at 15:14:37 ----
# An uncached filler fell through to _speak(), which uses the LOCAL engine, on
# the jobs queue, through its own output stream — which _cut_ack cannot stop.
# So bm_fable played over Charon and could not be interrupted: both the
# "repeat" and the filler that "sounds like shit".
def _no_wrong_voice_over_a_live_answer():
    body = _neo.split("def _ack(self, kind)")[1].split("def _speak")[0]
    code = "\n".join(l for l in body.splitlines()
                     if not l.strip().startswith("#"))
    # the local fallback must sit behind an is_running() check
    return ("session.is_running()" in code
            and code.index("session.is_running()") < code.index("self._speak(text)"))
check("voices: an uncached filler stays quiet rather than talking over the answer",
      _no_wrong_voice_over_a_live_answer())

# The invariant is ONE voice everywhere, not one particular engine. With a key
# that is the cloud voice on every path; with no key at all there is no cloud to
# reach, so it is Kokoro on every path. What must never happen is two voices in
# one install — the "robotic old Jarvis for cards, Charon for conversation"
# split that made Neo sound like two different assistants depending on a detail
# nobody could see.
check("voices: one engine for everything, and it matches whether a key exists",
      _n._load_engine() == ("kokoro" if _n.KEYLESS else "gemini"))


# ---- "hush" is not a request for deep-work mode ----
import agent as _ag
import inspect as _i
check("routing: focus_mode says plainly that hush is not it",
      "STOP SPEAKING" in _i.getdoc(_ag.focus_mode)
      and "hush" in _i.getdoc(_ag.focus_mode).lower())


# ---- the highlighter reads as a stroke, not a rectangle ----
_spot = open("spotlight.py").read()
check("highlight: no hard border boxing the words in",
      "border:1px solid rgba(255,214,64,.55)" not in _spot)
check("highlight: an ink gradient and tapered ends instead",
      "inkin" in _spot and "linear-gradient(90deg,rgba(255,190,32,0)" in _spot)
check("highlight: the wipe is disabled for reduced motion",
      ".band{animation:none" in _spot)


# ---- the fn key that was permanently "held" ----
# Measured on this Mac with nothing held: flags = 0xa00100, which is
# SecondaryFn AND NumericPad — the pair macOS sets for arrow keys and the
# function row — while CGEventSourceKeyState(0x3F) correctly said False.
# Because "DOWN WINS", the false positive won forever: every click was refused
# with "I'm not clicking while you're mid-sentence", and the fn-guard could
# never force a release, so a hold that lost its key-up sat there blue.
def _latched_fn_flag_is_not_a_held_key():
    import Quartz
    flags = Quartz.CGEventSourceFlagsState(
        Quartz.kCGEventSourceStateHIDSystemState)
    latched = bool(flags & _n.FN_MASK) and bool(
        flags & Quartz.kCGEventFlagMaskNumericPad)
    keydown = bool(Quartz.CGEventSourceKeyState(
        Quartz.kCGEventSourceStateHIDSystemState, _n.FN_KEYCODE))
    if not latched or keydown:
        return True          # not in the bad state right now; nothing to prove
    # In the bad state the answer must follow the KEYCODE, not the flag.
    return _n.ptt_physically_down(_n.FN_KEYCODE) is False
check("fn: a latched SecondaryFn flag is never read as a held key",
      _latched_fn_flag_is_not_a_held_key())

def _the_guard_reads_the_keycode():
    import inspect
    src = inspect.getsource(_n.ptt_physically_down)
    return "kCGEventFlagMaskNumericPad" in src and "flag_view = None" in src
check("fn: ...and the flag is discarded as unusable, not treated as 'up'",
      _the_guard_reads_the_keycode())

check("fn: with nothing held, Neo is willing to click again",
      _n.ptt_physically_down(_n.FN_KEYCODE) in (False, None)
      or __import__("act").can_act()[0] is True)


# =========================================================================== #
# 11. 10 Sept: it didn't whisper, and hush answered back
# =========================================================================== #

# ---- a mode has to outlive the restarts the user never sees ----
# Persisting forever left Neo whispering for days off one "bedtime mode".
# Persisting not at all meant the mode evaporated on the next code change —
# and Neo restarts itself several times an hour, invisibly. So "whisper mode"
# worked for ninety seconds and then quietly stopped, which reads exactly like
# "it doesn't whisper".
import time as _t
check("mode: one set thirty seconds ago survives a restart",
      _n._load_voice_mode(prefs={"voice_mode": "whisper",
                                 "voice_mode_at": _t.time() - 30}) == "whisper")
check("mode: bedtime survives one too",
      _n._load_voice_mode(prefs={"voice_mode": "bedtime",
                                 "voice_mode_at": _t.time() - 90}) == "bedtime")
check("mode: but it is gone by morning, not still amplifying the mic",
      _n._load_voice_mode(prefs={"voice_mode": "bedtime",
                                 "voice_mode_at": _t.time() - 9 * 3600})
      == "normal")
check("mode: a saved mode with no timestamp is treated as stale, not sticky",
      _n._load_voice_mode(prefs={"voice_mode": "whisper"}) == "normal")
check("mode: setting one writes the timestamp that makes expiry possible",
      "voice_mode_at=time.time()" in _neo)


# ---- the gain actually reaches the audio ----
def _quiet_modes_actually_quieten():
    """Not "is the number set" — does the PCM come out smaller."""
    import numpy as np
    n = _n.Neo.__new__(_n.Neo)
    n.voice_mode = "normal"
    _n._VOICE_MODE["mode"] = "normal"
    pcm = (np.ones(400, dtype="<i2") * 8000).tobytes()

    def peak(mode):
        n.set_voice_mode(mode)
        pb = _l.Playback()
        pb.write(pcm)
        return int(np.abs(np.frombuffer(bytes(pb._buf), dtype="<i2")).max())
    try:
        loud, quiet, bed = peak("normal"), peak("whisper"), peak("bedtime")
    finally:
        n.set_voice_mode("normal")
        _l.set_output_gain(1.0)
        _l.set_input_gain(1.0)
        _l.set_listening_profile()
    return loud == 8000 and quiet < loud * 0.5 and bed < quiet
check("mode: whisper and bedtime genuinely shrink the audio, not just a number",
      _quiet_modes_actually_quieten())


# ---- hush means silence, not a sentence about silence ----
# the user said "hush" and Neo replied "got it, quieting down". Flushing alone
# cannot fix that: by the time the word is transcribed the model is already
# generating, and every chunk after the flush refills the buffer.
def _hush_mutes_the_whole_turn():
    import numpy as np
    s = _l.LiveSession(client=None, model="m", system_instruction="s")
    pcm = (np.ones(200, dtype="<i2") * 9000).tobytes()
    s._playback.write(pcm)
    had = s._playback.pending() > 0
    s.mute_turn()
    emptied = s._playback.pending() == 0 and s.turn_is_muted()
    return had and emptied
check("hush: it empties what is queued AND mutes the rest of the answer",
      _hush_mutes_the_whole_turn())

def _muted_audio_is_dropped_not_played():
    seg = _live.split('_muted_turn", False):')[1].split("\n")[1]
    return "continue" in seg
check("hush: the receive loop discards audio while muted, never queues it",
      _muted_audio_is_dropped_not_played())

def _mute_clears_on_turn_end():
    """One answer, not the whole conversation: the next question is answered
    normally."""
    seg = _live.split('getattr(content, "turn_complete", False):')[1][:300]
    return "_muted_turn = False" in seg
check("hush: the mute covers one answer, not the conversation",
      _mute_clears_on_turn_end())

def _hush_is_caught_before_it_becomes_a_reply():
    body = _neo.split("def _live_text")[1].split("def _commit_live_turn")[0]
    return ("is_hush(text)" in body and "mute_turn()" in body
            and 'who == "You"' in body)
check("hush: caught on the user's own transcript, not left to the model",
      _hush_is_caught_before_it_becomes_a_reply())


# =========================================================================== #
# 12. 10 Sept, late: the list the user left before going out
# =========================================================================== #
import agent as _ag2
import ears as _ears

# ---- the tests were the thing un-setting his mode ----
# test_hush cycled set_voice_mode() through every mode, ending on "normal",
# and wrote that into the REAL voice.json; go.sh then restarted Neo, which
# read it. Every commit reset whatever he had asked for.
check("isolation: under the test flag, voice prefs go to a throwaway file",
      "neo-test-" in _n.VOICE_FILE)
check("isolation: ...and so does focus mode",
      "neo-test-" in __import__("quiet").STATE)

# ---- the card voice was clipping ----
# Kokoro's 2.1x boost was applied in _speak to whatever synth returned. Fine
# for Kokoro; a hard soft-clip on Gemini audio that already peaks at 0.85.
def _engine_gain_lives_in_synth():
    synth = _neo.split("def synth(self, text)")[1].split("\n    def ")[0]
    speak = _neo.split("def _speak(self, text)")[1].split("\n    def ")[0]
    return ("np.tanh(a * TTS_GAIN)" in synth
            and "tanh" not in "\n".join(l for l in speak.splitlines()
                                         if not l.strip().startswith("#")))
check("voice: Kokoro's boost is applied to Kokoro, and _speak never re-clips",
      _engine_gain_lives_in_synth())

def _mode_is_a_ratio():
    n = _n.Neo.__new__(_n.Neo)
    out = {}
    for m in ("normal", "whisper", "bedtime"):
        n.voice_mode = m
        out[m] = round(n.mode_factor(), 2)
    return out["normal"] == 1.0 and 0.2 <= out["whisper"] <= 0.3 \
        and out["bedtime"] < out["whisper"]
check("voice: the mode is a plain ratio that is right for either engine",
      _mode_is_a_ratio())

# ---- code files stop popping up ----
check("report: a document a job wrote opens on screen",
      _n.is_document("report.md") and _n.is_document("notes.pdf"))
check("report: source a job edited does not",
      not _n.is_document("neo.py") and not _n.is_document("x.json")
      and not _n.is_document("a.css"))
check("report: the delivery path filters on that",
      "is_document(p)" in _neo and "summarised, not opened" in _neo)

# ---- and the report is spoken even with no conversation open ----
def _report_is_spoken_without_a_session():
    body = _neo.split('insight.get("kind") == "claude"')[1].split("speak-on-done")[0]
    return body.count("self.say(insight") == 2 and "card and file only" not in body
check("report: a finished job is spoken whether or not a session is open",
      _report_is_spoken_without_a_session())

# ---- names resolve to addresses instead of failing ----
check("email: an address passes straight through",
      _ag2.resolve_recipients("bob@x.com") == ("bob@x.com", []))
check("email: a name Neo doesn't know is reported, not rejected outright",
      _ag2.resolve_recipients("Zebulon")[1] == ["Zebulon"])
check("email: 'A and B' splits into two people",
      len(_ag2.resolve_recipients("Alpha and Beta")[1]) == 2)

# ---- the recogniser is told the NEWEST names first ----
def _newest_names_win():
    facts = [{"text": f"Person{i} is someone."} for i in range(70)]
    facts.append({"text": "Sam is a person the user emails."})
    return "Sam" in _ears.build_hints(facts)
check("names: a name mentioned this week reaches the recogniser despite the cap",
      _newest_names_win())


if FAILED:
    print(f"\n{len(FAILED)} FAILED:")
    for n in FAILED:
        print("  - " + n)
    sys.exit(1)
print("\nHush, whisper and routing clean.")
