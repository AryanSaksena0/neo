"""test_voice_feel.py — the things the user actually hears.

Four complaints, all of them about how Neo SOUNDS rather than what it knows:
the voice flipping back to the old one, one filler phrase over and over,
clipped words that come out of a speech engine as a mumble, and a stale
conversation being picked back up.

Every check runs the code. Where a fixture is needed it is built here, and
nothing touches convo.json or the voice cache on disk — an earlier version of
this file called convo.save() against the real state file and wiped a day of
conversation, which is exactly the kind of thing a test must never do.

Run: python3 test_voice_feel.py
"""
# Suites live in tests/ but RUN from the repo root, so that the modules
# under test import and open("neo.py") still resolves. This makes the
# import work either way, so a suite can also be run directly.
import os as _bootstrap_os, sys as _bootstrap_sys
_bootstrap_sys.path.insert(0, _bootstrap_os.path.dirname(
    _bootstrap_os.path.dirname(_bootstrap_os.path.abspath(__file__))))

import os
import sys
import tempfile
import time

sys.path.insert(0, ".")
import banter
import convo
import memory

FAILED = []


def check(name, cond):
    print(("PASS - " if cond else "FAIL - ") + name)
    if not cond:
        FAILED.append(name)


def skip(name, why=""):
    print(f"SKIP - {name}" + (f" ({why})" if why else ""))


# =========================================================================== #
# 1. Fillers. The right voice, and more than one of them.
# =========================================================================== #
# This is the bug, exactly: only a few lines per bucket are ever synthesised
# in Neo's real voice, and pick() used to choose from the whole list — so most
# of the time it fell through to the local engine. Both complaints, one cause.
CACHED = {"On it.", "One moment.", "Let me check."}

for kind in ("lookup", "quick", "hands", "medium", "long", "present"):
    picks = {banter.pick(kind, available=CACHED) for _ in range(60)}
    check(f"{kind}: every filler is one that exists in the real voice "
          f"({sorted(picks)})", picks <= CACHED)

check("with nothing cached at all it still returns something rather than "
      "leaving Neo silent mid-task",
      all(banter.pick(k, available=set()) for k in banter.ACKS))

# A bucket with one cached line is a catchphrase, not a personality. "lookup"
# was exactly that: "Let me check." in front of every single web search.
thin = {"Let me check.", "On it.", "One moment.", "Give me a second.",
        "Checking that now."}
picks = {banter.pick("lookup", available=thin) for _ in range(80)}
check(f"a thin bucket borrows from a compatible one instead of repeating "
      f"itself ({len(picks)} distinct)", len(picks) >= banter.MIN_VARIETY)
check("...and everything it borrowed is still in the real voice", picks <= thin)

check("a bucket that is already varied does not borrow",
      {banter.pick("quick", available=set(banter.ACKS["quick"]))
       for _ in range(60)} <= set(banter.ACKS["quick"]))

check("every bucket can reach 'quick', so there is always somewhere to borrow "
      "from", all(b == "quick" or "quick" in banter.NEIGHBOUR.get(b, ())
                  or any("quick" in banter.NEIGHBOUR.get(n, ())
                         for n in banter.NEIGHBOUR.get(b, ()))
                  for b in banter.ACKS))

last = None
same = 0
for _ in range(200):
    p = banter.pick("quick")
    same += (p == last)
    last = p
check(f"a phrase never repeats back to back ({same} repeats in 200)", same == 0)


# =========================================================================== #
# 2. Spoken English. Nothing a speech engine will slur.
# =========================================================================== #
# "One sec." comes out between "seck" and "sek"; "Let me check" gets run
# together into "lemme check". the user heard both and called it out.
BANNED = ["lemme", "gonna", "wanna", "gotta", "kinda", "sorta", "dunno",
          "cuz", "yep", "nope", "'em", "'cause", "info", "pics", "vs.",
          "e.g.", "i.e.", "etc.", "asap", "fyi"]
CLIPPED = [" sec.", " sec ", " secs", "min.", "'ll be a sec"]

problems = []
for bucket, lines in banter.ACKS.items():
    for line in lines:
        low = line.lower()
        for bad in BANNED:
            if bad in low:
                problems.append(f"{bucket}: {line!r} contains {bad!r}")
        for bad in CLIPPED:
            if bad in low:
                problems.append(f"{bucket}: {line!r} contains {bad!r}")
check(f"no filler contains a clipped or slurred word ({problems[:3]})",
      not problems)

check("no filler is written with digits, which a speech engine reads out in "
      "its own way",
      not [l for lines in banter.ACKS.values() for l in lines
           if any(c.isdigit() for c in l)])

check("every filler ends like a sentence, so it does not run into the answer",
      all(l.rstrip().endswith((".", "!", "?"))
          for lines in banter.ACKS.values() for l in lines if l.strip()))

check("every bucket has enough lines to be worth rotating",
      all(len(v) >= banter.MIN_VARIETY for v in banter.ACKS.values()))

