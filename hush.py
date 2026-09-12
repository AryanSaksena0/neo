"""
hush.py — "shush" while Neo is talking, and a voice that can drop to a whisper.

TWO THINGS, ONE MODULE, BECAUSE THEY ARRIVE THE SAME WAY
Both are things the user says WHILE Neo is speaking, which is the one moment the
microphone is normally shut. Everything here exists to make that moment
listenable without giving up the thing that makes Neo trustworthy.

THE PRIVACY LINE, AND EXACTLY WHERE IT MOVED
NEO.md's promise is "the mic ONLY opens while you hold fn, so there's no
always-on listening." This moves that line by one bounded case and no further:

    the mic is also open while NEO ITSELF IS SPEAKING, and not one moment longer

It opens when speech starts, closes the instant speech stops, and the audio
never leaves the machine — the transcription is the local Whisper model that is
already loaded, so nothing is uploaded, logged or kept. Windows are discarded
as soon as they are matched. NEO_HUSH=0 turns the whole thing off and the fn
key still hushes instantly, as it always has.

THE ECHO PROBLEM, WHICH IS THE REAL ENGINEERING HERE
The mic is open precisely when the speakers are loudest, so the first thing it
hears is Neo. Run a transcriber on that and Neo reads its own sentence back —
and the moment its own sentence contains "stop", it hushes itself mid-word.

The guard is that we know exactly what Neo is saying. Every match is checked
against Neo's own live transcript, and a hush word that also appears in what
Neo is currently saying is treated as echo and ignored. The failure this
chooses is the safe one: if the user says "stop" at the same moment Neo says
"stop worrying", Neo keeps talking and the fn key is right there. The opposite
failure — Neo silencing itself because of a word it just said — would make the
feature feel broken rather than merely imperfect.
"""

import collections
import re
import threading
import time

# --------------------------------------------------------------------------- #
# Pure matching — tested in test_hush.py, no audio required
# --------------------------------------------------------------------------- #

# Said WHILE Neo is talking, these mean "stop, now". Short on purpose: this
# list runs against a transcript of a room with a speaker playing in it, and
# every extra phrase is another way to be wrong.
_HUSH = (
    "mute", "shush", "hush", "shut up", "be quiet", "quiet",
    "stop talking", "stop speaking", "stop", "enough", "thats enough",
    "that is enough", "okay stop", "ok stop", "alright stop",
    "nevermind", "never mind", "forget it", "cancel that", "shh", "sh",
)

# "talk quietly" contains "quiet", so these are checked FIRST and a match here
# blocks the hush list entirely. Getting that precedence wrong means "talk
# quietly" silences Neo instead of softening it.
_WHISPER_ON = (
    "whisper mode", "whisper", "whispering", "talk quietly", "speak quietly",
    "talk quieter", "speak quieter", "talk softly", "speak softly",
    "talk softer", "speak softer", "lower your voice", "quieter voice",
    "keep it down", "keep your voice down", "turn it down", "turn down",
    "volume down", "less loud", "not so loud", "too loud", "quiet mode",
    "quiet voice", "be softer", "softer",
)

# BEDTIME. Not a louder whisper — a different microphone. See neo.py:
# whisper mode changes only how loudly Neo answers; bedtime also opens the mic
# right up so an actual whisper across a dark room registers at all.
_BEDTIME_ON = (
    "bedtime mode", "bedtime", "night mode", "nighttime mode", "sleep mode",
    "good night", "goodnight", "quiet hours", "everyone is asleep",
    "everyones asleep", "people are sleeping",
)

# Leaves BOTH bedtime and whisper. One way out, and it is the obvious phrase —
# a mode you cannot remember how to exit is a mode you stop using.
_WHISPER_OFF = (
    "normal mode", "day mode", "daytime mode", "wake up", "morning mode",
    "good morning", "exit bedtime", "end bedtime", "leave bedtime",
    "turn off bedtime", "turn off night mode", "stop bedtime",
    "talk normal", "talk normal mode", "normal voice",
    "stop whispering", "stop the whisper",
    "talk normally", "speak normally", "speak normal",
    "out loud", "louder", "speak up", "talk louder", "turn it up",
    "turn up", "volume up", "full volume", "normal volume", "back to normal",
)


def norm(text):
    """Lowercase, letters and spaces only, single-spaced and space-padded, so
    a phrase test is a plain substring test and can't match mid-word."""
    low = re.sub(r"[^a-z0-9 ]", " ", str(text or "").lower())
    return " " + re.sub(r"\s+", " ", low).strip() + " "


