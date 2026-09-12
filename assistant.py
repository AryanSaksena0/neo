"""
Neo — a simple conversational assistant powered by Gemini (free tier).
Run:  python assistant.py
Type 'quit' to exit.
"""

import os
from dotenv import load_dotenv
from google import genai
from google.genai import types

# Load the API key from .env
load_dotenv()
API_KEY = os.getenv("GEMINI_API_KEY")

if not API_KEY or API_KEY == "paste_your_free_key_here":
    print("⚠️  No API key found. Open the .env file and paste your free Gemini key.")
    raise SystemExit

client = genai.Client(api_key=API_KEY)

# This is the assistant's personality / instructions. Edit this to make it yours.
SYSTEM_PROMPT = (
    "You are Neo, a sharp, friendly personal assistant. "
    "Keep replies short and conversational. Be direct and helpful."
)

# Free-tier conversational model with solid limits (10 req/min, 250/day).
MODEL = "gemini-2.5-flash"


def main():
    print("Neo is online. Type 'quit' to exit.\n")

    # Keeps the conversation memory for this session.
    chat = client.chats.create(
        model=MODEL,
        config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
    )

    while True:
        user = input("You: ").strip()
        if user.lower() in ("quit", "exit"):
            print("Neo: Later.")
            break
        if not user:
            continue
        try:
            response = chat.send_message(user)
            print(f"Neo: {response.text}\n")
        except Exception as e:
            print(f"Neo: (error) {e}\n")


if __name__ == "__main__":
    main()
