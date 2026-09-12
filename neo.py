"""
Neo — a hands-free voice assistant for your Mac.

Hold the fn (Globe) key, talk, release. Neo listens, thinks, and talks back in a
natural voice. Witty, mood-aware, and it remembers you across sessions.

Everything runs locally except the "brain" (Gemini free tier):
    ears  -> faster-whisper  (speech to text, on-device)
    brain -> Gemini 2.5 Flash (one call per command)
    mouth -> Kokoro-82M       (text to speech, on-device)

The mic ONLY opens while you hold fn, so there's no always-on listening.

Run:  python neo.py
Quit: Ctrl-C in the terminal, or `pkill -f neo.py`
"""

import os
import re
import sys
import json
import datetime
import signal
import threading
import queue
import time

import numpy as np
from dotenv import load_dotenv

# --- friendly dependency check ---------------------------------------------
def _need(pkg, pipname=None):
    print(f"\n[neo] Missing dependency: {pkg}")
    print(f"      Run setup first:  ./setup.sh   (or pip install {pipname or pkg})\n")
    sys.exit(1)

# NEO_NO_AUDIO=1 — for the test suites, and nothing else.
#
# Importing sounddevice initialises PortAudio, which on macOS spins up a
# CoreAudio HALC I/O thread for the process. In a TEST process that thread has
# nothing to do and no stream to serve, and it faulted anyway: a hard SIGSEGV
# roughly twenty seconds in, inside a CFFI callback, with no Python frame on
# the crashing thread and no stream ever opened by the suite. Every run put a
# "Python quit unexpectedly" dialog on their screen.
#
# The tests do not need a real audio subsystem — they check routing, parsing
# and wiring, and every place that would actually make a sound is stubbed
# already. So the honest fix is not to start PortAudio at all. The app itself
# never sets this, so nothing about how Neo really behaves changes.
if os.getenv("NEO_NO_AUDIO") == "1":
    import types as _types

    class _SilentStream:
        def __init__(self, *a, **k): pass
        def start(self): pass
        def stop(self): pass
        def close(self): pass
        def write(self, *a, **k): pass
        def read(self, *a, **k): return (b"", False)
        def __enter__(self): return self
        def __exit__(self, *a): return False

    _silent = _types.ModuleType("sounddevice")
    _silent.query_devices = lambda *a, **k: []
    _silent.play = lambda *a, **k: None
    _silent.stop = lambda *a, **k: None
    _silent.wait = lambda *a, **k: None
    _silent.rec = lambda *a, **k: None
    _silent.default = _types.SimpleNamespace(device=(None, None), samplerate=None)
    _silent._terminate = lambda *a, **k: None
    _silent._initialize = lambda *a, **k: None
    for _n in ("InputStream", "OutputStream", "RawInputStream",
               "RawOutputStream", "Stream", "RawStream"):
        setattr(_silent, _n, _SilentStream)
    # Into sys.modules, so EVERY module that imports sounddevice — live.py and
    # hush.py both do it lazily inside functions — gets the same silence. A
    # stub that only covers neo.py leaves PortAudio to be initialised by the
    # first other import, which is exactly what still crashed.
    sys.modules["sounddevice"] = _silent
    sd = _silent
else:
    try:
        import sounddevice as sd
    except Exception:
        _need("sounddevice")

try:
    from google import genai
    from google.genai import types
except Exception:
    _need("google-genai", "google-genai")

# These two load heavy models, imported lazily inside warmup() so the app starts
# talking sooner and errors are clearer.

from memory import (
    load_memory, save_memory, add_fact, extract_remember, build_system_prompt,
    forget_last as mem_forget_last, summarize as mem_summarize,
    consolidate as memory_consolidate, core_facts, relevant_facts,
)
import convo
from overlay import Overlay, State
from commands import (
    clean_for_speech, wants_brain, wants_recap, wants_forget,
    wants_lookup, wants_radar, wants_visual,
    wants_repeat, wants_tidy_memory, wants_speed, wants_close, is_computer_task,
    wants_key_setup, parse_offline_fact,
    wants_live_off,
    strip_address,
    bump_usage, remaining_today, is_quota_error, is_transient_error, DAILY_LIMIT,
    thinking_budget_for, cap_spoken_budget, THINK_OFF, THINK_DYNAMIC, THINK_TYPED,
    usage_warning, split_for_tts, sounds_like_cant, is_actionable, needs_goal_check,
    parse_voice_switch, VOICES, task_label, parse_music, is_simple_request,
    promises_future_work, is_pushback,
)
import metrics
import audio_out
import brain
import providers
import context
import deck
import ears as ears_mod
import live as live_mod
import canvas
import chrome
import hands
import claude_bridge
import agent
import skills
import banter
import hud
from sentinel import Sentinel
from claude_bridge import ClaudeBridge

import Quartz
from PyObjCTools import AppHelper
from Cocoa import NSApplication, NSObject, NSTimer


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
API_KEY = os.getenv("GEMINI_API_KEY")

# Which model does which job now lives in providers.py, because pinning one id
# here is what left Neo on a two-generation-old model with a dead lite fallback
# 404ing every turn. MODEL is resolved at boot and re-resolved if it ever fails.
MODEL = os.getenv("NEO_MODEL", "gemini-2.5-flash")   # placeholder until resolved
STT_MODEL = os.getenv("NEO_STT_MODEL", "base.en")   # tiny.en is faster, base.en more accurate
DEFAULT_VOICE = "bm_fable"                          # British male — the JARVIS register
TTS_SPEED = float(os.getenv("NEO_SPEED", "0.92"))   # <1 = slower + clearer diction
# Optional cloud voice: Gemini's TTS is on the free tier but its daily cap is
# small and unpublished — so it's a switchable engine ("switch your voice to
# gemini"), NEVER the only path: any error falls straight back to Kokoro.
GEMINI_TTS_MODEL = os.getenv("NEO_GEMINI_TTS", "gemini-2.5-flash-preview-tts")
# ONE voice, and live.py owns it. These were two independent settings that
# happened to agree, and "happened to agree" is not a guarantee: the filler
# lines are synthesised here and the answers are spoken by the live socket, so
# the moment the two drifted the user heard one voice say "One sec." and a
# different one give the answer.
GEMINI_TTS_VOICE = os.getenv("NEO_GEMINI_VOICE") or live_mod.VOICE
# THE TESTS MUST NEVER TOUCH THE REAL ONE.
#
# test_hush.py cycles set_voice_mode() through all three modes to check every
# knob moves, and every cycle ends on "normal" — writing that into the real
# voice.json. go.sh then restarts Neo, which reads the file. So every commit
# quietly reset whatever the user had asked for: they set whisper at 22:43 on 10
# Sept, a commit went in at 22:48, and Neo came back normal. "Whisper mode
# doesn't stay on" was, for the third time, true — and this time it was the
# test suite un-setting it.
#
# Under NEO_NO_AUDIO (the test flag), preferences go to a throwaway file.
def _state_file(name):
    if os.getenv("NEO_NO_AUDIO") == "1":
        import tempfile
        return os.path.join(tempfile.gettempdir(), f"neo-test-{name}")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


VOICE_FILE = _state_file("voice.json")
# Where synthesised filler lines are kept between runs.
#
# Two problems, one fix. The lines have to be in the LIVE voice or they sound
# like a different person reading them — and synthesising a dozen of them
# through the cloud on every boot would be a dozen API calls before Neo can
# hear anything, which is the opposite of what boot needs. Synthesised once,
# written to disk, reused for the life of the machine.
VOICE_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               ".voicecache")
MIC_RATE = 16000                                    # whisper wants 16 kHz
TTS_RATE = 24000                                    # kokoro output rate
TTS_GAIN = float(os.getenv("NEO_GAIN", "2.1"))      # loudness boost (soft-limited, won't clip)
# WHISPER MODE. "talk quietly" / "whisper mode" drops the voice instead of
# changing it: a gain this far below TTS_GAIN is roughly thirteen decibels
# down, which is the difference between a room hearing Neo and only the user
# hearing them. Deliberately NOT done by prompting the cloud voice to whisper —
# a style instruction that the model reads out loud instead of obeying is a
# worse failure than being merely quiet, and they asked for either.
WHISPER_GAIN = float(os.getenv("NEO_WHISPER_GAIN", "0.5"))
# The live socket sends finished PCM, so there is no gain to change — only the
# samples. Lower than the local number because live audio arrives hotter.
WHISPER_LIVE_GAIN = float(os.getenv("NEO_WHISPER_LIVE_GAIN", "0.34"))
# A whisper is slower as well as quieter. Small on purpose: past about 10% the
# local voice stops sounding soft and starts sounding broken.
WHISPER_SPEED = float(os.getenv("NEO_WHISPER_SPEED", "0.94"))
# How long after Neo starts speaking the hush mic opens. The playback buffer is
# shallowest in the first moments of an answer, so a device open there is the
# one most likely to be heard as a stutter.
HUSH_OPEN_DELAY_S = float(os.getenv("NEO_HUSH_DELAY", "0.6"))

# BEDTIME MODE — the one that changes the MICROPHONE, not just the speaker.
#
# Whisper mode makes Neo quieter. Bedtime also makes Neo listen far closer: the
# room is dark, the house is asleep, and the user is breathing words rather than
# saying them. A whisper across a desk lands around 30 dB below ordinary
# speech, which is under the noise floor everything here is tuned to ignore.
#
# This only works BECAUSE it is a mode you ask for. Amplifying eight times and
# dropping the gate to near zero is exactly wrong in a normal room — every
# fridge hum and keyboard click becomes a turn. At night, with one person in
# the room, there is nothing else for it to hear.
BEDTIME_MIC_GAIN = float(os.getenv("NEO_BEDTIME_MIC_GAIN", "8.0"))
# The gate that decides "that was basically silence". MIC_GATE is 0.003; a
# whisper sits well below it, which is why whispering to Neo normally produces
# "the mic was basically silent" and nothing else.
BEDTIME_MIC_GATE = float(os.getenv("NEO_BEDTIME_MIC_GATE", "0.0004"))
# Quieter than whisper mode. If the point is not waking anyone, the answer has
# to be quieter than the question.
BEDTIME_GAIN = float(os.getenv("NEO_BEDTIME_GAIN", "0.34"))
BEDTIME_LIVE_GAIN = float(os.getenv("NEO_BEDTIME_LIVE_GAIN", "0.22"))
BEDTIME_SPEED = float(os.getenv("NEO_BEDTIME_SPEED", "0.92"))
# What it takes for a hold to count as speech in bedtime. live.turn_opens is
# strict on purpose — holding the key in silence must never become a turn,
# because the model answers silence by inventing what it heard — and all three
# of its tests reject a whisper. These are the same three numbers, moved.
BEDTIME_VOICE_BASE = float(os.getenv("NEO_BEDTIME_VOICE_BASE", "0.0006"))
BEDTIME_PEAK_RATIO = float(os.getenv("NEO_BEDTIME_PEAK_RATIO", "1.25"))
BEDTIME_VOICE_CHUNKS = int(os.getenv("NEO_BEDTIME_VOICE_CHUNKS", "5"))

VOICE_MODES = ("normal", "whisper", "bedtime")
MIN_SPEECH_SEC = 0.4                                # ignore accidental taps
# Loudness floor: below this an utterance is treated as silence (stops Whisper
# hallucinating words out of room noise). Tuned LOW on purpose — the built-in
# MacBook Air mic delivers normal speech around 0.009 rms, so the old 0.012
# threw away real talking. True silence sits near 0.0005, so 0.003 still
# separates cleanly.
MIC_GATE = float(os.getenv("NEO_MIC_GATE", "0.003"))
DEBUG = os.getenv("NEO_DEBUG") == "1"
# How long a turn captured before the models finished loading will wait for them
# rather than being discarded. Generous on purpose: waiting fifteen seconds and
# getting an answer beats a press that silently did nothing, and the ceiling
# only exists so a genuinely wedged warmup still ends with a spoken apology.
WARMUP_HOLD_S = float(os.getenv("NEO_WARMUP_HOLD", "45"))
# Anti-freeze ceilings. A lost fn key-up (the tap gets disabled by macOS and the
# UP edge fires while it's dead) would otherwise leave the recorder running
# forever — "stuck listening" with the mic indicator stuck on.
MAX_HOLD_SEC = float(os.getenv("NEO_MAX_HOLD", "120"))   # force-stop a hold past this
# Watchdog: how long a dead glow (listening + OS-confirmed key-UP + no job) must
# persist before we unstick it.
#
# This was 2s, and that was the bug the July anti-freeze work left behind. The
# whole safety argument for a short window was "the OS confirms the key is up,
# so a real hold is never cut" — but the OS reading was a single query of a flag
# that isn't reliable for the fn key (see ptt_physically_down). One bad sample
# and a live recording died 2s in. The log is full of it: dozens of unstick
# lines, and turns that captured 0.5s of a sentence the user was still speaking.
#
# The fix is on both sides: the reading below is now two independent OS views
# with "down wins", it has to say up several times in a row, and this window is
# wide enough that a real pause can never fall inside it.
STUCK_LISTEN_SEC = float(os.getenv("NEO_STUCK_LISTEN", "8"))
WATCHDOG_POLL_SEC = 1                                    # how often the watchdog checks
# A turn longer than this is not slow, it is WEDGED. The brain answers in
# seconds, a Claude job is fire-and-forget, and the longest legitimate turn
# measured here is well under a minute. Past this the watchdog stops describing
# the problem and fixes it — it used to log "it should time out shortly" every
# two minutes forever while Neo held the turn lock and every fn press did
# nothing.
STUCK_TURN_S = float(os.getenv("NEO_STUCK_TURN_S", "150"))
# How many consecutive "key is up" readings force a release. The guard timer
# below runs at 4 Hz, so 3 votes = ~0.75s of agreement — fast enough that a
# genuinely lost key-up still feels instant, strict enough that one bad sample
# can't cut a sentence in half.
FN_RELEASE_VOTES = int(os.getenv("NEO_FN_VOTES", "3"))
FN_GUARD_INTERVAL = 0.25

# How a press behaves. "live" is the default and the point of the product: tap
# the key and you're in a real conversation — talk, get answered in a beat, keep
# going, and it hangs up on its own when you stop. There is no mode to invoke
# and no phrase to remember, because a conversational assistant you have to
# switch into conversational mode is not a conversational assistant.
#
# "hold" is the old push-to-talk pipeline (hold, speak, release, wait). It stays
# as the automatic fallback whenever live can't run, and NEO_MODE=hold pins it.
MODE = (os.getenv("NEO_MODE") or "live").strip().lower()
# How long a live session sits open with nobody speaking before it hangs up.
# Short, because it now opens on every interaction rather than on request — an
# open mic you forgot about is the one thing worse than a slow reply.
LIVE_IDLE_SEC = float(os.getenv("NEO_LIVE_IDLE", "30"))
# How long a finished presentation may still be worth narrating. Past this it
# is answering a question the user has moved on from, which reads as Neo replying
# to something from ten minutes ago — because it is.
DECK_STALE_SEC = float(os.getenv("NEO_DECK_STALE", "150"))
# How many narration paragraphs go into one spoken turn, and how long to wait
# for one to finish before giving up on the rest.
DECK_CHUNK = int(os.getenv("NEO_DECK_CHUNK", "2"))
DECK_TURN_MAX_S = float(os.getenv("NEO_DECK_TURN_MAX", "75"))

# LOCAL MODE — Neo runs with no API key at all.
#
# This used to be sys.exit(1), and that one line was the worst thing in the
# product. The onboarding's "Skip for now" button and the README both promised
# that skipping the key still left you a working Neo "in a local voice"; both
# were false. Skip it and the app died on the next launch, silently, after the
# person had already installed Homebrew, waited through a model download and
# granted four macOS permissions. The escape hatch was a trap, at the exact
# moment someone decides whether this thing is real.
#
# It was never true that Neo needs a key to be useful. Speech in is
# faster-whisper, on device. Speech out is Kokoro, on device. Every fixed
# phrase in commands.py is pure Python. Timers, reminders, the calendar,
# Contacts, the clipboard, opening apps and files, music, volume, memory —
# all macOS, no network. Markets, crypto, scores and the weather are keyless
# public endpoints. That is a genuinely useful assistant, and it works thirty
# seconds after the app opens, with no account, no signup and no key.
#
# The one thing it cannot do is THINK: no free-form conversation, no vision,
# no decks. So local mode says exactly that, in one sentence, and offers the
# route — see LocalBrain.respond and commands.wants_key_setup.
KEYLESS = (not API_KEY) or API_KEY == "paste_your_free_key_here"
if KEYLESS:
    print("[neo] No Gemini API key — starting in LOCAL MODE.")
    print("[neo] I can hear you and talk back, tell you the time and the "
          "weather, and open your apps. Thinking needs a free key: "
          "say \"add my key\".")


def log(msg):
    """Timestamped line. Goes to the console, and to neo.log when run via LaunchAgent."""
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _now_tag():
    """Hidden per-turn tag giving Neo the real local date and time.

    Reads the machine's own timezone rather than a hardcoded one, so it stays
    right when the clocks change or the user travels. context.py owns this now so
    the live path and this one can't drift apart — they did, and the live path
    ended up with no clock at all and answered four hours off in UTC."""
    try:
        return f"[[now: {context.spoken_time()}]] "
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# Speech models (loaded once at startup)
# --------------------------------------------------------------------------- #
def _voice_prefs():
    try:
        with open(VOICE_FILE) as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def _save_voice_prefs(**kw):
    prefs = _voice_prefs()
    prefs.update(kw)
    try:
        with open(VOICE_FILE, "w") as f:
            json.dump(prefs, f)
    except OSError:
        pass


def _load_voice():
    """Voice choice: env NEO_VOICE > voice.json (set by 'switch your voice
    to ...') > DEFAULT_VOICE."""
    return os.getenv("NEO_VOICE") or _voice_prefs().get("voice") or DEFAULT_VOICE


def _load_engine():
    """Which engine speaks when the live socket ISN'T the one talking.

    Defaults to the cloud voice now, not the local one. Everything Neo says
    through a live session is Charon; everything it said any other way — a card
    the user tapped "tell me" on, a finished Claude job, a timer — came out as
    bm_fable, which is the "robotic old Jarvis" they keeps hearing. Same
    assistant, two different people, depending on a detail they cannot see.

    Falling back is already handled inside Speech.synth: any cloud failure,
    including the daily quota running out, trips the breaker and Kokoro takes
    over mid-reply. So this costs nothing on a bad day and fixes the voice on
    a good one.
    With no key there is no cloud voice to default to, and asking for one
    would spend the whole first turn failing over to Kokoro anyway. Local mode
    starts where it is going to end up.
    """
    if KEYLESS:
        return "kokoro"
    return os.getenv("NEO_TTS") or _voice_prefs().get("engine") or "gemini"


# The one source of truth for how loudly Neo is speaking. Module level because
# Speech.synth needs it and has no handle on the Neo instance — passing one in
# would mean threading it through every synthesis call for a single boolean.
_VOICE_MODE = {"mode": "normal"}


def _whisper_now():
    """Both quiet modes slow the local voice slightly."""
    return _VOICE_MODE["mode"] in ("whisper", "bedtime")


def clean_mode(mode):
    """Any spelling of a mode name -> one of VOICE_MODES. Pure."""
    m = str(mode or "").strip().lower()
    if m.startswith("bed") or m.startswith("night") or m.startswith("sleep"):
        return "bedtime"
    if m.startswith("whis") or m.startswith("quiet"):
        return "whisper"
    return "normal"


# How long a quiet mode outlives the moment it was asked for.
#
# Both previous versions of this were wrong in opposite directions. Persisting
# it forever meant one "bedtime mode" left Neo whispering on every boot for
# days. Not persisting it at all meant the mode evaporated on the next code
# change — and Neo restarts itself several times an hour, invisibly, so
# "whisper mode" would work for ninety seconds and then quietly stop. That is
# what the user hit: it genuinely did not whisper, because by the time they spoke
# again Neo had restarted into normal.
#
# The honest shape is an EXPIRY. A quiet mode means "the room is quiet right
# now", so it should survive a restart thirty seconds later and be gone by
# morning. Four hours does both.
VOICE_MODE_TTL_H = float(os.getenv("NEO_VOICE_MODE_TTL_H", "4"))


def _load_voice_mode(now=None, prefs=None):
    """The saved mode if it is still fresh, else "normal". Pure given prefs."""
    env = os.getenv("NEO_VOICE_MODE")
    if env:
        return clean_mode(env)
    prefs = _voice_prefs() if prefs is None else prefs
    mode = clean_mode(prefs.get("voice_mode") or "normal")
    if mode == "normal":
        return "normal"
    try:
        set_at = float(prefs.get("voice_mode_at"))
    except (TypeError, ValueError):
        return "normal"          # no timestamp: treat it as stale, not sticky
    now = time.time() if now is None else now
    if now - set_at > VOICE_MODE_TTL_H * 3600:
        return "normal"
    return mode


def _trim_tail_silence(a, thresh=4e-3, keep=int(0.05 * TTS_RATE)):
    """Kokoro pads segments with a VARIABLE amount of trailing silence — cut
    it so OUR fixed gap (below) sets the pacing, giving speech an even,
    unhurried rhythm instead of random run-ons and dead air."""
    if a.size == 0:
        return a
    loud = np.flatnonzero(np.abs(a) > thresh)
    if loud.size == 0:
        return a[:0]
    return a[:min(a.size, int(loud[-1]) + keep)]


