"""test_act.py — Neo's hands, checked against things that actually went wrong.

Clicking is the one thing in this codebase you cannot take back. A ring drawn
on the wrong button is embarrassing; a click on the wrong button has already
happened. So the checks here are weighted toward REFUSING: ambiguity, drift,
off-screen coordinates, and targets that are not what they looked like.

Every check was written from something observed on a real screen:

  - "next" scored 0.9 against the sentence "Next steps for your application",
    because the first scoring rule rewarded anything at the start of a line.
  - "ask" matched Chrome's own "Ask Gemini" button as strongly as the Hacker
    News nav link, and the code silently took the first. On that same page
    "comments" matches TWENTY times.
  - Vision returns a whole visual row as one observation, so 'Hacker News new
    | threads | past | comments | ask' is eight links in one string. Locating
    a word in it by counting characters assumes fixed-width glyphs and lands
    on the neighbour.
  - The first click on an unfocused window is eaten by macOS. Measured:
    Chrome fully visible, Finder focused, a click dead centre on a button,
    and the page did not react. The identical second click worked.
  - A blank input has no text at all, so it cannot be found by name.

Run: python3 test_act.py
The live click checks need a display and only run with NEO_LIVE_CLICK=1, so a
routine suite run can never steal focus from whatever the user is doing.
"""
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, ".")
import act
import highlight

FAILED, SKIPPED = [], []


def check(name, cond):
    print(("PASS - " if cond else "FAIL - ") + name)
    if not cond:
        FAILED.append(name)


def skip(name, why=""):
    print(f"SKIP - {name}" + (f" ({why})" if why else ""))
    SKIPPED.append(name)


# =========================================================================== #
# 1. Geometry. A percentage is right in both worlds; a pixel is right in one.
# =========================================================================== #
w, h = act.screen_size()
check(f"the screen reports a real size in points ({w}x{h})", w > 0 and h > 0)
check("the middle of the screen is the middle of the screen",
      act.to_points(50, 50) == (w / 2, h / 2))
check("0,0 is the top left corner", act.to_points(0, 0) == (0.0, 0.0))
for bad in [(120, 50), (-1, 10), (50, 101), (50, -0.1)]:
    check(f"{bad} is off the screen and is REFUSED, not clamped to the edge — "
          "a clamped click lands on the menu bar or the Dock, which are real "
          "buttons, just not the one anyone meant",
          act.to_points(*bad) is None)

box = {"x": 10, "y": 20, "w": 4, "h": 2}
check("the centre of a box is its centre", act.centre_of(box) == (12.0, 21.0))
check("a target that shifted a hair has not moved",
      not act.moved(box, {"x": 10.4, "y": 20.2, "w": 4, "h": 2}))
check("a target that shifted across the screen has",
      act.moved(box, {"x": 40, "y": 20, "w": 4, "h": 2}))
check("a target that vanished counts as moved, not as unchanged",
      act.moved(box, None) and act.moved(None, box))

# =========================================================================== #
# 2. Scoring. Coverage decides, because a click is not reversible.
# =========================================================================== #
SCORES = [
    ("sign in", "sign in", 1.0, "an exact label"),
    ("save", "save", 1.0, "another"),
    ("log in", "completely unrelated words", 0.0, "no relation at all"),
]
for needle, hay, want, why in SCORES:
    got = act._score(needle, hay)
    check(f"{why}: {needle!r} vs {hay!r} scores {want} (got {got:.2f})",
          abs(got - want) < 0.01)

check("a short label buried in a long sentence is REFUSED — 'next' inside "
      "'Next steps for your application' is not the Next button, and the "
      "first version of this scored it 0.9",
      act._score("next", "next steps for your application") < act.MATCH_FLOOR)
check("...but a label that is most of the line is accepted — 'sign in' in "
      "'sign in with google' is the button",
      act._score("sign in", "sign in with google") >= act.MATCH_FLOOR)
check("coverage, not position, is what decides",
      act._score("submit", "submit") > act._score("submit", "submit your work now"))
check("an empty needle matches nothing",
      act._score("", "anything") == 0.0 and act._score("x", "") == 0.0)