# The instruction has to reach the model too, not just the filler table.
P = memory.PERSONALITY
for word in ("lemme", "gonna", "kinda", "sec"):
    check(f"the prompt names {word!r} as banned", word in P)
check("...and says plainly that ORDINARY contractions are still correct — "
      "banning those outright is what would make him sound like a robot",
      "Ordinary contractions are fine" in P)


# =========================================================================== #
# 3. The register. Jarvis, and jokes only when they are welcome.
# =========================================================================== #
for phrase, why in [
    ("REGISTER IS JARVIS", "the register is named outright"),
    ("HAVE A POINT OF VIEW", "he is told to pick a side rather than hedge"),
    ("Notice things", "noticing is what makes it feel intelligent"),
    ("Competence is the personality", "charisma is being ahead, not quips"),
    ("Never open with a quip", "the answer comes first"),
    ("NEVER JOKE WHEN THEY ARE STRESSED", "the timing rule the user asked for"),
    ("Never joke about them", "the target is the situation, never them"),
    ("never do a bit twice", "no repeating a line he has heard"),
]:
    check(f"the prompt: {why}", phrase in P)

check("the stressed rule names real signals, so it is detectable rather than "
      "aspirational",
      all(s in P for s in ("short sentences", "repeating themselves", "still broken")))
check("humour is capped rather than encouraged everywhere",
      "Most turns should have none" in P)


# =========================================================================== #
# 4. Conversation history. Warm, then gone.
# =========================================================================== #
NOW = 1_700_000_000.0
HOUR = 3600.0


def turn(role, text, ago):
    return {"role": role, "text": text, "at": NOW - ago * HOUR}


YESTERDAY = [turn("user", "fantasy football draft help", 9),
             turn("model", "here are some rankings", 9)]
JUST_NOW = [turn("user", "what is the weather", 0.05),
            turn("model", "cold", 0.05)]

kept = convo.fresh(YESTERDAY + JUST_NOW, now=NOW)
check("a conversation from nine hours ago is not carried into a new one — "
      "reseeding it is what makes Neo answer into a thread the user left last "
      "night", [t["text"] for t in kept] == ["what is the weather", "cold"])

check("a conversation from ten minutes ago IS carried, because that is the "
      "same conversation",
      len(convo.fresh(JUST_NOW, now=NOW)) == 2)

check("history still has to begin with a user turn, or Gemini rejects it",
      convo.fresh([turn("model", "orphan reply", 0.1)] + JUST_NOW,
                  now=NOW)[0]["role"] == "user")

check("a turn with no stamp is treated as old rather than kept forever",
      convo.fresh([{"role": "user", "text": "ancient"}], now=NOW) == [])

check("a corrupt stamp does not crash the load",
      convo.fresh([{"role": "user", "text": "x", "at": "not a number"}],
                  now=NOW) == [])

recorded = convo.record([], "hello", "hi there", now=NOW)
check("a new turn is stamped so it can be aged out later",
      all(t.get("at") == NOW for t in recorded))

check("the stamp survives the sanitiser — dropping it there would make every "
      "turn look ancient the moment it hit disk, and the conversation would "
      "reset on every restart",
      convo._clean(recorded)[0].get("at") == NOW)

check("the window is long enough to survive a restart and a lunch break",
      2 * HOUR <= convo.STALE_AFTER_S <= 8 * HOUR)


# =========================================================================== #
# 5. One voice. Anything spoken while a conversation is open goes through it.
# =========================================================================== #
import inspect
import neo

src = inspect.getsource(neo.Neo.say)
check("say() prefers the open conversation, so a card, a timer or a finished "
      "job speaks in the SAME voice as the answer before it",
      "session.speak" in src and "is_running" in src)

ack = inspect.getsource(neo.Neo._ack)
check("the filler is chosen from what is cached in the real voice, not from "
      "the whole list", "cached_phrases" in ack and "available=ready" in ack)

check("Speech can say which phrases exist in the real voice",
      callable(getattr(neo.Speech, "cached_phrases", None)))


class _FakeSpeech:
    """A Speech with a known cache, so cached_phrases is exercised for real."""
    cache = {"On it."}
    _ack_path = neo.Speech._ack_path
    cached_phrases = neo.Speech.cached_phrases


ready = _FakeSpeech().cached_phrases(["On it.", "definitely not cached zzqq"])
check("cached_phrases reports what is there and not what is not",
      ready == {"On it."})



# =========================================================================== #
# 6. Fewer interruptions. the user's words: "there needs to be less unprompted
#    cards, and reminders and all that, its pretty bad. because sometimes i am
#    in places where that can be distracting and harmful."
# =========================================================================== #
import situation

src_neo = open("neo.py").read()
check("the boot card is OFF by default — Neo restarts whenever a file changes, "
      "so it was not an occasional hello, it was several cards an hour saying "
      "nothing", 'os.getenv("NEO_BOOT") or "off"' in src_neo)