class Speech:
    def __init__(self):
        self.stt = None
        self.ears = None            # set in warmup(), once the client exists
        self.voice = _load_voice()
        self.engine = _load_engine()   # "kokoro" (local, always works) | "gemini"
        self.gemini_client = None      # bound after the brain boots
        self._gemini_dead = False      # tripped on first failure -> local voice
        self._tts_key_rest = {}        # key -> when to try it again (TTS only)
        self._pipes = {}  # kokoro pipeline per accent ('a' American, 'b' British)
        self._pipe_lock = threading.Lock()   # _pipe is built from two threads now
        self.voice_ready = threading.Event()  # local voice finished loading
        # (voice, phrase) -> audio, for millisecond acks. Keyed by VOICE
        # because the filler and the answer must come from the same mouth.
        self.cache = {}

    def _pipe(self, lang_code):
        """The one place a Kokoro pipeline is built. Locked because the voice now
        loads on a background thread while Neo is already listening — so the
        first reply and the warmup can arrive here at the same moment, and
        building two pipelines for one voice wastes six seconds and a lot of
        memory. Double-checked so the common case never takes the lock's cost."""
        if lang_code in self._pipes:
            return self._pipes[lang_code]
        with self._pipe_lock:
            if lang_code not in self._pipes:
                from kokoro import KPipeline
                self._pipes[lang_code] = KPipeline(lang_code=lang_code)
        return self._pipes[lang_code]

    def warmup(self):
        """Get the EARS up, and only the ears. Everything else waits behind them.

        Measured, on this machine: importing faster_whisper 3.1s, importing
        kokoro 3.0s, the Whisper model 0.5s, the Kokoro pipeline 3.3s. Nearly
        ten seconds, of which the ears are under four — and Neo cannot hear a
        word until the whole thing finishes. Running the two in parallel threads
        does NOT help: it is import work, so the GIL serialises it anyway
        (tried it; boot stayed at ten seconds).

        What actually helps is noticing that the mouth is not on the critical
        path. Nothing can need the voice until Neo has heard a question and
        thought about an answer, which is seconds away at the very least. So the
        voice loads on a background thread and Neo is listening at ~4s instead
        of ~10s. _pipe is locked, and whoever speaks first simply blocks there
        until it is ready — normally not at all.
        """
        log("Warming up speech models (first run downloads them)...")
        from faster_whisper import WhisperModel
        self.stt = WhisperModel(STT_MODEL, device="cpu", compute_type="int8")

        def _load_voice():
            try:
                self._pipe(self.voice[0])
            except Exception as e:
                log(f"voice {self.voice} unavailable ({e}) — falling back to af_heart")
                self.voice = "af_heart"
                try:
                    self._pipe("a")
                except Exception as e2:
                    log(f"local voice failed entirely ({e2}) — cloud voice only.")
                    self.voice_ready.set()
                    return
            log(f"Voice: {self.voice} at {TTS_SPEED}x. Say 'switch your voice to "
                "george, emma, heart, bella...' to change it.")
            self.voice_ready.set()

        threading.Thread(target=_load_voice, daemon=True,
                         name="neo-voice-warm").start()

        # The filler lines are NOT built here. They need the Gemini client,
        # which the brain owns and which does not exist yet at warmup time.
        # neo.py calls _recache() once it has bound the client.
        log("Ready. Hold fn, talk, let go — Neo answers the moment you release.")

    def attach_ears(self, client, facts=()):
        """Give the cloud transcription path its client. Separate from warmup()
        because the brain (and therefore the client) doesn't exist yet at that
        point, and the local model has to be ready first so there is always a
        floor to fall back to."""
        try:
            self.ears = ears_mod.Ears(
                client=client,
                local=lambda a: self._transcribe_local(a)[0],
                log=log,
                hints=ears_mod.build_hints(facts))
        except Exception as e:
            log(f"cloud transcription unavailable ({e}); on-device only.")
            self.ears = None

    def set_voice(self, voice_id):
        """Switch the voice live and persist it across restarts. False if the
        new voice's pipeline won't load (keeps the current one)."""
        try:
            self._pipe(voice_id[0])
        except Exception as e:
            log(f"voice {voice_id} failed to load: {e}")
            return False
        self.voice = voice_id
        self.engine = "kokoro"
        _save_voice_prefs(voice=voice_id, engine="kokoro")
        # The ack cache is NOT cleared. It is keyed by the live voice, not by
        # this one: 'switch your voice to george' changes the voice Kokoro
        # reads answers with on the fallback path, and has nothing to do with
        # the line played while a live tool runs. Clearing it here is what
        # used to leave Neo with no filler at all until a slow re-synthesis
        # finished.
        return True

    def set_engine(self, engine):
        """'gemini' = cloud voice (free tier, small daily cap, auto-falls back
        to Kokoro on any error); 'kokoro' = the local voice."""
        self.engine = engine
        self._gemini_dead = False
        _save_voice_prefs(engine=engine)

    # ---- the filler lines, in the voice that answers ------------------- #
    # These are the "One sec." / "Let me check." lines Neo plays the instant a
    # tool starts. They used to be synthesised with KOKORO while the answer
    # that followed came out of the live socket in Gemini's voice — two
    # different people in one exchange, and the local one is markedly the
    # older, flatter of the two. That is the "weird old robotic voice".
    #
    # So they are synthesised through Gemini's TTS in the SAME voice the live
    # session speaks with, and written to disk so the cost is paid once ever
    # rather than once per boot.
    def _ack_path(self, text, voice):
        import hashlib
        key = hashlib.sha1(f"{voice}\x00{text}".encode("utf-8")).hexdigest()[:20]
        return os.path.join(VOICE_CACHE_DIR, f"{key}.f32")

    def _ack_from_disk(self, text, voice):
        try:
            with open(self._ack_path(text, voice), "rb") as f:
                a = np.frombuffer(f.read(), dtype=np.float32)
            return a if a.size else None
        except OSError:
            return None

    def _ack_to_disk(self, text, voice, audio):
        try:
            os.makedirs(VOICE_CACHE_DIR, exist_ok=True)
            path = self._ack_path(text, voice)
            # Written beside the target and renamed, so a boot interrupted
            # mid-write can never leave a truncated file that then loads as a
            # burst of noise for the rest of the machine's life.
            tmp = path + ".part"
            with open(tmp, "wb") as f:
                f.write(np.asarray(audio, dtype=np.float32).tobytes())
            os.replace(tmp, path)
        except OSError as e:
            log(f"[voice] couldn't cache a line: {e}")

    def cached_phrases(self, phrases, voice=None):
        """Which of these already exist on disk in Neo's real voice.

        This is what stops them sounding like two different people. Only a few
        lines per bucket are ever synthesised through the cloud voice, so
        choosing a filler without checking meant most of them fell through to
        the local one — see banter.pick.
        """
        voice = voice or GEMINI_TTS_VOICE
        ready = set()
        for p in phrases or ():
            if p in self.cache or os.path.exists(self._ack_path(p, voice)):
                ready.add(p)
        return ready

    def ack_audio(self, text, voice=None):
        """One filler line, ready to play. None if it isn't available.

        None on purpose, rather than falling back to the local voice: a filler
        in the wrong voice is worse than no filler at all. Silence for a beat
        reads as thinking; a stranger's voice reads as broken.
        """
        voice = voice or GEMINI_TTS_VOICE
        hit = self.cache.get((voice, text))
        if hit is not None:
            return hit
        disk = self._ack_from_disk(text, voice)
        if disk is not None:
            self.cache[(voice, text)] = disk
            return disk
        return None

    def _synth_into(self, target, phrases, voice=None):
        """Make the missing lines, slowly, in the live voice.

        SLOWLY is the point. Firing every phrase at once produced a wall of
        `429 RESOURCE_EXHAUSTED` in the log — 50 of 53 lines never got made,
        and every one of those fell back to the local voice, which is the
        wrong-voice problem the user kept hearing. The free TTS tier is a rate,
        not a wall: spaced out, the same requests succeed.

        Nothing here is on the critical path, so waiting costs nothing. And
        because each line is written to disk the moment it exists, a run that
        only gets halfway still leaves those lines made forever.
        """
        voice = voice or GEMINI_TTS_VOICE
        gap = float(os.getenv("NEO_VOICE_GAP", "4"))
        backoff = gap
        # A DAY quota does not clear by waiting a minute. The fill used to
        # retry regardless, so an exhausted allowance meant a 429 in the log
        # every sixty seconds until midnight — for lines that could not be
        # made today no matter how many times it asked.
        self._tts_dry = False
        for t in phrases:
            if getattr(self, "_tts_dry", False):
                log("[voice] the day's TTS allowance is gone — the rest of "
                    "the cache fills tomorrow. Nothing sounds wrong "
                    "meanwhile; Neo only ever uses lines that are ready.")
                break
            if self._ack_from_disk(t, voice) is not None:
                continue                    # already paid for, on a previous run
            audio = self._gemini_tts(t, voice=voice)
            if audio is None:
                # Almost always a 429. Back off and keep going — the next boot
                # retries whatever is still missing, and the cache only fills.
                backoff = min(backoff * 2, 60.0)
                time.sleep(backoff)
                continue
            backoff = gap
            target[(voice, t)] = audio
            self._ack_to_disk(t, voice, audio)
            time.sleep(gap)

    def _recache(self):
        """Make sure every filler line exists on disk, in the live voice.

        Nothing blocks boot any more. Lines already on disk cost a stat call;
        lines that are missing are synthesised on a background thread, and
        until one exists Neo simply doesn't say it. That is the whole reason
        this moved off the critical path: it used to synthesise a dozen lines
        before Neo could hear anything, and Neo not being able to hear you is
        the one thing worse than Neo not having a filler ready.
        """
        # Stamp the cache with the voice it was built for. If the voice changes
        # while the fill runs, its audio was synthesised in the OLD voice and
        # merging it gives the user a stranger reading the filler lines. Discard
        # instead of merge.
        self._voice_gen = getattr(self, "_voice_gen", 0) + 1
        gen = self._voice_gen
        built_for = GEMINI_TTS_VOICE

        # Anything already on disk is instantly available through ack_audio(),
        # so the only work left is the lines that have never been made.
        want = [p for p in banter.all_phrases()
                if self._ack_from_disk(p, built_for) is None]
        if not want:
            log(f"Voice lines ready ({built_for}, from cache).")
            return

        def _fill():
            fresh = {}
            self._synth_into(fresh, want, voice=built_for)
            if gen != self._voice_gen:
                log(f"voice changed while caching — discarding "
                    f"{len(fresh)} stale lines.")
                return
            self.cache = {**self.cache, **fresh}   # swap, never mutate in place
            if len(fresh) < len(want):
                log(f"[voice] {len(want) - len(fresh)} filler line(s) couldn't "
                    f"be synthesised — Neo stays quiet on those rather than "
                    f"using the local voice.")
            log(f"Voice lines complete: {len(fresh)} new in {built_for}.")

        threading.Thread(target=_fill, daemon=True, name="neo-voice-cache").start()

    def _tts_clients(self, now=None):
        """(key, client) for every key that could synthesise, best first.

        BOTH free-tier TTS quotas are scoped PerProjectPerModel — the ten-a-day
        one and the per-minute one — so a second key in a second project is a
        second full allowance, not a shared one. That is the entire reason to
        hold more than one.

        A key that just hit its per-minute wall is sorted to the BACK rather
        than dropped, so the next phrase doesn't spend a round trip proving the
        same thing again, but a key is never made unreachable by this.

        The cooldown is local to TTS on purpose. providers.retire_key() would
        stand the key down for chat too, and chat is a different model with a
        different quota — a TTS rate limit says nothing about it.
        """
        import time as _t
        now = _t.time() if now is None else now
        out = []
        try:
            import providers
            from google import genai
            keys = providers.gemini_keys()
        except Exception:
            return [(None, self.gemini_client)] if self.gemini_client else []
        first = keys[0] if keys else None
        for key in keys:
            if key == first and self.gemini_client is not None:
                out.append((key, self.gemini_client))     # reuse the brain's
            else:
                try:
                    out.append((key, genai.Client(api_key=key)))
                except Exception:
                    pass
        if not out and self.gemini_client is not None:
            return [(None, self.gemini_client)]
        rest = getattr(self, "_tts_key_rest", {})
        out.sort(key=lambda kc: rest.get(kc[0], 0.0) > now)   # stable: resting last
        return out

    def _gemini_tts(self, text, voice=None):
        """Whole-reply synthesis via Gemini's TTS (24 kHz PCM16). None on any
        failure so the caller falls back to Kokoro."""
        # NOT gated on self.gemini_client any more. That is only the brain's
        # key; the other keys stand on their own, and refusing to use them
        # because the first one is missing is the same mistake as the break
        # below used to be.
        clients = self._tts_clients()
        if not clients:
            return None
        last = None
        # "Every key that answered said no more today" — the ONLY thing that
        # justifies giving up for the day.
        all_daily = bool(clients)
        for key, client in clients:
            audio, err = self._tts_once(text, voice, client)
            if audio is not None:
                return audio
            last = err
            try:
                import providers
                daily = providers.is_daily_quota(err)
            except Exception:
                daily = False
            if not daily:
                all_daily = False
            # KEEP GOING. This used to break on anything that wasn't a per-DAY
            # quota, on the theory that "the next key would hit the same wall a
            # second later". It would not: Google's own error names the scope,
            # GenerateRequestsPerMinutePerProjectPerModel — per PROJECT. A
            # second key in a second project has its own per-minute allowance,
            # and an empty response from one key says nothing about another.
            # 58 of the 94 quota failures in neo.log were per-minute, and every
            # one of them stopped here with a fully stocked second key unused.
            self._rest_tts_key(key, daily)
        # "Stop for the day" is ONLY for a day quota. A one-off — an empty
        # response, a dropped connection — is not a reason to give up on the
        # remaining lines, and treating it as one left the cache half made
        # with nothing wrong.
        if last is not None:
            # Only stop for the day when EVERY key is out for the day. One key
            # being dry is a reason to use the other one, not a reason to stop.
            if all_daily:
                self._tts_dry = True
            log(f"gemini tts failed on all {len(clients)} key(s): "
                f"{str(last)[:160]}")
        return None

    def _rest_tts_key(self, key, daily, now=None):
        """Sort a key that just failed to the back of the queue for a while.
        Never removes it — with one key, back of the queue is still the front."""
        import time as _t
        if not key:
            return
        if not hasattr(self, "_tts_key_rest"):
            self._tts_key_rest = {}
        now = _t.time() if now is None else now
        # A day quota won't clear before midnight; a per-minute one clears in
        # about a minute. Neither is a reason to forget the key exists.
        self._tts_key_rest[key] = now + (3600.0 if daily else 75.0)

    def _tts_once(self, text, voice, client):
        """One attempt on one key. (audio, error) — exactly one is None."""
        try:
            cfg = types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name=voice or GEMINI_TTS_VOICE))))
            bump_usage()
            r = client.models.generate_content(
                model=GEMINI_TTS_MODEL, contents=text, config=cfg)
            cand = (getattr(r, "candidates", None) or [None])[0]
            content = getattr(cand, "content", None) if cand else None
            parts = getattr(content, "parts", None) if content else None
            if not parts:
                # A refusal or an empty turn. Real, occasional, and NOT a
                # reason to stop synthesising the rest of the cache.
                return None, "no audio came back"
            data = parts[0].inline_data.data
            a = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            return (a, None) if a.size else (None, "empty audio")
        except Exception as e:
            return None, e

    def transcribe(self, audio_f32):
        """Returns (text, confident).

        Cloud ears first, on-device always as the floor — see ears.py. This used
        to be faster-whisper base.en and nothing else, which is how "the user
        Lee" reached the brain as "Sam Leigh" and a quiet room became a
        sentence of invented dialogue. A cloud model also takes a hint list, so
        Neo's own vocabulary (their products, the people they talk about) biases
        the recogniser instead of being guessed at phonetically.

        Confidence is only meaningful for the local path — the cloud models
        don't return logprobs — so a cloud transcript is trusted. It has earned
        that: the failure mode we're correcting is confident nonsense from the
        small local model, not hedged output from a large one.
        """
        if self.ears is not None:
            text = self.ears.transcribe(audio_f32, MIC_RATE)
            if self.ears.last_path != "local":
                return text, True
            if text:
                return text, True
        return self._transcribe_local(audio_f32)

    def _transcribe_local(self, audio_f32):
        """faster-whisper on the CPU. The offline floor, and the thing whose
        output has to be treated with suspicion."""
        segments, _ = self.stt.transcribe(
            audio_f32,
            language="en",
            vad_filter=True,
            beam_size=1,
            condition_on_previous_text=False,   # don't invent text from context
            no_speech_threshold=0.6,            # trust Whisper's own silence detector
        )
        kept, logprobs = [], []
        for s in segments:
            # drop segments Whisper itself thinks are probably silence
            if getattr(s, "no_speech_prob", 0.0) > 0.6:
                continue
            kept.append(s.text)
            lp = getattr(s, "avg_logprob", None)
            if lp is not None:
                logprobs.append(lp)
        text = " ".join(kept).strip()
        floor = float(os.getenv("NEO_STT_MINCONF", "-1.0"))
        confident = (not logprobs) or (sum(logprobs) / len(logprobs) >= floor)
        return text, confident

    def synth(self, text):
        """Yield (audio_chunk_f32, rms) for the whole reply, IN ORDER.
        Three prosody fixes live here (the 'mumbling' fixes):
        - long text is fed to Kokoro in sentence-packed chunks under its
          ~500-char prosody cliff (split_for_tts),
        - each segment's ragged trailing silence is trimmed,
        - a consistent 0.22s breath is inserted BETWEEN segments,
        - and TTS_SPEED (default 0.95) slows diction slightly for clarity."""
        # Cloud voice first if chosen — whole reply, one call; ANY failure
        # (quota, network, SDK) trips the breaker and Kokoro takes over.
        if self.engine == "gemini" and not self._gemini_dead:
            audio = self._gemini_tts(text)
            if audio is not None:
                step = TTS_RATE // 2
                for i in range(0, audio.size, step):
                    a = audio[i:i + step]
                    yield a, float(np.sqrt(np.mean(a ** 2))) if a.size else 0.0
                return
            self._gemini_dead = True
            log("gemini voice unavailable — switching to the local voice")
        pipe = self._pipe(self.voice[0])
        gap = np.zeros(int(TTS_RATE * 0.22), dtype=np.float32)
        first = True
        for piece in split_for_tts(text):
            speed = TTS_SPEED * (WHISPER_SPEED if _whisper_now() else 1.0)
            for _, _, audio in pipe(piece, voice=self.voice, speed=speed):
                a = _trim_tail_silence(np.asarray(audio, dtype=np.float32).reshape(-1))
                if not a.size:
                    continue
                # KOKORO'S GAIN, APPLIED TO KOKORO. This used to live in
                # _speak, applied to whatever synth returned — which was fine
                # while synth only ever returned Kokoro, and became a hard
                # soft-clip the day the default engine switched to Gemini:
                # cloud audio already peaks near 0.85, so tanh(0.85 * 2.1)
                # flattens everything above 0.4 into a wall. That is the
                # "completely unintelligible, breaking up" card voice. The
                # boost exists because Kokoro is quiet; so it is applied here,
                # to Kokoro, and to nothing else.
                a = np.tanh(a * TTS_GAIN)
                if not first:
                    yield gap, 0.0
                first = False
                yield a, float(np.sqrt(np.mean(a ** 2)))


# --------------------------------------------------------------------------- #
# Neo brain — one persistent chat for conversational continuity
# --------------------------------------------------------------------------- #
class BrainError(Exception):
    """The brain (or a tool it called) hard-failed — network/tool error, not a
    refusal. Signals _handle to escalate the ORIGINAL ask to Claude instead of
    shrugging 'something glitched' and dropping a real request on the floor."""


def _history_from_turns(turns):
    """Saved conversation exchanges -> Gemini chat history, so the thread survives
    a restart. Guarded: a bad Content shape must never stop the brain booting."""
    out = []
    for t in turns:
        try:
            role = "user" if t.get("role") == "user" else "model"
            text = t.get("text", "")
            if text:
                out.append(types.Content(role=role, parts=[types.Part(text=text)]))
        except Exception:
            continue
    return out


class LocalBrain:
    """The brain on an install with no key. Duck-types Brain for the handful of
    things the chain actually touches, and is honest about the one thing it
    cannot do.

    It is not a stub that errors. Everything reaching this object has already
    fallen past every fixed phrase in the chain above — the timers, the
    reminders, the calendar, the markets, the Mac control — so by definition
    what is left is a question that needs a model. There is no model. Saying so
    in one sentence, with the route out, is the whole job.

    `client` is None on purpose rather than absent: the boot path hands it to
    providers.warm, attach_ears, agent.bind and skills.set_ctx, all of which
    take a client of None and fall back to their local rung.
    """

    client = None
    model = None
    heavy_model = None

    def __init__(self, skills_desc=""):
        self.mem = load_memory()
        self.skills_desc = skills_desc
        self._turns = convo.load()
        self.last_show = None
        self.last_visual = None
        self._last_ts = None

    # -- the one that matters -------------------------------------------------
    def respond(self, user_text, job_note="", typed=False):
        # EXACTLY what is routed without a model, and nothing more. The first
        # version of this sentence listed reminders, the calendar and the
        # markets — none of which work keyless, because remind.parse and
        # agenda.parse both take a client and every tool is reached through the
        # model's tool-calling. Neo claiming a capability it does not have, in
        # the one message whose entire job is being honest about what it cannot
        # do, is the worst possible place to break rule two.
        return ("I can hear you and talk back, tell you the time and the "
                "weather, open your apps and websites, and remember things. "
                "Thinking about that one needs a free key. Say "
                "\"add my key\" and I'll walk you through it, it takes "
                "about a minute.")

    # -- things the chain calls, answered without a model ---------------------
    def forget(self):
        return ("Tell me what to forget and I'll drop it. Say \"forget about\" "
                "and then the thing.")

    def recap(self):
        n = len(self.mem.get("facts", []))
        if not n:
            return "I don't know anything about you yet. Tell me something and I'll keep it."
        return (f"I'm holding {n} thing{'s' if n != 1 else ''} about you. "
                "Ask me to forget any of them.")

    def visualize(self, text):
        return False          # drawing needs a model

    def lookup(self, text):
        return None           # grounded search needs a model

    def delivered(self, text, reply):
        return False

    def polish_result(self, *a, **k):
        # No model to polish with, so hand back what the tool actually said
        # rather than nothing. A tool's own sentence is already written to be
        # spoken; polishing improves it, it is not required for it to work.
        for cand in a:
            if isinstance(cand, str) and cand.strip():
                return cand
        return ""

    def next_key(self, err=None):
        return False          # nowhere to rotate to

    def _gen(self, prompt):
        return None


class Brain:
    def __init__(self, skills_desc=""):
        # HARD TIMEOUT on every Gemini call. Without it, one wedged HTTPS
        # connection hangs the worker thread forever: fn still glows, speech
        # queues behind the stuck job, and Neo looks crashed while "listening".
        self._build_client()
        self.mem = load_memory()
        self.skills_desc = skills_desc  # what's loaded, so the brain USES them
        self._fast_model = None         # resolved lazily; False = none work
        # CONVERSATION CONTINUITY across restarts: reseed the chat with the last
        # few exchanges so "any update on that?" survives a relaunch. This is the
        # running conversation, NOT the fact dump — facts come in per-turn below.
        self._turns = convo.load()
        # for_replay here too: this chat object is built ONCE at boot and
        # never rebuilt, so an orphan seeded now sits in its context for the
        # whole life of the process.
        history = _history_from_turns(convo.for_replay(self._turns))
        # what's already in the always-on system prompt, so per-turn recall
        # doesn't repeat it
        self._core = {(f.get("text", "") if isinstance(f, dict) else str(f))
                      for f in core_facts(self.mem.get("facts", []))}
        self.last_show = None    # structured spec attached to the last reply, if any
        self.last_visual = None  # freeform [[visual: ...]] request from the last reply
        # Which model runs the conversation. Resolved rather than pinned: see
        # providers.py for why a hardcoded id is how Neo ended up two
        # generations behind with a dead fallback 404ing every turn.
        global MODEL
        provider, model = providers.resolve("chat", self.client, log)
        if model:
            MODEL = model
        self.model = MODEL
        self.heavy_model = None      # resolved lazily; the ladder rarely needs it

        # The agent loop: Gemini gets Neo's toolbox and may chain tool calls
        # (search -> read -> check numbers -> act) before answering. NEO_AGENT=0
        # falls back to plain chat if tool-calling ever misbehaves.
        # The standing brief goes to BOTH paths — see context.py. Assembling
        # this knowledge separately per path is what let the live session end up
        # with no clock at all.
        cfg = {"system_instruction": (build_system_prompt(self.mem)
                                      + self.skills_desc
                                      + "\n\n" + context.brief())}
        # THINKING is now decided per turn in respond(), not switched off for the
        # whole session. Killing it globally to save two seconds is what made a
        # reasoning model answer like it couldn't reason. NEO_THINK=0 forces the
        # old always-off behaviour back if latency ever matters more.
        if os.getenv("NEO_AGENT") != "0":
            cfg["tools"] = agent.TOOLS
            try:
                cfg["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(
                    maximum_remote_calls=8)
            except Exception:
                pass   # older SDKs: automatic calling is on by default anyway
        # Kept so a per-turn config can be rebuilt WITHOUT losing the system
        # prompt or the toolbox: send_message(config=...) replaces the
        # create-time config rather than merging into it, so anything not
        # repeated here would silently vanish for that turn.
        self._base_cfg = dict(cfg)
        try:
            self.chat = self.client.chats.create(
                model=MODEL, config=types.GenerateContentConfig(**cfg),
                history=history)
        except Exception as e:
            # never lose the TOOLS to a thinking-config quirk: retry without
            # the latency tweak first, and only then fall back to plain chat
            cfg.pop("thinking_config", None)
            try:
                self.chat = self.client.chats.create(
                    model=MODEL, config=types.GenerateContentConfig(**cfg),
                    history=history)
                log(f"thinking-off unsupported ({e}); tools kept.")
            except Exception as e2:
                log(f"agent tools unavailable ({e2}); falling back to plain chat.")
                self.chat = self.client.chats.create(
                    model=MODEL,
                    config=types.GenerateContentConfig(
                        system_instruction=build_system_prompt(self.mem) + self.skills_desc),
                    history=history)

    def _build_client(self):
        """A client on whichever Gemini key still has quota today."""
        keys = providers.live_keys()
        self.key = keys[0] if keys else API_KEY
        try:
            self.client = genai.Client(
                api_key=self.key,
                http_options=types.HttpOptions(timeout=45_000))   # ms
        except Exception:
            self.client = genai.Client(api_key=self.key)

    def next_key(self, err=None):
        """This key is dry. Stand it down and rebuild on the next one.

        Returns True when there was somewhere else to go. The live VOICE is
        the reason this exists: nothing else free does speech-to-speech, so
        when the project running it hits its daily wall the only way to keep
        Neo sounding like Neo is another project's allowance.
        """
        if err is not None and not providers.is_daily_quota(err):
            return False
        before = getattr(self, "key", None)
        providers.retire_key(before)
        keys = providers.live_keys()
        if not keys or keys[0] == before:
            log("[keys] every Gemini key is out of quota for today.")
            return False
        self._build_client()
        live, down = providers.key_state()
        log(f"[keys] that key is dry — switched to the next one "
            f"({live} live, {down} resting).")
        # The resolver's per-model notes belong to the OLD project's quota.
        providers.forget()
        return True

    def turn_config(self, budget):
        """The create-time config plus this turn's thinking budget, in the shape
        THIS model accepts.

        Not every model speaks the same dialect: the 2.x family takes
        thinking_budget and rejects thinking_level, the flash-lite models are
        the other way round, and sending the wrong one is a 400 on every single
        call. providers.probe_style() finds out once and caches it; if it can't
        (model busy, SDK too old), we send no thinking config at all rather than
        a guess, because inheriting the default is always safe.
        """
        if budget is None:
            return None
        style = providers.probe_style(self.client, MODEL, log)
        kwargs = providers.thinking_kwargs(style, budget) if style else None
        if not kwargs:
            return None
        cfg = dict(self._base_cfg)
        try:
            cfg["thinking_config"] = types.ThinkingConfig(**kwargs)
        except Exception:
            return None       # SDK too old for this field: inherit defaults
        try:
            return types.GenerateContentConfig(**cfg)
        except Exception:
            return None

    def heavy(self, prompt):
        """One-shot on the strongest model available — the escalation route for
        questions the conversation model gets wrong. Falls back to the ordinary
        model rather than failing the turn."""
        if self.heavy_model is None:
            _p, m = providers.resolve("heavy", self.client, log)
            self.heavy_model = m or MODEL
        try:
            return self.client.models.generate_content(
                model=self.heavy_model, contents=prompt)
        except Exception as e:
            log(f"heavy model {self.heavy_model} failed ({e}); using {MODEL}")
            providers.report_failure("heavy", "gemini", self.heavy_model, log)
            self.heavy_model = MODEL
            return self.client.models.generate_content(model=MODEL, contents=prompt)

    def _gen(self, prompt, fast=False):
        """One-shot generation. fast=True routes to the cheapest model that can
        form a sentence — polishing a tool result needs no reasoning.

        The old version kept its own candidate list and its own probe loop here;
        that's providers.py's job now, so a retired id is handled in one place
        instead of two. On failure the pick is cleared and the next call
        re-resolves rather than repeating a dead model."""
        if fast:
            if self._fast_model is None:
                _p, m = providers.resolve("fast", self.client, log)
                self._fast_model = m or False
            if self._fast_model:
                try:
                    return self.client.models.generate_content(
                        model=self._fast_model, contents=prompt)
                except Exception as e:
                    log(f"fast model {self._fast_model} failed ({e}); "
                        "using the main model")
                    providers.report_failure("fast", "gemini", self._fast_model, log)
                    self._fast_model = None
        return self.client.models.generate_content(model=MODEL, contents=prompt)

    def respond(self, user_text, job_note="", typed=False):
        import time
        self.last_show = None
        self.last_visual = None
        # after a long silence, nudge Neo to greet like a person first
        gap = banter.gap_hint(getattr(self, "_last_ts", None))
        self._last_ts = datetime.datetime.now()
        # job_note tells Neo what Claude is doing RIGHT NOW, so a follow-up like
        # "make sure you don't change code" doesn't get answered as if nothing's
        # happening (that confusion is what made Neo feel oblivious).
        # Per-turn RECALL: surface only the facts that touch what they just said,
        # instead of carrying all of memory in every call. The core essentials
        # are already in the system prompt (excluded here so they don't repeat).
        rel = relevant_facts(self.mem.get("facts", []), user_text, exclude=self._core)
        recall = ""
        if rel:
            items = "; ".join(f.get("text", "") if isinstance(f, dict) else str(f)
                              for f in rel)
            recall = f"[[note: also relevant about the user — {items}]] "
        # HE'S LOOKING AT THE SCREEN, YOU AREN'T. When the user says something
        # didn't work, that's ground truth, not a debate — and the failure mode
        # is answering it with an unrelated detail ("I don't see anything" ->
        # "the date is off by a day"). Force a real check instead of a theory.
        push = ""
        if is_pushback(user_text):
            push = ("[[note: the user is telling you your last claim did NOT actually "
                    "happen. They are looking at the screen and you are not, so they are "
                    "right and you are wrong — do not argue, do not explain it away, "
                    "and do not pivot to some other detail. FIRST call look_at_screen "
                    "(or the matching check tool) to see what is ACTUALLY there, then "
                    "say plainly what you found and fix the real problem. If you never "
                    "actually did the thing, admit that in one sentence.]] ")
        # WHAT HE IS LOOKING AT. Not a screenshot and not the clipboard's
        # contents — a line naming what's there, so "read me this" resolves to
        # a tool call instead of "which thing do you mean?". See situation.py.
        here = ""
        try:
            import situation
            block = situation.line(text=user_text)
            if block:
                here = f"[[{block}]] "
        except Exception:
            pass
        msg = _now_tag() + gap + job_note + recall + push + here + user_text

        # How hard to think about THIS turn. "What time is it" gets none and
        # stays instant; "work out why the deploy failed" gets a dynamic budget
        # and is allowed the extra second.
        if os.getenv("NEO_THINK") == "0":
            budget = THINK_OFF
        else:
            # cap_spoken_budget is the hard ceiling on silence. Uncapped
            # thinking measured at 9.3 seconds on this key and produced a WRONG
            # answer anyway — a spoken turn never gets to spend that.
            budget = cap_spoken_budget(
                thinking_budget_for(user_text, has_job=bool(job_note),
                                    pushback=bool(push)))
            # A TYPED turn (the stealth box) has no voice waiting on it: the
            # ceiling that keeps a spoken answer under three seconds is the
            # wrong constraint there. Let the model think properly.
            if typed and budget != THINK_OFF:
                budget = THINK_TYPED
        turn_cfg = self.turn_config(budget)
        if DEBUG:
            log(f"thinking budget for this turn: {budget}")

        resp = None
        moved = 0
        for attempt in range(4):   # transient retry + up to two model/key moves
            try:
                count = bump_usage()
                if DEBUG:
                    log(f"gemini call {count}/{DAILY_LIMIT} today")
                resp = self.chat.send_message(msg, config=turn_cfg)
                break
            except Exception as e:
                if is_quota_error(e):
                    # A DAILY, PER-MODEL, PER-KEY quota (20 a day on the newest
                    # flash). It used to end the day here. Now: next key, or
                    # next model, and the same turn goes again.
                    if moved < 2 and self._reopen_chat(why="quota"):
                        moved += 1
                        turn_cfg = self.turn_config(budget)
                        continue
                    return ("I've used up every model I have for today on the free plan. "
                            "Catch me again tomorrow.")
                if is_transient_error(e) and attempt < 3:
                    time.sleep(1.5)
                    continue
                log(f"gemini error: {e}")
                # Hard failure (tool crash, dead connection, retry exhausted):
                # raise so the caller can hand the goal to Claude, not shrug.
                raise BrainError(str(e))
        reply = (resp.text or "").strip()
        spoken, facts = extract_remember(reply)
        spoken, spec = canvas.extract_show(spoken)
        spoken, vis = canvas.extract_visual(spoken)
        if spec is not None and canvas.valid(spec):
            self.last_show = spec
        elif vis:
            self.last_visual = vis
        saved = False
        for f in facts:
            saved = add_fact(self.mem, f) or saved
        if saved:
            save_memory(self.mem)
        out = spoken or "Hmm, I blanked. Say that again?"
        # Mirror the exchange to disk so the thread survives Neo restarting.
        self._turns = convo.record(self._turns, user_text, out)
        convo.save(self._turns)
        return out

    def _reopen_chat(self, why=""):
        """The brain's model ran dry (a per-model, per-key DAILY quota) or died.
        Move on and re-open the chat WITH the history, so the conversation
        carries on instead of ending in "catch me tomorrow". Order: the next
        model on this key (its other models still have their own quota),
        then the next key. Returns True when something changed."""
        global MODEL
        old_model = self.model
        providers.report_failure("chat", "gemini", old_model, log)
        provider, model = providers.resolve("chat", self.client, log)
        if model and model != old_model:
            MODEL = model
            self.model = model
            log(f"[models] chat: {old_model} is out ({why}) — moving to {model}")
        elif self.next_key():
            provider, model = providers.resolve("chat", self.client, log)
            if not model:
                return False
            MODEL = self.model = model
            agent.bind(client=self.client)
            log(f"[models] chat: {old_model} is out on that key ({why}) — next key, {model}")
        else:
            return False
        try:
            history = self.chat.get_history() if hasattr(self.chat, "get_history") else []
        except Exception:
            history = []
        try:
            self.chat = self.client.chats.create(
                model=self.model, config=types.GenerateContentConfig(**self._base_cfg),
                history=history)
        except Exception as e:
            log(f"[models] couldn't re-open the chat: {e}")
            return False
        return True

    def delivered(self, request, reply):
        """GOAL-LOCK: does the reply actually contain the outcome the user asked
        for? YES-biased and cheap (lite model) — only a clear miss (refusal,
        deflection, hurdle-story with no result, answering something else)
        returns False. Verifier down -> trust the reply."""
        prompt = (
            "the user asked their assistant: \"" + request[:400] + "\"\n"
            "The assistant replied: \"" + (reply or "")[:600] + "\"\n\n"
            "Did the reply DELIVER what was asked — the information itself, the "
            "action done, or a background job now running that will produce it? "
            "Answer YES unless the reply clearly refuses, deflects, only explains "
            "why it couldn't, asks what they meant instead of acting, or answers a "
            "different question. Reply with exactly one word: YES or NO.")
        try:
            bump_usage()
            r = self._gen(prompt, fast=True)
            return not str(r.text or "").strip().upper().startswith("NO")
        except Exception as e:
            log(f"goal check skipped ({e})")
            return True

    def polish_result(self, task, ok, raw):
        """Turn a finished job's raw report into the ONE thing the user wants to
        hear: did it work, plus the substance — no process, no jargon, no
        'ask me for more'. Returns None on any failure (caller falls back)."""
        raw = re.sub(r"\s+", " ", raw or "").strip()[:1800]
        if not raw:
            return None
        prompt = (
            "A background job just finished for the user. They do NOT want process "
            "talk — no tools, commands, file names, code identifiers, tests, or "
            "how anything was done.\n"
            f"The job: {task}\nIt {'succeeded' if ok else 'FAILED'}.\n"
            f"Raw report:\n{raw}\n\n"
            "Rewrite this as what their assistant SAYS OUT LOUD, in one go:\n"
            "- Open with whether it worked, in plain words.\n"
            "- Then ONLY the substance they actually asked for — the findings, "
            "numbers, or outcomes — in flowing conversational sentences a "
            "non-programmer instantly gets. Everything they asked for now; never "
            "tell them to ask for details later.\n"
            "- NEVER: hurdles that were overcome along the way, tools or "
            "methods used, email addresses or message bodies (people are "
            "names, messages are one-line gists), file names, code, IDs.\n"
            "- If it failed, say what went wrong in one plain sentence and the "
            "single most useful next move.\n"
            "- If it built a new ability, just say what they can now ask for.\n"
            "- As short as the content allows — two sentences is the norm, "
            "more only if they asked for a list. Zero filler, zero jargon, no "
            "markdown, no lists, no symbols."
        )
        try:
            bump_usage()
            r = self._gen(prompt, fast=True)
            out = (r.text or "").strip()
            return out or None
        except Exception as e:
            log(f"polish skipped ({e})")
            return None

    def lookup(self, query):
        """Answer a real-world / current question by searching the live web."""
        import web
        results = web.web_search(query, 6)
        ctx = []
        for r in results[:5]:
            ctx.append(f"- {r['title']} ({r['url']})")
        for r in results[:2]:                 # pull a bit of body for depth
            body = web.fetch_text(r["url"], 1200)
            if body:
                ctx.append(body)
        context = "\n".join(ctx) or "(no results found)"
        prompt = (
            "Answer the question from these live web results, OUT LOUD style: "
            "lead with the answer itself in one or two conversational sentences, "
            "then stop — they'll ask if they want more. Never describe what the "
            "results 'are about', never mention searching. If they don't answer "
            "it, say so in one honest sentence.\n\n"
            f"Question: {query}\n\nWeb results:\n{context}"
        )
        try:
            bump_usage()
            r = self._gen(prompt, fast=True)   # mechanical summarize -> lite model
            return (r.text or "").strip() or "I couldn't find a clear answer on that."
        except Exception as e:
            if is_quota_error(e):
                return "I'm maxed out for today on the free plan. Try me tomorrow."
            log(f"lookup error: {e}")
            return "I had trouble looking that up just now."

    def visualize(self, request):
        """Freeform on-demand visual. Returns True if something opened."""
        ctx = ""
        try:
            bump_usage()
            return canvas.visualize(self.client, MODEL, request, ctx) is not None
        except Exception as e:
            if is_quota_error(e):
                return False
            log(f"visualize error: {e}")
            return False

    def recap(self):
        return mem_summarize(self.mem)

    def forget(self):
        gone = mem_forget_last(self.mem)
        return f"Done, forgot that: {gone}" if gone else "There's nothing to forget."


