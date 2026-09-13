"""
onboard.py — the first five minutes with Neo.

A full-screen, first-run experience that Neo narrates in its own voice. Five
scenes: welcome, permissions, the key, first words, what Neo does. It runs once,
leaves a marker, and never appears again unless asked (`python onboard.py`).

    python onboard.py --demo      see it without touching Neo

WHY IT EXISTS
NEO.md has said "setup is the worst part of the product" since the beginning.
Four permission prompts, a terminal, a .env file, a key from a Google page — for
the user that was an afternoon; for anyone else it is the reason they never hear
Neo speak. This replaces all of it with Neo walking the person through it,
checking each step for real as it happens, and saying so out loud.

WHAT PREMIUM MEANS HERE
Restraint. One accent, the orb, the system typeface set large and light, and
each scene doing one thing. Nothing animates that isn't telling the person
something — a permission row moves when macOS actually grants it, not on a
timer. The whole thing should feel like the Mac's own setup assistant, if the
Mac could talk.

THE ONE RULE
Every check is real. The permissions list asks macOS. The key is validated by
its shape and saved only when it looks right. "First words" advances when Neo
actually hears a sentence. A setup flow that says "done" before it is done
teaches the person to distrust everything Neo says afterwards.
"""

import json
import os
import re
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MARKER = os.path.join(HERE, ".neo.onboarded")
ENV_PATH = os.path.join(HERE, ".env")
KEY_URL = "https://aistudio.google.com/apikey"
AUDIO_DIR = os.path.join(HERE, "onboard_audio")

# Gemini keys: the classic "AIza..." shape, and the newer "AQ." prefix their
# own keys carry. Shape only — the real test is the first request.
KEY_RX = re.compile(r"\b(AIza[0-9A-Za-z_\-]{30,}|AQ\.[0-9A-Za-z_\-]{40,})\b")

# The tour. Every card is something that WORKS TODAY, with the exact words
# that trigger it — nothing aspirational, and no presentations (their call:
# the one part of Neo they do not consider built in). No names, no personal
# history; these are for whoever installs it next. Ordered by what makes a
# person sit up: the screen first, the doing second, the knowing last.
TOUR = [
    ("It sees what you see",
     "“What does this error mean?”",
     "Every question carries what's on your screen. “This” and “that” are real words to it."),
    ("It points. It doesn't explain.",
     "“Where do I turn off read receipts?”",
     "A ring lands on the actual control and follows you through the whole flow. "
     "“Highlight the deadline” puts a highlighter on the exact words."),
    ("It does it. It doesn't describe it.",
     "“Open Safari, go to the form, put my email in.”",
     "Opens apps, clicks by name, finds a field by its label and types. Plays, "
     "pauses and skips your music."),
    ("Your day, written and read back",
     "“Put lunch on Thursday at one.” · “Remind me to call mum at six.”",
     "Calendar and Reminders, confirmed only after Neo reads the entry back. "
     "Missing a time? It asks once. Never otherwise."),
    ("Your mail, documents, clipboard",
     "“Did anyone email about the invoice?” · “Summarise this PDF.”",
     "Reads the inbox, writes replies into Drafts, never sends. Finds files by "
     "what's inside them, scans included."),
    ("Answers with shape",
     "“Lease or buy?” · “How does compounding work?”",
     "A panel beside the answer: comparison, process, timeline, breakdown. "
     "Gone when you move on."),
    ("Live numbers, no subscription",
     "“What's Apple at?” · “What's the score?” · “Odds on a rate cut?”",
     "Markets, crypto, live sport, prediction markets, weather. Real and current."),
    ("It learns you. And itself.",
     "“Remember I take the 8:15.” · “Teach yourself to track my reading.”",
     "Kept across restarts. Writes and tests its own new abilities. Watches its "
     "own log and repairs what it can."),
    ("Your voice, your volume",
     "“Whisper.” · “Bedtime.” · “Hush.”",
     "Whisper and bedtime turn everything down and listen for a quieter you. "
     "Hush is instant silence — nothing said, nothing else."),
]


# Short, and in Neo's register: certain, unhurried, no filler. These are
# recorded once in Neo's own voice (onboard_audio/) — during setup there is no
# key yet, so the live voice isn't available, and a stand-in voice would be
# the first thing anyone heard.
NARRATION = {
    # Short. Every one of these is heard while the person is reading the screen,
    # so anything the screen already says is noise. The old set narrated the
    # interface back at them and opened with "Give me three minutes", which is
    # a toll, not a welcome.
    "welcome": "Hey. I'm Neo. Let me walk you through setting me up, it takes about two minutes.",
    "perms":   "First, macOS has to let me in. Tap each one and say yes.",
    "key":     "One free key and I can think. Press the button, then just copy it. I'll do the rest.",
    "connect": "Now the things I work with. Press connect, then approve.",
    "about":   "A few things about you, so I'm yours from the start.",
    "first":   "Go on then. Hold the fn key, say anything, and let go.",
    "tour":    "A few things worth trying.",
    "done":    "That's it. Hold the key whenever you need me.",
}


def _mac_first_name():
    """The Mac's own idea of who is logged in, as a first name to prefill."""
    try:
        import subprocess
        full = subprocess.run(["id", "-F"], capture_output=True, text=True, timeout=3).stdout.strip()
        return full.split()[0] if full else ""
    except Exception:
        return ""


def save_about(m, log=print):
    """What they told us at setup, into the profile (person.json) and, for
    the free-text notes, into memory as facts. This is where a fresh install
    stops being generic: the name goes into every prompt from here on."""
    try:
        import person
        p = person.load()
        who = p.setdefault("person", {})
        name = (m.get("name") or "").strip()
        if name:
            who["first_name"] = name
            if not who.get("name"):
                who["name"] = name
        role = (m.get("role") or "").strip()
        if role:
            who["role"] = role
        length = (m.get("length") or "").strip()
        if length in ("short", "medium", "long"):
            person.note_preference(p, {"length": length}, source="onboarding")
        p.setdefault("updated", {})["person"] = person._now().isoformat(timespec="seconds")
        person.save(p)
    except Exception as e:
        log(f"[onboard] couldn't save the profile: {e}")
    try:
        import memory
        mem = memory.load_memory()
        added = False
        role = (m.get("role") or "").strip()
        name = (m.get("name") or "").strip() or "They"
        if role:
            added |= memory.add_fact(mem, f"{name} is a {role}." if not role.lower().startswith(("a ", "an ")) else f"{name} is {role}.")
        notes = (m.get("notes") or "").strip()
        for sentence in [x.strip() for x in notes.replace("\n", " ").split(".") if len(x.strip()) > 8][:6]:
            added |= memory.add_fact(mem, sentence.rstrip(".") + ".")
        if added:
            memory.save_memory(mem)
    except Exception as e:
        log(f"[onboard] couldn't save the notes: {e}")


