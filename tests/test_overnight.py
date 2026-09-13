"""test_overnight.py — the run the user asked for, checked one fault at a time.

His list: the hands are broken, the Claude fallback is poor, Neo cannot report
its own bugs, it is wrong a lot, it is not conversational, and it fails
silently. Plus the one that started it — Neo read a JSON tool call out loud.

Every check here comes from a specific line in a real transcript, quoted where
it helps. Nothing greps for a phrase where it could run the code instead.

Run: python3 test_overnight.py
"""
# Suites live in tests/ but RUN from the repo root, so that the modules
# under test import and open("neo.py") still resolves. This makes the
# import work either way, so a suite can also be run directly.
import os as _bootstrap_os, sys as _bootstrap_sys
_bootstrap_sys.path.insert(0, _bootstrap_os.path.dirname(
    _bootstrap_os.path.dirname(_bootstrap_os.path.abspath(__file__))))

import os
import sys
import time

sys.path.insert(0, ".")

# Checks that touch the user's REAL machine — opening files, activating apps,
# writing his clipboard, drawing on screen — only run when this is set.
# They were on by default and the suite runs dozens of times a day, so every
# run stole his focus and opened documents at him while he was working.
LIVE = os.getenv("NEO_LIVE_TESTS") == "1"

FAILED = []


def check(name, cond):
    print(("PASS - " if cond else "FAIL - ") + name)
    if not cond:
        FAILED.append(name)


# =========================================================================== #
# 1. Neo read a tool call out loud, character by character.
#
#    Neo: [[hand_to_claude: {"task": "Open the file '2026_ppr_draft_strategy…
#
#    On the spoken path the model's words ARE the audio, so a "hidden" tag is
#    not hidden. The written prompt teaches four of them and promises "He will
#    not hear it" — which is simply false there — and the model invented a
#    fifth by analogy.
# =========================================================================== #
import re

import memory

written = memory.build_system_prompt(memory.load_memory(), spoken=False)
spoken = memory.build_system_prompt(memory.load_memory(), spoken=True)

emitted = lambda p: sorted(set(re.findall(r"\[\[(\w+):", p)) - {"now"})
check(f"the WRITTEN prompt still teaches its tags ({emitted(written)}) — that "
      "path strips them before speaking, so they work there",
      len(emitted(written)) >= 2)
check("the SPOKEN prompt teaches none of them",
      emitted(spoken) == [])
check("...and says outright never to speak in brackets",
      "NEVER SPEAK IN BRACKETS" in spoken)
check("...and points at the tool that replaces the memory tag",
      "remember_this" in spoken)
check("the false promise is gone from the spoken prompt",
      "He will not hear it" not in spoken)
check("stripping tags does not gut the prompt — the personality survives",
      "REGISTER IS JARVIS" in spoken and len(spoken) > len(written) * 0.7)
check("spoken_prompt is pure and repeatable",
      memory.spoken_prompt(written) == memory.spoken_prompt(written))

import agent

check("there is a remember_this TOOL, since the spoken path cannot write a "
      "hidden note", any(t.__name__ == "remember_this" for t in agent.TOOLS))

# And if the model slips anyway, the intent was real — run it.
import live

sess = live.LiveSession.__new__(live.LiveSession)
sess.log = lambda m: None
sess._rescued = set()
ran = []
sess.tools_by_name = {"hand_to_claude": lambda task="", project="": ran.append(task)}
SAID = ('I will hand this over. [[hand_to_claude: {"task": "Open the file '
        '2026_ppr_draft_strategy.md immediately"}]]')
check("a tool the model SAID instead of calling is actually run — the request "
      "was real, and letting it evaporate into a sentence helps nobody",
      sess._rescue_spoken_tool(SAID) is True)
time.sleep(0.4)
check("...with the arguments it meant", ran and "2026_ppr" in ran[0])
check("the transcript re-sending the same line does not run it twice",
      sess._rescue_spoken_tool(SAID) is False)
check("ordinary speech is left alone",
      sess._rescue_spoken_tool("It's open now, top right.") is False)