# --------------------------------------------------------------------------- #
# Audio recorder — opens the mic ONLY while recording is active
# --------------------------------------------------------------------------- #
# Bluetooth headsets (AirPods, AirPods Max) are TERRIBLE microphones on macOS:
# using one as an input forces the link into low-bandwidth call mode (HFP), so
# speech comes back thin and quiet AND the music in your ears degrades too.
# Neo therefore RECORDS from the Mac's built-in mic while whatever you're
# wearing keeps playing audio — best of both. Override with NEO_MIC="some name"
# (or NEO_MIC=default to just use the system input).
_BUILTIN_HINTS = ("macbook", "built-in", "internal")
_BLUETOOTH_HINTS = ("airpod", "beats", "bluetooth", "headset", "buds", "hands-free")
# Continuity: macOS silently promotes a nearby iPhone to the DEFAULT input, and
# then delivers silence whenever the phone is locked, in a pocket, or just not
# in the mood. That's the "(held 0.4s but the mic was basically silent — input
# device issue?)" line in the log: nothing was broken, Neo was recording a
# phone that wasn't listening. A phone is never the right mic for a Mac
# assistant you talk to at the keyboard, so it gets treated like a headset.
_PHONE_HINTS = ("iphone", "ipad", "continuity")

_AVOID_HINTS = _BLUETOOTH_HINTS + _PHONE_HINTS


def safe_input_device(devices=None):
    """A CONCRETE built-in input device name, or None if there isn't one.

    Different question from pick_input_device, which may answer None meaning
    "the system default is fine". For a stream that opens while the speakers
    are playing, "the system default" is not fine: it follows the headset, and
    opening a Bluetooth microphone costs the output quality for as long as it
    is open. This only ever names a real built-in device.
    """
    try:
        import sounddevice as _sd
        devices = list(_sd.query_devices()) if devices is None else devices
    except Exception:
        return None
    for d in devices:
        name = d.get("name", "")
        if (d.get("max_input_channels", 0) > 0
                and any(h in name.lower() for h in _BUILTIN_HINTS)
                and not any(a in name.lower() for a in _AVOID_HINTS)):
            return name
    return None


def pick_input_device(devices, system_default_name="", override=None):
    """Choose which mic to record from.

    Order: an explicit NEO_MIC override, then the Mac's built-in mic whenever
    the system default is something that delivers silence half the time (a
    Bluetooth headset, or a Continuity iPhone/iPad), else the system default.

    Pure (takes a device list) so it's testable off a Mac. Returns a device
    NAME, or None meaning 'use the system default'."""
    if override:
        if override.strip().lower() == "default":
            return None
        for d in devices:
            if override.strip().lower() in d.get("name", "").lower() and d.get("max_input_channels", 0) > 0:
                return d["name"]
        return None

    def _builtin():
        for d in devices:
            name = d.get("name", "")
            if (d.get("max_input_channels", 0) > 0
                    and any(h in name.lower() for h in _BUILTIN_HINTS)):
                return name
        return None

    default_l = (system_default_name or "").lower()
    if default_l and any(h in default_l for h in _AVOID_HINTS):
        return _builtin()

    # Sometimes the default has no usable name at all. We can't tell what macOS
    # picked, so prefer the built-in mic if there is one: it is the only input
    # on a laptop that is always present, always awake, and never wanders off.
    if not default_l:
        return _builtin()
    return None


class Recorder:
    def __init__(self):
        self._frames = []
        self._stream = None
        self._device = None                 # cached choice (None = system default)
        self._device_at = None              # when it was resolved (see DEVICE_TTL_S)
        self._probing = False               # a background probe is already running
        self._warned = None
        self._lock = threading.Lock()       # guards _frames (touched by the PortAudio cb)
        self._io_lock = threading.Lock()    # serializes device open/close so the audio
                                            # thread and the watchdog never race the mic

    # Device enumeration is SLOW (seconds, when Bluetooth is around), so it must
    # never run on the per-turn path: a slow mic open makes macOS time out the fn
    # event tap and the key-up gets lost — the "stuck listening" freeze. Resolve
    # once, then refresh only every DEVICE_TTL_S so plugging in headphones is
    # still picked up within a few turns.
    DEVICE_TTL_S = 60

    def _refresh_device(self):
        """Enumerate devices and cache the pick. SLOW — only ever call this off
        the recording path (boot warmup, or the background refresh below)."""
        import time as _t
        try:
            devices = list(sd.query_devices())
            try:
                default_name = sd.query_devices(kind="input").get("name", "")
            except Exception:
                default_name = ""
            dev = pick_input_device(devices, default_name, os.getenv("NEO_MIC"))
            if dev != self._warned:
                if dev:
                    log(f"mic: using '{dev}' — the system input '{default_name}' "
                        "drops out (phone or headset). NEO_MIC=default to override.")
                self._warned = dev
            self._device = dev
        except Exception as e:
            log(f"mic: device probe failed ({e}) — using the system default")
            self._device = None
        self._device_at = _t.time()

    def _resolve_device(self):
        """The mic to open, returned INSTANTLY from cache — never blocks.

        Device enumeration can hang for seconds (Bluetooth), and this runs on the
        audio thread that also processes 'stop'. A hang here therefore strands the
        stop command in the queue: state sticks on 'listening', the fn key-up looks
        lost, and even the watchdog's abort can't get through. That deadlock is the
        freeze — so resolution NEVER happens inline. A stale/empty cache just uses
        the system default this turn and refreshes in the background."""
        import time as _t
        stale = (self._device_at is None
                 or (_t.time() - self._device_at) >= self.DEVICE_TTL_S)
        if stale and not self._probing:
            self._probing = True

            def probe():
                try:
                    self._refresh_device()
                finally:
                    self._probing = False

            threading.Thread(target=probe, daemon=True).start()
        return self._device        # None until the first probe lands = system default

    def _close_stream(self):
        """Tear the stream down so nothing keeps holding the mic. Both calls are
        guarded: a half-open stream from a failed start must still get released,
        or the orange mic indicator stays on forever."""
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

    def start(self):
        # NOTE: this does blocking PortAudio device I/O — it MUST run on the audio
        # thread, never in the fn-tap callback. A slow mic open on the main run loop
        # makes macOS time out and disable the event tap, and the fn key-up gets
        # lost (the freeze). See Neo._audio_loop.
        with self._io_lock:
            with self._lock:
                self._frames = []
            def cb(indata, frames, time_info, status):
                with self._lock:
                    self._frames.append(indata.copy())
            # A stream left open by a failed/interrupted turn would keep the mic
            # (orange indicator) held. Always clear the old one before opening a new.
            self._close_stream()
            stream = None
            device = self._resolve_device()
            try:
                try:
                    # Serialised against live.py's streams — see AUDIO_LOCK.
                    # Two paths opening PortAudio at once segfaults the process.
                    with live_mod.AUDIO_LOCK:
                        stream = sd.InputStream(
                            samplerate=MIC_RATE, channels=1, dtype="float32",
                            callback=cb, device=device
                        )
                except Exception:
                    # Chosen device refused (unplugged mid-turn?) — fall back to
                    # the system default rather than losing the utterance, and
                    # drop the cache so the next turn re-picks a live device.
                    self._device_at = None
                    if device is None:
                        raise
                    log(f"mic: '{device}' wouldn't open, using the system default")
                    # The device list may simply be stale — something was
                    # plugged in or unplugged since PortAudio last looked.
                    live_mod.reset_portaudio(sd, log)
                    with live_mod.AUDIO_LOCK:
                        stream = sd.InputStream(
                            samplerate=MIC_RATE, channels=1, dtype="float32",
                            callback=cb
                        )
                with live_mod.AUDIO_LOCK:
                    stream.start()
                self._stream = stream
            except Exception:
                # start failed: make sure nothing half-open holds the mic, then re-raise
                if stream is not None:
                    try:
                        stream.stop()
                        stream.close()
                    except Exception:
                        pass
                self._stream = None
                raise

    def stop(self):
        with self._io_lock:
            self._close_stream()
            with self._lock:
                if not self._frames:
                    return np.zeros(0, dtype=np.float32)
                return np.concatenate(self._frames, axis=0).flatten()


