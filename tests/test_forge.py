"""test_forge.py — the skill builder, driven through every way it goes wrong.

The whole point of forge.py is that a skill is not trusted until it has been
RUN and JUDGED, so these checks are mostly about rejection: the file that
never appeared, the one that will not import, the one whose matches() does not
match the sentence that asked for it, and — the one that matters most — the
one that returns a confident sentence and does no work at all.

Claude is injected as a plain function, so the entire loop runs here in
milliseconds with no CLI, no network and no minutes of waiting. The skills
directory is redirected at a temporary folder, so nothing here can touch a
real skill.

Run: python3 test_forge.py
"""
# Suites live in tests/ but RUN from the repo root, so that the modules
# under test import and open("neo.py") still resolves. This makes the
# import work either way, so a suite can also be run directly.
import os as _bootstrap_os, sys as _bootstrap_sys
_bootstrap_sys.path.insert(0, _bootstrap_os.path.dirname(
    _bootstrap_os.path.dirname(_bootstrap_os.path.abspath(__file__))))

import os
import shutil
import sys
import tempfile

sys.path.insert(0, ".")
import forge
import skills

FAILED = []


def check(name, cond):
    print(("PASS - " if cond else "FAIL - ") + name)
    if not cond:
        FAILED.append(name)


# --------------------------------------------------------------------------- #
# A temporary skills/ so a test can never disturb a real skill.
# --------------------------------------------------------------------------- #
TMP = tempfile.mkdtemp(prefix="neoforge")
REAL_DIR = skills.SKILLS_DIR
skills.SKILLS_DIR = TMP

GOOD = '''
NAME = "word_count"
DESCRIPTION = "Counts words in a phrase. 'how many words in ...'"
import re
def matches(text):
    return "how many words" in text.lower()
def handle(text, ctx):
    m = re.search(r"how many words (?:are )?in (.+)", text, re.I)
    if not m:
        return "Say it as: how many words in, then the phrase."
    n = len(m.group(1).strip().strip("?").split())
    return f"That's {n} words."
def self_test():
    return (matches("how many words in the quick brown fox")
            and not matches("hello there")
            and handle("how many words in a b c", None) == "That's 3 words.")
'''

# The failure that matters most: fluent, confident, and does nothing.
LIAR = '''
NAME = "word_count"
DESCRIPTION = "Counts words in a phrase. 'how many words in ...'"
def matches(text):
    return "how many words" in text.lower()
def handle(text, ctx):
    return "That's 5 words."
def self_test():
    return matches("how many words in x") and not matches("hello there")
'''

# These four all have a self_test that passes while the skill is broken —
# which is not a contrivance, it is the whole reason the trial exists. A
# self_test written by the same model that wrote the skill tests what the code
# does, and every one of these bugs lives in what it DOESN'T do.
def _skill(matches_body, handle_body):
    return ('NAME = "word_count"\nDESCRIPTION = "counts words"\n'
            f'def matches(text):\n    {matches_body}\n'
            f'def handle(text, ctx):\n    {handle_body}\n'
            'def self_test():\n    return not matches("hello there")\n')


NO_MATCH = _skill('return "counterfactual banana" in text.lower()',
                  'return "four"')
RAISES = _skill('return "how many words" in text.lower()',
                'raise RuntimeError("boom")')
BAD_TEST = GOOD.replace("return (matches(", "return False and (matches(")
SYNTAX = "NAME = 'x'\ndef matches(text)\n    return True\n"
# A matcher that claims everything, with a self_test that never checks a
# negative — so it passes its own test and would still hijack every turn.
GREEDY = ('NAME = "grabby"\n'
          'DESCRIPTION = "grabs everything"\n'
          'def matches(text):\n    return True\n'
          'def handle(text, ctx):\n    return "hi"\n'
          'def self_test():\n    return matches("anything") is True\n')
NOT_STR = _skill('return "how many words" in text.lower()', 'return 3')
TALKS = _skill('return "how many words" in text.lower()',
               'ctx.say("out loud"); ctx.later(3600, "later"); '
               'ctx.notify("t", "d"); return "That\'s 3 words."')


def write(name, body):
    with open(os.path.join(TMP, name), "w") as f:
        f.write(body)


def claude_writing(name, body, ok=True):
    """A fake Claude that drops one file and reports success."""
    def run(brief):
        write(name, body)
        return ok, "done"
    return run


def claude_doing_nothing(brief):
    return True, "I had a look and decided not to."


