"""test_deck_hard.py — the presentation pipeline, attacked rather than admired.

Every check here exists because a real defect was found by adversarial audit,
not because the code looked like it might want a test. Each one FAILS on the
code as it was this morning. Run: python3 test_deck_hard.py
"""
# Suites live in tests/ but RUN from the repo root, so that the modules
# under test import and open("neo.py") still resolves. This makes the
# import work either way, so a suite can also be run directly.
import os as _bootstrap_os, sys as _bootstrap_sys
_bootstrap_sys.path.insert(0, _bootstrap_os.path.dirname(
    _bootstrap_os.path.dirname(_bootstrap_os.path.abspath(__file__))))

import json, re as _re, sys, time, threading
sys.path.insert(0, ".")
import deck, providers

FAILED = []
def check(name, cond):
    print(("PASS - " if cond else "FAIL - ") + name)
    if not cond:
        FAILED.append(name)

# =========================================================================== #
# 1. Slide 0 was NEVER shown. Every deck. The title card held through the
#    whole first paragraph and then jumped to slide 2.
# =========================================================================== #
STEPS = [{"narration": "Atrial fibrillation is chaos in the upper chambers.", "visual": None},
         {"narration": "Because blood pools there, a clot can form.", "visual": None},
         {"narration": "That clot travels north and blocks an artery.", "visual": None}]

t = deck.Tracker(STEPS, min_dwell=0)
first = t.feed("Atrial fibrillation is")
check("slide 0 is announced as soon as Neo speaks", ("slide", 0) in first)

def walk(steps, chunk=3):
    tr = deck.Tracker(steps, min_dwell=0)
    seen = []
    for s in steps:
        w = s["narration"].split()
        for i in range(0, len(w), chunk):
            seen += [e for e in tr.feed(" ".join(w[i:i+chunk]))]
    return seen

order = [e[1] for e in walk(STEPS) if e[0] == "slide"]
check("every slide is reached, in order, exactly once",
      order == [0, 1, 2])
check("a 1-slide deck still shows its only slide",
      ("slide", 0) in deck.Tracker([{"narration": "Only step here.", "visual": None}], min_dwell=0).feed("Only step"))
check("an empty deck emits nothing rather than crashing",
      deck.Tracker([], min_dwell=0).feed("anything") == [])

# ---- the lead word: measured against the REAL transcript in neo.log -------- #
# Slides were consistently four to six words late, because cue_hit needs 55% of
# an eight-word cue before it fires. A step that opens on a word said nowhere
# earlier in the deck is proof on its own, and drops that to one word.
MACRO = [{"narration": "Every living thing is built from a handful of "
                       "ingredients called macromolecules.", "visual": None},
         {"narration": "Carbohydrates are the body's primary energy source, "
                       "from simple sugars like glucose.", "visual": None},
         {"narration": "Lipids are non-polar molecules for long-term storage "
                       "and cell membranes.", "visual": None},
         {"narration": "Proteins are diverse molecules performing nearly every "
                       "function in a cell.", "visual": None},
         {"narration": "Nucleic acids like DNA carry the genetic "
                       "instructions.", "visual": None},
         {"narration": "Together these four — carbohydrates, lipids, proteins "
                       "and nucleic acids — build every living thing.",
          "visual": None}]
_leads = [c["lead"] for c in deck.plan_cues(MACRO)]
check("a step opening on a word nobody has said yet gets a lead",
      _leads[1:5] == ["carbohydrates", "lipids", "proteins", "nucleic"])
check("a recap at the end does NOT disqualify the openings it repeats",
      _leads[5] == "together")
check("a step whose opening word was already said gets no lead",
      deck.plan_cues([{"narration": "Glucose is a sugar.", "visual": None},
                      {"narration": "Glucose is also fuel.", "visual": None}]
                     )[1]["lead"] == "")


def _lead_fires_on_the_first_word():
    """Feed the real narration a couple of words at a time and see how far past
    each step's own opening the slide change lands."""
    tr, said, lag = deck.Tracker(MACRO, min_dwell=0), 0, {}
    starts, n = [], 0
    for s in MACRO:
        starts.append(n)
        n += len(s["narration"].split())
    for s in MACRO:
        w = s["narration"].split()
        for i in range(0, len(w), 2):
            for ev in tr.feed(" ".join(w[i:i + 2])):
                if ev[0] == "slide":
                    lag[ev[1]] = said + len(w[i:i + 2]) - starts[ev[1]]
            said += len(w[i:i + 2])
    return sorted(lag) == [0, 1, 2, 3, 4, 5] and max(lag.values()) <= 2


check("every slide lands within two words of its own opening",
      _lead_fires_on_the_first_word())


def _lead_never_runs_ahead():
    """The failure worse than being late. A lead word said in an EARLIER step's
    narration must not pull its slide forward."""
    steps = [{"narration": "The heart has four chambers and pumps blood to the "
                           "lungs before it reaches the body.", "visual": None},
             {"narration": "Blood leaves through the aorta at high pressure.",
              "visual": None}]
    tr = deck.Tracker(steps, min_dwell=0)
    fired = []
    for w in steps[0]["narration"].split():
        fired += [e[1] for e in tr.feed(w) if e[0] == "slide"]
    # "blood" opens step 1 but step 0 says it twice, so it earns no lead and
    # step 1 must still be waiting when step 0 finishes.
    return fired == [0] and deck.plan_cues(steps)[1]["lead"] == ""


check("a word already spoken cannot pull the next slide forward",
      _lead_never_runs_ahead())

# =========================================================================== #
# 2. _clean_visual raised on ordinary model slips, and one bad field cost the
#    other two slides in the same batch their artwork.
# =========================================================================== #
MALFORMED = [
    {"kind": "figure", "labels": {"k": {"text": "a"}}},          # dict not list
    {"kind": "flow", "nodes": {"a": {"id": "a", "label": "A"},
                               "b": {"id": "b", "label": "B"}}},
    {"kind": "process", "steps": {"1": {"label": "x"}}},
    {"kind": "atom", "symbol": "C", "shells": 2},                 # int not list
    {"kind": "atom", "symbol": "C", "shells": [2, "banana", 4]},
    {"kind": "atom", "symbol": "C", "shells": [2], "protons": float("inf")},
    {"kind": "scene", "parts": [{"id": "a", "shape": "disc", "x": 5, "y": 5,
                                 "w": 5, "h": 5}],
     "beats": [{"set": [{"d": {}}]}]},                            # list not dict
    {"kind": "grid", "cols": [{"title": "A"}, {"title": "B"}],
     "rows": [{"label": "r", "cells": 3}, {"label": "q", "cells": ["1", "2"]}]},
    {"kind": "molecule", "atoms": [{"el": "C", "x": 1, "y": 1},
                                   {"el": "O", "x": 2, "y": 2}],
     "bonds": [{"from": 0, "to": 1, "order": True}]},             # True in (1,2,3)
    {"kind": "crosssection", "layers": "not a list"},
    {"kind": "versus", "left": "string", "right": None},
    {"kind": "svg", "svg": 12345},
    None, {}, [], "string", 42,
]
raised = []
for m in MALFORMED:
    try:
        deck._clean_visual(m)
    except Exception as e:
        raised.append((m if isinstance(m, dict) else type(m).__name__, type(e).__name__, str(e)[:60]))
check("no ordinary model slip makes _clean_visual raise", not raised)
if raised:
    for r in raised[:6]:
        print("     raised:", r[1], r[2])

check("a boolean bond order is not treated as order 1/2/3",
      deck._clean_visual({"kind": "molecule",
          "atoms": [{"el": "C", "x": 1, "y": 1}, {"el": "O", "x": 2, "y": 2}],
          "bonds": [{"from": 0, "to": 1, "order": True}]})["bonds"][0]["order"] == 1)

def _one_bad_visual_costs_one_slide():
    plan = {"title": "T", "slides": [{"narration": f"Step {i}.", "visual": None}
                                     for i in range(3)]}
    art = json.dumps({"visuals": [
        {"kind": "number", "value": "1", "label": "L", "sub": ""},
        {"kind": "flow", "nodes": {"a": {"id": "a"}, "b": {"id": "b"}}},   # poison
        {"kind": "number", "value": "3", "label": "L", "sub": ""}]})
    deck.merge_visuals(plan, art)
    got = [(s.get("visual") or {}).get("kind") for s in plan["slides"]]
    return got[0] == "number" and got[2] == "number"
check("one malformed visual does not take its neighbours down",
      _one_bad_visual_costs_one_slide())

# =========================================================================== #
# 2b. A TRUNCATED answer cost all three of its steps their artwork. Real log
#     line: `had no usable JSON (first 90 chars: '```json\n{\n  "visuals": [')`
#     — with two finished figures sitting inside the part that did arrive.
# =========================================================================== #
WHOLE = json.dumps({"visuals": [
    {"kind": "number", "value": "1", "label": "A", "sub": ""},
    {"kind": "number", "value": "2", "label": "B", "sub": ""},
    {"kind": "number", "value": "3", "label": "C", "sub": ""}]})
check("a whole answer still parses the ordinary way",
      [v["value"] for v in deck.loads_visuals(WHOLE)] == ["1", "2", "3"])
check("a fenced answer parses",
      len(deck.loads_visuals("```json\n" + WHOLE + "\n```")) == 3)
check("prose either side of the JSON parses",
      len(deck.loads_visuals("Sure! Here you go:\n" + WHOLE + "\nHope that helps.")) == 3)
check("a truncated answer keeps the figures that DID arrive",
      [v["value"] for v in deck.loads_visuals(WHOLE[:WHOLE.index('"3"')])]
      == ["1", "2"])
check("a truncated answer is worth more than nothing",
      len(deck.loads_visuals('```json\n{"visuals": [{"kind":"number","value":"1",'
                             '"label":"A","sub":""},{"kind":"number","valu')) == 1)
check("a '}' inside a drawn path does not end the object early",
      len(deck.loads_visuals('{"visuals":[{"kind":"svg","svg":"<text>a } b</text>",'
                             '"caption":"x"}]}')) == 1)
check("a salvaged batch still lands on the right steps",
      (lambda p: (deck.merge_visuals(p, WHOLE[:WHOLE.index('"3"')]),
                  [(s.get("visual") or {}).get("value") for s in p["slides"]])[1]
       == ["1", "2", None])({"title": "T", "slides": [
           {"narration": f"S{i}.", "visual": None} for i in range(3)]}))
check("nothing usable is still nothing, not a crash",
      deck.loads_visuals("I'm sorry, I can't help with that.") == []
      and deck.loads_visuals("") == [] and deck.loads_visuals(None) == [])

# =========================================================================== #
# 2c. chart — there was no graph shape at all, so "a number over time" had to
#     be hand-drawn as raw SVG, and that is the batch whose JSON came back
#     unparseable in the log.
# =========================================================================== #
CHART = {"kind": "chart", "chart": "line", "caption": "Share price",
         "x": {"label": "Year", "ticks": ["2024", "2025", "2026"]},
         "y": {"label": "$", "min": 0, "max": 70, "ticks": ["0", "35", "70"]},
         "series": [{"name": "CMG", "tone": "green",
                     "points": [[0, 52], [1, 69], [2, 37]]}],
         "notes": [{"x": 1, "y": 69, "text": "peak", "say": "an all-time high"}]}
_c = deck.safe_visual(CHART)
check("a chart survives cleaning with its data intact",
      _c and _c["series"][0]["points"] == [(0.0, 52.0), (1.0, 69.0), (2.0, 37.0)])
check("chart annotations become cued callouts",
      deck.plan_cues([{"narration": "It hit an all-time high in June.",
                       "visual": _c}])[0]["callouts"][0]["text"] == "peak")
_cp = deck._chart(_c)
check("a chart renders real axes and a plotted line",
      'class="plot"' in _cp and 'class="axis"' in _cp and "2025" in _cp)


def _finite(svg):
    """Every number in the drawn markup is a real number.

    A NaN coordinate is the failure that shows as a blank frame with no error
    anywhere: the browser drops the attribute and draws nothing. Checked on the
    chart's own svg, not the whole page — the page CSS legitimately contains
    the word 'infinite'.
    """
    return not _re.search(r'="[^"]*(nan|inf|NaN|Infinity)', svg)