# --------------------------------------------------------------------------- #
# Orchestrator — ties the fn key to record -> think -> speak
# --------------------------------------------------------------------------- #
class Neo:
    def __init__(self, state):
        self.state = state
        self.speech = Speech()
        self.brain = None
        self.ready = False
        # Stealth: double-press fn toggles it; a press then opens the chat box
        # and nothing makes a sound (stealth.py). Persisted, four-hour TTL.
        import stealth as _stealth
        self.stealth = _stealth.Mode(_state_file("stealth.json"))
        self._taps = _stealth.DoubleTap()
        _stealth.box.on_send = self._stealth_send
        self._stealth_turn = None       # the typed turn in flight, if any
        # Set the instant boot() finishes, so a turn captured during warmup can
        # wait for the models instead of being discarded.
        self._ready_evt = threading.Event()
        self.recorder = Recorder()
        self.notifier = None       # set by main() when the card UI is available
        self.live = None           # the real-time conversation, when one is open
        self._live_degraded = False   # set once live has proven unavailable here
        self.sentinel = None       # started in boot()
        self.claude = ClaudeBridge(self._deliver_insight)   # Claude Code on the Max plan
        self.last_spoken = ""                    # for "say that again"
        self.hud_on = False                      # status HUD available?
        self.panel_on = False                    # answer panel available?
        self._hud_actions = []                   # pending "waiting on you" items
        self._turn_hud_armed = None              # pending latency-gate timer
        self._turn_hud_shown = False             # did THIS turn raise the tab?
        self._interrupt = threading.Event()      # barge-in: fn press cuts speech
        self._ack_playing = False   # a filler line is on the speaker right now
        self.voice_mode = _load_voice_mode()   # "normal" | "whisper"
        _VOICE_MODE["mode"] = self.voice_mode
        self._hush = None           # the listener; built once the models are up
        self._speaking_now = False  # is Neo's own voice on the speaker?
        self._busy = threading.Lock()
        # A transcript fragment buffer that outlived its session used to be
        # concatenated onto the NEXT session's first line, welding an hours-old
        # question onto a new one inside a single user turn.
        self._live_pending = None
        self._turns_lock = threading.Lock()
        # self.live isn't assigned until well after the guard that checks it,
        # so two quick presses could each build a whole LiveSession — two
        # sockets and two open microphones, one of them orphaned until its
        # idle timer. This closes that window.
        self._live_starting = False
        self._live_start_lock = threading.Lock()
        self._ack_lock = threading.Lock()        # a deferred ack never overlaps the reply
        self._busy_since = None    # when the current turn started (watchdog)
        self._jobs = queue.Queue()
        self._audio_cmds = queue.Queue()   # start/stop the mic OFF the fn-tap thread
        self._hush_cmds = queue.Queue()    # ...and the hush mic off the SOCKET thread
        # one worker handles the slow STT/LLM/TTS pipeline off the UI thread
        threading.Thread(target=self._worker, daemon=True).start()
        # a dedicated thread owns the mic device, so the fn-tap callback stays
        # instant and macOS can never disable the tap mid-hold (the freeze)
        threading.Thread(target=self._audio_loop, daemon=True).start()
        # and one that owns the hush mic, for the same reason — see _hush_loop
        threading.Thread(target=self._hush_loop, daemon=True,
                         name="neo-hush-mic").start()
        # follows the headphones, but only ever between turns
        threading.Thread(target=self._device_watch, daemon=True,
                         name="neo-audio-devices").start()
        threading.Thread(target=self._watchdog, daemon=True).start()
        if UNDER_AGENT:
            threading.Thread(
                target=_reload_watcher, daemon=True,
                kwargs={"busy": lambda: bool(
                    getattr(self, "_holding", False)
                    or self._busy.locked()
                    or getattr(getattr(self, "live", None),
                               "connected", False))}).start()
        threading.Thread(target=self._boot_timeout, daemon=True).start()

    def _boot_timeout(self, limit=120):
        """If warmup wedges (HuggingFace download, Kokoro init), self.ready stays
        False and every hold gets silently eaten. Warmup is normally ~15s, so a
        miss at 120s means it hung — say so loudly instead of looking crashed."""
        time.sleep(limit)
        if not self.ready:
            log(f"STARTUP STALLED: not ready after {limit}s — a model download or "
                "init looks hung. Neo can't hear you yet; re-run ./setup.sh or "
                "check the network.")
            try:
                self._deliver_insight({
                    "key": "boot-stalled", "kind": "warn", "urgency": "high",
                    "title": "Neo is stuck warming up",
                    "detail": f"Not ready after {limit}s — likely a hung model "
                              "download. Voice won't work until this clears."})
            except Exception:
                pass

    def boot(self):
        try:
            # Probe the mic here, during warmup, so the FIRST fn press never pays
            # for device enumeration on the audio thread (that's what deadlocked
            # the stop command and looked like a lost key-up).
            threading.Thread(target=self.recorder._refresh_device, daemon=True).start()
            self.speech.warmup()
            # Skills load BEFORE the brain so the brain's prompt lists them —
            # a skill the brain doesn't know it has is a skill it can't use
            # (the calendar bug: built it, then said "I can't access that").
            names, desc = [], ""
            try:
                names, _ = skills.load_all()
                if names:
                    lines = [f"{m.NAME}: {m.DESCRIPTION[:100]}" for m in skills.loaded()]
                    desc = ("\n\nYOUR LOADED SKILLS — run any of them with "
                            "use_skill(name, their request verbatim):\n- "
                            + "\n- ".join(lines))
            except Exception as e:
                log(f"skill preload failed ({e}); brain runs without the list.")
            # No key: the local brain. Same surface, no model, honest about it.
            self.brain = (LocalBrain(skills_desc=desc) if KEYLESS
                          else Brain(skills_desc=desc))
            # The second brain: built from the machine once a day, off-thread,
            # never prompting (person.py).
            threading.Thread(target=self._profile_loop, daemon=True,
                             name="neo-profile").start()
            # Resolve what a press will actually need before printing the line,
            # or the boot log says "unresolved" for things that are simply
            # lazy — which reads exactly like a failure.
            providers.warm(self.brain.client, log)
            log(f"Models: {providers.describe()}")
            _live_k, _rest_k = providers.key_state()
            if _live_k + _rest_k > 1:
                log(f"Gemini keys: {_live_k} live"
                    + (f", {_rest_k} resting until their quota rolls over"
                       if _rest_k else "")
                    + " — the voice keeps going when one runs dry.")
            self.speech.gemini_client = self.brain.client   # cloud voice option
            # NOW build the filler lines. This used to run inside warmup(),
            # which happens before the brain exists — so gemini_client was None
            # for every single phrase, _gemini_tts returned None 53 times, and
            # the log said "53 filler line(s) couldn't be synthesised" on every
            # boot. Combined with "stay silent rather than use the wrong
            # voice", that meant Neo made NO sound at all while a tool ran. A
            # guaranteed failure, in the one place guaranteed to be silent
            # about it.
            try:
                self.speech._recache()
            except Exception as e:
                log(f"[voice] filler cache skipped: {e}")
            # Cloud ears, primed with the proper nouns the user actually says.
            self.speech.attach_ears(self.brain.client,
                                    self.brain.mem.get("facts", []))
            # How a finished deck narrates itself. deck.present() returns
            # instantly now (a blocking tool killed the websocket), so the
            # script comes back through here once the window is actually up.
            agent.on_narrate = self._narrate_deck
            agent.on_busy = self._deck_busy
            agent.on_say = self._say_live
            # Card titles get shortened by the fast model. Bound here rather
            # than at construction because the brain does not exist yet then.
            try:
                self.claude.client = self.brain.client
                import providers as _p
                _, self.claude.title_model = _p.resolve(
                    "fast", self.brain.client, log=lambda m: None)
            except Exception as e:
                log(f"[claude] no title model: {e}")
            agent.bind(claude_bridge=self.claude,   # give the toolbox its hands
                       client=self.brain.client, model=MODEL,   # ...and its eyes
                       logger=log,                  # ...and a voice in the log
                       set_voice_mode=self.set_voice_mode)   # whisper / normal
            agent.on_step = self._live_step          # brain tool use -> the HUD
            skills.set_ctx(skills.Ctx(client=self.brain.client, model=MODEL,
                                      say=self.say, notify=self._deliver_insight))
            if names:
                log(f"Skills loaded: {', '.join(names)}")
            # Mid-job progress is LOGGED, not carded. "First pass hit a wall,
            # trying another approach" is Neo narrating its own process, and
            # they asked to be interrupted when a job finishes — not while it
            # is still going.
            self.claude.on_progress = lambda line: log(f"[claude] {line}")
            self.claude.on_step = self._claude_step      # live steps -> the HUD
            # EVERY Claude job raises the task bar — no matter which path
            # started it (voice route, agent tool, skill build, ladder net).
            self.claude.on_start = self._claude_hud_start
            # The hush listener needs the local Whisper model, so it can only
            # be built once warmup is done.
            self._hush = self._build_hush()
            # Neo reads its own log. Silent cards only — see selfwatch.py.
            try:
                import selfwatch
                self._selfwatch = selfwatch.SelfWatch(
                    notify=self._deliver_insight, log=log)
                self._selfwatch.start()
            except Exception as e:
                log(f"[selfwatch] not running ({e})")
            if self.whispering():
                live_mod.set_output_gain(WHISPER_LIVE_GAIN)
                log("[voice] still in whisper mode from last time — say "
                    "'normal voice' to bring it back up.")
            self.ready = True
            self._ready_evt.set()
            # A restart and a crash leave the same hole in the log. Say which.
            gap = getattr(self, "_reload_gap", None)
            if gap is not None:
                log(f"(that was a reload into new code — {gap:.0f}s without a "
                    "key listener, not a crash)")
            # Boot hello: ON SCREEN by default (silent card that fades) — Neo
            # never talks out loud uninvited. NEO_BOOT=say brings the voice
            # back; NEO_BOOT=off (or NEO_QUIET_BOOT=1) disables entirely.
            # DEFAULT OFF. Neo restarts whenever a file changes, so a boot
            # card is not an occasional hello — it is a card several times an
            # hour saying nothing. the user asked for far fewer unprompted cards
            # and this was the single biggest source. NEO_BOOT=card brings it
            # back, NEO_BOOT=say makes it spoken.
            mode = (os.getenv("NEO_BOOT") or "off").lower()
            if os.getenv("NEO_QUIET_BOOT") == "1":
                mode = "off"
            line = banter.boot_line()
            if mode == "say":
                self.say(line)
            elif mode != "off":
                self._quiet_line(line, title=line)
        except Exception as e:
            log(f"STARTUP FAILED: {e}")
            log("Try re-running ./setup.sh — a model or dependency may be missing.")
            return
        # The sentinel: Neo noticing things before you ask (see sentinel.py).
        if os.getenv("NEO_SENTINEL") != "0":
            try:
                self.sentinel = Sentinel(self._deliver_insight)
                self.sentinel.start()
                from sentinel import CARD_KINDS
                log("Sentinel watching: "
                    + (", ".join(sorted(CARD_KINDS)) or "nothing"))
            except Exception as e:
                log(f"sentinel disabled ({e}); voice still works.")

    def _quiet_line(self, detail, title="Neo"):
        """Surface a line SILENTLY: low-urgency card that fades on its own.
        'Tell me' speaks it for anyone who wants the voice."""
        self._deliver_insight({
            "key": f"line:{datetime.datetime.now().timestamp():.0f}",
            "kind": "info", "urgency": "low",
            "title": title if title != "Neo" else detail,
            "detail": detail})

    # -- sentinel plumbing ----------------------------------------------------
    def _deliver_insight(self, insight):
        """Called from the sentinel thread; hop to the main thread for UI.

        Nothing gets through while they are presenting or on a call. A card
        during a screen share is not on their screen, it is on everybody's —
        their words for it were "distracting and harmful", and for a shared
        screen that is exactly right.
        """
        try:
            import quiet
            if quiet.is_on():
                log(f"[card] held back — focus mode: "
                    f"{str(insight.get('title'))[:60]}")
                return
        except Exception:
            pass
        try:
            import situation
            if situation.presenting():
                log(f"[card] held back while presenting: "
                    f"{str(insight.get('title'))[:60]}")
                return
        except Exception:
            pass
        if insight.get("kind") == "claude":
            # A Claude job just finished — it may have built a new skill.
            try:
                before = {m.NAME for m in skills.loaded()}
                names, _ = skills.load_all()
                fresh = [n for n in names if n not in before]
                if fresh:
                    insight = dict(insight)
                    insight["detail"] += (" And I just learned something new: my "
                                          + fresh[0].replace("_", " ") + " skill is live.")
            except Exception as e:
                log(f"skill reload failed: {e}")
            self._claude_hud = None              # job over — card no longer restorable
            if self.hud_on:                      # Claude job done -> drop the busy card
                hud.end_activity()
            # BLURT IT OUT, POLISHED. When Claude finishes, Neo speaks the
            # result immediately and in ONE GO: a Gemini pass turns the raw
            # report into outcome + substance, plain English, zero process
            # talk. Falls back to the bridge's short line if the pass fails.
            try:
                # DID IT PRODUCE A FILE? Then hand them the file. They asked for a
                # document, got a good one, and Neo read a summary of it aloud
                # instead of opening it — then took a SCREENSHOT when they said
                # "just show me the document". A document is delivered by being
                # on screen, not by being narrated.
                made = []
                try:
                    proj = (self.claude.last or {}).get("project", "")
                    path = claude_bridge.projects().get(proj, "")
                    made = claude_bridge.artifacts(insight.get("summary", ""), path)
                except Exception as e:
                    log(f"[claude] couldn't look for files: {e}")

                if made:
                    self.claude.last_artifacts = made
                    claude_bridge.save_artifacts(made)
                    # DOCUMENTS OPEN. CODE DOES NOT.
                    #
                    # The rule above is right for the thing it was written
                    # for: a report they asked for is delivered by being on
                    # screen. It was wrong for everything else. A job that
                    # edited three .py files popped three source files onto
                    # their screen, which is not a result — it is homework. For
                    # code, the result is the plain-English summary Neo is
                    # about to say anyway.
                    docs = [p for p in made if is_document(p)]
                    code = [p for p in made if p not in docs]
                    if code:
                        log(f"[claude] {len(code)} code file(s) changed — "
                            "summarised, not opened")
                    for p in docs[:2]:
                        try:
                            hands.open_url("file://" + p)
                        except Exception as e:
                            log(f"[claude] couldn't open {p}: {e}")
                    spoken = claude_bridge.handoff_line(made)
                    insight = dict(insight)
                    insight["detail"] = spoken
                    log(f"[claude] opened {len(made)} file(s) from the job")
                else:
                    spoken = None
                    if self.brain is not None and "summary" in insight:
                        spoken = self.brain.polish_result(
                            insight.get("task", "a job"), insight.get("ok", True),
                            insight["summary"])
                    if spoken:
                        insight = dict(insight)
                        insight["detail"] = spoken   # card and voice match

                # ONLY SPEAK IF NEO CAN DO IT IN ITS OWN VOICE. With no live
                # session open this used to go to the local engine, which is
                # the "old Jarvis" the user keeps hearing — and it read a whole
                # paragraph in it, so when they pressed the key to interrupt they
                # got the two voices overlapping. The card is already on
                # screen and the file is already open; a wrong-voice
                # monologue adds nothing.
                session = getattr(self, "live", None)
                if session is not None and session.is_running():
                    self.say(insight["detail"])
                else:
                    # NO SESSION IS NO LONGER A REASON TO STAY SILENT.
                    #
                    # The restriction above was written when the local path
                    # meant the local ENGINE — bm_fable, the "old Jarvis". It
                    # was right to keep that voice out of a conversation. But
                    # the local path now speaks Charon too (the default engine
                    # is the cloud voice, Kokoro only as the fallback), so the
                    # reason for the silence is gone — and what remained was a
                    # report the user explicitly asked for arriving as a card they
                    # then had to tap "tell me" on.
                    log("[claude] job done, no conversation open — speaking it")
                    self.say(insight["detail"])
            except Exception as e:
                log(f"speak-on-done failed: {e}")
        # The HUD's "waiting on you" panel takes REAL pipeline nudges only —
        # never boot greetings or progress chatter ('info'), which were showing
        # as permanent items. And it only surfaces while a task is in progress.
        if self.hud_on and insight.get("kind") in ("pipeline", "leads", "money"):
            try:
                item = {"label": insight["title"], "meta": ""}
                self._hud_actions = ([item] + [a for a in self._hud_actions
                                               if a["label"] != item["label"]])[:3]
                hud.set_actions(self._hud_actions)
            except Exception as e:
                log(f"hud actions failed: {e}")
        if self.notifier is not None:
            AppHelper.callAfter(self.notifier.notify, insight)
        else:
            log(f"[sentinel] {insight['urgency'].upper()}: {insight['title']}")

    # -- the working tab -------------------------------------------------------
    # Two activity cards can exist: the TURN card (this request, any kind) and
    # the CLAUDE card (a background job). The turn card appears the moment a
    # request is heard — the user always SEES Neo working, never wonders if it's
    # stuck. When a turn ends, the Claude card gets restored if a job is still
    # running; otherwise the HUD hides (their rule: only visible during work).

    def _hud_task(self, title, lines=None, chip="working", busy=True, eta=""):
        """Show 'here's what I'm doing' on the HUD (no-op if HUD is off)."""
        if self.hud_on:
            try:
                hud.set_activity(chip, title, lines=list(lines or []),
                                 busy=busy, eta=eta)
            except Exception as e:
                log(f"hud task failed: {e}")

    # How long a turn must run before the working tab appears. Simple/quick
    # answers (chat, single lookups) finish first and never raise it; only
    # genuinely slow, multi-step work crosses the line. their rule: no HUD
    # for "how are you" or "what's the weather", HUD for the hard stuff.
    HUD_DELAY = 2.0

    def _cancel_turn_timer(self):
        t = getattr(self, "_turn_hud_armed", None)
        if t is not None:
            t.cancel()
            self._turn_hud_armed = None

    def _arm_turn_hud(self, text):
        """Don't raise the working tab yet — arm a timer. If the turn is still
        going after HUD_DELAY it appears; if it finishes first (the common,
        quick case) it never does. Obvious small talk is suppressed outright."""
        self._turn_title = task_label(text, 60)
        self._turn_steps = ["on it"]
        self._turn_hud_shown = False
        self._cancel_turn_timer()
        if not self.hud_on or is_simple_request(text) or self._stealth_on():
            return
        t = threading.Timer(self.HUD_DELAY, self._show_turn_hud)
        t.daemon = True
        self._turn_hud_armed = t
        t.start()

    def _show_turn_hud(self):
        """Timer fired (or a heavy route asked): the turn is taking real work,
        so raise the tab with whatever steps have accumulated."""
        self._turn_hud_shown = True
        self._hud_task(getattr(self, "_turn_title", "Working"),
                       getattr(self, "_turn_steps", ["on it"]), chip="working")

    def _live_step(self, step):
        """A live step from the brain's tools ('Searching the web'). Accumulate
        it; only PAINT the HUD if it's already up — a quick single-tool answer
        stays hidden, a slow chain shows motion once the timer raises the tab."""
        try:
            # The island shows the current step inside the capsule.
            self.state.set_step(step)
            if self._stealth_on():
                import stealth as _stealth
                _stealth.box.set_working(step)
            steps = (getattr(self, "_turn_steps", []) + [step])[-4:]
            self._turn_steps = steps
            if getattr(self, "_turn_hud_shown", False):
                self._hud_task(getattr(self, "_turn_title", "Working"), steps,
                               chip="working")
        except Exception as e:
            log(f"hud step failed: {e}")

    def _claude_hud_start(self, task, proj):
        """A Claude job started (any path) — raise its card. Known-long work,
        so it shows immediately and supersedes the turn's own latency timer."""
        self._cancel_turn_timer()
        # No "Claude — " prefix. `task` is already a five-or-six-word title of
        # what the user asked for, and the prefix ate a third of the width to say
        # something the BUILDING chip already says.
        self._claude_hud = {"title": task,
                            "steps": [f"working in {proj}"]}
        self._hud_task(self._claude_hud["title"], self._claude_hud["steps"],
                       chip="building")

    def _claude_step(self, step):
        """A live step from Claude ('Reading db.py') -> its card, JARVIS-style.
        Called from the job thread."""
        try:
            ch = getattr(self, "_claude_hud", None) or {"title": "Claude working",
                                                        "steps": []}
            # Never the same line twice in a row. Three web searches used to
            # render as three identical rows saying "Looking online", which
            # reads as stuck rather than busy. label_step now says WHAT it is
            # searching for, so genuine repeats are rare — and when they do
            # happen they are one row, not a stack.
            if not ch["steps"] or ch["steps"][-1] != step:
                ch["steps"] = (ch["steps"] + [step])[-12:]
            self._claude_hud = ch
            # How long it has been running, on the chip. A card that has said
            # BUILDING for four minutes with no clock looks stalled.
            started = (self.claude.current or {}).get("started")
            eta = ""
            if started:
                mins = int((datetime.datetime.now() - started).total_seconds() // 60)
                eta = f"{mins} min" if mins >= 1 else ""
            self._hud_task(ch["title"], ch["steps"], chip="building", eta=eta)
        except Exception as e:
            log(f"hud step failed: {e}")

    def _turn_hud_done(self):
        """Turn over: cancel any pending timer, then restore the Claude card if a
        job's still running, otherwise drop the HUD."""
        self._cancel_turn_timer()
        if not self.hud_on:
            return
        try:
            if self.claude.current is not None and getattr(self, "_claude_hud", None):
                self._hud_task(self._claude_hud["title"], self._claude_hud["steps"],
                               chip="building")
            # only tear the tab down if this turn actually raised it (or a job's
            # card is up) — a suppressed simple turn leaves the HUD untouched.
            elif getattr(self, "_turn_hud_shown", False):
                hud.end_activity()
        except Exception as e:
            log(f"hud done failed: {e}")

    def on_card_action(self, insight, action):
        """Card handled (main thread). accept -> Neo says it; either way, snooze."""
        if insight.get("kind") == "need":
            # APPROVE: connect now (the real prompt), read something back, say
            # it, and re-run the request that hit the wall. Off the main
            # thread — a permission prompt can sit for as long as it likes.
            if action == "accept":
                threading.Thread(target=self._approve_need, args=(insight.get("connector", ""),),
                                 daemon=True, name="neo-approve").start()
            else:
                self.say("Okay, not now.")
            return
        if self.sentinel is not None:
            self.sentinel.mark(insight["key"], action, insight.get("urgency", "medium"))
        if action == "accept":
            # Claude results are blurted automatically now — tapping the card
            # must not parrot the exact same thing twice (the 16:55 double).
            if clean_for_speech(insight["detail"]) == (self.last_spoken or ""):
                return
            self.say(insight["detail"])

    def _approve_need(self, key):
        import connectors
        try:
            said, resume = connectors.approve(key, log=log)
        except Exception as e:
            said, resume = f"That didn't connect: {e}", ""
        session = getattr(self, "live", None)
        if resume and session is not None and session.is_running():
            # In a conversation: one user-role turn carries both the news and
            # the request, so the model acts on it in the same voice.
            log(f"[connect] {key} approved — picking the request back up in the conversation")
            session.speak(f"(Connection update: {said}) Now do what I asked: {resume}", log=log)
            return
        self.say(said)
        if resume:
            log(f"[connect] {key} approved — picking the request back up: {resume!r}")
            self._jobs.put(("text", resume))

    def _needs_hook(self, card):
        """connectors.need -> the notice card, on the main thread."""
        n = getattr(self, "notifier", None)
        if n is None:
            return
        try:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(n.notify, card)
        except Exception as e:
            log(f"[connect] couldn't raise the card: {e}")

    def say(self, text):
        """Say something. Through the open conversation when there is one.

        THE VOICE HAS TO BE THE SAME VOICE. The live socket speaks as Charon;
        the local engine speaks as bm_fable. Anything routed to the local
        engine while a conversation is open is a different person mid-exchange
        — which is what the user hears as "the voice changes back to the old one"
        whenever a card, a timer or a job result speaks.

        Live also puts the line in the conversation, which is where it belongs:
        Neo said it, so Neo should remember saying it.
        """
        session = getattr(self, "live", None)
        if session is not None and session.is_running():
            try:
                if session.speak(clean_for_speech(text), log=log):
                    return
            except Exception as e:
                log(f"[voice] couldn't say that through the conversation: {e}")
        self._jobs.put(("speak", text))

    # called on fn DOWN (fn-tap thread = main run loop) — MUST return instantly.
    # No device I/O here: a slow mic open would make macOS disable the event tap
    # and the fn key-up would be lost (that's the freeze). We only flip the glow
    # and hand the actual mic-open to the audio thread.
    def on_press(self):
        """A press. In live mode this is a toggle — open the conversation, or
        end the one that's running. In hold mode it starts recording.

        Either way this MUST return instantly: it runs on the tap thread, and a
        slow press is what makes macOS disable the event tap and lose the
        key-up. Everything real happens on another thread.
        """
        # A fresh question gets a fresh right to ONE filler. Neo used to say
        # "Let me check." and then, a beat later, "One sec." for the same
        # request — the model issues tool calls in more than one batch and
        # every batch made a noise. See _live_ack.
        self._ack_turn = getattr(self, "_ack_turn", 0) + 1
        self._taps_press()

        # A new question means the last answer's panel is no longer the answer.
        # Leaving it up while Neo talks about something else is the same
        # confusion as a stale highlight, and it has a 90s life of its own that
        # would otherwise outlast the thing it was drawn for.
        if getattr(self, "panel_on", False):
            try:
                import panel as _panel
                _panel.clear()
            except Exception:
                pass

        # Give the microphone back. The hush listener holds an input stream
        # while Neo speaks, and a press means the user wants to talk INTO that
        # same device — two streams on one mic is at best contention and at
        # worst a capture that opens and hears nothing, which reads in the log
        # as "let go, but nothing was said". A queue put, never a device call:
        # this is the fn-tap thread and it has to return in microseconds.
        self._speaking_now = False
        try:
            self._hush_cmds.put_nowait("stop")
        except Exception:
            pass

        # HOLD TO TALK. Key down opens the mic; key up means "I'm done, answer
        # me". The session itself stays open between holds, so the conversation
        # keeps its thread and a second question doesn't start from nothing.
        session = getattr(self, "live", None)
        if session is not None and session.is_running():
            session.begin_turn()
            self.state.set("listening")
            return

        if self._stealth_on():
            # A press in stealth opens the box. THE MIC NEVER OPENS IN
            # STEALTH — not on a tap, not on a hold. The first version opened
            # it on a long-ish press "for a whisper", and a normal person's tap
            # is long-ish: the orange dot lit, a stray "Neo" was transcribed,
            # and the chain answered it out of the previous conversation.
            # Stealth is typed. That is the whole point of it.
            import stealth as _stealth
            _stealth.box.show(focus=True)
            return

        if self._live_default():
            # CUT THE LOCAL VOICE FIRST. This return came before the barge-in
            # block below, so in live mode — which is every normal day —
            # pressing the key never stopped local speech. A finished job read
            # a paragraph in the local voice, the user pressed the key to make it
            # stop, the live session opened and started answering, and both
            # kept going: two voices at once, jumping between them. Exactly
            # what they described.
            self._interrupt.set()
            try:
                with live_mod.AUDIO_LOCK:
                    sd.stop()      # also kills a filler mid-word; sd.wait() blocks
            except Exception:
                pass
            # Glow now so the press feels instant; the socket opens off-thread.
            log("(holding — opening a conversation)")
            self.state.set("listening")
            threading.Thread(target=self._start_live, args=(True,),
                             daemon=True).start()
            return
        log("(holding — recording)")

        if self._busy.locked():
            # They are pressing the key because nothing is happening. If the turn
            # holding the lock has been there far too long, the press IS the
            # report — recover instead of returning silently, which is what
            # made an hour of presses do nothing at all.
            since = self._busy_since
            if since and (time.time() - since) > STUCK_TURN_S:
                threading.Thread(target=self._force_unstick,
                                 args=((time.time() - since),), daemon=True,
                                 name="neo-unstick-press").start()
                return
            if self.state.get() == "speaking":
                # BARGE-IN: you talk, Neo shuts up and listens. Crucial for
                # natural conversation — nobody waits out a monologue.
                self._interrupt.set()
            else:
                return  # mid-think: a Gemini call can't be safely aborted
        # Glow immediately so the press feels instant; the audio thread opens the
        # mic a beat later and flips us back to idle if the device won't open.
        self.state.set("listening")
        self._audio_cmds.put(("start", None))

    # called on fn UP (fn-tap thread = main run loop) — also instant. The mic
    # close + audio hand-off happens on the audio thread.
    def on_release(self, long_hold=False):
        """Key up. In a live conversation this is the signal to answer.

        A lost key-up used to mean a permanent freeze. Now the worst case is
        that Neo answers a moment early: the fn-guard forces this same release,
        which ends the turn cleanly rather than stranding an open mic."""
        if self._taps_release():
            threading.Thread(target=self._toggle_stealth, daemon=True,
                             name="neo-stealth").start()
            return
        if self._stealth_on():
            return                       # the box is up; nothing was recorded
        session = getattr(self, "live", None)
        if session is not None and session.is_running():
            session.end_turn()
            return
        if self._live_default():
            return          # session still opening; its first turn starts armed
        if self.state.get() != "listening":
            return
        self._audio_cmds.put(("stop", long_hold))

    def _stealth_send(self, text):
        """A line from the box. If an answer is still being worked out, this
        line is ADDED to that question: the in-flight reply is dropped when it
        lands and the two lines run again as one. "Stop" just drops it."""
        text = (text or "").strip()
        if not text:
            return
        turn = getattr(self, "_stealth_turn", None)
        if turn is not None and not turn.get("done"):
            turn["superseded"] = True
            if text.lower().strip(".!") in ("stop", "cancel", "never mind", "nevermind"):
                turn["cancelled"] = True
                import stealth as _stealth
                _stealth.box.set_working("")
                log("[stealth] cancelled the question in flight")
                return
            log("[stealth] added to the question in flight")
        self._jobs.put(("text", text))

    # Stealth bits, tolerant of a Neo built without __init__ (the tests do).
    def _stealth_on(self):
        st = getattr(self, "stealth", None)
        return bool(st is not None and st.active())

    def _taps_press(self):
        t = getattr(self, "_taps", None)
        if t is not None:
            t.press()

    def _taps_release(self):
        t = getattr(self, "_taps", None)
        return bool(t is not None and t.release())

    def _live_default(self):
        """True when a press should open a conversation rather than record.
        False once live has failed on this machine — one spoken apology, then
        the reliable pipeline, rather than a broken press every time."""
        return (MODE == "live" and not getattr(self, "_live_degraded", False)
                and getattr(self, "brain", None) is not None
                and not self._stealth_on())

    def _audio_loop(self):
        """Owns the mic device. Everything that opens or closes the audio stream
        runs here, NOT in the fn-tap callback — that's what keeps the event tap
        alive so a fn key-up can never be lost. Un-killable: one bad turn must
        never end the loop."""
        while True:
            cmd, arg = self._audio_cmds.get()
            try:
                if cmd == "start":
                    try:
                        self.recorder.start()
                    except Exception as e:
                        # A dead mic must never look like a crash — glow off, say it once.
                        log(f"[neo] mic error: {e}")
                        self.state.set("idle")
                        if self._can_notice_drop():
                            self.say("I couldn't open the mic just now — "
                                     "check the input device.")
                elif cmd == "stop":
                    audio = self.recorder.stop()
                    if arg:  # long_hold: fn stuck/forgotten, capture is junk
                        self.state.set("idle")
                        self.say("That was a long hold — say it again?")
                    else:
                        self._jobs.put(("audio", audio))
                elif cmd == "abort":
                    # watchdog unstick: drop the mic, don't transcribe anything
                    try:
                        self.recorder.stop()
                    except Exception:
                        pass
                    self.state.set("idle")
            except Exception as e:
                log(f"[neo] audio loop recovered from: {e}")
                self.state.set("idle")

    def _to_idle(self):
        """Back to idle — unless a barge-in already put us in 'listening'."""
        if self.state.get() != "listening":
            self.state.set("idle")
        self.state.set_level(0.0)
        self.state.set_step("")

    def _force_unstick(self, age):
        """A turn has wedged. Give Neo a working worker and tell them.

        Python cannot kill a thread, and the thread is almost always parked
        inside a blocking PortAudio write or a synthesis call that will never
        return — so it cannot be asked to stop either. The only real recovery
        is to ABANDON it: stop the audio device (which is what usually
        unblocks it), take a fresh lock so the old holder cannot block anyone,
        and start a new worker. The stuck thread, if it ever wakes, finishes
        against a lock nobody is waiting on and dies quietly.
        """
        log(f"WATCHDOG: a turn has been stuck for {int(age)}s — abandoning it "
            "and starting a fresh worker.")
        try:
            self._interrupt.set()
        except Exception:
            pass
        try:
            with live_mod.AUDIO_LOCK:
                sd.stop()          # unblocks a wedged stream.write in _speak
        except Exception as e:
            log(f"WATCHDOG: couldn't stop the audio device: {e}")
        try:
            live_mod.reset_portaudio()
        except Exception:
            pass
        # A FRESH LOCK. The wedged thread still holds the old one forever;
        # rebinding means nothing new ever waits on it.
        self._busy = threading.Lock()
        self._busy_since = None
        self._stuck_recoveries = getattr(self, "_stuck_recoveries", 0) + 1
        try:
            self.state.set("idle")
        except Exception:
            pass
        threading.Thread(target=self._worker, daemon=True,
                         name=f"neo-worker-{self._stuck_recoveries}").start()
        # They have been pressing a dead key. Say something.
        try:
            self.say("Something jammed there and I've reset myself. Say that "
                     "again.")
        except Exception:
            pass

    def _worker(self):
        # Un-killable: any failure in one turn must never end the loop.
        import time as _t
        while True:
            kind, payload = self._jobs.get()
            try:
                with self._busy:
                    self._busy_since = _t.time()   # the watchdog watches this
                    if kind == "speak":
                        try:
                            self._speak(payload)
                        finally:
                            self._to_idle()
                    elif kind == "text":
                        # Typed in the stealth box: straight to the chain.
                        # Anything they typed WHILE the last answer was being
                        # worked out is folded into this one — "what's Apple
                        # at" then "and Microsoft" becomes one question —
                        # and the island stays dark; the box has its own
                        # working line.
                        more = []
                        while True:
                            try:
                                k2, p2 = self._jobs.get_nowait()
                            except queue.Empty:
                                break
                            if k2 == "text":
                                more.append(p2)
                            else:
                                self._jobs.put((k2, p2))
                                break
                        prev = getattr(self, "_stealth_turn", None)
                        carry = ([prev["text"]] if prev and prev.get("superseded")
                                 and not prev.get("cancelled") else [])
                        text = " ".join(carry + [payload] + more)
                        if more or carry:
                            log(f"[stealth] one question from {len(carry) + 1 + len(more)} lines")
                        self._stealth_turn = {"text": text, "superseded": False, "done": False}
                        try:
                            self._handle_text(text, typed=True)
                        finally:
                            self._stealth_turn["done"] = True
                            self._to_idle()
                    else:
                        self._handle(payload)
            except Exception as e:
                log(f"worker recovered from: {e}")
                self._to_idle()
            finally:
                self._busy_since = None

    def _handle(self, audio):
        import time as _t
        self._turn_t0 = _t.time()
        self._first_sound_ms = None
        stt_ms = None
        try:
            held = audio.size / MIC_RATE
            if audio.size < MIN_SPEECH_SEC * MIC_RATE:
                self.state.set("idle")
                return
            # Loudness gate: silence/room noise -> ignore (stops Whisper hallucinating).
            # BUT never ignore SILENTLY on a deliberate hold — a swallowed
            # utterance is indistinguishable from a crash. Tell them.
            # BEDTIME: amplify before anything judges the level. A whisper
            # sits roughly thirty decibels under ordinary speech — well below
            # MIC_GATE — so without this the honest answer is always "the mic
            # was basically silent", which is exactly what the user got when they
            # tried to whisper.
            if self.bedtime():
                audio = np.clip(audio * self.mic_gain(), -1.0, 1.0)
            rms = float(np.sqrt(np.mean(audio ** 2))) if audio.size else 0.0
            if DEBUG:
                print(f"[neo] captured {held:.1f}s  rms={rms:.4f}")
            if rms < self.mic_gate():
                log(f"(held {held:.1f}s but the mic was basically silent — input device issue?)")
                if held >= 1.2 and self._can_notice_drop():
                    self._speak("I couldn't hear anything on the mic just now. "
                                "If you were talking, the input device may have "
                                "switched — check the mic.")
                else:
                    self.state.set("idle")
                return
            if not self.ready or self.brain is None:
                # Do NOT throw the question away. Boot is ~11s and Neo restarts
                # itself on every edit, so that window sits right after every
                # save — a press that lands in it used to vanish, leaving one
                # line in a log nobody reads. That silence is most of what "the
                # fn key sometimes doesn't work" actually was. The audio is
                # already captured; hold it and answer late instead.
                log(f"(caught mid-boot — holding {held:.1f}s of audio until warm)")
                self.state.set("thinking")
                if not self._ready_evt.wait(WARMUP_HOLD_S) or self.brain is None:
                    self.state.set("idle")
                    log(f"(still warming up after {WARMUP_HOLD_S:.0f}s — dropped it)")
                    if self._can_notice_drop():
                        self._speak("I was still starting up and lost that one. "
                                    "Say it again?")
                    return
                log("(warm now — answering what you asked during boot)")
            self.state.set("thinking")
            text, confident = self.speech.transcribe(audio)
            stt_ms = int((_t.time() - self._turn_t0) * 1000)
            if not text:
                log(f"(held {held:.1f}s, sound present, but no words came through)")
                if held >= 1.2 and self._can_notice_drop():
                    self._speak("I heard sound but couldn't make out any words. "
                                "Say that again?")
                else:
                    self.state.set("idle")
                return
            if not confident:
                log(f"You (low confidence, not acting): {text}")
                self._speak("Didn't quite catch that. Say it again?")
                return
            self._handle_text(text)
        except Exception as e:
            log(f"error: {e}")
        finally:
            if stt_ms is not None:      # only real turns, not taps/silence
                # stt_path is here so the metrics file answers "are the cloud
                # ears actually being used, or did it quietly fall back?" —
                # a silent fallback is the failure mode worth catching.
                ears_obj = getattr(self.speech, "ears", None)
                metrics.record({"stt_ms": stt_ms,
                                "stt_path": getattr(ears_obj, "last_path", "local"),
                                "first_sound_ms": self._first_sound_ms,
                                "total_ms": int((_t.time() - self._turn_t0) * 1000)})
                self._turn_hud_done()   # drop the tab (or restore Claude's)
            self._to_idle()

    def _handle_text(self, text, typed=False):
        """The chain, from words. Both ears reach here: a held-and-transcribed
        turn, and a typed line from the stealth box. Everything after this
        point is about the words, not how they arrived."""
        import agent as _agent
        _agent.last_request = text
        try:
            log(f"You: {text}")
            # "Um, Neo, open Safari." -> "open Safari." before any routing
            text = strip_address(text)
            # Arm the working tab, but don't raise it yet — simple/quick asks
            # ("how are you", "what's the weather") finish before the latency
            # gate and never show it; only slow, multi-step work does.
            self._arm_turn_hud(text)

            # TYPED (stealth) GOES STRAIGHT TO THE BRAIN. The shortcuts below
            # are keyword routes built for speed on the voice path — a
            # sentence with "calendar" in it went to the calendar-peek skill,
            # which answered "go to calendar and find a slot for the three of
            # us" with a Daylight Saving Time event. Typed questions are
            # longer and more considered; they get the model, with tools.
            import stealth as _stealth
            if typed and _stealth.wants_off(text) and self._stealth_on():
                self._toggle_stealth(False)
                return
            if not typed:
                # "That's all" / "hang up" ends a conversation. There is no
                # matching command to START one, on purpose — a press already does
                # that, and a conversational assistant shouldn't need a magic word.
                if wants_live_off(text) and getattr(self, "live", None) is not None:
                    self._end_live("you asked")
                    return

                import stealth as _stealth
                if _stealth.wants_off(text) and self._stealth_on():
                    self._toggle_stealth(False)
                    return
                if _stealth.wants_on(text) and not self._stealth_on():
                    self._toggle_stealth(True)
                    return

                # The way out of local mode, and it is checked HIGH — above
                # everything that needs a model — because in local mode this is
                # the single most important sentence a person can say, and the
                # chain below it cannot answer anything.
                if wants_key_setup(text):
                    if KEYLESS:
                        threading.Thread(target=self._add_key_flow, daemon=True,
                                         name="neo-addkey").start()
                    else:
                        self._speak("You've already got a key in and I'm using it. "
                                    "Add a second one for a bigger daily allowance "
                                    "by putting it in dot env as GEMINI underscore "
                                    "API underscore KEY underscore 2.")
                    return

                # The two questions a person asks first to find out whether the
                # thing they just installed is real. Both are answerable with
                # no model at all — the clock is the machine's, Open-Meteo
                # needs no key — so in local mode they are routed here rather
                # than falling through to "I can't think yet". With a key the
                # model routes them as before; this changes nothing there.
                if KEYLESS:
                    _fact = parse_offline_fact(text)
                    if _fact == "time":
                        self._speak(agent.get_time())
                        return
                    if _fact == "weather":
                        self._speak(agent.get_weather())
                        return

                if wants_repeat(text):
                    self._speak(self.last_spoken or "I haven't said anything yet.")
                    return
                if wants_speed(text):
                    self._speak(metrics.summarize())
                    return

                # "Switch your voice to george" — live, persistent, no Gemini call.
                v = parse_voice_switch(text)
                if v is not None:
                    vid = VOICES.get(v)
                    if v == "gemini":
                        self.speech.set_engine("gemini")
                        self._speak("Trying the Gemini voice — this is it. If its "
                                    "free quota runs dry I fall back to my own voice.")
                    elif v in ("local", "kokoro", "normal"):
                        self.speech.set_engine("kokoro")
                        self._speak("Back on my own voice.")
                    elif not v:
                        self._speak("Give me a name. British: fable, george, or emma. "
                                    "American: heart, bella, nova, michael, or eric. "
                                    "Or say gemini for the cloud voice.")
                    elif vid is None:
                        self._speak(f"Don't know a voice called {v}. Try fable, george, "
                                    "emma, heart, bella, nova, michael, eric — or gemini.")
                    elif self.speech.set_voice(vid):
                        self._speak(f"This is {v}. I'll keep it unless you say otherwise.")
                    else:
                        self._speak("That one wouldn't load, so I'm keeping this voice.")
                    return

                # Local commands first (no Gemini call needed).
                if wants_brain(text):
                    brain.show()
                    self._speak("Opening my memory.")
                    return
                if wants_close(text):
                    self._speak(hands.close_windows())
                    return

                # Chrome, with the right profile — "open schoology in my school
                # profile". Checked before the plain open/lookup matchers so the
                # profile request isn't reduced to launching an app.
                if chrome.wants_chrome(text):
                    self._ack("quick")
                    self.state.set("thinking")
                    self._speak(chrome.go(
                        text,
                        notify=lambda title, detail, urg: self._deliver_insight(
                            {"key": f"chrome:{title[:24]}", "kind": "info",
                             "urgency": urg, "title": title, "detail": detail}),
                        say=None))
                    return

                want = skills.parse_teach(text)
                if want:
                    self._speak(self.claude.start(skills.author_task(want), "neo", framed=False))
                    return
                if skills.wants_skill_list(text):
                    self._speak(skills.summary())
                    return
                rep = skills.parse_repair(text)
                if rep is not None:
                    if not rep:
                        self._speak("Nothing's crashed recently. Which skill do you mean?")
                    else:
                        err = skills.last_error()
                        detail = err[1] if err and err[0] == rep else None
                        self._speak(self.claude.start(skills.author_repair_task(rep, detail), "neo", framed=False))
                    return

                if wants_tidy_memory(text):
                    self._ack("quick")
                    self.state.set("thinking")
                    bump_usage()
                    if KEYLESS:
                        self._speak("Tidying memory needs a model to judge what's "
                                    "worth keeping, and that needs a free key. Say "
                                    "\"add my key\" and I'll set it up.")
                    else:
                        self._speak(memory_consolidate(self.brain.mem, self.brain.client, MODEL))
                    return

                # Claude Code — explicit "ask claude to ..." wins over everything below,
                # so a task that names a skill still goes to Claude.
                ct = claude_bridge.parse_task(text)
                if ct:
                    self._speak(self.claude.start(ct[0], ct[1]))   # HUD via on_start
                    return
                if claude_bridge.wants_cancel(text):
                    self._speak(self.claude.cancel())
                    return
                if claude_bridge.wants_status(text):
                    self._speak(self.claude.status())
                    return
                if claude_bridge.wants_report(text):
                    self._speak(self.claude.report())
                    return

                # A real computer/code/data GOAL ("open terminal, get into the project,
                # open the signups database") -> hand the WHOLE goal to Claude Code,
                # which decomposes and executes it. Routed here so the literal
                # "open terminal" matcher below can't reduce it to launching an app.
                # Works even when Gemini's asleep — this path needs no Gemini call.
                if is_computer_task(text):
                    proj = claude_bridge.detect_project(text)
                    self._speak(self.claude.start(text, proj))     # HUD via on_start
                    return

                if wants_forget(text):
                    self._speak(self.brain.forget())
                    return
                if wants_recap(text):
                    self._speak(self.brain.recap())
                    return
                # On-demand visuals — "visualize X", "draw me X", "make a diagram of X"
                if wants_visual(text):
                    self._ack("visual")
                    self.state.set("thinking")
                    ok = self.brain.visualize(text)
                    self._speak("On your screen." if ok else
                                "That one didn't come together. Say it a different way?")
                    return

                # Hands — screen, apps, browser, typing (hands.py)
                if hands.wants_screen(text):
                    self._ack("screen")
                    self.state.set("thinking")
                    if KEYLESS:
                        self._speak("Looking at your screen needs eyes I don't have "
                                    "without a key. I can still read the text on it "
                                    "if you ask me to copy it. Say \"add my key\" "
                                    "for the rest.")
                    else:
                        self._speak(hands.describe_screen(self.brain.client, MODEL, text))
                    return
                music = parse_music(text)
                if music is not None:
                    action, query, app = music
                    self._speak(hands.music_do(action, query, app))
                    return
                typed = hands.parse_type(text)
                if typed is not None:
                    self._speak(hands.type_text(typed))
                    return
                if "browser" in text.lower():
                    q = hands.parse_search(text)
                    if q:
                        self._speak(hands.search_web(q))
                        return
                opened = hands.parse_open(text)
                if opened:
                    kind, target = opened
                    self._speak(hands.open_app(target) if kind == "app" else hands.open_url(target))
                    return

                if wants_radar(text):
                    if self.sentinel is not None:
                        self._speak(self.sentinel.pending_summary())
                    else:
                        self._speak("The sentinel's off right now, so my radar's dark.")
                    return

                if wants_lookup(text):
                    ack = self._ack_after("lookup")   # only fills if the search is slow
                    self.state.set("thinking")
                    self._live_step("Searching the web")
                    lu = self.brain.lookup(text)
                    self._ack_done(ack)
                    if self._dead_end(text, lu):   # search shrugged -> Claude digs
                        self._speak(self._escalate(text, lu))
                    else:
                        self._speak(lu)
                    return

                # Custom skills — abilities Neo has learned. Built-ins win; the
                # agent fallback below catches whatever no skill claims.
                sk = skills.find(text)
                if sk is not None:
                    self.state.set("thinking")
                    self._live_step(f"Running {sk.NAME.replace('_', ' ')}")
                    out = skills.run(sk, text)
                    if self._dead_end(text, out):   # NO skill gets to say "can't"
                        self._speak(self._escalate(text, out))
                    else:
                        self._speak(out)
                    return

            # If Claude is mid-job, tell the brain — so follow-ups ("don't change
            # code", "how's it going", "also check X") are handled with awareness
            # instead of "nothing's happening."
            job_note = self._job_context()
            if self._stealth_on():
                # They are READING this one. The personality is tuned for being
                # heard — numbers spelled out, everything as one breath — and
                # that reads as odd on a screen ("three hundred thirty-two
                # dollars"). Say so, per turn, without touching the memory.
                job_note += ("[STEALTH: they are reading your reply in a small text "
                             "box, not hearing it. Write it: digits and symbols "
                             "($332.68, 6pm, 1.9%), short lines, no spelled-out "
                             "numbers, no filler, no markdown.]\n")
            # A task-shaped ask usually makes the brain call tools (search,
            # screen, numbers) before it can answer — seconds of silence. Drop
            # an instant cached ack so first-sound is ~0.3s, not the whole call.
            # Pure chit-chat skips this (no "On it." before "I'm good, you?").
            ack = None
            if is_actionable(text):
                # Deferred: stays silent if the brain answers fast, so a normal
                # turn is ONE clean sentence instead of "On it." + gap + answer.
                ack = self._ack_after("quick")
                self.state.set("thinking")
            try:
                reply = self.brain.respond(text, job_note=job_note, typed=typed)
                self._ack_done(ack)
            except BrainError as e:
                self._ack_done(ack)
                # The brain TRIED and hard-failed (tool crashed / connection died).
                # A real ask doesn't die here — hand the original goal to Claude,
                # which runs on its own stack and can dig where the brain couldn't.
                if is_actionable(text) and self.claude.current is None:
                    log(f"(brain errored: {e} — escalating to Claude Code)")
                    self._speak(self._escalate(text, f"the assistant hit an error: {e}"))
                else:
                    self._speak("Something glitched on my end. Say that again?")
                return
            # THE LADDER'S SAFETY NET. The brain is told to never dead-end a
            # real request with "I can't" — but prompts aren't enforcement.
            # If it declines an actionable ask anyway, hand the ORIGINAL
            # request straight to Claude Code, which can do nearly anything.
            if self._dead_end(text, reply):
                self._speak(self._escalate(text, reply))
                return
            # NO EMPTY PROMISES. The brain sometimes *describes* doing the work
            # ("I'll build that and let you know when it's ready") without ever
            # calling hand_to_claude — so the user waits for something that will
            # never arrive. This is unfixable by prompt alone, so it's enforced:
            # a promise of later delivery with NO job actually running becomes a
            # real dispatch right now. (Escalating is cheap; a broken promise
            # costs their trust.)
            if promises_future_work(reply) and self.claude.current is None:
                log("(promised future work with no job running — dispatching for real)")
                self._speak(self._escalate(text, reply))
                return
            # GOAL-LOCK. A hurdle-story is not an answer. For real requests,
            # verify the reply contains what they ASKED for; if not, hand the
            # goal (plus what already failed) to Claude and keep going. The
            # verify is a paid round-trip BEFORE they hear anything, so skip it
            # when the reply is already long, confident, and non-hedgy.
            if (is_actionable(text) and self.claude.current is None
                    and needs_goal_check(reply)
                    and not self.brain.delivered(text, reply)):
                log("(reply missed the goal — escalating to Claude Code)")
                self._speak(self._escalate(text, reply))
                return
            warn = usage_warning()      # tack on a free-tier heads-up if we just crossed a line
            if warn:
                reply = f"{reply} {warn}"
            self._say_and_show(reply)
        except Exception as e:
            log(f"error: {e}")

    def _can_notice_drop(self):
        """At most one 'I couldn't hear you' notice per 20s — feedback,
        not nagging, if the room is just noisy."""
        import time as _t
        last = getattr(self, "_last_drop_notice", 0.0)
        if _t.time() - last < 20:
            return False
        self._last_drop_notice = _t.time()
        return True

    def _watchdog(self):
        """The un-stick thread. If a queued press left the glow on 'listening'
        while a long job runs, reset the glow; if a turn runs absurdly long
        (a hung call the timeout somehow missed), say so in the log instead
        of impersonating a crash."""
        import time as _t
        warned = 0.0
        stuck_since = None
        while True:
            _t.sleep(WATCHDOG_POLL_SEC)
            # If macOS ever disabled the event tap (a callback overran, user input
            # storm, sleep/wake), re-enable it here. Without this, a disabled tap
            # means every fn press is silently ignored — Neo goes deaf until a
            # restart. Cheap and thread-safe; the belt to the fn-guard's suspenders.
            try:
                tap = _TAP_REFS.get("tap")
                if tap is not None and not Quartz.CGEventTapIsEnabled(tap):
                    Quartz.CGEventTapEnable(tap, True)
                    log("WATCHDOG: the fn event tap was disabled — re-enabled it.")
            except Exception:
                pass

            t0 = self._busy_since
            if t0:
                age = _t.time() - t0
                if age > 25 and self.state.get() == "listening" and self._busy.locked():
                    self.state.set("idle")   # stale glow from a press queued mid-job
                if age > STUCK_TURN_S:
                    # AND ACTUALLY DO SOMETHING ABOUT IT.
                    #
                    # This used to log "it should time out shortly" every two
                    # minutes and nothing ever timed anything out. On the night
                    # of 2026-08-31 it said that seventy-two times in a row
                    # about the SAME turn — 4,340 seconds — while Neo sat
                    # holding self._busy. Every fn press in that hour hit
                    # `if self._busy.locked(): return` and did nothing, so from
                    # their side Neo had simply died, and the log knew and
                    # said so and did not act.
                    self._force_unstick(age)
                elif age > 120 and _t.time() - warned > 120:
                    warned = _t.time()
                    log(f"WATCHDOG: a turn has been running {int(age)}s.")

            # Lost fn key-up: glow stuck on 'listening', nothing in flight, and
            # the OS confirms fn is physically UP. The busy-FREE case the old
            # watchdog deliberately skipped — exactly the freeze. The fn-guard
            # timer should catch this first; this is the backstop if the run
            # loop is starved. Requiring the OS to confirm fn-up means a real
            # long hold is never cut off.
            # A live conversation owns the glow and the mic; this watchdog is
            # for the RECORDER path only. It fired mid-conversation and
            # "unstuck" a session that was working, which is worse than the
            # freeze it exists to prevent.
            session = getattr(self, "live", None)
            if session is not None and session.is_running():
                stuck_since = None
                continue
            holder = _TAP_REFS.get("holder") or {}
            key = holder.get("key", FN_KEYCODE)
            cond = (self.state.get() == "listening" and not self._busy.locked()
                    and self._jobs.empty() and ptt_physically_down(key) is False)
            if not cond:
                stuck_since = None
            else:
                stuck_since = stuck_since or _t.time()
                if watchdog_should_unstick(self.state.get(), self._busy.locked(),
                                           self._jobs.empty(), False,
                                           _t.time() - stuck_since, STUCK_LISTEN_SEC):
                    log(f"WATCHDOG: stuck on 'listening' for {int(STUCK_LISTEN_SEC)}s "
                        f"with {PTT_NAMES.get(key, 'the key')} up and no job — "
                        "unsticking (a key-up was lost).")
                    # Drop the mic on the audio thread, not here — only one thread
                    # ever touches the device.
                    self._audio_cmds.put(("abort", None))
                    self.state.set("idle")
                    h = _TAP_REFS.get("holder")
                    if h:
                        h["down"] = False
                    stuck_since = None

    # ----------------------------------------------------------- live mode ---
    def _start_live(self, holding=False):
        """Open a real-time conversation: mic streams up, voice streams back,
        the model decides when you've stopped talking.

        This is the DEFAULT path — a press lands here, not in the recorder. The
        important property is that failure is survivable: no live model on this
        key, no network, audio device busy, and Neo says one line, flips
        _live_degraded, and every press after that uses the old pipeline. One
        apology, never a broken key.
        """
        # One at a time. self.live is not assigned until far below — after a
        # providers.resolve and the whole prompt build — so two presses in that
        # window both passed this guard and each built a full LiveSession:
        # two sockets, two open microphones, and one orphan holding the device
        # until its idle timer. That is a mic indicator that will not settle.
        with self._live_start_lock:
            if self._live_starting:
                return
            if getattr(self, "live", None) is not None and self.live.is_running():
                return
            self._live_starting = True
        try:
            self._start_live_inner(holding)
        finally:
            with self._live_start_lock:
                self._live_starting = False

    def key_arrived(self, key=""):
        """A Gemini key just landed mid-session. Become the full Neo, now.

        KEYLESS is decided once, at import, from the .env that existed then.
        So a key saved during onboarding changed nothing in the running
        process: the brain stayed LocalBrain, the voice stayed Kokoro, and the
        onboarding cheerfully said "Got it. That's my real voice from here on."
        while continuing in the local one. Neo promising a thing and not doing
        it is the single worst output this codebase has a rule against, and it
        was happening in the first two minutes of every install.

        Restarting would also fix it, but a restart during onboarding throws
        the person back to the welcome scene, so this upgrades in place: the
        same swap the boot path does, minus the parts that are already up.
        Safe to call twice; returns True when Neo is now the full version.
        """
        global KEYLESS, API_KEY
        if key:
            os.environ["GEMINI_API_KEY"] = key
        API_KEY = os.getenv("GEMINI_API_KEY") or API_KEY
        if not API_KEY or API_KEY == "paste_your_free_key_here":
            return False
        if not KEYLESS:
            return True                     # already the full version
        try:
            KEYLESS = False
            desc = getattr(self.brain, "skills_desc", "") if self.brain else ""
            self.brain = Brain(skills_desc=desc)
            providers.warm(self.brain.client, log)
            # the cloud voice, and the filler lines re-cut in it
            self.speech.gemini_client = self.brain.client
            self.speech.set_engine("gemini")
            try:
                self.speech._recache()
            except Exception as e:
                log(f"[key] filler cache skipped: {e}")
            self.speech.attach_ears(self.brain.client,
                                    self.brain.mem.get("facts", []))
            try:
                self.claude.client = self.brain.client
                _, self.claude.title_model = providers.resolve(
                    "fast", self.brain.client, log=lambda m: None)
            except Exception as e:
                log(f"[key] no title model: {e}")
            agent.bind(claude_bridge=self.claude, client=self.brain.client,
                       model=MODEL, logger=log,
                       set_voice_mode=self.set_voice_mode)
            skills.set_ctx(skills.Ctx(client=self.brain.client, model=MODEL,
                                      say=self.say, notify=self._deliver_insight))
            log(f"[key] a key landed — full version live. Models: {providers.describe()}")
            return True
        except Exception as e:
            # Back to honest local mode rather than a half-upgraded Neo.
            KEYLESS = True
            log(f"[key] upgrade failed ({type(e).__name__}: {e}); staying local.")
            return False

    def _add_key_flow(self):
        """The way out of local mode, spoken. No terminal, no file to edit.

        Opens Google's key page in the browser the person already uses and
        already signed into, then watches the clipboard. They press Create API
        key and copy; Neo writes it into .env and restarts itself into the full
        version. The key is never read aloud, never logged, and never shown on
        screen.

        Deliberately the user's own Chrome rather than Neo's headless one:
        this is a Google account action on their account, and it should happen
        somewhere they can see it, signed in as themselves, with no automation
        touching the button.
        """
        import onboard as _ob
        import desk as _desk

        self._speak("Opening the key page now. Sign in if it asks, press Create "
                    "API key, and copy it. I'll take it from there and you'll "
                    "never see it again.")
        try:
            subprocess.Popen(["open", _ob.KEY_URL])
        except Exception as e:
            log(f"[key] couldn't open the key page: {e}")
            self._speak("I couldn't open the browser. The page is "
                        "aistudio dot google dot com slash apikey.")
            return

        deadline = time.time() + KEY_WAIT_S
        seen = (_desk.clipboard(limit=400) or "")
        while time.time() < deadline:
            time.sleep(1.0)
            now = (_desk.clipboard(limit=400) or "")
            if now == seen:
                continue
            seen = now
            m = _ob.KEY_RX.search(now)
            if not m:
                continue
            if not _ob.save_key(m.group(0)):
                continue
            log("[key] a key was saved from the clipboard.")
            # Upgrade in place first — no restart, no gap, no "come back in a
            # few seconds". Only fall back to the relaunch if that fails.
            if self.key_arrived(m.group(0)):
                self._speak("Got it. That's my real voice, and I can think "
                            "now. Ask me anything.")
                return
            self._speak("Got it. Give me a few seconds to come back as the "
                        "full version.")
            time.sleep(2.0)
            if UNDER_AGENT:
                mark_reload()
                for _s in (sys.stdout, sys.stderr):
                    try:
                        _s.flush()
                    except Exception:
                        pass
                os._exit(0)          # KeepAlive brings Neo straight back
            self._speak("Saved. Restart me and I'll be the full version.")
            return

        self._speak("I didn't see a key on the clipboard. No rush, I still work "
                    "for timers, reminders and your calendar. Say \"add my key\" "
                    "whenever you want to try again.")

    def _start_live_inner(self, holding):
        # The live socket IS the cloud. With no key there is nothing to open,
        # and every held turn goes down the local path instead: faster-whisper
        # for the words, the chain for the answer, Kokoro for the voice.
        if KEYLESS:
            return
        if self.brain is None:
            self.state.set("idle")
            self._speak("Still warming up. Give me a second.")
            return

        _provider, model = providers.resolve("live", self.brain.client, log)
        if not model:
            self._degrade("there's no live voice model on this key")
            return

        # Live sessions hear tone and timing that a transcript throws away, so
        # the prompt asks for a different register than the written one.
        instruction = (
            # spoken=True strips every bracketed-tag instruction. On this path
            # the model's words ARE the audio, so a "hidden" tag is read out —
            # which is how the user heard a JSON tool call spelled out to them.
            build_system_prompt(self.brain.mem, spoken=True)
            + self.brain.skills_desc +
            "\n\n" + context.brief() +
            "\n\nBEFORE ANY TOOL, ASK: do I already know this? Greetings, "
            "small talk, opinions, anything conversational, and any fact that "
            "is simply true — a conversion, a definition, how something works — "
            "get an IMMEDIATE spoken answer and NO tool call whatsoever. "
            "the user said hello and you ran a web search and the code "
            "interpreter; that is the failure to avoid. Reach for a tool only "
            "when the answer genuinely changes over time (news, prices, "
            "weather, scores, what is happening now) or is on this machine. "
            "When in doubt, answer from what you know — being fast is part of "
            "being right here.\n\n"
            "You are in a live voice conversation. Talk the way a person "
            "does out loud: short turns, contractions, no lists, no markdown, "
            "no reading out URLs. Say the thing rather than announcing that you "
            "are about to say it. If the user interrupts, stop immediately and "
            "listen. If they go quiet mid-thought, wait — don't fill the pause."
            )

        self.state.set("listening")
        self._live_pending = None       # nothing carries over from last time
        self.live = live_mod.LiveSession(
            client=self.brain.client,
            model=model,
            system_instruction=instruction,
            tools=agent.TOOLS,
            log=log,
            on_state=self._live_state,
            on_text=self._live_text,
            on_end=self._live_ended,
            on_tools=self._live_ack,
            on_speech_start=self._cut_ack,
            on_level=self.state.set_level,
            on_slow=self._live_slow,
            hints=ears_mod.build_hints(self.brain.mem.get("facts", [])),
            # The running conversation, so a new socket picks up mid-thread
            # instead of starting blank. This is what makes "you asked me a
            # question, here's my answer" work across a hang-up.
            # for_replay, not trim: a question Neo never got to answer must
            # not be seeded into a new socket, or the model answers the stale
            # question instead of what the user just said.
            # Slice first, THEN pair. Slicing after for_replay re-cut the list
            # at an arbitrary offset and could hand Gemini a history starting
            # on a model turn — which is exactly the window that replayed a
            # five-hour-old question.
            history=convo.for_replay(
                convo.trim(list(self.brain._turns))[-8:]))
        self.live.idle_timeout = LIVE_IDLE_SEC
        # Same input device the recorder would have picked — so a Continuity
        # iPhone is skipped here too.
        self.live.device = self.recorder._resolve_device()
        try:
            # Order matters and it used to be backwards. begin_turn() opens the
            # microphone on its own worker; start() opens the speaker on the
            # session thread. Called the old way they entered PortAudio at the
            # same moment from two threads and took the whole process down.
            # start() first, then arm — the pre-roll buffer already protects the
            # first word, so nothing is lost by waiting a few milliseconds.
            self.live.start()
            if holding:
                self.live.begin_turn()
            log("(live session starting)")
        except Exception as e:
            log(f"live failed to start ({e})")
            self.live = None
            self._degrade("I couldn't open a live session", err=e)

    def _degrade(self, why, err=None):
        """Live can't run here. Say it once, then behave like the old Neo — a
        slow assistant beats a key that does nothing.

        UNLESS IT IS ONLY THIS PROJECT THAT IS DRY. The live voice is the one
        thing with no free alternative anywhere — nothing else does
        speech-to-speech — so before giving it up, try the next Gemini key.
        Falling back to hold-to-talk also means falling back to a different
        VOICE, which the user will notice immediately and dislike.
        """
        if err is not None and self.brain is not None:
            try:
                if self.brain.next_key(err):
                    log("live: that project is out of quota — retrying the "
                        "conversation on the next key.")
                    self._start_live(holding=False)
                    return
            except Exception as e:
                log(f"[keys] couldn't switch keys: {e}")
        self._live_degraded = True
        self.state.set("idle")
        log(f"live unavailable ({why}) — falling back to hold-to-talk.")
        self._speak(f"{why[0].upper()}{why[1:]}, so hold the key and talk instead.")

    def _end_live(self, reason="done"):
        """Close the conversation quietly. No spoken sign-off: hanging up should
        feel like the end of a call, not like switching modes."""
        session, self.live = self.live, None
        if session is None:
            return
        try:
            session.stop(reason)
        except Exception as e:
            log(f"live teardown: {e}")
        self.state.set("idle")

    def _live_state(self, name):
        # The live session is the main way Neo speaks, so this is the main way
        # the hush mic learns to open and — more importantly — to close.
        try:
            self._speaking_changed(name == "speaking")
        except Exception:
            pass

        """Mirror the live session's state onto the same glow the pipeline uses.

        "live" means the socket is up but nobody is talking, and that maps to
        IDLE — not listening. It used to map to listening, so after every answer
        the orb sat there animating as though it were recording, with the mic
        closed. An assistant that looks like it's listening when it isn't is a
        worse bug than one that looks asleep: you can't trust either state.

        The rule: the orb shows listening only while the key is actually held.
        """
        try:
            session = getattr(self, "live", None)
            holding = bool(session and getattr(session, "_holding", False))
            # A presentation building in the background must keep the orb lit.
            # Neo says their filler line, the turn completes, the session reports
            # "idle" — and the orb went dark while fifty seconds of real work
            # was still running. From outside that is indistinguishable from a
            # crash, which is exactly how the user read it.
            if (not holding and name in ("live", "idle")
                    and session is not None and session.is_busy()):
                self.state.set("thinking")
                return
            if name in ("live", "idle"):
                self.state.set("listening" if holding else "idle")
                if not holding:
                    self.state.set_step("")
            elif name == "listening" and not holding:
                self.state.set("idle")      # stale: the key is already up
            else:
                self.state.set(name)
        except Exception:
            pass

    def _live_text(self, who, text):
        log(f"{who}: {text}")
        # A presentation on screen follows Neo's OWN words — this is the entire
        # sync mechanism. Nothing is timed: slide two appears when Neo starts
        # SAYING slide two, and an arrow lands on the word it belongs to.
        #
        # The transcript is not the voice, though, and that distinction is the
        # whole game. Gemini streams output_transcription as the model GENERATES
        # text, roughly three times faster than the audio can be spoken — so the
        # words arrive long before the user hears them. The playback buffer holds
        # exactly the difference, so hand it over and let the deck wait it out.
        # Without this a six-slide walkthrough finished in twenty seconds while
        # the voice was still on slide two.
        if who == "Neo":
            try:
                session = getattr(self, "live", None)
                pb = getattr(session, "_playback", None) if session else None
                lag = pb.lag_seconds() if pb is not None else 0.0
                deck.feed(text, lag)
            except Exception as e:
                log(f"[deck] {e}")
        # Live transcripts arrive in fragments; buffer per speaker and commit a
        # whole turn when the other side starts. Without this the conversation
        # is never written down at all, and every session — and every restart —
        # begins with total amnesia.
        pending = getattr(self, "_live_pending", None)
        if pending and pending["who"] != who:
            self._commit_live_turn(pending["who"], pending["text"])
            pending = None
        if pending is None:
            pending = {"who": who, "text": ""}
        pending["text"] = (pending["text"] + " " + text).strip()
        self._live_pending = pending
        # Hanging up has to work from inside the conversation too — being stuck
        # on an open mic because the only exit is a keypress would be worse than
        # the freeze this whole release is about fixing.
        if who == "You" and wants_live_off(text):
            threading.Thread(target=self._end_live, args=("you said so",),
                             daemon=True).start()
        # The onboarding's "first words" step waits for exactly this.
        if who == "You":
            ob = getattr(self, "onboarding", None)
            if ob is not None:
                try:
                    ob.heard(text)
                except Exception:
                    pass
            # "That was too long" / "just show it" — a preference, kept
            # instantly and without a model call (person.py).
            try:
                import person as _prof
                pref = _prof.preference_from(text)
                if pref:
                    _prof.save(_prof.note_preference(_prof.load(), pref))
                    log(f"[profile] noted {pref}")
            except Exception:
                pass
        if who == "You":
            try:
                import agent as _agent
                _agent.last_request = text
            except Exception:
                pass
        # "Go stealth" said out loud: obey it here, on the transcript, so the
        # model never answers it with a sentence.
        if who == "You":
            try:
                import stealth as _st
                if _st.wants_on(text) and not self._stealth_on():
                    session = getattr(self, "live", None)
                    if session is not None:
                        session.mute_turn()
                    threading.Thread(target=self._toggle_stealth, args=(True,),
                                     daemon=True, name="neo-stealth").start()
                    return
            except Exception:
                pass
        # HUSH — silence, and nothing else.
        #
        # "hush", "shush", "be quiet", "stop talking", "that's enough". These
        # used to go to the model like any other sentence, and it answered
        # them: "got it, quieting down". A hush that produces a reply has not
        # been obeyed. Caught here, on their own transcript, the moment it is
        # recognised — the answer already in flight is dropped and the rest of
        # it is discarded as it arrives.
        if who == "You":
            try:
                import hush as _h
                if _h.is_hush(text):
                    session = getattr(self, "live", None)
                    if session is not None:
                        session.mute_turn()
                    self._interrupt.set()
                    try:
                        with live_mod.AUDIO_LOCK:
                            sd.stop()
                    except Exception:
                        pass
                    self.state.set("idle")
                    log(f"[hush] {text.strip()!r} — silent, no reply")
            except Exception as e:
                log(f"[hush] {e}")

    def _toggle_stealth(self, on=None):
        """Double-press, or "go stealth" / "normal mode". Ends any live
        conversation on the way in; says nothing on the way out (the box
        confirms both, silently)."""
        import stealth as _stealth
        if on is None:
            now_on = self.stealth.toggle()
        else:
            self.stealth.set(on)
            now_on = bool(on)
        if now_on:
            log("[stealth] on")
            try:
                self._interrupt.set()
                with live_mod.AUDIO_LOCK:
                    sd.stop()
            except Exception:
                pass
        # whichever way it went, the second tap is not a question: drop any
        # recording the first tap started (in voice mode a press records)
        if self.state.get() == "listening":
            self._audio_cmds.put(("abort", None))
        if now_on:
            if getattr(self, "live", None) is not None:
                self._end_live("stealth")
            self.state.set("idle")
            _stealth.box.flash("Stealth on. Press fn to type. Double-press fn to go back.")
        else:
            log("[stealth] off")
            _stealth.box.flash("Voice back.", seconds=2.0)

    def _profile_loop(self):
        import person as _prof
        while True:
            try:
                if _prof.stale():
                    _prof.refresh(log=log)
            except Exception as e:
                log(f"[profile] {type(e).__name__}: {e}")
            time.sleep(3600)

    def _learn_from_session(self):
        if self.brain is None:
            return
        turns = list(getattr(self.brain, "_turns", []) or [])
        since = getattr(self, "_learned_upto", 0)
        if len(turns) <= since:
            return
        self._learned_upto = len(turns)

        def _go():
            try:
                import learn
                learn.from_session(
                    turns, lambda pr: getattr(self.brain._gen(pr), "text", ""),
                    since=since, log=log,
                    name=(__import__("person").load().get("person", {}).get("first_name") or ""))
            except Exception as e:
                log(f"[learn] {type(e).__name__}: {e}")
        threading.Thread(target=_go, daemon=True, name="neo-learn").start()

    def _commit_live_turn(self, who, text):
        """Write one finished live turn into the same conversation store the
        typed/held path uses, so both modes share one memory."""
        text = (text or "").strip()
        if not text or self.brain is None:
            return
        try:
            role = "user" if who == "You" else "model"
            # Three threads reach this: the live receive loop, the key handler
            # via _end_live, and the stall watchdog. Two unsynchronised
            # read-modify-writes mean the later one wins and the earlier turn
            # is LOST — and if the lost one is Neo's reply, that is another
            # orphaned question.
            with self._turns_lock:
                turns = list(self.brain._turns)
                turns.append({"role": role, "text": text})
                self.brain._turns = convo.trim(turns)
                convo.save(self.brain._turns)
            if role == "model" and text:
                # A real answer came back, so the "no answer" streak is over.
                self._dead_turn_tries = 0
            if role == "user":
                # Same fact extraction the pipeline does — things said out loud
                # in a live session are just as worth remembering.
                for fact in extract_remember(text)[1]:
                    if add_fact(self.brain.mem, fact):
                        save_memory(self.brain.mem)
        except Exception as e:
            log(f"[live] couldn't record that turn: {e}")

    # How many times a dropped answer is re-asked before Neo says so out loud.
    # Once. Twice would be a model that cannot answer this question being asked
    # a third time while they waits.
    RETRY_DEAD_TURNS = 1

    def _retry_dead_turn(self):
        """The socket gave nothing back. Reopen and ask their question again."""
        asked = ""
        try:
            for turn in reversed(self.brain._turns or []):
                if turn.get("role") == "user" and turn.get("text"):
                    asked = turn["text"]
                    break
        except Exception:
            pass
        tries = getattr(self, "_dead_turn_tries", 0)
        if not asked or tries >= self.RETRY_DEAD_TURNS:
            self._dead_turn_tries = 0
            self.say("That one didn't come back. Ask me again and I'll have "
                     "another go.")
            return
        self._dead_turn_tries = tries + 1
        log(f"[live] re-asking the question that got no answer "
            f"(attempt {tries + 2}): {asked[:60]!r}")
        try:
            self._start_live(holding=False)
        except Exception as e:
            log(f"[live] couldn't reopen to retry: {e}")
            self._dead_turn_tries = 0
            return
        # Give the socket a moment to finish connecting before speaking into it.
        for _ in range(24):
            session = getattr(self, "live", None)
            if session is not None and session.is_running():
                break
            time.sleep(0.25)
        session = getattr(self, "live", None)
        if session is None or not session.is_running():
            self._dead_turn_tries = 0
            log("[live] the retry session never came up")
            return
        session.speak(asked, log=log)

    def _live_ended(self, reason):
        # Flush whatever was mid-sentence when the socket closed.
        # Pop atomically: two callers reaching _live_ended (a stall, then a
        # stop) would otherwise commit the same text twice.
        with self._turns_lock:
            pending, self._live_pending = getattr(self, "_live_pending", None), None
        if pending:
            self._commit_live_turn(pending["who"], pending["text"])
        self._live_pending = None
        log(f"[live] closed ({reason}).")
        self.live = None
        # LEARN FROM THE SESSION. One fast-model call over what was said,
        # off-thread; facts to memory, preferences to the profile. This is
        # how the second brain updates on the live path, where the
        # [[remember]] tag can't be used because it would be read aloud.
        self._learn_from_session()
        # A TURN THAT NEVER CAME BACK MUST NOT DIE IN SILENCE.
        #
        # This is what the user calls Neo "wadding off": the answer watchdog fires
        # when nothing has come back, the session is torn down, and they are left
        # looking at a dark orb having asked a real question. Verbatim:
        #
        #   16:11:00  You: Go find the task descriptions on Chrome...
        #   16:11:13  [live] no answer came back — resetting the session.
        #
        # Nothing was said. So: say something, and ASK IT AGAIN — once. They
        # asked a question; the right response to a dropped answer is the
        # answer, not silence and not an apology they have to reply to.
        if reason == "no answer":
            threading.Thread(target=self._retry_dead_turn, daemon=True,
                             name="neo-retry-turn").start()
            return
        # THE DECK GOES WITH IT. A walkthrough is part of a conversation, not a
        # thing in its own right, and it only ever closed itself when the
        # tracker reached the final slide. Any narration that stopped early —
        # a lost chunk, a dropped socket, the user interrupting — left a
        # full-screen window sitting there with nobody talking to it, which is
        # what they saw. Closing with the session covers every one of those.
        try:
            if deck.current() is not None:
                log("[deck] the conversation ended — taking the walkthrough "
                    "down with it")
                deck.close("conversation ended")
        except Exception as e:
            log(f"[deck] couldn't close the walkthrough: {e}")

    def _escalate(self, text, tried=""):
        """Hand the ORIGINAL goal to Claude, with whatever half-answer already
        happened marked as a hurdle to route around. Returns the spoken line."""
        note = ""
        if tried:
            note = ("\n\n(Neo already tried and got only this far: \""
                    + tried[:280] + "\" — treat that as a hurdle, take another "
                    "route, and deliver the actual outcome.)")
        return self.claude.start(text + note, claude_bridge.detect_project(text))

    def _dead_end(self, text, reply):
        """True when an answer is a 'can't' shrug to a real request — the
        ladder's net: those never get spoken, they get escalated to Claude."""
        if sounds_like_cant(reply) and is_actionable(text) and self.claude.current is None:
            log("(dead-end answer — escalating to Claude Code)")
            return True
        return False

    def _job_context(self):
        """What Claude is doing right now, or just did — handed to the brain so
        follow-ups keep the thread. Two cases that both used to read as 'nothing's
        happening': (1) a job is mid-run ('don't change the code'), and (2) a job
        just finished and the user references its finding ('the workaround for the
        referral thing') — current is already None by then, so we lean on last."""
        cur = self.claude.current
        if cur is not None:
            return (f"[[note: RIGHT NOW a Claude Code job is running: "
                    f"'{cur['task']}' in the {cur['project']} project. If the user is "
                    "talking about it — adding a constraint, asking how it's going, "
                    "telling you what to focus on — acknowledge it's in progress and "
                    "that you'll have the result shortly. Do NOT say nothing is "
                    "happening, and do NOT start a second job.]] ")
        last = self.claude.last
        if last is not None:
            try:
                ended = datetime.datetime.fromisoformat(last["ended"])
                age = (datetime.datetime.now() - ended).total_seconds()
            except (ValueError, KeyError, TypeError):
                age = 1e9
            if age < 480:   # within ~8 min, the last result is live context
                return (f"[[note: Claude just finished a job — '{last['task']}' in "
                        f"{last['project']}. What it found/did: {last['summary']} "
                        "If their asking about 'it', 'that', 'the workaround', "
                        "'the fix', or otherwise following up on this, answer FROM "
                        "this result — don't ask 'for what?' or start a new job.]] ")
        return ""

    def _say_and_show(self, text):
        """Speak, and put any visual Neo attached on screen.
        Structured specs render instantly (before speech); freeform visuals
        need a design call, so they open moments after Neo finishes talking."""
        spec, self.brain.last_show = self.brain.last_show, None
        vis, self.brain.last_visual = self.brain.last_visual, None
        if spec is not None:
            try:
                canvas.show(spec)
            except Exception as e:
                log(f"canvas error: {e}")
        self._speak(text)
        if vis:
            self.state.set("thinking")
            if not self.brain.visualize(vis):
                log(f"freeform visual failed: {vis}")

    def _mark_first_sound(self):
        """Stamp when this turn's first audio actually reached the speaker."""
        if getattr(self, "_first_sound_ms", None) is None and getattr(self, "_turn_t0", None):
            import time as _t
            self._first_sound_ms = int((_t.time() - self._turn_t0) * 1000)

    def _ack_after(self, kind, delay=1.1):
        """Speak a filler ack ONLY if the real work is still running after `delay`.
        Fast turns then come back as ONE clean sentence instead of "Checking." +
        gap + the answer (the choppiness); slow turns still get instant feedback.
        Returns an Event — call .set() the moment the work finishes to cancel it.
        The shared lock guarantees the ack and the real reply never overlap."""
        done = threading.Event()

        def fire():
            if done.wait(delay):
                return                      # work beat the timer — stay silent
            if self._stealth_on():
                return                      # stealth: the box shows the step
            with self._ack_lock:
                if done.is_set():
                    return
                self._ack(kind)

        threading.Thread(target=fire, daemon=True).start()
        return done

    def _ack_done(self, ack):
        """Cancel a deferred ack and wait out one that's already mid-sentence,
        so the real reply never talks over it."""
        if ack is None:
            return
        ack.set()
        with self._ack_lock:
            pass

    # Which cached line fits which tool. The live path used to send its filler
    # THROUGH the model (send_realtime_input(text=...)), which needs a full
    # round trip before a single sound comes out — so a "quick" acknowledgement
    # arrived a second or more after the silence had already started reading as
    # a crash. These play from the pre-synthesized cache instead: milliseconds.
    _ACK_FOR_TOOL = {
        "present": "present", "close_presentation": None,
        "search_web": "lookup", "read_webpage": "lookup", "get_weather": "lookup",
        "look_at_screen": "hands", "control_mac": "hands",
        "type_on_keyboard": "hands", "press_key": "hands", "open_app": "hands",
        "hand_to_claude": "long", "create_skill": "long", "improve_skill": "long",
        "use_skill": "medium", "repair_skill": "medium",
        "browse": "lookup", "read_email": "lookup",
        "free_time_on_screen": "hands", "find_meeting_time": "medium",
        "find_person": "lookup", "browse_click": "hands", "browse_type": "hands",
        "create_google_doc": "hands", "compose_gmail": "hands",
        "connect_service": "medium", "set_reminder": "quick", "add_to_calendar": "quick",
        "think_hard": "medium", "morning_brief": "medium", "prep_for_meeting": "lookup",
        "copy_screen_text": "hands", "message_someone": "hands",
    }

    # How long a tool has to be running before covering it with a spoken line
    # is worth it. Under this, the answer itself arrives about when the filler
    # would have finished, and the result is "Checking." — gap — the real
    # sentence, which is choppier than just answering.
    ACK_DELAY_S = float(os.getenv("NEO_ACK_DELAY", "1.4"))

    def _deck_busy(self, working):
        """A presentation is building (or has finished).

        Two things hang off this and they are the same fact. The live session
        must not hang up — a build puts nothing on the socket, so the idle
        timer read fifty seconds of building as fifty seconds of silence and
        closed the conversation before the deck was ready. And the orb must
        stay lit, because the user watching it go dark mid-build has no way to
        tell "still working" from "glitched and gave up".
        """
        session = getattr(self, "live", None)
        if working:
            # Remember WHICH conversation asked for this, and when. A build
            # runs for the better part of a minute and the session it belongs
            # to can be gone long before it finishes — see _narrate_deck.
            self._deck_session = session
            self._deck_asked_at = time.time()
        try:
            if session is not None:
                if working:
                    session.hold("building a presentation")
                else:
                    session.release("presentation ready")
        except Exception as e:
            log(f"[deck] couldn't hold the session: {e}")
        try:
            self.state.set("thinking" if working else "idle")
        except Exception:
            pass

    def _say_live(self, text):
        """Say ONE line right now, through the open conversation.

        A screen walkthrough speaks between clicks — "now click Privacy" —
        without the model being asked anything, so it needs a way in that is
        not a tool result. If the conversation has closed there is nothing to
        say it through, and saying it in the local voice would be a different
        voice mid-sentence, so it is dropped with a line in the log.
        """
        session = getattr(self, "live", None)
        if session is None or not session.is_running():
            log(f"[point] wanted to say {text[:40]!r} but the conversation "
                f"has ended")
            return
        session.speak(text, log=log)

    def _narrate_deck(self, script):
        """The presentation is on screen. Tell Neo to start talking.

        Pushed into the SAME live session rather than returned from the tool,
        because the tool returned twenty seconds ago — that is the whole point
        of the rewrite. If the session has since closed there is nothing to say
        it through, and saying it locally would be the wrong voice reading a
        six-paragraph script, so it is dropped with a line in the log.
        """
        session = getattr(self, "live", None)
        if session is None or not session.is_running():
            log("[deck] the deck is ready but the conversation has ended — "
                "not narrating it")
            return
        # It has to be the SAME conversation. This used to check only that
        # SOME session was running, and self.live is rebound every time a new
        # socket opens — so a deck whose session had died in the meantime was
        # narrated into whatever conversation happened to be open when it
        # finished. That is the bug where Neo suddenly answers something from
        # several minutes ago: it is not answering late, it is answering the
        # right question into the wrong conversation.
        wanted = getattr(self, "_deck_session", None)
        if wanted is not None and session is not wanted:
            log("[deck] ready, but this is a different conversation now — "
                "not narrating it")
            return
        # And it has to still be recent. A build that took minutes (a 503 storm
        # on the art call will do it) is no longer what the user is waiting for,
        # even inside one long session.
        asked = getattr(self, "_deck_asked_at", 0.0)
        if asked and time.time() - asked > DECK_STALE_SEC:
            log(f"[deck] ready after {time.time() - asked:.0f}s — too late to "
                f"be what they asked for; not narrating it")
            return
        # DELIVER IT A FEW SLIDES AT A TIME, not as one wall of text.
        #
        # A whole walkthrough handed over in a single turn came back cut off:
        # neo.log 11:58:13-11:58:50, Neo read two slides of five and stopped
        # mid-sentence on "applies its learned knowledge", then nothing until
        # the idle timer closed the session thirty seconds later. Raising the
        # output ceiling did not fix it, because the ceiling was not what it
        # hit — a model asked to recite several hundred words verbatim in one
        # turn simply stops partway, and that failure is silent.
        #
        # Short turns do not have that problem. The tracker follows Neo's
        # actual words either way, so nothing about the sync changes; the deck
        # advances on slide three because Neo said slide three, exactly as
        # before. The next piece goes in when the audio for this one has
        # drained, so it sounds continuous.
        parts = [p.strip() for p in (script or "").split("\n\n") if p.strip()]
        if len(parts) <= DECK_CHUNK + 1:
            if session.speak(script, log=log):
                log("[deck] handed Neo the script — narrating now")
            return
        head, rest = parts[0], parts[1:]
        first = "\n\n".join([head] + rest[:DECK_CHUNK])
        if not session.speak(first, log=log):
            return
        log(f"[deck] narrating now — {len(rest)} paragraphs, "
            f"{DECK_CHUNK} at a time")
        threading.Thread(target=self._narrate_rest, daemon=True,
                         name="neo-deck-narration",
                         args=(session, rest[DECK_CHUNK:])).start()

    def _narrate_rest(self, session, parts):
        """Feed the remaining narration in, a few paragraphs at a time, each
        one going out as the previous one finishes playing."""
        while parts:
            if session is not getattr(self, "live", None) \
                    or not session.is_running():
                log("[deck] the conversation ended mid-walkthrough — stopping")
                return
            if not self._wait_until_quiet(session):
                log("[deck] Neo never finished that paragraph — stopping "
                    "rather than talking over them")
                return
            chunk, parts = parts[:DECK_CHUNK], parts[DECK_CHUNK:]
            if not session.speak("Keep going, in the same voice, without "
                                 "any preamble:\n\n" + "\n\n".join(chunk),
                                 log=log):
                return

    def _wait_until_quiet(self, session, limit=DECK_TURN_MAX_S):
        """Block until Neo has said a paragraph AND gone quiet again.

        WAITING FOR SILENCE IS NOT ENOUGH, and getting that wrong broke the
        first real deck this ran on. When a chunk is handed over the buffer is
        still empty — the audio has not come back yet — so "is it quiet?" is
        instantly true and the next chunk went out about a second later. All
        five paragraphs were pushed as three overlapping turns inside three
        seconds; Neo said the first two and the rest were lost, the tracker
        never reached the last slide, and the window therefore never closed
        itself. neo.log 13:22:24 to 13:23:19, verbatim: "narrating now — 5
        paragraphs, 2 at a time" ... "Neo never finished that paragraph".

        So wait for them to START first, then for them to stop.
        """
        playback = getattr(session, "_playback", None)
        if playback is None:
            return False
        deadline = time.time() + limit
        # They have to begin. If nothing is ever queued the turn produced no
        # audio at all, and pushing more at it would only make that worse.
        started = False
        while time.time() < deadline:
            if not session.is_running():
                return False
            if playback.pending() > 0:
                started = True
                break
            time.sleep(0.2)
        if not started:
            return False
        quiet = 0
        while time.time() < deadline:
            if not session.is_running():
                return False
            # The buffer empties between phrases as well as at the end, so a
            # single empty reading is not "finished" — it has to stay empty.
            quiet = quiet + 1 if playback.pending() == 0 else 0
            if quiet >= 6:
                return True
            time.sleep(0.2)
        return False

    def _live_slow(self):
        """A turn is taking a long time. Say so — and keep waiting.

        This used to be where the session got destroyed. A thinking model and
        a wedged socket are both silence, so cutting it off after a few seconds
        is a guess, and it guessed wrong on a real question thirteen seconds
        in. Now the only thing that happens here is that the user stops wondering.
        """
        try:
            self._ack("medium")
        except Exception as e:
            log(f"[live] couldn't say it's still going: {e}")

    def _live_ack(self, names):
        """A live tool call is starting. Cover it with ONE spoken line, and only
        if it is actually slow.

        Two rules, both of them things the user noticed out loud:

        ONE PER QUESTION. The model does not issue all its tool calls in a
        single batch — it searches, reads the result, then searches again — and
        this fires per batch. So one question produced "Let me check." followed
        by "One sec.", which is not how a person talks. The turn counter is
        bumped on key-down; the first ack of a turn claims it and the rest of
        that turn stays quiet.

        ONLY WHEN SLOW. A filler exists to cover dead air. If the tool comes
        back before the line would even have started, there was no dead air to
        cover and the line just talks over the answer. So nothing is spoken for
        ACK_DELAY_S, and the guards are re-checked at the end of that wait
        rather than at the start of it — by then we know whether the model has
        started speaking, which is the only real signal that the work is done.

        Off-thread throughout, because _ack blocks on sd.wait() until the line
        finishes and this is called from the asyncio receive task — blocking
        there freezes the whole session (see _close_mic_soon for the same
        lesson).
        """
        # Every slow tool gets a line. The map below picks the FLAVOUR; a
        # tool that isn't in it still gets "quick" unless it is one of the
        # instant ones. Before this, a tool missing from the map got NOTHING
        # — fourteen seconds of dead air while free_time_on_screen read the
        # calendar (22:04:28 → 22:04:42), which is the silence he calls
        # "it doesn't say one sec, hold on".
        kind = None
        for n in names or ():
            if n in self._ACK_FOR_TOOL:
                kind = self._ACK_FOR_TOOL[n]
                break
            if n not in live_mod._INSTANT_TOOLS and kind is None:
                kind = "quick"
        if kind is None:
            return

        turn = getattr(self, "_ack_turn", 0)
        if getattr(self, "_acked_turn", None) == turn:
            return                      # this question has already had its line
        self._acked_turn = turn

        # Tools that are ALWAYS slow (the browser, vision, a directory lookup)
        # don't need the wait that keeps a quick tool from being talked over.
        delay = self.ACK_DELAY_S if kind in ("quick", None) else min(self.ACK_DELAY_S, 0.5)

        def _fire():
            time.sleep(delay)
            if getattr(self, "_ack_turn", 0) != turn:
                return                  # they asked something else; that turn's line
            # Never talk over the live model. A tool call normally means it has
            # gone quiet, but if it has started answering the cached line would
            # play ON TOP of it — two voices at once, which is exactly what it
            # sounds like: a second Neo in the room.
            session = getattr(self, "live", None)
            playback = getattr(session, "_playback", None) if session else None
            if playback is not None and getattr(playback, "playing", False):
                return
            if self.state.get() == "speaking":
                return
            self._ack(kind)

        threading.Thread(target=_fire, daemon=True, name="neo-live-ack").start()

    def set_voice_mode(self, mode, say=True):
        """normal / whisper / bedtime. Idempotent; returns True if it changed.

        One switch sets all four things a mode is made of — how loud Neo is on
        the local voice and on the socket, how hard it listens, and where the
        "that was silence" line sits — so the three can never drift apart.
        """
        mode = clean_mode(mode)
        if mode == getattr(self, "voice_mode", "normal"):
            return False
        self.voice_mode = mode
        _VOICE_MODE["mode"] = mode
        _save_voice_prefs(voice_mode=mode, voice_mode_at=time.time())

        out = {"normal": 1.0, "whisper": WHISPER_LIVE_GAIN,
               "bedtime": BEDTIME_LIVE_GAIN}[mode]
        mic = BEDTIME_MIC_GAIN if mode == "bedtime" else 1.0
        try:
            live_mod.set_output_gain(out)
            live_mod.set_input_gain(mic)
        except Exception:
            pass
        try:
            import hush as _hush
            # "Shush" has to work in a whisper too, or bedtime would be the one
            # mode you cannot interrupt.
            _hush.set_sensitivity(mic, BEDTIME_MIC_GATE if mode == "bedtime"
                                  else _hush.RMS_GATE)
        except Exception:
            pass
        try:
            # THE ONE THAT ACTUALLY MAKES A WHISPER AUDIBLE. Amplifying is not
            # enough on its own: two of turn_opens' three tests are ratios
            # against the room, so gain moves the audio and the bar together.
            # Bedtime lowers the bar as well — a whisper does not stand twice
            # above the noise floor, and it never will.
            if mode == "bedtime":
                live_mod.set_listening_profile(
                    base=BEDTIME_VOICE_BASE, peak_ratio=BEDTIME_PEAK_RATIO,
                    needed=BEDTIME_VOICE_CHUNKS)
            else:
                live_mod.set_listening_profile()      # back to the room default
        except Exception:
            pass
        log(f"[voice] {mode} mode"
            + (f" — microphone x{mic:.0f}, gate {self.mic_gate():.4f}"
               if mode == "bedtime" else ""))
        return True

    def mic_gate(self):
        """The level below which captured audio counts as silence, right now."""
        return BEDTIME_MIC_GATE if self._whispering() else MIC_GATE

    def mic_gain(self):
        return BEDTIME_MIC_GAIN if self._whispering() else 1.0

    def bedtime(self):
        return self._whispering()

    def _whispering(self):
        """Bedtime, or stealth: either way they are not going to speak up, so the
        mic is amplified and the silence gate lowered before anything judges
        the level."""
        return (getattr(self, "voice_mode", "normal") == "bedtime"
                or (getattr(self, "stealth", None) is not None and self.stealth.active()))

    # ---- hush: listening ONLY while Neo's own voice is on the speaker ---- #
    def _build_hush(self):
        """Made once, after the models are up. Never fatal — if this cannot be
        built, the fn key still stops Neo instantly, as it always has."""
        # OFF BY DEFAULT, and the reason is the orange dot.
        #
        # macOS shows its microphone indicator whenever ANY app holds the mic,
        # by design, with no API to suppress it — that is the entire point of
        # the light. This listener opens the mic while Neo SPEAKS, so with it
        # on, the dot appears during every single answer. the user spotted that
        # before it ever shipped to them, and they are right: a light that says
        # "something is listening" must mean it, and it must be rare.
        #
        # The fn key has always stopped Neo instantly and costs no microphone
        # at all, so the default loses nothing. NEO_HUSH=1 turns voice hush on
        # for anyone who wants it.
        if os.getenv("NEO_HUSH") != "1":
            log("[hush] voice hush is off (no mic held while Neo speaks, so no "
                "orange dot). fn still stops me instantly. NEO_HUSH=1 enables.")
            return None
        try:
            import hush as hush_mod

            def _local_only(window):
                # The LOCAL model, deliberately, never the cloud ears. This is
                # the one microphone the user did not explicitly open, so the
                # audio must not leave the machine — not to a provider, not to
                # a log, not to disk.
                text, _confident = self.speech._transcribe_local(window)
                return text

            # A CONCRETE DEVICE, NEVER None.
            #
            # pick_input_device returns None to mean "use the system default",
            # which is correct for recording — but this stream opens while Neo
            # is SPEAKING, and the system default input becomes the AirPods the
            # moment they connect. Opening a Bluetooth headset's microphone
            # flips it into hands-free mode: output drops to mono telephone
            # quality mid-sentence. That is what the user heard as the voice being
            # glitchy and broken, and it started the day this listener landed.
            device = safe_input_device()
            if device is None:
                log("[hush] no built-in mic to listen on safely — off. "
                    "The fn key still stops me.")
                return None

            return hush_mod.HushListener(
                transcribe=_local_only,
                on_action=self._hush_action,
                neo_text=lambda: self.last_spoken or self._live_partial(),
                device=device,
                log=log)
        except Exception as e:
            log(f"[hush] unavailable ({e}) — the fn key still stops me")
            return None

    def _live_partial(self):
        """What the live model is saying right now, for the echo guard."""
        try:
            return getattr(self.live, "last_text", "") or ""   # live.py property
        except Exception:
            return ""

    def _hush_action(self, action, phrase):
        """Called from the hush worker. Must be quick and must never raise.

        Only ever "hush" — see hush.should_act for why mode changes are not
        allowed to come from this microphone.
        """
        if action != "hush":
            return
        # HUSH. Exactly what a key press does, minus the key: cut the local
        # voice, drop whatever the live socket has already sent, and stop.
        log(f"[hush] {phrase!r} — stopping")
        self._interrupt.set()
        try:
            with live_mod.AUDIO_LOCK:
                sd.stop()
        except Exception:
            pass
        session = getattr(self, "live", None)
        if session is not None:
            try:
                session.silence()
            except Exception:
                pass
        self.state.set("idle")

    def _speaking_changed(self, speaking):
        """Neo started or stopped talking. Hand the mic work to a worker and
        RETURN — this is called from live.py's asyncio receive loop, which is
        the task feeding the speaker.

        It used to open the stream right here. Opening a PortAudio input stream
        is device I/O and routinely takes over a hundred milliseconds; spending
        that on the receive loop stops audio reaching the playback buffer, the
        buffer drains, and _callback writes silence into the middle of the
        sentence. That is what the user heard as stuttering and broken English on
        the first answer — worst on the first, because the device is cold.

        The codebase already learned this once, in _open_mic_now: device I/O
        never happens on a thread something else is waiting on.
        """
        if bool(speaking) == bool(getattr(self, "_speaking_now", False)):
            return
        self._speaking_now = bool(speaking)
        if getattr(self, "_hush", None) is None:
            return
        try:
            self._hush_cmds.put_nowait("start" if speaking else "stop")
        except Exception:
            pass

    def _audio_check(self):
        """Is Neo's voice going where the user is actually listening?

        NO MICROPHONE. A true round-trip — play a tone, record it — would be a
        better test and would light the orange indicator every time it ran,
        which is not a trade worth making for a check that should be constant.

        What can be known without listening: CoreAudio names the device the
        system is really using, PortAudio names the device Neo would open, and
        those two disagreeing IS the bug that sent every answer to the laptop
        speakers while the AirPods sat idle. Returns a complaint, or None.
        """
        try:
            real = audio_out.system_default_output()
            cached = audio_out.portaudio_default_output(sd)
            if not real or not cached:
                return None
            if audio_out.output_is_stale(real, cached):
                return (f"My voice is going to {cached}, but your Mac is set "
                        f"to {real}. I'll follow it at the next gap.")
        except Exception:
            return None
        return None

    def _device_watch(self, every=3.0):
        """Follow the speakers the user is actually using — but only ever between
        turns.

        Re-enumeration is not a lookup, it is a teardown: sd._terminate()
        invalidates EVERY open PortAudio stream, ours included. The first
        version of this ran inline, just before opening an output stream, and
        the log shows exactly what that cost:

            21:19:25  (holding — opening a conversation)
            21:19:25  [audio] output moved to 'MacBook Air Speakers'
            21:19:28  (let go, but nothing was said — ignoring)
            21:19:31  (let go, but nothing was said — ignoring)

        The microphone was already open when the reset fired, so it was
        invalidated and captured nothing. Two questions in a row vanished, and
        the answer that did arrive came out broken.

        So: one owner, on its own clock, acting only when Neo is neither
        listening nor speaking and no stream of ours is open. A headset that
        connects mid-sentence is followed a few seconds later, at the next gap,
        which nobody can hear.
        """
        while True:
            time.sleep(every)
            try:
                if not self._audio_idle():
                    continue
                with live_mod.AUDIO_LOCK:
                    if not self._audio_idle():
                        continue          # re-check under the lock
                    moved = audio_out.ensure_current_output(sd, log)
                if moved:
                    # Say it on screen, never out loud — this fires while Neo
                    # is idle, and an assistant that announces its own plumbing
                    # unprompted is worse than one that stays quiet.
                    try:
                        self._deliver_insight({
                            "key": "audio-output-moved", "kind": "info",
                            "urgency": "low", "title": "Voice moved speakers",
                            "detail": f"Now playing through "
                                      f"{audio_out.describe()}."})
                    except Exception:
                        pass
            except Exception as e:
                log(f"[audio] device watch: {e}")

    def _audio_idle(self):
        """True only when tearing PortAudio down cannot break anything."""
        try:
            if self.state.get() in ("listening", "speaking"):
                return False
            if getattr(self, "_speaking_now", False):
                return False
            if getattr(self, "_ack_playing", False):
                return False
            listener = getattr(self, "_hush", None)
            if listener is not None and listener.listening():
                return False
            session = getattr(self, "live", None)
            if session is not None:
                if getattr(session, "_holding", False):
                    return False
                if getattr(session._playback, "_stream", None) is not None:
                    return False
                if getattr(session, "_mic_stream", None) is not None:
                    return False
        except Exception:
            return False          # can't prove it is safe -> assume it isn't
        return True

    def _hush_loop(self):
        """Owns the hush microphone. Un-killable: one bad open must never end
        the loop, or hush silently stops working for the rest of the session."""
        while True:
            cmd = self._hush_cmds.get()
            try:
                listener = getattr(self, "_hush", None)
                if listener is None:
                    continue
                if cmd == "start":
                    # Let the answer get going before adding a device open to
                    # the pile. Nobody shushes the first syllable, and the
                    # first moments of playback are exactly when the buffer is
                    # shallowest and least able to absorb a hiccup.
                    time.sleep(HUSH_OPEN_DELAY_S)
                    if not getattr(self, "_speaking_now", False):
                        continue        # already finished; never open at all
                    listener.start()
                else:
                    listener.stop()
            except Exception as e:
                log(f"[hush] mic {cmd} failed: {e}")

    def whispering(self):
        """True in whisper AND bedtime — both mean 'answer quietly'."""
        return getattr(self, "voice_mode", "normal") in ("whisper", "bedtime")

    def speech_gain(self):
        """The multiplier the local voice is played at right now (kept for the
        tests and the log; _speak uses mode_factor)."""
        return {"normal": TTS_GAIN, "whisper": WHISPER_GAIN,
                "bedtime": BEDTIME_GAIN}[getattr(self, "voice_mode", "normal")]

    def mode_factor(self):
        """How much quieter than normal the current mode is, as a ratio that
        is correct for EITHER engine. WHISPER_GAIN and BEDTIME_GAIN were tuned
        as absolute multipliers on Kokoro, so dividing by Kokoro's normal gain
        recovers the intended ratio: whisper ≈ 0.24, bedtime ≈ 0.16."""
        return self.speech_gain() / max(TTS_GAIN, 1e-6)

    def _cut_ack(self):
        """The live model has started speaking — stop the filler mid-word.

        The filler exists for exactly one reason: to cover dead air while a
        tool runs. The moment the real answer arrives there is no dead air
        left to cover, so continuing to play it is not politeness, it is a
        second voice talking over the first.

        _live_ack already refuses to START a line while the model is speaking.
        That guard can only see the present: at 17:21:30 the model was silent,
        so the line began, and at 17:21:32 the model began on top of it. This
        is the other half — the model announcing itself to whatever is already
        making noise.
        """
        if not getattr(self, "_ack_playing", False):
            return
        self._ack_playing = False
        try:
            with live_mod.AUDIO_LOCK:
                sd.stop()
        except Exception:
            pass

    def _ack(self, kind):
        """Millisecond acknowledgement: play a pre-synthesized voice line
        suited to how long this kind of task takes, then return so the real
        work can start.

        The line is chosen from the ones that ACTUALLY EXIST in Neo's real
        voice, not from the whole list. Picking freely and hoping is what made
        them sound like a different person most of the time: only a few lines
        per bucket are ever synthesised through the cloud voice, so ten times
        out of twelve the filler came out of the local engine — which is both
        "the voice changes back to the old one" and "they only ever says one
        thing", from a single cause.
        """
        every = [p for lines in banter.ACKS.values() for p in lines]
        ready = self.speech.cached_phrases(every)
        text = banter.pick(kind, available=ready)
        audio = self.speech.ack_audio(text)
        if audio is None:
            # Nothing in this bucket is cached yet (the background fill is
            # still running, or the day's TTS quota is gone). Any other cached
            # line will do — they all mean "working on it".
            for alt in sorted(ready):
                audio = self.speech.ack_audio(alt)
                if audio is not None:
                    text = alt
                    break
        if audio is None:
            # SAY SOMETHING. Staying silent here was a mistake and the log
            # shows exactly what it cost: the Gemini cache failed to build (it
            # ran before the client existed), so every line was missing, so Neo
            # made no sound at all from the moment a tool started — and the
            # answer watchdog reset the session before they ever spoke. the user
            # asked a question and got nothing back.
            #
            # A filler in the local voice is a cosmetic problem. Silence while
            # Neo is working is indistinguishable from Neo being broken, which
            # is the thing the filler exists to prevent. So: cloud voice when
            # it's ready, local voice when it isn't, never nothing.
            # NOT DURING A CONVERSATION.
            #
            # _speak goes to the local engine, on the jobs queue, through its
            # own output stream — which _cut_ack cannot stop, because it only
            # knows how to stop sd.play(). So an uncached filler during a live
            # answer played bm_fable straight over Charon and could not be
            # interrupted. That is both of the things the user reported at 15:14:37
            # on 9 Sept: the "repeat" (two Neos talking) and the filler that
            # "sounds like shit" (the wrong voice entirely).
            #
            # The old comment here argued that silence is worse than the wrong
            # voice. That was written when the alternative was silence for the
            # WHOLE tool call. It isn't: the model is about to speak anyway, in
            # the right voice, usually within a second. Half a second of quiet
            # beats a second voice.
            session = getattr(self, "live", None)
            if session is not None and session.is_running():
                log(f"[voice] '{kind}' not cached — staying quiet rather than "
                    "talking over the answer in the wrong voice")
                return
            log(f"[voice] '{kind}' not cached yet — using the local voice once")
            self._speak(text)
            return
        log(f"Neo: {text}")
        self._mark_first_sound()
        self.state.set("speaking")
        self._ack_playing = True
        try:
            # NO GAIN. This used to be tanh(audio * 2.1) with the comment
            # "match live loudness", and it did the opposite: these lines come
            # from Gemini TTS already peaking around 0.85, so the boost drove
            # two to six percent of samples past 1.0 into the squash, lifted
            # RMS by about seventy percent, and hardened the timbre. The live
            # ANSWER is played raw straight off the socket, so the filler
            # arrived sounding like a different, harsher voice a beat before
            # Neo spoke normally — which is exactly what the user described.
            #
            # The gain exists for Kokoro, which really is quieter. Cloud audio
            # is already at the right level and must be left alone.
            audio = np.asarray(audio, dtype=np.float32)
            if self.whispering():
                # The filler is pre-synthesised at full level, so it is the one
                # thing that would still shout while everything else whispers.
                audio = audio * self.mode_factor()
            # NO DEVICE RE-ENUMERATION HERE. The filler plays DURING a live
            # answer, and ensure_current_output can call _terminate() —
            # which invalidates every open PortAudio stream, including the one
            # the live socket is speaking through. Re-reading the devices is
            # for paths where nothing else is playing: _speak, and the live
            # playback stream's own open.
            sd.play(audio, samplerate=TTS_RATE)
            sd.wait()
        except Exception as e:
            log(f"ack audio error: {e}")
        finally:
            self._ack_playing = False
            sd.stop()

    def _speak(self, text):
        if self._stealth_on():
            # STEALTH: the words go to the box, and nothing else happens.
            text = (text or "").strip()
            turn = getattr(self, "_stealth_turn", None)
            if turn is not None and turn.get("superseded") and not turn.get("done"):
                # they added to (or cancelled) the question while this was being
                # worked out; the combined one is next. Don't show a stale reply.
                log(f"Neo (dropped, superseded): {text}")
                return
            if text:
                log(f"Neo: {text}")
                self.last_spoken = text
                import stealth as _stealth
                _stealth.box.add("Neo", text)
                _stealth.box.show(focus=True)
            return
        text = clean_for_speech(text)   # never read markdown/URLs/emoji aloud
        if not text:
            return
        log(f"Neo: {text}")              # EVERYTHING spoken lands in the transcript
        self.last_spoken = text          # for "say that again"
        # Fallback-path deck sync. The live path follows Neo's real transcript;
        # this one can only estimate, because synthesis gives back audio and not
        # words. Only ever runs when live mode is already unavailable.
        try:
            deck.pace(text)
        except Exception:
            pass
        self._interrupt.clear()
        self.state.set("speaking")
        self._speaking_changed(True)
        # STREAM: play each segment the moment it's synthesized instead of
        # waiting for the whole reply — first sound after ONE sentence, not
        # all of them. Order is guaranteed (one generator, sequential); the
        # old "jumbled words" bug came from buggy TEXT splitting before
        # synthesis (mangled emails/decimals), not from streaming playback.
        # Barge-in (fn press) still cuts within ~0.2s via short slices.
        SLICE = TTS_RATE // 5
        stream = None
        try:
            try:
                # No device check here — _device_watch owns that, and only
                # when nothing is streaming. See its docstring for the cost.
                with live_mod.AUDIO_LOCK:   # never open PortAudio unserialised
                    stream = sd.OutputStream(samplerate=TTS_RATE, channels=1,
                                             dtype="float32")
                    stream.start()
            except Exception as e:
                log(f"speaker wouldn't open ({e}) — re-reading the devices")
                live_mod.reset_portaudio(sd, log)
                with live_mod.AUDIO_LOCK:
                    stream = sd.OutputStream(samplerate=TTS_RATE, channels=1,
                                             dtype="float32")
                    stream.start()
            for chunk, _rms in self.speech.synth(text):
                if self._interrupt.is_set():
                    break
                a = np.ascontiguousarray(np.asarray(chunk, dtype=np.float32).reshape(-1))
                if not a.size:
                    continue
                # ONLY the mode. Engine level is already right coming out of
                # synth (Kokoro boosted, Gemini left alone), so this is a
                # plain ratio — 1.0 normally, a fraction when whispering — and
                # never tanh, which would re-clip audio that is already at
                # level.
                a = a * self.mode_factor()
                i = 0
                while i < a.size:
                    if self._interrupt.is_set():
                        break
                    seg = a[i:i + SLICE]
                    self._mark_first_sound()
                    self.state.set_level(min(1.0, float(np.sqrt(np.mean(seg ** 2))) * 6))
                    stream.write(seg)
                    i += SLICE
        except Exception as e:
            log(f"speech error: {e}")
        finally:
            # The mic closes the moment the voice does. This is the promise the
            # whole hush feature rests on, so it lives in a finally.
            self._speaking_changed(False)
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass


# --------------------------------------------------------------------------- #
# Global fn-key listener via a Quartz event tap (hold-to-talk)
# --------------------------------------------------------------------------- #
FN_MASK = Quartz.kCGEventFlagMaskSecondaryFn

# Push-to-talk keys, by hardware keycode. fn stays the default because that's
# the muscle memory, but it is genuinely the worst key on the board for this:
# macOS reserves it (dictation, the emoji picker, input-source switching) and
# it's the one modifier without a durable HID state, which is the whole reason
# this file grew a guard, a watchdog and a recovery path. Right-Option is an
# ordinary modifier that reports its state correctly, so it is wired up in
# parallel — no setting to change, both keys just work. If fn ever misbehaves
# again, hold right-Option instead and none of the recovery code is involved.
FN_KEYCODE = 0x3F           # kVK_Function (the globe key)
RIGHT_OPTION_KEYCODE = 0x3D  # kVK_RightOption
# fn is the only key by default — it's the one the user wants and a second key
# just makes the tap logic harder to reason about. NEO_PTT=both re-enables
# right-option as a spare if fn ever gets taken away by macOS again.
PTT_KEYS = {FN_KEYCODE: FN_MASK}
if (os.getenv("NEO_PTT") or "").lower() in ("both", "option", "right-option"):
    PTT_KEYS[RIGHT_OPTION_KEYCODE] = Quartz.kCGEventFlagMaskAlternate
PTT_KEYCODE = FN_KEYCODE     # what ptt_physically_down() checks by default
PTT_NAMES = {FN_KEYCODE: "fn", RIGHT_OPTION_KEYCODE: "right-option"}


# Keep tap objects alive for the whole program so they're not garbage-collected.
_TAP_REFS = {}


def reconcile_fn_state(down, real_down, held_s, max_hold_s,
                       up_votes=1, votes_needed=1, tap_lost=True):
    """Decide what to do about a hold we THINK is active. Pure, so it's tested.

      down          — our belief that the key is held (the tap's holder).
      real_down     — the OS's view: True, False, or None if unknown.
      up_votes      — how many CONSECUTIVE times the OS has said "up", including
                      this reading. Defaults to 1 so a caller that doesn't
                      count votes keeps the original single-reading behaviour.
      votes_needed  — how many of those it takes to act.
      tap_lost      — was the event tap actually disabled during this hold?
                      Defaults True so existing callers behave as before.

    Returns 'release' when the key-up was genuinely lost, 'maxhold' when a real
    hold ran past the ceiling, else None.

    Two guards, and the second one is the one that matters.

    The votes came first, for the truncation bug: a single False used to force a
    release, so one unreliable sample threw away the sentence in progress. Three
    agreeing samples is better than one, but it did not fix it, because
    ptt_physically_down is not wrong at random — it is wrong for as long as the
    HID layer keeps the globe key's synthesized flag clear, which is easily
    longer than three quarters of a second. The log shows the result: a hold
    that force-released at :10 and again at :15, one press, the mic indicator
    blinking off and back on in between.

    So the real guard is tap_lost. Ask what this function is actually FOR: it
    recovers a key-UP that was never delivered. A key-up can only go undelivered
    if the tap was dead when it fired — that is the only failure mode there is.
    While the tap is alive it will deliver the key-up itself, so there is
    nothing to recover and no reason to guess from a reading we know lies. When
    the tap never dropped, the only thing that can still end a hold here is the
    max-hold ceiling.
    """
    if not down:
        return None
    if real_down is False and up_votes >= max(1, votes_needed) and tap_lost:
        return "release"
    if held_s >= max_hold_s:
        return "maxhold"
    return None


def watchdog_should_unstick(state, busy_locked, jobs_empty, fn_down, listen_age, threshold):
    """True when the glow is stuck on 'listening' with no work in flight and the
    OS confirms the push-to-talk key is physically up — a lost key-up left the
    recorder running. Pure so it's testable.

    busy_locked means a real turn is running (not our case), and fn_down must be
    exactly False so an unknown reading never cuts a hold. The threshold is what
    changed: it used to be 2 seconds, which put an ordinary mid-sentence pause
    inside the kill window and silently threw away half of what the user said.
    """
    return (state == "listening" and not busy_locked and jobs_empty
            and fn_down is False and listen_age >= threshold)


def ptt_physically_down(keycode=None):
    """The OS's own view of the push-to-talk key, independent of our event tap.
    True / False / None when Quartz can't answer.

    This used to be one line: read the HID modifier flags, test the secondary-fn
    bit. That reading is not trustworthy for this particular key. Shift and
    Command are durable hardware modifier states; the globe/fn key is handled up
    in Apple's HID layer and its flag is synthesized onto individual
    flagsChanged events, so querying it out of band can report "up" while the
    key is very much held. Everything downstream treated a False as gospel and
    killed the recording.

    So ask twice, two different ways. CGEventSourceKeyState asks about the
    physical key by keycode; CGEventSourceFlagsState asks about the modifier
    bit. DOWN WINS: False is only returned when both authorities agree the key
    is up, and None when neither can answer. A false release is now a
    two-independent-failures event instead of a coin flip.
    """
    code = PTT_KEYCODE if keycode is None else keycode
    mask = PTT_KEYS.get(code, FN_MASK)

    key_view = None
    try:
        key_view = bool(Quartz.CGEventSourceKeyState(
            Quartz.kCGEventSourceStateHIDSystemState, code))
    except Exception:
        key_view = None

    flag_view = None
    try:
        flags = Quartz.CGEventSourceFlagsState(
            Quartz.kCGEventSourceStateHIDSystemState)
        flag_view = bool(flags & mask)
        # THE FLAG LIES AFTER AN ARROW KEY.
        #
        # macOS sets kCGEventFlagMaskSecondaryFn for arrow keys, the function
        # row and several international layouts — not only for the physical
        # globe key — and it sets kCGEventFlagMaskNumericPad alongside it when
        # it does. That pairing latches: measured on this Mac on 9 Sept with
        # nothing held at all, flags were 0xa00100, which is SecondaryFn AND
        # NumericPad, while CGEventSourceKeyState(0x3F) correctly said False.
        #
        # Because DOWN WINS below, that false positive won permanently. Every
        # consequence the user reported follows from it: act.can_act() refused
        # every click with "I'm not clicking while you're mid-sentence", and
        # the fn-guard could never force a release, so a hold that lost its
        # key-up sat there blue until a watchdog cleaned it up seconds later.
        #
        # So the flag only counts as evidence when NumericPad is NOT also set.
        # When it is, the keycode is the only honest reading — and the keycode
        # is the physical key, which is what the question actually asks.
        if flag_view and code == FN_KEYCODE:
            try:
                if flags & Quartz.kCGEventFlagMaskNumericPad:
                    flag_view = None      # unusable, not "up"
            except Exception:
                pass
    except Exception:
        flag_view = None

    if key_view is None and flag_view is None:
        return None
    if key_view or flag_view:
        return True
    return False


# Kept under the old name so anything still calling it keeps working.
fn_physically_down = ptt_physically_down


# --------------------------------------------------------------------------- #
# A tap that is ENABLED but DEAF
# --------------------------------------------------------------------------- #
# CGEventTapIsEnabled can answer True while the tap delivers nothing at all.
# Sleep/wake does it: on 6 September the Mac slept at 17:26, woke at 17:30, and
# Neo never saw another fn press. Nothing was "disabled", so the existing
# watchdog had nothing to catch, and the key was simply dead for an hour and a
# half while the run loop sat there perfectly healthy waiting for events that
# were never coming.
#
# The detector is a COMPARISON, not a timeout. The HID system knows when it
# last saw a modifier change and so do we; if the system has seen one much more
# recently than our tap has delivered one, events are going past us.
TAP_DEAF_S = float(os.getenv("NEO_TAP_DEAF", "90"))
TAP_REBUILD_COOLDOWN_S = 60.0
# How many modifier events the SYSTEM must have counted that our tap never
# delivered. Counting is the whole correction: the first version compared
# "seconds since the system last saw one" against "seconds since we last got
# one", and a single event we legitimately never see — secure input, the lock
# screen, another login session — read as total deafness. It rebuilt the tap
# 134 times in four days, some of them exactly 60 seconds apart, which is the
# cooldown floor. A count says how MANY we missed, and missing ten in a row is
# not something a lock screen explains.
TAP_MISSED_EVENTS = int(os.getenv("NEO_TAP_MISSED", "12"))

_TAP_HEALTH = {"last_event": 0.0, "last_rebuild": 0.0, "rebuilds": 0,
               "count_at_last_event": 0}


def system_flags_count():
    """How many modifier changes the HID system has counted, ever. Monotonic."""
    try:
        return int(Quartz.CGEventSourceCounterForEventType(
            Quartz.kCGEventSourceStateHIDSystemState,
            Quartz.kCGEventFlagsChanged))
    except Exception:
        return None


def note_tap_event(now=None):
    """The tap delivered something. Called for every event including the
    'tap disabled' ones — any delivery at all proves it is still listening.

    Records the system's counter at the same moment, so the next check can ask
    how many events went past us rather than merely how long it has been.
    """
    _TAP_HEALTH["last_event"] = time.time() if now is None else now
    c = system_flags_count()
    if c is not None:
        _TAP_HEALTH["count_at_last_event"] = c


def system_flags_age():
    """Seconds since the HID system last saw a modifier change, or None if
    Quartz won't say. This is the second opinion the detector needs."""
    try:
        return float(Quartz.CGEventSourceSecondsSinceLastEventType(
            Quartz.kCGEventSourceStateHIDSystemState,
            Quartz.kCGEventFlagsChanged))
    except Exception:
        return None


def tap_looks_deaf(ours_age, missed, quiet_s=TAP_DEAF_S,
                   need_missed=TAP_MISSED_EVENTS):
    """True when a lot of modifier events went past a tap that delivered none.
    Pure, so this judgement is tested rather than argued about.

    `missed` is how far the system's counter has advanced since our last
    delivery. Both halves matter: the quiet window stops a scheduling hiccup
    reading as deafness, and the count stops the handful of events a session
    tap legitimately never sees — secure input fields, the lock screen, a
    fast-user-switched session — reading as it either.

    Rebuilding a healthy tap is not free. It briefly removes the run-loop
    source, so a press landing in that window is lost; a rebuild LOOP costs
    exactly the key it is trying to protect.
    """
    if ours_age is None or ours_age < quiet_s:
        return False
    if missed is None:
        return False
    return missed >= need_missed


def rebuild_tap(log=log):
    """Throw the tap away and build a fresh one.

    MAIN RUN LOOP ONLY. It removes and adds a run-loop source, and doing that
    from a worker thread is its own separate bug — which is why the fn-guard
    timer owns this and the background watchdog does not.
    """
    neo = _TAP_REFS.get("neo")
    if neo is None:
        return False
    old_tap, old_source = _TAP_REFS.get("tap"), _TAP_REFS.get("source")
    try:
        if old_tap is not None:
            Quartz.CGEventTapEnable(old_tap, False)
        if old_source is not None:
            Quartz.CFRunLoopRemoveSource(
                Quartz.CFRunLoopGetCurrent(), old_source,
                Quartz.kCFRunLoopCommonModes)
    except Exception as e:
        log(f"WATCHDOG: couldn't retire the old tap ({e}) — building anyway.")
    made = make_event_tap(neo, fatal=False)
    if made is None:
        log("WATCHDOG: the replacement tap wouldn't build. Input Monitoring "
            "may have been revoked — check Privacy & Security.")
        return False
    log("WATCHDOG: fn tap rebuilt — it is listening again.")
    return True


def watch_tap_health(now=None, log=log):
    """One cheap check per fn-guard tick. Returns True if it rebuilt."""
    now = time.time() if now is None else now
    last = _TAP_HEALTH.get("last_event", 0.0)
    if not last:
        return False                    # nothing has ever arrived; nothing to judge
    if now - _TAP_HEALTH.get("last_rebuild", 0.0) < TAP_REBUILD_COOLDOWN_S:
        return False                    # never loop on this
    count = system_flags_count()
    missed = (None if count is None
              else count - _TAP_HEALTH.get("count_at_last_event", 0))
    if not tap_looks_deaf(now - last, missed):
        return False
    log(f"WATCHDOG: the fn tap says it is enabled but {missed} key events went "
        "past it without being delivered — that is what sleep/wake does to a "
        "tap. Rebuilding it.")
    _TAP_HEALTH["last_rebuild"] = now
    _TAP_HEALTH["rebuilds"] = _TAP_HEALTH.get("rebuilds", 0) + 1
    return rebuild_tap(log=log)


def make_event_tap(neo, fatal=True):
    # "key" is which PTT key started the current hold, so the guard asks the OS
    # about the right one. "up_votes" is the consecutive-release counter — see
    # reconcile_fn_state for why one reading is not enough.
    holder = {"down": False, "tap": None, "since": 0.0,
              "key": FN_KEYCODE, "up_votes": 0, "disabled_at": 0.0}

    def callback(proxy, type_, event, refcon):
        try:
            # ANY delivery proves the tap is still listening — including the
            # "you have been disabled" notices, which is why this is first.
            note_tap_event()
            # macOS disables long-lived taps periodically; re-enable them. Note
            # when it happened: a key-up can only have been lost if the tap was
            # actually dead, so the watchdog uses this as corroboration.
            if type_ in (Quartz.kCGEventTapDisabledByTimeout,
                         Quartz.kCGEventTapDisabledByUserInput):
                holder["disabled_at"] = time.time()
                if holder["tap"] is not None:
                    Quartz.CGEventTapEnable(holder["tap"], True)
                return event

            if type_ == Quartz.kCGEventFlagsChanged:
                flags = Quartz.CGEventGetFlags(event)
                # WHICH modifier changed. Reading the keycode instead of just
                # testing one mask is what lets fn and right-Option coexist,
                # and it stops an unrelated Shift press from being read as a
                # release of the key actually being held.
                try:
                    keycode = int(Quartz.CGEventGetIntegerValueField(
                        event, Quartz.kCGKeyboardEventKeycode))
                except Exception:
                    keycode = -1
                # The keycode REFINES the decision, it never gates it. Some
                # keyboards report 0 (or nothing useful) on the fn flagsChanged
                # event, and dropping those would make the key silently dead —
                # so an unrecognised keycode falls back to the mask test, which
                # is how this worked before and is known good.
                if keycode in PTT_KEYS:
                    is_down = bool(flags & PTT_KEYS[keycode])
                else:
                    is_down = any(bool(flags & m) for m in PTT_KEYS.values())
                    keycode = holder.get("key", FN_KEYCODE)
                if DEBUG:
                    print(f"[neo] flagsChanged key={PTT_NAMES.get(keycode, keycode)} "
                          f"flags={flags:#x} down={is_down}")
                if is_down and not holder["down"]:
                    holder["down"] = True
                    holder["key"] = keycode
                    holder["up_votes"] = 0
                    holder["since"] = time.time()   # for the fn-guard's max-hold
                    neo.on_press()
                elif not is_down and holder["down"]:
                    holder["down"] = False
                    holder["up_votes"] = 0
                    neo.on_release()
        except Exception as e:
            print(f"[neo] tap error: {e}")
        return event

    # Only real event types go in the mask. The "tap disabled" events are
    # delivered to the callback automatically and must NOT be masked.
    mask = Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged)
    tap = Quartz.CGEventTapCreate(
        Quartz.kCGSessionEventTap,
        Quartz.kCGHeadInsertEventTap,
        Quartz.kCGEventTapOptionListenOnly,
        mask,
        callback,
        None,
    )
    if tap is None:
        # Name the binary macOS is ACTUALLY judging. The bundle execs python,
        # so the Privacy pane entry that matters is often python's, not
        # Neo.app's — telling someone to grant Neo.app when the process is
        # python sends them to re-tick a box that was never the problem.
        print(
            "\n[neo] No keyboard listener — Input Monitoring and Accessibility\n"
            "      aren't granted yet. System Settings > Privacy & Security.\n"
            "      Grant BOTH to:\n"
            "        ~/Applications/Neo.app\n"
            f"        {python_identity()}\n"
            "      (the second one is what the bundle execs, and it is what\n"
            "       macOS attributes the permission to.)\n"
            "      I'll keep retrying, so grant it and Neo picks it up on its\n"
            "      own — nothing to restart.\n"
        )
        # Exit rather than run deaf. Under the LaunchAgent this is the whole
        # self-healing story: KeepAlive relaunches every ThrottleInterval, so
        # the moment the permission is granted the next relaunch just works and
        # the user never has to go back to a terminal.
        #
        # EXCEPT on a rebuild. Killing a working Neo mid-conversation because a
        # replacement tap wouldn't build would turn a recoverable glitch into a
        # restart, so that path takes None and keeps the old one.
        if not fatal:
            return None
        sys.exit(1)

    holder["tap"] = tap
    source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
    Quartz.CFRunLoopAddSource(
        Quartz.CFRunLoopGetCurrent(), source, Quartz.kCFRunLoopCommonModes
    )
    Quartz.CGEventTapEnable(tap, True)
    # Hold references so the GC can't kill the tap (this was the bug).
    _TAP_REFS["tap"] = tap
    _TAP_REFS["source"] = source
    _TAP_REFS["callback"] = callback
    _TAP_REFS["holder"] = holder
    _TAP_REFS["neo"] = neo   # the fn-guard timer reaches Neo through here
    # Start the clock now. Without this, a tap that has never delivered an
    # event looks exactly like one that has stopped delivering them.
    note_tap_event()
    return tap