check("a tool that does not exist is refused, not invented",
      sess._rescue_spoken_tool('[[not_a_tool: {"a": 1}]]') is False)
check("unreadable arguments are refused rather than guessed",
      sess._rescue_spoken_tool('[[hand_to_claude: {broken]]') is False)


# =========================================================================== #
# 2. "It's up in Accio" — while he was looking at something else.
# =========================================================================== #
import desk

check("open_file and the window check exist",
      callable(getattr(desk, "open_file", None))
      and callable(getattr(desk, "window_showing", None)))

DOC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "2026_ppr_draft_strategy.md")
if not LIVE:
    print("SKIP - live open checks (set NEO_LIVE_TESTS=1; they open files and "
          "switch apps on the real machine)")
elif os.path.isfile(DOC):
    import subprocess

    def front(app):
        subprocess.run(["osascript", "-e", f'tell application "{app}" to activate'],
                       capture_output=True)
        time.sleep(1.3)

    # Can this process raise another app at all? A bare venv python often
    # cannot: that needs automation rights the Neo app bundle has, and
    # osascript exits zero either way. Without it, "did it come to the front"
    # is unanswerable here and these checks would be measuring a macOS
    # permission rather than anything about Neo.
    front("Google Chrome")
    CAN_RAISE = desk.raise_app("TextEdit")
    front("Google Chrome")

    check("a file open only in a BACKGROUND window does not count as showing — "
          "Accio had it open behind, Neo saw the app come forward and said "
          "'it's up in Accio' while he was looking at something else",
          desk.window_showing(DOC) == "")

    if CAN_RAISE:
        ok, app = desk.open_file(DOC, log=lambda m: None)
        check(f"opening it is verified against the FRONT window ({app!r})",
              ok and app)
        check("...and the check agrees immediately afterwards",
              desk.window_showing(DOC) == app)
        # The Accio case exactly: already open behind, and `open -a` is a
        # no-op on an already-running app so it never came forward.
        front("Google Chrome")
        holder = desk.app_with_file(DOC)
        check(f"an app holding it out of sight is found ({holder!r})",
              bool(holder))
        ok2, app2 = desk.open_file(DOC, log=lambda m: None)
        check("...and either RAISED, or at least NAMED so he is told where it "
              f"is (got {ok2}, {app2!r}) — having it open somewhere he cannot "
              "see is not the same as it being open, and not a reason to say "
              "it failed", bool(app2))
    else:
        print("SKIP - live front-window checks (this process cannot raise "
              "apps; that is a macOS permission, not a Neo bug)")
    # THE INVARIANT, rather than a guess about what happens to be in front.
    # Three earlier versions of this check asserted "after switching away it is
    # not showing" and kept flaking, because the app being switched TO
    # sometimes had the file open too — in which case "showing" is correctly
    # true and the check was asserting the opposite of the truth. What must
    # always hold: window_showing only ever names the FRONTMOST app, and only
    # when that app's front window is titled after the file.
    front("Google Chrome")
    who = desk.window_showing(DOC)
    check(f"window_showing only ever names the frontmost app (said {who!r}, "
          f"front is {desk.frontmost_app()!r})",
          who == "" or who == desk.frontmost_app())
    check("...and never claims a file is showing when nothing is titled after "
          "it", desk.window_showing("/tmp/a_file_nothing_has_open_zqq.md") == "")
else:
    print("SKIP - live open checks (the document is gone)")

check("a file that does not exist is never reported as opened",
      desk.open_file("/definitely/not/here.md", log=lambda m: None) == (False, ""))
check("every common document type has an opener that will definitely show it, "
      "because plain `open` exits zero and shows nothing when a type has no "
      "handler — which is what markdown does on this Mac",
      all(desk._OPENERS.get(e) for e in (".md", ".txt", ".pdf", ".csv")))