def _hit(padded, phrases):
    """The longest matching phrase, or None. Longest wins so "stop talking"
    is never reported as the shorter "stop"."""
    found = [p for p in phrases if f" {p} " in padded]
    return max(found, key=len) if found else None


def voice_mode_request(text):
    """"normal" | "whisper" | "bedtime", or None when the words are not about
    how Neo is listening and speaking.

    ORDER IS THE WHOLE FUNCTION. OFF is tested first because "stop whispering"
    contains "whisper" and "turn off night mode" contains "night mode" —
    reading either as a request to ENTER the mode builds one nobody can leave.
    Bedtime then beats whisper, because bedtime is the stronger, more specific
    ask and several of its phrases imply quiet as well.
    """
    padded = norm(text)
    if _hit(padded, _WHISPER_OFF):
        return "normal"
    if _hit(padded, _BEDTIME_ON):
        return "bedtime"
    if _hit(padded, _WHISPER_ON):
        return "whisper"
    return None


def is_hush(text):
    """True when this is 'stop talking, right now'.

    A request to change the VOLUME is never a request for silence, so a whisper
    phrase vetoes the whole hush list — otherwise the "quiet" inside "talk
    quietly" would cut Neo off instead of softening them.
    """
    if voice_mode_request(text) is not None:
        return False
    return _hit(norm(text), _HUSH) is not None


def hush_phrase(text):
    """Which word matched, for the log and for the echo check."""
    if voice_mode_request(text) is not None:
        return None
    return _hit(norm(text), _HUSH)


def echoes_neo(heard_phrase, neo_text):
    """True when the phrase we matched is just Neo's own voice in the room.

    The mic is open while the speakers are playing, so this is not an edge
    case — it is the default case, and without it Neo hushes itself the first
    time it says "stop" in a sentence.
    """
    if not heard_phrase or not neo_text:
        return False
    return f" {heard_phrase} " in norm(neo_text)


_MODE_PHRASES = {"normal": _WHISPER_OFF, "bedtime": _BEDTIME_ON,
                 "whisper": _WHISPER_ON}


def should_act(heard_text, neo_text):
    """("hush", phrase) or (None, phrase-or-None). Nothing else.

    IT ONLY EVER HUSHES, AND THAT IS THE FIX FOR A REAL BUG. This used to
    return mode changes too, and on 8 September that undid the mode it had just
    been asked for:

        22:00:29  [voice] bedtime mode — microphone x8, gate 0.0004
        22:00:34  [hush] heard 'whisper' -> whisper

    Bedtime amplifies the microphone eight times, so the listener heard NEO'S
    OWN confirmation — which contains the word "whisper" — and downgraded the
    mode five seconds after it was set. The echo guard could not save it: it
    compares against Neo's transcript, and at that instant there wasn't one yet.

    The asymmetry is the point. A hush is URGENT — it has to work mid-sentence,
    which is the whole reason this microphone exists at all. A mode change is
    not: the user can hold the key and ask, the same way they ask for anything
    else, through a path that knows the difference between their voice and Neo's.
    Letting the risky listener drive the durable setting was the mistake.
    """
    if voice_mode_request(heard_text) is not None:
        return None, None          # a mode change is never this listener's job
    phrase = hush_phrase(heard_text)
    if not phrase:
        return None, None
    if echoes_neo(phrase, neo_text):
        return None, phrase
    return "hush", phrase


# --------------------------------------------------------------------------- #
# The listener
# --------------------------------------------------------------------------- #
IN_RATE = 16000
WINDOW_S = 1.5            # how much audio each match looks at
HOP_S = 0.6               # how often it looks
# Below this the room is silent and running a transcriber on it is pure waste.
# Neo's own voice through the speakers usually sits well above this, which is
# why the echo guard does the real work and this is only a cheap first filter.
RMS_GATE = 0.012

# Bedtime turns these up and down: the same amplification the rest of Neo gets,
# applied here too, or "shush" would be the one thing a whisper couldn't do.
_SENS = {"gain": 1.0, "gate": RMS_GATE}


def set_sensitivity(gain=1.0, gate=RMS_GATE):
    _SENS["gain"] = max(1.0, float(gain))
    _SENS["gate"] = max(0.0001, float(gate))


def sensitivity():
    return _SENS["gain"], _SENS["gate"]