class _Heartbeat(NSObject):
    """A no-op timer target. Its only job is to hand control back to Python a few
    times a second so a pending Ctrl-C (SIGINT) actually gets to run."""
    def beat_(self, timer):
        pass


class _FnGuard(NSObject):
    """Belt-and-suspenders against a lost key-up. macOS disables long-lived
    event taps periodically; if the key-UP fires during that window it's gone
    and the recorder would run forever ('stuck listening', mic indicator stuck
    on). Four times a second we ask the OS for the REAL key state and force the
    release the tap missed. Runs on the main run loop — same thread as the tap —
    so there's no race on holder.

    Two things stop it firing on a hold that is still in progress, because it
    did, repeatedly, and that is what made the mic indicator blink mid-sentence.

    Votes: the OS has to say "up" FN_RELEASE_VOTES readings running, and any
    single "down" resets the count. That handles a one-off bad sample.

    Corroboration: it only acts if the tap was ACTUALLY DISABLED during this
    hold. A key-up cannot be lost while the tap is alive — the tap would have
    delivered it — so with a healthy tap there is nothing to recover, and a
    "key is up" reading is just ptt_physically_down being unreliable about the
    globe key. Votes alone were not enough because that reading does not fail at
    random: it stays wrong for as long as the HID layer keeps fn's synthesized
    flag clear, which comfortably outlasts three quarters of a second. One press
    was force-released at :10 and again at :15, and the second on_press in
    between was a re-latched flagsChanged event.

    A genuinely lost key-up still recovers in well under a second, because a
    lost key-up means the tap dropped, which is exactly what we now require.
    """
    def check_(self, timer):
        # FIRST, and outside the hold logic below, which returns early whenever
        # the key is up — and a deaf tap means the key is ALWAYS up as far as
        # Neo can tell, so anything after that return would never run.
        try:
            watch_tap_health()
        except Exception as e:
            print(f"[neo] tap health: {e}")
        try:
            holder = _TAP_REFS.get("holder")
            neo = _TAP_REFS.get("neo")
            if not holder or neo is None or not holder.get("down"):
                return
            real = ptt_physically_down(holder.get("key", FN_KEYCODE))
            if real is False:
                holder["up_votes"] = holder.get("up_votes", 0) + 1
            else:
                holder["up_votes"] = 0

            held = time.time() - holder.get("since", time.time())
            # Did the tap actually drop during THIS hold? If it never did, the
            # key-up cannot have been lost and there is nothing to recover — so
            # a "key is up" reading is the OS being unreliable about fn, not a
            # missed event. Corroboration, not a coin flip.
            tap_lost = holder.get("disabled_at", 0.0) > holder.get("since", 0.0)
            action = reconcile_fn_state(holder["down"], real, held, MAX_HOLD_SEC,
                                        up_votes=holder["up_votes"],
                                        votes_needed=FN_RELEASE_VOTES,
                                        tap_lost=tap_lost)
            name = PTT_NAMES.get(holder.get("key"), "the key")
            if action == "release":
                holder["down"] = False
                holder["up_votes"] = 0
                log(f"recovered a lost {name} key-up (the tap missed it) — releasing.")
                neo.on_release()
            elif action == "maxhold":
                holder["down"] = False
                holder["up_votes"] = 0
                log(f"{name} held past {int(MAX_HOLD_SEC)}s — force-stopping.")
                neo.on_release(long_hold=True)
        except Exception as e:
            print(f"[neo] fn-guard error: {e}")


