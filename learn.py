"""
learn.py — Neo learns from every conversation, without being told to.

memory.json only grew when the model chose to end a reply with a
[[remember: ...]] tag. On the LIVE path that tag is stripped from the prompt
(it would be read aloud), so a whole day of talking taught Neo nothing. That
is the "second brain isn't updating" bug.

Two learners, both free of user effort:

  preference_from()   Instant, no model. "That was too long" -> length: short.
                      Runs on every user turn as it arrives.
  from_session()      One fast-model call when a live session CLOSES, over
                      that session's turns. Pulls out durable facts (what they
                      do, who they mention, what they're working on, how they
                      want things), never chit-chat. Facts go to memory.json,
                      preferences to profile.json.

One call per session, not per turn, so it costs nothing worth counting.
"""

import json
import re

import memory
import person as profile

_PROMPT = """You keep notes for a personal assistant about the person it works for.
Below is one conversation (You = the person, Neo = the assistant).

Return ONLY JSON:
{"facts": ["...", ...],
 "preferences": {"length": "short|long" or omit, "mode": "text|voice" or omit, "humour": "none" or omit},
 "people": [{"name": "...", "relation_or_role": "..."}],
 "working_on": ["..."]}

facts: 0-5 DURABLE things worth knowing next week — routines, commitments,
projects, people, tools they use, how they like things done, corrections they
made. Each one a short third-person sentence starting with their name or
"They". NOT what the weather was, NOT the answer to a trivia question, NOT
anything Neo said. Empty list if nothing durable was said.
preferences: only if they SAID something about how they want answers.
people: anyone they named, with the role if clear.
working_on: named projects, documents or tasks they are actively on.
No prose, no code fence."""


def _turn_text(turns, since=0, limit=40):
    lines = []
    for t in list(turns)[since:][-limit:]:
        if isinstance(t, dict):
            who = "You" if t.get("role") == "user" else "Neo"
            text = t.get("text") or " ".join(
                p.get("text", "") for p in t.get("parts", []) if isinstance(p, dict))
        else:
            who, text = "?", str(t)
        text = (text or "").strip()
        if text:
            lines.append(f"{who}: {text[:600]}")
    return "\n".join(lines)


def parse(raw):
    """Model text -> dict, tolerant of fences. Pure."""
    try:
        import deck
        d = deck._loads(raw)
    except Exception:
        d = None
    if not isinstance(d, dict):
        m = re.search(r"\{.*\}", str(raw or ""), re.S)
        try:
            d = json.loads(m.group(0)) if m else {}
        except ValueError:
            d = {}
    facts = [str(f).strip() for f in d.get("facts", []) if str(f).strip()]
    facts = [f for f in facts if 12 <= len(f) <= 220 and not f.lower().startswith("neo ")]
    prefs = {k: v for k, v in (d.get("preferences") or {}).items()
             if k in ("length", "mode", "humour", "shape") and isinstance(v, str)}
    people = [p for p in d.get("people", []) if isinstance(p, dict) and p.get("name")]
    work = [str(w).strip() for w in d.get("working_on", []) if str(w).strip()][:5]
    return {"facts": facts[:5], "preferences": prefs, "people": people[:6], "working_on": work}


def from_session(turns, ask, since=0, log=print, name=""):
    """Learn from one session. `ask(prompt) -> text` is the fast model.
    Returns what was learned, or None if nothing/failed."""
    text = _turn_text(turns, since=since)
    if len(text) < 40 or "You:" not in text:
        return None
    try:
        raw = ask(_PROMPT.replace("their name", name or "their name") + "\n\n" + text)
    except Exception as e:
        log(f"[learn] model failed ({type(e).__name__})")
        return None
    got = parse(raw)
    added = 0
    if got["facts"]:
        mem = memory.load_memory()
        for f in got["facts"]:
            added += bool(memory.add_fact(mem, f))
        if added:
            memory.save_memory(mem)
    p = profile.load()
    changed = False
    if got["preferences"]:
        profile.note_preference(p, got["preferences"], source="session")
        changed = True
    if got["people"]:
        ppl = p.setdefault("people", {})
        for person in got["people"]:
            entry = ppl.setdefault(person["name"], {})
            if person.get("relation_or_role") and not entry.get("relation"):
                entry["relation"] = person["relation_or_role"]
                changed = True
    if got["working_on"]:
        wk = p.setdefault("work", {})
        cur = [w for w in wk.get("current", []) if w not in got["working_on"]]
        wk["current"] = (got["working_on"] + cur)[:8]
        changed = True
    if changed:
        profile.save(p)
    log(f"[learn] session: {added} new fact(s)"
        + (f", prefs {got['preferences']}" if got["preferences"] else "")
        + (f", working on {got['working_on']}" if got["working_on"] else ""))
    return got