check("no chart coordinate is ever nan or inf", _finite(_cp))
BAD_CHARTS = [
    {"kind": "chart"},                                   # nothing at all
    {"kind": "chart", "series": "not a list"},
    {"kind": "chart", "series": [{"points": "nope"}]},
    {"kind": "chart", "series": [{"points": [[0, float("nan")], [1, 2]]}]},
    {"kind": "chart", "series": [{"points": [[0, float("inf")], [1, 2]]}]},
    {"kind": "chart", "series": [{"points": [[0, 5], [1, 5]]}]},   # dead flat
    {"kind": "chart", "series": [{"points": [[3, 5], [3, 9]]}]},   # zero x range
    {"kind": "chart", "chart": "bar", "series": [{"points": [[0, 1]]}]},
    {"kind": "chart", "series": [{"points": [{"x": 1, "y": 2}, {"x": 2, "y": 3}]}],
     "y": {"ticks": {"a": "b"}}, "x": {"ticks": [{"no": 1}]}},
]
_craised, _cpages = [], []
for c in BAD_CHARTS:
    try:
        cc = deck._clean_visual(c)
        if cc:
            _cpages.append(deck._chart(cc))
    except Exception as e:
        _craised.append((c.get("chart"), type(e).__name__, str(e)[:60]))
check("no malformed chart makes the cleaner raise", not _craised)
if _craised:
    for r in _craised[:4]:
        print("     raised:", r)
check("a flat or degenerate chart still draws finite coordinates",
      bool(_cpages) and all(_finite(p) for p in _cpages))


def _ticks(svg):
    return _re.findall(r'class="tick"[^>]*>([^<]*)<', svg)


def _tick_y(svg, label):
    m = _re.search(r'class="tick" x="[-\d.]+" y="([\d.]+)"[^>]*>' + label + "<", svg)
    return float(m.group(1)) if m else None


# A chart that contradicts itself is worse than an ugly one: a 5k bar drawn
# above a gridline labelled 4k is a chart that lies about its own numbers.
OVER = deck.safe_visual({"kind": "chart", "chart": "bar",
                         "y": {"ticks": ["0", "2k", "4k"]},
                         "x": {"ticks": ["Jun", "Jul", "Aug"]},
                         "series": [{"name": "S", "points": [[0, 1200], [1, 2600],
                                                             [2, 5000]]}]})
_over = deck._chart(OVER)
check("a value past the last tick widens the axis instead of overflowing it",
      _tick_y(_over, "4k") is not None and _tick_y(_over, "4k") > 100)
check("numeric ticks are drawn at their value, not at their place in the list",
      abs((_tick_y(_over, "0") - _tick_y(_over, "2k"))
          - (_tick_y(_over, "2k") - _tick_y(_over, "4k"))) < 1.0)
check("a tick label parses out of the way people write them",
      [deck._tick_value(t) for t in ("0", "2k", "$35", "12%", "1,200", "3.5M",
                                     "Jun", "")]
      == [0.0, 2000.0, 35.0, 12.0, 1200.0, 3500000.0, None, None])
check("an axis with no ticks gets round numbers, not fifths of the data",
      _ticks(deck._chart(deck.safe_visual(
          {"kind": "chart", "series": [{"name": "x",
                                        "points": [[0, 0], [1, 3100]]}]})))
      == ["0", "1k", "2k", "3k", "4k"])
def _bars_sit_under_their_ticks():
    """Bars were laid out on a private "slot" scale while the tick labels were
    spread edge to edge, so every bar but the middle one stood beside the month
    it belonged to.

    The leading space in ` x="` matters: `[^>]*x="` matches the `x` of `rx="3"`
    further along the same tag, which is how this check first "passed" against
    a corner radius.
    """
    xs = [float(v) for v in _re.findall(r'class="bar"[^>]* x="([\d.]+)"', _over)]
    ws = [float(v) for v in _re.findall(r'class="bar"[^>]* width="([\d.]+)"', _over)]
    centres = [x + w / 2 for x, w in zip(xs, ws)]
    # The category ticks are the row that shares a y — picking them by a
    # hardcoded y matched a value tick at y=548 as well.
    rows = {}
    for x, y in _re.findall(r'class="tick" x="([\d.]+)" y="([\d.]+)"', _over):
        rows.setdefault(y, []).append(float(x))
    ticks = max(rows.values(), key=len) if rows else []
    return (len(centres) == 3 and len(ticks) == 3
            and all(abs(c - t) < 2.0 for c, t in zip(centres, sorted(ticks))))


check("bars line up with the ticks under them", _bars_sit_under_their_ticks())
check("a chart with two series names them without drawing on the plot",
      deck._chart(deck.safe_visual(
          {"kind": "chart", "y": {"ticks": ["0", "10"]},
           "series": [{"name": "A", "points": [[0, 1], [1, 9]]},
                      {"name": "B", "points": [[0, 2], [1, 4]]}]})).count(
          'class="legend"') == 2)

# =========================================================================== #
# 3. dress(): batching, refusals, one shared budget
# =========================================================================== #
class _R:
    def __init__(s, t, fr="STOP"):
        s.text = t
        s.candidates = [type("C", (), {"finish_reason": fr})()]

def _fake(behaviour):
    import re as _re
    class _M:
        def generate_content(s, model=None, contents="", **kw):
            nums = [int(x) for x in _re.findall(r"^(\d+)\. ", contents, _re.M)]
            return behaviour(model, nums[0] if nums else 0, len(nums))
    return type("C", (), {"models": _M()})()

_saved = providers.resolve
providers.resolve = lambda kind, client, log=None: ("gemini", "heavy" if kind == "heavy" else "chat")

def art(off, n):
    return json.dumps({"visuals": [{"kind": "number", "value": str(off + i),
                                    "label": "L", "sub": ""} for i in range(n)]})

for n in (1, 2, 3, 4, 7, 12):
    plan = {"title": "T", "slides": [{"narration": f"Step {i} words.", "visual": None}
                                     for i in range(n)]}
    d, _ = deck.dress(plan, _fake(lambda m, o, c: _R(art(o, c))), log=lambda m: None)
    vals = [(s.get("visual") or {}).get("value") for s in d["slides"]]
    check(f"{n:>2} slides: every batch lands on the right steps",
          vals == [str(i) for i in range(n)])

def _refusal_falls_through():
    calls = []
    def b(model, off, cnt):
        calls.append(model)
        if model == "heavy":
            return _R("I'm sorry, I can't help with creating medical diagrams.")
        return _R(art(off, cnt))
    plan = {"title": "T", "slides": [{"narration": "a", "visual": None}]}
    d, _ = deck.dress(plan, _fake(b), log=lambda m: None)
    return "chat" in calls and (d["slides"][0].get("visual") or {}).get("kind") == "number"
check("a refusal is a failure, so the second model is actually tried",
      _refusal_falls_through())

def _a_down_model_is_reported_so_it_stops_being_picked():
    """A 503 on the heavy model was logged and forgotten. providers.py kept it
    cached as the heavy model, so the NEXT presentation opened with the same
    failed round trip, and the one after that. models.json is only re-probed
    when something says the pick stopped working — and nothing here ever did.
    """
    told = []
    saved_report = providers.report_failure
    providers.report_failure = lambda job, prov, model, log=None: told.append(model)

    class _Boom:
        def generate_content(s, model=None, contents="", **kw):
            raise RuntimeError("503 UNAVAILABLE: the model is overloaded")

    try:
        out = deck.visuals_batch({"title": "T", "slides": [{"narration": "a"}]},
                                 type("C", (), {"models": _Boom()})(),
                                 "gemini-3.7-flash", 0, log=lambda m: None)
    finally:
        providers.report_failure = saved_report
    return out is None and "gemini-3.7-flash" in told


check("a model that 503s is reported, so the next deck doesn't repeat it",
      _a_down_model_is_reported_so_it_stops_being_picked())
check("a refusal is NOT reported as the model being down",
      not deck.model_is_down(ValueError("I can't help with that request"))
      and deck.model_is_down(RuntimeError("503 UNAVAILABLE"))
      and deck.model_is_down(RuntimeError("429 RESOURCE_EXHAUSTED")))


# =========================================================================== #
# 3b. Groq was in the candidate lists but NOTHING except ears.py could call it.
#     Every text job handed the model id to client.models.generate_content —
#     i.e. asked Gemini for a model called "openai/gpt-oss-120b", a guaranteed
#     404. Gemini's free tier is the binding constraint, so a fallback that
#     cannot actually be reached is worse than no fallback: it looks like one.
# =========================================================================== #
def _groq_is_actually_callable():
    import providers as _pv
    sent = {}

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"choices": [{"message": {"content": '{"visuals":[]}'},
                                 "finish_reason": "stop"}]}

    import requests as _rq
    saved = _rq.post

    def fake_post(url, headers=None, json=None, timeout=None):
        sent.update(url=url, body=json)
        return _Resp()

    _rq.post = fake_post
    try:
        r = _pv.generate(None, "groq", "openai/gpt-oss-120b", "draw me a thing",
                         max_output_tokens=4096)
    finally:
        _rq.post = saved
    return (sent.get("url", "").startswith("https://api.groq.com")
            and sent["body"]["model"] == "openai/gpt-oss-120b"
            and sent["body"]["max_tokens"] == 4096
            and r.text == '{"visuals":[]}'
            and r.candidates[0].finish_reason == "STOP")


check("a groq model is actually reachable, not just listed",
      _groq_is_actually_callable())


def _the_provider_travels_with_the_model():
    """resolve() returns (provider, model) and BOTH have to reach the call. A
    deck that resolved to Groq and then dialled Gemini is the whole bug."""
    import inspect as _i
    src = _i.getsource(deck.dress) + _i.getsource(deck.build)
    return ("providers.generate" in _i.getsource(deck.visuals_batch)
            and "provider=provider" in src
            and "client.models.generate_content" not in src)


check("the resolved provider reaches the call, not just the model name",
      _the_provider_travels_with_the_model())


# =========================================================================== #
# 3c. THE PIPELINE TIMED OUT, which is why every slide was plain text.
#     20:18:50 batch 0 -> 3.7-flash 503 (17s). 20:19:17 batch 3 -> 504
#     DEADLINE_EXCEEDED (44s). 20:19:23 budget expired, "no visuals". 20:19:27
#     a batch SUCCEEDED and was thrown away, four seconds late.
# =========================================================================== #
check("one figure per call, not three — three is what times out",
      deck.VISUALS_BATCH == 1)
check("the output ceiling is a figure's worth, not a target to fill",
      2_000 <= deck.MAX_OUTPUT_TOKENS <= 20_000)
check("the model that actually answers is tried first",
      _re.search(r'\(\("chat", chat\), \("heavy", heavy\)\)',
                 __import__("inspect").getsource(deck.dress)) is not None)


def _each_figure_lands_as_it_arrives():
    """Figures used to be held until every batch was done, so one slow call
    cost the whole deck its artwork. Each one must reach the screen alone."""
    import inspect as _i
    src = _i.getsource(deck.dress)
    return "on_step(offset, text)" in src and "on_step=None" in src


check("a figure reaches the screen the moment it exists",
      _each_figure_lands_as_it_arrives())


def _a_finished_walkthrough_closes_itself():
    """It just sat there when Neo stopped talking — full screen, borderless,
    over everything, until it was dismissed by hand."""
    steps = [{"narration": "First step words here now.", "visual": None},
             {"narration": "Second and final step words.", "visual": None}]
    p = deck.Presentation(deck.fallback_visuals(json.loads(json.dumps(
        {"title": "T", "slides": steps}))), log=lambda m: None, min_dwell=0)
    before = p.finished()
    for s in steps:
        p.feed(s["narration"], lag=0.0)     # nothing queued: heard as spoken
    done_when_heard = p.finished()

    # And the other half: with speech still queued the deck must NOT consider
    # itself finished. The tracker runs on transcript time, about three times
    # ahead of the voice, so closing on it alone took the window away while
    # the user was still listening to the last slide.
    q = deck.Presentation(deck.fallback_visuals(json.loads(json.dumps(
        {"title": "T", "slides": steps}))), log=lambda m: None, min_dwell=0)
    for s in steps:
        q.feed(s["narration"], lag=8.0)     # eight seconds still to be spoken
    still_talking = not q.finished()
    q.close()
    return (not before) and done_when_heard and still_talking


check("a walkthrough that has been walked through reports itself finished",
      _a_finished_walkthrough_closes_itself())
check("and there is a linger before it goes, not an abrupt cut",
      2.0 <= deck.LINGER_S <= 20.0)


