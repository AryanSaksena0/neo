"""
skills.py — Neo grows its own abilities.

A skill is one Python file in skills/ following a strict contract. Neo loads
them at boot, hot-reloads on demand, and — this is the point — WRITES NEW ONES:
say "teach yourself to <do something>" and Neo turns it into an engineering
brief for Claude Code, which builds the skill, tests it, and drops it in
skills/. Neo reloads and the ability is live. Permanently. That's the loop
that means there's no fixed feature list: judgment (Gemini) + construction
(Claude Code) + this folder as muscle memory.

The contract every skill file must follow:

    NAME = "coin_flip"                     # short slug
    DESCRIPTION = "Flips a coin. Trigger: 'flip a coin'."
    def matches(text: str) -> bool: ...    # fast + pure: does this utterance want me?
    def handle(text: str, ctx) -> str: ... # do the work; return what Neo SPEAKS
    def self_test() -> bool: ...           # offline check; False/raise = not loaded

ctx (a Ctx object) gives skills safe reach into Neo's world:
    ctx.client / ctx.model      google-genai client + model name (1 call = 1 quota)
    ctx.search(q, n)            live web search -> [{"title","url"}]
    ctx.fetch(url, chars)       readable text of a page
    ctx.open_app(name) / ctx.open_url(url)
    ctx.show(spec)              canvas visual (see canvas.py spec kinds)
    ctx.business()              live the project numbers as a string
    ctx.remember(fact)          save to Neo's long-term memory

Loader safety: a skill that fails its self_test, breaks the contract, or
throws on import is skipped with a log line — it can never take Neo down.
Broken matches()/handle() at runtime are caught the same way.
"""

import importlib.util
import os
import re
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
SKILLS_DIR = os.path.join(HERE, "skills")

_REQUIRED = ("NAME", "DESCRIPTION", "matches", "handle", "self_test")

_registry = []        # list of loaded skill modules
_ctx = None           # the shared Ctx handed to handle()


# --------------------------------------------------------------------------- #
# the context object skills get
# --------------------------------------------------------------------------- #
class Ctx:
    def __init__(self, client=None, model="", say=None, notify=None):
        self.client = client
        self.model = model
        self._say = say          # neo.say -> Neo speaks (queued, thread-safe)
        self._notify = notify    # neo._deliver_insight -> on-screen card

    def say(self, text):
        """Speak through Neo's voice — including LATER, from a timer thread."""
        if self._say:
            self._say(text)
            return True
        print(f"[skills] (no voice bound) say: {text}")
        return False

    def notify(self, title, detail, urgency="medium", key=None):
        """Raise a sentinel-style card (urgency: low/medium/high)."""
        if self._notify:
            self._notify({"key": key or f"skill:{title[:30]}", "kind": "skill",
                          "urgency": urgency, "title": title, "detail": detail})
            return True
        return False

    def later(self, seconds, text):
        """Schedule Neo to SPEAK something after a delay (timers, reminders).
        Returns the timer (cancel() to abort). Survives as long as Neo runs."""
        import threading
        t = threading.Timer(max(1, float(seconds)), lambda: self.say(text))
        t.daemon = True
        t.start()
        return t

    def search(self, query, n=6):
        import web as leads
        return leads.web_search(query, n)

    def fetch(self, url, chars=1500):
        import web as leads
        return leads.fetch_text(url, chars)

    # ---- hands: the screen, and acting on it ---------------------------- #
    # A skill could search the web and open a URL and then had to stop. There
    # was no click anywhere in Neo, so "fill this form in" or "get the thing
    # out of that page" could not be written at all — which is why skills that
    # needed it were brittle inline AppleScript or nothing. See act.py.
    def click(self, what, near=None, intent="", app=None):
        """Click something by name. (ok, why). Refuses rather than guessing
        when several things on screen match."""
        import act
        return act.click(what, client=self.client, near=near,
                         intent=intent or what, app=app)

    def click_field(self, label, where="below", app=None):
        """Click into the input box under (or beside) a label, and CHECK that
        it really is a text box before saying so. (ok, why)."""
        import act
        return act.click_field(label, client=self.client, where=where,
                               app=app)

    def type(self, text):
        """Type into whatever has focus. (ok, why)."""
        import act
        return act.type_text(text)

    def press(self, key):
        """One key: return, tab, escape, down... (ok, why)."""
        import act
        return act.press(key)

    def scroll(self, amount=5, direction="down"):
        import act
        return act.scroll(amount, direction)

    def focus(self, app):
        """Bring an app to the front. Do this before clicking in it — the
        first click on an unfocused window is eaten by macOS."""
        import act
        return act.focus(app)

    def screen_text(self):
        """Everything readable on screen, as one string."""
        import act
        return act.screen_text()

    def wait_for(self, text, timeout=20):
        """Block until some text shows up on screen. True if it did."""
        import act
        return act.wait_for(text, timeout=timeout)

    def open_app(self, name):
        import hands
        return hands.open_app(name)

    def open_url(self, url):
        import hands
        return hands.open_url(url)

    def show(self, spec):
        import canvas
        return canvas.show(spec)

    def business(self):
        import agent
        return agent.get_business_numbers()

    def remember(self, fact):
        import memory
        mem = memory.load_memory()
        if memory.add_fact(mem, fact):
            memory.save_memory(mem)
            return True
        return False