check("mid-job progress is logged, not carded — he asked to hear about a job "
      "when it FINISHES, not while it is still going",
      "self.claude.on_progress = lambda" in src_neo)

import inspect
import neo as _neo
deliver = inspect.getsource(_neo.Neo._deliver_insight)
check("every card is checked against 'is he presenting' before it is shown",
      "situation.presenting" in deliver)

# presenting() decides whether he gets interrupted at all, so its judgement
# has to be right in both directions.
import desk
_real = desk.frontmost_app
_orig_cache = dict(situation._PRESENT_CACHE)
try:
    def when(app):
        desk.frontmost_app = lambda: app
        situation._PRESENT_CACHE["at"] = 0.0
        return situation.presenting()

    for app in ("zoom.us", "Microsoft Teams", "Webex"):
        check(f"{app} in front counts as presenting", when(app))
    for app in ("Google Chrome", "Terminal", "Preview", "Finder", ""):
        check(f"{app or 'nothing'} in front does NOT — he works fullscreen most "
              "of the day, and treating that as presenting would silence Neo "
              "almost always, which is a different bug rather than a fix",
              not when(app))

    desk.frontmost_app = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    situation._PRESENT_CACHE["at"] = 0.0
    check("a broken frontmost check does not jam Neo silent forever",
          situation.presenting() is False)
finally:
    desk.frontmost_app = _real
    situation._PRESENT_CACHE.update(_orig_cache)

# It runs before every card, so it cannot be slow.
situation._PRESENT_CACHE["at"] = 0.0
situation.presenting()
t0 = time.time()
for _ in range(50):
    situation.presenting()
cached = time.time() - t0
check(f"the check is cached, because it runs before every card "
      f"({cached * 1000:.1f}ms for 50)", cached < 0.02)

check("the fullscreen test requires the window to start at the very top — a "
      "merely maximised window still leaves the menu bar",
      "kCGWindowLayer" in inspect.getsource(situation._fullscreen)
      and 'b.get("Y", -1) == 0' in inspect.getsource(situation._fullscreen))


# =========================================================================== #
# 7. The owner's own memory — an AUDIT, not a product test.
# =========================================================================== #
# This section used to assert the literal contents of one person's memory.json:
# how to pronounce their name, their email sign-off, their activities list,
# their internship, what they are driving at in college admissions. Two things
# wrong with that. It cannot pass on anybody else's Mac, so for every user but
# one it is a guaranteed red suite. And the expected phrases are themselves
# personal facts, sitting in a file that ships.
#
# So it is now opt-in and data-driven. Put the phrases you want audited in
# .memory-audit (gitignored, same idea as .personal-words):
#
#     keep: Best,\nYour Name        # must be in memory.json
#     gone: some old project        # must NOT be in memory.json
#
# then run:  NEO_MEMORY_AUDIT=1 .venv/bin/python test_voice_feel.py
import memory

_AUDIT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".memory-audit")
if os.getenv("NEO_MEMORY_AUDIT") != "1":
    skip("the owner's memory audit", "opt in with NEO_MEMORY_AUDIT=1")
elif not os.path.exists(_AUDIT):
    skip("the owner's memory audit", "no .memory-audit file")
else:
    facts = " ".join(
        f.get("text") if isinstance(f, dict) else str(f)
        for f in memory.load_memory().get("facts", []))
    for _ln in open(_AUDIT, encoding="utf-8"):
        _ln = _ln.split("#")[0].strip()
        if not _ln or ":" not in _ln:
            continue
        _kind, _phrase = _ln.split(":", 1)
        _phrase = _phrase.strip().encode().decode("unicode_escape")
        if _kind.strip().lower() == "keep":
            check(f"memory keeps: {_phrase[:40]!r}", _phrase in facts)
        elif _kind.strip().lower() == "gone":
            check(f"memory dropped: {_phrase[:40]!r}", _phrase not in facts)

P = memory.PERSONALITY
for word in ("lemme", "gonna", "kinda", "sec"):
    check(f"the prompt names {word!r} as banned", word in P)
check("...and says plainly that ORDINARY contractions are still correct — "
      "banning those outright is what would make him sound like a robot",
      "Ordinary contractions are fine" in P)


# =========================================================================== #
# 3. The register. Jarvis, and jokes only when they are welcome.
# =========================================================================== #
for phrase, why in [
    ("REGISTER IS JARVIS", "the register is named outright"),
    ("HAVE A POINT OF VIEW", "he is told to pick a side rather than hedge"),
    ("Notice things", "noticing is what makes it feel intelligent"),
    ("Competence is the personality", "charisma is being ahead, not quips"),
    ("Never open with a quip", "the answer comes first"),
    ("NEVER JOKE WHEN THEY ARE STRESSED", "the timing rule the user asked for"),
    ("Never joke about them", "the target is the situation, never them"),
    ("never do a bit twice", "no repeating a line he has heard"),
]:
    check(f"the prompt: {why}", phrase in P)