def _the_screen_never_recites_the_script():
    """You can read or you can listen. Printing the narration while speaking it
    is what made a whole presentation read as text on a background."""
    narration = ("DNA contains the sugar deoxyribose and the base thymine, "
                 "while RNA contains ribose and uracil.")
    card = deck._key_line(narration)
    return (len(card.split()) <= deck.KEYWORD_LIMIT
            and card.lower() not in narration.lower()
            and "deoxyribose" in card.lower())


check("the no-figure card is a label, never a line of the script",
      _the_screen_never_recites_the_script())


# =========================================================================== #
# 3d. HE ASKED A QUESTION AND HEARD NOTHING AT ALL. Straight from neo.log:
#       20:31:33  You: Can you give me a presentation on ... Doomsday?
#       20:31:34  [live] search_web
#       20:31:35  [voice] no cached line for 'lookup' yet — staying quiet
#       20:31:46  [live] present
#       20:31:58  [live] no answer came back — resetting the session.
#     Two separate bugs, both introduced by the previous round's "fixes".
# =========================================================================== #
import live as _live


def _a_running_tool_is_not_a_dead_socket():
    """Twelve seconds is the budget for "the socket answered nothing". It is
    not the budget for "Neo is building a presentation" — and killing the
    session mid-tool is silent from outside: the question just never gets
    answered."""
    # Written against the CONSTANTS, not a hardcoded twenty seconds. The idle
    # budget was raised from 12s to 28s when a real question sat silent for 13
    # and got its session killed — and this check, pinned to 20, then failed
    # for a change that was correct.
    # There are TWO thresholds now, and the split is the point: answer_slow
    # means "tell him it is taking a while", answer_overdue means "the socket
    # is dead". A thinking model and a wedged socket are both silence, so the
    # first one must never end anything.
    now = 1000.0
    slow_at = now - (_live.ANSWER_TIMEOUT_S + 1)
    dead_at = now - (_live.ANSWER_HARD_S + 1)
    tool_slow = now - (_live.TOOL_ANSWER_TIMEOUT_S + 1)
    return (_live.ANSWER_TIMEOUT_S < _live.ANSWER_HARD_S
            # slow enough to mention, nowhere near dead
            and _live.answer_slow(slow_at, now)
            and not _live.answer_overdue(slow_at, now)
            # a running tool is not even worth mentioning that early
            and not _live.answer_slow(slow_at, now, in_tool=True)
            and _live.answer_slow(tool_slow, now, in_tool=True)
            # only the hard ceiling ends anything
            and _live.answer_overdue(dead_at, now)
            and not _live.answer_overdue(0, now)
            and not _live.answer_slow(0, now))


check("a tool that takes a while is not mistaken for a wedged socket",
      _a_running_tool_is_not_a_dead_socket())
check("the tool budget is longer than the idle one, and still bounded",
      _live.ANSWER_TIMEOUT_S < _live.TOOL_ANSWER_TIMEOUT_S <= 180)


def _the_in_flight_count_cannot_leak():
    """Leaking upward disables the watchdog forever; leaking downward re-arms
    it mid-tool. Both are silent."""
    import inspect as _i
    src = _i.getsource(_live.LiveSession._handle_tools)
    return "finally:" in src and "self._tools_running = max(0" in src


check("the in-flight tool count is released even when the turn raises",
      _the_in_flight_count_cannot_leak())


def _neo_never_works_in_total_silence():
    """The filler cache was built before the Gemini client existed, so all 53
    lines failed — and "stay silent rather than use the wrong voice" turned a
    cosmetic problem into no sound at all."""
    import inspect as _i, neo as _neo
    ack = _i.getsource(_neo.Neo._ack)
    boot = _i.getsource(_neo.Neo.run) if hasattr(_neo.Neo, "run") else ""
    warm = _i.getsource(_neo.Speech.warmup)
    # A CALL, not a mention: warmup's comment explains where the cache is
    # built now, and grepping the raw text matched that comment.
    import ast as _ast, textwrap as _tw
    calls = [n for n in _ast.walk(_ast.parse(_tw.dedent(warm)))
             if isinstance(n, _ast.Call)
             and getattr(n.func, "attr", "") == "_recache"]
    return "self._speak(text)" in ack and not calls


check("a missing filler line falls back to a voice, never to silence",
      _neo_never_works_in_total_silence())


# =========================================================================== #
# 3e. Three failures in one run, all visible on screen or in the log.
# =========================================================================== #
def _a_figure_without_a_picture_does_not_ask_for_one():
    """_figure emits <image href="/img/N"> based on whether the MODEL wrote a
    search string, not on whether that search found anything. A failed fetch
    therefore rendered an empty frame with labels pointing into it — the blank
    slab captioned "Spinal canal" and "Spinal cord"."""
    import inspect as _i
    src = _i.getsource(deck.dress)
    return 'v.pop("image", None)' in src and "i not in images" in src


check("a figure whose picture never arrived is drawn, not left blank",
      _a_figure_without_a_picture_does_not_ask_for_one())
check("an icon-sized download is rejected as the placeholder it is",
      deck.IMAGE_MIN_BYTES >= 8000)
check("commons' own placeholder files are ranked out",
      all(w in deck._AVOID for w in ("question book", "placeholder", "no image")))

# The cap that cut Neo off mid-sentence. The log ends at "Neo: Critically,"
# with no error and no turn_complete — a turn hitting its output ceiling is
# silent by design, and the ceiling was never set at all.
import live as _lv


def _the_live_turn_has_room_to_finish():
    import inspect as _i
    src = _i.getsource(_lv.LiveSession._config)
    return "max_output_tokens" in src


check("a spoken turn is given room to finish, not the server default",
      _the_live_turn_has_room_to_finish())
check("and losing that field degrades the session instead of killing it",
      any("max_output_tokens" in fields for _n, fields in _lv.LiveSession.TIERS))

# The wrong voice. 53 lines through a free TTS tier is a wall of 429s, and
# every line that fails falls back to the local voice.
import banter as _b


def _the_cloud_voice_list_fits_the_quota():
    """The list may now be LONG, and that is deliberate.

    Capping it at sixteen was the old defence against the wrong voice: only
    cached lines sound like Neo, so a long list meant most fillers fell through
    to the local engine. But capping it also meant Neo had two phrases per
    bucket and sounded like a catchphrase machine — which is the complaint that
    replaced it.

    The defence moved into pick(), which will only ever return a line that IS
    cached. So the list is free to be long and fill in over days, and the thing
    worth asserting is no longer its length — it is that nothing outside the
    cache can ever be spoken. That is checked directly below.
    """
    phrases = _b.all_phrases()
    return (len(set(phrases)) == len(phrases)          # no line paid for twice
            # still at least one line per bucket, or a whole category of
            # acknowledgement has no cloud voice at all
            and all(any(p in phrases for p in lines)
                    for lines in _b.ACKS.values()))


def _pick_never_leaves_the_cache():
    """The real guarantee: a filler is never spoken in the wrong voice."""
    for bucket, lines in _b.ACKS.items():
        cached = set(lines[:2])                        # a deliberately thin cache
        for _ in range(80):
            if _b.pick(bucket, available=cached) not in cached:
                return False
    # ...and with an EMPTY cache it still says something, because silence
    # while Neo is working is indistinguishable from Neo being broken.
    return all(_b.pick(b, available=set()) for b in _b.ACKS)


check("a filler is NEVER spoken in a voice it was not synthesised in",
      _pick_never_leaves_the_cache())
check("the cloud voice list has no duplicates and covers every bucket",
      _the_cloud_voice_list_fits_the_quota())


# =========================================================================== #
# 3f. THE TRANSCRIPT IS NOT THE VOICE. This is the assumption the whole sync
#     was built on, and it was wrong. From neo.log, the Chipotle deck:
#       21:05:06  Neo: cumbersome.
#       21:05:14  Neo: center stage.
#     ~70 words in 8 seconds. Seventy words take about 28 seconds to SAY.
#     Gemini streams output_transcription as the model GENERATES text, roughly
#     3x faster than it can be spoken — so the deck ran the whole walkthrough
#     in twenty seconds while the voice was still on slide two.
# =========================================================================== #
def _slides_follow_the_voice_not_the_transcript():
    steps = [{"narration": "Chipotle shares had climbed past three thousand "
                           "dollars, making ordinary trading cumbersome.",
              "visual": None},
             {"narration": "To fix this, Chipotle executed a historic fifty "
                           "for one stock split in June.", "visual": None},
             {"narration": "Every existing share was divided into fifty "
                           "smaller pieces at roughly sixty dollars.",
              "visual": None},
             {"narration": "In the months following, the stock saw heightened "
                           "volatility as excitement settled.", "visual": None}]
    p = deck.Presentation({"title": "T", "slides": deck.fallback_visuals(
        {"slides": json.loads(json.dumps(steps))})["slides"]},
        log=lambda m: None, min_dwell=0)
    seen = []
    t0 = time.time()
    p.push = lambda m: seen.append((time.time() - t0, m.get("t"), m.get("i")))

    SPOKEN_WPS = 25.0            # 2.5 words/sec, sped up 10x for the test
    lag_words, last = 0.0, t0
    for s in steps:
        for w in s["narration"].split():
            now = time.time()
            lag_words = max(0.0, lag_words - (now - last) * SPOKEN_WPS) + 1
            last = now
            p.feed(w, lag=lag_words / SPOKEN_WPS)
            time.sleep(1 / 75.0)          # transcript runs 3x ahead of speech
    generated_at = time.time() - t0
    time.sleep(3.0)
    slides = [(t, i) for t, k, i in seen if k == "slide"]
    p.close()
    order = [i for _t, i in slides]
    # Every slide, in order, and the last one lands AFTER the transcript ran
    # out — i.e. it is tracking the voice, not the text stream.
    return (order == [0, 1, 2, 3]
            and slides[-1][0] > generated_at
            and all(b >= a for (a, _x), (b, _y) in zip(slides, slides[1:])))


check("slides follow the voice, not the transcript racing ahead of it",
      _slides_follow_the_voice_not_the_transcript())
check("the playback buffer is what measures the gap",
      hasattr(_lv.Playback, "lag_seconds")
      and abs(_lv.Playback.lag_seconds.__doc__ is not None) >= 0)
check("a slide is never held back indefinitely by a stalled speaker",
      5.0 <= deck.MAX_SYNC_LAG_S <= 60.0)


def _the_drift_fallback_cannot_walk_the_whole_deck():
    """The timeout that advances a step whose cue never matched must not
    cascade. Off-script narration used to walk every remaining slide in one or
    two feeds — which is the other half of "it ran through everything"."""
    steps = [{"narration": f"Step {i} has its own distinct opening words here.",
              "visual": None} for i in range(6)]
    tr = deck.Tracker(steps, min_dwell=0)
    # Nothing matching any cue, far more words than any single step's budget.
    fired = [e[1] for e in tr.feed(" ".join(["blah"] * 400)) if e[0] == "slide"]
    return len(fired) <= 2


check("an off-script narration cannot walk the deck to the end in one go",
      _the_drift_fallback_cannot_walk_the_whole_deck())
check("the drift timeout is generous — late is survivable, early is not",
      deck.DRIFT_TOLERANCE >= 2.0)


# =========================================================================== #
# 3g. THE CONVERSATION HUNG UP WHILE THE DECK WAS BUILDING. Verbatim:
#       21:32:59  Neo: "Give me a few seconds..."
#       21:33:33  [live] quiet for a while — closing the session.
#       21:34:26  [deck] on screen, finished: 6/6 drawn
#       21:34:26  the deck is ready but the conversation has ended
#     A build puts no traffic on the socket, so the idle timer counted it as
#     silence. Perfect deck, nobody left to narrate it, dark orb.
# =========================================================================== #
def _a_build_keeps_the_conversation_open():
    s = _lv.LiveSession.__new__(_lv.LiveSession)
    s._busy = 0
    s._last_activity = time.time() - 999      # long past any idle timeout
    s.log = lambda m: None
    before = _lv.should_hang_up(s._last_activity, time.time(), 30)
    s.hold("building")
    held = s.is_busy() and not _lv.should_hang_up(s._last_activity,
                                                  time.time(), 30)
    s.release("done")
    # Released, and idle timing restarts FROM NOW rather than from whenever the
    # socket last carried something.
    return (before and held and not s.is_busy()
            and not _lv.should_hang_up(s._last_activity, time.time(), 30))


check("a build in progress does not look like an idle conversation",
      _a_build_keeps_the_conversation_open())
check("the idle loop actually consults it",
      "self._busy" in __import__("inspect").getsource(_lv.LiveSession._idle_loop))