# =========================================================================== #
# 3. A docstring is not a guard.
# =========================================================================== #
# Tested through the PURE predicate. An earlier version called control_mac
# with the allowed scripts to prove they got through, which really did open
# Safari and start playing music mid-run and stole focus from every check
# after it — including the ones about which window is in front.
for script in ('tell application "Accio" to open "2026 PPR draft strategy.md"',
               'tell app "Finder" to reveal file "report.pdf"',
               'do shell script "open /Users/x/notes.txt"',
               'tell application "Preview" to open "notes.pdf"'):
    check(f"control_mac REFUSES to open a file ({script[:36]}…) — it was told "
          "not to in capitals and did it on the very next turn",
          agent.is_file_open_script(script))
for script in ('tell application "Spotify" to return name of current track',
               'tell application "Music" to play',
               'tell application "Safari" to open location "https://x.com"',
               'tell application "System Events" to keystroke "s" using command down',
               'tell application "Notes" to make new note'):
    check(f"...and still allows real automation ({script[:36]}…)",
          not agent.is_file_open_script(script))
check("the refusal names the tool that does work",
      "open_document" in agent.control_mac(
          'tell application "X" to open "a.md"'))


# =========================================================================== #
# 4. The hands existed and the BRAIN could not reach them.
# =========================================================================== #
names = {t.__name__ for t in agent.TOOLS}
for want, why in [("click_on_screen", "click anything"),
                  ("type_into", "put text in a specific box"),
                  ("scroll_screen", "see what is below the fold"),
                  ("wait_for_screen", "find out whether a click did anything")]:
    check(f"the brain can now {why} ({want})", want in names)

src = open("agent.py").read()
check("type_into tries the DIRECT click first — on the web the label usually "
      "IS the box, and aiming below 'Search Wikipedia' lands on the page",
      src.index("ok, why = act.click(field") < src.index("act.click_field(field"))
check("click_on_screen takes the app, because the first click on an unfocused "
      "window is eaten by macOS",
      "app or None" in src and "swallowed" in src)

import context

brief = context.brief()
check("the standing brief TELLS it that it has hands — a tool nothing mentions "
      "is a tool nothing calls", "YOU HAVE HANDS" in brief)
check("...and that ok means the click went out, not that the page obeyed",
      "NOT mean the page did what you wanted" in " ".join(brief.split()))


# =========================================================================== #
# 5. A refusal exits zero, so it read as success.
# =========================================================================== #
import claude_bridge as cb

REAL = ("I tried to set that up to run, but I can't without access to the "
        "project folder.")
check("the refusal that started this is recognised as one", cb.refused(REAL))
check("a long report that hit ONE wall is not a refusal — it did the work",
      not cb.refused("I wrote the full plan to draft.md. I could not find the "
                     "config file so I used the defaults throughout."))
for good in ("Done. Wrote 2026_ppr_draft_strategy.md.",
             "Here's the answer: fifty five users.",
             "Fixed it and the tests pass.", ""):
    check(f"...nor is {good[:34]!r}", not cb.refused(good))
check("a refusal is retried even though the exit code was zero",
      cb.should_retry(0, False, False, REAL))
check("a real success is not retried",
      not cb.should_retry(0, False, False, "Done, wrote the file."))
check("a hard crash is still retried", cb.should_retry(1, False, False, ""))
check("never more than once", not cb.should_retry(0, False, True, REAL))
check("a cancelled job is never retried", not cb.should_retry(1, True, False, ""))
again = cb.retry_task("GOAL", REAL)
check("the retry pushes back instead of repeating the brief — the user had to "
      "say 'you don't need access to the project folder' himself",
      "not an answer" in again and "HURDLE" in again)
check("a crash retry still gets the ordinary treatment",
      "DIFFERENT approach" in cb.retry_task("GOAL", "Traceback: boom")
      and "not an answer" not in cb.retry_task("GOAL", "Traceback: boom"))


# =========================================================================== #
# 6. Neo could not report its own bugs.
# =========================================================================== #
import selfrepair

