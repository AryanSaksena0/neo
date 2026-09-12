"""
live.py — how Neo talks. Native speech-to-speech over one socket.

This is the DEFAULT path, not a mode. Tap the key and you are in a conversation;
it hangs up on its own when you stop. There is nothing to switch into, because
an assistant you have to put into conversational mode isn't one.

The old turn-based pipeline (hold -> record -> Whisper -> Gemini -> Kokoro ->
play) is strictly serial, and neo_metrics.jsonl says what that costs: 6.1s,
6.9s and 65.7s to first sound on the last five real turns. Nothing overlaps, so
even a perfect model and perfect transcription land somewhere around three
seconds and still feel like a walkie-talkie. It stays in the codebase as the
fallback for when this file can't run, and for nothing else.

Mic audio streams up continuously at 16 kHz,
model audio streams back at 24 kHz, and the model decides when you've finished
talking. There is no transcribe step and no synthesis step to wait on, because
the model hears and speaks directly. Barge-in is a message from the server
rather than something Neo fakes with a flag, so talking over Neo actually stops
it mid-word.

What it keeps from the pipeline: Neo's system prompt, Neo's memory, and Neo's
whole toolbox. Live sessions don't auto-execute functions the way chat sessions
do, so tool calls come back as messages and get run here, on a worker thread,
with the result sent back into the same socket.

Design rules, learned from the freeze family in neo.py:
  - Audio device I/O never happens on the event loop.
  - Every teardown path releases the mic, including the ones that raise.
  - A failure to connect is not fatal. Neo says one line, marks itself
    degraded, and every press after that uses the old pipeline — so the worst
    outcome is the behaviour we already had, never a key that does nothing.
"""

import asyncio
import itertools
import json
import re
import os
import queue
import threading
import time

IN_RATE = 16000            # what the Live API accepts: 16-bit PCM, little-endian
OUT_RATE = 24000           # what it returns
IN_CHUNK = 1600            # 100 ms of mic audio per send
# How many 100 ms chunks may go up in one message when the loop is behind.
# Two seconds is enough to clear a backlog quickly without building a blob big
# enough to stall the socket on a slow connection.
SEND_BATCH_MAX = int(os.getenv("NEO_SEND_BATCH", "20"))
# Hang up after this much silence. Long enough to think mid-sentence without
# being cut off; short enough that a forgotten session doesn't hold the mic.
# Hanging up is cheap now — the next session carries the conversation in with
# it — so this is about the mic, not about losing your place.
IDLE_TIMEOUT_S = float(os.getenv("NEO_LIVE_IDLE", "45"))

VOICE = os.getenv("NEO_LIVE_VOICE", "Charon")   # deep and calm, matches bm_fable

# Hold the key to talk, let go and Neo answers. That means Neo — not the model's
# silence detector — decides when a turn ends, so automatic activity detection
# is switched OFF and turn boundaries are sent explicitly (activity_start on the
# first real sound, activity_end the moment the key comes up).
#
# Verified against the real key: activity_end -> first audio back in 942 ms, and
# a 20-SECOND held turn completes normally. There is no server-side limit
# anywhere near the ~5s where holds used to die, which confirms that was always
# a local bug and never the API.
#
# Manual turns also mean a long hold cannot freeze anything: audio just streams
# until you let go. Nothing is waiting for a pause that never comes.
# ---- Is anyone actually talking? -------------------------------------------
# This decides whether a hold becomes a turn at all, and getting it wrong is
# not a small bug: fed silence, the model does not stay quiet, it INVENTS
# speech. Real capture from the log — key held about a second, nothing said:
#
#     [01:00:57] (holding — opening a conversation)
#     [01:00:58] (let go — thinking)
#     [01:00:59] You: ¿Qué es eso?
#
# That "You:" line is the model's own transcription of pure silence. It then
# answered its own hallucination. So the bar for opening a turn has to be
# genuinely reliable, not approximately right.
#
# The first version compared PEAK amplitude against a threshold calibrated for
# RMS, which is why it passed on room tone every time — peak runs far above RMS
# for both speech and noise, so the gate was effectively wide open.
#
# Measured on this machine: normal speech sits around 0.009 RMS, true silence
# near 0.0005. 0.003 sits well clear of both.
# RE-MEASURED on this machine (MacBook Air Microphone, 15 s of an ordinary
# room with nobody talking):
#
#     p10 0.0047   p50 0.0089   p90 0.0132   max 0.0192
#
# The old comment above said "normal speech sits around 0.009, true silence
# near 0.0005", and 0.003 was chosen to sit between them. That calibration is
# simply wrong for a real room: this room's SILENCE has a median of 0.0089 —
# the number the code believed was speech. At a 0.003 gate, 98.7% of silent
# chunks read as voice and the longest unbroken run of "speech" in a silent
# room was 4.7 SECONDS. The gate was not tight, it was wide open, which is why
# tapping fn in a noisy room opens a turn and the model answers a question
# nobody asked by inventing one.
#
# An absolute threshold cannot separate speech from a room, because rooms
# differ by more than speech and room differ. So the real gate is RELATIVE to
# whatever this room is doing right now, with the absolute value as a floor for
# a genuinely quiet one. See gate_for().
VOICE_RMS = float(os.getenv("NEO_MIC_GATE", "0.005"))
# How far above the room's own level a chunk has to sit to count as voice.
NOISE_RATIO = float(os.getenv("NEO_NOISE_RATIO", "2.2"))
# ...and the highest the adaptive gate may ever climb. Without a ceiling a very
# loud room would raise the bar past speech and lock the user out entirely, which
# is a worse failure than the one being fixed: Neo ignoring you looks identical
# to Neo being broken.
NOISE_CEILING = float(os.getenv("NEO_NOISE_CEILING", "0.025"))
# Chunks of this hold needed before the room estimate means anything.
NOISE_MIN_SAMPLES = 8
# A turn opens on VOICE_CHUNKS voiced chunks WITHIN the last VOICE_WINDOW —
# not on a consecutive run. That distinction is the difference between working
# and not: real speech dips to room level between words and on every stop
# consonant, so a "consecutive" requirement long enough to reject noise is also
# long enough to be broken by ordinary speech. Replaying a real recording
# through it, a consecutive-5 rule missed quiet speech entirely while still
# letting a silent hold through.
#
# Tuned by replaying 30 s of this room, plus speech modelled with realistic
# syllabic dips, through the actual gate:
#   taps (0.4 s) that opened a turn:      0 of 99
#   silent 2.5 s holds that opened:       1 of 25
#   speech missed:                        none (opens 0.5-0.9 s in)
# Needing 8 of the last 10 also means a turn cannot open before 0.8 s of audio
# exists at all, so a tap is arithmetically incapable of starting one.
VOICE_WINDOW = int(os.getenv("NEO_VOICE_WINDOW", "10"))
VOICE_CHUNKS = int(os.getenv("NEO_VOICE_CHUNKS", "8"))
# A secondary guard: something in the window has to stand clearly above the
# room, not merely above the gate. It costs nothing in this room and it is what
# stops a steady hum that happens to sit just over the line from reading as a
# sustained sentence.
PEAK_RATIO = float(os.getenv("NEO_PEAK_RATIO", "2.0"))
# Pre-roll kept while we're deciding, so confirming speech half a second in
# doesn't eat the first word. Flushed into the socket the moment the turn
# opens, so a longer decision window costs nothing.
PREROLL_CHUNKS = 14

# Pushed into the mic queue when the key comes up. Everything queued BEFORE it
# is audio the user actually spoke, so the send loop drains all of that, then ends
# the turn. Two bugs died with this sentinel:
#
#   - Latency. The loop polled the queue on a 200 ms timeout and only noticed
#     the release on the next tick, so every single answer carried up to 200 ms
#     of dead time that had nothing to do with the model (measured at 563 ms).
#   - A truncated tail. Closing the mic used to drain the queue, throwing away
#     whatever hadn't been uploaded yet — the last fraction of the sentence,
#     silently, every time.
_END_OF_TURN = object()

# PortAudio's device open is NOT thread-safe, and Neo opens two streams from two
# different threads within milliseconds of each other: the speaker from the
# session thread in _main, and the mic from the worker _open_mic_now spawns when
# the key goes down. Racing them segfaults the interpreter during teardown —
# EXC_BAD_ACCESS inside _PyGC_CollectNoFail — which reads as "I pressed fn and
# Neo died", intermittently, because it is a race. Every device open and close
# goes through this.
_PA_LOCK = threading.RLock()
# The SAME lock, under a public name, because neo.py has to take it too.
#
# PortAudio's open/close path is not thread-safe and a concurrent open segfaults
# the whole process — the crash report is unambiguous:
#     EXC_BAD_ACCESS (SIGSEGV) in libportaudio  Pa_OpenStream
#                                               OpenAndSetupOneAudioUnit
# This lock already guarded live.py's two streams, but it was private, so the
# turn-based path in neo.py — the fallback recorder's mic and the local TTS
# speaker — opened streams with no lock at all. Two paths, one device, no
# mutual exclusion. Both files now serialise on this.
AUDIO_LOCK = _PA_LOCK