check("holds nest, so two overlapping jobs cannot release each other early",
      (lambda s: (s.hold(), s.hold(), s.release(), s.is_busy() and
                  (s.release(), not s.is_busy())[1])[-1])(
          type("S", (), {"_busy": 0, "_last_activity": 0.0,
                         "log": lambda self, m: None,
                         "hold": _lv.LiveSession.hold,
                         "release": _lv.LiveSession.release,
                         "is_busy": _lv.LiveSession.is_busy})()))


def _a_dead_model_is_not_retried_for_every_figure():
    """Six batches each burned 3-20s failing on the SAME 429/503/504 before
    falling back — a ~15s art phase became 51 seconds, which is what let the
    conversation time out. One failure is enough evidence."""
    import inspect as _i
    src = _i.getsource(deck.dress)
    return "dead = set()" in src and "if model in dead:" in src


check("a model that already failed this build is not tried again per figure",
      _a_dead_model_is_not_retried_for_every_figure())


def _quarantine_never_leaves_zero_models():
    """...but it must never quarantine the last one standing, or a single blip
    leaves the deck with nothing to draw with."""
    import inspect as _i
    return "len(models) - len(dead) > 1" in _i.getsource(deck.dress)


check("the quarantine never strands the deck with no model at all",
      _quarantine_never_leaves_zero_models())


def _truncation_is_named():
    msgs = []
    plan = {"title": "T", "slides": [{"narration": "a", "visual": None}]}
    deck.dress(plan, _fake(lambda m, o, c: _R('{"visuals":[{"kind":"num', "MAX_TOKENS")),
               log=msgs.append)
    return any("MAX_TOKEN" in m.upper() for m in msgs)
check("a truncated answer says so instead of vanishing", _truncation_is_named())

def _budget_is_shared_not_per_batch():
    def hang(model, off, cnt):
        time.sleep(30)
        return _R(art(off, cnt))
    saved = deck.ART_BUDGET_S
    deck.ART_BUDGET_S = 2.0
    try:
        plan = {"title": "T", "slides": [{"narration": f"S{i}.", "visual": None}
                                         for i in range(12)]}   # 4 batches
        t0 = time.time()
        deck.dress(plan, _fake(hang), log=lambda m: None)
        return (time.time() - t0) < 5.0        # was 4 x budget
    finally:
        deck.ART_BUDGET_S = saved
check("a hanging model costs ONE budget, not one per batch",
      _budget_is_shared_not_per_batch())
providers.resolve = _saved

# =========================================================================== #
# 4. sanitize_svg — correctness and safety
# =========================================================================== #
check("a '<' in prose does not eat the next close tag",
      "&lt; b" in deck.sanitize_svg('<text x="1">a < b</text><circle r="4"/>')
      and "</text>" in deck.sanitize_svg('<text x="1">a < b</text><circle r="4"/>'))
check("a '>' inside a quoted attribute keeps the element self-closed",
      deck.sanitize_svg('<circle data-part="a>b" r="3"/><rect width="2" height="2"/>')
      .count("<rect") == 1)
check("entities survive instead of being double-escaped",
      deck.sanitize_svg('<text>Na&#8594;K &amp; Cl&#8315;</text>') ==
      "<text>Na→K &amp; Cl⁻</text>")
check("an unwound tag closes as ITSELF, not as </g>",
      deck.sanitize_svg('<g data-part="a"><text x="1">hi</g><circle r="2"/>')
      == '<g data-part="a"><text x="1">hi</text></g><circle r="2" />')

ATTACKS = [
    '<svg onload=alert(1)><script>alert(1)</script><path d="M0,0"/></svg>',
    '<g><foreignObject><iframe src=x></iframe></foreignObject><circle r="3"/></g>',
    '<a href="javascript:alert(1)"><text>hi</text></a>',
    '<image href="http://evil.example/x.png"/><rect width="1" height="1"/>',
    '<circle onclick="steal()" r="1" style="x:url(http://e)"/>',
    '<use xlink:href="http://evil.example/#x"/><path d="M1,1"/>',
    '<style>*{background:url(http://e)}</style><path d="M0,0"/>',
    '< script>alert(1)</script ><path d="M1,1"/>',
    '<circle r="1" onmouseover=alert(1)>',
    '<path d="M0,0" fill="url(javascript:alert(1))"/>',
]
BANNED = ("script", "onload", "onclick", "onmouseover", "javascript:", "iframe",
          "foreignobject", "href", "style=", "<img", "<image")
leaks = [a for a in ATTACKS if any(b in deck.sanitize_svg(a).lower() for b in BANNED)]
check("every injection attempt is stripped", not leaks)
for l in leaks:
    print("     LEAK:", deck.sanitize_svg(l)[:70])

t0 = time.time()
for bomb in ("<!-- " * 18000, "<script " * 8000, "<image " * 11000,
             "<g>" * 3000, '<circle r="' * 5000):
    deck.sanitize_svg(bomb)
check(f"pathological input can't spin (5 bombs in {time.time()-t0:.2f}s)",
      time.time() - t0 < 5.0)
check("a 200KB input is bounded", len(deck.sanitize_svg("<path d='M0,0'/>" * 20000)) < 400_000)

# =========================================================================== #
# 5. Rendering can't break the page, whatever the model sends
# =========================================================================== #
HOSTILE = '"><\\/script><script>alert(1)</script><img src=x onerror=alert(1)>\'`${x}'
KINDS = [
    {"kind": "figure", "shape": "heart", "image": HOSTILE,
     "labels": [{"text": HOSTILE, "say": HOSTILE, "x": 5, "y": 5}]},
    {"kind": "flow", "nodes": [{"id": "a", "label": HOSTILE, "sub": HOSTILE, "x": 20, "y": 30},
                               {"id": "b", "label": HOSTILE, "sub": "", "x": 70, "y": 60}],
     "edges": [{"from": "a", "to": "b", "label": HOSTILE}]},
    {"kind": "crosssection", "style": "rings",
     "layers": [{"label": HOSTILE, "sub": HOSTILE}, {"label": HOSTILE, "sub": ""}]},
    {"kind": "process", "steps": [{"label": HOSTILE, "sub": HOSTILE},
                                  {"label": HOSTILE, "sub": ""}]},
    {"kind": "versus", "left": {"title": HOSTILE, "points": [HOSTILE]},
     "right": {"title": HOSTILE, "points": [HOSTILE]}},
    {"kind": "grid", "cols": [{"title": HOSTILE}, {"title": HOSTILE}],
     "rows": [{"label": HOSTILE, "cells": [HOSTILE, HOSTILE]},
              {"label": HOSTILE, "cells": [HOSTILE, HOSTILE]}]},
    {"kind": "molecule", "caption": HOSTILE,
     "atoms": [{"el": "C", "x": 30, "y": 50, "label": HOSTILE},
               {"el": "O", "x": 70, "y": 50, "lone": 2}],
     "bonds": [{"from": 0, "to": 1, "order": 2, "label": HOSTILE}]},
    {"kind": "atom", "symbol": "C", "name": HOSTILE, "shells": [2, 4]},
    {"kind": "scene", "stage": "body", "caption": HOSTILE,
     "parts": [{"id": "a", "shape": "tube", "x": 50, "y": 50, "w": 20, "h": 10,
                "label": HOSTILE}],
     "beats": [{"say": HOSTILE, "set": {"a": {"squash": 0.4}}, "note": HOSTILE}]},
    {"kind": "svg", "svg": '<g data-part="a"><path d="M1,1 L9,9" stroke="#fff"/></g>',
     "caption": HOSTILE,
     "beats": [{"say": HOSTILE, "set": {"a": {"dy": 3}}, "note": HOSTILE}]},
    {"kind": "number", "value": HOSTILE, "label": HOSTILE, "sub": HOSTILE},
    {"kind": "chart", "chart": "line", "caption": HOSTILE,
     "x": {"label": HOSTILE, "ticks": [HOSTILE, HOSTILE]},
     "y": {"label": HOSTILE, "ticks": [HOSTILE, HOSTILE]},
     "series": [{"name": HOSTILE, "tone": HOSTILE, "points": [[0, 1], [1, 4]]}],
     "notes": [{"x": 1, "y": 4, "text": HOSTILE, "say": HOSTILE}]},
    {"kind": "line", "text": HOSTILE},
]
cleaned = [deck.safe_visual(k) for k in KINDS]
check("every visual kind survives hostile text without being refused",
      sum(1 for c in cleaned if c) >= 10)
page = deck.render_html({"title": HOSTILE, "slides": [
    {"narration": HOSTILE, "visual": c} for c in cleaned if c]})
check("hostile text never opens a script tag",
      page.count("<script") == 1 and page.count("</script") == 1)
def _no_live_event_attribute():
    """A naive substring check is wrong: `onerror` legitimately appears INSIDE
    an escaped run (&lt;img src=x onerror=...&gt;), which is inert text. What
    matters is whether any real element carries an on* ATTRIBUTE."""
    from html.parser import HTMLParser
    found = []
    class P(HTMLParser):
        def handle_starttag(self, tag, attrs):
            found.extend(a for a, _ in attrs if a.lower().startswith("on"))
    p = P()
    p.feed(page)
    return not found
check("no element carries a live event attribute", _no_live_event_attribute())
check("hostile markup is escaped, never emitted raw",
      "&lt;img" in page and "<img" not in page.lower())
import re as _re
beats = _re.findall(r'data-beats="([^"]*)"', page)
import html as _h
ok = True
for b in beats:
    try:
        json.loads(_h.unescape(b))
    except Exception:
        ok = False
check(f"every data-beats attribute is still valid JSON ({len(beats)} found)", ok)

# =========================================================================== #
# 6. present(): one window, newest wins, gate never leaks
# =========================================================================== #
providers.resolve = lambda kind, client, log=None: ("gemini", "m")
deck.Presentation.open_window = lambda self: None
deck.collect_images = lambda plan, log=print, fetch=None: {}

def _newest_build_wins():
    """Ask for a second presentation while the first is still drawing.

    The invariant is about ORPHANS, not about opening exactly once. The window
    now opens on the plan and the artwork is swapped in afterwards, so asking
    twice legitimately opens two — what must never happen is a full-screen
    window left running that nothing can close, which is what the original
    defect was: both builds installed themselves, `_set` was unconditional, and
    close_presentation only ever closes `current`, so the loser survived for
    the life of the process.

    So: every presentation that was ever installed must end up closed except
    the survivor, and the survivor must be the NEWEST one.
    """
    import re as _re
    installed, calls = [], []
    real_install = deck._install

    def spy(p, gen):
        ok = real_install(p, gen)
        if ok:
            installed.append(p)
        return ok

    class _M:
        def generate_content(s, model=None, contents="", **kw):
            nums = [int(x) for x in _re.findall(r"^(\d+)\. ", contents, _re.M)]
            is_art = "kind" in contents          # the visuals prompt, not the plan
            calls.append("art" if is_art else "plan")
            if is_art and calls.count("art") == 1:
                time.sleep(1.6)                  # the FIRST build's art is slow
            if is_art:
                return _R(art(nums[0] if nums else 0, len(nums) or 1))
            return _R(json.dumps({"title": "T",
                                  "slides": [{"narration": "Step words here."}]}))

    client = type("C", (), {"models": _M()})()
    deck._install = spy
    try:
        deck.present("first topic", client, log=lambda m: None)
        time.sleep(0.2)
        deck.present("second topic", client, log=lambda m: None)
        time.sleep(3.0)
        alive = [p for p in installed if not p.closed]
        return (len(alive) <= 1
                and (not alive or alive[0] is installed[-1])
                and deck.current() in (None, installed[-1]))
    finally:
        deck._install = real_install
        deck.close()
check("a second request does not leave a first window orphaned",
      _newest_build_wins())


