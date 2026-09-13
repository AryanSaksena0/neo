"""test_visuals.py — the answer panel decides what to draw, and when not to.

Every check here runs the real chooser against a question someone would
actually ask. Nothing opens a window: visuals.py is pure on purpose, so the
judgement calls can be argued with in a test instead of on screen.

The rule the whole file is defending: a component chosen badly is worse than no
picture, because the user believes what is on his screen. deck.py learned that
first — see the comment on choose_shape — and this is the same lesson applied
to single answers.

Run: python3 test_visuals.py
"""
# Suites live in tests/ but RUN from the repo root, so that the modules
# under test import and open("neo.py") still resolves. This makes the
# import work either way, so a suite can also be run directly.
import os as _bootstrap_os, sys as _bootstrap_sys
_bootstrap_sys.path.insert(0, _bootstrap_os.path.dirname(
    _bootstrap_os.path.dirname(_bootstrap_os.path.abspath(__file__))))

import sys

sys.path.insert(0, ".")
import visuals

FAILED = []


def check(name, cond):
    print(("PASS - " if cond else "FAIL - ") + name)
    if not cond:
        FAILED.append(name)


# =========================================================================== #
# 1. Is a picture warranted at all?
# =========================================================================== #
# The panel is only worth having if it stays away from ordinary talk. A visual
# over "what time is it" is the behaviour that makes people turn a feature off.
for q in ("what's the difference between a Roth and a traditional IRA",
          "how does a heap sort work",
          "walk me through the signup flow",
          "explain the stages of mitosis",
          "break down where our revenue came from",
          "compare Neon and Supabase"):
    check(f"wants a panel: {q!r}", visuals.wants_visual(q))

for q in ("what time is it",
          "set a timer for ten minutes",
          "play something by Drake",
          "open Chrome",
          "what's the weather tomorrow",
          "text Priya that I'm running late",
          "what's Apple trading at",
          "who won the game"):
    check(f"no panel for: {q!r}", not visuals.wants_visual(q))

# "how do I" is a real trap: it reads like an explanation and is very often a
# request to DO the thing. The _NEVER list wins over the score on purpose.
check("a doing-question beats its own explaining words",
      not visuals.wants_visual("how do I open Chrome"))


# =========================================================================== #
# 2. Which component
# =========================================================================== #
two = [{"label": "a", "detail": "x"}, {"label": "b", "detail": "y"}]
four = two + [{"label": "c"}, {"label": "d"}]

check("a difference question is a comparison",
      visuals.choose_kind("what's the difference between X and Y", two)
      == "compare")
check("a how-does question is a process",
      visuals.choose_kind("how does photosynthesis work", four) == "steps")
check("a history question is a timeline",
      visuals.choose_kind("the history of the transistor", four) == "timeline")
check("a what-is question with one item is a definition",
      visuals.choose_kind("what is a monad", [{"label": "a"}]) == "define")

nums = [{"label": "a", "value": "41%"}, {"label": "b", "value": "28%"},
        {"label": "c", "value": "31%"}]
check("numbers that sum to a whole are a breakdown",
      visuals.choose_kind("where did the signups come from", nums)
      == "breakdown")
check("levelled items are a hierarchy whatever the words say",
      visuals.choose_kind("tell me about the org", [
          {"label": "CEO"}, {"label": "Eng", "level": 1},
          {"label": "Web", "level": 2}]) == "hierarchy")


# ---- the vetoes: a component that cannot render the data must not win ----
check("one item is never a comparison",
      visuals.choose_kind("difference between X and Y",
                          [{"label": "only one"}]) != "compare")
check("six items is never a two-to-four comparison",
      visuals.choose_kind("compare these", [{"label": str(i)} for i in range(6)])
      != "compare")
check("no numbers means no proportion bars",
      visuals.choose_kind("break down the parts", two) != "breakdown")