# PortAudio enumerates the audio devices ONCE, when it initialises, and never
# looks again. Every device that comes and goes while Neo is running — a
# Continuity iPhone microphone drifting in and out of range, Zoom's virtual
# device appearing when a call starts, AirPods connecting — leaves that cached
# list wrong, and every stream open after that fails against a device index
# that no longer means what PortAudio thinks it means.
#
# Measured, from neo.log: Neo started at 00:10:46 and worked. At 01:29 every
# press failed with
#     PaMacCore (AUHAL) Error line 1332: err='-10851' Invalid Property Value
#     Error opening RawOutputStream: PaErrorCode -9986
# while a FRESH python process on the same machine, at the same moment, opened
# the identical stream without complaint. Nothing was wrong with the hardware.
# The only broken thing was this process's idea of it.
#
# Tearing PortAudio down and bringing it back up is the only way a running
# process re-reads the device list. It is safe precisely when we need it —
# after an open has already failed, with no stream to lose.
_last_reset = [0.0]
RESET_COOLDOWN_S = 5.0


def reset_portaudio(sd, log=print):
    """Re-read the device list. True if a reset actually happened."""
    with _PA_LOCK:
        if time.time() - _last_reset[0] < RESET_COOLDOWN_S:
            return False        # already tried a moment ago; don't thrash
        _last_reset[0] = time.time()
        try:
            sd._terminate()
            sd._initialize()
            log("[audio] re-read the audio devices (something was plugged in "
                "or went away while Neo was running)")
            return True
        except Exception as e:
            log(f"[audio] could not reset the audio system: {e}")
            return False


# --------------------------------------------------------------------------- #
# Pure helpers — tested in test_neo.py.
# --------------------------------------------------------------------------- #
def tool_map(tools):
    """name -> callable, for dispatching what the server asks for. Anything
    without a __name__ is skipped rather than crashing the session."""
    out = {}
    for fn in tools or ():
        name = getattr(fn, "__name__", None)
        if name and callable(fn):
            out[name] = fn
    return out


def call_tool(tools_by_name, name, args, log=print):
    """Run one requested tool and return a JSON-able response dict. A tool that
    raises must come back as a readable error, never as an exception — the
    model can route around "that failed" but not around a dead socket."""
    fn = tools_by_name.get(name)
    if fn is None:
        return {"error": f"no tool named {name}"}
    try:
        result = fn(**(args or {}))
        return {"result": str(result)[:4000]}
    except TypeError as e:
        return {"error": f"bad arguments for {name}: {e}"}
    except Exception as e:
        log(f"[live] tool {name} failed: {e}")
        return {"error": f"{type(e).__name__}: {e}"}


# How long Neo waits for an answer after saying "I'm done" (activity_end)
# before deciding the socket is wedged. Measured first-audio-back on this key is
# ~950 ms; twelve seconds is far past any real answer and far short of the idle
# hang-up, which is what used to swallow this failure in thirty seconds of
# silence that looked exactly like a crash.
# How long a closed turn may produce NOTHING before Neo assumes the socket is
# wedged. Twelve seconds was fine when the prompt was small; it is not now.
# The spoken system prompt is about 6,500 tokens and there are forty tool
# schemas alongside it, so a complex question — "go find the task descriptions
# in my email and work out a plan" — can sit silent for longer than that while
# the model decides what to do. Measured in the log: 13 seconds of silence, and
# the watchdog killed a session that was very likely about to answer.
#
# It is now generous, because the cost of waiting changed: a timeout no longer
# means a silent death. Neo says something and asks the question again (see
# neo.Neo._retry_dead_turn), so being slightly slow to give up is cheap and
# being too quick to give up throws away a real answer.
ANSWER_TIMEOUT_S = float(os.getenv("NEO_ANSWER_TIMEOUT", "28"))

# THE WATCHDOG MUST NOT KILL A MODEL THAT IS THINKING.
#
# A thinking model and a wedged socket look identical from here: both are
# silence. There is no client-side signal that separates them, so guessing
# after twelve seconds — or twenty-eight — is guessing. It killed a real
# question thirteen seconds in that was very likely about to be answered.
#
# So the first threshold no longer ends anything. At ANSWER_TIMEOUT_S Neo says
# it is still working, once, and keeps waiting; silence is never mysterious and
# nothing is thrown away. Only at the HARD ceiling — long past any plausible
# think time — is the socket treated as dead, and even then the turn is
# re-asked rather than dropped.
ANSWER_HARD_S = float(os.getenv("NEO_ANSWER_HARD", "120"))

# ...but a turn that is RUNNING A TOOL gets much longer, because a tool that
# takes a while is not a wedged socket. The log has the cost of not making that
# distinction: the user asked for a presentation, the model called search_web and
# then present, and twelve seconds later the watchdog decided nothing had come
# back and reset the session. They never heard anything at all. The tool was
# working the whole time.
TOOL_ANSWER_TIMEOUT_S = float(os.getenv("NEO_TOOL_TIMEOUT", "60"))


def answer_slow(awaiting_since, now, limit=ANSWER_TIMEOUT_S, in_tool=False):
    """Long enough that the user deserves to be told, but NOT a reason to give up.

    0 means we are not waiting on anything — between turns, and while Neo is
    already speaking, this must never fire.
    """
    if not awaiting_since:
        return False
    return (now - awaiting_since) >= (TOOL_ANSWER_TIMEOUT_S if in_tool else limit)


def answer_overdue(awaiting_since, now, limit=ANSWER_HARD_S, in_tool=False):
    """The socket is DEAD, not thinking. Pure, so it's testable.

    This is the hard ceiling, not the first sign of slowness. A thinking model
    and a wedged socket are both silence, so the only honest way to tell them
    apart is to wait long enough that no amount of thinking explains it.

    `in_tool` says a tool is still running, which can legitimately take minutes.
    """
    if not awaiting_since:
        return False
    ceiling = max(limit, TOOL_ANSWER_TIMEOUT_S) if in_tool else limit
    return (now - awaiting_since) >= ceiling


def should_hang_up(last_activity, now, idle_timeout=IDLE_TIMEOUT_S):
    """A live socket left open forever is a mic left open forever. Close it
    after a stretch of nobody saying anything."""
    return (now - last_activity) >= idle_timeout


# While a tool runs, the model is blocked and says nothing. Measured on this
# key: the model decides to call a tool at ~500 ms, then there is silence for
# however long the tool takes, then the answer. A second of dead air in a spoken
# conversation reads as "it broke", so Neo fills it the way a person would.
# Several per tool, rotated. One fixed string said the same way every time is
# how a person notices they are talking to a machine — the third "One sec,
# looking that up." lands as a recording, not a reply. These are short on
# purpose: a filler exists to cover the gap before the real answer, and one that
# runs long collides with it.
_FILLERS = {
    "search_web":   ["Let me check.", "One sec.", "Looking now.",
                     "Hang on.", "Checking that."],
    "read_webpage": ["Reading it.", "Opening that now.", "One sec."],
    "calculate":    ["Working it out.", "Let me do the maths.", "One sec."],
    "get_weather":  ["Checking.", "One sec."],
    "look_at_screen": ["Let me look.", "Having a look."],
    "hand_to_claude": ["Passing that to Claude.", "Handing it over now."],
    "use_skill":    ["On it.", "Doing that now."],
    # Building a deck takes several seconds — by far the longest wait Neo ever
    # makes them sit through. The filler has to actually say what is happening
    # and that it will take a moment, or the silence reads as a crash and they
    # asks again (which used to make Neo build it all over).
    "present":      ["Yeah, let me put a quick walkthrough together for you —"
                     " give me a sec.",
                     "Good one to see rather than hear. Building it now, one"
                     " moment.",
                     "Let me put this on screen for you — just a few seconds."],
    "close_presentation": [""],
}
_DEFAULT_FILLERS = ["One sec.", "Hang on.", "Just a moment."]
_filler_turn = itertools.count()


def filler_for(names, fillers=None):
    """What to say while these tools run, or "" when there's nothing worth
    saying. One line only — talking over your own answer is worse than silence,
    so a fast tool gets nothing and the caller decides using should_fill()."""
    table = _FILLERS if fillers is None else fillers
    if not names:
        return ""
    spin = next(_filler_turn)
    for n in names:
        if n in table:
            options = table[n]
            if isinstance(options, str):        # a caller passed a flat table
                return options
            return options[spin % len(options)] if options else ""
    return _DEFAULT_FILLERS[spin % len(_DEFAULT_FILLERS)]