def _nothing_shows_until_it_is_all_ready():
    """The contract the user asked for, in his words: "I want the presentation to
    show up when everything is done, the text synced up, and Neo actually
    ready."

    And the failure that forced it — present() used to block while the plan was
    written, inside a live tool dispatch, so the websocket sat silent for
    nineteen seconds and the server hung up:

        21:19:52  could not return tool results: no close frame received
        21:19:52  session ended: APIError: 1006 abnormal closure

    The deck appeared and Neo never spoke. So present() must return at once,
    the window must open only when the figures are in, and the script must come
    back through on_ready AFTER that.
    """
    import re as _re
    events, script = [], []
    real_open = deck.Presentation.open_window
    real_replace = deck.Presentation.replace

    def spy_open(self):
        events.append("open")

    def spy_replace(self, d, images=None):
        events.append("art")
        return real_replace(self, d, images)

    class _M:
        def generate_content(s, model=None, contents="", **kw):
            nums = [int(x) for x in _re.findall(r"^(\d+)\. ", contents, _re.M)]
            if "kind" in contents:
                time.sleep(0.8)
                return _R(art(nums[0] if nums else 0, len(nums) or 1))
            time.sleep(0.8)               # the plan is slow too
            return _R(json.dumps({"title": "T", "slides": [
                {"narration": f"Step {i} words here."} for i in range(3)]}))

    deck.Presentation.open_window, deck.Presentation.replace = spy_open, spy_replace
    try:
        t0 = time.time()
        out = deck.present("a topic", type("C", (), {"models": _M()})(),
                           log=lambda m: None, on_ready=script.append)
        inline = time.time() - t0
        returned_at_once = inline < 0.4 and out is not None
        time.sleep(0.4)
        nothing_yet = "open" not in events      # still building, still hidden
        time.sleep(4.0)
        opened = "open" in events
        narrated = bool(script) and bool(script[0])
        # The window must not have opened before the artwork was folded in.
        clean = events.count("open") == 1
        return returned_at_once and nothing_yet and opened and narrated and clean
    finally:
        deck.Presentation.open_window = real_open
        deck.Presentation.replace = real_replace
        deck.close()


check("present() returns at once — a blocking tool kills the websocket",
      _nothing_shows_until_it_is_all_ready())


def _a_failed_build_still_tells_neo():
    """If the deck can't be built, on_ready must still fire — otherwise Neo
    waits forever for a script that is never coming and simply says nothing."""
    told = []

    class _Dead:
        def generate_content(s, **kw):
            raise RuntimeError("503 UNAVAILABLE")

    deck.present("x", type("C", (), {"models": _Dead()})(),
                 log=lambda m: None, on_ready=told.append)
    time.sleep(2.5)
    deck.close()
    return told == [None]


check("a build that fails still releases Neo instead of leaving him mute",
      _a_failed_build_still_tells_neo())


def _swap_carries_markup_not_a_reload():
    """`replace` must push the new markup. Pushing a bare {t:'deck'} makes the
    page call location.reload(), which blanks a full-screen window in the
    middle of a sentence — the exact seam the two-call design exists to hide.
    """
    plan = {"title": "T", "slides": [{"narration": "One.", "visual": None},
                                     {"narration": "Two.", "visual": None}]}
    p = deck.Presentation(deck.fallback_visuals(json.loads(json.dumps(plan))),
                          log=lambda m: None)
    sent = []
    p.push = lambda m: sent.append(m)
    dressed = json.loads(json.dumps(plan))
    dressed["slides"][0]["visual"] = {"kind": "number", "value": "1",
                                      "label": "L", "sub": ""}
    deck.fallback_visuals(dressed)
    p.replace(dressed, {0: b"x"})
    deck_msgs = [m for m in sent if m.get("t") == "deck"]
    return (len(deck_msgs) == 1
            and deck_msgs[0].get("html")
            and 'data-i="0"' in deck_msgs[0]["html"]
            and p.images.get(0) == b"x")
check("swapping the art in pushes markup, never a reload",
      _swap_carries_markup_not_a_reload())

import agent
def _gate_never_leaks():
    agent._present_lock = None
    agent._client = object()
    class _Boom:
        def present(self, *a, **k): raise RuntimeError("boom")
        def close(self, *a): pass
    saved = sys.modules.get("deck")
    sys.modules["deck"] = _Boom()
    try:
        agent.present("x")
        return agent._present_lock is None
    finally:
        sys.modules["deck"] = saved
        agent._present_lock = None
check("a crashing build releases the single-flight gate", _gate_never_leaks())

# =========================================================================== #
# 13. THE FIGURE PROBLEM. Three separate defects, all of which put a broken
#     picture on screen and none of which raised anything.
#
#  a) Every label was `opacity:0` and was only ever revealed by a CALLOUT,
#     which fires when the cue matcher finds that label's words in what Neo
#     actually said. A two-sentence narration does not name every part of a
#     figure, so most labels were never revealed AT ALL — what reached the
#     screen was a bare silhouette with nothing named on it.
#  b) A label's (x, y) is a percentage of the DRAWING, but it was resolved
#     against the whole frame while the drawing was scaled and translated
#     somewhere else — so the dot landed near the organ, never on it, and the
#     leader line joined a name to empty space.
#  c) Every label stacked in ONE right-hand gutter, so a part on the left of
#     the drawing got a leader line dragged straight across the artwork.
# =========================================================================== #
FIG = {"kind": "figure", "image": "", "shape": "heart", "labels": [
    {"text": "Right atrium", "x": 22, "y": 30},
    {"text": "Left atrium", "x": 74, "y": 28},
    {"text": "Right ventricle", "x": 26, "y": 72},
    {"text": "Left ventricle", "x": 78, "y": 70}]}
fig_svg = deck._render_visual(deck.safe_visual(FIG), 0)

check("every label of a figure is in the markup",
      all(t in fig_svg for t in ("Right atrium", "Left atrium",
                                 "Right ventricle", "Left ventricle")))

# (a) The reveal must not depend on a callout. `.lead.on` is the highlight now;
#     `.step.on .lead` is what makes it visible.
_css = deck.render_html({"title": "t", "slides": []})
check("a figure's labels are revealed by the slide, not by a callout",
      ".step.on .lead" in _css and ".lead.on {" not in _css)

# (b) Anchors must land inside the box the silhouette actually occupies.
_tf, _box = deck.place_shape("heart", 300.0, 37.0, 400.0, 546.0)
bx, by, bw, bh = _box
dots = [(float(m.group(1)), float(m.group(2)))
        for m in _re.finditer(r'<circle cx="(-?[\d.]+)" cy="(-?[\d.]+)"', fig_svg)]
check("leader-line anchors land on the drawing, not beside it",
      bool(dots) and all(bx - 1 <= x <= bx + bw + 1 and by - 1 <= y <= by + bh + 1
                         for x, y in dots))

# (c) A label on the left half is named on the LEFT, so no leader crosses the art.
def _side_of(label):
    m = _re.search(r'data-c="%d".*?text-anchor="(\w+)"'
                   % [l["text"] for l in FIG["labels"]].index(label), fig_svg)
    return m.group(1) if m else ""
check("a part on the left of the drawing is named on the left",
      _side_of("Right atrium") == "end" and _side_of("Left atrium") == "start")

check("place_shape fits the silhouette inside the rect it was given",
      bw <= 400.0 + 0.5 and bh <= 546.0 + 0.5 and bw > 0 and bh > 0)

# =========================================================================== #
# 14. A chart written the natural way was thrown away ENTIRELY. The cleaner
#     demanded [[x, y]] pairs and a "name"; a flat list of y-values and a
#     "label" — which is what the model writes most of the time — parsed to no
#     series, returned None, and the whole step silently degraded to a bullet
#     line. That is the batch whose JSON neo.log recorded as unusable.
# =========================================================================== #
flat = deck.safe_visual({"kind": "chart", "chart": "line",
                         "series": [{"label": "Untreated",
                                     "points": [0.2, 0.6, 2.2, 3.2]}]})
check("a chart series written as a flat list of values survives",
      flat is not None and flat["kind"] == "chart"
      and flat["series"][0]["points"] == [(0.0, 0.2), (1.0, 0.6),
                                          (2.0, 2.2), (3.0, 3.2)])
check("a chart series named with 'label' keeps its name",
      flat["series"][0]["name"] == "Untreated")
paired = deck.safe_visual({"kind": "chart", "series": [
    {"name": "A", "data": [[0, 1], [1, 4], [2, 9]]}]})
check("[[x, y]] pairs still parse, under 'data' as well as 'points'",
      paired is not None and paired["series"][0]["points"] == [(0.0, 1.0),
                                                              (1.0, 4.0),
                                                              (2.0, 9.0)])

# =========================================================================== #
# 15. No slide ever had a heading. For a labelled figure that is a defensible
#     choice; for a versus (two lists), a crosssection (four bands) or a
#     process (five dots) it means the screen never says what it is ABOUT.
# =========================================================================== #
titled = {"title": "Deck", "slides": [
    {"title": "Rate vs rhythm control", "narration": "n",
     "visual": {"kind": "line", "text": "x"}}]}
check("a slide's title reaches the page",
      "Rate vs rhythm control" in deck.steps_html(titled))
check("a slide with no title renders no empty caption",
      'class="cap"' not in deck.steps_html(
          {"title": "d", "slides": [{"narration": "n", "visual": None}]}))
check("parse_plan keeps the per-slide title the plan asked for",
      (deck.parse_plan(json.dumps({"title": "T", "slides": [
          {"title": "The conduction system", "narration": "n"}]}))
       or {"slides": [{}]})["slides"][0].get("title") == "The conduction system")

# =========================================================================== #
# 16. Commons is multilingual and ranks foreign editions of the same plate
#     highly — with the labels BAKED IN. The old guard was a list of filename
#     suffixes ("-de.", "-fr." and six more), which caught nothing that was
#     merely NAMED in another language and no language off the list.
# =========================================================================== #
for name, expect in (("File:Heart diagram-de.svg", True),
                     ("File:Schema serca (pl).png", True),
                     ("File:\u0421\u0445\u0435\u043c\u0430 \u0441\u0435\u0440\u0434\u0446\u0430.svg", True),
                     ("File:\u5fc3\u81d3.png", True),
                     ("File:Nephron_fr_labels.svg", True),
                     ("File:Blausen 0470 HeartWall.png", False),
                     ("File:Gray's Anatomy plate 490.png", False),
                     ("File:Heart diagram-en.svg", False)):
    check(f"foreign_language({name[5:28]!r}) is {expect}",
          deck.foreign_language(name) is expect)
check("a foreign-language plate is not a candidate at all",
      deck.rank_image("File:Herz Diagramm-de.svg", "image/svg+xml", 0) is None)
check("an English plate still ranks",
      deck.rank_image("File:Blausen heart.png", "image/png", 0) is not None)

# =========================================================================== #
# 17. THE DECK THAT CAME BACK AS FOUR LINES OF BROKEN ENGLISH.
#
# neo.log 11:57:26, five figure calls in the same second:
#     visuals 0+ failed on gemini-2.5-flash: 429 RESOURCE_EXHAUSTED
#     visuals 1+ ... 2+ ... 3+ ... 4+   (identical)
# then 1/5 steps got a real visual. The other four rendered through
# fallback_visuals, which printed the first four non-filler words of the
# narration in 60pt type:
#     BEFORE MODEL CAN ANYTHING / ONCE TRAINED MODEL MOVES /
#     DURING INFERENCE PROVIDE TRAINED / EFFICIENCY INFERENCE CRUCIAL BECAUSE
# Two separate defects: the artwork was aimed at a model with a 20-a-day quota,
# and the failure mode was illiterate.
# =========================================================================== #
_quota_err = ("ClientError: 429 RESOURCE_EXHAUSTED. {'error': {'code': 429, "
              "'quotaId': 'GenerateRequestsPerDayPerProjectPerModel-FreeTier'}}")
check("a per-DAY 429 is recognised as out-of-quota, not as 'busy'",
      providers.is_daily_quota(_quota_err) is True)
check("a per-minute 429 is NOT treated as out for the day",
      providers.is_daily_quota("429 RESOURCE_EXHAUSTED PerMinute") is False)
check("a 503 is not mistaken for a quota problem",
      providers.is_daily_quota("503 UNAVAILABLE high demand") is False)

def _exhausted_model_is_skipped():
    keys = {"gemini", "groq"}
    before = providers.usable_candidates("heavy", keys)
    if not before:
        return False
    p0, m0 = before[0]
    providers.mark_exhausted(p0, m0)
    try:
        after = providers.usable_candidates("heavy", keys)
        return after and after[0] != (p0, m0) and (p0, m0) in after
    finally:
        providers._EXHAUSTED.pop((p0, m0), None)
check("a model that is out of quota drops to the back instead of being tried "
      "five more times", _exhausted_model_is_skipped())

def _all_exhausted_still_returns_something():
    keys = {"gemini", "groq"}
    order = providers.usable_candidates("heavy", keys)
    for p, m in order:
        providers.mark_exhausted(p, m)
    try:
        return len(providers.usable_candidates("heavy", keys)) == len(order)
    finally:
        for p, m in order:
            providers._EXHAUSTED.pop((p, m), None)
check("if everything is exhausted the caller still gets a list to fail against",
      _all_exhausted_still_returns_something())