HERE = os.path.dirname(os.path.abspath(__file__))
SINGLETON_LOCK = os.path.join(HERE, ".neo.lock")
# Set by the app bundle's launcher, so Neo knows something will restart it.
UNDER_AGENT = os.getenv("NEO_MANAGED") == "1"
RELOAD_POLL_S = float(os.getenv("NEO_RELOAD_POLL", "5"))
# How long the source has to sit STILL before a change counts. An edit is
# almost never one file: neo.py, then act.py, then commands.py, seconds apart —
# and the old watcher restarted on each one. Three restarts, three boots, three
# windows where the key did nothing, for what was a single change. 246 of the
# first 329 boots in neo.log were this. Waiting for the writes to stop makes it
# one restart.
RELOAD_SETTLE_S = float(os.getenv("NEO_RELOAD_SETTLE", "3"))
# ...but never wait forever. Something that rewrites a file on a timer would
# otherwise defer the restart indefinitely and Neo would run stale code all day.
RELOAD_SETTLE_MAX_S = float(os.getenv("NEO_RELOAD_SETTLE_MAX", "60"))
# Left behind on the way out so the next process can say how long the gap was.
# A restart and a crash look identical from the outside; this is the difference.
RELOAD_MARKER = os.path.join(HERE, ".neo.reload")
# How long Neo watches the clipboard after opening the key page. Long
# enough for a Google sign-in and a project to be created; short enough
# that a forgotten flow does not poll all day.
KEY_WAIT_S = float(os.getenv("NEO_KEY_WAIT", "300"))
# The binary macOS actually attributes Input Monitoring to — see python_identity.
PYTHON_ID_PATH = os.path.join(HERE, ".neo.python")


