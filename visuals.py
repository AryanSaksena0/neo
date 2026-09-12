"""
visuals.py — deciding WHAT to draw when a spoken answer alone isn't enough.

The pure half of Neo's answer panel. No Cocoa, no WebKit, no model: everything
here is a function of text, so it can be checked against real questions in
test_visuals.py without opening a window or spending a request.

WHY THIS EXISTS SEPARATELY FROM deck.py
deck.py builds a full-screen narrated presentation, and it only ever runs when
the user says the word "presentation". That is a big, deliberate thing. Most of
what they ask is smaller than that and still lands better with a picture: "what's
the difference between a Roth and a traditional IRA", "how does a heap work",
"where did our signups go this month". A 4867-line deck is the wrong hammer for
those, so this is the small one — one panel, one component, gone when they're done.

WHAT A COMPONENT IS
A small set of layouts that between them cover almost everything worth drawing
for a spoken answer. The model does not invent a layout; it picks one of these
and supplies the content, which is the whole reason this works where free-form
slide generation did not. See KINDS.

THE ITEM SHAPE IS DELIBERATELY UNIFORM
Every component consumes the same item — label, detail, value, tag, level, all
optional — and renders the fields it cares about. One parser, eight layouts.
The alternative (a bespoke schema per component) means the model has to get the
schema right before it can get the content right, and it is much better at
content than at schemas.
"""

import json
import re

# --------------------------------------------------------------------------- #
# The catalogue
# --------------------------------------------------------------------------- #
# name -> (what it is for, which item fields carry the weight)
KINDS = {
    "steps": ("An ordered process. Stages, a sequence, how something happens "
              "from start to finish.", ("label", "detail")),
    "compare": ("Two to four things side by side. Differences, trade-offs, "
                "this versus that.", ("label", "detail", "tag")),
    "stat": ("One to four headline numbers, each with a caption. How much, "
             "how many, how fast.", ("value", "label", "detail")),
    "timeline": ("Events in time, down a rail. History, a schedule, what "
                 "happened when.", ("label", "detail")),
    "breakdown": ("Parts of a whole, with proportion bars. What something is "
                  "made of, where it went.", ("label", "value", "detail")),
    "hierarchy": ("Nested structure. What contains what, how something is "
                  "organised.", ("label", "detail", "level")),
    "table": ("Rows against columns. Several things measured the same way.",
              ("label", "value", "detail")),
    "define": ("A term and its properties. What something IS.",
               ("label", "detail")),
}

# Fallbacks that can render ANY set of items without looking broken, best
# first. A component whose data turns out not to fit gets remapped to one of
# these rather than drawn badly — the deck learned the same lesson the hard way.
SAFE_KINDS = ("steps", "define", "compare")

# Sized against the real window, not by taste. panel.py is 500px tall; a step
# with a detail line runs about 56px, and the header takes ~75. Six items is
# what fits without clipping — and the panel is click-through, so a reader
# cannot scroll to recover anything that overflows. It has to fit or not exist.
MAX_ITEMS = 6
MAX_LABEL = 64
MAX_DETAIL = 140


# --------------------------------------------------------------------------- #
# Should there be a picture at all?
# --------------------------------------------------------------------------- #
# Weighted because one weak signal should not summon a panel over a one-line
# answer. "what is a mole" is a definition and deserves one; "what time is it"
# is not and must never get one.
_WANT = {
    3: ("what's the difference", "whats the difference", "difference between",
        "compare", "versus", " vs ", "walk me through", "step by step",
        "how does", "how do", "how is", "how are", "break down", "breakdown"),
    2: ("explain", "what are the", "stages", "phases", "process", "timeline",
        "history of", "structure of", "parts of", "made of", "made up of",
        "pros and cons", "trade-off", "tradeoff", "relationship between"),
    1: ("what is", "what's a", "whats a", "why does", "why do", "types of",
        "kinds of", "how much", "how many", "summarise", "summarize"),
}

# Things that are never worth a panel however they are phrased. A question can
# contain "how do" and still be a request to DO something.
_NEVER = (
    "what time", "what's the time", "set a timer", "remind me", "play ",
    "pause", "skip", "volume", "open ", "close ", "email", "text ", "message ",
    "call ", "search for", "google", "weather", "temperature", "score",
    "stock", "price of", "who won", "turn on", "turn off", "screenshot",
)

WANT_THRESHOLD = 2


def wants_visual(question, threshold=WANT_THRESHOLD):
    """Would a picture genuinely help answer this? Pure.

    This is a HINT, not a gate — the model makes the real call, because it can
    see its own answer and this can only see the question. It exists so the
    system prompt can be nudged and so the behaviour is testable: "how does a
    heap work" scoring above "what time is it" is a property worth locking down.
    """
    low = " " + re.sub(r"[^a-z0-9' ]", " ", (question or "").lower()) + " "
    low = re.sub(r"\s+", " ", low)
    if any(n in low for n in _NEVER):
        return False
    score = 0
    for weight, phrases in _WANT.items():
        for p in phrases:
            if p in low:
                score += weight
                break              # one hit per weight band, not per phrase
    return score >= threshold