check("the 20-a-day model is not a candidate for artwork at all",
      ("gemini", "gemini-2.5-flash") not in providers.CANDIDATES["heavy"])

# The fallback has to be a phrase a person would write.
_fb = deck.fallback_visuals({"slides": [
    {"title": "Model Training Data",
     "narration": "Before an AI model can start doing anything useful it must "
                  "be trained on a very large amount of data.",
     "visual": None}]})
check("a step with no artwork shows its real title, not keyword wreckage",
      _fb["slides"][0]["visual"]["text"] == "Model Training Data")
check("the fallback never emits the old scrambled keyword line",
      _fb["slides"][0]["visual"]["text"] != deck._key_line(
          _fb["slides"][0]["narration"]))
check("a step that already has artwork is left alone",
      deck.fallback_visuals({"slides": [
          {"title": "T", "narration": "n", "visual": {"kind": "line",
                                                      "text": "kept"}}]}
          )["slides"][0]["visual"]["text"] == "kept")

# A figure that was about to arrive must not be killed by our own deadline.
check("one figure call is given longer than the model actually takes (41s "
      "measured)", deck.BATCH_TIMEOUT_S >= 45)
check("the whole art phase still has room for a retry",
      deck.ART_BUDGET_S > deck.BATCH_TIMEOUT_S * 2)

# =========================================================================== #
# 18. THE BILL. the user believed Neo was free. The .env said the Groq account
#     "bills to a card rather than sitting on the free tier" and CLAUDE.md
#     repeated it — and Groq stayed SECOND in the heavy list anyway, so every
#     time Gemini 503'd a presentation was drawn on a paid account. neo.log
#     has 26 completed gpt-oss-120b calls. A comment is not a control.
# =========================================================================== #
import os as _os
check("groq is marked paid, not free", providers.PROVIDERS["groq"]["free"] is False)
check("gemini is free", providers.PROVIDERS["gemini"]["free"] is True)
check("a paid provider is invisible even when its key is present",
      providers.have("groq") is False or "groq" in providers.ALLOW_PAID)

def _paid_needs_opt_in():
    """have() is the only gate, so prove it is the thing doing the work."""
    spec = providers.PROVIDERS["groq"]
    was = spec["free"]
    saved = set(providers.ALLOW_PAID)
    try:
        spec["free"] = False
        providers.ALLOW_PAID.clear()
        _os.environ["GROQ_API_KEY"] = "x" * 40
        blocked = providers.have("groq") is False
        providers.ALLOW_PAID.add("groq")
        allowed = providers.have("groq") is True
        return blocked and allowed
    finally:
        spec["free"] = was
        providers.ALLOW_PAID.clear()
        providers.ALLOW_PAID.update(saved)
check("NEO_ALLOW_PAID is what turns a paid provider on, and nothing else",
      _paid_needs_opt_in())

def _no_paid_in_any_default_chain():
    """With only free keys configured, nothing billable can be reached."""
    free = {n for n, sp in providers.PROVIDERS.items() if sp.get("free")}
    for job in providers.CANDIDATES:
        for prov, _m in providers.usable_candidates(job, free):
            if not providers.PROVIDERS[prov].get("free"):
                return False
    return True
check("no job can reach a billable provider on free keys alone",
      _no_paid_in_any_default_chain())

check("every provider declares free or paid explicitly — no silent default",
      all("free" in sp for sp in providers.PROVIDERS.values()))
check("every OpenRouter model Neo asks for is a ':free' id",
      all(m.endswith(":free")
          for job in providers.CANDIDATES
          for p, m in providers.CANDIDATES[job] if p == "openrouter"))
check("there is a free alternative for every job, so Gemini running dry is "
      "not the end",
      all(any(p != "gemini" and providers.PROVIDERS[p].get("free")
              for p, _m in providers.CANDIDATES[job])
          for job in ("chat", "heavy", "fast")))

# =========================================================================== #
# 19. THE SHAPE IS CHOSEN IN CODE NOW. The model picked its own and picked
#     badly — a five-slide deck came back with `versus` twice, and the only
#     figure that rendered at all was `svg`, the kind this file's header calls
#     unusable. It also meant every call carried the whole 12-shape catalogue.
# =========================================================================== #
REAL = [("Clot to brain", "First the clot breaks free, then it enters the "
                          "circulation, and finally it lodges in an artery."),
        ("Rate vs rhythm", "Rate control lets it fibrillate, whereas rhythm "
                           "control restores sinus rhythm; the trade-off is "
                           "symptoms versus simplicity."),
        ("The atrial wall", "The wall has three layers: an inner endocardium, "
                            "the muscular myocardium, and an outer epicardium."),
        ("Speed and scale", "Latency grew and costs rose over time as usage "
                            "doubled.")]
_want = {"Clot to brain": "process", "Rate vs rhythm": "versus",
         "The atrial wall": "crosssection", "Speed and scale": "chart"}
for _t, _n in REAL:
    check(f"choose_shape reads {_t!r} as {_want[_t]}",
          deck.choose_shape(_n, _t)[0] == _want[_t])
check("the fallback shape is always one that any prose can fill",
      all(deck.choose_shape(n, t)[1] in deck.SAFE_SHAPES for t, n in REAL))
check("the fallback is never the same as the primary",
      all(deck.choose_shape(n, t)[0] != deck.choose_shape(n, t)[1]
          for t, n in REAL))
check("a narration with no signal at all still gets a real shape",
      deck.choose_shape("", "")[0] in deck.SHAPE_SPECS)

def _no_three_in_a_row():
    same = [{"narration": "First this, then that, then the next stage.",
             "title": "s"} for _ in range(5)]
    return [p for p, _a in deck.plan_shapes(same)].count("process") < 5
check("three identical shapes in a row are broken up", _no_three_in_a_row())

_one = deck.visuals_prompt({"slides": [{"narration": "n"}]}, kinds=["process"])
_all = deck.visuals_prompt({"slides": [{"narration": "n"}]})
check("a one-shape prompt is a fraction of the full catalogue",
      len(_one) < len(_all) * 0.25)
check("the chosen shape is named so the model cannot substitute another",
      'Use the "process" shape' in _one)
check("a one-shape prompt carries only that shape's spec",
      '"kind":"versus"' not in _one and '"kind":"process"' in _one)
check("the SVG tutorial is not sent unless SVG was chosen",
      "BUILD FROM CURVES" not in _one)

# =========================================================================== #
# 20. THE QUALITY GATE. A visual can clear its schema and still be empty.
# =========================================================================== #
for _v, _thin, _name in (
        ({"kind": "flow", "nodes": [{"id": "a"}, {"id": "b"}],
          "edges": [{"from": "a", "to": "b"}]}, True, "a two-node flow"),
        ({"kind": "versus", "left": {"points": ["x"]},
          "right": {"points": ["y", "z"]}}, True, "a one-bullet side"),
        ({"kind": "process", "steps": [{"label": "a"}, {"label": "b"}]}, True,
         "a two-step process"),
        ({"kind": "figure", "image": "", "labels": [{"text": "x"}]}, True,
         "a figure with one label and no picture"),
        ({"kind": "figure", "image": "heart anatomy", "labels": []}, False,
         "a figure that fetched a real picture"),
        ({"kind": "line", "text": "x"}, False, "a kind with no rule")):
    check(f"too_thin: {_name} -> {_thin}", deck.too_thin(_v) is _thin)
check("too_thin never raises on rubbish", deck.too_thin(None) is True
      and deck.too_thin({"kind": "flow"}) is True)

# =========================================================================== #
# 21. A SLIDE HAS TO STAY UP LONG ENOUGH TO BE LOOKED AT. Two cue openings
#     arriving in one transcript fragment advanced the deck twice inside a
#     second — a figure appearing and vanishing before it was read.
# =========================================================================== #
def _dwell_holds_then_releases():
    steps = [{"narration": "Carbohydrates are sugars used for energy."},
             {"narration": "Lipids store energy and build membranes."},
             {"narration": "Proteins do the work of the cell."}]
    now = [0.0]
    tr = deck.Tracker(steps, min_dwell=4.5, clock=lambda: now[0])
    tr.feed("Carbohydrates are sugars. Lipids store energy. Proteins do work.")
    held = tr.index == 0
    now[0] = 5.0
    tr.feed("more")
    moved = tr.index == 1
    now[0] = 10.0
    tr.feed("more")
    return held and moved and tr.index == 2
check("a burst of cues advances one slide per dwell, not straight to the end",
      _dwell_holds_then_releases())
check("a deferred advance is not a lost one — the deck still gets there",
      deck.MIN_SLIDE_S >= 2.0)

# =========================================================================== #
# 22. THE CLOSING BEAT. A walkthrough that stops dead on its last figure has
#     no ending.
# =========================================================================== #
_withtake = {"title": "T", "takeaway": "A quivering atrium makes clots.",
             "slides": [{"title": "A", "narration": "n",
                         "visual": {"kind": "line", "text": "x"}}]}
check("a takeaway renders as a closing card",
      "A quivering atrium makes clots." in deck.steps_html(_withtake))
check("the closing card sits one past the last slide, out of the Tracker's "
      "reach", 'class="step closing" data-i="1"' in deck.steps_html(_withtake))
check("no takeaway means no empty closing card",
      "closing" not in deck.steps_html(
          {"title": "T", "slides": [{"narration": "n", "visual": None}]}))
check("parse_plan keeps the takeaway the plan wrote",
      (deck.parse_plan(json.dumps({"title": "T", "takeaway": "Remember this.",
                                   "slides": [{"narration": "n"}]}))
       or {}).get("takeaway") == "Remember this.")

# =========================================================================== #
# 23. THE SEGFAULT. the user got "Python quit unexpectedly". The crash report:
#       EXC_BAD_ACCESS (SIGSEGV) in libportaudio  Pa_OpenStream
#                                                 OpenAndSetupOneAudioUnit
#     PortAudio's open path is not thread-safe. live.py already serialised its
#     own two streams on _PA_LOCK — but the lock was PRIVATE, so neo.py's
#     recorder mic and local TTS speaker opened with no lock at all. Two paths,
#     one device, no mutual exclusion.
# =========================================================================== #
import live as _live
import inspect as _ins
check("live.py exposes the audio lock for neo.py to share",
      hasattr(_live, "AUDIO_LOCK") and _live.AUDIO_LOCK is _live._PA_LOCK)
_neosrc = open("neo.py").read()

def _every_app_stream_is_guarded():
    """EVERY sd.*Stream( in neo.py must sit under AUDIO_LOCK — no exceptions,
    including the CLI mic diagnostic. An exception is a hole, and this check
    exists precisely because the holes were the ones nobody was looking at."""
    lines = _neosrc.split("\n")
    for i, line in enumerate(lines):
        if "sd.InputStream(" not in line and "sd.OutputStream(" not in line:
            continue
        if "AUDIO_LOCK" not in "\n".join(lines[max(0, i - 5):i + 1]):
            return False
    return True
check("every PortAudio stream the app opens is under the shared lock",
      _every_app_stream_is_guarded())

# =========================================================================== #
# 24. WHAT THE USER PHOTOGRAPHED. Three slides, three separate rendering faults.
#
#  a) Words cut in half: "~150 mm2 silicon footprin", "thermal dissipation ac",
#     "or causes permanent sili". _txt was a bare slice, and a hard cut at 72
#     characters lands mid-word most of the time. He read it as spelling
#     mistakes, which is exactly what it looks like.
#  b) SVG <text> does not wrap, so a 60-character `sub` in a 208-unit box ran
#     clean out of the box and through the one next to it.
#  c) Arrows backed off a flat 40% of the centre-to-centre distance, which has
#     nothing to do with where a box actually ends.
# =========================================================================== #
check("a trimmed string never ends mid-word",
      deck._txt("Billions of sub-10nm gates integrated within a 150 mm silicon "
                "footprint", 60).endswith("\u2026")
      and not deck._txt("Billions of sub-10nm gates integrated within a 150 mm "
                        "silicon footprint", 60).rstrip("\u2026").endswith("sili"))
check("a string that fits is returned untouched",
      deck._txt("Copper Cold Plate", 40) == "Copper Cold Plate")
check("an over-long single word is still cut, rather than overflowing",
      len(deck._txt("x" * 200, 30)) <= 31)

_w = deck.wrap_text("Submerged blades in non-conductive dielectric fluid", 26, 3)
check("wrap_text never exceeds its width", all(len(l) <= 26 for l in _w))
check("wrap_text keeps every word intact",
      " ".join(_w).replace("\u2026", "").split()[:3]
      == ["Submerged", "blades", "in"])