check("the stressed rule names real signals, so it is detectable rather than "
      "aspirational",
      all(s in P for s in ("short sentences", "repeating themselves", "still broken")))
check("humour is capped rather than encouraged everywhere",
      "Most turns should have none" in P)


# =========================================================================== #
# 4. Conversation history. Warm, then gone.
# =========================================================================== #
NOW = 1_700_000_000.0
HOUR = 3600.0


def turn(role, text, ago):
    return {"role": role, "text": text, "at": NOW - ago * HOUR}


YESTERDAY = [turn("user", "fantasy football draft help", 9),
             turn("model", "here are some rankings", 9)]
JUST_NOW = [turn("user", "what is the weather", 0.05),
            turn("model", "cold", 0.05)]

kept = convo.fresh(YESTERDAY + JUST_NOW, now=NOW)
check("a conversation from nine hours ago is not carried into a new one — "
      "reseeding it is what makes Neo answer into a thread the user left last "
      "night", [t["text"] for t in kept] == ["what is the weather", "cold"])

check("a conversation from ten minutes ago IS carried, because that is the "
      "same conversation",
      len(convo.fresh(JUST_NOW, now=NOW)) == 2)

check("history still has to begin with a user turn, or Gemini rejects it",
      convo.fresh([turn("model", "orphan reply", 0.1)] + JUST_NOW,
                  now=NOW)[0]["role"] == "user")

check("a turn with no stamp is treated as old rather than kept forever",
      convo.fresh([{"role": "user", "text": "ancient"}], now=NOW) == [])

check("a corrupt stamp does not crash the load",
      convo.fresh([{"role": "user", "text": "x", "at": "not a number"}],
                  now=NOW) == [])

recorded = convo.record([], "hello", "hi there", now=NOW)
check("a new turn is stamped so it can be aged out later",
      all(t.get("at") == NOW for t in recorded))

check("the stamp survives the sanitiser — dropping it there would make every "
      "turn look ancient the moment it hit disk, and the conversation would "
      "reset on every restart",
      convo._clean(recorded)[0].get("at") == NOW)

check("the window is long enough to survive a restart and a lunch break",
      2 * HOUR <= convo.STALE_AFTER_S <= 8 * HOUR)


# =========================================================================== #
# 5. One voice. Anything spoken while a conversation is open goes through it.
# =========================================================================== #
import inspect
import neo

src = inspect.getsource(neo.Neo.say)
check("say() prefers the open conversation, so a card, a timer or a finished "
      "job speaks in the SAME voice as the answer before it",
      "session.speak" in src and "is_running" in src)

ack = inspect.getsource(neo.Neo._ack)
check("the filler is chosen from what is cached in the real voice, not from "
      "the whole list", "cached_phrases" in ack and "available=ready" in ack)

check("Speech can say which phrases exist in the real voice",
      callable(getattr(neo.Speech, "cached_phrases", None)))


class _FakeSpeech:
    """A Speech with a known cache, so cached_phrases is exercised for real."""
    cache = {"On it."}
    _ack_path = neo.Speech._ack_path
    cached_phrases = neo.Speech.cached_phrases


ready = _FakeSpeech().cached_phrases(["On it.", "definitely not cached zzqq"])
check("cached_phrases reports what is there and not what is not",
      ready == {"On it."})



# =========================================================================== #
# 6. Fewer interruptions. the user's words: "there needs to be less unprompted
#    cards, and reminders and all that, its pretty bad. because sometimes i am
#    in places where that can be distracting and harmful."
# =========================================================================== #
import situation

src_neo = open("neo.py").read()
check("the boot card is OFF by default — Neo restarts whenever a file changes, "
      "so it was not an occasional hello, it was several cards an hour saying "
      "nothing", 'os.getenv("NEO_BOOT") or "off"' in src_neo)
check("mid-job progress is logged, not carded — he asked to hear about a job "
      "when it FINISHES, not while it is still going",
      "self.claude.on_progress = lambda" in src_neo)

import inspect
import neo as _neo
deliver = inspect.getsource(_neo.Neo._deliver_insight)
check("every card is checked against 'is he presenting' before it is shown",
      "situation.presenting" in deliver)

# presenting() decides whether he gets interrupted at all, so its judgement
# has to be right in both directions.
import desk
_real = desk.frontmost_app
_orig_cache = dict(situation._PRESENT_CACHE)
try:
    def when(app):
        desk.frontmost_app = lambda: app
        situation._PRESENT_CACHE["at"] = 0.0
        return situation.presenting()

    for app in ("zoom.us", "Microsoft Teams", "Webex"):
        check(f"{app} in front counts as presenting", when(app))
    for app in ("Google Chrome", "Terminal", "Preview", "Finder", ""):
        check(f"{app or 'nothing'} in front does NOT — he works fullscreen most "
              "of the day, and treating that as presenting would silence Neo "
              "almost always, which is a different bug rather than a fix",
              not when(app))

    desk.frontmost_app = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    situation._PRESENT_CACHE["at"] = 0.0
    check("a broken frontmost check does not jam Neo silent forever",
          situation.presenting() is False)