a = selfrepair.signature("open_document", "could not open /Users/a/2026_ppr.md")
b = selfrepair.signature("open_document", "could not open /Users/a/other.md")
c = selfrepair.signature("click_on_screen", "could not open /Users/a/2026_ppr.md")
check("the same fault with a different filename is ONE bug", a == b)
check("a different tool is a different bug", a != c)
check("and so is a different count", selfrepair.signature("t", "failed 3 times")
      == selfrepair.signature("t", "failed 7 times"))

sent = []
now = 1_700_000_000.0
old_path = selfrepair.FILED_PATH
selfrepair.FILED_PATH = "/tmp/neo_filed_test.json"
try:
    if os.path.exists(selfrepair.FILED_PATH):
        os.remove(selfrepair.FILED_PATH)
    ok, why = selfrepair.file_bug("open_document",
                                  "said it opened the file and nothing appeared",
                                  asked_for="show me the document",
                                  start=sent.append, log=lambda m: None, now=now)
    check("a bug gets filed", ok and len(sent) == 1)
    check("the brief carries the log tail, which is the only part Claude can "
          "actually act on", "neo.log" in sent[0])
    check("...and what the user had asked for", "show me the document" in sent[0])
    check("...and tells Claude to reproduce before changing anything",
          "Reproduce it before" in sent[0])
    check("...and to add a check that fails first", "FAILS before your fix" in sent[0])
    ok2, why2 = selfrepair.file_bug("open_document",
                                    "said it opened the file and nothing appeared",
                                    start=sent.append, log=lambda m: None, now=now)
    check("the same bug five times in an evening is ONE report",
          not ok2 and len(sent) == 1 and "already" in why2)
    ok3, _ = selfrepair.file_bug("open_document",
                                 "said it opened the file and nothing appeared",
                                 start=sent.append, log=lambda m: None,
                                 now=now + 25 * 3600)
    check("...and can be filed again the next day", ok3)
    check("a report with no detail is refused, because Claude cannot act on it",
          selfrepair.file_bug("x", "bad", start=sent.append)[0] is False)
    check("with no way to reach Claude it says so rather than pretending",
          selfrepair.file_bug("t", "a real detailed problem here",
                              start=None)[0] is False)
finally:
    selfrepair.FILED_PATH = old_path

check("report_bug is a tool the model can call",
      "report_bug" in {t.__name__ for t in agent.TOOLS})
check("the brief tells it when to file one", "report_bug" in brief)
check("...and to carry on afterwards rather than reporting the report",
      "Filing a bug is not an answer" in brief)


# =========================================================================== #
# 7. Failing silently: an action that claims success without checking.
# =========================================================================== #
import hands

check("opening an app waits for it to actually come to the front — `open -a` "
      "exits zero whether or not anything appears",
      "_came_forward" in open("hands.py").read())
check("an app that does not exist is reported honestly",
      "couldn't find an app" in hands.open_app("Zqq Not A Real App"))
check("a launch that never fronts is reported honestly too",
      "hasn't come to the front" in open("hands.py").read())


# =========================================================================== #
# 8. Conversational failures, straight from the transcript.
# =========================================================================== #
P = memory.PERSONALITY
check('"You\'re absolutely right, it wasn\'t open." — agreeing costs a sentence '
      "and delivers nothing; the correction IS the instruction",
      "ABSOLUTELY RIGHT" in P)
check('"I can search again, or we could pass it back to Claude" — handing the '
      "work back as a menu", "DO NOT OFFER THEM A MENU" in P)
check('"Which project did Claude work on last?" — a question it had the '
      "answer to", "DO NOT ASK WHEN YOU CAN LOOK" in P)
check("losing the thread between two sentences", "FOLLOW THE THREAD" in P)
check("all of it survives into the spoken prompt, which is the path that "
      "actually talks to him",
      all(s in spoken for s in ("ABSOLUTELY RIGHT", "DO NOT OFFER THEM A MENU",
                                "FOLLOW THE THREAD")))