check("wrap_text obeys its line limit and marks the overflow",
      len(deck.wrap_text("word " * 80, 20, 2)) == 2
      and deck.wrap_text("word " * 80, 20, 2)[-1].endswith("\u2026"))
check("wrap_text on nothing is nothing, not a crash",
      deck.wrap_text("", 20) == [] and deck.wrap_text(None, 20) == [])

_flow = deck._render_visual(deck.safe_visual({
    "kind": "flow",
    "nodes": [{"id": "a", "label": "Liquid Coolant Intake",
               "sub": "Conducts thermal energy significantly faster than "
                      "ambient air", "x": 20, "y": 30, "tone": "blue"},
              {"id": "b", "label": "Copper Cold Plate",
               "sub": "Metal block mounted directly on the silicon surface",
               "x": 60, "y": 30, "tone": "amber"},
              {"id": "c", "label": "Exhaust", "sub": "out", "x": 60,
               "y": 75, "tone": "green"}],
    "edges": [{"from": "a", "to": "b", "label": "Cool fluid", "flow": True},
              {"from": "c", "to": "b", "label": "Conduction", "flow": True}]}), 0)
_lines = _re.findall(r'<text class="box-sub"[^>]*>([^<]*)</text>', _flow)
check("no line of a flow box is wide enough to leave the box",
      bool(_lines) and all(len(l) <= deck.SUB_WRAP for l in _lines))
check("a long sub becomes several lines instead of one long one",
      len(_lines) > 2)

def _arrows_touch_their_boxes():
    """Each arrow end must sit on the rim of its own node's box, not adrift."""
    for cx, cy, tx, ty, w, h in ((0, 0, 300, 0, 250, 100),
                                 (0, 0, 0, 300, 250, 100),
                                 (0, 0, 200, 200, 250, 100)):
        ex, ey = deck._edge_of_box(cx, cy, tx, ty, w, h, pad=0)
        on_side = abs(abs(ex - cx) - w / 2) < 1e-6 or abs(abs(ey - cy) - h / 2) < 1e-6
        inside = abs(ex - cx) <= w / 2 + 1e-6 and abs(ey - cy) <= h / 2 + 1e-6
        if not (on_side and inside):
            return False
    return True
check("an arrow ends on the edge of the box it points at",
      _arrows_touch_their_boxes())
check("a zero-length edge does not divide by zero",
      deck._edge_of_box(5, 5, 5, 5, 100, 50) == (5, 5))

# =========================================================================== #
# 25. FOCUS. "Highlight, enlarge or do something to each part while the audio
#     is talking about it, so it's easy to know what the main focus is."
# =========================================================================== #
_page = deck.render_html({"title": "t", "slides": []})
check("the page is told what Neo is saying, not just which slide",
      "'say'" in _page and "function focus(" in _page)
check("focusing dims the rest so the lift reads as attention",
      ".focusing .node" in _page and ".node.live" in _page)

def _say_events_reach_the_page():
    sent = []
    p = deck.Presentation({"title": "T", "slides": [
        {"narration": "Copper cold plates sit on the silicon.", "visual": None},
        {"narration": "Immersion tanks drench the whole board.", "visual": None}]},
        log=lambda m: None, min_dwell=0)
    p.push = lambda m: sent.append(m)
    p.feed("Copper cold plates sit on the silicon", lag=0.0)
    p.close()
    says = [m for m in sent if m.get("t") == "say"]
    return bool(says) and "copper" in says[-1]["w"]
check("the spoken tail is streamed so the page can follow it",
      _say_events_reach_the_page())

# =========================================================================== #
# 26. A SECOND GEMINI PROJECT KEEPS THE VOICE ALIVE.
#
# Google's 429 names the scope itself — GenerateRequestsPerDayPerProject... —
# so a second project under the same account is a second full allowance. This
# matters more for the live voice than for anything else: nothing else free
# does speech-to-speech at all, so when the project running it hits its daily
# wall the alternative is not a slower answer, it is a DIFFERENT VOICE.
# =========================================================================== #
_ENV = {"GEMINI_API_KEY": "AIzaFIRSTkey000000000",
        "GEMINI_API_KEY_2": "AIzaSECONDkey00000000",
        "GEMINI_API_KEY_3": "   ",
        "GEMINI_API_KEY_4": "paste_your_key_here_xx"}
_keys = providers.gemini_keys(_ENV)
check("every configured key is found, in order",
      _keys == ["AIzaFIRSTkey000000000", "AIzaSECONDkey00000000"])
check("blank and placeholder slots are ignored, not tried",
      all("paste" not in k and k.strip() for k in _keys))
check("a duplicate key is not counted twice",
      providers.gemini_keys({"GEMINI_API_KEY": "AIzaSAMEkey0000000000",
                             "GEMINI_API_KEY_2": "AIzaSAMEkey0000000000"})
      == ["AIzaSAMEkey0000000000"])

def _rotation():
    providers._key_down.clear()
    try:
        if providers.live_keys(_keys) != _keys:
            return False
        providers.retire_key(_keys[0])
        if providers.live_keys(_keys) != _keys[1:]:
            return False
        # Everything dry must still hand back something: going silent is worse
        # than trying a key that will probably 429.
        providers.retire_key(_keys[1])
        return providers.live_keys(_keys) == _keys
    finally:
        providers._key_down.clear()
check("a dry key steps aside and the next one takes over", _rotation())

def _rest_expires():
    providers._key_down.clear()
    try:
        providers.retire_key(_keys[0], rest=-1)      # already elapsed
        return providers.live_keys(_keys) == _keys
    finally:
        providers._key_down.clear()
check("a rested key comes back when its quota rolls over", _rest_expires())

check("key_state reports what the boot line needs",
      providers.key_state(_keys) == (2, 0))

# The rotation must fire on a DAILY 429 and on nothing else — a 503 or a
# per-minute limit is transient and retiring a key for hours over one would
# throw away good quota.
check("only a per-day quota error retires a key",
      providers.is_daily_quota(
          "429 quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier")
      and not providers.is_daily_quota("503 UNAVAILABLE")
      and not providers.is_daily_quota(
          "429 GenerateRequestsPerMinutePerProjectPerModel-FreeTier"))

import inspect as _i2
import neo as _neo2
_deg = _i2.getsource(_neo2.Neo._degrade)
check("the voice tries another key BEFORE falling back to hold-to-talk",
      "next_key" in _deg and _deg.index("next_key") < _deg.index("_live_degraded"))
check("Brain can rebuild itself on a different key",
      callable(getattr(_neo2.Brain, "next_key", None))
      and callable(getattr(_neo2.Brain, "_build_client", None)))

# =========================================================================== #
# 27. A PROVIDER THAT ASKS FOR A CARD IS NOT FREE, whatever this repo claims.
#
# SambaNova was marked free: True here on my judgement. Its very first call
# answered HTTP 402, balance_units 0, "A payment method is required." That is
# the Groq mistake a second time, from the same cause: a flag set by belief
# rather than by observation. The flags stay, but they are no longer the only
# thing standing between the user and a bill.
# =========================================================================== #
check("sambanova is marked paid, because its API said so",
      providers.PROVIDERS["sambanova"]["free"] is False)
check("a 402 payment demand is recognised",
      providers.is_payment_required(
          'HTTP 402 {"error":{"code":"PAYMENT_METHOD_REQUIRED",'
          '"message":"A payment method is required."}}') is True)
check("insufficient credit is recognised too",
      providers.is_payment_required("insufficient_quota: add billing") is True)
check("a busy or rate-limited provider is NOT mistaken for a paywall",
      not providers.is_payment_required("429 RESOURCE_EXHAUSTED PerDay")
      and not providers.is_payment_required("503 UNAVAILABLE high demand")
      and not providers.is_payment_required("504 DEADLINE_EXCEEDED"))

def _paywalled_is_disabled():
    """The runtime observation must actually switch the provider off."""
    saved_free = providers.PROVIDERS["cerebras"]["free"]
    saved = set(providers._PAYWALLED)
    import os as _os
    was = _os.environ.get("CEREBRAS_API_KEY")
    try:
        _os.environ["CEREBRAS_API_KEY"] = "x" * 40
        providers._PAYWALLED.clear()
        before = providers.have("cerebras")
        providers.mark_paywalled("cerebras", log=lambda *_: None)
        return before is True and providers.have("cerebras") is False
    finally:
        providers._PAYWALLED.clear()
        providers._PAYWALLED.update(saved)
        providers.PROVIDERS["cerebras"]["free"] = saved_free
        if was is None:
            _os.environ.pop("CEREBRAS_API_KEY", None)
        else:
            _os.environ["CEREBRAS_API_KEY"] = was
check("a provider that demands payment is switched off for the session",
      _paywalled_is_disabled())

_dsrc = open("deck.py").read()
check("the deck checks for a paywall BEFORE treating it as a quota blip",
      _dsrc.index("is_payment_required") < _dsrc.index("is_daily_quota"))

# =========================================================================== #
# 28. A REAL PICTURE, POINTED AT HONESTLY.
#
# Drawing was tried every free way and none of them worked. A published
# teaching plate beats all of them — but a fetched picture used to be opaque,
# so deck.py refused to draw a leader line on one and showed a bare photograph
# with a list of words beside it. The model can SEE the image now, so it can
# say where things are. The catch, measured: asked for percentages it returned
# PIXELS, and two of four coordinates fell outside the frame entirely.
# Unverified, that is a confident marker on the wrong structure.
# =========================================================================== #
import imagery

check("pixel coordinates are detected and converted, not taken as percent",
      imagery.normalise_points([{"name": "A", "x": 40, "y": 427},
                                {"name": "B", "x": 300, "y": 500}], 563, 1088)[0]["x"]
      == round(40 / 563 * 100, 2))
check("a point outside the frame is dropped, not clamped",
      [p["name"] for p in imagery.normalise_points(
          [{"name": "in", "x": 40, "y": 427},
           {"name": "off", "x": 712, "y": 428}], 563, 1088)] == ["in"])
check("percentages are left alone",
      imagery.normalise_points([{"name": "A", "x": 35, "y": 40}], 800, 800)
      == [{"name": "A", "x": 35.0, "y": 40.0}])
check("a nameless or non-numeric point is dropped",
      imagery.normalise_points([{"name": "", "x": 10, "y": 10},
                                {"name": "B", "x": "?", "y": 10}], 100, 100) == [])
check("two markers on the same spot become one",
      len(imagery.spread_ok([{"name": "A", "x": 50, "y": 50},
                             {"name": "B", "x": 52, "y": 51}])) == 1)

# The blind second look. It must accept prose agreement and reject everything
# else — this is the only thing standing between the user and a confident lie.
check("verification accepts a prose agreement",
      imagery.verdict("Left ventricle",
                      "the structure at the centre is the **Left ventricle**"))
check("verification accepts a reordered name",
      imagery.verdict("Left ventricle", "ventricle, left"))
check("verification REJECTS a different structure",
      not imagery.verdict("Left ventricle", "Right atrium"))
check("verification REJECTS a refusal",
      not imagery.verdict("Aortic valve", "NONE"))
check("verification rejects an empty answer",
      not imagery.verdict("Aorta", "") and not imagery.verdict("", "Aorta"))

check("a sentence is reduced to a real search query",
      imagery.search_term("the human heart and how blood flows through it")
      == "human heart blood flows")

def _verify_fails_closed():
    """If the verifier cannot be reached, show NOTHING. Silence is safe; a
    marker nobody checked is not."""
    class _Boom:
        class models:
            @staticmethod
            def generate_content(*a, **k):
                raise RuntimeError("no network")
    import io as _io
    from PIL import Image as _Im
    buf = _io.BytesIO(); _Im.new("RGB", (200, 200)).save(buf, format="PNG")
    return imagery.verify(buf.getvalue(),
                          [{"name": "A", "x": 50, "y": 50}], "x",
                          _Boom(), log=lambda *_: None) == []
check("a verifier that cannot be reached shows no markers at all",
      _verify_fails_closed())

# ---- the renderer has to put the dot where the part actually is ----------
_LB = {"kind": "figure", "image": "x", "verified": True,
       "iw": 857, "ih": 1125,        # portrait plate in a landscape frame
       "labels": [{"text": "Glomerulus", "x": 50, "y": 53, "say": "glomerulus"},
                  {"text": "Bowman's capsule", "x": 50, "y": 42,
                   "say": "bowman"}]}