def onboarded():
    return os.path.exists(MARKER)


def mark_done():
    try:
        with open(MARKER, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def key_in(text):
    """The first thing in `text` shaped like a Gemini key, or None. Pure."""
    m = KEY_RX.search(str(text or ""))
    return m.group(1) if m else None


def key_already_set(env_path=ENV_PATH):
    try:
        with open(env_path) as f:
            for line in f:
                if line.startswith("GEMINI_API_KEY=") and len(line.split("=", 1)[1].strip()) >= 12:
                    return True
    except OSError:
        pass
    return False


KEY_SLOTS = ["GEMINI_API_KEY"] + [f"GEMINI_API_KEY_{i}" for i in range(2, 9)]


def existing_keys(env_path=ENV_PATH):
    """Every Gemini key already in .env, in slot order. Pure given a path."""
    out = []
    try:
        with open(env_path) as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    for slot in KEY_SLOTS:
        for line in lines:
            if line.startswith(slot + "="):
                v = line.split("=", 1)[1].strip()
                if len(v) >= 12 and "paste" not in v.lower():
                    out.append(v)
                break
    return out


def save_key(key, env_path=ENV_PATH):
    """Add a Gemini key, into the next FREE slot. Returns True on a real write.

    This used to overwrite GEMINI_API_KEY every time, so a second key replaced
    the first and the person was capped at one project's allowance forever —
    while providers.py had rotation across GEMINI_API_KEY_2..8 sitting there
    unused. The free tier is per PROJECT, so a second project on the same
    Google account is a second full day's allowance, and hitting the wall at
    midday is the single most common way this product disappoints someone.

    Re-adding a key already present is a no-op that still returns True, so
    pasting the same thing twice never silently eats a slot.
    """
    key = str(key or "").strip()
    if not KEY_RX.fullmatch(key):
        return False
    try:
        with open(env_path) as f:
            lines = f.read().splitlines()
    except OSError:
        lines = []

    have = existing_keys(env_path)
    if key in have:
        os.environ.setdefault("GEMINI_API_KEY", have[0])
        return True

    used = set()
    for i, line in enumerate(lines):
        for slot in KEY_SLOTS:
            if line.startswith(slot + "="):
                v = line.split("=", 1)[1].strip()
                if len(v) >= 12 and "paste" not in v.lower():
                    used.add(slot)
                break
    slot = next((s for s in KEY_SLOTS if s not in used), None)
    if slot is None:
        return False                      # eight keys is already absurd

    out, placed = [], False
    for line in lines:
        # a placeholder in the target slot gets replaced rather than duplicated
        if line.startswith(slot + "=") and not placed:
            out.append(f"{slot}={key}")
            placed = True
            continue
        out.append(line)
    if not placed:
        out.append(f"{slot}={key}")
    try:
        with open(env_path, "w") as f:
            f.write("\n".join(out) + "\n")
        os.environ[slot] = key
        os.environ.setdefault("GEMINI_API_KEY", key)
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# The page
# --------------------------------------------------------------------------- #
_HTML = r"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
  :root{
    --ink:#000; --card:#111114; --card2:#1A1A1F;
    --fg:#F5F5F7; --fg2:#A1A1A6; --fg3:#6E6E73;
    --blue:#2997FF; --ok:#30D158; --mark:#FFD60A;
    --line:rgba(255,255,255,.09);
    --ease:cubic-bezier(.2,.8,.2,1);
  }
  *{box-sizing:border-box}
  html,body{margin:0;height:100%;background:var(--ink);color:var(--fg);
    font-family:-apple-system,"SF Pro Display","SF Pro Text",Helvetica,sans-serif;
    -webkit-font-smoothing:antialiased;overflow:hidden;user-select:none;
    -webkit-user-select:none;cursor:default}
  #vignette{position:fixed;inset:0;pointer-events:none;
    background:radial-gradient(70% 60% at 50% 40%,rgba(255,255,255,.035),transparent 70%)}

  #stage{position:absolute;inset:0}
  .scene{position:absolute;inset:0;display:flex;flex-direction:column;
    align-items:center;justify-content:center;text-align:center;
    padding:44px 56px 84px;opacity:0;transform:scale(.985);pointer-events:none;
    transition:opacity .6s var(--ease),transform .6s var(--ease)}
  .scene.on{opacity:1;transform:none;pointer-events:auto}

  #dots{position:absolute;left:0;right:0;bottom:28px;display:flex;gap:8px;justify-content:center}
  #dots i{width:6px;height:6px;border-radius:50%;background:#fff;opacity:.22;transition:all .45s var(--ease)}
  #dots i.cur{opacity:1;transform:scale(1.3)} #dots i.done{opacity:.5}

  /* type */
  .mark{font-size:76px;font-weight:700;letter-spacing:-.045em;line-height:1;margin:0 0 10px}
  h1{font-size:46px;line-height:1.08;font-weight:600;letter-spacing:-.03em;margin:0 0 14px}
  p.sub{font-size:20px;line-height:1.45;color:var(--fg2);margin:0 0 30px;max-width:48ch;letter-spacing:-.01em}
  .key{display:inline-block;font-family:inherit;font-size:.68em;font-weight:600;padding:.3em .72em;
    border:1px solid rgba(255,255,255,.2);border-radius:.5em;vertical-align:.1em;
    background:rgba(255,255,255,.06);color:var(--fg);margin:0 .12em;
    box-shadow:0 1px 0 rgba(255,255,255,.08),0 4px 12px rgba(0,0,0,.4)}
  .arrive{opacity:0;transform:translateY(10px);transition:opacity .7s var(--ease),transform .7s var(--ease)}
  .arrive.in{opacity:1;transform:none}

  /* buttons */
  .row{display:flex;gap:12px;align-items:center;justify-content:center}
  button{font-family:inherit;font-size:15px;font-weight:500;border:0;cursor:pointer;padding:13px 28px;
    border-radius:999px;transition:transform .15s var(--ease),background .2s,opacity .2s}
  button:active{transform:scale(.97)}
  .primary{background:var(--blue);color:#fff;min-width:150px}
  .primary:hover{background:#4AA8FF}
  .primary[disabled]{opacity:.28;cursor:default}
  .ghost{background:transparent;color:var(--fg2);padding:13px 18px} .ghost:hover{color:var(--fg)}
  .mini{font-size:13px;padding:8px 14px;border-radius:999px;background:rgba(255,255,255,.08);
    color:var(--fg);border:1px solid var(--line)} .mini:hover{background:rgba(255,255,255,.14)}
  .foot{position:absolute;left:0;right:0;bottom:54px;font-size:12.5px;color:var(--fg3);line-height:1.7;text-align:center}
  .foot a{color:var(--fg2);text-decoration:none} .foot a:hover{color:var(--fg)}
  .foot .dot{margin:0 8px;opacity:.5}
  .hint{font-size:13px;color:var(--fg3);margin-top:18px;line-height:1.6}
  .card{background:var(--card);border:1px solid var(--line);border-radius:18px}

  /* The hidden attribute has to actually hide, even when the class sets a
     display. Without this the "I already have one" note is visible from the
     start and the button does nothing visible. */
  /* what pressing Connect will actually do */
  .how{font-size:10.5px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;
       margin-left:9px;padding:2px 7px;border-radius:999px;vertical-align:1.5px;
       border:1px solid rgba(255,255,255,.14);color:var(--fg3)}
  .how.h-one{color:#7ee2a8;border-color:rgba(126,226,168,.34)}
  .how.h-sign{color:#8fb8ff;border-color:rgba(143,184,255,.34)}
  .sub.quota{font-size:15px;color:var(--fg3);margin-top:-2px;max-width:48ch}
  .sub.quota b{color:var(--fg2)}
  /* Say where a pre-filled value came from. Seeing your own name already in
     the box reads as "how does it know that" unless something says it came
     off the Mac itself — which it did, from `id -F`, with nothing looked up. */
  .prefill{display:block;margin-top:6px;font-size:13.5px;color:var(--fg3)}
  /* One by one. Six cards landing together is a wall; staggered, each one is
     read. --i is the card's index, set inline by tile(). */
  .tile.card{padding:16px 17px 15px;border-radius:15px}
  .tile .say{font-size:15px;font-weight:500;letter-spacing:-.01em;margin:0;color:var(--fg)}
  .tile .does{margin-top:7px;font-size:13px;color:var(--fg3);line-height:1.45}
  .scene.on .step{opacity:0;transform:translateY(10px);
    animation:stepIn .5s var(--ease) forwards;
    animation-delay:calc(var(--i) * 110ms + 140ms)}
  @keyframes stepIn{to{opacity:1;transform:none}}
  @media (prefers-reduced-motion:reduce){
    .scene.on .step{animation:none;opacity:1;transform:none}
  }
  [hidden]{display:none !important}
  /* ---- depth. A flat black rectangle with centred text is what "bare bones"
     means; one soft light behind the wordmark is what makes it look built. ---- */
  body::before{content:"";position:fixed;inset:0;pointer-events:none;z-index:0;
    background:
      radial-gradient(120vw 70vh at 50% -18%, rgba(41,151,255,.10), transparent 62%),
      radial-gradient(80vw 46vh at 50% 118%, rgba(255,255,255,.045), transparent 60%)}
  .scene{position:relative;z-index:1}

  /* ---- the demo line: what Neo is actually for, in its own words, typed out.
     This replaces three CSS mock-ups of windows that read as grey placeholder
     boxes. Real sentences a person can say beat a drawing of a window. ---- */
  .demo{margin:26px 0 30px;min-height:74px;display:flex;flex-direction:column;
        align-items:center;justify-content:center;gap:9px}
  .demoline{display:flex;align-items:center;gap:3px;font-size:21px;
            letter-spacing:-.01em;color:var(--fg);min-height:28px}
  .demo .a{font-size:15px;color:var(--fg3);min-height:20px;
           transition:opacity .45s var(--ease)}
  .caret{display:inline-block;width:2px;height:1.05em;background:var(--blue);
         border-radius:1px;animation:blink 1.05s step-end infinite}
  @keyframes blink{0%,100%{opacity:1}50%{opacity:0}}
  @media (prefers-reduced-motion:reduce){ .caret{animation:none} }

  /* ---- the welcome hero. Type, not fake screenshots. The old welcome put
     three CSS mock-ups of windows on the first screen; they read as grey
     placeholder boxes, which is the worst possible first impression for a
     product whose whole claim is that it does real things. ---- */
  .hero{font-size:56px;line-height:1.06;letter-spacing:-.025em;font-weight:600;
        margin:18px 0 14px;max-width:15ch}
  .hero .key.big{font-size:.62em;padding:.12em .36em;vertical-align:.10em;
        border-radius:10px;margin:0 .04em}
  .sub.wide{max-width:46ch;font-size:17px;line-height:1.5}
  @media (max-height:720px){ .hero{font-size:44px} }


  /* a tiny mac window */
  .win{position:absolute;left:18px;right:18px;top:18px;bottom:-10px;background:var(--card2);
    border-radius:10px 10px 0 0;border:1px solid var(--line);box-shadow:0 10px 30px rgba(0,0,0,.5)}
  .win .bar{height:18px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:4px;padding:0 8px}
  .win .bar i{width:6px;height:6px;border-radius:50%;background:#3A3A40}
  .win .bar i:first-child{background:#FF5F57}.win .bar i:nth-child(2){background:#FEBC2E}.win .bar i:nth-child(3){background:#28C840}
  .ln{height:6px;border-radius:3px;background:#2C2C33}
  .lbl{height:7px;border-radius:3px;background:#3A3A42}

  /* -- point: settings rows, a ring lands on the second toggle -- */ .v-point .tg:after{content:"";position:absolute;top:2px;left:2px;width:11px;height:11px;border-radius:50%;background:#fff}
  @keyframes ring{0%,18%{opacity:0;transform:scale(1.6)}30%{opacity:1;transform:scale(1)}40%{transform:scale(1.06)}50%{transform:scale(1)}80%{opacity:1}92%,100%{opacity:0}}

  /* -- see: an error dialog, then the answer chip -- */
  @keyframes chip{0%,30%{opacity:0;transform:translateY(8px)}42%{opacity:1;transform:none}85%{opacity:1}95%,100%{opacity:0}}

  /* -- do: a form field types itself, then the button presses -- */
  @keyframes typeit{0%,15%{width:0}45%,100%{width:112px}}
  @keyframes blink{50%{opacity:0}}
  @keyframes press{0%,60%{filter:brightness(1);transform:scale(1)}66%{filter:brightness(.7);transform:scale(.95)}72%,100%{filter:brightness(1);transform:scale(1)}}

  /* -- remind: a notification slides in -- */ .v-remind .note .t small{display:block;color:var(--fg2);font-size:10px}
  @keyframes slide{0%,20%{transform:translateX(120%)}32%{transform:translateX(0)}82%{transform:translateX(0)}94%,100%{transform:translateX(120%)}}

  /* -- mail: a draft fills in -- */.v-mail .body .ln:nth-child(3){animation-delay:.5s;max-width:60%}
  @keyframes grow{0%,20%{width:0}40%,88%{width:100%}100%{width:0}}

  /* -- answer: a comparison panel, bars grow -- */.v-answer .bar:nth-child(4) i{animation-delay:.3s}
  @keyframes fill{0%,15%{width:0}45%,88%{width:var(--w)}100%{width:0}}

  /* -- highlight: a line in a document -- */
  @keyframes sweep{0%,22%{width:0}40%,88%{width:calc(100% + 6px)}100%{width:0}}

  /* permissions */
  .perms{width:600px;text-align:left;margin:-6px 0 26px;padding:2px 22px}
  .perm{display:grid;grid-template-columns:26px 1fr auto;gap:16px;align-items:center;padding:13px 0;border-bottom:1px solid var(--line)}
  .perm:last-child{border-bottom:0}
  .perm .dot{width:22px;height:22px;border-radius:50%;border:1.5px solid rgba(255,255,255,.28);position:relative;transition:all .45s var(--ease)}
  .perm.ok .dot{border-color:var(--ok);background:var(--ok);box-shadow:0 0 18px rgba(48,209,88,.45)}
  .perm.ok .dot:after{content:"";position:absolute;left:7px;top:3px;width:5px;height:10px;border:solid #04250f;border-width:0 2px 2px 0;transform:rotate(45deg)}
  .perm .nm{font-size:15.5px;font-weight:500;letter-spacing:-.01em}
  .perm .why{font-size:12.5px;color:var(--fg2);margin-top:2px;line-height:1.4}
  .perm.ok .mini{visibility:hidden}
  .perm.opt{padding:8px 0} .perm.opt .why{display:none} .perm.opt .nm{font-weight:400;color:var(--fg2)}
  .perm.opt .nm:after{content:"optional";font-size:10.5px;color:var(--fg3);margin-left:9px;letter-spacing:.06em;text-transform:uppercase}
  .perm.rec .nm:after{content:"recommended";font-size:10.5px;color:var(--blue);margin-left:9px;letter-spacing:.06em;text-transform:uppercase}
  .perm.rec.ok .nm:after{content:"connected";color:var(--ok)}
  .scene[data-s="perms"]{padding-top:20px;padding-bottom:72px}
  .scene[data-s="perms"] h1{font-size:40px;margin-bottom:10px}
  .scene[data-s="perms"] p.sub{margin-bottom:20px;font-size:17px;max-width:56ch}

  /* the key */
  .keybox{width:600px;margin:-6px 0 28px;padding:20px 22px;display:flex;align-items:center;gap:14px;text-align:left}
  .keybox .dot{width:10px;height:10px;border-radius:50%;background:var(--fg3);flex:none}
  .keybox.wait .dot{background:var(--blue);animation:pulse 1.4s ease-in-out infinite;box-shadow:0 0 14px var(--blue)}
  .keybox.ok .dot{background:var(--ok);box-shadow:0 0 14px rgba(48,209,88,.6)}
  @keyframes pulse{50%{opacity:.35}}
  .keybox .t{font-size:15px;color:var(--fg2)} .keybox.ok .t{color:var(--fg)}
  .keybox code{font:13px ui-monospace,Menlo,monospace;color:var(--fg3)}

  /* about you */
  .form{width:600px;text-align:left;padding:18px 22px 8px;margin:-6px 0 26px;display:flex;flex-direction:column;gap:14px}
  .form label{display:flex;flex-direction:column;gap:6px;font-size:12.5px;color:var(--fg2);letter-spacing:.01em}
  .form .opt{font-size:10.5px;color:var(--fg3);letter-spacing:.06em;text-transform:uppercase;margin-left:6px}
  .form input,.form textarea{font-family:inherit;font-size:14.5px;color:var(--fg);background:rgba(255,255,255,.07);
    border:1px solid rgba(255,255,255,.12);border-radius:10px;padding:9px 12px;outline:none;resize:none}
  .form input:focus,.form textarea:focus{border-color:rgba(255,255,255,.35)}
  .form input::placeholder,.form textarea::placeholder{color:rgba(255,255,255,.3)}
  .form .seg{display:flex;gap:6px}
  .form .seg button{font-family:inherit;font-size:12.5px;font-weight:500;padding:8px 12px;border-radius:999px;
    background:rgba(255,255,255,.07);color:var(--fg2);border:1px solid rgba(255,255,255,.1);min-width:0}
  .form .seg button.on{background:#fff;color:#111;border-color:#fff}
  .scene[data-s="about"]{padding-top:24px}
  .scene[data-s="about"] h1{font-size:40px;margin-bottom:8px}
  .scene[data-s="about"] p.sub{font-size:16px;margin-bottom:18px;max-width:52ch}

  /* first words: the key itself, big */
  .bigkey{font-size:34px;font-weight:600;padding:.45em 1em;border-radius:.4em;border:1px solid rgba(255,255,255,.22);
    background:linear-gradient(180deg,#26262C,#141417);box-shadow:0 2px 0 rgba(255,255,255,.08),0 18px 40px rgba(0,0,0,.6);
    margin:0 0 30px;animation:hold 3.2s var(--ease) infinite}
  @keyframes hold{0%,35%{transform:translateY(0);box-shadow:0 2px 0 rgba(255,255,255,.08),0 18px 40px rgba(0,0,0,.6)}
    45%,70%{transform:translateY(3px);box-shadow:0 0 0 rgba(255,255,255,.08),0 8px 20px rgba(0,0,0,.6)}
    80%,100%{transform:translateY(0);box-shadow:0 2px 0 rgba(255,255,255,.08),0 18px 40px rgba(0,0,0,.6)}}
  .heard{margin:0 0 30px;min-height:40px;font-size:26px;font-weight:400;color:var(--fg);letter-spacing:-.02em}
  .heard:before{content:"\201C";color:var(--blue)} .heard:after{content:"\201D";color:var(--blue)}
  .heard:empty:before,.heard:empty:after{content:""}

  .also{font-size:13.5px;color:var(--fg2);margin:0 0 26px}
  .also .dot{margin:0 9px;opacity:.5}
  .scene[data-s="tour"]{padding-top:34px}
  .scene[data-s="tour"] h1{font-size:40px;margin-bottom:6px}
  .scene[data-s="tour"] p.sub{font-size:16px;margin-bottom:22px}
  .scene[data-s="tour"] .tile .say{font-size:13.5px;margin-top:9px}
  .scene[data-s="tour"] .tile .what{display:none}

  @media (prefers-reduced-motion:reduce){*{animation:none!important}.scene,.arrive{transition:none}}
</style></head><body>
  <div id="vignette"></div>
  <div id="stage">

    <section class="scene" data-s="welcome">
      <div class="mark arrive">Neo</div>
      <h1 class="hero arrive">Hold <span class="key big">fn</span>.<br>Say it. Let go.</h1>
      <p class="sub wide arrive">No window to open, no wake word, nothing to click.
        The microphone is open only while the key is down.</p>
      <div class="demo arrive" aria-hidden="true">
        <div class="demoline"><span class="q" id="demoQ"></span><span class="caret"></span></div>
        <div class="a" id="demoA"></div>
      </div>
      <div class="row arrive"><button class="primary" onclick="go('perms')">Set me up</button></div>
      <div class="foot arrive">Two minutes<span class="dot">·</span>Nothing bills<span class="dot">·</span>Nothing leaves your Mac but the question<br>
        <a href="#" onclick="send({action:'open_doc',doc:'docs/TERMS.md'});return false">Terms</a><span class="dot">·</span><a href="#" onclick="send({action:'open_doc',doc:'docs/PRIVACY.md'});return false">Privacy</a></div>
    </section>

    <section class="scene" data-s="perms">
      <h1 class="arrive">Let macOS let me in.</h1>
      <p class="sub arrive">Tap each one and say yes. I tick them off as they land.</p>
      <div class="perms card arrive" id="perms"></div>
      <div class="row arrive"><button class="primary" id="permsNext" disabled onclick="go('key')">Continue</button></div>
      <div class="hint arrive" id="permsHint"></div>
    </section>

    <section class="scene" data-s="key">
      <h1 class="arrive">Copy a key.</h1>
      <p class="sub wide arrive">That is the entire step. <b>Copy it and I take it from
        your clipboard</b> — nothing to paste, nothing to save. Free, and it never bills.</p>
      <p class="sub wide arrive quota"><b>Then do it again.</b> The free allowance is per
        Google <i>project</i>, not per account, so a second project on the same
        account is a second full day. Make two or three; I'll stack them and move
        to the next one when today's runs out.</p>
      <div class="keybox card arrive" id="keybox"><div class="dot"></div><div class="t" id="keyText">Waiting for you to copy a key…</div></div>
      <div class="row arrive">
        <button class="primary" onclick="send({action:'open_key_page'})">Get me a key</button>
        <button class="ghost" onclick="document.getElementById('haveKey').hidden=false">I already have one</button>
        <button class="ghost" id="keyNext" onclick="go('connect')">Later</button>
      </div>
      <div class="hint arrive" id="haveKey" hidden>Just copy it. I'm watching the
        clipboard and I'll pick it up the moment you do.</div>
      <div class="hint arrive">Skip it and Neo still works — voice, timers, reminders, your calendar, the markets, your Mac. Only thinking waits. Say "add my key" any time.</div>
    </section>

    <section class="scene" data-s="connect">
      <h1 class="arrive">Connect what Neo works with.</h1>
      <p class="sub wide arrive">Press <b>Connect</b>, then <b>Allow</b> on the prompt.
        I read something back each time so you know it worked. All optional — say
        "connect calendar" any time later.</p>
      <div class="perms card arrive" id="conns"></div>
      <div class="row arrive"><button class="primary" onclick="go('about')">Continue</button></div>
      <div class="hint arrive" id="connHint"></div>
    </section>

    <section class="scene" data-s="about">
      <h1 class="arrive">A few things about you.</h1>
      <p class="sub arrive">So Neo is yours from the first sentence. Change any of it
        later by just telling Neo. <span class="prefill">Your name is filled in from
        this Mac's account — nothing was looked up.</span></p>
      <div class="form card arrive">
        <label>What should Neo call you?<input id="aName" type="text" autocomplete="off" spellcheck="false" placeholder="First name"></label>
        <label>What do you do?<input id="aRole" type="text" autocomplete="off" spellcheck="false" placeholder="Student · engineer · founder · teacher · parent…"></label>
        <label>How do you like answers?
          <div class="seg" id="aLen">
            <button type="button" data-v="short" class="on">Short</button>
            <button type="button" data-v="medium">Answer + one line of why</button>
            <button type="button" data-v="long">Detailed</button>
          </div></label>
        <label>Anything Neo should know? <span class="opt">optional</span><textarea id="aNotes" rows="2" placeholder="People you mention a lot, what you're working on, how you like things done…"></textarea></label>
      </div>
      <div class="row arrive"><button class="primary" id="aboutNext" onclick="sendAbout()">Continue</button></div>
    </section>

    <section class="scene" data-s="first">
      <div class="bigkey arrive">fn</div>
      <h1 class="arrive">Hold it. Say it. Let go.</h1>
      <p class="sub arrive">Try <b>"what time is it?"</b> or <b>"what's the weather?"</b> —
      those two work even before you add a key. Neo answers the moment you release.</p>
      <div class="heard arrive" id="heard"></div>
      <div class="row arrive"><button class="primary" id="firstNext" disabled onclick="go('tour')">Continue</button>
        <button class="ghost" onclick="go('tour')">Skip</button></div>
    </section>

    <section class="scene" data-s="tour">
      <h1 class="arrive">Things to try.</h1>
      <p class="sub arrive">Say any of them, whenever you like.</p>
      <div class="tiles six arrive" id="tour"></div>
      <div class="also arrive" id="also"></div>
      <div class="row arrive"><button class="primary" onclick="finish()">Start using Neo</button></div>
    </section>

  </div>
  <div id="dots"></div>
<script>
  var ORDER = ["welcome","perms","key","connect","about","first","tour"];
  var CONNS = %CONNS%;
  var cur = "welcome";
  var TOUR = %TOUR%;
  var PERMS = %PERMS%;

  function send(m){ try{ window.webkit.messageHandlers.neo.postMessage(JSON.stringify(m)); }catch(e){} }
  function esc(s){ return String(s==null?"":s).replace(/[&<>"']/g,function(c){
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]; }); }

  // The little living windows. Each is a real thing Neo does, drawn small
  // and looping, so the person SEES the use case instead of reading it.
  // which window goes with which card, and the one line under it
  var CARDS = [
    {v:"point",  say:"Where do I turn off read receipts?", does:"a ring lands on the real control and follows you"},
    {v:"hl",     say:"Highlight the line about the deadline.", does:"a highlighter goes over the actual words"},
    {v:"remind", say:"Remind me to call mum at six.", does:"into Reminders, then read back to you"},
    {v:"see",    say:"What does this error mean?", does:"it's already looking at your screen"},
    {v:"do",     say:"Open the form and put my email in.", does:"it does it, it doesn't explain it"},
    {v:"mail",   say:"Draft a reply to that.", does:"written into Drafts. You press send"},
    {v:"answer", say:"Lease or buy?", does:"a small panel appears beside the answer"}
  ];
  // No more CSS mock-ups of windows. They read as grey placeholder boxes, which
  // is the opposite of what this screen is for. A real sentence you can say,
  // and one line on what Neo does about it, is the whole card.
  function tile(c, i){
    return '<div class="tile card step" style="--i:'+i+'">'+
           '<div class="say">\u201C'+esc(c.say)+'\u201D</div>'+
           (c.does ? '<div class="does">'+esc(c.does)+'</div>' : '')+
           '</div>';
  }
  // Guarded: the welcome scene no longer has a #showcase, and one missing
  // element used to throw here and take every later line of this script with
  // it — including the tour and the scene machinery.
  function fill(id, html){ var el = document.getElementById(id); if (el) el.innerHTML = html; }

  /* The welcome demo: type a real command, show what Neo does, move on.
     Pauses itself when the scene isn't showing, and respects reduced motion. */
  var DEMO = [
    ["What does this error mean?",        "reads what's on your screen"],
    ["Where do I turn off read receipts?","a ring lands on the real control"],
    ["Remind me to call mum at six.",     "goes into Reminders, read back to you"],
    ["Summarise the PDF I've got open.",  "opens it, reads it, tells you"],
    ["What's Apple at?",                  "live price, no subscription"]
  ];
  (function demoLoop(){
    var q = document.getElementById("demoQ"), a = document.getElementById("demoA");
    if (!q || !a) return;
    var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    var i = 0;
    function show(){
      if (cur !== "welcome"){ setTimeout(show, 700); return; }   // only on screen 1
      var item = DEMO[i % DEMO.length]; i++;
      a.style.opacity = 0;
      if (reduce){ q.textContent = item[0]; a.textContent = item[1]; a.style.opacity = 1;
                   setTimeout(show, 3600); return; }
      q.textContent = ""; var n = 0;
      (function type(){
        if (cur !== "welcome") { setTimeout(show, 700); return; }
        q.textContent = item[0].slice(0, ++n);
        if (n < item[0].length) return setTimeout(type, 26 + Math.random() * 30);
        a.textContent = item[1]; a.style.opacity = 1;
        setTimeout(show, 2300);
      })();
    }
    show();
  })();
  fill("tour", [CARDS[3],CARDS[4],CARDS[5],CARDS[6],CARDS[0],CARDS[2]].map(tile).join(""));
  fill("also", 'Also: ' + TOUR.slice(6).map(function(c){ return esc(c[0]); }).join('<span class="dot">·</span>'));

  function arrive(scene){
    var els = scene.querySelectorAll(".arrive");
    els.forEach(function(el){ el.classList.remove("in"); });
    els.forEach(function(el, i){ setTimeout(function(){ el.classList.add("in"); }, 100 + i * 100); });
  }
  function go(s){
    if (ORDER.indexOf(s) < 0) return;
    cur = s;
    document.querySelectorAll(".scene").forEach(function(el){
      var on = el.getAttribute("data-s") === s;
      el.classList.toggle("on", on);
      if (on) arrive(el); });
    var i = ORDER.indexOf(s);
    document.querySelectorAll("#dots i").forEach(function(el, j){
      el.classList.toggle("cur", j === i); el.classList.toggle("done", j < i); });
    send({action:"scene", scene:s});
  }
  function finish(){ send({action:"done"}); }

  function renderPerms(state){
    var box = document.getElementById("perms"), html = "";
    var required = 0, granted = 0;
    PERMS.forEach(function(p){
      var ok = state && state[p.key] === true;
      if (p.required){ required++; if (ok) granted++; }
      html += '<div class="perm'+(ok?' ok':'')+(p.required?'':' opt')+'">'+
        '<div class="dot"></div><div><div class="nm">'+esc(p.name)+'</div>'+
        '<div class="why">'+esc(p.why)+'</div></div>'+
        '<button class="mini" onclick="send({action:\'open_settings\',key:\''+p.key+'\'})">Open Settings</button></div>';
    });
    box.innerHTML = html;
    var btn = document.getElementById("permsNext");
    btn.disabled = granted < required;
    document.getElementById("permsHint").textContent = granted < required
      ? (required - granted) + " required permission" + (required-granted===1?"":"s") + " still to grant."
      : "All set.";
  }
  var connState = {}, connNote = {};
  function renderConns(){
    document.getElementById("conns").innerHTML = CONNS.map(function(c){
      var ok = connState[c.key] === true, note = connNote[c.key] || c.why;
      // Say what pressing Connect will actually DO. "one tap" means macOS
      // raises its own prompt; "sign in" means a browser or terminal window.
      // Presenting both identically is what made this screen feel broken.
      var how = ok ? '' : (c.how ? '<span class="how h-'+c.how.split(" ")[0]+'">'+esc(c.how)+'</span>' : '');
      return '<div class="perm'+(ok?' ok':'')+(c.rec?' rec':'')+'"><div class="dot"></div><div><div class="nm">'+esc(c.name)+how+'</div>'+
             '<div class="why">'+esc(note)+'</div></div>'+
             '<button class="mini" onclick="send({action:\'connect\',key:\''+c.key+'\'})">'+(ok?'Check':'Connect')+'</button></div>';
    }).join("");
  }
  function renderKey(k){
    var box = document.getElementById("keybox"), t = document.getElementById("keyText");
    box.className = "keybox card arrive in" + (k.saved ? " ok" : (k.watching ? " wait" : ""));
    if (k.saved){
      var n = k.count || 1;
      t.innerHTML = n > 1
        ? "<b>"+n+" keys in.</b> That is "+n+"\u00d7 the daily allowance. "+
          "Copy another any time \u2014 I'm still watching."
        : "Key saved \u2014 that's my real voice. <code>"+esc(k.tail)+"</code><br>"+
          "<b>Copy a second one</b> from a new Google project and you get double the day.";
      document.getElementById("keyNext").textContent = "Continue";
      document.getElementById("keyNext").className = "primary";
    } else if (k.already){
      t.textContent = "A key is already set up on this Mac. Copy a new one to replace it, or continue.";
      document.getElementById("keyNext").textContent = "Continue";
      document.getElementById("keyNext").className = "primary";
    } else {
      t.textContent = "Waiting for a key on the clipboard…";
    }
  }
  function renderHeard(text){
    var h = document.getElementById("heard");
    if (!text) return;
    h.textContent = text;
    document.getElementById("firstNext").disabled = false;
  }
  var aboutLen = "short";
  document.querySelectorAll("#aLen button").forEach(function(b){
    b.addEventListener("click", function(){
      aboutLen = b.getAttribute("data-v");
      document.querySelectorAll("#aLen button").forEach(function(x){ x.classList.toggle("on", x === b); });
    });
  });
  function sendAbout(){
    send({action:"about", name:document.getElementById("aName").value.trim(),
          role:document.getElementById("aRole").value.trim(), length:aboutLen,
          notes:document.getElementById("aNotes").value.trim()});
    go("first");
  }
  function renderAbout(a){
    if (a.name && !document.getElementById("aName").value) document.getElementById("aName").value = a.name;
  }

  window.render = function(payload){
    var s = typeof payload === "string" ? JSON.parse(payload) : payload;
    if (s.perms) renderPerms(s.perms);
    if (s.key) renderKey(s.key);
    if (s.heard) renderHeard(s.heard);
    if (s.about) renderAbout(s.about);
    if (s.conns){ connState = s.conns.state || {}; connNote = s.conns.note || {}; renderConns(); }
  };
  renderConns();
  document.addEventListener("keydown", function(e){
    if (e.key !== "Enter" || e.target.tagName === "TEXTAREA") return;
    var btn = document.querySelector('.scene.on .primary');
    if (btn && !btn.disabled) btn.click();
  });
  document.getElementById("dots").innerHTML = ORDER.map(function(){ return "<i></i>"; }).join("");
  go("welcome"); send({action:"ready"});
</script></body></html>"""


class Onboarding:
    """The window. Main thread only, like every other window here."""

    def __init__(self, say=None, log=print, on_done=None, env_path=None,
                 mark=True, app=None):
        # `app` is the running Neo. Optional, so the --demo path and the tests
        # construct this with no app at all; when it IS there, a key saved
        # mid-flow upgrades the live process instead of waiting for a restart.
        self._app = app
        self._say = say or (lambda t: None)
        self._log = log
        self._on_done = on_done
        self._env = ENV_PATH if env_path is None else env_path
        self._mark = mark
        self._state = {"perms": {}, "key": {}, "heard": "", "about": {"name": _mac_first_name()},
                       "conns": {"state": {}, "note": {}}}
        self._scene = "welcome"
        self._narrated = set()
        self._clip_seen = None
        self._closed = False
        self._voice = None          # the afplay process for the current line
        self._polling = False       # a permissions check is running off-thread
        self._build()

    # ---- window ------------------------------------------------------------ #
    def _build(self):
        from Cocoa import (NSApplication, NSWindow, NSColor, NSScreen, NSMakeRect,
                           NSBackingStoreBuffered, NSTimer, NSObject,
                           NSWindowStyleMaskBorderless, NSWindowStyleMaskTitled,
                           NSWindowStyleMaskClosable, NSWindowStyleMaskFullSizeContentView)
        import WebKit

        outer = self
        frame = NSScreen.mainScreen().frame()
        W, H = min(1000, frame.size.width - 80), min(680, frame.size.height - 80)
        x = frame.origin.x + (frame.size.width - W) / 2
        y = frame.origin.y + (frame.size.height - H) / 2
        mask = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                | NSWindowStyleMaskFullSizeContentView)
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, W, H), mask, NSBackingStoreBuffered, False)
        win.setTitlebarAppearsTransparent_(True)
        win.setTitle_("")
        win.setBackgroundColor_(NSColor.colorWithSRGBRed_green_blue_alpha_(0.027, 0.035, 0.047, 1))
        win.setMovableByWindowBackground_(True)

        class _OnboardBridge(NSObject):
            def userContentController_didReceiveScriptMessage_(self, c, message):
                try:
                    outer._on_message(json.loads(str(message.body())))
                except Exception as e:
                    outer._log(f"[onboard] {e}")

        cfg = WebKit.WKWebViewConfiguration.alloc().init()
        bridge = _OnboardBridge.alloc().init()
        cfg.userContentController().addScriptMessageHandler_name_(bridge, "neo")
        web = WebKit.WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, W, H), cfg)
        try:
            web.setValue_forKey_(False, "drawsBackground")
        except Exception:
            pass
        import connectors as _conn
        html = (_HTML.replace("%TOUR%", json.dumps(TOUR))
                     .replace("%CONNS%", json.dumps([
                         {"key": k, "name": n, "why": w, "rec": r,
                          "how": _conn.HOW_LABEL.get(how, "")}
                         for k, n, w, r, how in _conn.CONNECTORS]))
                     .replace("%PERMS%", json.dumps([
                         {"key": k, "name": n, "why": w,
                          "required": k in __import__("perms").REQUIRED}
                         for k, n, w, _ in __import__("perms").PERMISSIONS])))
        web.loadHTMLString_baseURL_(html, None)
        win.setContentView_(web)
        win.makeKeyAndOrderFront_(None)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.win, self.web, self._bridge = win, web, bridge

        class _OnboardTick(NSObject):
            def tick_(self, timer):
                try:
                    outer._tick()
                except Exception as e:
                    outer._log(f"[onboard] tick: {e}")
        t = _OnboardTick.alloc().init()
        self._ticker = t
        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0, t, "tick:", None, True)

    def _push(self):
        js = "window.render && render(%s)" % json.dumps(json.dumps(self._state))
        try:
            self.web.evaluateJavaScript_completionHandler_(js, None)
        except Exception:
            pass

    # ---- the loop ---------------------------------------------------------- #
    def _tick(self):
        if self._closed:
            return
        if self._scene == "perms" and not self._polling:
            # Off the main thread: a status() call talks to TCC and takes
            # most of a second. Blocking here froze the page's animation and
            # anything else the process was doing while the list was up.
            self._polling = True
            threading.Thread(target=self._poll_perms, daemon=True,
                             name="neo-onboard-perms").start()
        # Watch for as long as they are on the key scene — NOT "until one key
        # lands". Stopping at the first key is what made a second and third
        # impossible to add: the screen invited another, the copy did nothing,
        # and the person concluded the feature was broken. Which it was.
        # Each extra key is another full day's allowance, so this is the one
        # place in the product where more is genuinely better.
        if self._scene == "key":
            self._watch_clipboard()
        self._push()

    def _poll_perms(self):
        try:
            import perms
            self._state["perms"] = perms.status()
        except Exception as e:
            self._log(f"[onboard] perms: {e}")
        finally:
            self._polling = False

    def _watch_clipboard(self):
        try:
            import desk
            text = desk.clipboard(limit=600) or ""
        except Exception:
            return
        if text == self._clip_seen:
            return
        self._clip_seen = text
        key = key_in(text)
        if key and save_key(key, self._env):
            n = len(existing_keys(self._env))
            self._state["key"] = {"saved": True, "tail": "…" + key[-6:],
                                  "where": self._env, "count": n, "watching": True}
            self._log(f"[onboard] Gemini key saved from the clipboard -> {self._env}")
            # Actually BECOME the full version before saying so. This line used
            # to promise "that's my real voice from here on" while the process
            # carried on in Kokoro with the local brain, because KEYLESS is
            # decided at import. Claiming a thing and not doing it is the one
            # output this project forbids, and it was happening here.
            upgraded = False
            app = getattr(self, "_app", None)
            if app is not None and hasattr(app, "key_arrived"):
                try:
                    upgraded = bool(app.key_arrived(key))
                except Exception as e:
                    self._log(f"[onboard] key upgrade failed: {e}")
            self._state["key"]["upgraded"] = upgraded
            if n == 1:
                self._say("Got it. That's my real voice from here on. Add a second "
                          "key if you want and you get double the daily allowance."
                          if upgraded else
                          "Got it, key saved. I'll be the full version next time I start.")
            else:
                self._say(f"That's {n} keys. {n} times the daily allowance.")

    # ---- messages from the page ------------------------------------------- #
    def _on_message(self, m):
        action = m.get("action")
        if action == "ready":
            self._narrate("welcome")
        elif action == "scene":
            self._scene = m.get("scene", "welcome")
            if self._scene == "connect":
                import connectors
                self._state["conns"]["state"] = connectors.status()
            if self._scene == "key":
                # Always watching. "Already set" is information, not a stop —
                # a person swapping keys, or testing the flow, still needs the
                # paste to land.
                self._state["key"] = {"already": key_already_set(self._env),
                                      "watching": True}
            self._narrate(self._scene)
            self._tick()
        elif action == "open_settings":
            import perms
            perms.open_settings(m.get("key", ""))
        elif action == "open_doc":
            # The policies live in docs/ now (the repo root had a hundred files
            # and pushed the README below the fold on GitHub). Still resolved by
            # BASENAME against a known folder, never by a path from the page —
            # the page must not be able to ask for an arbitrary file.
            import subprocess
            name = os.path.basename(m.get("doc", ""))
            for base in (os.path.join(HERE, "docs"), HERE):
                doc = os.path.join(base, name)
                if name and os.path.exists(doc):
                    subprocess.Popen(["open", doc])
                    break
        elif action == "open_key_page":
            import subprocess
            subprocess.Popen(["open", KEY_URL])
            self._state["key"]["watching"] = True
            self._push()
        elif action == "about":
            save_about(m, log=self._log)
            self._push()
        elif action == "connect":
            key = m.get("key", "")
            threading.Thread(target=self._connect, args=(key,), daemon=True,
                             name="neo-onboard-connect").start()
        elif action == "done":
            self._finish()

    def _connect(self, key):
        """Ask for the permission now, then read something back and show it
        on the row — off the main thread, since a prompt can sit for a while."""
        import connectors
        try:
            first = connectors.connect(key, log=self._log)
            seen = connectors.test(key, log=self._log)
            ok = connectors.status().get(key) is True or seen.lower().startswith(f"{key} connected") \
                or "connected:" in seen.lower()
            self._state["conns"]["state"][key] = bool(ok)
            self._state["conns"]["note"][key] = seen or first
            self._log(f"[onboard] connect {key}: {first} {seen}")
        except Exception as e:
            self._state["conns"]["note"][key] = f"Couldn't connect: {e}"
        self._push()

    def _narrate(self, scene):
        if scene in self._narrated:
            return
        self._narrated.add(scene)
        # Played by afplay, OUTSIDE this process. In-process playback ran a
        # Python callback per audio buffer, and anything that held the
        # interpreter for a while — the permissions poll, a WebKit message —
        # starved it: the line broke up the moment a button was pressed. A
        # child process can't be starved by us. One at a time: a new scene
        # cuts the old line and plays its own.
        self._hush()
        path = os.path.join(AUDIO_DIR, f"{scene}.wav")
        if not os.path.exists(path):
            # Missing file -> silence. Never a stand-in voice: the first
            # voice a person hears IS Neo, or there is no voice.
            self._log(f"[onboard] no recorded line for {scene!r}")
            return
        import subprocess
        try:
            self._voice = subprocess.Popen(
                ["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            self._log(f"[onboard] afplay: {e}")

    def _hush(self):
        """Stop whatever line is playing."""
        v, self._voice = self._voice, None
        if v is not None and v.poll() is None:
            try:
                v.terminate()
            except OSError:
                pass

    # ---- Neo tells us it heard something ----------------------------------- #
    def heard(self, text):
        """Called by Neo when a real turn arrives. Advances 'first words'."""
        if self._scene == "first" and text and len(text.split()) >= 1:
            self._state["heard"] = text.strip()[:120]
            self._push()

    def _finish(self):
        if self._closed:
            return
        self._closed = True
        if self._mark:
            mark_done()
        self._narrate("done")
        # give the last line time to be heard before the window goes
        try:
            self._timer.invalidate()
        except Exception:
            pass
        try:
            self.win.orderOut_(None)
        except Exception:
            pass
        if self._on_done:
            try:
                self._on_done()
            except Exception:
                pass


if __name__ == "__main__":
    import sys
    from PyObjCTools import AppHelper
    from Cocoa import NSApplication
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(0)
    demo = "--demo" in sys.argv
    env_path = ENV_PATH
    if demo:
        # The REAL flow against a COPY of .env: the clipboard is genuinely
        # watched and a pasted key is genuinely written — just not into the
        # file Neo runs on, and the first-run marker is left alone. So the key
        # step can be tried for real without risking a working key.
        import shutil, tempfile
        env_path = os.path.join(tempfile.gettempdir(), "neo-onboard-demo.env")
        try:
            shutil.copy(ENV_PATH, env_path)
        except OSError:
            open(env_path, "w").close()
        print(f"demo: a pasted key will be saved to {env_path} (not the real .env)")

    def _say(t):
        print("Neo:", t)

    ob = Onboarding(say=_say, log=print, env_path=env_path, mark=not demo,
                    on_done=lambda: AppHelper.stopEventLoop())
    if demo:
        import threading as _th
        def _fake_hello():
            # stand in for a real first turn once they reaches that scene
            while not ob._closed:
                if ob._scene == "first":
                    time.sleep(3); ob.heard("Hello Neo, can you hear me?"); break
                time.sleep(0.5)
        _th.Thread(target=_fake_hello, daemon=True).start()
    AppHelper.runEventLoop()
    if demo:
        try:
            saved = [l for l in open(env_path).read().splitlines() if l.startswith("GEMINI_API_KEY=")]
            print("demo: .env copy ends with ->", saved[0][:24] + "…" if saved else "(no key)")
        except OSError:
            pass