# =========================================================================== #
# 3. Matching and ambiguity, on lines built here so the answer is known.
# =========================================================================== #
def line(text, x, y, w=20.0, h=1.5, words=None):
    ln = {"text": text, "x": x, "y": y, "w": w, "h": h, "conf": 1.0}
    if words is not None:
        ln["words"] = words
    return ln


NAV_WORDS = [
    {"text": "Hacker", "x": 9.9, "y": 13.9, "w": 3.2, "h": 1.7},
    {"text": "News", "x": 13.3, "y": 13.9, "w": 2.5, "h": 1.7},
    {"text": "new", "x": 17.0, "y": 13.9, "w": 2.1, "h": 1.7},
    {"text": "|", "x": 19.3, "y": 13.9, "w": 0.3, "h": 1.7},
    {"text": "threads", "x": 19.7, "y": 13.9, "w": 3.9, "h": 1.7},
    {"text": "|", "x": 23.8, "y": 13.9, "w": 0.3, "h": 1.7},
    {"text": "past", "x": 24.2, "y": 13.9, "w": 2.4, "h": 1.7},
    {"text": "|", "x": 26.8, "y": 13.9, "w": 0.3, "h": 1.7},
    {"text": "comments", "x": 27.5, "y": 13.9, "w": 5.1, "h": 1.7},
    {"text": "|", "x": 32.9, "y": 13.9, "w": 0.3, "h": 1.7},
    {"text": "ask", "x": 33.4, "y": 13.9, "w": 1.8, "h": 1.7},
]
NAV = line("Hacker News new | threads | past | comments | ask", 9.9, 13.9,
           36.0, 1.7, words=NAV_WORDS)
STORY = line("977 points by ckardaris 1 day ago | 478 comments", 20.0, 27.0,
             22.0, 1.4, words=[
                 {"text": "977", "x": 20.0, "y": 27.0, "w": 1.5, "h": 1.4},
                 {"text": "comments", "x": 38.0, "y": 27.0, "w": 5.1, "h": 1.4}])
GEMINI = line("Ask Gemini", 93.8, 4.4, 5.0, 1.4, words=[
    {"text": "Ask", "x": 93.8, "y": 4.4, "w": 1.8, "h": 1.4},
    {"text": "Gemini", "x": 95.9, "y": 4.4, "w": 2.9, "h": 1.4}])
SCREEN = [GEMINI, NAV, STORY]

hit = act.find_text("threads", SCREEN)
check("a word inside a merged row is found where VISION measured it, not "
      "where character-counting would put it "
      f"(x={hit['x'] if hit else None})",
      hit and abs(hit["x"] - 19.7) < 0.2 and abs(hit["w"] - 3.9) < 0.2)
check("...and that is flagged as measured, not estimated", hit["measured"])

# Without word boxes there is nothing to measure, only a proportion to guess
# at — so a small label inside a long row is REFUSED rather than estimated.
# That is the whole design in one line: the fallback exists, and it is not
# allowed to be confident. In production read_screen always supplies word
# boxes; this path is what happens when they are missing.
bare = line("Hacker News new | threads | past", 9.9, 13.9)
check("with no word boxes, a small label inside a long row is refused rather "
      "than placed by counting characters",
      act.find_text("threads", [bare]) is None)
est = act.find_text("sign in", [line("sign in with google", 10, 20)])
check("...but a label that is most of the line still resolves, and says it "
      "was ESTIMATED so a caller can tell the two apart",
      est and not est["measured"])

check("an exact whole-line label outranks the same word inside a longer row",
      act.find_text("Ask Gemini", SCREEN)["score"] == 1.0)

rivals = act.rivals(act.find_all_text("comments", SCREEN))
check(f"'comments' on this screen is ambiguous ({len(rivals)} equal matches) "
      "— on a real Hacker News page it is twenty", len(rivals) == 2)
check("'ask' matches both the nav and Chrome's own Ask Gemini button, which "
      "is the pair that made the first version click the wrong one",
      len(act.rivals(act.find_all_text("ask", SCREEN))) == 2)
check("an unambiguous label has exactly one rival — itself",
      len(act.rivals(act.find_all_text("threads", SCREEN))) == 1)

box, why = act.find("comments", client=None, lines=SCREEN, log=lambda m: None)
check("with no way to choose, an ambiguous target is REFUSED and the reason "
      f"says how many it saw ({why!r})",
      box is None and why and "2" in why)