_svg = deck._render_visual(deck.safe_visual(_LB), 0)
_vx = [float(m) for m in _re.findall(r'data-vx="(-?[\d.]+)"', _svg)]
# 857x1125 into 1000x620 fits by HEIGHT: drawn width is 620*857/1125 = 472,
# so the image spans x 264..736 and a part at x=50% must land at 500 — NOT at
# 500 by luck of the viewBox, but by mapping through the letterboxed rect.
check("a marker maps into the letterboxed image, not the whole viewBox",
      bool(_vx) and all(264 <= x <= 736 for x in _vx))
check("the verified figure carries a dimmer for the spotlight to mask",
      'class="dimmer"' in _svg)
check("labels sit in the gutter with a leader line, so two parts 11% apart "
      "cannot collide", _svg.count("<polyline") == 2)
check("an unverified picture still gets NO leader lines",
      "<polyline" not in deck._render_visual(deck.safe_visual(
          {"kind": "figure", "image": "x",
           "labels": [{"text": "A", "x": 20, "y": 20}]}), 0))

_page = deck.render_html({"title": "t", "slides": []})
check("the page dims the plate and lights only the named part",
      ".fig.verified.focusing .photo" in _page and ".pin.live" in _page)
check("the spotlight is a MASK, so the lit structure keeps its brightness",
      "spot-mask" in _page and "spot-hole" in _page)

# =========================================================================== #
# 29. NEO WAS TRANSCRIBING HIMSELF.
#
# neo.log 22:26, verbatim:
#     Neo: ...I'd need more info to find a specific company called Similie AI.
#     You: specific company called It's an artificial intelligence company.
#
# The first four words of the user's "question" are Neo's own. He pressed the key
# while Neo was still speaking; the mic opened, Neo kept playing out of the
# speakers, and the microphone recorded him. The model then answered a question
# it had half asked itself — which reads exactly like a transcription failure,
# and was really an echo.
#
# The only place playback was ever dropped is the server's `interrupted` event.
# Automatic activity detection is deliberately OFF here, so that event
# effectively never fires: ONE occurrence in a full day of log.
# =========================================================================== #
import live as _lv2
import inspect as _i3

_begin = _i3.getsource(_lv2.LiveSession.begin_turn)
check("pressing the key stops Neo mid-sentence",
      "flush()" in _begin)
check("and the drop is logged, so an echo is diagnosable next time",
      "dropped" in _begin.lower() or "stopped talking" in _begin.lower())

_msg = _i3.getsource(_lv2.LiveSession._handle_message)
check("audio still streaming in while the key is held is discarded",
      "_holding" in _msg and _msg.index("if self._holding") < _msg.index("_playback.write"))

def _held_key_silences_playback():
    """The real objects: hold the key, push audio in, and nothing may play."""
    s = _lv2.LiveSession(client=None, model="m", system_instruction="s")
    s._open_mic_now = lambda hold_id=None: None
    s._close_mic_now = lambda hold_id=None: None
    s._playback.write(b"\x00\x01" * 4000)      # Neo is mid-answer
    had = s._playback.pending() > 0
    s.begin_turn()                              # the user presses the key
    return had and s._playback.pending() == 0
check("audio already queued is dropped the moment the key goes down",
      _held_key_silences_playback())

def _flush_is_not_destructive_when_idle():
    s = _lv2.LiveSession(client=None, model="m", system_instruction="s")
    s._open_mic_now = lambda hold_id=None: None
    s._close_mic_now = lambda hold_id=None: None
    s.begin_turn()                              # nothing queued: must not raise
    return s._playback.pending() == 0
check("pressing the key with nothing playing is harmless",
      _flush_is_not_destructive_when_idle())

# =========================================================================== #
# 30. A NAME HEARD OUT LOUD IS PROBABLY SPELLED SLIGHTLY WRONG.
#
# the user asked about "Similie AI". The search returned definitions of the
# literary device "simile"; Neo said it could not find a company and stopped.
# The company is Simile AI — ONE LETTER — and the word was sitting in the
# results it had just read.
#
# It stopped because the brief told it to: "Never call the same tool twice for
# one question... Do not retry." That rule was written to kill a runaway tool
# loop and it also banned the one move that would have worked.
# =========================================================================== #
import context as _ctx
_b = " ".join(_ctx.brief().lower().split())
# Scoped, not blanket. There is a second, correct "do not retry" in the brief —
# about re-running a FAILED call with identical arguments, which really is
# pointless. What must not survive is a blanket ban that also forbids a better
# query. So: every "do not retry" has to be about the SAME call or search.
_no_retry = [_b[m - 30:m + 60] for m in
             [i for i in range(len(_b)) if _b.startswith("do not retry", i)]]
check("a better second search is allowed, not forbidden",
      "better query" in _b
      and all("same call" in c or "same search" in c for c in _no_retry))
check("repeating the IDENTICAL search is still forbidden",
      "same search twice" in _b or "identical query" in _b)
check("the brief says a heard name is probably misspelled",
      "one letter away" in _b or "spelled slightly wrong" in _b)
check("and says a dictionary hit is not proof the thing is fictional",
      "does not exist" in _b and "dictionary" in _b)
check("the refinement is bounded — two attempts, then answer",
      "second attempt" in _b)
check("the real case is written down so it is not re-broken",
      "similie" in _b and "simile ai" in _b)

# =========================================================================== #
# 31. THE MIC "GOING IN AND OUT".
#
# neo.log: Neo started 00:10:46 and worked. From 01:29 every single press died
# the same way —
#     PaMacCore (AUHAL) Error line 1332: err='-10851' Invalid Property Value
#     [live] could not open audio (Error opening RawOutputStream: -9986)
# — while a FRESH python process on the same machine, at the same moment,
# opened the identical 24 kHz mono stream without complaint.
#
# Nothing was wrong with the hardware. PortAudio reads the device list ONCE at
# initialisation, and the user's machine has devices that come and go: a
# Continuity "the user Iphone 16 Microphone" and a ZoomAudioDevice. When one
# appears or leaves, every cached index is wrong and every open fails until the
# process restarts. It was also the SPEAKER failing, not the microphone, which
# is why "the mic is going in and out" was a red herring.
# =========================================================================== #
check("there is a way to re-read the device list at runtime",
      callable(getattr(_lv2, "reset_portaudio", None)))

def _reset_actually_reinitialises():
    calls = []
    class _FakeSD:
        @staticmethod
        def _terminate(): calls.append("terminate")
        @staticmethod
        def _initialize(): calls.append("initialize")
    _lv2._last_reset[0] = 0.0          # clear the cooldown
    ok = _lv2.reset_portaudio(_FakeSD(), log=lambda *_: None)
    return ok and calls == ["terminate", "initialize"]
check("resetting tears PortAudio down and brings it back up, in that order",
      _reset_actually_reinitialises())

def _reset_is_rate_limited():
    """A failing open must not spin the audio system in a loop."""
    class _FakeSD:
        @staticmethod
        def _terminate(): pass
        @staticmethod
        def _initialize(): pass
    _lv2._last_reset[0] = 0.0
    first = _lv2.reset_portaudio(_FakeSD(), log=lambda *_: None)
    second = _lv2.reset_portaudio(_FakeSD(), log=lambda *_: None)
    return first is True and second is False
check("a second reset inside the cooldown is refused, so it cannot thrash",
      _reset_is_rate_limited())

def _speaker_retries_after_a_reset():
    """The real Playback: fail once, reset, succeed. This is the exact shape
    of the failure the user hit, and it must recover without a restart."""
    class _Stream:
        def start(self): pass
        def stop(self): pass
        def close(self): pass
    tries = []
    class _FakeSD:
        @staticmethod
        def RawOutputStream(**kw):
            tries.append(1)
            if len(tries) == 1:
                raise RuntimeError("Error opening RawOutputStream: -9986")
            return _Stream()
        @staticmethod
        def _terminate(): pass
        @staticmethod
        def _initialize(): pass
    _lv2._last_reset[0] = 0.0
    pb = _lv2.Playback()
    pb.start(_FakeSD(), log=lambda *_: None)
    return len(tries) == 2 and pb._stream is not None
check("a speaker that fails once opens on the retry, without a restart",
      _speaker_retries_after_a_reset())

def _speaker_gives_up_honestly():
    """If it still cannot open, the error must surface — not be swallowed."""
    class _FakeSD:
        @staticmethod
        def RawOutputStream(**kw):
            raise RuntimeError("still broken")
        @staticmethod
        def _terminate(): pass
        @staticmethod
        def _initialize(): pass
    _lv2._last_reset[0] = 0.0
    pb = _lv2.Playback()
    try:
        pb.start(_FakeSD(), log=lambda *_: None)
        return False
    except RuntimeError:
        return True
check("a speaker that is genuinely broken still raises, so Neo degrades",
      _speaker_gives_up_honestly())

_neosrc2 = open("neo.py").read()
check("the fallback recorder and local speaker recover the same way",
      _neosrc2.count("reset_portaudio") >= 2)

# =========================================================================== #
# 32. POINTING AT THE REAL SCREEN.
#
# The lesson from the diagram work, applied where it matters. Asked outright
# for coordinates on a screenshot, gemini-3.5-flash-lite put the SAME button at
# y=27.8 on one call and y=71.0 on the next, and confidently located a "New Tab
# button" on a screen with no browser open at all.
#
# So the model never gives a coordinate. The screen is covered in a labelled
# grid and the question becomes "which cell?" — a multiple choice, which it is
# good at, and which lets it answer NULL. Offered that, it correctly returned
# null for a button that did not exist.
# =========================================================================== #
import pointer as _pt

check("a percentage answer is taken as-is",
      _pt.as_box({"x": 50, "y": 40, "w": 10, "h": 5}, 1400, 900)["x"] == 50.0)
check("a pixel answer is converted, not believed",
      _pt.as_box({"x": 700, "y": 450}, 1400, 900)["x"] == 50.0)
check("a point off the edge of the screen is refused",
      _pt.as_box({"x": 150, "y": 40}, 100, 100) is None)

# same_control is deliberately looser than the anatomy check it borrows from:
# one button is honestly "New tab", "plus icon" or "the + button".
for claim, answer, want in (("New tab", "plus icon (new tab)", True),
                            ("Settings", "Settings button", True),
                            ("plus icon", "plus icon", True),
                            ("Privacy & Security", "Bookmarks", False),
                            ("gear icon", "three dots", False),
                            ("Save", "NONE", False),
                            ("", "Settings", False)):
    check(f"same_control({claim!r}, {answer!r}) is {want}",
          _pt.same_control(claim, answer) is want)

check("generic UI words alone are not a match — Settings must not equal "
      "Bookmarks just because both are buttons",
      _pt.same_control("Settings button", "Bookmarks button") is False)

# The screen-changed test is what makes it a WALKTHROUGH rather than one ring:
# it waits for the click before moving on.
check("an unchanged screen is not mistaken for progress",
      _pt.changed([100] * 100, [100] * 100) is False)
check("a real change is noticed",
      _pt.changed([100] * 100, [160] * 100) is True)
check("a blinking cursor does not count as a click",
      _pt.changed([100] * 100, [101] * 100) is False)
check("a first look with nothing to compare against counts as changed",
      _pt.changed(None, [1, 2, 3]) is True)

_ptsrc = open("pointer.py").read()
check("the model is never asked for coordinates",
      "Do NOT give coordinates" in _ptsrc)
check("it is allowed to say the control is not on screen",
      "do not guess" in _ptsrc.lower() and "cell:null" in _ptsrc)
# Compare against the CALL, not the def. "_open_window(" matches its own
# definition first, which sits above the loop — so the naive version of this
# check was comparing a call site to a function signature and passing or
# failing for reasons unrelated to the ordering it claims to test.
check("every step is verified before a ring is drawn",
      _ptsrc.index("verify_step(shot") < _ptsrc.index('_open_window({"dim"'))
check("the walkthrough waits for the screen to change between steps",
      "wait_for_change" in _ptsrc)
check("the tool returns immediately — a blocking tool gets re-fired",
      "Thread(target=_run" in open("agent.py").read())

_spot = open("spotlight.py").read()
check("the overlay is click-through, so it cannot eat the click it points at",
      "setIgnoresMouseEvents_(True)" in _spot)
check("the overlay positions in percentages, so retina scaling cannot skew it",
      "%" in _spot and "PERCENTAGES" in _spot)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED:")
    for f in FAILED:
        print("   -", f)
    sys.exit(1)
print("All hard checks passed.")
