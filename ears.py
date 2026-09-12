"""
ears.py — turning held audio into words.

The old path was faster-whisper base.en on CPU and nothing else. base.en is the
second-smallest English model there is, and the log shows what that costs:
"My name is Ariane Six" for "My name is the user", and a full sentence of
invented dialogue out of a noisy room. Every one of those reached Gemini as if
it were what the user said, and Gemini answered it correctly — which is what
"Neo is stupid" actually looked like from the outside.

So transcription now goes to a real model when the network is there:

    Groq whisper-large-v3-turbo   (best free ears; needs GROQ_API_KEY)
    Gemini audio input            (no second account; uses the key already here)
    faster-whisper, on-device     (offline floor, always available)

Two things make the cloud paths safe to put in the hot path. Every call is
bounded by a hard timeout, because the entire family of freezes in this app came
from something blocking a thread nobody was watching. And every failure falls
through to the next path silently, so the worst case is exactly the old
behaviour rather than a dead turn.

The other win is free: a cloud model accepts a hint list. Feeding it the proper
nouns the user actually says — their name, their products, the people they talk about —
is why "Ariane Six" stops happening. Local Whisper has no equivalent.
"""

import io
import os
import re
import wave

CLOUD_TIMEOUT_S = float(os.getenv("NEO_STT_TIMEOUT", "8"))

# Words no acoustic model gets right without help, because they aren't English.
# Anything here is worth its weight: a wrong proper noun poisons the whole turn.
# Words every install shares. Everything personal comes from memory and the
# profile (names of people, places, projects) — never from here.
BASE_HINTS = (
    "Neo", "Gemini", "Claude", "Kokoro", "Safari", "Chrome", "Spotify",
)

# Whisper's party trick when handed silence or room tone: it emits the caption
# boilerplate from its training data. Never let these reach the brain.
_HALLUCINATIONS = (
    "thanks for watching", "thank you for watching", "subscribe",
    "please subscribe", "see you next time", "you're watching",
    "transcription by", "subtitles by", "amara.org", "bye bye",
)


# --------------------------------------------------------------------------- #
# Pure helpers — no network, no models. Tested in test_neo.py.
# --------------------------------------------------------------------------- #
def to_wav_bytes(audio_f32, rate=16000):
    """float32 mono in [-1, 1] -> a 16-bit PCM WAV in memory. Both cloud paths
    want a real container rather than raw samples, and every audio library on
    earth reads WAV, so this is the cheapest common denominator."""
    import numpy as np
    a = np.asarray(audio_f32, dtype="float32").reshape(-1)
    a = np.clip(a, -1.0, 1.0)
    pcm = (a * 32767.0).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(rate))
        w.writeframes(pcm)
    return buf.getvalue()


def clean_transcript(text):
    """Trim a raw transcript to something worth acting on, or "" if it isn't.

    Two jobs: strip the bracketed stage directions every ASR model emits
    ("[BLANK_AUDIO]", "(music playing)"), and drop the caption boilerplate
    Whisper invents when it hears nothing. Returning "" is a real answer here —
    it's how a turn gets discarded instead of asked."""
    if not text:
        return ""
    t = re.sub(r"[\[\(<][^\]\)>]{0,40}[\]\)>]", " ", text)
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return ""
    bare = t.lower().strip(" .,!?-")
    if bare in _HALLUCINATIONS or any(bare == h for h in _HALLUCINATIONS):
        return ""
    # A "sentence" with no letters in it is punctuation noise, not speech.
    if not re.search(r"[a-z]", t, re.I):
        return ""
    return t


def build_hints(facts=(), extra=()):
    """Proper nouns to bias the recognizer toward, newest first, deduped and
    capped. Drawn from Neo's memory so the list grows as the user mentions new
    people and products, without anyone maintaining it."""
    seen, out = set(), []
    for word in list(extra) + list(BASE_HINTS):
        if word and word.lower() not in seen:
            seen.add(word.lower())
            out.append(word)
    # NEWEST FIRST, actually. The docstring always said so; the loop walked
    # the facts oldest-first and the 60-name cap filled up before it reached
    # anything recent. So the names the user mentioned THIS WEEK were the ones
    # the recogniser was never told about — "Sam" came out as "Roni" on a
    # highlight label because the fact naming them was number 65 of 64 slots.
    for fact in reversed(list(facts)):
        text = fact.get("text", "") if isinstance(fact, dict) else str(fact)
        for word in re.findall(r"\b[A-Z][a-zA-Z]{2,}\b", text):
            if word.lower() not in seen and len(out) < 60:
                seen.add(word.lower())
                out.append(word)
    return out[:60]