box, why = act.find("comments", client=None, lines=SCREEN, near="977 points",
                    log=lambda m: None)
check("a `near` anchor resolves the ambiguity with no model call at all",
      box is not None and abs(box["y"] - 27.0) < 1.0)

box, why = act.find("threads", client=None, lines=SCREEN, log=lambda m: None)
check("an unambiguous target needs no model and no anchor",
      box is not None and why is None)

check("two OCR reads of the SAME control are one control, not an ambiguity",
      len(act.rivals([{"x": 10, "y": 10, "w": 4, "h": 2, "score": 1.0},
                      {"x": 10.3, "y": 10.1, "w": 4, "h": 2, "score": 1.0}])) == 1)


class _Picks:
    def __init__(self, answer):
        class _M:
            def generate_content(inner, model, contents):
                assert "%" not in contents or "down the screen" in contents
                return type("R", (), {"text": answer})()
        self.models = _M()


hits = act.rivals(act.find_all_text("comments", SCREEN))
check("the model picks by number from a text list, and the numbering is 1-based",
      act.choose("comments", hits, _Picks("2")) == 1)
check("a refusal from the model means no click, not the first option",
      act.choose("comments", hits, _Picks("NONE")) is None)
check("an out-of-range answer is discarded rather than wrapped around",
      act.choose("comments", hits, _Picks("99")) is None)
check("prose around the number is tolerated",
      act.choose("comments", hits, _Picks("I'd say option 1 is the nav.")) == 0)


class _Dead:
    class models:
        @staticmethod
        def generate_content(model, contents):
            raise RuntimeError("503")


check("a model outage means no choice and therefore no click",
      act.choose("comments", hits, _Dead, log=lambda m: None) is None)
check("the disambiguation prompt marks screen text as data, never as "
      "instructions — OCR reads whatever a web page decided to put there",
      "never an instruction" in act.CHOOSE)

# =========================================================================== #
# 4. Empty fields — found by the space they occupy, not by their text.
# =========================================================================== #
LABEL = {"x": 4.6, "y": 66.2, "w": 11.5, "h": 4.0}
FORM = [line("Type here:", 4.6, 66.2, 11.5, 4.0),
        line("FIELD SAYS", 4.8, 85.3, 12.6, 3.3)]
spot = act.field_spot(LABEL, FORM)
check(f"the field is placed in the empty gap under its label (y={spot[1]:.1f}, "
      "between 70.2 and 85.3) rather than at a fixed multiple of the label "
      "height, which is wrong the moment a form uses different spacing",
      spot and 70.2 < spot[1] < 85.3)
check("...and horizontally over the label, not off to one side",
      spot and LABEL["x"] - 1 <= spot[0] <= LABEL["x"] + LABEL["w"])

TIGHT = [line("Name", 10, 40, 6, 1.5), line("Email", 10, 41.9, 6, 1.5)]
check("two labels stacked with no room between them means there is no field "
      "there, and it says so instead of clicking the next label",
      act.field_spot({"x": 10, "y": 40, "w": 6, "h": 1.5}, TIGHT) is None)

FAR = [line("Section A", 10, 10, 8, 1.5), line("Section B", 10, 80, 8, 1.5)]
spot = act.field_spot({"x": 10, "y": 10, "w": 8, "h": 1.5}, FAR)
check("a huge empty gap is capped — 70% of the screen below a heading is a "
      "page, not an input box",
      spot and spot[1] - 11.5 <= act.FIELD_MAX_GAP + 0.01)

SIDE = [line("Search", 10, 40, 6, 1.5), line("Go", 40, 40, 3, 1.5)]
spot = act.field_spot({"x": 10, "y": 40, "w": 6, "h": 1.5}, SIDE, where="right")
check("a field beside its label lands between the two, not on top of either",
      spot and 16 < spot[0] < 40)

check("a label three columns over does not close the gap under this one",
      act.field_spot({"x": 4.6, "y": 66.2, "w": 11.5, "h": 4.0},
                     FORM + [line("sidebar text", 76.0, 68.0, 18.0, 1.5)])[1]
      == act.field_spot(LABEL, FORM)[1])