def settle_fingerprint(latest, interval, settle, settle_max,
                       fingerprint=None, now=None, sleep=None):
    """Wait until the source stops changing; return the fingerprint it stopped
    at. Clock, sleep and fingerprint are injectable so this is testable without
    burning real seconds.

    settle=0 returns immediately, which is what the tests want and what anyone
    who sets NEO_RELOAD_SETTLE=0 is asking for.
    """
    import time as _t
    fingerprint = fingerprint or source_fingerprint
    now = now or _t.time
    sleep = sleep or _t.sleep
    deadline = now() + settle_max
    while True:
        quiet_until = now() + settle
        moved = False
        while now() < quiet_until:
            sleep(max(0.001, min(interval, quiet_until - now())))
            fp = fingerprint()
            if fp > latest:
                latest = fp          # still being written — restart the clock
                moved = True
                break
        if not moved:
            return latest
        if now() >= deadline:
            return latest            # someone is writing forever; go anyway


LAUNCH_LABEL = "app.neo.assistant"


def supervised(pid=None, label=LAUNCH_LABEL, run=None):
    """Is launchd ACTUALLY going to restart Neo if it exits right now?

    NEO_MANAGED only says the app bundle launched us, and that is not the same
    thing: `open Neo.app` sets it too, and nothing watches that. So the
    difference between a restart and a shutdown was a belief Neo had no way to
    check — and on 9 Sept it was wrong. A Claude job edited agent.py, the
    reload watcher exited cleanly to come back on the new code, and nothing
    brought it back. Neo was simply gone for eighteen minutes until the user
    noticed the silence.

    Ask launchd, and take its answer: the job has to exist AND own this pid.
    """
    import subprocess
    pid = os.getpid() if pid is None else pid
    try:
        if run is None:
            out = subprocess.run(
                ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                capture_output=True, text=True, timeout=5).stdout
        else:
            out = run()
    except Exception:
        return False
    return f"pid = {pid}" in (out or "")