def set_ctx(ctx):
    global _ctx
    _ctx = ctx


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #
# Pure filler/greetings no legitimate task skill should ever claim. A matcher
# that grabs several of these is broken (e.g. `return "e" in text`) and would
# hijack normal conversation — the loader refuses it, same as a failed self_test.
_GREEDY_PROBES = ("", "hello there", "okay thanks", "never mind", "how are you")


def _too_greedy(matches):
    """True if matches() claims neutral phrases — a runaway matcher that would
    steal every turn. Pure so it's testable."""
    try:
        hits = sum(1 for p in _GREEDY_PROBES if matches(p))
    except Exception:
        return False    # a matcher that throws is handled at runtime, not here
    return hits >= 3


def _shadows_builtin(name):
    """True when a skill has taken the name of one of Neo's own tools."""
    try:
        import agent
        return str(name).strip().lower() in {
            getattr(t, "__name__", "").lower() for t in agent.TOOLS}
    except Exception:
        return False          # can't tell -> let it load, as before


def load_all():
    """(Re)scan skills/ and load everything that passes the contract + self_test.
    Returns (loaded_names, skipped: [(file, reason)])."""
    global _registry
    _registry = []
    skipped = []
    if not os.path.isdir(SKILLS_DIR):
        os.makedirs(SKILLS_DIR, exist_ok=True)
        return [], []
    for fn in sorted(os.listdir(SKILLS_DIR)):
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        path = os.path.join(SKILLS_DIR, fn)
        try:
            spec = importlib.util.spec_from_file_location(f"neo_skill_{fn[:-3]}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            missing = [a for a in _REQUIRED if not hasattr(mod, a)]
            if missing:
                skipped.append((fn, f"missing {', '.join(missing)}"))
                continue
            if mod.self_test() is not True:
                skipped.append((fn, "self_test returned falsy"))
                continue
            if _too_greedy(mod.matches):
                skipped.append((fn, "matches() is too greedy (claims neutral phrases)"))
                continue
            if _shadows_builtin(mod.NAME):
                # A skill named after a tool Neo already has is dead weight at
                # best and a coin flip at worst: the model sometimes calls the
                # real tool and sometimes use_skill(), and only one of them is
                # the code that actually works. This is not hypothetical — a
                # Claude job asked to "fix the highlight skill" wrote
                # skills/highlight_on_screen.py alongside the built-in
                # highlight_on_screen, reported success, and nothing changed.
                skipped.append((fn, f"NAME {mod.NAME!r} is already a built-in "
                                    "tool — rename it or delete it"))
                continue
            _registry.append(mod)
        except Exception as e:
            skipped.append((fn, f"{type(e).__name__}: {e}"))
            if os.getenv("NEO_DEBUG") == "1":
                traceback.print_exc()
    for fn, why in skipped:
        print(f"[skills] skipped {fn}: {why}")
    return [m.NAME for m in _registry], skipped


def loaded():
    return list(_registry)


def summary():
    """Spoken list of current skills."""
    if not _registry:
        return ("No custom skills yet. Say 'teach yourself to' do something "
                "and I'll build one.")
    names = [m.NAME.replace("_", " ") for m in _registry]
    return (f"I've got {len(names)} custom skill{'s' if len(names) != 1 else ''}: "
            + ", ".join(names) + ".")


# --------------------------------------------------------------------------- #
# matching + running
# --------------------------------------------------------------------------- #
def find(text):
    """First skill whose matches() claims this utterance, else None."""
    for mod in _registry:
        try:
            if mod.matches(text):
                return mod
        except Exception as e:
            print(f"[skills] {getattr(mod, 'NAME', '?')}.matches crashed: {e}")
    return None


_last_error = None    # (skill_name, error_text) — fuel for self-repair


def last_error():
    return _last_error


def run(mod, text):
    """Run a skill's handler. Never raises; crashes are remembered so the
    skill can be REPAIRED (Voyager-style: feed the error back, don't discard)."""
    global _last_error
    try:
        out = mod.handle(text, _ctx)
        return (out or "").strip() or "Done, but that skill had nothing to say."
    except Exception as e:
        name = getattr(mod, "NAME", "?")
        _last_error = (name, f"{type(e).__name__}: {e} (utterance: {text!r})")
        print(f"[skills] {name}.handle crashed: {e}")
        if os.getenv("NEO_DEBUG") == "1":
            traceback.print_exc()
        return (f"My {name.replace('_', ' ')} skill just glitched. "
                "Say 'fix that skill' and I'll have Claude repair it.")


# --------------------------------------------------------------------------- #
# growing: turn a wish into a Claude Code brief
# --------------------------------------------------------------------------- #
_TEACH_RES = (
    re.compile(r"\bteach yourself (?:how )?to\s+(.+)$", re.I),
    re.compile(r"\b(?:build|make|add|create|write) (?:yourself )?a (?:new )?skill "
               r"(?:to|for|that(?: can)?|which)\s+(.+)$", re.I),
    re.compile(r"\bgive yourself the ability to\s+(.+)$", re.I),
    # "learn to X" only as a COMMAND (start of utterance, optional address),
    # so "I want to learn how to code someday" stays a conversation.
    re.compile(r"^\s*(?:hey |ok |okay )?(?:neo[, ]+)?(?:can you |could you |please )?"
               r"learn (?:how )?to\s+(.+)$", re.I),
)


def parse_teach(text):
    """'teach yourself to track my gym streak' -> 'track my gym streak' | None."""
    clean = text.strip().rstrip(".!?")
    for rx in _TEACH_RES:
        m = rx.search(clean)
        if m:
            want = m.group(1).strip()
            if want:
                return want
    return None


_REPAIR_RES = (
    re.compile(r"\b(?:fix|repair|debug) (?:that|the last|your last) skill\b", re.I),
    re.compile(r"\b(?:fix|repair|debug) (?:your|the|my)?\s*([a-z0-9_ ]+?)\s+skill\b", re.I),
)


def parse_repair(text):
    """
    "fix that skill" -> name from the last crash (or "" if none recorded).
    "repair your headlines skill" -> "headlines".
    None when this isn't a repair request.
    """
    m = _REPAIR_RES[0].search(text)
    if m:
        return _last_error[0] if _last_error else ""
    m = _REPAIR_RES[1].search(text)
    if m:
        return m.group(1).strip().replace(" ", "_")
    return None


def author_repair_task(name, error=None):
    """Claude Code brief to repair a broken skill in place."""
    err = f"\nThe most recent runtime failure was: {error}" if error else ""
    return f"""Repair a broken SKILL for Neo: skills/{name}.py — it exists, keep its NAME and purpose, fix the defect.{err}

Read skills.py first for the contract (NAME, DESCRIPTION, matches, handle, self_test) and the ctx capabilities. Diagnose the bug, fix it, and make the skill more defensive: handle() must catch its own failures and return honest spoken text, never raise. STRENGTHEN self_test() to cover the exact failure that occurred (with any network/ctx parts stubbed).

HOUSE RULES: stdlib + ctx only, no new pip deps, keep it under ~150 lines, don't touch any other file.

VERIFY before finishing: run  python -c "import importlib.util; s=importlib.util.spec_from_file_location('t','skills/{name}.py'); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); assert m.self_test() is True; print('SKILL OK')"  and make it pass, then run python test_neo.py to confirm nothing else broke."""


def author_improve_task(name, change):
    """Claude Code brief: rebuild an existing skill to their spec."""
    return f"""Modify an existing SKILL for Neo: skills/{name}.py — it works, but the user wants it to behave differently. Their request, treat it as the spec: {change}

Read skills.py first for the contract (NAME, DESCRIPTION, matches, handle, self_test) and ctx capabilities, then read the current skills/{name}.py. Apply the change while keeping NAME and the contract intact. Update DESCRIPTION if behavior changed. Extend self_test() to cover the new behavior (network/GUI parts stubbed). handle() must never raise.

HOUSE RULES: stdlib + ctx only, no new pip deps, keep it under ~180 lines, don't touch any other file.

VERIFY before finishing: run  python -c "import importlib.util; s=importlib.util.spec_from_file_location('t','skills/{name}.py'); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); assert m.self_test() is True; print('SKILL OK')"  and make it pass, then run python test_neo.py to confirm nothing else broke."""


def wants_skill_list(text):
    low = re.sub(r"[^a-z ]", " ", text.lower().replace("'", ""))
    return any(p in low for p in ("what skills", "list your skills", "your skills",
                                  "what can you do now", "what have you learned"))


def author_task(want):
    """The engineering brief handed to Claude Code. Precise on purpose —
    the contract below is what the loader enforces."""
    existing = ", ".join(m.NAME for m in _registry) or "(none yet)"
    return f"""Build a new SKILL for Neo (this repo's voice assistant). the user asked Neo to learn to: {want}

Create exactly one file: skills/<short_snake_case_name>.py — pick a name not in: {existing}

THE CONTRACT (the loader in skills.py enforces this; read skills.py first):
- NAME = "<same snake_case name>"
- DESCRIPTION = one line: what it does + 2-3 example trigger phrases.
- matches(text: str) -> bool — pure and fast. Match the natural spoken phrasings of this request (people talk loosely; cover variants), but NEVER match unrelated speech. Use re on a lowercased copy.
- handle(text: str, ctx) -> str — do the actual work and return ONE OR TWO short sentences Neo will SPEAK aloud (no markdown, no URLs, no emoji). Parse any parameters from the utterance itself. Persist state as JSON next to the skill file (skills/<name>_data.json) if the skill needs memory.
- self_test() -> bool — offline, fast, no network/GUI/audio: exercise matches() positives AND negatives, plus core handle() logic with any network/ctx parts stubbed or skipped. Return True only if healthy.

ctx capabilities (documented in skills.py): ctx.search(q,n) live web search; ctx.fetch(url,chars) page text; ctx.open_app/ctx.open_url; ctx.show(spec) for canvas visuals (spec kinds in canvas.py); ctx.say(text) speak through Neo NOW; ctx.later(seconds, text) schedule Neo to speak after a delay (timers/reminders); ctx.notify(title, detail, urgency) raise an on-screen card; ctx.business() live the project numbers; ctx.remember(fact); ctx.client + ctx.model for a direct Gemini call ONLY if reasoning over fetched content is essential (each call costs their free-tier quota — most skills need zero).

NEO HAS HANDS. Use these instead of writing your own AppleScript or shelling out — they read the screen with the OS's own OCR, they refuse to act when a target is ambiguous, and they check afterwards that what they did actually happened:
- ctx.focus(app) -> (ok, why). ALWAYS call this before clicking in an app. macOS gives the first click on an unfocused window to the window manager, so without it your first action is silently swallowed and everything after it goes to the wrong place.
- ctx.click(what, near=..., intent=..., app=...) -> (ok, why). Finds visible TEXT by name and clicks it. `intent` is a plain sentence saying which one you mean ("the site navigation link at the top") and is used when several things on screen share a name — without it an ambiguous target is REFUSED rather than guessed, which is deliberate. `near` picks the match closest to some other text.
- ctx.click_field(label, where="below"|"right", app=...) -> (ok, why). For an EMPTY input box, which has no text and so cannot be found by name. Finds the label and clicks the space next to it, then types two characters and re-reads the screen to prove it really is a text box.
- ctx.type(text) / ctx.press(key) -> (ok, why). Typing goes wherever focus is, so click first and check the (ok, why) it gave you.
- ctx.screen_text() -> str, everything readable on screen right now.
- ctx.wait_for(text, timeout=20) -> bool. A page load takes an unknown time; poll for the text that means it arrived instead of sleeping a guess.
Every one of these returns (ok, why) or a value — CHECK IT. `ok` means the click was sent at a verified location; it does NOT mean the page did what you wanted, so confirm the outcome with wait_for or screen_text before telling the user it worked. Each screen read costs about 1.3 seconds, so read once and reuse rather than calling screen_text in a loop.

HOUSE RULES: Python stdlib + ctx only, NO new pip dependencies. Never block more than ~20 seconds. Catch your own exceptions and return honest spoken failure text. handle() must never raise. Keep the file under ~150 lines. Match the codebase's comment style — plain, short, no corporate filler.

STYLE: prefer simple, boring code. No multi-line string concatenation for regexes (a real past build shipped a SyntaxError from a missing '+' — one bad character wastes the whole job); build long patterns in a single string or with explicit intermediate variables. Only self_test assertions for behavior the code actually implements.

VERIFY — this is a GATE, not a suggestion: run  python -c "import importlib.util; s=importlib.util.spec_from_file_location('t','skills/<name>.py'); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); assert m.self_test() is True; print('SKILL OK')"  — if it errors or fails, FIX THE FILE AND RUN IT AGAIN, repeating until it prints SKILL OK. Do not end the job without having seen SKILL OK in your own command output. Then run python test_neo.py to confirm nothing else broke. Do NOT modify any existing file."""


if __name__ == "__main__":
    names, skips = load_all()
    print(f"loaded: {names or 'none'}")
    for fn, why in skips:
        print(f"skipped {fn}: {why}")