finally:
    desk.frontmost_app = _real
    situation._PRESENT_CACHE.update(_orig_cache)

# It runs before every card, so it cannot be slow.
situation._PRESENT_CACHE["at"] = 0.0
situation.presenting()
t0 = time.time()
for _ in range(50):
    situation.presenting()
cached = time.time() - t0
check(f"the check is cached, because it runs before every card "
      f"({cached * 1000:.1f}ms for 50)", cached < 0.02)

check("the fullscreen test requires the window to start at the very top — a "
      "merely maximised window still leaves the menu bar",
      "kCGWindowLayer" in inspect.getsource(situation._fullscreen)
      and 'b.get("Y", -1) == 0' in inspect.getsource(situation._fullscreen))


# =========================================================================== #
# 7b. (The owner's memory audit lives in section 7 above, opt-in.)
# =========================================================================== #
import memory

P = memory.PERSONALITY
for phrase, why in [
    ("NEVER DEAD-END THEM", "an alternative always beats an apology"),
    ("UNDER PRESSURE", "what changes when he is rushed"),
    ("STUDY MODE", "what helps when he is revising"),
    ("ONE line of why", "the answer shape he picked"),
]:
    check(f"the prompt: {why}", phrase in P)




# =========================================================================== #
# 8. The job card. the user's screenshot: a title cut mid-phrase across two lines,
#    three identical "Looking online" rows, an internal tool name on screen,
#    and a line struck through most of the card.
# =========================================================================== #
import claude_bridge as _cb
import commands as _cmd


def _tool_event(name, inp=None):
    return {"type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": name,
                                     "input": inp or {}}]}}


a = _cb.label_step(_tool_event("WebSearch", {"query": "2026 PPR rankings"}))
b = _cb.label_step(_tool_event("WebSearch", {"query": "six person league draft"}))
check(f"two searches read as two DIFFERENT steps ({a!r} vs {b!r}) — they were "
      "both the string 'Looking online', so a job that searched four times "
      "showed four identical rows and looked stuck rather than busy",
      a != b and "PPR" in a and "six person" in b)
check("a fetched page is named by its site, not its whole URL",
      _cb.label_step(_tool_event(
          "WebFetch", {"url": "https://www.fantasypros.com/nfl/rankings/x.php"}))
      == "Looking up fantasypros.com")
check("an unrecognised tool does NOT put its internal name on his screen — "
      "'ToolSearch' appeared on a real card and means nothing to him",
      _cb.label_step(_tool_event("ToolSearch")) == "Working on it"
      and _cb.label_step(_tool_event("SomeFutureTool")) == "Working on it")
check("the ones that were already good still are",
      _cb.label_step(_tool_event("Read", {"file_path": "/a/b/deck.py"}))
      == "Reading deck.py")
check("a step label never runs long enough to overflow its row",
      all(len(_cb.label_step(_tool_event("WebSearch", {"query": "x" * 300})) or "")
          < 60 for _ in "x"))

# The title. task_label only trimmed; this is a real condensation.
LONG = ("Um so the tomorrow at noon or today at noon basically is my fantasy "
        "football draft. It's a six person league and I'll just give you the "
        "breakdown. One quarterback, two running backs, two wide receivers, "
        "one tight end, one flex, a kicker and a defence.")
check("with no model available the title falls back to the old trim rather "
      "than failing", _cmd.short_title(LONG, client=None) == _cmd.task_label(LONG, 70))


class _Titles:
    def __init__(self, answer):
        class _M:
            def generate_content(inner, model, contents):
                return type("R", (), {"text": answer})()
        self.models = _M()


check("a good short title is used as given",
      _cmd.short_title(LONG, client=_Titles("Six person fantasy draft"))
      == "Six person fantasy draft")
check("quotes, trailing stops and ellipses are stripped",
      _cmd.short_title(LONG, client=_Titles('  "Six person fantasy draft."  '))
      == "Six person fantasy draft")
check("a model that ignored the word limit is discarded, not shown — a long "
      "title is the bug we are fixing",
      _cmd.short_title(LONG, client=_Titles(
          "A very long title that rambles on well past any sensible limit"))
      == _cmd.task_label(LONG, 70))
check("an empty answer falls back too",
      _cmd.short_title(LONG, client=_Titles("   ")) == _cmd.task_label(LONG, 70))


class _DeadTitle:
    class models:
        @staticmethod
        def generate_content(model, contents):
            raise RuntimeError("503")


check("a model outage costs the nice title, not the job",
      _cmd.short_title(LONG, client=_DeadTitle, log=lambda m: None)
      == _cmd.task_label(LONG, 70))

# The card markup.
hud_src = open("hud.py").read()
check("no strikethrough — it put a line through most of the card",
      "line-through" not in hud_src)
check("older steps collapse into one line instead of scrolling away",
      "earlier step" in hud_src)