def mark_reload(path=RELOAD_MARKER, now=None):
    """Record that this exit was deliberate, and when."""
    import time as _t
    try:
        with open(path, "w") as f:
            f.write(str(_t.time() if now is None else now))
    except OSError:
        pass


def consume_reload_mark(path=RELOAD_MARKER, now=None):
    """Seconds Neo was gone for a self-restart, or None if this boot wasn't one.
    Removes the marker, so it reports once."""
    import time as _t
    try:
        with open(path) as f:
            left_at = float(f.read().strip())
    except (OSError, ValueError):
        return None
    try:
        os.remove(path)
    except OSError:
        pass
    gap = (_t.time() if now is None else now) - left_at
    return gap if 0 <= gap < 3600 else None


def python_identity(executable=None):
    """The binary macOS attributes Input Monitoring to.

    The app bundle's launcher EXECs .venv/bin/python, and exec replaces the
    process image — so by the time CGEventTapCreate runs, the thing macOS is
    judging is Homebrew's python, not Neo.app. make_app.sh's header warns about
    exactly this and then the launcher does it one level down.

    It works today because that python has been granted. The trap is that
    Homebrew's python is ad-hoc signed (no Team ID), so the grant is pinned to
    this exact binary: `brew upgrade python@3.12` replaces it, the grant no
    longer applies, and fn goes dead with no message in any log. Recording the
    identity means the next boot can at least SAY that is what happened.
    """
    return os.path.realpath(executable or sys.executable)


def python_identity_changed(current, path=PYTHON_ID_PATH):
    """(changed, previous) — and remembers `current` for next time. A first run
    is never 'changed'; there is nothing to compare against yet."""
    prev = None
    try:
        with open(path) as f:
            prev = f.read().strip() or None
    except OSError:
        pass
    if prev != current:
        try:
            with open(path, "w") as f:
                f.write(current)
        except OSError:
            pass
    return (prev is not None and prev != current), prev


def source_fingerprint(directory=None):
    """Newest mtime across Neo's own source. Cheap enough to poll, and it
    changes the instant any module is edited."""
    directory = directory or HERE
    newest = 0.0
    try:
        for name in os.listdir(directory):
            if not name.endswith(".py") or name.startswith("test_"):
                continue
            try:
                newest = max(newest, os.path.getmtime(os.path.join(directory, name)))
            except OSError:
                continue
    except OSError:
        return 0.0
    return newest


DOCUMENT_EXTS = (".md", ".txt", ".pdf", ".docx", ".doc", ".rtf", ".pages",
                 ".html", ".htm", ".csv", ".xlsx", ".pptx", ".key")


def is_document(path):
    """Is this something the user reads, as opposed to something a program does?
    Pure. A file Claude wrote as a DELIVERABLE opens on screen; source it
    edited as WORK gets described instead."""
    return str(path or "").lower().endswith(DOCUMENT_EXTS)


def _reload_watcher(interval=None, on_change=None, busy=None,
                    settle=None, settle_max=None, restartable=None):
    """Exit cleanly when Neo's source changes, so the LaunchAgent brings it back
    running the new code.

    The point is that Neo is supposed to be installed once and then live
    forever — which means "I edited a file, now go restart it" is a bug in the
    product, not an instruction. Only runs when something is managing us; a
    terminal run has nobody to restart it, so it would just die.
    """
    import time as _t
    interval = RELOAD_POLL_S if interval is None else interval
    settle = RELOAD_SETTLE_S if settle is None else settle
    settle_max = RELOAD_SETTLE_MAX_S if settle_max is None else settle_max
    restartable = supervised if restartable is None else restartable
    baseline = source_fingerprint()
    while True:
        _t.sleep(interval)
        try:
            now = source_fingerprint()
        except Exception:
            continue
        if now > baseline:
            # Let the edit finish. Checking BEFORE the busy test on purpose: a
            # multi-file save should collapse into one restart whether or not a
            # conversation happens to be running.
            if settle:
                now = settle_fingerprint(now, interval, settle, settle_max)
            # Never restart out from under a conversation. Editing a file while
            # the user is mid-hold used to kill the process between "holding" and
            # the model's first word — which looks exactly like the freeze this
            # whole rebuild was about. The new code can wait for a gap.
            if busy is not None:
                try:
                    if busy():
                        continue
                except Exception:
                    pass
            # NEVER EXIT INTO NOTHING. Exiting is only a "restart" if
            # something is going to start us again; otherwise it is Neo
            # quietly turning itself off, which is exactly what happened on
            # 9 Sept. Running slightly stale code is a far smaller problem
            # than not running at all.
            if not restartable():
                log("Code changed on disk, but launchd is NOT supervising this "
                    "process — exiting now would leave Neo dead. Staying up on "
                    "the old code. Run ./make_app.sh --start to fix the agent, "
                    "and this restart will happen on its own.")
                baseline = now          # don't say it again for this edit
                continue
            log("Code changed on disk — restarting into the new version.")
            # So the next boot can report the gap instead of leaving a hole in
            # the log that reads like a crash.
            mark_reload()
            if on_change:
                return on_change()
            # os._exit skips every atexit hook and buffer flush, so without this
            # the line above never reaches the file and a deliberate restart
            # looks exactly like a segfault in the log. It cost three
            # misdiagnoses.
            for _s in (sys.stdout, sys.stderr):
                try:
                    _s.flush()
                except Exception:
                    pass
            # Clean exit: KeepAlive treats it the same as a crash and relaunches.
            os._exit(0)
_lock_handle = None      # module-level so the fd lives as long as the process


def claim_singleton(path=SINGLETON_LOCK):
    """True if we're the only Neo running; False if another one already is.

    This matters the moment Neo stops being a thing you start by hand. Once it
    launches at login AND you can still type `python neo.py`, two copies fight
    over one microphone and one event tap — and the symptom is indistinguishable
    from the freezes this whole rebuild was about: presses that do nothing, a
    mic indicator that won't go out, audio that arrives half-captured.

    An flock is the right primitive: the OS drops it when the process dies, so a
    crashed Neo never leaves a stale lock the way a pidfile would.
    """
    global _lock_handle
    try:
        import fcntl
        handle = open(path, "w")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        handle.write(str(os.getpid()))
        handle.flush()
        _lock_handle = handle          # keep it open for the process's lifetime
        return True
    except BlockingIOError:
        return False
    except Exception:
        return True      # can't lock (odd filesystem) — don't block a real start


def mic_check(seconds=4.0):
    """`python neo.py --mic` — which microphone Neo opens, and whether it can
    actually hear you.

    "Which device is selected" is only half the question. A Continuity iPhone
    shows up as a perfectly healthy input and then hands back digital silence,
    which is indistinguishable from a broken app unless you look at the level.
    So this opens the exact device Neo would open and shows the meter moving.
    """
    import sounddevice as sd
    import numpy as _np

    devices = list(sd.query_devices())
    try:
        system_default = sd.query_devices(kind="input").get("name", "")
    except Exception:
        system_default = ""
    override = os.getenv("NEO_MIC")
    chosen = pick_input_device(devices, system_default, override)

    print("\nInputs macOS can see:")
    for d in devices:
        if d.get("max_input_channels", 0) <= 0:
            continue
        name = d.get("name", "?")
        tags = []
        if name == system_default:
            tags.append("system default")
        if any(h in name.lower() for h in _AVOID_HINTS):
            tags.append("drops out — phone/headset")
        if any(h in name.lower() for h in _BUILTIN_HINTS):
            tags.append("built-in")
        print(f"   - {name}" + (f"   [{', '.join(tags)}]" if tags else ""))

    target = chosen or system_default or "(system default)"
    print(f"\nSystem default : {system_default or 'unknown'}")
    print(f"NEO_MIC        : {override or '(not set)'}")
    print(f"NEO WILL USE   : {target}")
    if chosen:
        print("                 ^ deliberately NOT the system default, because")
        print("                   that one hands back silence when it wanders off.")
    if any(h in target.lower() for h in _AVOID_HINTS):
        print("\n!! Neo is about to record a phone or headset. If it's asleep or")
        print("   out of range you get silence. Set the input in System Settings")
        print("   > Sound, or run with NEO_MIC='MacBook Air Microphone'.")

    print(f"\nRecording {seconds:.0f}s from that device — say something.\n")
    frames = []
    try:
        with live_mod.AUDIO_LOCK:      # same rule as everywhere else
            _diag = sd.InputStream(
                samplerate=MIC_RATE, channels=1, dtype="float32",
                device=chosen,
                callback=lambda i, f, t, s: frames.append(i.copy()))
        with _diag:
            import time as _t
            start, bar_at = _t.time(), 0.0
            while _t.time() - start < seconds:
                _t.sleep(0.1)
                if frames and _t.time() - bar_at >= 0.2:
                    bar_at = _t.time()
                    recent = _np.concatenate(frames[-4:]).reshape(-1)
                    rms = float(_np.sqrt(_np.mean(recent ** 2)))
                    filled = min(40, int(rms * 800))
                    print(f"\r   [{'#' * filled}{'.' * (40 - filled)}] "
                          f"{rms:.4f}", end="", flush=True)
    except Exception as e:
        print(f"\n\nCouldn't open that device: {e}")
        print("If this is a permissions error, grant Microphone access to")
        print("whatever is running Neo (~/Applications/Neo.app, or Terminal).")
        return 1

    if not frames:
        print("\n\nNo audio arrived at all. That's a permissions problem.")
        return 1
    audio = _np.concatenate(frames).reshape(-1)
    peak = float(_np.max(_np.abs(audio)))
    rms = float(_np.sqrt(_np.mean(audio ** 2)))
    print(f"\n\n   peak {peak:.4f}   average {rms:.4f}   "
          f"(Neo ignores anything under {MIC_GATE})")
    if rms < MIC_GATE:
        print(f"\n   SILENT. '{target}' is not picking you up.")
        print("   If that's your iPhone, unplug it from Continuity or set the")
        print("   input to the MacBook mic in System Settings > Sound.")
        return 1
    print(f"\n   Good — '{target}' hears you. Neo will use this.\n")
    return 0


def doctor():
    """`python neo.py --doctor` — say exactly what is stopping fn from working.

    "It doesn't work" with a silent log is unfixable, and every failure here is
    invisible by design: macOS denies permissions without telling the app, and a
    tap that was never created looks identical to a key nobody pressed.
    """
    ok = True
    print("\nNeo doctor\n" + "-" * 52)

    running = not claim_singleton()
    print(f"{'RUNNING ':<22} {'yes — another Neo has the lock' if running else 'no'}")

    tap = Quartz.CGEventTapCreate(
        Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
        Quartz.kCGEventTapOptionListenOnly,
        Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged), lambda *a: a[2], None)
    if tap is None:
        ok = False
        print(f"{'KEY LISTENER':<22} BLOCKED — grant Input Monitoring AND")
        print(f"{'':<22} Accessibility to whatever is running Neo")
        print(f"{'':<22} (~/Applications/Neo.app, or Terminal)")
    else:
        Quartz.CGEventTapEnable(tap, False)
        print(f"{'KEY LISTENER':<22} ok")

    # Which binary the Input Monitoring grant is really attached to. The bundle
    # execs python, so this is usually NOT Neo.app, and knowing that is the
    # difference between granting the right thing and re-ticking the wrong box.
    _py = python_identity()
    print(f"{'GRANT IS PINNED TO':<22} {_py}")
    if "/Cellar/" in _py or "/homebrew/" in _py:
        print(f"{'':<22} (Homebrew python, ad-hoc signed — a `brew upgrade`")
        print(f"{'':<22}  replaces it and silently voids the grant)")

    state = ptt_physically_down(FN_KEYCODE)
    print(f"{'FN KEY READABLE':<22} "
          + ("ok (hold fn and re-run to see True)" if state is not None
             else "Quartz can't read it — the guard will never force a release"))
    print(f"{'WATCHING':<22} " + ", ".join(PTT_NAMES.get(k, str(k)) for k in PTT_KEYS))

    try:
        import sounddevice as _sd
        devs = list(_sd.query_devices())
        ins = [d["name"] for d in devs if d.get("max_input_channels", 0) > 0]
        try:
            sys_default = _sd.query_devices(kind="input").get("name", "")
        except Exception:
            sys_default = ""
        if not ins:
            ok = False
            print(f"{'MICROPHONE':<22} NO INPUTS — grant Microphone access")
        else:
            chosen = pick_input_device(devs, sys_default, os.getenv("NEO_MIC"))
            print(f"{'SYSTEM INPUT':<22} {sys_default or 'unknown'}")
            # Output is the half that was silently wrong for months: PortAudio
            # froze its list at login, so headphones connected later never got
            # the voice. These two lines disagreeing IS the bug.
            try:
                import audio_out as _ao
                print(f"{'SPEAKER (real)':<22} {_ao.describe()}")
                print(f"{'SPEAKER (portaudio)':<22} "
                      f"{_ao.portaudio_default_output(_sd) or 'unknown'}")
            except Exception as _e:
                print(f"{'SPEAKER':<22} couldn't read it ({_e})")
            if chosen:
                print(f"{'MICROPHONE':<22} ok — Neo will use '{chosen}'")
                print(f"{'':<22} (skipping the system input: phones and headsets")
                print(f"{'':<22}  hand back silence. NEO_MIC=default overrides.)")
            elif any(h in (sys_default or "").lower() for h in _AVOID_HINTS):
                ok = False
                print(f"{'MICROPHONE':<22} the system input is a phone/headset and")
                print(f"{'':<22} there's no built-in mic to fall back to.")
                print(f"{'':<22} Switch the input in System Settings > Sound.")
            else:
                print(f"{'MICROPHONE':<22} ok — using the system input")
    except Exception as e:
        ok = False
        print(f"{'MICROPHONE':<22} FAILED ({e})")

    try:
        import providers as _p
        print(f"{'MODELS':<22} {_p.describe()}")
        print(f"{'PROVIDERS':<22} {', '.join(sorted(_p.keys_present())) or 'none'}")
    except Exception as e:
        print(f"{'MODELS':<22} could not read ({e})")

    # The one that catches people out: macOS can eat a bare fn press before any
    # app sees it, depending on what the globe key is bound to.
    try:
        import subprocess
        v = subprocess.run(["defaults", "read", "-g", "AppleFnUsageType"],
                           capture_output=True, text=True, timeout=5).stdout.strip()
        meaning = {"0": "Do Nothing", "1": "Change Input Source",
                   "2": "Show Emoji & Symbols", "3": "Start Dictation"}.get(v, v or "unset")
        good = v in ("0", "") or meaning == "unset"
        print(f"{'GLOBE KEY BOUND TO':<22} {meaning}"
              + ("" if good else "   <-- MAKE THIS 'Do Nothing'"))
        if not good:
            ok = False
            print(f"{'':<22} System Settings > Keyboard > Press globe key to")
            print(f"{'':<22} macOS eats the tap before Neo ever sees it.")
    except Exception:
        pass

    print("-" * 52)
    print("All clear — tap fn and talk.\n" if ok else
          "Fix the lines above, then just tap fn. Nothing needs restarting.\n")
    return 0 if ok else 1


def main():
    if "--doctor" in sys.argv:
        sys.exit(doctor())
    if "--mic" in sys.argv:
        sys.exit(mic_check())

    # One Neo at a time. See claim_singleton: two copies sharing a mic looks
    # exactly like the freeze bug, and "it's already running" is the one message
    # that saves an hour of debugging the wrong thing.
    if not claim_singleton():
        print("[neo] Neo is already running (started at login, or in another "
              "terminal).\n"
              "      Talk to that one — there's no need to start it yourself.")
        if UNDER_AGENT:
            # KeepAlive relaunches whatever we do, so idle a while first rather
            # than spinning up a new process every few seconds forever.
            time.sleep(60)
        sys.exit(0)

    # Cocoa app (needed for the overlay + run loop), but no Dock icon / menu.
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(1)  # NSApplicationActivationPolicyAccessory (no Dock icon, windows still show)

    # Make Ctrl-C (and `kill`) quit immediately and cleanly.
    def _bye(*_a):
        os._exit(0)
    signal.signal(signal.SIGINT, _bye)
    signal.signal(signal.SIGTERM, _bye)

    state = State()
    overlay = None
    if os.getenv("NEO_NO_OVERLAY") != "1":
        try:
            overlay = Overlay(state)   # must be built on the main thread
        except Exception as e:
            print(f"[neo] overlay disabled ({e}); voice still works.")
    neo = Neo(state)

    # Notification card (the sentinel's voice-on-screen). Optional, like the overlay.
    if os.getenv("NEO_NO_OVERLAY") != "1" and os.getenv("NEO_SENTINEL") != "0":
        try:
            from notify import Notifier
            neo.notifier = Notifier(neo.on_card_action)   # main thread, like Overlay
            import connectors as _connectors
            _connectors.on_need = neo._needs_hook
        except Exception as e:
            print(f"[neo] notification card disabled ({e}); sentinel will just log.")

    # Status HUD (top-right glass panel: what Neo's doing + waiting-on-you).
    # Optional + crash-safe like the overlay; NEO_NO_HUD=1 disables.
    if os.getenv("NEO_NO_HUD") != "1" and os.getenv("NEO_NO_OVERLAY") != "1":
        try:
            hud.HUD()          # main thread, like Overlay
            neo.hud_on = True
        except Exception as e:
            print(f"[neo] status HUD disabled ({e}); everything else works.")

    # Answer panel (bottom-right: the SHAPE of what Neo is saying). Same
    # main-thread rule and the same crash-safety as the HUD — a window that
    # won't build must never be the reason the voice doesn't work.
    if os.getenv("NEO_NO_PANEL") != "1" and os.getenv("NEO_NO_OVERLAY") != "1":
        try:
            import panel as panel_mod
            panel_mod.Panel()
            neo.panel_on = True
        except Exception as e:
            print(f"[neo] answer panel disabled ({e}); the voice is unaffected.")

    # The stealth chat box (top-right). Same rules as the panel.
    if os.getenv("NEO_NO_OVERLAY") != "1":
        try:
            import stealth as _stealth_mod
            _stealth_mod.Chat()
        except Exception as e:
            print(f"[neo] stealth box disabled ({e}); the voice is unaffected.")

    # Was this boot a self-restart, and how long was Neo gone? Consumed here
    # (before anything can crash) and reported once the models are up, so the
    # log shows the real size of the gap instead of an unexplained hole.
    neo._reload_gap = consume_reload_mark()

    # If the interpreter changed underneath us, the Input Monitoring grant is
    # keyed to a binary that no longer exists. Say so BEFORE the tap is built,
    # because if it fails this is almost always why.
    _py = python_identity()
    _py_changed, _py_prev = python_identity_changed(_py)
    if _py_changed:
        log("The Python running Neo changed since the last boot:")
        log(f"  was: {_py_prev}")
        log(f"  now: {_py}")
        log("  If fn stops working, that's why — macOS pins Input Monitoring to "
            "the exact binary. Re-grant it in Privacy & Security.")

    # FIRST RUN. The guided setup — permissions, the key, first words, the
    # tour — narrated by Neo. Once, then a marker, never again unless asked
    # (`python onboard.py`). Main thread, because it is a window.
    neo.onboarding = None
    if os.getenv("NEO_NO_OVERLAY") != "1" and os.getenv("NEO_SKIP_ONBOARD") != "1":
        try:
            import onboard
            if not onboard.onboarded():
                neo.onboarding = onboard.Onboarding(
                    say=neo.say, log=log, app=neo,
                    on_done=lambda: setattr(neo, "onboarding", None))
                log("[onboard] first run — walking them through setup")
        except Exception as e:
            print(f"[neo] onboarding didn't open ({e}); Neo works regardless.")

    # Load heavy models in the background so the UI comes up immediately.
    threading.Thread(target=neo.boot, daemon=True).start()

    make_event_tap(neo)

    # Heartbeat so the Python interpreter regularly regains the main thread,
    # which lets the Ctrl-C handler above actually fire (max ~0.25s delay).
    heartbeat = _Heartbeat.alloc().init()
    _TAP_REFS["heartbeat"] = heartbeat
    _TAP_REFS["hb_timer"] = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        0.25, heartbeat, "beat:", None, True
    )

    # The fn-guard: four times a second, reconcile our belief about the key with
    # the OS's real state so a key-up the tap dropped can never wedge us on
    # 'listening' — while requiring several readings in a row to agree, so a
    # single unreliable sample can't cut a sentence in half.
    fn_guard = _FnGuard.alloc().init()
    _TAP_REFS["fn_guard"] = fn_guard
    _TAP_REFS["fn_guard_timer"] = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        FN_GUARD_INTERVAL, fn_guard, "check:", None, True
    )

    print(f"[neo] Listening for {' and '.join(PTT_NAMES.get(k, str(k)) for k in PTT_KEYS)}"
          f" — {MODE} mode. (Ctrl-C to quit.)")
    AppHelper.runEventLoop()


if __name__ == "__main__":
    main()