# =========================================================================== #
# 3. Parsing what the model actually sends
# =========================================================================== #
piped = visuals.parse_items(
    "Build the heap | sift every parent down\n"
    "Swap the root | the largest moves to the end")
check("pipes parse into label and detail",
      len(piped) == 2 and piped[0]["label"] == "Build the heap"
      and piped[0]["detail"] == "sift every parent down")

check("a value is the third field",
      visuals.parse_items("Ambassadors | best channel | 41%")[0]["value"]
      == "41%")

check("JSON parses too — it is what the model reaches for first",
      visuals.parse_items('[{"label":"A","detail":"first"}]')[0]["label"] == "A")

check("a fenced JSON block still parses",
      visuals.parse_items('```json\n[{"label":"A"}]\n```')[0]["label"] == "A")

# Numbering is drawn by the component. Two sets of numbers looks like a bug,
# and models add "1." unprompted about half the time.
check("the model's own numbering is stripped",
      visuals.parse_items("1. First thing\n2. Second thing")[0]["label"]
      == "First thing")
check("bullets are stripped too",
      visuals.parse_items("- First\n• Second")[0]["label"] == "First")

# Markdown must never reach the screen: literal asterisks are the clearest
# possible tell that something was pasted rather than designed.
check("markdown is stripped, not rendered",
      "*" not in visuals.parse_items("**Bold** thing | `code`")[0]["label"])

check("a runaway paragraph is capped, not allowed to break the layout",
      len(visuals.parse_items("x" * 500)[0]["label"]) <= visuals.MAX_LABEL)

check("more items than fit are dropped, not squeezed",
      len(visuals.parse_items("\n".join(f"item {i}" for i in range(40))))
      == visuals.MAX_ITEMS)

check("junk parses to nothing rather than raising",
      visuals.parse_items(None) == [] and visuals.parse_items("") == []
      and visuals.parse_items("{{{{") != None)


# =========================================================================== #
# 4. build(): the finished spec, or honestly nothing
# =========================================================================== #
check("no items means no panel — never an empty card",
      visuals.build("A title", "") is None)
check("no title means no panel",
      visuals.build("", "a | b\nc | d") is None)

spec = visuals.build("Roth vs traditional", "Roth | tax-free later\n"
                                            "Traditional | deducted now")
check("a comparison builds", spec and spec["kind"] == "compare"
      and len(spec["items"]) == 2)

# The model naming a component is a suggestion, not a command: it does not
# know whether the data can carry it.
forced = visuals.build("One thing", "only item here", kind="compare")
check("a model-named component that the data can't carry is replaced",
      forced and forced["kind"] != "compare")

kept = visuals.build("How it runs", "First | a\nSecond | b\nThird | c",
                     kind="steps")
check("a model-named component the data CAN carry is honoured",
      kept and kept["kind"] == "steps")

check("a nonsense component name falls back instead of failing",
      (visuals.build("X", "a | b\nc | d", kind="hologram") or {}).get("kind")
      in visuals.KINDS)

# Bars need a width. Raw counts get normalised against the largest, because a
# bar chart of absolute numbers with no axis is decoration, not information.
bars = visuals.build("Signups", "Ambassadors | | 400\nSearch | | 200\n"
                                "TikTok | | 100", kind="breakdown")
check("a breakdown gets percentage widths for every bar",
      bars and all("pct" in i for i in bars["items"]))
check("...normalised so the largest fills the bar",
      bars and bars["items"][0]["pct"] == 100 and bars["items"][1]["pct"] == 50)

pcts = visuals.build("Split", "A | | 40%\nB | | 35%\nC | | 25%",
                     kind="breakdown")
check("...but real percentages are left exactly as they are",
      pcts and [i["pct"] for i in pcts["items"]] == [40, 35, 25])

check("every kind in the catalogue is one the panel can draw",
      set(visuals.KINDS) >= set(visuals.SAFE_KINDS))


# =========================================================================== #
# 5. The panel and the tool are actually wired to each other
# =========================================================================== #
import agent