class Judge:
    """A stand-in for the judging model. Returns whatever it is told to."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.seen = []
        outer = self

        class _M:
            def generate_content(inner, model, contents):
                outer.seen.append(contents)
                a = outer.answers.pop(0) if outer.answers else '{"ok": false, "why": "no"}'
                return type("R", (), {"text": a})()
        self.models = _M()


def clean():
    for f in os.listdir(TMP):
        p = os.path.join(TMP, f)
        shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else os.remove(p)


try:
    # ======================================================================= #
    # 1. Finding what Claude wrote.
    # ======================================================================= #
    clean()
    check("nothing on disk means nothing found",
          forge.newest_of(set(), forge.skill_files()) is None)
    write("a.py", GOOD)
    check("the one new file is the one it wrote",
          forge.newest_of(set(), forge.skill_files()) == "a.py")
    before = forge.skill_files()
    write("b.py", GOOD)
    check("a second build finds the SECOND file, not the first",
          forge.newest_of(before, forge.skill_files()) == "b.py")
    # THE MOST IMPORTANT CHECK IN THIS FILE. It is here because the bug it
    # describes actually happened, on the very first real build:
    #
    # Claude failed in five seconds and wrote nothing. newest_of fell back to
    # "whichever skill file changed most recently" — which ranged over EVERY
    # EXISTING SKILL — and picked calendar_peek.py. Three attempts later the
    # build was given up on and the failure path renamed a working skill out
    # of existence. A builder that can delete the skills you already have is
    # worse than no builder.
    import time
    time.sleep(0.02)
    write("a.py", GOOD + "\n# touched\n")
    check("when Claude writes NOTHING, no file is claimed — not the most "
          "recently touched one, not anything. A build that wrote nothing "
          "must never be able to reach an existing skill",
          forge.newest_of(forge.skill_files(), forge.skill_files()) is None)

    clean()
    write("existing.py", GOOD)
    r = forge.forge("some thing", claude_doing_nothing,
                    client=Judge(), log=lambda m: None, attempts=2)
    check("...and after a build that wrote nothing fails, a skill that was "
          "already there is still there and still live",
          not r["ok"] and os.path.exists(os.path.join(TMP, "existing.py"))
          and not os.path.exists(os.path.join(TMP, "_failed_existing.py")))

    check("quarantine flatly refuses a file this build did not create",
          forge.quarantine("existing.py", created_here=False,
                           log=lambda m: None) is False
          and os.path.exists(os.path.join(TMP, "existing.py")))

    # ======================================================================= #
    # 2. Loading. Every reason a file is not usable.
    # ======================================================================= #
    clean()
    write("good.py", GOOD)
    mod, problem = forge.load_one("good.py")
    check("a healthy skill loads", mod is not None and problem is None)

    for name, body, expect in [
        ("syntax.py", SYNTAX, "SyntaxError"),
        ("badtest.py", BAD_TEST, "self_test"),
        ("greedy.py", GREEDY, "neutral phrases"),
    ]:
        write(name, body)
        mod, problem = forge.load_one(name)
        check(f"{name} is rejected, and the reason says why ({problem!r})",
              mod is None and expect in (problem or ""))

    write("partial.py", "NAME='x'\nDESCRIPTION='y'\n")
    mod, problem = forge.load_one("partial.py")
    check("a file missing half the contract names what is missing",
          mod is None and "matches" in problem and "handle" in problem)
    check("a file that was never written is reported, not crashed on",
          forge.load_one("ghost.py")[0] is None)

    # ======================================================================= #
    # 3. The trial. This is the part that did not exist before.
    # ======================================================================= #
    clean()
    write("good.py", GOOD)
    mod, _ = forge.load_one("good.py")
    run = forge.trial(mod, "how many words in the quick brown fox")
    check("a working skill runs and returns its answer",
          run["matched"] and run["output"] == "That's 4 words." and not run["error"])
    check("and the trial times it, because a spoken answer has a deadline",
          run["seconds"] >= 0)

    write("nomatch.py", NO_MATCH)
    mod, _ = forge.load_one("nomatch.py")
    run = forge.trial(mod, "how many words in the quick brown fox")
    check("a skill whose matches() does not match the sentence that ASKED for "
          "it is caught here — Neo would never once reach it, and self_test "
          "would never notice",
          not run["matched"] and "never reach it" in run["error"])

    write("raises.py", RAISES)
    mod, _ = forge.load_one("raises.py")
    run = forge.trial(mod, "how many words in a b c")
    check("handle() blowing up is evidence, not a crashed build",
          "RuntimeError" in run["error"] and "boom" in run["error"])

    write("notstr.py", NOT_STR)
    mod, _ = forge.load_one("notstr.py")
    run = forge.trial(mod, "how many words in a b c")
    check("handle() returning a non-string is caught — Neo SPEAKS that value",
          "int" in run["error"])

    # A trial must not speak, schedule, or notify.
    write("talks.py", TALKS)
    mod, _ = forge.load_one("talks.py")
    run = forge.trial(mod, "how many words in a b c")
    check("a trial CAPTURES what the skill would say instead of saying it — "
          "a build must not talk over him",
          run["spoken"] == ["out loud"] and run["output"] == "That's 3 words.")
    check("...and captures timers instead of leaving one to fire in an hour",
          run["scheduled"] == [(3600, "later")])

    # ======================================================================= #
    # 4. The judge.
    # ======================================================================= #
    v = forge.judge("x", GOOD, {"error": "handle() raised"}, client=None)
    check("a trial that already failed needs no model to be judged",
          not v["ok"] and "raised" in v["why"])

    v = forge.judge("x", GOOD, {"output": "fine", "seconds": 0.1}, client=None)
    check("WITH NO JUDGE AVAILABLE THE ANSWER IS NO. An unjudged build passing "
          "is exactly the state this module exists to end",
          not v["ok"])

    j = Judge('{"ok": true, "why": "counts them properly"}')
    v = forge.judge("count words", GOOD, {"output": "That's 6 words.",
                                          "seconds": 0.1}, client=j)
    check("a good skill is passed", v["ok"])
    check("the judge is shown the SOURCE, not just the answer — a hardcoded "
          "reply is invisible from the output and obvious from the code",
          "def handle" in j.seen[0])
    check("...and the sentence the user actually said", "count words" in j.seen[0])
    check("...and is told the file is data, not instructions to it",
          "DATA" in j.seen[0])

    j = Judge('```json\n{"ok": false, "why": "the count is hardcoded"}\n```')
    v = forge.judge("count words", LIAR, {"output": "That's 5 words.",
                                          "seconds": 0.1}, client=j)
    check("a fenced verdict is still read", not v["ok"] and "hardcoded" in v["why"])

    for junk in ("I think it's fine", "", "{}", '{"why": "no ok field"}'):
        v = forge.judge("x", GOOD, {"output": "y", "seconds": 0.1},
                        client=Judge(junk))
        check(f"an unreadable verdict ({junk!r:22}) is NOT treated as a pass",
              not v["ok"])

    class DeadJudge:
        class models:
            @staticmethod
            def generate_content(model, contents):
                raise RuntimeError("503")

    v = forge.judge("x", GOOD, {"output": "y", "seconds": 0.1},
                    client=DeadJudge, log=lambda m: None)
    check("a judge that is down fails the build rather than waving it through",
          not v["ok"])

    # ======================================================================= #
    # 5. The whole loop.
    # ======================================================================= #
    clean()
    want = "how many words in a phrase"
    r = forge.forge(want, claude_writing("wc.py", GOOD),
                    client=Judge('{"ok": true, "why": "does the job"}'),
                    log=lambda m: None)
    check("a good build passes on the first attempt",
          r["ok"] and r["attempts"] == 1 and r["file"] == "wc.py")
    check("and the answer it actually gave is carried back, so Neo can prove "
          "it works rather than assert it",
          "words" in r["spoken"])
    check("a passing skill is LEFT IN PLACE so the loader picks it up",
          os.path.exists(os.path.join(TMP, "wc.py")))

    clean()
    tries = {"n": 0}

    def improving(brief):
        tries["n"] += 1
        write("wc.py", LIAR if tries["n"] == 1 else GOOD)
        return True, "done"

    r = forge.forge(want, improving,
                    client=Judge('{"ok": false, "why": "the count is hardcoded"}',
                                 '{"ok": true, "why": "it counts them now"}'),
                    log=lambda m: None)
    check("a skill that only PRETENDS to work is rejected and rebuilt, and "
          "the second attempt passes",
          r["ok"] and r["attempts"] == 2)

    clean()
    r = forge.forge(want, claude_writing("wc.py", LIAR),
                    client=Judge(*['{"ok": false, "why": "hardcoded"}'] * 5),
                    log=lambda m: None, attempts=3)
    check("a skill that never works is given up on after the attempt budget",
          not r["ok"] and r["attempts"] == 3)
    check("AND IT IS NOT LEFT LIVE. A rejected skill that passes its own "
          "self_test would otherwise sit there claiming every matching "
          "sentence from now on",
          not os.path.exists(os.path.join(TMP, "wc.py"))
          and os.path.exists(os.path.join(TMP, "_failed_wc.py")))
    check("the loader ignores a parked file", "_failed_wc.py".startswith("_"))

    clean()
    r = forge.forge(want, claude_doing_nothing, client=Judge(),
                    log=lambda m: None, attempts=2)
    check("Claude finishing without writing anything is reported plainly",
          not r["ok"] and "no skill file" in r["why"])

    # A signed-out Claude fails in five seconds and, from in here, looks
    # exactly like a task it could not do. The first real run burned three
    # attempts on it and told the user the skill couldn't be built — true, and
    # completely unhelpful.
    clean()
    signed_out = lambda brief: (
        False, "Failed to authenticate. API Error: 401 OAuth access token has "
               "expired. Re-authenticate to continue.")
    r = forge.forge(want, signed_out, client=Judge(), log=lambda m: None,
                    attempts=3)
    check("a signed-out Claude stops the loop at ONCE instead of retrying",
          r["attempts"] == 1 and r["why"] == "signed_out")
    check("...and Neo says what to actually do about it",
          "signed out" in forge.spoken_result(want, r)
          and "run claude" in forge.spoken_result(want, r))

    import claude_bridge as _cb
    check("a real build log is recognised as a sign-in failure",
          _cb.auth_problem("Failed to authenticate. API Error: 401 OAuth "
                           "access token has expired."))
    for benign in ("I built the skill and it works", "the file had 401 lines",
                   "", "wrote skills/thing.py and self_test passed"):
        check(f"...and ordinary output is not ({benign[:32]!r})",
              not _cb.auth_problem(benign))

    clean()
    r = forge.forge(want, claude_writing("wc.py", SYNTAX),
                    client=Judge(), log=lambda m: None, attempts=1)
    check("a file that will not even import fails the build",
          not r["ok"] and "SyntaxError" in r["why"])

    clean()
    seen_briefs = []

    def capture(brief):
        seen_briefs.append(brief)
        write("wc.py", LIAR)
        return True, "done"

    forge.forge(want, capture,
                client=Judge(*['{"ok": false, "why": "it hardcodes the number"}'] * 4),
                log=lambda m: None, attempts=3)
    check("the first brief is the normal authoring brief",
          "Build a new SKILL" in seen_briefs[0])
    check("the SECOND brief carries the evidence — what it said when run, and "
          "why that was rejected — instead of asking again from scratch",
          "hardcodes the number" in seen_briefs[1]
          and "That's 5 words." in seen_briefs[1])
    check("...and tells it to EDIT that file rather than add another skill",
          "do not start a new one" in seen_briefs[1])
    check("...and forbids the obvious cheat of weakening the test",
          "weaker" in seen_briefs[1])

    clean()
    stop = {"now": False}

    def slow(brief):
        stop["now"] = True
        write("wc.py", LIAR)
        return True, "done"

    r = forge.forge(want, slow, client=Judge(), log=lambda m: None,
                    should_stop=lambda: stop["now"], attempts=3)
    check("a build can be cancelled mid-loop", r["why"] == "cancelled")

    # ======================================================================= #
    # 6. What he hears.
    # ======================================================================= #
    said = forge.spoken_result(want, {"ok": True, "spoken": "That's 6 words.",
                                      "attempts": 1})
    check(f"success is short and proves itself ({said!r})",
          "Learned it" in said and "6 words" in said)
    said = forge.spoken_result(want, {"ok": False, "attempts": 3,
                                      "why": "it hardcodes the number"})
    check(f"failure says how many tries and what was wrong ({said!r})",
          "3 tries" in said and "hardcodes" in said)
    check("...and says plainly that nothing was switched on",
          "nothing's changed" in said)
    check("a cancelled build says so and nothing more",
          forge.spoken_result(want, {"ok": False, "why": "cancelled"})
          == "Stopped that build.")
    for r in ({"ok": True}, {"ok": False}, {}, {"ok": False, "attempts": 1}):
        check(f"spoken_result survives a sparse result {r}",
              isinstance(forge.spoken_result(want, r), str))

finally:
    skills.SKILLS_DIR = REAL_DIR
    shutil.rmtree(TMP, ignore_errors=True)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED:")
    for n in FAILED:
        print("  - " + n)
    sys.exit(1)
print("Forge clean.")