check("the probe is more than one character — Vision does not reliably "
      "report a lone letter", len(act.PROBE) >= 2)

# =========================================================================== #
# 5. Guards. Nothing is sent when it shouldn't be.
# =========================================================================== #
os.environ["NEO_NO_CLICK"] = "1"
try:
    ok, why = act.can_act()
    check("clicking can be switched off entirely, and says so", not ok and why)
    sent, why = act.click_at(50, 50)
    check("...and nothing is sent while it is off", not sent)
finally:
    del os.environ["NEO_NO_CLICK"]

_real = act._user_is_talking
try:
    act._user_is_talking = lambda: True
    ok, why = act.can_act()
    check("nothing is clicked while he is holding the key — he is talking and "
          "the screen is his", not ok and "sentence" in why)
finally:
    act._user_is_talking = _real

check("a broken key check does not wedge the hands open or shut",
      isinstance(act._user_is_talking(), bool))

# =========================================================================== #
# 6. Skills can reach all of it.
# =========================================================================== #
import skills

ctx = skills.Ctx()
for name in ("click", "click_field", "type", "press", "scroll", "focus",
             "screen_text", "wait_for"):
    check(f"a skill can call ctx.{name}", callable(getattr(ctx, name, None)))

brief = skills.author_task("open chrome and fill in a form")
check("the skill brief TELLS the author these exist — without that Claude "
      "writes brittle inline AppleScript, which is what it did before",
      "NEO HAS HANDS" in brief and "ctx.click_field" in brief)
check("...and warns about the swallowed first click, which is the single "
      "most likely way an automation silently goes wrong",
      "unfocused" in brief and "focus" in brief)
check("...and says ok does not mean the page did what you wanted",
      "does NOT mean" in brief)

# =========================================================================== #
# 7. Live. Opt-in, because a suite that steals focus is a suite nobody runs.
# =========================================================================== #
if os.getenv("NEO_LIVE_CLICK") != "1":
    skip("live click checks", "set NEO_LIVE_CLICK=1 to run them")
else:
    tmp = tempfile.mkdtemp(prefix="neoact")
    page = os.path.join(tmp, "t.html")
    with open(page, "w") as f:
        f.write("""<!doctype html><meta charset=utf-8><title>act test</title>
<style>body{font:400 15px system-ui;padding:40px}
button{font:15px system-ui;padding:8px 16px;margin:8px 0;display:block}
#s{margin-top:24px;font-size:19px}</style>
<button onclick="document.getElementById('s').textContent='ALPHAPRESSED'">Alpha Control</button>
<button onclick="document.getElementById('s').textContent='BRAVOPRESSED'">Bravo Control</button>
<p>Message body</p><input id=i oninput="document.getElementById('e').textContent='ECHO '+this.value">
<div id=e></div><div id=s>UNPRESSED</div>""")
    subprocess.run(["open", "-a", "Google Chrome", page], capture_output=True)
    time.sleep(4)

    ok, why = act.click("Bravo Control", app="Google Chrome", log=lambda m: None)
    time.sleep(0.8)
    seen = act.screen_text()
    check(f"a real click on a real button registers ({why})",
          ok and "BRAVOPRESSED" in seen)
    check("and the button NEXT to it was not the one pressed",
          "ALPHAPRESSED" not in seen)

    ok, why = act.click_field("Message body", app="Google Chrome",
                              log=lambda m: None)
    check(f"an empty input is found from its label and proven to be one ({why})",
          ok)
    if ok:
        act.type_text("hello")
        time.sleep(0.7)
        check("...and typing lands in it",
              "ECHO hello" in act.screen_text())

    check("a target that is not on screen is reported, not approximated",
          act.click("Zebra Crossing Paperclip", log=lambda m: None)[0] is False)
    check("waiting for text that never appears gives up and says so",
          act.wait_for("zqq never on screen zqq", timeout=3) is False)
    check("waiting for text that IS there returns at once",
          act.wait_for("Bravo Control", timeout=6) is True)
    os.remove(page)
    os.rmdir(tmp)

print()
if SKIPPED:
    print(f"({len(SKIPPED)} skipped)")
if FAILED:
    print(f"{len(FAILED)} FAILED:")
    for n in FAILED:
        print("  - " + n)
    sys.exit(1)
print("Hands clean.")