check("the elapsed time rides on the chip, so a four-minute job never reads "
      "as stalled", "s.eta" in hud_src)
check("the title is clamped to two lines and breaks on a word",
      "line-clamp:2" in hud_src and "overflow-wrap:break-word" in hud_src)
check("the pulsing dot respects reduced motion",
      "prefers-reduced-motion" in hud_src)

neo_src = open("neo.py").read()
check("the same step is never listed twice in a row",
      'ch["steps"][-1] != step' in neo_src)
check("the card title is the request itself, with no 'Claude — ' prefix "
      "eating a third of the width", 'f"Claude — {task}"' not in neo_src)

check("the prompt forbids asking permission for something already done — he "
      "asked Neo to send a job to Claude, Neo sent it, then asked whether it "
      "should send it",
      "NEVER ASK PERMISSION FOR SOMETHING YOU HAVE ALREADY DONE" in memory.PERSONALITY)
check("...while still naming the things that DO get checked first",
      "putting an event on their calendar" in memory.PERSONALITY)




# =========================================================================== #
# 9. Delivering a finished job. All of this is from one real exchange:
#    the user asked for a fantasy football draft document, Claude wrote a good
#    184-line one with real player names, and then every single thing about
#    handing it over went wrong.
# =========================================================================== #
import claude_bridge as _cb2
import tempfile as _tf

_proj = _tf.mkdtemp(prefix="neoart")
_doc = os.path.join(_proj, "2026_ppr_draft_strategy.md")
open(_doc, "w").write("# strategy\nBijan, Gibbs, Chase\n")

REPORT = ("The strategy is worked out and written up. I wrote the full "
          "round-by-round plan for all six pick positions to "
          "`2026_ppr_draft_strategy.md` in the neo folder, with a cheat-sheet "
          "table and the hard rules at the bottom.")

found = _cb2.artifacts(REPORT, _proj)
check(f"a job's report names the file it wrote, and Neo finds it ({found})",
      found == [_doc])
check("a file that does not exist is never opened, however confidently the "
      "report talks about it",
      _cb2.artifacts("I wrote it all to report_that_never_existed.md", _proj) == [])
check("Neo's own plumbing is not mistaken for something the user wanted",
      _cb2.artifacts("I read CLAUDE.md and requirements.txt and package.json",
                     _proj) == [])
open(os.path.join(_proj, "CLAUDE.md"), "w").write("x")
check("...even when those files really are there",
      _cb2.artifacts("updated CLAUDE.md", _proj) == [])
check("a report with no file at all yields nothing",
      _cb2.artifacts("I looked into it and here is what I think.", _proj) == [])
check("no project folder means no guessing at paths",
      _cb2.artifacts(REPORT, "") == [])

line = _cb2.handoff_line(found)
check(f"what Neo SAYS is one sentence about opening it ({line!r}) — he asked "
      "for a document and got a summary read aloud instead, which is the "
      "opposite of delivering it",
      len(line) < 90 and "opened" in line.lower())
check("the filename is spoken as words, not as a filename with underscores "
      "and an extension",
      "_" not in line and ".md" not in line)
check("several files are counted rather than listed",
      "2 more" in _cb2.handoff_line([_doc, "/a/b.md", "/a/c.md"]))
check("no files means nothing to say", _cb2.handoff_line([]) == "")

import shutil as _sh
_sh.rmtree(_proj, ignore_errors=True)

src_n = open("neo.py").read()
check("a finished job OPENS what it made instead of narrating it",
      'hands.open_url("file://" + p)' in src_n)
check("...and the files are remembered, so \"show me the document\" later "
      "still works", "self.claude.last_artifacts = made" in src_n)
# SUPERSEDED, deliberately. This used to assert the opposite: that with no
# conversation open Neo stays silent, because the local path meant the local
# ENGINE — bm_fable, the "old Jarvis". That reason is gone: the default engine
# is now the cloud voice, so the local path speaks Charon too, and what the
# silence actually produced was a report the user asked for arriving as a card
# he had to tap "tell me" on. The invariant that survives is the VOICE one.
check("with no conversation open Neo still speaks the result",
      "no conversation open — speaking it" in src_n)
# One voice everywhere. Which engine that is depends on whether this install
# has a key: cloud when it does, Kokoro when it does not. Two voices in one
# install is the bug; either engine alone is fine.
_neo_m = __import__("neo")
check("...and it is the same voice on every path, key or no key",
      _neo_m._load_engine() == ("kokoro" if _neo_m.KEYLESS else "gemini"))

import agent as _ag
check("there is a tool for \"show me the document\"",
      any(t.__name__ == "open_document" for t in _ag.TOOLS))

# Barge-in. The single line that caused "two things talking at the same time".
import inspect as _in
press = _in.getsource(_ag and __import__("neo").Neo.on_press)
check("pressing the key cuts the local voice BEFORE the live branch returns — "
      "it used to return first, so in live mode (every normal day) the key "
      "never stopped local speech at all",
      press.index("self._interrupt.set()") < press.index("_start_live"))