# =========================================================================== #
# 9. Deep-work mode. the user: "if I am doing important deep work... I don't want
#    Neo taking control of my computer... if I ask it to open a document, it
#    silently finds the document and opens it, without clicking a bunch of
#    things because that distracts me."
#
#    All of this rests on things that were MEASURED first, not assumed:
#      open -g          opens a file and the frontmost app never changes
#      reading over AX  is completely side-effect free
#      AppleScript      drives scriptable apps with no cursor at all
#      pressing a control DOES front that app — unavoidable on macOS
#      web page content is NOT reachable through accessibility in Chrome
# =========================================================================== #
import quiet
import act
import commands as cmd

NOW = 1_700_000_000.0
quiet.off()
try:
    check("focus mode starts off", not quiet.is_on())
    quiet.on()
    check("...turns on", quiet.is_on())
    # Expiry is checked against the stamp just written, with a clock far
    # enough ahead. Writing a 2023 stamp instead made it read as ALREADY
    # expired, so every behavioural check below silently ran in normal mode
    # and passed for the wrong reason.
    check("...and expires on its own, because nobody remembers to turn it off",
          not quiet.is_on(now=time.time() + (quiet.MAX_H + 1) * 3600))
    check("...with time left reported in minutes, for saying it out loud",
          0 < quiet.left() <= quiet.MAX_H * 60)

    check("in focus mode Neo will NOT move his cursor",
          act.can_act()[0] is False)
    check("...and says why, naming the way through rather than just refusing",
          "focus mode" in act.can_act()[1].lower()
          and "go ahead" in act.can_act()[1].lower())
    check("...and nothing is sent even if a click is attempted",
          act.click_at(50, 50)[0] is False)
    check("'go ahead' overrides it for one action", act.can_act(force=True)[0] is True)

    quiet.off()
    check("out of focus mode the hands work normally", act.can_act()[0] is True)
finally:
    quiet.off()

for said in ("i'm working", "focus mode", "leave me alone", "deep work",
             "stay out of my way", "i'm studying", "do not disturb",
             "don't touch my screen", "i'm locked in", "no distractions"):
    check(f"{said!r} turns it on", cmd.wants_quiet(said) is True)
for said in ("focus mode off", "turn off focus", "i am done working",
             "go ahead", "stop focus mode", "you can use my mouse"):
    check(f"{said!r} turns it off", cmd.wants_quiet(said) is False)
for said in ("what's the weather", "open the document", "how are you",
             "set a timer for ten minutes", "what's apple trading at"):
    check(f"{said!r} is neither", cmd.wants_quiet(said) is None)

check("a focus_mode tool exists so Neo can set it itself",
      "focus_mode" in {t.__name__ for t in agent.TOOLS})
check("the brief explains what does and does not change",
      "focus_mode" in brief and "carries on exactly as before" in brief)

src_desk = open("desk.py").read()
check("opening a file in focus mode uses open -g, which was measured leaving "
      "the frontmost app untouched", '"open", "-g"' in src_desk)
src_neo = open("neo.py").read()
check("no cards get through in focus mode", "held back — focus mode" in src_neo)

# The honest half: what focus mode does NOT change.
check("answering, searching and reading never needed the screen, so they are "
      "untouched — focus mode is not Neo doing less",
      all(t in {x.__name__ for x in agent.TOOLS}
          for t in ("search_web", "read_document", "what_did_i_copy",
                    "hand_to_claude", "draft_email", "check_market")))

# Live proof, on the real machine.
import subprocess

if not LIVE:
    print("SKIP - the live focus-mode proof (set NEO_LIVE_TESTS=1; it opens a "
          "file on the real machine)")
    quiet.off()
else:
    FRESH = "/tmp/neo_quiet_check.md"
    open(FRESH, "w").write("# quiet check\n")
    quiet.on()
    try:
        subprocess.run(["osascript", "-e",
                        'tell application "Google Chrome" to activate'],
                       capture_output=True)
        time.sleep(1.4)
        was = desk.frontmost_app()
        ok_q, app_q = desk.open_file(FRESH, log=lambda m: None)
        time.sleep(0.6)
        now_front = desk.frontmost_app()
        check(f"THE WHOLE POINT: the file opened ({ok_q}, {app_q!r}) and his "
              f"frontmost app did not change ({was!r} -> {now_front!r})",
              ok_q and was == now_front)
    finally:
        quiet.off()
        os.path.exists(FRESH) and os.remove(FRESH)