class HushListener:
    """Listens ONLY while Neo speaks. Owns nothing else.

    transcribe(float32 mono @16k) -> str   the already-loaded local Whisper
    on_action(action, phrase)              "hush" | "whisper" | "normal"
    neo_text()                             what Neo is saying right now
    """

    def __init__(self, transcribe, on_action, neo_text=None, device=None,
                 log=print):
        self._transcribe = transcribe
        self._on_action = on_action
        self._neo_text = neo_text or (lambda: "")
        self._device = device
        self._log = log
        self._stream = None
        self._buf = collections.deque(maxlen=int(IN_RATE * WINDOW_S))
        self._lock = threading.Lock()
        self._running = threading.Event()
        self._worker = None
        self.misfires = 0

    # -- lifecycle ---------------------------------------------------------- #
    def start(self):
        """Neo has started speaking. Open the mic; never raise — a hush that
        cannot start must never be the reason the voice doesn't work."""
        if self._running.is_set():
            return False
        try:
            import numpy as np
            import sounddevice as sd
        except Exception:
            return False
        self._running.set()
        with self._lock:
            self._buf.clear()
        # PortAudio's OPEN path is not thread-safe, and this stream opens
        # while the speaker stream is running. live.py's AUDIO_LOCK is the
        # process-wide serialisation point for exactly this; opening outside it
        # is the kind of hole that shows up as a glitch rather than an error.
        try:
            import live as _live
            pa_lock = _live.AUDIO_LOCK
        except Exception:
            import threading as _t
            pa_lock = _t.Lock()

        try:
            def cb(indata, frames, time_info, status):
                try:
                    a = np.frombuffer(bytes(indata), dtype="int16")
                    with self._lock:
                        self._buf.extend(a.astype("float32") / 32768.0)
                except Exception:
                    pass
            # Hold a hard reference to the callback for as long as the stream
            # lives. PortAudio calls it from CoreAudio's realtime thread, and
            # if Python collects the closure first that thread segfaults inside
            # ffi_closure_SYSV — which is exactly the crash signature already
            # showing up in this repo's own diagnostic reports.
            self._cb = cb
            with pa_lock:
                self._stream = sd.RawInputStream(
                    samplerate=IN_RATE, channels=1, dtype="int16",
                    blocksize=int(IN_RATE * 0.1), device=self._device,
                    callback=cb)
                self._stream.start()
        except Exception as e:
            self._log(f"[hush] mic unavailable ({e}); the fn key still stops me")
            self._running.clear()
            self._stream = None
            return False
        self._worker = threading.Thread(target=self._loop, daemon=True,
                                        name="neo-hush")
        self._worker.start()
        return True

    def stop(self):
        """Neo has stopped speaking. Close the mic immediately — this is the
        promise the whole feature rests on."""
        self._running.clear()
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                import live as _live
                pa_lock = _live.AUDIO_LOCK
            except Exception:
                pa_lock = None
            try:
                if pa_lock is not None:
                    with pa_lock:
                        stream.stop()
                        stream.close()
                else:
                    stream.stop()
                    stream.close()
            except Exception:
                pass
        self._cb = None            # only after the stream is closed
        with self._lock:
            self._buf.clear()

    def listening(self):
        return self._running.is_set() and self._stream is not None

    # -- the loop ----------------------------------------------------------- #
    def _loop(self):
        import numpy as np
        # Let a little audio arrive before the first look, or the first window
        # is mostly silence and costs a transcribe for nothing.
        time.sleep(HOP_S)
        while self._running.is_set():
            try:
                with self._lock:
                    window = np.array(self._buf, dtype="float32")
                if window.size >= IN_RATE * 0.7:
                    gain, gate = sensitivity()
                    if gain > 1.0:
                        window = np.clip(window * gain, -1.0, 1.0)
                    rms = float(np.sqrt(np.mean(window ** 2)))
                    if rms >= gate:
                        self._consider(window)
            except Exception as e:
                self._log(f"[hush] {type(e).__name__}: {e}")
            self._running.wait(HOP_S)

    def _consider(self, window):
        heard = ""
        try:
            heard = (self._transcribe(window) or "").strip()
        except Exception:
            return
        if not heard:
            return
        try:
            said = self._neo_text() or ""
        except Exception:
            said = ""
        action, phrase = should_act(heard, said)
        if action is None:
            if phrase:
                # Matched a word, then recognised it as Neo's own voice coming
                # back. Worth counting: a lot of these means the echo guard is
                # carrying the feature, which is exactly what it is for.
                self.misfires += 1
            return
        self._log(f"[hush] heard {phrase!r} -> {action}")
        with self._lock:
            self._buf.clear()          # don't match the same window twice
        try:
            self._on_action(action, phrase)
        except Exception as e:
            self._log(f"[hush] acting on {action} failed: {e}")