check("...and stops a filler that is already mid-word, since that plays "
      "through a blocking wait", "sd.stop()" in press)




# =========================================================================== #
# 10. "Can you open the 2026 PPR draft strategy.md file and show it to me?"
#     Neo wrote AppleScript around the name exactly as he said it, macOS put an
#     error dialog on his screen, and the fallback found nothing because the
#     job's file list had been wiped by a restart. The document was right there.
# =========================================================================== #
import desk as _desk

check("a spoken filename finds the real file — spaces are not underscores, "
      "and he says the extension out loud",
      any(h["name"] == "2026_ppr_draft_strategy.md"
          for h in _desk.find_files("2026 PPR draft strategy.md", limit=3)))

hits = _desk.find_files("2026 PPR draft strategy", limit=4)
check(f"...and it comes FIRST. Spotlight offered neo.log ahead of it, because "
      f"the phrase appears inside the log, and sorting purely by recency let a "
      f"content match beat the file actually called that "
      f"({[h['name'] for h in hits[:2]]})",
      hits and hits[0]["name"] == "2026_ppr_draft_strategy.md")
check("an exact name match outranks a partial one",
      hits and hits[0]["named"] == 2)
check("a file that merely mentions the words is marked as NOT a name match, "
      "so open_document can refuse it",
      all(h["named"] == 0 for h in hits if h["name"] == "neo.log"))

for sep in ("_", "-", " "):
    q = "2026" + sep + "ppr" + sep + "draft" + sep + "strategy"
    check(f"{sep!r} as the separator still finds it",
          any(h["name"] == "2026_ppr_draft_strategy.md"
              for h in _desk.find_files(q, limit=3)))

# The file list has to outlive a restart.
_saved = _cb2.load_artifacts()
try:
    _cb2.save_artifacts([__file__])
    check("the files a job wrote are remembered on DISK — Neo hot-reloads on "
          "every source change, so a job that finished at 02:39 had its list "
          "wiped by a restart at 02:46, and Neo then said there was no "
          "document at all", _cb2.load_artifacts() == [__file__])
    _cb2.save_artifacts(["/definitely/not/here.md"])
    check("a remembered file that has since been deleted is dropped rather "
          "than opened", _cb2.load_artifacts() == [])
finally:
    _cb2.save_artifacts(_saved)

_agent_src = open("agent.py").read()
check("open_document exists and replaced the old artifact-only version",
      any(t.__name__ == "open_document" for t in _ag.TOOLS))
_od = dict((t.__name__, t.__doc__ or "") for t in _ag.TOOLS)["open_document"]
check("...its docstring forbids AppleScript for opening files, which is what "
      "actually happened", "NEVER use control_mac" in _od)
check("...and forbids look_at_screen, which is what happened the time before",
      "look_at_screen" in _od)
check("control_mac's own docstring warns it off files too, since that is the "
      "tool the model reached for",
      "NEVER use this to OPEN A FILE" in _agent_src)




# =========================================================================== #
# 11. "Look at my screen. It's not open." Three requests in a row for the same
#     document, and Neo said it was on screen when it was not, then asked him
#     which project Claude had worked on, then told him no such file existed —
#     while holding the path to it.
# =========================================================================== #
check("opening a file is VERIFIED, not assumed. `open` exits zero for a type "
      "with no handler, which is exactly what markdown does on this Mac, so "
      "Neo said 'it's on your screen now' while nothing was",
      callable(getattr(_desk, "opened_ok", None))
      and callable(getattr(_desk, "open_file", None)))
check("a file that is not there is reported, not opened",
      _desk.open_file("/definitely/not/a/file.md", log=lambda m: None)
      == (False, ""))
check("there is a guaranteed opener per file type for when the system handler "
      "does nothing",
      _desk._OPENERS.get(".md") and _desk._OPENERS.get(".pdf"))

# Built here rather than pointed at a file on one person's Desktop. The old
# version named a document that only existed on the machine this was written
# on, so on every other Mac these four checks silently skipped — a test that
# only runs for its author is not a test.
_DOCDIR = tempfile.mkdtemp()
DOC = os.path.join(_DOCDIR, "2027_draft_strategy.md")
with open(DOC, "w", encoding="utf-8") as _f:
    _f.write("# Draft strategy\n\nRound one targets, tiers, and the plan for "
             "the middle rounds.\n")
if os.path.isfile(DOC):
    check("a description that shares words with the document matches it",
          _desk.looks_like("draft strategy", DOC))
    check("an unrelated description does not",
          not _desk.looks_like("my chemistry homework", DOC))
    check("no description at all is not a mismatch",
          _desk.looks_like("", DOC) and _desk.looks_like("the document", DOC))
    check("filler words alone never decide it — 'the file you just made' is "
          "not a description of anything",
          _desk.looks_like("the file you just made", DOC))
else:
    skip("looks_like against the real document")