# =========================================================================== #
# 10. "Why does Neo just wad off every once in a while."
#
#     From the log, the whole failure in two lines:
#       16:11:00  You: Go find the task descriptions on Chrome...
#       16:11:13  [live] no answer came back — resetting the session.
#
#     Thirteen seconds of silence and the watchdog tore the session down. Neo
#     said NOTHING, so from where he was sitting it simply stopped existing
#     while he waited for an answer to a real question.
# =========================================================================== #
import live as _live

# THE WATCHDOG CANNOT KILL A THINKING MODEL ANY MORE. A thinking model and a
# wedged socket are both silence — there is no client-side signal separating
# them — so anything that ends a session on a few seconds of quiet is guessing,
# and it guessed wrong on a real question thirteen seconds in.
check(f"being slow ({_live.ANSWER_TIMEOUT_S}s) and being dead "
      f"({_live.ANSWER_HARD_S}s) are now two different thresholds",
      _live.ANSWER_TIMEOUT_S < _live.ANSWER_HARD_S)
check("the hard ceiling is long past any plausible think time",
      _live.ANSWER_HARD_S >= 90)
for t in (13, 30, 60, 110):
    check(f"a turn {t}s in is NOT killed — this is exactly the window the old "
          "watchdog destroyed a real question in",
          not _live.answer_overdue(1000.0, 1000.0 + t))
check("...but he IS told it is taking a while, so silence is never mysterious",
      _live.answer_slow(1000.0, 1030.0))
check("...only once — the flag is per turn",
      "self._said_slow" in open("live.py").read())
check("a genuinely dead socket is still given up on eventually",
      _live.answer_overdue(1000.0, 1000.0 + _live.ANSWER_HARD_S + 1))
check("a running tool is not even called slow that early",
      not _live.answer_slow(1000.0, 1040.0, in_tool=True))
check("nothing fires between turns",
      not _live.answer_overdue(0, 99999.0) and not _live.answer_slow(0, 99999.0))
import neo as _neo

check("the slow hook exists and does not end anything",
      hasattr(_neo.Neo, "_live_slow"))

check("a dropped answer is re-asked rather than mourned",
      hasattr(_neo.Neo, "_retry_dead_turn"))
check("...exactly once, because a model that cannot answer a question will "
      "not answer it on the third try either while he waits",
      _neo.Neo.RETRY_DEAD_TURNS == 1)

import inspect as _in

ended = _in.getsource(_neo.Neo._live_ended)
check("the retry is wired to the 'no answer' close specifically, not to every "
      "hang-up", '"no answer"' in ended and "_retry_dead_turn" in ended)

# Drive the real method with the session and brain mocked out.
obj = _neo.Neo.__new__(_neo.Neo)
obj._dead_turn_tries = 0
said, started, spoken_into = [], [], []


class _Brain:
    _turns = [{"role": "user", "text": "what's the weather"},
              {"role": "model", "text": "cold"},
              {"role": "user", "text": "go find the task descriptions on Chrome"}]


class _Sess:
    def is_running(self):
        return True

    def speak(self, text, log=None):
        spoken_into.append(text)
        return True


obj.brain = _Brain()
obj.say = said.append
obj._start_live = lambda holding=False: (started.append(1),
                                         setattr(obj, "live", _Sess()))[0]
obj.live = None
obj._retry_dead_turn()
check("it reopens the conversation", started == [1])
check(f"...and asks HIS question again, not a generic apology ({spoken_into})",
      spoken_into == ["go find the task descriptions on Chrome"])
check("...and says nothing extra while doing it — he wanted an answer, not a "
      "status report", said == [])