# --------------------------------------------------------------------------- #
# Which component
# --------------------------------------------------------------------------- #
_SIGNALS = {
    # Sequence words, and they have to be generous: the default is now
    # `define`, which never numbers anything, so a genuine process that fails
    # to match here loses its numbering rather than gaining a false one. That
    # is the right way round, but it means missing "how a heap sort runs"
    # simply because the phrasing was "how a" and not "how does".
    "steps": (3, ("step", "steps", "process", "how does", "how do", "how is",
                  "how a ", "how an ", "how to", "stages", "phases",
                  "walk me through", "first", "then", "order of", "sequence",
                  "what happens", "workflow", "pipeline", "lifecycle",
                  "happens when", " runs", " works")),
    "compare": (3, ("difference", "differences", "versus", " vs ", "compare",
                    "compared", "better than", "trade-off", "tradeoff",
                    "pros and cons", "either", "instead of")),
    "timeline": (3, ("timeline", "history", "when did", "chronology", "era",
                     "century", "year by year", "schedule", "roadmap")),
    "breakdown": (3, ("breakdown", "break down", "made of", "made up of",
                      "composed of", "parts of", "share of", "proportion",
                      "percentage", "where did", "split")),
    "hierarchy": (3, ("hierarchy", "structure of", "organised", "organized",
                      "nested", "contains", "belongs to", "taxonomy",
                      "tree", "reports to", "layers")),
    "stat": (2, ("how much", "how many", "how fast", "how long", "revenue",
                 "count", "total", "average", "rate", "number of")),
    "table": (2, ("table", "rows", "columns", "spec", "specs", "side by side",
                  "each of", "for each")),
    "define": (1, ("what is", "what's a", "whats a", "define", "definition",
                   "meaning of", "stands for", "what does")),
}


def choose_kind(text, items=(), signals=None):
    """Which component best fits this answer. Pure, so it is tested against
    real questions rather than argued about.

    The TEXT decides first, because that is what the user actually asked. The
    shape of the data only breaks ties and vetoes impossibilities: you cannot
    draw proportion bars without numbers, and a single item is never a
    comparison.
    """
    signals = _SIGNALS if signals is None else signals
    low = " " + re.sub(r"[^a-z0-9' ]", " ", (text or "").lower()) + " "
    scored = {}
    for kind, (weight, words) in signals.items():
        hits = sum(1 for w in words if w in low)
        if hits:
            scored[kind] = weight * hits

    items = list(items or ())
    numeric = sum(1 for i in items if _number_in(i.get("value")) is not None)
    levelled = sum(1 for i in items if int(i.get("level") or 0) > 0)

    # Data evidence, worth less than what they said but enough to break a tie.
    if numeric >= max(2, len(items) - 1) and items:
        scored["breakdown"] = scored.get("breakdown", 0) + 2
        scored["stat"] = scored.get("stat", 0) + 1
    if levelled:
        scored["hierarchy"] = scored.get("hierarchy", 0) + 3

    # Vetoes. A component that cannot honestly render this data must not win.
    if len(items) < 2:
        for k in ("compare", "table", "breakdown", "timeline", "hierarchy"):
            scored.pop(k, None)
    if len(items) > 4:
        scored.pop("compare", None)      # more than four columns is a table
        scored.pop("stat", None)
    if not numeric:
        scored.pop("breakdown", None)

    if not scored:
        # NOTHING IN THE TEXT SAYS "SEQUENCE", so don't draw one. `steps`
        # numbers every row 1..n, and numbering things that have no order is a
        # claim the content does not support — the diffusion panel came back as
        # "1. High Concentration, 2. Gradient, 3. Random Movement", which reads
        # as a procedure and isn't one. `define` shows the same label and
        # detail with no implied ordering, so it is the honest default: being
        # less helpful about genuinely ordered content is a smaller error than
        # inventing an order.
        return "define"
    return max(scored, key=lambda k: (scored[k], -list(signals).index(k)))


def _number_in(value):
    """The first number in a value, or None. '42%', '$1,200', 3 -> 42, 1200, 3."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"-?\d[\d,]*\.?\d*", str(value))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Parsing what the model sent
# --------------------------------------------------------------------------- #
def parse_items(raw):
    """Items from whatever the model produced. Never raises.

    JSON first, because that is what it reaches for and what deck.py already
    proves it can do. Pipe-delimited lines second, because function-call
    arguments get mangled often enough that a fallback earns its keep:

        Heap sort | build the heap first | 40%

    maps to label | detail | value, which is the order they matter in.
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return _clean(raw)

    text = str(raw).strip()
    if not text:
        return []

    # JSON, possibly wrapped in a code fence the model added unasked.
    fenced = re.sub(r"^```[a-z]*\s*|\s*```$", "", text).strip()
    for candidate in (fenced, text):
        if candidate[:1] in "[{":
            try:
                loaded = json.loads(candidate)
            except ValueError:
                continue
            if isinstance(loaded, dict):
                loaded = loaded.get("items") or [loaded]
            if isinstance(loaded, list):
                return _clean(loaded)

    out = []
    for line in fenced.splitlines():
        line = line.strip().lstrip("-•*").strip()
        # A leading "1." / "2)" is numbering, not content — the component draws
        # its own numbers and two sets of them looks like a bug.
        line = re.sub(r"^\d+[.)]\s*", "", line)
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        item = {"label": parts[0]}
        if len(parts) > 1:
            item["detail"] = parts[1]
        if len(parts) > 2:
            item["value"] = parts[2]
        out.append(item)
    return _clean(out)