# Tools fast enough that a filler would collide with the real answer.
_INSTANT_TOOLS = {"claude_progress", "list_skills", "open_app", "open_website",
                  "press_key", "type_on_keyboard", "open_memory_brain",
                  "get_time", "close_presentation"}


def should_fill(names, instant=None):
    """True when these tools are slow enough to be worth covering."""
    skip = _INSTANT_TOOLS if instant is None else instant
    return bool(names) and not all(n in skip for n in names)


# How many tool calls one turn may make before Neo stops dispatching.
#
# A chat session gets this for free (maximum_remote_calls). A LIVE session does
# not — tool calls arrive as messages and we dispatch them ourselves, so
# "unbounded" was the default and it bit immediately: search_web returned
# something unhelpful, the model called it again, and it ran ~1/second for
# minutes. Neo never spoke, the orb flickered between thinking and listening on
# every call, and the only way out was killing it.
MAX_TOOL_CALLS_PER_TURN = int(os.getenv("NEO_MAX_TOOL_CALLS", "6"))


def chunk_rms(chunk):
    """RMS of one 16-bit PCM chunk, 0..1. RMS, not peak — see VOICE_RMS."""
    import array
    import math
    try:
        samples = array.array("h")
        samples.frombytes(chunk)
    except Exception:
        return 0.0
    if not samples:
        return 0.0
    total = 0
    for s in samples:
        total += s * s
    return math.sqrt(total / len(samples)) / 32768.0


def noise_floor(levels, pctile=0.2, min_samples=NOISE_MIN_SAMPLES):
    """This room's own level, from the chunks seen so far in this hold.

    A low percentile rather than the minimum: one freak-quiet frame should not
    define the room. Returns None until there is enough to be worth trusting,
    and the caller then falls back to the absolute floor. Pure.
    """
    if not levels or len(levels) < min_samples:
        return None
    ordered = sorted(levels)
    return ordered[min(len(ordered) - 1, int(len(ordered) * pctile))]


def gate_for(levels, base=None, ratio=None, ceiling=None):
    """The level a chunk must beat to count as voice, for THIS room. Pure.

    Bounded at both ends on purpose. The floor keeps a silent room from setting
    a gate so low that its own hiss opens turns; the ceiling keeps a loud one
    from setting a gate so high that the user cannot get a word in.
    """
    base = (_LISTEN["base"] or VOICE_RMS) if base is None else base
    ratio = NOISE_RATIO if ratio is None else ratio
    ceiling = NOISE_CEILING if ceiling is None else ceiling
    floor = noise_floor(levels)
    if floor is None:
        return base
    return max(base, min(ceiling, floor * ratio))


def turn_opens(levels, base=None, ratio=None, ceiling=None,
               window=None, needed=None, peak_ratio=None):
    """Has the user actually started talking? `levels` is every chunk level seen
    so far in this hold, oldest first. Pure, so it can be replayed against a
    real recording — which is the only way any of these numbers were chosen.

    Three conditions, and all three have to hold:
      - the bar is set by THIS room (gate_for), not by a constant,
      - most of the last second is over that bar, tolerating the dips that
        every real sentence has between words, and
      - something in there stands clearly above the room.
    """
    window = VOICE_WINDOW if window is None else window
    needed = (_LISTEN["needed"] or VOICE_CHUNKS) if needed is None else needed
    peak_ratio = ((_LISTEN["peak_ratio"] or PEAK_RATIO)
                  if peak_ratio is None else peak_ratio)
    if not levels:
        return False
    recent = levels[-window:]
    # Fewer chunks than we need voiced ones: not enough audio to judge. This is
    # what makes a TAP impossible rather than merely unlikely.
    if len(recent) < needed:
        return False
    gate = gate_for(levels, base, ratio, ceiling)
    if sum(1 for v in recent if v >= gate) < needed:
        return False
    floor = noise_floor(levels)
    if floor is None:
        floor = VOICE_RMS if base is None else base
    return max(recent) >= floor * peak_ratio


def call_signature(name, args):
    """A stable key for 'the model asked for this exact thing again'."""
    try:
        items = sorted((str(k), str(v)) for k, v in (args or {}).items())
    except Exception:
        items = []
    return name + "(" + ",".join(f"{k}={v}" for k, v in items) + ")"


def loop_verdict(signature, seen, budget_used, budget=MAX_TOOL_CALLS_PER_TURN):
    """What to do about this tool call: "run", "repeat", or "budget".

    Two separate protections, because they fail differently. `repeat` catches a
    model asking the identical question twice — the answer will be identical, so
    running it again cannot help. `budget` catches a model working through
    genuinely different calls but going nowhere. Pure, so it's tested.
    """
    if budget_used >= budget:
        return "budget"
    if signature in seen:
        return "repeat"
    return "run"


# How loud the live answer is played. 1.0 normally; whisper mode turns it
# down. A multiplier and not a prompt: the socket sends finished PCM, so the
# samples are the only thing left to change.
_OUT_GAIN = [1.0]
# Flipped off for good the first time scaling fails, so the voice can never
# alternate between quiet and loud chunks.
_SCALING_OK = [True]


# Bedtime amplifies the microphone. Applied on the way to the socket, so
# Gemini receives a whisper at ordinary speaking level instead of something it
# cannot hear at all. There is no VAD to fool — automatic_activity_detection is
# disabled and the key decides the turn — so this is purely about level.
_IN_GAIN = [1.0]


def set_input_gain(gain):
    _IN_GAIN[0] = max(1.0, min(24.0, float(gain)))


# HOW HARD NEO LISTENS FOR THE START OF A TURN.
#
# turn_opens() is the thing that decides a hold contained real speech, and it
# is deliberately strict: holding the key in silence must never become a turn,
# because the model answers silence by inventing what it thinks it heard.
#
# Every one of its three conditions rejects a whisper:
#   - the gate floors at VOICE_RMS (0.005) and a whisper is well under it,
#   - it wants 8 of the last 10 chunks over that gate,
#   - and it wants a peak twice the room's noise floor, which a whisper is not.
#
# Amplifying alone does not fix this. Two of the three tests are RATIOS against
# the room, so multiplying everything by eight moves the audio and the bar
# together. Bedtime therefore has to move the bar as well.
_LISTEN = {"base": None, "peak_ratio": None, "needed": None}


def set_listening_profile(base=None, peak_ratio=None, needed=None):
    """None on any field restores the normal-room default for that field."""
    _LISTEN["base"] = base
    _LISTEN["peak_ratio"] = peak_ratio
    _LISTEN["needed"] = needed


def listening_profile():
    return dict(_LISTEN)


def input_gain():
    return _IN_GAIN[0]


def amplify_pcm(chunk, gain):
    """int16 PCM, louder, hard-clipped. Pure so the clipping is tested."""
    if gain <= 1.0 or not chunk:
        return chunk
    try:
        import numpy as _np
        a = _np.frombuffer(chunk, dtype="<i2").astype("float32") * gain
        return _np.clip(a, -32768, 32767).astype("<i2").tobytes()
    except Exception:
        return chunk


def set_output_gain(gain):
    _OUT_GAIN[0] = max(0.02, min(1.0, float(gain)))


def output_gain():
    return _OUT_GAIN[0]