check("show_visual is a tool the model can reach",
      any(t.__name__ == "show_visual" for t in agent.TOOLS))

_src = open("agent.py").read()
check("stop_showing clears the panel as well as the ring",
      "panel.clear()" in _src)

_panel_src = open("panel.py").read()
check("panel: every kind in the catalogue has a renderer",
      all(f"{k}:" in _panel_src for k in visuals.KINDS))
check("panel: model text is escaped before it reaches the DOM",
      "function esc(" in _panel_src and "innerHTML" in _panel_src)
check("panel: the window never eats a click",
      "setIgnoresMouseEvents_(True)" in _panel_src)
check("panel: its pusher class name is unique in the process",
      "_PanelPusher" in _panel_src
      and "_PanelPusher" not in open("hud.py").read())


# =========================================================================== #
# 6. The diffusion panel — 9 Sept, and why it looked broken
# =========================================================================== #
# What the user got: "High Concentration 80", then two rows with EMPTY grey bars
# and no number at all, then "Low Concentration 20". A breakdown with holes in
# it does not read as a design choice, it reads as a failure to load.
DIFFUSION = ("High Concentration | Molecules tightly packed | 80\n"
             "Gradient | Natural difference driving movement\n"
             "Random Movement | Constant motion without direction\n"
             "Low Concentration | Molecules spread out | 20")

check("bars: a breakdown needs a number on EVERY row, not just two of them",
      not visuals._fits("breakdown", visuals.parse_items(DIFFUSION)))
check("bars: ...so that panel is drawn as something else entirely",
      visuals.build("Simple diffusion", DIFFUSION, kind="breakdown")["kind"]
      != "breakdown")
check("bars: a real breakdown, where every row has its number, still works",
      visuals.build("Where signups came from",
                    "Ambassadors | | 41%\nSearch | | 28%\nTikTok | | 31%",
                    kind="breakdown")["kind"] == "breakdown")
check("bars: every bar it does draw has a width to draw",
      all(i.get("pct") is not None for i in
          visuals.build("Split", "A | | 40%\nB | | 35%\nC | | 25%",
                        kind="breakdown")["items"]))

# Same rule for stat tiles: a blank tile looks like a number that failed.
check("tiles: a stat row needs every figure, not some of them",
      not visuals._fits("stat", visuals.parse_items(
          "Users | | 412\nMRR | no number here\nChurn | | 3%")))


# ---- numbering things that have no order is a claim the content can't back --
# The fallback used to be `steps`, which numbers every row, so four concepts
# about diffusion came out as "1. High Concentration, 2. Gradient,
# 3. Random Movement" — a procedure that does not exist.
check("order: with no sequence in the words, nothing gets numbered",
      visuals.choose_kind("simple diffusion",
                          [{"label": "a"}, {"label": "b"}, {"label": "c"}])
      == "define")
check("order: a genuine process is still numbered",
      visuals.build("How a heap sort runs",
                    "Build the heap | x\nSwap the root | y\nRepeat | z"
                    )["kind"] == "steps")
check("order: ...however he phrases it",
      all(visuals.build(q, "One | x\nTwo | y\nThree | z")["kind"] == "steps"
          for q in ("how does photosynthesis work",
                    "how a heap sort runs",
                    "walk me through the signup flow",
                    "what happens when you press send")))

_panel = open("panel.py").read()
check("bars: the renderer refuses to draw a bar with no number behind it",
      "hasBar" in _panel and "i.pct !== undefined" in _panel)
check("bars: one accent colour — length is the information, not hue",
      "linear-gradient(90deg,var(--cyan),var(--amber))" not in _panel)
check("define: the default component has structure but no numbering",
      ".prop" in _panel and "pip" in _panel)


if FAILED:
    print(f"\n{len(FAILED)} FAILED:")
    for n in FAILED:
        print("  - " + n)
    sys.exit(1)
print("\nVisual answers clean.")