# Second failure in a row: stop trying, and tell him.
started.clear(); spoken_into.clear()
obj._retry_dead_turn()
check("a second dead turn stops retrying", started == [])
check(f"...and he is TOLD, which is the whole complaint ({said})",
      len(said) == 1 and "didn't come back" in said[0])
check("...and the counter resets so the next question starts clean",
      obj._dead_turn_tries == 0)

obj._dead_turn_tries = 0
obj.brain = type("B", (), {"_turns": []})()
said.clear()
obj._retry_dead_turn()
check("with no question to re-ask it still says something rather than dying "
      "quietly", len(said) == 1)

check("a real answer clears the streak, so an unlucky turn does not make the "
      "next one give up early",
      "self._dead_turn_tries = 0" in open("neo.py").read())




# =========================================================================== #
# 11. "Neo crashed AGAIN." It had not crashed — it was WEDGED, which from
#     where he sits is worse, because the process is alive and the log knows.
#
#       01:51:10  WATCHDOG: a turn has been running 4340s — likely a hung
#                 call; it should time out shortly.
#
#     Seventy-two of those, about the SAME turn, over an hour and twelve
#     minutes. Nothing ever timed anything out: the watchdog only ever LOGGED.
#     Meanwhile the worker held self._busy, so every fn press hit
#     `if self._busy.locked(): return` and did nothing at all.
# =========================================================================== #
import threading as _th
import queue as _q

check(f"a turn past {_neo.STUCK_TURN_S:.0f}s is treated as wedged, not slow — "
      "the brain answers in seconds and a Claude job is fire-and-forget, so "
      "there is no legitimate turn anywhere near this long",
      60 < _neo.STUCK_TURN_S <= 300)

src_n2 = open("neo.py").read()
check("the watchdog now ACTS instead of narrating — 'it should time out "
      "shortly' was aspirational and nothing ever enforced it",
      "self._force_unstick(age)" in src_n2)
check("...and a press while wedged recovers too, because the press IS the "
      "report that nothing is happening", "neo-unstick-press" in src_n2)

# Drive it against a thread that genuinely cannot be killed, which is the
# real case: the worker is parked inside a blocking PortAudio write.
obj = _neo.Neo.__new__(_neo.Neo)
obj._busy = _th.Lock()
obj._busy_since = None
obj._interrupt = _th.Event()
obj._jobs = _q.Queue()
obj.state = type("S", (), {"set": lambda s, v: None, "get": lambda s: "idle"})()
told = []
obj.say = told.append

wedged = _th.Event()


def _never_returns():
    with obj._busy:
        obj._busy_since = time.time()
        wedged.set()
        time.sleep(9999)


_th.Thread(target=_never_returns, daemon=True).start()
wedged.wait(3)
old_lock = obj._busy
check("the wedged turn holds the lock", old_lock.locked())

obj._force_unstick(4340)
time.sleep(0.4)
check("recovery takes a FRESH lock — Python cannot kill the thread, so the "
      "only real fix is to stop anyone waiting on the one it holds",
      obj._busy is not old_lock and not obj._busy.locked())
check("...the abandoned thread keeps the old lock, and nobody cares",
      old_lock.locked())
check("...a new worker is running",
      any(t.name.startswith("neo-worker-") for t in _th.enumerate()))
check("...a new turn can proceed immediately",
      obj._busy.acquire(timeout=1))
check(f"...and he is TOLD, having pressed a dead key for an hour ({told})",
      len(told) == 1 and "jammed" in told[0])
obj._busy.release()

check("recovery also resets the audio device, since a wedged PortAudio write "
      "is the most likely thing holding the turn — and it blocks any new live "
      "session too", "reset_portaudio" in _in.getsource(_neo.Neo._force_unstick))
check("...and sets the interrupt, which is what unblocks a stuck _speak",
      "_interrupt.set()" in _in.getsource(_neo.Neo._force_unstick))


print()
if FAILED:
    print(f"{len(FAILED)} FAILED:")
    for n in FAILED:
        print("  - " + n)
    sys.exit(1)
print("Overnight run clean.")