def transcript_is_worth_it(text, min_words=1):
    """Guard against acting on a fragment. One clear word ("stop", "yes") is a
    real command, so the floor is deliberately low — this only catches empty."""
    return bool(text) and len(text.split()) >= min_words


# --------------------------------------------------------------------------- #
# Cloud paths. Each raises on any problem; the caller falls through.
# --------------------------------------------------------------------------- #
def _groq_transcribe(wav_bytes, hints=()):
    import requests
    prompt = ", ".join(hints[:40])
    r = requests.post(
        "https://api.groq.com/openai/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {os.getenv('GROQ_API_KEY')}"},
        files={"file": ("speech.wav", wav_bytes, "audio/wav")},
        data={"model": "whisper-large-v3-turbo", "response_format": "text",
              "language": "en", "temperature": "0",
              **({"prompt": prompt} if prompt else {})},
        timeout=CLOUD_TIMEOUT_S)
    r.raise_for_status()
    return r.text.strip()


def _gemini_transcribe(client, model, wav_bytes, hints=()):
    from google.genai import types
    hint_line = ""
    if hints:
        hint_line = ("\nNames and terms that are likely to appear, spelled "
                     "correctly: " + ", ".join(hints[:40]) + ".")
    instruction = (
        "Transcribe this audio verbatim. Return ONLY the words spoken, with "
        "normal punctuation and capitalisation. Do not translate, summarise, "
        "answer, or add commentary. If there is no intelligible speech, return "
        "exactly the word NOSPEECH." + hint_line)
    cfg = types.GenerateContentConfig(temperature=0.0)
    # Transcription needs no reasoning, but "no reasoning" is spelled
    # differently per model family and the wrong spelling is a 400 on every
    # call — which would silently drop every turn back to the small local
    # model. providers knows which dialect this model speaks.
    try:
        import providers
        kwargs = providers.thinking_kwargs(providers.style_of(model), 0)
        if kwargs:
            cfg.thinking_config = types.ThinkingConfig(**kwargs)
    except Exception:
        pass
    r = client.models.generate_content(
        model=model,
        contents=[types.Part.from_bytes(data=wav_bytes, mime_type="audio/wav"),
                  instruction],
        config=cfg)
    text = (getattr(r, "text", "") or "").strip()
    if text.upper().startswith("NOSPEECH"):
        return ""
    return text


# --------------------------------------------------------------------------- #
# The one entry point Neo calls.
# --------------------------------------------------------------------------- #
class Ears:
    """Transcription with a cloud-first, local-always fallback chain.

    `local` is a callable taking float32 audio and returning text — Neo passes
    its faster-whisper wrapper. It is never removed from the chain, so pulling
    the network cable degrades Neo instead of breaking it.
    """

    def __init__(self, client=None, local=None, log=print, hints=()):
        self.client = client
        self.local = local
        self.log = log
        self.hints = list(hints)
        self._cloud_off_until = 0.0      # circuit breaker after repeated failure
        self._fails = 0
        self.last_path = None            # "groq" / "gemini" / "local", for metrics

    def set_hints(self, hints):
        self.hints = list(hints)

    def _cloud_ready(self):
        import time
        if os.getenv("NEO_CLOUD_STT") == "0":
            return False
        return time.time() >= self._cloud_off_until

    def _trip(self):
        """Three failures in a row and the cloud is clearly unreachable — stop
        paying the timeout on every turn for the next few minutes."""
        import time
        self._fails += 1
        if self._fails >= 3:
            self._cloud_off_until = time.time() + 300
            self._fails = 0
            self.log("[ears] cloud transcription unreachable — local for 5 min.")

    def transcribe(self, audio_f32, rate=16000):
        """Words for this utterance, or "" when there was nothing to hear."""
        import providers

        wav = None
        if self._cloud_ready():
            try:
                wav = to_wav_bytes(audio_f32, rate)
            except Exception as e:
                self.log(f"[ears] could not encode audio ({e}); using local.")

        if wav is not None:
            provider, model = providers.resolve("stt", self.client, self.log)
            if provider:
                try:
                    if provider == "groq":
                        raw = _groq_transcribe(wav, self.hints)
                    else:
                        raw = _gemini_transcribe(self.client, model, wav, self.hints)
                    self._fails = 0
                    self.last_path = provider
                    return clean_transcript(raw)
                except Exception as e:
                    self.log(f"[ears] {provider} transcription failed ({e}); "
                             "falling back to on-device.")
                    providers.report_failure("stt", provider, model, self.log)
                    self._trip()

        if self.local is None:
            return ""
        self.last_path = "local"
        try:
            return clean_transcript(self.local(audio_f32))
        except Exception as e:
            self.log(f"[ears] on-device transcription failed ({e}).")
            return ""
