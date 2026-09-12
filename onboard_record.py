"""
onboard_record.py — record the onboarding narration in Neo's own voice.

    .venv/bin/python onboard_record.py            # only lines that changed
    .venv/bin/python onboard_record.py --all      # every line

Each line in onboard.NARRATION becomes onboard_audio/<scene>.wav (24 kHz,
16-bit — what afplay wants) spoken by the same Gemini voice the live session
uses, so the first voice a person hears IS Neo. The text that produced each file is kept beside
it in <scene>.txt; a line whose text hasn't changed is not re-recorded, which
keeps this cheap enough to run after every copy edit.
"""
import os
import sys
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv()

import onboard
import providers
from google import genai
from google.genai import types

MODEL = os.getenv("NEO_GEMINI_TTS", "gemini-2.5-flash-preview-tts")
# Derived, never hardcoded: the narration has to be the same person as the live
# session. This file used to default to "Charon" independently of live.py, so
# the two could drift apart without anything noticing.
import live as _live
VOICE = os.getenv("NEO_GEMINI_VOICE") or os.getenv("NEO_LIVE_VOICE") or _live.VOICE


# A style instruction the model follows but doesn't speak. Without one it
# sometimes stops after the first sentence — "A few things to try." and
# nothing else came back, twice, before this was added.
# This instruction is the single biggest thing about how the narration FEELS,
# and the first version got it wrong: "calmly and unhurried" produced a voice
# that drags, which on a setup screen reads as slow and strange rather than
# calm. Warm and normally-paced is what a person helping you set something up
# actually sounds like. The style is followed, never spoken.
STYLE = ("Say this warmly and naturally, at a normal conversational pace, "
         "like a friend helping you set something up. Do not drag it out: ")


def record(text, keys):
    last = None
    for key in keys:
        try:
            client = genai.Client(api_key=key)
            cfg = types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE))))
            r = client.models.generate_content(model=MODEL, contents=STYLE + text, config=cfg)
            data = r.candidates[0].content.parts[0].inline_data.data
            a = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            if a.size:
                return a
            last = "empty audio"
        except Exception as e:
            last = e
    raise RuntimeError(f"no key could record it: {last}")


def save_wav(path, a, rate=24000):
    pcm = (np.clip(a, -1, 1) * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def main():
    keys = providers.gemini_keys()
    if not keys:
        sys.exit("no GEMINI_API_KEY in .env")
    os.makedirs(onboard.AUDIO_DIR, exist_ok=True)
    everything = "--all" in sys.argv
    for scene, text in onboard.NARRATION.items():
        wav = os.path.join(onboard.AUDIO_DIR, f"{scene}.wav")
        txt = os.path.join(onboard.AUDIO_DIR, f"{scene}.txt")
        # The sidecar records the VOICE as well as the text, because a voice
        # change has to re-record just as surely as a copy change does. Without
        # this, switching the voice left every unchanged line in the old one —
        # which is how the onboarding ended up half Sulafat and half Charon,
        # two different people inside one two-minute flow.
        want = f"{VOICE}\n{text}"
        have = open(txt).read() if os.path.exists(txt) else None
        same = os.path.exists(wav) and have == want
        if same and not everything:
            print(f"  {scene:<8} unchanged ({VOICE})")
            continue
        a = record(text, keys)
        save_wav(wav, a)
        open(txt, "w").write(want)
        print(f"  {scene:<8} {a.size / 24000:.1f}s  peak {float(np.abs(a).max()):.2f}")


if __name__ == "__main__":
    main()
