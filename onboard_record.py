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
VOICE = os.getenv("NEO_GEMINI_VOICE", "Charon")


# A style instruction the model follows but doesn't speak. Without one it
# sometimes stops after the first sentence — "A few things to try." and
# nothing else came back, twice, before this was added.
STYLE = "Say this calmly and unhurried: "


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
        same = os.path.exists(wav) and os.path.exists(txt) and open(txt).read() == text
        if same and not everything:
            print(f"  {scene:<8} unchanged")
            continue
        a = record(text, keys)
        save_wav(wav, a)
        open(txt, "w").write(text)
        print(f"  {scene:<8} {a.size / 24000:.1f}s  peak {float(np.abs(a).max()):.2f}")


if __name__ == "__main__":
    main()