_od2 = dict((t.__name__, t.__doc__ or "") for t in _ag.TOOLS)["open_document"]
check("the tool is told never to ask him which project or what it is called — "
      "Neo asked him 'which project did Claude work on last' while the answer "
      "was in its own hands", "never ask them which project" in _od2)
check("...and still forbids AppleScript and look_at_screen for this",
      "NEVER use control_mac" in _od2 and "look_at_screen" in _od2)

_ag_src2 = open("agent.py").read()
check("a description that matches NOTHING falls back to the documents from "
      "this conversation instead of failing — 'the fantasy football one' has "
      "no words in common with 2026_ppr_draft_strategy.md, and Neo told him "
      "the file did not exist", "if not paths:\n        paths = remembered" in _ag_src2)
check("success is only ever claimed after the check passed",
      "VERIFIED, OR NOT CLAIMED" in _ag_src2)


print()
# =========================================================================== #
# N. Two keys means two allowances — use them.
# =========================================================================== #
# the user pays attention to this: "I have 2 keys, shouldn't there be some usage
# left?" There was. Both free-tier TTS quotas are scoped PerProjectPerModel —
# Google's own 429 says GenerateRequestsPerMinutePerProjectPerModel — so a key
# in a second project has its own allowance. The old loop broke out on anything
# that wasn't a per-DAY quota, so 58 of the 94 quota failures in neo.log (the
# per-minute ones) plus every transient empty response stopped dead with a
# fully stocked second key untouched.
import numpy as _np
import neo as _neo


class _FakeClient:
    """Answers with a canned failure, or a bit of PCM16 that looks like audio."""
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = 0
        self.models = self

    def generate_content(self, model=None, contents=None, config=None):
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        if self.outcome is None:
            class _R:  # a real, occasional empty turn
                candidates = [None]
            return _R()
        pcm = (_np.ones(2400, dtype=_np.int16) * 1000).tobytes()
        return type("R", (), {"candidates": [type("C", (), {
            "content": type("Ct", (), {"parts": [type("P", (), {
                "inline_data": type("I", (), {"data": pcm})()})()]})()})()]})()


def _speech_with(clients):
    sp = _neo.Speech.__new__(_neo.Speech)
    sp.gemini_client = None
    sp._tts_key_rest = {}
    sp._tts_dry = False
    sp._gemini_dead = False
    sp.engine = "gemini"
    sp._tts_clients = lambda now=None: clients
    return sp


_MINUTE_429 = Exception(
    "429 RESOURCE_EXHAUSTED. {'quotaId': "
    "'GenerateRequestsPerMinutePerProjectPerModel-FreeTier'}")
_DAY_429 = Exception(
    "429 RESOURCE_EXHAUSTED. {'quotaId': "
    "'GenerateRequestsPerDayPerProjectPerModel-FreeTier'}")

_k1, _k2 = _FakeClient(_MINUTE_429), _FakeClient("audio")
_sp = _speech_with([("key1", _k1), ("key2", _k2)])
_got = _sp._gemini_tts("hello")
check("keys: a per-minute limit on one key moves to the next (it is PER PROJECT)",
      _got is not None and _got.size > 0 and _k2.calls == 1)
check("keys: one key being rate-limited never stops the day",
      _sp._tts_dry is False)

_k1b, _k2b = _FakeClient(None), _FakeClient("audio")
_spb = _speech_with([("key1", _k1b), ("key2", _k2b)])
check("keys: an empty response from one key is retried on the next",
      _spb._gemini_tts("hello") is not None and _k2b.calls == 1)

_k1c, _k2c = _FakeClient(_DAY_429), _FakeClient(_DAY_429)
_spc = _speech_with([("key1", _k1c), ("key2", _k2c)])
check("keys: every key out for the day DOES stop for the day",
      _spc._gemini_tts("hello") is None and _spc._tts_dry is True
      and _k2c.calls == 1)

_k1d, _k2d = _FakeClient(_DAY_429), _FakeClient("audio")
_spd = _speech_with([("key1", _k1d), ("key2", _k2d)])
_spd._gemini_tts("hello")
check("keys: one key dry for the day does NOT stop the other",
      _spd._tts_dry is False)

# A failed key goes to the back of the queue, but is never dropped: with a
# single key, the back of the queue is still the only key there is.
_sp_rest = _neo.Speech.__new__(_neo.Speech)
_sp_rest._tts_key_rest = {}
_sp_rest._rest_tts_key("key1", daily=False, now=1000.0)
_sp_rest._rest_tts_key("key2", daily=True, now=1000.0)
check("keys: a per-minute failure rests briefly, a daily one rests long",
      60 < _sp_rest._tts_key_rest["key1"] - 1000.0 < 300
      and _sp_rest._tts_key_rest["key2"] - 1000.0 >= 3600)


if FAILED:
    print(f"{len(FAILED)} FAILED:")
    for n in FAILED:
        print("  - " + n)
    sys.exit(1)
print("Voice and feel clean.")