class Playback:
    """The speaker end. Chunks arrive from the socket faster than they play, so
    they buffer here; barge-in means dropping the buffer instantly.

    Kept deliberately dumb and lock-guarded because it is touched from three
    places: the asyncio receive task, the PortAudio callback thread, and
    whichever thread calls stop()."""

    def __init__(self):
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._stream = None
        self.playing = False

    def write(self, pcm_bytes):
        gain = _OUT_GAIN[0]
        if gain < 0.999 and _SCALING_OK[0]:
            # Scaled HERE, on the receive task, rather than in _callback —
            # _callback runs on PortAudio's realtime thread and numpy work
            # there is how you get glitches instead of a quiet voice.
            #
            # ALL OR NOTHING. Swallowing a per-chunk failure meant some chunks
            # were quiet and the next was full volume, which is not "a bug that
            # rarely fires" — it is a voice that jumps in level mid-sentence.
            # One failure turns scaling off for the session instead.
            try:
                import numpy as _np
                a = _np.frombuffer(pcm_bytes, dtype="<i2").astype("float32")
                pcm_bytes = (_np.clip(a * gain, -32768, 32767)
                             .astype("<i2").tobytes())
            except Exception as e:
                _SCALING_OK[0] = False
                self._log_once(f"[audio] can't soften the live voice ({e}); "
                               "staying at full volume rather than stuttering")
        with self._lock:
            self._buf.extend(pcm_bytes)
            self.playing = True

    def flush(self):
        """Barge-in: everything not yet played is no longer wanted."""
        with self._lock:
            self._buf.clear()
            self.playing = False

    def _log_once(self, msg):
        if not getattr(self, "_warned_scale", False):
            self._warned_scale = True
            try:
                print(msg)
            except Exception:
                pass

    def pending(self):
        with self._lock:
            return len(self._buf)

    def lag_seconds(self):
        """How far BEHIND the transcript the sound is, right now.

        This is the number the whole presentation was missing. Gemini's
        `output_transcription` is the model's text as it is GENERATED, not as
        it is spoken — measured in neo.log, 70 words of transcript arrived in 8
        seconds, and 70 words take about 28 seconds to say. The slides followed
        the transcript, so a six-slide walkthrough tore through itself in
        twenty seconds while the voice was still on slide two.

        Audio that has arrived but not yet played IS the gap. 16-bit mono at
        OUT_RATE, so bytes / (rate * 2) is exactly how many seconds of speech
        are still queued ahead of what the user can hear.
        """
        with self._lock:
            return len(self._buf) / float(OUT_RATE * 2)

    def _callback(self, outdata, frames, time_info, status):
        want = frames * 2                      # 16-bit mono
        with self._lock:
            take = min(want, len(self._buf))
            chunk = bytes(self._buf[:take])
            del self._buf[:take]
            if not self._buf:
                self.playing = False
        if take < want:
            chunk += b"\x00" * (want - take)   # silence rather than an underrun
        outdata[:] = chunk

    def start(self, sd, log=print):
        if self._stream is not None:
            return
        with _PA_LOCK:
            if self._stream is not None:
                return

            def _open():
                # Deliberately NO device re-enumeration here. This runs while
                # the session is live and the MICROPHONE stream is often
                # already open; _terminate() invalidates every open stream, so
                # doing it here silently killed the mic. neo.py's _device_watch
                # owns this now and only acts when nothing is streaming.
                st = sd.RawOutputStream(
                    samplerate=OUT_RATE, channels=1, dtype="int16",
                    blocksize=1200, callback=self._callback)
                st.start()
                return st
            try:
                self._stream = _open()
            except Exception as e:
                # A stale device list, almost always. Re-read it and try once
                # more before giving up on the whole conversation.
                log(f"[audio] speaker wouldn't open ({e}) — retrying")
                if not reset_portaudio(sd, log):
                    raise
                self._stream = _open()

    def stop(self):
        self.flush()
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception:
                pass
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None


