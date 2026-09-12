"""
voice.py — their writing voice, learned from their real LinkedIn posts (and more
samples over time). Injected into everything Neo writes (posts, carousels,
outreach) so the output sounds like them, not generic AI.

Add more samples anytime: voice.add_samples([...]) — e.g. from sent emails.
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "voice.json")


def load():
    try:
        with open(PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"profile": "", "samples": []}


def style_block(max_samples=6):
    """A prompt fragment to prepend so Neo writes in their voice."""
    v = load()
    if not v.get("profile") and not v.get("samples"):
        return ""
    out = "Write in their own voice. Match it closely.\n" + v.get("profile", "")
    samples = v.get("samples", [])[:max_samples]
    if samples:
        out += "\n\nReal examples of how the user writes:\n" + "\n---\n".join(samples)
    return out + "\n\n"


def add_samples(new):
    v = load()
    v.setdefault("samples", [])
    for x in new:
        x = (x or "").strip()
        if x and x not in v["samples"]:
            v["samples"].append(x)
    try:
        with open(PATH, "w") as f:
            json.dump(v, f, indent=2)
    except OSError:
        pass
    return len(v["samples"])