def _clean(rows):
    """Trim to the fields components actually read, cap the lengths, drop the
    empties. A label that runs to a paragraph breaks every layout here."""
    out = []
    for row in rows:
        if isinstance(row, str):
            row = {"label": row}
        if not isinstance(row, dict):
            continue
        label = _text(row.get("label") or row.get("title") or
                      row.get("name") or "", MAX_LABEL)
        detail = _text(row.get("detail") or row.get("description") or
                       row.get("text") or row.get("note") or "", MAX_DETAIL)
        value = row.get("value", row.get("number", row.get("amount", "")))
        value = _text(value, 24)
        tag = _text(row.get("tag") or row.get("badge") or "", 20)
        try:
            level = max(0, min(3, int(row.get("level") or 0)))
        except (TypeError, ValueError):
            level = 0
        if not (label or detail or value):
            continue
        item = {"label": label}
        if detail:
            item["detail"] = detail
        if value:
            item["value"] = value
        if tag:
            item["tag"] = tag
        if level:
            item["level"] = level
        out.append(item)
        if len(out) >= MAX_ITEMS:
            break
    return out


def _text(value, cap):
    s = re.sub(r"\s+", " ", str(value if value is not None else "")).strip()
    # Markdown never reaches the screen: the panel renders text, and **bold**
    # arriving as literal asterisks is the single most obvious tell that
    # something was pasted rather than designed.
    s = re.sub(r"[*_`#]+", "", s)
    return s[:cap].strip()


# --------------------------------------------------------------------------- #
# The finished spec
# --------------------------------------------------------------------------- #
def build(title, raw_items, kind="", subtitle="", question=""):
    """A spec panel.py can render, or None if there is nothing worth drawing.

    `kind` is the model's suggestion and is honoured when it is real and the
    data supports it. Otherwise code decides, which is deck.py's hard-won rule:
    a component chosen badly is worse than no picture, because the user believes
    what is on their screen.
    """
    items = parse_items(raw_items)
    title = _text(title, MAX_LABEL)
    subtitle = _text(subtitle, MAX_DETAIL)
    if not items or not title:
        return None

    want = str(kind or "").strip().lower()
    text = f"{question} {title} {subtitle}"
    if want not in KINDS:
        want = choose_kind(text, items)
    else:
        # The model named a real component. Trust it unless the data cannot
        # carry it — one item is not a comparison, no numbers is not a
        # breakdown — in which case fall back rather than draw a lie.
        if not _fits(want, items):
            want = choose_kind(text, items)
    if not _fits(want, items):
        want = next((k for k in SAFE_KINDS if _fits(k, items)), "define")

    if want == "breakdown":
        items = _with_percentages(items)
    return {"kind": want, "title": title, "subtitle": subtitle, "items": items}


def _fits(kind, items):
    """Can this component render this data honestly?"""
    n = len(items)
    if n == 0:
        return False
    if kind in ("compare",):
        return 2 <= n <= 4
    if kind in ("table", "timeline", "hierarchy"):
        return n >= 2
    numbered = sum(1 for i in items if _number_in(i.get("value")) is not None)
    if kind == "stat":
        # EVERY tile needs its number. A stat row with a blank tile in it looks
        # like the number failed to load.
        return n <= 4 and numbered == n
    if kind == "breakdown":
        # EVERY bar needs its number, and this is the check that was wrong.
        # It used to accept two numbered items out of any number, so a panel
        # about diffusion came back with "High Concentration 80", then two
        # bars with no value at all — empty grey tracks that read as broken —
        # and "Low Concentration 20". A breakdown with holes in it is not a
        # breakdown; it is a list, and there is a component for that.
        return n >= 2 and numbered == n
    return True


def _with_percentages(items):
    """Give every bar a 0-100 width. Values that already look like percentages
    are left alone; raw counts are normalised against the largest, because a
    bar chart of absolute numbers with no axis is just decoration."""
    nums = [(i, _number_in(i.get("value"))) for i in items]
    known = [n for _, n in nums if n is not None]
    if not known:
        return items
    as_pct = all(0 <= n <= 100 for n in known) and any(
        "%" in str(i.get("value", "")) for i, _ in nums)
    biggest = max(known) or 1.0
    out = []
    for item, n in nums:
        copy = dict(item)
        if n is None:
            copy["pct"] = 0
        elif as_pct:
            copy["pct"] = int(max(0, min(100, n)))
        else:
            copy["pct"] = int(max(0, min(100, round(n / biggest * 100))))
        out.append(copy)
    return out


def catalogue_for_prompt():
    """The component list, as the tool docstring shows it to the model."""
    return "\n".join(f"  {name} — {why}" for name, (why, _f) in KINDS.items())