# --------------------------------------------------------------------------- #
# The session.
# --------------------------------------------------------------------------- #
class LiveSession:
    """One conversation. start() returns immediately; everything runs on a
    private thread with its own event loop so nothing here can stall Neo's
    main run loop or its worker."""

    def __init__(self, client, model, system_instruction, tools=(),
                 log=print, on_state=None, on_text=None, on_end=None, hints=(),
                 history=(), on_tools=None, on_slow=None, on_speech_start=None,
                 on_level=None):
        # Turn state for hold-to-talk. `_holding` gates the mic: audio only
        # leaves the machine while the key is down, so Neo never hears itself
        # and an idle session costs nothing. `_speaking` records whether a real
        # turn was ever opened, so letting go after saying nothing closes
        # nothing — see end_turn.
        self._holding = False
        self._speaking = False
        self._hold_id = 0         # bumped per hold, so a slow mic open that
                                  # lands after the key came up closes itself
        self._preroll = []        # audio held back while deciding
        self._situation = ""      # what's in front of them this turn
        self._situation_sent = ""  # ...and what the model already knows
        self._levels = []         # this hold's chunk levels, for gate_for()
        # Tool-loop protection, reset at the start of every turn.
        self._tool_calls = 0
        self._tools_seen = set()
        self._tools_running = 0   # tools in flight; see answer_overdue
        self.client = client
        self.model = model
        self.system_instruction = system_instruction
        self.hints = list(hints or ())
        # Prior turns, replayed into the socket so a new session isn't amnesiac.
        self.history = list(history or ())
        self.tools = list(tools or ())
        self.tools_by_name = tool_map(self.tools)
        self._rescued = set()   # tools that were SPOKEN and then actually run
        self.log = log
        self.on_state = on_state or (lambda s: None)
        self.on_text = on_text or (lambda who, text: None)
        self.on_end = on_end or (lambda reason: None)
        # Fires the instant a tool call starts, so Neo can make a NOISE from
        # its local cache instead of waiting on a model round trip.
        self.on_tools = on_tools or (lambda names: None)
        # Fires on the edge where the model's own voice STARTS coming out of
        # the speaker. on_tools covers the silence before an answer; this is
        # how whoever filled that silence learns to shut up.
        self.on_speech_start = on_speech_start or (lambda: None)
        # Mic loudness while they hold the key, 0..1, for the indicator's bars.
        self.on_level = on_level or (lambda lv: None)

        self._stop = threading.Event()
        self._thread = None
        # LONG QUESTIONS.
        #
        # This was 64 chunks. At IN_CHUNK = 1600 samples that is 100 ms each,
        # so the queue held SIX AND A HALF SECONDS of speech and dropped
        # everything after that on the floor — `except queue.Full: pass` in the
        # mic callback. the user held the key for forty-three seconds on 9 Sept
        # asking Neo to hand a job to Claude; thirty-six of those seconds were
        # discarded while the orb sat there looking like it was listening.
        #
        # 1200 chunks is two minutes, and costs 3.8 MB of RAM at 3200 bytes a
        # chunk. There is no version of this where saving four megabytes is
        # worth losing the question.
        self._mic_q = queue.Queue(maxsize=1200)
        self._mic_stream = None
        self.device = None       # which input to open; set by neo.py
        self._playback = Playback()
        self._last_activity = time.time()
        # When the turn was closed and we started waiting for a reply. 0 = not
        # waiting. Cleared the instant any audio or turn_complete arrives.
        self._awaiting_since = 0.0
        self._said_slow = False   # "still working on it", said once per turn
        # Called when a turn has been quiet long enough to be worth mentioning.
        # NOT a failure — the session keeps waiting.
        self.on_slow = on_slow or (lambda: None)
        self._beat = 0.0             # heartbeat; see _pulse_watch
        self._mic_hold_id = None     # which hold owns the open stream
        self._ended = False          # on_end fires once; see _fire_end
        self._end_lock = threading.Lock()
        self._last_said = ("", "")   # dedupe repeated transcript frames
        self._muted_turn = False     # set by mute_turn(); cleared on turn end
        self.idle_timeout = IDLE_TIMEOUT_S   # neo.py may shorten this per session
        self.connected = False
        self.error = None
        self._loop = None        # the session thread's event loop, for speak()
        self._session = None
        self._busy = 0           # off-socket work in flight; see hold()

    # ---- lifecycle -------------------------------------------------------- #
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="neo-live")
        self._thread.start()

    def stop(self, reason="asked to"):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=6)
        self._teardown_audio()
        self.connected = False
        self.on_state("idle")
        self._fire_end(reason)

    def silence(self):
        """Drop everything not yet spoken. Exactly what barge-in does, minus
        the key — used when the user says "shush" instead of pressing fn."""
        try:
            self._playback.flush()
        except Exception:
            pass

    def mute_turn(self):
        """HUSH. Silence now, and stay silent for the rest of this answer.

        Flushing alone is not enough and that is the whole bug. By the time
        "hush" has been transcribed the model is already generating, and every
        chunk that arrives after the flush refills the buffer — so Neo dutifully
        answers "got it, quieting down", which is the one thing a hush must
        never produce. Nobody says "shush" and wants a sentence back.

        So the rest of the turn is DISCARDED as it arrives, not merely the part
        already queued. turn_complete clears it, so the next question is
        answered normally.
        """
        self._muted_turn = True
        self.silence()

    def turn_is_muted(self):
        return bool(getattr(self, "_muted_turn", False))

    @property
    def last_text(self):
        """The most recent thing NEO said, for the hush echo guard. Empty when
        the last transcript line was their, which is the safe answer: an
        empty string never suppresses a real hush."""
        try:
            who, text = self._last_said
        except Exception:
            return ""
        return text if who == "Neo" else ""

    def is_running(self):
        return bool(self._thread and self._thread.is_alive())

    def hold(self, why=""):
        """Keep this conversation open — real work is happening off-socket.

        The idle hang-up counts SOCKET traffic, and building a presentation
        puts none on it. So a fifty-second build read as fifty seconds of
        nobody talking and the session closed itself mid-build:

            21:32:59  Neo: "Give me a few seconds..."
            21:33:33  [live] quiet for a while — closing the session.
            21:34:26  [deck] on screen, finished: 6/6 drawn
            21:34:26  the deck is ready but the conversation has ended

        A held session also never looks idle to the user: neo.py keeps the orb lit
        for exactly as long as this is held, so "still working" and "still
        connected" are the same fact.
        """
        self._busy += 1
        self._last_activity = time.time()
        if why:
            self.log(f"[live] holding the session open — {why}")

    def release(self, why=""):
        """The work is done. Idle timing resumes FROM NOW, not from whenever
        the socket last had traffic on it."""
        self._busy = max(0, self._busy - 1)
        self._last_activity = time.time()
        if why:
            self.log(f"[live] released — {why}")

    def is_busy(self):
        return self._busy > 0

    def speak(self, text, log=None):
        """Make Neo say something NOW, without the user having asked again.

        This is how a presentation narrates itself. Building one takes tens of
        seconds, and a tool that blocks for that long inside the dispatch kills
        the socket outright — neo.log, verbatim:

            21:19:33  [live] present
            21:19:52  could not return tool results: no close frame received
            21:19:52  session ended: APIError: 1006 abnormal closure

        Nineteen seconds of a websocket with nothing on it, and the server
        hangs up. The deck appeared and Neo never spoke, because there was no
        longer a session to speak from.

        So present() returns immediately and the finished script is pushed back
        in through here when the deck is actually on screen. turn_complete=True
        is what makes the model treat it as its turn to talk.
        """
        loop, session = self._loop, self._session
        if loop is None or session is None or self._stop.is_set():
            return False

        async def _send():
            from google.genai import types as _t
            await session.send_client_content(
                turns=[_t.Content(role="user", parts=[_t.Part(text=text)])],
                turn_complete=True)

        try:
            asyncio.run_coroutine_threadsafe(_send(), loop).result(timeout=8)
            self._awaiting_since = time.time()
            return True
        except Exception as e:
            (log or self.log)(f"[live] couldn't hand Neo the script: {e}")
            return False

    # ---- hold to talk ----------------------------------------------------- #
    def begin_turn(self):
        """Key down. Open the mic gate; the turn itself starts on first sound.

        Idempotent, because the key handler is not the only thing that can call
        it: a re-latched flagsChanged event mid-hold used to run this a second
        time and reset a turn that was already in progress.

        Nothing here touches the audio device. This runs on the event-tap
        thread, and opening a PortAudio stream takes long enough that macOS will
        disable a tap that sits in it — which loses the key-up, which is the
        freeze this whole file was written to kill. The open is handed to a
        worker; see _open_mic_now.
        """
        if self._holding:
            return
        self._hold_id += 1
        self._last_activity = time.time()
        # PRESSING THE KEY MEANS "STOP TALKING, I AM SPEAKING NOW."
        #
        # It did not, and the microphone paid for it. The only place playback
        # was ever dropped is the server's `interrupted` event, which fires
        # when the SERVER decides someone started talking — and automatic
        # activity detection is deliberately disabled here, so it effectively
        # never fires: one occurrence in a whole day of neo.log. Neo therefore
        # kept playing out of the speakers while the mic was open, and the mic
        # recorded them.
        #
        # It reached the transcript as their own words. Verbatim, 22:26:
        #     Neo: ...to find a specific company called Similie AI...
        #     You: specific company called It's an artificial intelligence
        #          company.
        # The first four words of "their" question are Neo's. The model then
        # answered a question half of which it had asked itself.
        pending = self._playback.pending()
        self._playback.flush()
        if pending:
            self.log(f"[live] stopped talking — {pending / (OUT_RATE * 2):.1f}s "
                     f"of answer dropped because the key went down")
        self.on_state("listening")
        self._said_slow = False       # fresh turn, fresh right to one update
        self._tool_calls = 0          # fresh budget for a fresh question
        self._tools_seen = set()
        self._tools_running = 0
        self._holding = True
        self._preroll = []
        self._levels = []             # this hold's room estimate, from scratch
        self._prep_situation()
        self._open_mic_now(self._hold_id)

    def _prep_situation(self):
        """Work out what's in front of them, on a worker, while they talk.

        Two threads must not do this: the event-tap thread (macOS disables a
        tap that blocks, which loses the key-up) and the asyncio loop (which
        would freeze the receive task and the watchdog together). Reading the
        frontmost document is an osascript call, so it is neither cheap nor
        predictable. It runs here instead, and if it isn't finished by the
        time the turn opens the header is simply skipped — a missing line
        costs nothing, a stalled key costs everything.
        """
        def _work():
            try:
                import situation
                self._situation = situation.line()
            except Exception:
                self._situation = ""
        self._situation = ""
        threading.Thread(target=_work, daemon=True,
                         name="neo-situation").start()

    def end_turn(self):
        """Key up — 'I'm done, answer me'.

        Hands off to the send loop rather than doing the work here: the loop has
        to push the last queued audio before closing the turn, or the tail of
        the sentence is lost. The mic is closed there too, once the audio is
        safely away.
        """
        if not self._holding:
            return self._speaking   # already ended; a stray key-up changes nothing
        self._holding = False
        self._last_activity = time.time()
        try:
            self._mic_q.put_nowait(_END_OF_TURN)
        except queue.Full:
            try:                       # make room; never drop the end marker
                self._mic_q.get_nowait()
                self._mic_q.put_nowait(_END_OF_TURN)
            except Exception:
                self._close_mic_soon()  # queue wedged: at least free the device
        return self._speaking

    # ---- audio in --------------------------------------------------------- #
    # The mic device is opened per HOLD, not per session. The session stays up
    # for 45s so the conversation keeps its thread, but an open input stream
    # keeps macOS's orange recording dot lit that whole time — which says "this
    # is always listening" when it isn't. Open on key-down, close on key-up.
    def _open_mic(self, sd, device=None):
        if self._mic_stream is not None:
            return
        def cb(indata, frames, time_info, status):
            try:
                self._mic_q.put_nowait(bytes(indata))
            except queue.Full:
                pass          # drop rather than block PortAudio's thread
        with _PA_LOCK:
            if self._mic_stream is not None:
                return

            def _open(dev):
                st = sd.RawInputStream(
                    samplerate=IN_RATE, channels=1, dtype="int16",
                    blocksize=IN_CHUNK, device=dev, callback=cb)
                st.start()
                return st
            try:
                self._mic_stream = _open(device)
            except Exception as e:
                self.log(f"[audio] mic wouldn't open ({e}) — retrying")
                if not reset_portaudio(sd, self.log):
                    raise
                # After a reset the old index may name a different device, or
                # nothing at all. The system default is the only safe answer.
                self._mic_stream = _open(None)
            # Remember WHOSE hold this stream belongs to, so a late worker from
            # an earlier hold can't close it.
            self._mic_hold_id = self._hold_id

    def _open_mic_now(self, hold_id=None):
        """Open the input device, off whatever thread asked for it.

        The caller is the fn-tap callback, which must return in microseconds.
        Opening a PortAudio input stream is device I/O and routinely takes over
        a hundred milliseconds; block the tap in it and macOS disables the tap,
        the key-UP is delivered to nobody, and the recorder runs forever. That
        is the original freeze, and calling this synchronously quietly put it
        back. So it happens on a worker.

        hold_id is the race guard: if the key came up while the device was still
        opening, the stream we just opened belongs to a hold that is over, and
        it gets closed immediately rather than lighting the mic indicator for a
        turn nobody is speaking into.
        """
        def _open():
            try:
                import sounddevice as sd
                self._open_mic(sd, self.device)
            except Exception as e:
                self.log(f"[live] couldn't open the mic: {e}")
                return
            if hold_id is not None and (hold_id != self._hold_id
                                        or not self._holding):
                self._close_mic_now(hold_id)   # only if it's still MY stream

        threading.Thread(target=_open, daemon=True, name="neo-mic-open").start()

    def _close_mic_now(self, hold_id=None):
        """Release the device so the orange dot goes out between questions.

        Deliberately does NOT drain the queue any more: it used to, and since it
        ran before the send loop had finished uploading, it threw away the end
        of the sentence. The loop now closes the turn first and calls this
        after, by which point anything still queued is genuinely stray."""
        if hold_id is not None and hold_id != getattr(self, "_mic_hold_id", None):
            return                      # this stream belongs to a later hold
        stream, self._mic_stream = self._mic_stream, None
        if stream is None:
            return
        with _PA_LOCK:                  # never race the open worker
            for fn in ("stop", "close"):
                try:
                    getattr(stream, fn)()
                except Exception:
                    pass

    def _close_mic_soon(self, hold_id=None):
        """Close the mic WITHOUT blocking the caller.

        _close_mic_now does real PortAudio device I/O — stop() and close() can
        each take hundreds of milliseconds, and will block outright if the open
        worker still holds the device lock. The send loop used to call it
        directly, which meant that work ran ON THE ASYNCIO EVENT LOOP: the
        receive task, the tool dispatch and the no-answer watchdog all stopped
        dead with it.

        The symptom was a log that ends mid-turn at "(let go — thinking)" and
        never writes another line, with Neo silent until it was restarted —
        indistinguishable from a hang, because it was one.
        """
        threading.Thread(target=self._close_mic_now, args=(hold_id,),
                         daemon=True, name="neo-mic-close").start()

    def _teardown_audio(self):
        if self._mic_stream is not None:
            for fn in ("stop", "close"):
                try:
                    getattr(self._mic_stream, fn)()
                except Exception:
                    pass
            self._mic_stream = None
        self._playback.stop()

    # ---- the loop --------------------------------------------------------- #
    def _fire_end(self, reason):
        """on_end, exactly once, from whichever exit gets there first.

        This was the quiet disaster. on_end was only called from stop() and two
        watchdogs — NOT from the normal end of a session (idle hang-up, server
        go_away, socket error, process exit), which all return through _run's
        finally. So neo.py never flushed `_live_pending`, and the buffer holding
        NEO'S JUST-SPOKEN REPLY was dropped on the floor.

        The user's question had already been written to disk. The answer never
        was. Every clean hang-up therefore manufactured an orphaned question —
        one Neo had genuinely answered out loud — and the next session replayed
        it as an open question and answered it again, hours later.
        """
        with self._end_lock:
            if self._ended:
                return
            self._ended = True
        try:
            self.on_end(reason)
        except Exception:
            pass

    def _run(self):
        try:
            asyncio.run(self._main())
        except Exception as e:
            self.error = e
            self.log(f"[live] session ended: {type(e).__name__}: {e}")
        finally:
            self._teardown_audio()
            self.connected = False
            self.on_state("idle")
            self._fire_end("closed")

    def _transcription_config(self, types):
        """Adaptation phrases aren't in every SDK build, and a rejected field
        would cost the whole connection — so try with them and fall back."""
        if self.hints:
            try:
                return types.AudioTranscriptionConfig(
                    adaptation_phrases=list(self.hints)[:40])
            except Exception:
                pass
        return types.AudioTranscriptionConfig()

    # Optional setup fields, most valuable first. Each connect attempt drops one
    # more tier — see _main.
    #
    # This exists because of a real outage: live.py sent enable_affective_dialog,
    # the server answered "Unknown name enableAffectiveDialog: Cannot find
    # field", and the SDK surfaced that as `APIError: 1011 Internal error`. The
    # socket died in the same second it opened, every single time, and from the
    # outside it looked like the key was pressed and Neo ignored it. One
    # unsupported optional field took out the entire product.
    #
    # The lesson isn't "remove that field", it's that a PREVIEW API will do this
    # again. So the config is built in tiers and a failed setup retries with
    # less, down to a bare session that is guaranteed to work. Neo degrades; it
    # does not die.
    # ORDER MATTERS, and max_output_tokens is deliberately alone in the first
    # tier. It used to share a tier with realtime_input_config — which is what
    # carries automatic_activity_detection=disabled, the entire basis of
    # hold-to-talk. One rejected token ceiling would therefore have silently
    # handed turn-taking back to the server's voice detector, so releasing the
    # key would stop meaning "answer me" and a mid-sentence pause would cut
    # the user off. Drop the cheapest thing first, and drop it by itself.
    TIERS = (
        ("the output ceiling", ("max_output_tokens",)),
        ("tuning", ("realtime_input_config", "context_window_compression",
                    "session_resumption")),
        ("tools", ("tools",)),
        ("transcripts", ("input_audio_transcription",
                         "output_audio_transcription")),
    )

    def _config(self, tier=0):
        from google.genai import types
        cfg = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            # THE CAP THAT CUT NEO OFF MID-WORD. Unset, every turn ran on the
            # server default, and a whole narration is ONE turn: the log ends
            # at "Neo: Critically," with no error and no turn_complete, then
            # nothing until the idle timer closed the session 38 seconds later.
            # Hitting the output ceiling is silent by design, which is why it
            # reads as Neo randomly giving up on a sentence.
            #
            # 16384 was the first fix and it was still a ceiling — the deck
            # prompt was then written to keep a whole walkthrough under 150
            # words to stay clear of it, which is why decks flashed past in
            # thirteen seconds. Both of those were treating the symptom.
            #
            # Probed against this key: 16k, 32k, 64k and 128k are all accepted
            # at connect. Spoken audio costs on the order of 25 tokens a
            # second, so 65536 is hours of talking — far past any answer, and
            # the point is that no plausible turn can reach it. If the server
            # ever refuses the field it is dropped on its own (see TIERS).
            max_output_tokens=int(os.getenv("NEO_LIVE_MAX_TOKENS", "65536")),
            system_instruction=self.system_instruction,
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=VOICE))),
            # Transcripts of both sides: the HUD wants them, memory wants them,
            # and without them a live turn leaves no trace in the log. The hint
            # list is the same one that stops "the user" coming back as
            # "Ariane Six" in the pipeline.
            input_audio_transcription=self._transcription_config(types),
            output_audio_transcription=types.AudioTranscriptionConfig(),
        )
        if self.tools:
            cfg.tools = self.tools

        # NOTE: enable_affective_dialog is deliberately NOT set. The server
        # rejects the field outright on this model and takes the whole session
        # down with it. Don't add it back without testing a real connect.
        for attr, value in (
            ("realtime_input_config", lambda: types.RealtimeInputConfig(
                # OFF. The key decides when a turn ends, not a silence timer —
                # so pausing mid-sentence while still holding never cuts you
                # off, however long the pause.
                automatic_activity_detection=types.AutomaticActivityDetection(
                    disabled=True))),
            ("context_window_compression", lambda: types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow())),
            ("session_resumption", lambda: types.SessionResumptionConfig()),
        ):
            try:
                setattr(cfg, attr, value())
            except Exception:
                pass

        # Strip whatever this tier has given up on.
        for _name, fields in self.TIERS[:tier]:
            for field in fields:
                try:
                    setattr(cfg, field, None)
                except Exception:
                    pass
        return cfg

    async def _main(self):
        import sounddevice as sd

        try:
            # Only the speaker opens here. The mic follows the key — see
            # _open_mic_now — so nothing is recording until you hold.
            self._playback.start(sd, self.log)
            if self._holding:
                self._open_mic(sd, self.device)
        except Exception as e:
            self.error = e
            self.log(f"[live] could not open audio ({e}) — staying on the pipeline.")
            return

        # Connect, dropping optional config on each failure. See TIERS.
        session_cm = None
        last_error = None
        for tier in range(len(self.TIERS) + 1):
            try:
                session_cm = self.client.aio.live.connect(
                    model=self.model, config=self._config(tier))
                session = await session_cm.__aenter__()
                if tier:
                    given_up = ", ".join(n for n, _f in self.TIERS[:tier])
                    self.log(f"[live] connected without: {given_up} "
                             f"(the server rejected them: {last_error})")
                break
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                session_cm = None
                if tier == len(self.TIERS):
                    raise
                self.log(f"[live] setup rejected ({last_error}) — retrying with less")

        try:
            self.connected = True
            self._loop = asyncio.get_running_loop()
            self._session = session
            self._last_activity = time.time()
            self.on_state("live")
            self.log(f"[live] connected ({self.model}) — just talk.")

            # Replay what was already said. A new socket starts blank, so
            # without this every hang-up wipes the conversation and Neo asks
            # you something, times out, and has forgotten it ever asked.
            # turn_complete=False seeds context without provoking a reply.
            if self.history:
                try:
                    from google.genai import types as _t
                    turns = [_t.Content(role=h.get("role", "user"),
                                        parts=[_t.Part(text=h.get("text", ""))])
                             for h in self.history if h.get("text")]
                    if turns:
                        await session.send_client_content(turns=turns,
                                                          turn_complete=False)
                        self.log(f"[live] carried {len(turns)} earlier turns in")
                except Exception as e:
                    self.log(f"[live] couldn't replay history: {e}")

            sender = asyncio.create_task(self._send_loop(session))
            receiver = asyncio.create_task(self._receive_loop(session))
            watchdog = asyncio.create_task(self._idle_loop())
            done, pending = await asyncio.wait(
                {sender, receiver, watchdog},
                return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:                 # surface a real error, ignore cancels
                exc = task.exception() if not task.cancelled() else None
                if exc:
                    raise exc
        finally:
            self._loop = None
            self._session = None
            if session_cm is not None:
                try:
                    await session_cm.__aexit__(None, None, None)
                except Exception:
                    pass

    async def _send_loop(self, session):
        """Stream mic audio, but only while the key is held, and mark the turn
        boundaries explicitly.

        The gate is the whole design: between holds nothing is uploaded, so the
        session can stay open indefinitely (keeping the conversation) without
        listening to the room or spending quota."""
        from google.genai import types
        loop = asyncio.get_running_loop()

        while not self._stop.is_set():
            held = getattr(self, "_held_marker", None)
            if held is not None:
                self._held_marker = None
                chunk = held            # the end marker a batch stepped over
            else:
                try:
                    chunk = await loop.run_in_executor(
                        None, lambda: self._mic_q.get(timeout=0.2))
                except Exception:
                    continue    # queue empty: nothing to do until audio arrives

            # The key came up. Everything before this marker has already been
            # sent, so the turn can close immediately — no polling delay, no
            # lost tail.
            # AMPLIFY HERE, not at the send. The level test below, the
            # pre-roll buffer and the socket all have to see the same audio —
            # boosting only at the send left turn_opens judging the raw
            # whisper, so bedtime amplified something that had already been
            # rejected. One place, everything downstream.
            if chunk is not _END_OF_TURN and _IN_GAIN[0] > 1.0:
                chunk = amplify_pcm(chunk, _IN_GAIN[0])

            if chunk is _END_OF_TURN:
                if self._speaking:
                    try:
                        await session.send_realtime_input(
                            activity_end=types.ActivityEnd())
                        self.on_state("thinking")
                        self._awaiting_since = time.time()
                        self.log("(let go — thinking)")
                    except Exception as e:
                        self.log(f"[live] couldn't end the turn: {e}")
                else:
                    self.log("(let go, but nothing was said — ignoring)")
                    self.on_state("idle")
                # ONLY if no new hold has started. The queue holds up to 64
                # chunks — 6.4 seconds of audio — so this marker is drained
                # long after the key came up. Press again in that window and
                # the old turn's cleanup used to close the NEW hold's mic and
                # wipe its state: the orange dot blinks out mid-sentence and
                # the hold records nothing. That is the flashing indicator, and
                # it is the "(let go, but nothing was said — ignoring)" pairs
                # in the log.
                if not self._holding:
                    self._speaking = False
                    self._preroll = []
                    self._levels = []
                    self._close_mic_soon()
                continue

            if not self._holding:
                continue        # stray audio after the turn closed; drop it
            try:
                self.on_level(min(1.0, chunk_rms(chunk) * 8.0))
            except Exception:
                pass

            # Open the turn only once we're SURE someone is talking. Holding
            # the key and saying nothing must never become a turn, because the
            # model answers silence by inventing what it thinks it heard.
            if not self._speaking:
                self._preroll.append(chunk)
                if len(self._preroll) > PREROLL_CHUNKS:
                    self._preroll.pop(0)
                level = chunk_rms(chunk)
                # The bar is set by the room the user is actually in, not by a
                # constant measured in a different one. Bounded so one loud
                # passage in a long hold cannot drag the estimate around.
                self._levels.append(level)
                if len(self._levels) > 80:
                    del self._levels[0]
                if not turn_opens(self._levels):
                    continue
                try:
                    await session.send_realtime_input(
                        activity_start=types.ActivityStart())
                    self._speaking = True
                    self.on_state("listening")
                    # What they're looking at, if it changed since last time.
                    # turn_complete=False seeds it without provoking a reply —
                    # the same trick the history replay uses. Resending an
                    # unchanged line every turn would just pile up duplicates.
                    here = self._situation
                    if here and here != self._situation_sent:
                        try:
                            await session.send_client_content(
                                turns=[types.Content(role="user", parts=[
                                    types.Part(text=here)])],
                                turn_complete=False)
                            self._situation_sent = here
                        except Exception as e:
                            self.log(f"[live] couldn't send the situation: {e}")
                    # Flush the pre-roll so confirming speech a few chunks in
                    # doesn't clip the first word off the question.
                    for held in self._preroll:
                        await session.send_realtime_input(
                            audio=types.Blob(
                                data=held,
                                mime_type=f"audio/pcm;rate={IN_RATE}"))
                    self._preroll = []
                except Exception as e:
                    self.log(f"[live] couldn't start the turn: {e}")
                    continue

            # DRAIN, don't trickle. One await per 100 ms chunk means the send
            # loop can only just keep pace with the microphone in the best
            # case, and never catches up after a burst — so a long hold stayed
            # backlogged and the turn ended seconds after the key came up.
            # Everything already waiting goes in one blob.
            batch = [chunk]
            while len(batch) < SEND_BATCH_MAX:
                try:
                    nxt = self._mic_q.get_nowait()
                except queue.Empty:
                    break
                if nxt is _END_OF_TURN:
                    # Hold it here, not by pushing back into the Queue's
                    # internal deque — appendleft() reaches past the lock that
                    # makes Queue safe, and the mic callback writes to it from
                    # PortAudio's thread. The outer loop picks this up first.
                    self._held_marker = nxt
                    break
                if _IN_GAIN[0] > 1.0:
                    nxt = amplify_pcm(nxt, _IN_GAIN[0])
                batch.append(nxt)
            try:
                await session.send_realtime_input(
                    audio=types.Blob(data=b"".join(batch),
                                     mime_type=f"audio/pcm;rate={IN_RATE}"))
                self._last_activity = time.time()
            except Exception as e:
                self.log(f"[live] send stopped: {e}")
                return

    async def _receive_loop(self, session):
        while not self._stop.is_set():
            async for message in session.receive():
                if self._stop.is_set():
                    return
                self._last_activity = time.time()
                await self._handle_message(session, message)
            # receive() returning means the server closed the turn stream;
            # loop round and keep listening until someone asks us to stop.

    async def _handle_message(self, session, message):
        content = getattr(message, "server_content", None)
        if content is not None:
            # Barge-in. The server noticed the user started talking, so whatever
            # is still buffered is stale — drop it now, not after it plays.
            if getattr(content, "interrupted", False):
                self._playback.flush()
                # Only claim to be listening if the key is genuinely down.
                self.on_state("listening" if self._holding else "idle")

            turn = getattr(content, "model_turn", None)
            if turn is not None:
                for part in (getattr(turn, "parts", None) or ()):
                    blob = getattr(part, "inline_data", None)
                    if blob is not None and getattr(blob, "data", None):
                        self._awaiting_since = 0.0   # it answered
                        self._said_slow = False
                        # Flushing on key-down is not enough on its own: the
                        # server keeps streaming the rest of the answer, and
                        # those chunks would refill the buffer and play into
                        # the open microphone. While the key is held, whatever
                        # is still arriving belongs to the question the user has
                        # just interrupted.
                        if self._holding:
                            continue
                        # The leading edge of the answer. A filler may still be
                        # playing from on_tools — it was started because the
                        # tool looked slow, and the guard there could only ask
                        # whether the model had ALREADY begun. It hadn't; it
                        # begins here, a second or two later, straight over the
                        # top. Two voices at once, which is what the user heard.
                        # Hushed: throw the rest of the answer away rather
                        # than play it. See mute_turn.
                        if getattr(self, "_muted_turn", False):
                            continue
                        if not self._playback.playing:
                            try:
                                self.on_speech_start()
                            except Exception:
                                pass
                        self._playback.write(blob.data)
                        self.on_state("speaking")

            for attr, who in (("input_transcription", "You"),
                              ("output_transcription", "Neo")):
                tr = getattr(content, attr, None)
                text = (getattr(tr, "text", "") or "").strip() if tr else ""
                # Gemini re-sends the whole accumulated transcript as it firms
                # up, so the identical line arrived three times in a row and the
                # log read like Neo was stuck in a loop. Only surface changes.
                if text and self._last_said != (who, text):
                    self._last_said = (who, text)
                    if who == "Neo":
                        self._rescue_spoken_tool(text)
                    self.on_text(who, text)

            if getattr(content, "turn_complete", False):
                self._muted_turn = False      # the hush covered THIS answer only
                self._awaiting_since = 0.0
                self._said_slow = False
                # Answer finished. The socket stays up for the conversation, but
                # nothing is being recorded — so the orb goes quiet.
                self.on_state("idle")

        tool_call = getattr(message, "tool_call", None)
        if tool_call is not None:
            await self._handle_tools(session, tool_call)

        if getattr(message, "go_away", None) is not None:
            # The server is about to close this socket (they're time-limited).
            # Say so plainly instead of looking like a crash.
            self.log("[live] server is closing the session.")
            self._stop.set()

    def _pulse_watch(self, stall=8.0):
        """Runs on a plain thread, so a blocked event loop cannot silence it."""
        while not self._stop.is_set():
            time.sleep(2.0)
            # A session that has already ended has no heartbeat to miss. Belt
            # and braces with the _stop set on the hang-up path: both exits
            # have to be covered, because "the session thread stalled" is the
            # line that gets read when something is genuinely wrong and it is
            # worth nothing if it also fires on every clean close.
            if getattr(self, "_ended", False):
                return
            beat = getattr(self, "_beat", 0.0)
            if beat and time.time() - beat > stall:
                self.log(f"[live] the session thread stalled for "
                         f"{time.time() - beat:.0f}s — dropping it so the next "
                         f"press starts clean.")
                self._awaiting_since = 0.0
                self._stop.set()
                try:
                    self.on_state("idle")
                    self._fire_end("stalled")
                except Exception:
                    pass
                return

    async def _handle_tools(self, session, tool_call):
        """Run what the model asked for, concurrently, while saying something.

        Two things matter here and both are about the wait. Tools run in
        PARALLEL — three lookups take as long as the slowest, not the sum. And
        Neo speaks a short filler first, because the model goes silent the
        instant it decides to call a tool and dead air in a conversation reads
        as a crash.
        """
        from google.genai import types
        loop = asyncio.get_running_loop()
        calls = list(getattr(tool_call, "function_calls", None) or ())
        if not calls:
            return
        names = [getattr(fc, "name", "") for fc in calls]
        self.log(f"[live] {', '.join(names)}")
        self.on_state("thinking")
        # A tool call IS the model responding — it just hasn't spoken yet. The
        # no-answer watchdog only knows about audio, so without this it counted
        # a legitimate lookup as a dead socket and reset the session out from
        # under it at twelve seconds. Restart the clock instead: tools get their
        # own budget, and if THEY hang the watchdog still catches it.
        self._awaiting_since = time.time()

        # Local, cached, immediate. This is the first sound the user hears and it
        # has to land in milliseconds, so it does NOT go through the model.
        if should_fill(names):
            try:
                self.on_tools(names)
            except Exception:
                pass          # a missing filler is cosmetic; never fail the turn

        async def run(fc):
            name = getattr(fc, "name", "")
            args = dict(getattr(fc, "args", None) or {})
            sig = call_signature(name, args)
            verdict = loop_verdict(sig, self._tools_seen, self._tool_calls)

            if verdict == "budget":
                self.log(f"[live] tool budget spent — refusing {name}")
                result = {"error": (
                    "You have used this turn's whole tool budget. Do NOT call "
                    "another tool. Answer out loud now with what you already "
                    "know, and say plainly if you couldn't find something.")}
            elif verdict == "repeat":
                self.log(f"[live] {name} already ran with these arguments — refusing")
                result = {"error": (
                    f"You already called {name} with exactly these arguments "
                    "and got the answer above. Calling it again returns the "
                    "same thing. Use what you have, or try something different.")}
            else:
                self._tool_calls += 1
                self._tools_seen.add(sig)
                result = await loop.run_in_executor(
                    None, call_tool, self.tools_by_name, name, args, self.log)
            return types.FunctionResponse(
                id=getattr(fc, "id", None), name=name, response=result)

        self._tools_running += len(calls)
        try:
            responses = await asyncio.gather(*(run(fc) for fc in calls),
                                             return_exceptions=True)
        finally:
            # Always, including the paths that raise. A counter that leaks
            # upward disables the watchdog for the life of the session; one
            # that leaks downward re-arms it mid-tool, which is the bug.
            self._tools_running = max(0, self._tools_running - len(calls))
        good = [r for r in responses if not isinstance(r, BaseException)]
        if good:
            try:
                await session.send_tool_response(function_responses=good)
                self._awaiting_since = time.time()   # now it owes us words
            except Exception as e:
                self.log(f"[live] could not return tool results: {e}")

    # A tool the model SAID instead of calling. Once a prompt has shown it four
    # bracketed tag syntaxes it invents a fifth, and on this path there is no
    # such thing as a hidden tag — the user heard
    #
    #     [[hand_to_claude: {"task": "Open the file ...
    #
    # read out to them, punctuation and all. The prompt no longer teaches any of
    # it (memory.spoken_prompt), but a model can still slip, and when it does
    # the INTENT was real. So run it. Better a tool that fires a beat late than
    # a request that evaporates into a sentence nobody wanted to hear.
    _SPOKEN_TOOL = re.compile(r"\[\[\s*(\w+)\s*:\s*(\{.*?\})\s*\]\]", re.S)

    def _rescue_spoken_tool(self, text):
        """Model announced a tool out loud? Actually run it. Returns True if so."""
        m = self._SPOKEN_TOOL.search(text or "")
        if not m:
            return False
        name, raw = m.group(1), m.group(2)
        if name in self._rescued:
            return False               # the transcript re-sends as it firms up
        fn = self.tools_by_name.get(name)
        if fn is None:
            self.log(f"[live] said a tag out loud for an unknown tool: {name}")
            return False
        try:
            args = json.loads(raw)
            if not isinstance(args, dict):
                raise ValueError("not an object")
        except Exception as e:
            self.log(f"[live] {name} was spoken but its arguments were "
                     f"unreadable: {type(e).__name__}")
            return False
        self._rescued.add(name)
        self.log(f"[live] {name} was SPOKEN instead of called — running it")
        threading.Thread(target=self._run_rescued, args=(name, fn, args),
                         daemon=True, name="neo-tool-rescue").start()
        return True

    def _run_rescued(self, name, fn, args):
        try:
            fn(**args)
        except TypeError as e:
            self.log(f"[live] couldn't run the spoken {name}: {e}")
        except Exception as e:
            self.log(f"[live] the spoken {name} failed: {type(e).__name__}: {e}")

    async def _idle_loop(self):
        # Heartbeat. If this loop stops ticking, the asyncio event loop is
        # blocked — and a blocked loop is silent in exactly the same way a slow
        # model is, which cost hours. A watcher thread outside the loop notices
        # and says so instead of leaving the user holding a key that does nothing.
        self._beat = time.time()
        threading.Thread(target=self._pulse_watch, daemon=True,
                         name="neo-live-pulse").start()
        while not self._stop.is_set():
            self._beat = time.time()
            await asyncio.sleep(1.0)
            if self._holding:
                continue     # a hold is never idle, however long it runs

            # A closed turn that never got an answer. This is the failure that
            # used to be invisible: activity_end goes out, the socket wedges,
            # and Neo sits mute until the idle timer closes it half a minute
            # later. Never be silent about it — drop the socket so the next
            # press reconnects clean.
            in_tool = self._tools_running > 0 or self._busy > 0
            # Slow, but alive as far as anyone can tell. Say so ONCE and keep
            # waiting — this used to be where the session was destroyed.
            if (answer_slow(self._awaiting_since, time.time(), in_tool=in_tool)
                    and not self._said_slow):
                self._said_slow = True
                waited = time.time() - self._awaiting_since
                self.log(f"[live] {waited:.0f}s and still nothing back — "
                         "telling them it's still going, NOT giving up")
                try:
                    self.on_slow()
                except Exception as e:
                    self.log(f"[live] couldn't say it's still going: {e}")

            if answer_overdue(self._awaiting_since, time.time(), in_tool=in_tool):
                self._awaiting_since = 0.0
                self.log("[live] no answer came back — resetting the session.")
                self.on_state("idle")
                self._fire_end("no answer")
                self._stop.set()
                return

            if self._busy or self._tools_running > 0:
                # Work is running that produces no socket traffic. Not idle.
                #
                # A TOOL CALL COUNTS. It did not, and that is the bug where Neo
                # says "One moment, checking" and then vanishes: a long
                # search_web puts nothing on the socket while it runs, the idle
                # timer read thirty seconds of working as thirty seconds of
                # silence, and closed the conversation mid-tool. the user asked a
                # long fantasy-football question at 02:00:47, the tool started
                # at 02:00:48, and the session was shut at 02:01:18 — before
                # the answer ever came back.
                #
                # answer_overdue already knew about _tools_running. This branch
                # did not, so the two watchdogs disagreed about whether Neo was
                # doing anything.
                self._last_activity = time.time()
                continue

            if should_hang_up(self._last_activity, time.time(), self.idle_timeout):
                self.log("[live] quiet for a while — closing the session.")
                # Tell the pulse watcher we meant this. It runs on a plain
                # thread outside the event loop and only ever checks _stop, so
                # returning without setting it left it watching a heartbeat
                # that had legitimately stopped — and eight seconds after every
                # ordinary hang-up it logged "the session thread stalled for
                # 9s", which is a scary line describing nothing at all. The log
                # has it directly after "closed (closed)".
                self._stop.set()
                return
        return
