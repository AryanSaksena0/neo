"""
deck.py — Neo shows you the thing while it explains the thing.

Ask Neo to explain something and you get a voice answer. That's fine for "what
time is it" and useless for "explain this condition to me": the parts of an
explanation that are actually hard — where a thing sits, what flows into what,
what happens in which order — are spatial, and speech is not.

So: a full-screen deck, drawn not fetched, that ADVANCES ON NEO'S OWN WORDS.

The sync is the whole point, and it's the part that's easy to get wrong. The
obvious approach is to time the slides — narration is roughly N words, speech is
roughly 150 words a minute, advance every so many seconds. That drifts within
about thirty seconds and looks broken, because the model doesn't read the script
back verbatim and never speaks at a constant rate.

The live session already solves this and it took me a while to notice: it
streams `output_transcription` — Neo's own words, as it says them. So the deck
follows what Neo ACTUALLY said, not what it was supposed to say. Slide two
appears when Neo starts saying slide two. Arrows land on the word they belong
to. Nothing is estimated.

  neo.py  ->  present(topic)  ->  plan (fast)  ->  window opens, Neo starts talking
                                       |
                                       +--> visuals (background) --> streamed in
                                       |
              on_text("Neo", ...) -> Tracker -> SSE -> slide/callout advance

WHY IT'S BUILT LIKE THIS

  Two Gemini calls, not one. The first asks only for an outline and narration —
  small, fast, and it's everything Neo needs to start talking. The visuals are a
  second call that lands while Neo is still on slide one. A single call that
  returned everything would be correct and would also mean five silent seconds
  after a question, which is the thing this whole release is about not doing.

  Visuals are DRAWN, from a small vocabulary this file renders itself. Letting
  the model emit raw SVG was the first idea and the output was unusable —
  overlapping text, off-canvas paths, no consistency between slides. Giving it
  six shapes to compose instead means every deck looks deliberate, and a
  malformed spec degrades to a clean bullet slide instead of a broken page.

  A localhost HTTP server, not a file:// page. WebKit blocks fetch on file://,
  and the deck has to receive events for minutes after it loads. One stdlib
  ThreadingHTTPServer on a random port, server-sent events, no dependency.

  Its own process for the window, like canvas.py. A GUI run loop in Neo's
  process is a GUI run loop that can hang Neo.

Everything above the runtime section is pure and tested in test_neo.py.
"""

import html
import json
import math
import os
import re
import threading
import time

# How many slides a normal explanation gets. Long enough to actually develop an
# idea, short enough that Neo isn't monologuing for four minutes.
# How long present() waits for the figures before showing anything. Long
# enough that the normal case opens finished; short enough that a stalled art
# call still puts a walkthrough on screen while Neo is talking.
ART_WAIT_S = 9.0

# How long the last slide stays up after Neo stops talking. Long enough to
# read the final figure, short enough that a finished walkthrough is not a
# full-screen window you have to go and dismiss.
LINGER_S = float(os.getenv("NEO_DECK_LINGER", "6"))
# The least time any slide stays on screen, however fast the cues arrive.
MIN_SLIDE_S = float(os.getenv("NEO_MIN_SLIDE", "4.5"))
# Never hold a slide back further than this, however much audio is buffered.
# A ceiling matters because the buffer also grows when the speaker is paused or
# the machine stalls, and a deck frozen for half a minute is its own failure.
MAX_SYNC_LAG_S = float(os.getenv("NEO_SYNC_MAX_LAG", "25"))
# Five, not six. Six slides of narration is a long single turn, and a long
# single turn is what keeps getting truncated. Fewer, shorter steps means the
# whole script comfortably clears any output ceiling and each figure gets more
# time on screen.
DEFAULT_SLIDES = int(os.getenv("NEO_DECK_SLIDES", "5"))
MAX_SLIDES = 12

# Roughly what the live voice does. Only used by the safety valve below, never
# for the main sync.
WORDS_PER_SECOND = 2.6

# Cue matching. A cue is the opening of a slide's narration; we look for it in
# what Neo has just said. Not an exact match, because the model paraphrases its
# own script — enough of the distinctive words, close together, is the signal.
CUE_WORDS = 8            # how much of the narration opening becomes the cue
CUE_WINDOW = 26          # how many recent spoken words we look in
CUE_RATIO = 0.55         # fraction of cue words that must show up
LEAD_WINDOW = 4          # how recently a step's unique opening word must land
# If cue matching misses entirely, the deck must still not stall. Once Neo has
# spoken this much more than a slide's narration length, move on regardless.
# How far past a step's own word count the narration may run before the deck
# gives up waiting for the cue and moves on anyway. Generous on purpose: being
# a little late is survivable, running ahead of the voice is not.
DRIFT_TOLERANCE = float(os.getenv("NEO_DECK_DRIFT", "2.2"))

_STOP = {
    "a", "an", "the", "and", "or", "but", "so", "of", "to", "in", "on", "at",
    "is", "are", "was", "were", "be", "been", "it", "its", "this", "that",
    "these", "those", "as", "for", "with", "from", "by", "into", "about",
    "you", "your", "we", "our", "i", "im", "its", "there", "here", "not",
}

# --------------------------------------------------------------------------- #
# Text: normalising, cues, and matching what Neo actually said.
# --------------------------------------------------------------------------- #
def normalize(text):
    """Down to bare lowercase words. Punctuation, casing and spacing all differ
    between the script we wrote and the transcript of what was said, and none of
    those differences mean anything."""
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).split()


def cue_words(narration, limit=CUE_WORDS):
    """The distinctive opening of a slide, as a word list.

    Stop-words are dropped: "and then the" matches everything, which would fire
    slide three during slide one. What's left — the nouns and verbs a sentence
    actually turns on — is what identifies this slide and no other.
    """
    out = []
    for w in normalize(narration):
        if w in _STOP or len(w) < 3:
            continue
        out.append(w)
        if len(out) >= limit:
            break
    return out


def cue_hit(spoken, cue, window=CUE_WINDOW, ratio=CUE_RATIO):
    """Has Neo just started saying this? `spoken` is the whole normalised
    transcript so far; only the tail of it can constitute a start.

    Order is deliberately ignored. The model rearranges clauses constantly and
    an order-sensitive match misses those, which shows up as a slide that never
    advances — much worse than one that advances a beat early.
    """
    if not cue:
        return False
    tail = set(spoken[-window:])
    hits = sum(1 for w in set(cue) if w in tail)
    return hits >= max(1, round(len(set(cue)) * ratio))


def step_labels(step):
    """The labels on a step that have their own cue — currently the parts of a
    figure. Kept in one place so the Tracker doesn't have to know which visual
    shapes carry labels, only that some do."""
    visual = step.get("visual") or {}
    if visual.get("kind") == "figure":
        return list(visual.get("labels") or ())
    if visual.get("kind") == "chart":
        # A chart's annotations are labels with a `say`, so they ride the same
        # rails: "and then it fell back to thirty-seven" reveals the marker on
        # the point it is about, at the moment they hear about it.
        return list(visual.get("notes") or ())
    if visual.get("kind") in ("scene", "svg"):
        # A scene's BEATS are cued exactly like labels are — each one carries a
        # `say` and fires when Neo reaches those words. Routing them through
        # the same list means the Tracker needs to know nothing about scenes:
        # beat j of step i arrives as callout (i, j), and the page decides
        # whether that means "reveal a label" or "move the disc".
        return list(visual.get("beats") or ())
    return []


def plan_cues(steps):
    """Attach a cue and a spoken-length budget to every step and every label.
    Pure, and it's what the Tracker runs on."""
    out = []
    for s in steps or ():
        narration = s.get("narration", "") or ""
        beats = (s.get("visual") or {}).get("kind") in ("scene", "svg")
        labels = [{"cue": cue_words(l.get("say") or l.get("text") or "", 5),
                   "text": l.get("text", "") or l.get("say", ""),
                   "is_beat": beats}
                  for l in step_labels(s)]
        out.append({"cue": cue_words(narration),
                    "words": max(1, len(normalize(narration))),
                    "callouts": labels})

    # The LEAD WORD. Measured against the real transcript in neo.log, cue_hit
    # needs about five words of a step before it has seen enough of the cue to
    # fire — so every slide changed roughly a second and a half after Neo
    # started that paragraph, consistently late by the same margin.
    #
    # But a step usually opens on a word that belongs to it and to no other
    # step in the deck: "Carbohydrates", "Lipids", "Proteins", "Nucleic". One
    # of those is already proof, on its own, that Neo has moved on. So a first
    # cue word that appears in no other step's cue fires that step by itself,
    # and the lag drops from five words to one.
    #
    # What makes it safe is that the word must not have been said ALREADY.
    # Checked against every earlier step's whole narration, not just its cue:
    # the cue is the first eight content words, so a word appearing in the
    # middle of the previous paragraph would sail past a cue-only check and
    # run the deck a paragraph ahead of the voice.
    #
    # A LATER step reusing the word is fine and must not disqualify it. The
    # last step of a good explainer recaps — "together, these four:
    # carbohydrates, lipids, proteins and nucleic acids" — and a
    # whole-deck uniqueness test therefore disqualified every one of those
    # openings, which is most of the value. The Tracker only ever looks one
    # step ahead, so a word can only fire the step it belongs to.
    said_before = set()
    for step, s in zip(out, steps or ()):
        lead = step["cue"][0] if step["cue"] else ""
        step["lead"] = lead if lead and lead not in said_before else ""
        said_before.update(normalize(s.get("narration", "") or ""))
    return out


class Tracker:
    """Turns a stream of 'what Neo just said' into slide and callout events.

    Only ever moves forward. A deck that jumps backwards because a word
    recurred is worse than one that's slightly late, so once a slide is behind
    us it stays behind us.
    """

    def __init__(self, slides, drift=DRIFT_TOLERANCE, min_dwell=None,
                 clock=time.time):
        self.plan = plan_cues(slides)
        self.drift = drift
        # A FLOOR ON HOW LONG A SLIDE STAYS UP. The cue matcher is accurate to
        # a word or two, which is exactly the problem: a short paragraph, or
        # two paragraphs whose openings arrive together in one transcript
        # fragment, would advance the deck twice in under a second. the user sees
        # a figure appear and vanish before they have looked at it.
        #
        # A deferred advance is not a lost one — `spoken` keeps accumulating,
        # so the same cue fires on the next feed once the floor has passed.
        # The deck ends up slightly behind the voice rather than ahead of it,
        # which is the side to err on.
        self.min_dwell = MIN_SLIDE_S if min_dwell is None else min_dwell
        self._clock = clock
        self._last_advance = None
        self.spoken = []
        self.index = 0              # slide currently shown
        self._opened = False        # has slide 0 been announced yet?
        self.shown_callouts = set()  # (slide, callout) already fired
        self._budget = 0            # words the slides so far were supposed to take
        self._drifted = False       # last advance was the timeout, not a cue

    def feed(self, text):
        """Add a fragment of Neo's speech. Returns a list of events:
        ("slide", i) or ("callout", i, j)."""
        self.spoken.extend(normalize(text))
        events = []
        # THE FIRST SLIDE HAS TO BE ANNOUNCED. index starts at 0 and _events()
        # can only ever emit i >= 1, while the page opens on the title card at
        # -1 — so slide 0's figure was in the DOM and never revealed, on every
        # deck ever built. The title card held through the whole first
        # paragraph and then jumped to slide 2. Six-slide decks showed five
        # figures. Emit slide 0 the moment Neo says anything at all.
        if not self._opened and self.spoken and self.plan:
            self._opened = True
            self._last_advance = self._clock()
            events.append(("slide", 0))
        return events + self._events()

    def _events(self):
        events = []
        # Reset PER FEED. The brake below allows at most one timeout-driven
        # advance per batch of speech, so an off-script narration walks the
        # deck one step at a time instead of jumping to the end — but it must
        # never latch, or a deck that drifts once freezes for good.
        self._drifted = False
        # Slides first: a callout belongs to whichever slide is current.
        while self.index + 1 < len(self.plan):
            nxt = self.plan[self.index + 1]
            spoken_here = len(self.spoken) - self._budget
            due = spoken_here > self.plan[self.index]["words"] * self.drift
            # The drift fallback exists for a step whose cue is never matched.
            # It must NOT cascade: once it has fired, the next step gets its
            # own full budget before the fallback can fire again, or a
            # narration that has wandered off-script walks the whole deck to
            # the end in a couple of feeds. Only a real cue match may advance
            # more than one step in a single pass.
            if due and self._drifted:
                break
            lead = nxt.get("lead")
            # Only the last few words: the lead word has to have JUST been
            # said. Looking further back would fire on a word from the middle
            # of the previous paragraph and run the deck ahead of the voice,
            # which is the one failure worse than being late.
            just_said = bool(lead) and lead in self.spoken[-LEAD_WINDOW:]
            if just_said or cue_hit(self.spoken, nxt["cue"]) or due:
                # Hold. The cue is real and will still be there next feed.
                if self._last_advance is not None and \
                        self._clock() - self._last_advance < self.min_dwell:
                    break
                self._drifted = due and not (just_said or cue_hit(self.spoken,
                                                                  nxt["cue"]))
                self._budget += self.plan[self.index]["words"]
                self.index += 1
                self._last_advance = self._clock()
                events.append(("slide", self.index))
                continue
            break

        cur = self.plan[self.index] if self.index < len(self.plan) else None
        for j, c in enumerate(cur["callouts"] if cur else ()):
            if (self.index, j) in self.shown_callouts:
                continue
            # A figure LABEL with no cue belongs to the slide's entrance — show
            # it rather than never showing it. A scene BEAT is different: beats
            # are a sequence, and firing three cueless beats in one JS task
            # collapses every CSS transition, so the disc teleports to its final
            # state with no motion — which is the entire point of the shape.
            # An uncued beat waits for its turn instead.
            uncued_ok = (not c["cue"]) and not c.get("is_beat")
            if uncued_ok or cue_hit(self.spoken, c["cue"], window=18, ratio=0.6):
                self.shown_callouts.add((self.index, j))
                events.append(("callout", self.index, j))
        return events


# --------------------------------------------------------------------------- #
# Asking the model for a deck. Two calls: narration first (fast), art second.
# --------------------------------------------------------------------------- #
def plan_prompt(topic, context_note="", n=DEFAULT_SLIDES):
    """Outline + narration only. Deliberately says nothing about visuals — this
    call is on the critical path to Neo's first word and every extra token in
    the answer is silence the user is sitting through."""
    return f"""Plan a short spoken explainer about: {topic}

{context_note}

This is an educational explainer for one person who is trying to understand
the subject — the same job a textbook chapter or a lecture does. Explain the
subject accurately and plainly. For anything medical, explain the mechanism
and the terms, and leave diagnosis and treatment decisions to clinicians;
say so in the last step rather than declining to explain.

Write it to be SAID OUT LOUD over slides, not read. Conversational, direct, no
lists, no markdown, no reading out URLs or citations. Assume a smart adult who
does not know this subject.

Each slide's narration is TWO or THREE sentences — 45 to 70 words. Enough to
actually teach the step: say the thing, then say why it matters or what it
leads to. A slide whose narration is one short sentence is on screen for four
seconds, which is not long enough to look at the figure at all.

It must OPEN with the distinctive words of that slide, because the slide
changes when those words are spoken. Never open a slide with a filler phrase
like "and then" or "so basically".

No bullet points anywhere in the narration. The screen shows a labelled figure
and a short caption; the narration carries the explanation. So write the
narration as the whole explanation, not as commentary on something else.

Every slide also needs a "title": 2 to 5 words naming what is ON SCREEN for
that step. It is a caption in the corner of the figure, not a sentence and not
a heading to read aloud — "The conduction system", "Rate vs rhythm control".
Never say it out loud; it is only there so the picture has a subject.

Return ONLY JSON, no code fence:

{{"title": "<4-6 words, the subject itself>",
  "takeaway": "<ONE sentence, under 14 words: the thing worth remembering>",
  "slides": [
    {{"title": "<2-5 words naming this figure>",
      "narration": "<what Neo says while this figure is up>"}}
  ]}}

Exactly {n} steps. Build an argument across them — set up, develop, land it.
The last one should leave them with the thing that actually matters."""


# How many steps go into ONE visuals call.
#
# the user asked whether a 32k output ceiling was too low, and they were right to:
# six steps of drawn SVG can genuinely run past it, and a truncated answer is
# the WORST failure mode here — the JSON stops mid-object, nothing parses, and
# every step silently becomes plain text. Raising the ceiling alone just moves
# the cliff. Asking for three steps at a time means each answer is roughly half
# the size, the calls run in parallel so it is no slower, and one batch failing
# costs three steps instead of the whole walkthrough.
# ONE step per call. This was 3, and three detailed SVG figures in a single
# answer is simply too much to generate: measured on the real key, batches took
# 17s, 44s and 45s+, and the 504 DEADLINE_EXCEEDEDs in neo.log are the model
# giving up mid-figure. The whole art phase then expired with nothing, and a
# batch that DID succeed landed four seconds after the budget closed.
#
# One figure per call is a smaller answer, so it comes back in a few seconds
# instead of most of a minute; the calls run in parallel so six of them are no
# slower than two; and a failure now costs ONE slide instead of three.
VISUALS_BATCH = 1
# The whole art phase, not per batch.
ART_BUDGET_S = float(os.getenv("NEO_ART_BUDGET", "110"))
# One figure, not three. 64k invited the model to keep drawing until the
# request timed out — the ceiling was acting as a target.
MAX_OUTPUT_TOKENS = 12_000
# How long ONE figure may take before we stop waiting on it. Without this a
# single hung call held a worker for the entire budget.
# Measured: gemini-3.6-flash answered one figure in 41 s and had three others
# killed at 26 s by this timeout — the "504 DEADLINE_EXCEEDED" in the log is
# OUR deadline expiring, not the server's. A slow figure that arrives still
# beats a fast line of fallback text, and ART_BUDGET_S caps the phase anyway.
BATCH_TIMEOUT_S = float(os.getenv("NEO_ART_CALL_TIMEOUT", "48"))


# --------------------------------------------------------------------------- #
# The shape vocabulary, one spec per kind.
#
# These used to be one 19,000-character f-string sent in FULL on every figure
# call — the whole catalogue, plus a 6,800-character tutorial on hand-drawing
# SVG, for a model being asked to produce a single figure. Measured: 4,742
# tokens per call, five calls a deck, on a key with a 20-a-day allowance.
#
# Worse, it left the CHOICE of shape to the model, and the model chose badly:
# a five-slide deck came back with `versus` twice, and the one figure that
# rendered at all was `svg`. So the choice moves into code (choose_shape) and
# each call now carries only the spec for the shape it is being asked to draw.
# --------------------------------------------------------------------------- #
SHAPE_SPECS = {
    'figure': """{"kind":"figure","image":"<wikimedia commons search, 2-5 words>",
  "shape":"heart|brain|lung|kidney|cell|circle|none",
  "labels":[{"text":"Left atrium","x":38,"y":42,"say":"words from THIS step's
   narration where this label should appear"}]}
   A real picture or a simple organ silhouette with leader lines pointing at
   its parts — the classic labelled figure. BEST CHOICE for anatomy, a device,
   a specimen, anything with named parts. Prefer it whenever the subject is a
   real physical thing. `image` searches Wikimedia Commons for a real
   photograph or medical illustration; leave it out and the shape is drawn
   instead. x and y are percent of the picture, 6-94. 2-5 labels.""",

    'flow': """{"kind":"flow","nodes":[{"id":"a","label":"Short name","sub":"one detail",
   "x":18,"y":30,"tone":"blue|amber|green|rose|slate"}],
   "edges":[{"from":"a","to":"b","label":"optional","flow":true}]}
   Boxes with arrows that ANIMATE — set flow:true and a pulse travels along
   that arrow continuously, which is what makes a mechanism read as motion
   rather than a static picture. Use for how something works or moves.
   3-6 nodes, x/y are percent 10-90, keep them 22 apart horizontally or 18
   vertically so nothing collides.""",

    'crosssection': """{"kind":"crosssection","layers":[{"label":"Outer","sub":"what it does",
   "tone":"blue"}],"style":"rings|stack"}
   Concentric rings ("rings", for anything with an inside and an outside — a
   vessel wall, the earth, a cell) or stacked bands ("stack", for anything with
   a top and a bottom). 3-5 layers, outermost or topmost first.""",

    'process': """{"kind":"process","steps":[{"label":"Stage","sub":"what happens here"}]}
   3-6 stages, each revealed in turn as it is described. Use for sequence.""",

    'versus': """{"kind":"versus","left":{"title":"This","points":["...","..."],"tone":"green"},
   "right":{"title":"That","points":["..."],"tone":"rose"}}
   Two panels side by side. Use for a real contrast: this vs that, before and
   after, what works vs what doesn't. 2-4 points each, under 7 words.""",

    'svg': """{"kind":"svg","caption":"Lumbar spine — L4/L5 disc herniation",
  "svg":"<g data-part='disc'>...</g><g data-part='roots'>...</g>",
  "beats":[{"say":"words from this step's narration",
             "set":{"disc":{"dx":9,"scale":1.2,"tone":"rose"},
                    "roots":{"squash":0.45,"flash":true}},
             "note":"one short line for this beat"}]}
   ############################################################
   ## LAST RESORT. Use one of the shapes ABOVE unless the      ##
   ## subject is a literal physical OBJECT whose shape is the  ##
   ## point — an organ, a bone, a cross-section of a device.   ##
   ## Never use it for a process, a comparison, a system, a    ##
   ## quantity or anything with steps: those have real shapes  ##
   ## above and those shapes always come out right.            ##
   ############################################################

   This used to say it was the default and to reach for it first. That was
   wrong, and it is the single reason a deck looked broken: a flash model
   free-handing an anatomical plate produces overlapping labels, off-canvas
   paths and ellipses-called-organs, which is exactly why this file's own
   header says raw model SVG "was tried and was unusable". The composed shapes
   render identically every time because THIS FILE draws them; only their
   content comes from you. Prefer a correct schematic to a bad drawing.

   If you do use it: draw it properly, like a textbook plate.

   It also animates, exactly like `scene` does, through `beats` below. So there
   is no reason to drop to `scene`'s primitives to get movement: you get real
   anatomy AND movement here. A blob labelled "disc" next to some straight
   lines labelled "cauda equina" teaches nothing and looks like a placeholder.
   If you are about to draw an ellipse and call it an organ, stop and draw the
   organ.

   `svg` is the INNER markup of an SVG — no <svg> wrapper, no scripts, no
   external images. The frame is fixed for you:
     • viewBox is 0 0 1000 620. Stay inside it. Leave ~40 units of margin.
     • Dark background (#07090d). Draw in LIGHT strokes on dark.
     • Palette: #8fb6ff blue · #f4c66a amber · #5fd0aa green · #f2a0b3 rose
       · #a3b0c6 slate · #eef2f8 near-white. Use fill-opacity .12-.3 for
       filled organs, stroke-width 2-2.5 for outlines.
     • Text: font-size 17-21 for labels, 14-15 for notes, fill #eef2f8 or the
       part's colour. ALWAYS add text-anchor. Never let two labels overlap —
       lay them out deliberately, with leader lines if you need them.

   HOW TO MAKE IT LOOK EXPENSIVE, not like clip art:
     • BUILD FROM CURVES. Use <path> with C and Q curves for anything organic.
       Bone, muscle, vessels and organs have no straight edges and no perfect
       circles. A figure made of <ellipse> and <rect> reads as a placeholder
       no matter how it is labelled.
     • LAYER IT. Draw the deep structures first and the near ones over them,
       each as its own <g>. Depth is what separates a diagram from a doodle.
     • SHADE IT. Define gradients in <defs> and fill with them —
       <defs><linearGradient id="bone" x1="0" y1="0" x2="0" y2="1">
       <stop offset="0" stop-color="#eef2f8" stop-opacity=".22"/>
       <stop offset="1" stop-color="#8fb6ff" stop-opacity=".06"/>
       </linearGradient></defs> then fill="url(#bone)". Give every solid
       structure a gradient rather than a flat fill.
     • REPEAT STRUCTURE. Things that come in series — vertebrae, nerve roots,
       ribs, tubules, alveoli, teeth on a gear — must actually be drawn as a
       series, each one slightly different in size and angle the way a real one
       is. Nine identical horizontal lines is not a nerve bundle.
     • COMPOSE THE FRAME. Put the subject large and slightly left, and the
       labels in the space you left on the right, with thin leader lines
       (stroke-width 1, stroke="#a3b0c6", opacity .5) reaching the structures
       they name. Fill the 1000x620. A small drawing floating in the middle of
       a black rectangle looks unfinished.
     • ONE ACCENT. Draw the whole figure in slate and near-white, then use ONE
       colour for the structure this step is actually about. A drawing where
       everything is coloured has nothing emphasised.

   This is the standard. Match this density and this composition:

     <defs><linearGradient id="bone" x1="0" y1="0" x2="0" y2="1">
       <stop offset="0" stop-color="#eef2f8" stop-opacity=".26"/>
       <stop offset="1" stop-color="#8fb6ff" stop-opacity=".05"/>
     </linearGradient></defs>
     <g data-part="canal"><path d="M300,70 C286,180 284,300 292,420 C296,470
       302,510 312,548" fill="none" stroke="#a3b0c6" stroke-opacity=".35"
       stroke-width="26"/></g>
     <g data-part="vertebrae">
       <path d="M244,96 C244,80 268,74 300,74 C334,74 356,82 356,98 C356,116
         334,124 300,124 C266,124 244,114 244,96 Z" fill="url(#bone)"
         stroke="#cdd8ea" stroke-width="2"/>
       ...three more, each a little lower, wider and differently angled...
     </g>
     <g data-part="disc"><path d="M348,276 C378,272 396,282 394,292 C392,302
       372,306 350,299" fill="#f2a0b3" fill-opacity=".5" stroke="#f2a0b3"
       stroke-width="2.2"/></g>
     <g data-part="roots">
       ...five separate curves fanning down and apart, each thinner and
       fainter than the last...
     </g>
     <line x1="394" y1="288" x2="612" y2="266" stroke="#a3b0c6"
       stroke-opacity=".5" stroke-width="1"/>
     <text x="622" y="262" font-size="19" fill="#f2a0b3">Herniated nucleus
       pulposus</text>
     <text x="622" y="286" font-size="14" fill="#a3b0c6">L4/L5, extruded
       posteriorly</text>

   Note what that does: curves everywhere, a gradient on the solid structures,
   the repeating parts genuinely repeated and each one different, one accent
   colour on the thing the step is about, and the labels in a right-hand gutter
   on thin leader lines. Do that, for whatever the subject is.

   Real proportions, real structures, the parts a textbook would show and in
   the places they actually are. A vertebra has a body, pedicles, a canal and a
   spinous process; a nephron has a glomerulus, a loop and a collecting duct;
   the cauda equina is a spray of individual roots fanning down and outward
   from the conus, not a rectangle. **40 to 150 elements is normal for a good
   figure, and under 20 is a bad one.** Label every structure you draw.

   To ANIMATE, wrap a structure in <g data-part="name"> and drive it from
   `beats`: dx/dy (move), scale, squash (0.45 is a hard pinch), rotate,
   opacity, tone, flash. `say` is copied word for word from the narration and
   is when that beat fires. This is how "the disc bulges and compresses the
   roots" becomes something the user watches happen.""",

    'scene': """{"kind":"scene","stage":"body|plain","caption":"Lumbar spine — L4/L5",
  "parts":[{"id":"disc","shape":"disc","x":44,"y":58,"w":11,"h":5,"tone":"amber","label":"Disc"},
           {"id":"nerves","shape":"tube","x":62,"y":58,"w":20,"h":14,"tone":"blue",
             "strands":9,"label":"Cauda equina"}],
  "beats":[{"say":"words from the narration where this happens",
             "set":{"disc":{"dx":1.9,"scale":1.35,"tone":"rose"},
                    "nerves":{"squash":0.4,"tone":"rose","flash":true}},
             "note":"one short line shown on screen for this beat"}]}
   A LAST RESORT, and almost never the right answer. Read this before using it.

   `scene` builds a picture out of seven fixed primitives — a disc is an
   ellipse, a tube is a stack of straight lines, a blob is a blob. That is its
   ceiling. Asked for a herniated disc compressing the cauda equina it produces
   a yellow ellipse beside nine parallel lines on a stick figure, which is not
   a medical figure, it is a placeholder that happens to have the right words
   next to it.

   `svg` animates through the same beats and can draw the actual anatomy, so
   ANYTHING you were about to build here belongs there instead. Use `scene`
   only when the subject is genuinely abstract shapes moving — a signal passing
   between blocks, a valve opening, pressure building in a vessel — and there
   is nothing real to draw. If the step names a body part, an organ, a cell or
   a device, use `svg`.

   `stage`:"body" draws a see-through human behind the scene, for anatomy.
   `shape` is disc | tube | bone | blob | arrow | dot | bar. A `tube` is a
   BUNDLE of strands — nerves, vessels, fibres — and squashing one is the most
   useful animation available.
   Each beat's `set` changes named parts: dx/dy (move, roughly -10..10),
   scale, squash (vertical squeeze — 0.4 is a hard pinch), rotate, opacity,
   tone, flash:true. `say` is copied WORD FOR WORD from this step's narration
   and is when the beat fires. 2-6 beats, in the order you say them, each one
   a visible change. Give a step 3-6 parts, not twenty.""",

    'molecule': """{"kind":"molecule","caption":"Water — H2O",
  "atoms":[{"el":"O","x":50,"y":38,"lone":2},
           {"el":"H","x":31,"y":62},{"el":"H","x":69,"y":62}],
  "bonds":[{"from":0,"to":1,"order":1},{"from":0,"to":2,"order":1}]}
   A REAL STRUCTURAL FORMULA — the actual drawing, not a box with a name in it.
   Use it for any molecule, Lewis dot structure, functional group or skeletal
   formula: water, methane, CO2, glucose, an amino acid, a benzene ring, a
   phospholipid. `el` is the element symbol (or "R" for a variable group), x/y
   are percent 8-92 across and 10-88 down, `lone` is the number of LONE PAIRS
   on that atom (0-4, drawn as dots — this is what makes it a Lewis structure),
   `charge` is "+" or "-", `label` names a group under the atom ("carboxyl",
   "amino"). `order` on a bond is 1, 2 or 3 for single, double, triple.
   Lay the atoms out the way a textbook does: bent for water, linear for CO2,
   a hexagon for a ring. 2-24 atoms. This is the best shape you have for
   chemistry — reach for it before a box diagram.""",

    'atom': """{"kind":"atom","symbol":"C","name":"Carbon","protons":6,"neutrons":6,
  "shells":[2,4]}
   A Bohr shell diagram — nucleus, electron shells, electrons drawn in them,
   with the proton/neutron/valence numbers listed beside it. Use it for atomic
   structure, valence, why an element bonds the way it does, ions. `shells` is
   the electron count per shell from the inside out.""",

    'chart': """{"kind":"chart","chart":"line|bar|area|scatter",
  "caption":"Chipotle share price, split-adjusted",
  "x":{"label":"Year","ticks":["2024","2025","2026"]},
  "y":{"label":"$ / share","min":0,"max":70,"ticks":["0","35","70"]},
  "series":[{"name":"CMG","tone":"green","points":[[0,52],[1,69],[2,37]]}],
  "notes":[{"x":1,"y":69,"text":"all-time high","say":"reached an all-time
   high"}]}
   A REAL PLOTTED GRAPH with drawn axes, gridlines and labelled ticks — the
   line draws itself as the step opens. Use it whenever the step is a QUANTITY
   THAT CHANGES: a price over time, a population curve, a dose response, a
   reaction rate, results across groups, anything with a trend or a peak.
   `points` are [x, y] pairs in your own units; x is a plain number (0,1,2… for
   evenly spaced categories, or a real value), and the axis maths is done for
   you — do NOT try to compute pixel positions. `ticks` are the labels down the
   value axis and along the category axis. Up to 4 series on one chart, and
   name each one. `notes` are annotations pinned to a data point, and each one
   has a `say` copied word for word from the narration, so the marker appears
   on the point at the moment Neo talks about it. Reach for this instead of
   describing a number in words, and instead of hand-drawing a chart in `svg` —
   axes drawn by code are straight and correctly scaled, and yours will not be.""",

    'grid': """{"kind":"grid",
  "cols":[{"title":"Carbohydrates","tone":"amber"},{"title":"Lipids","tone":"green"}],
  "rows":[{"label":"Monomer","cells":["Monosaccharide (glucose)","Fatty acids + glycerol"]},
          {"label":"Bond","cells":["Glycosidic linkage","Ester linkage"]},
          {"label":"Main job","cells":["Fast fuel, cell walls","Membranes, long-term store"]}]}
   A REAL COMPARISON TABLE — 2-5 things across the columns, 2-6 properties down
   the rows. This is the densest teaching shape here and usually the right
   answer when a step covers several things, or several facts about each of
   several things. Cells are short clauses, not single words: name the actual
   molecule, the actual bond, the actual number. Prefer this over `versus`
   whenever there are more than two things or more than two properties.""",

    'number': """{"kind":"number","value":"5x","label":"what it counts",
   "sub":"one line of context"}
   One figure that carries the whole step. Use it when a number IS the point.""",
}

# The image guidance used to live in a global "CHOOSING BETWEEN A REAL PICTURE
# AND A DRAWING" section, most of which argued about when to hand-draw SVG
# instead. Code picks the shape now, so that argument is gone — but WHEN a
# figure should carry a photograph is still a real decision the model has to
# make, so it moves onto the shape it actually belongs to.
SHAPE_SPECS["figure"] += """

   ALWAYS PREFER A REAL PICTURE. Neo now looks at what comes back: it judges
   whether the picture is the right subject and good enough to learn from, it
   locates each part, and a second blind check confirms every marker before it
   is drawn. Anything that fails is dropped. So a fetched plate is no longer a
   guess — it is the best figure available, and it beats anything drawn.

   FETCH A REAL PICTURE when the subject has a real appearance a textbook has
   already drawn — an organ, a bone, a joint, a cell, a tissue, a specimen, a
   device. A published plate has real proportions, real texture and correct
   spatial relationships, and for someone trying to learn the subject it is
   worth more than the neatest schematic. Set `image` to the search.

   Write the search the way a textbook caption is titled: "lumbar disc
   herniation anatomy", "nephron kidney structure", "cardiac conduction system
   diagram". Not "back pain", not "the spine".

   LEAVE `image` OUT only when the subject is genuinely abstract — an idea, a
   comparison, a sequence of events, a quantity. Those have real shapes above.
   Anything with a PHYSICAL FORM should ask for a picture.

   `labels` are only needed when there is NO image: when a picture is fetched,
   Neo finds and verifies the parts itself from the picture."""


PROMPT_HEADER = """Design the visuals for a spoken walkthrough. The narration is
already written and CANNOT change — draw what is being said, step by step.

This is a teaching figure on a dark screen, the way a good textbook or a
Kurzgesagt explainer looks. It is NOT a slide deck. There are no titles, no
headings, no bullet lists, and no slide numbers — the voice does that work.
Every step is ONE figure. Anything you put on screen must be a label ON that
figure, not prose beside it.

{steps}

Return ONLY JSON, no code fence: {{"visuals": [ <one object per step, in order> ]}}

Choose the shape that genuinely fits the step.
"""

PROMPT_RULES = """Rules that matter:
  - TEACH. A screen with four words on it explains nothing. Aim for the density
    of a good textbook figure: real terms, real numbers, real mechanism. If a
    step could be understood without the picture, the picture is too thin.
  - Labels up to 6 words, a `sub` up to 12, a grid cell a short clause. USE that
    room. "Amino acid" teaches nothing; "20 kinds, joined by peptide bonds"
    teaches something.
  - Use the real vocabulary — name the actual structures, molecules and numbers.
    They are trying to learn the subject, not be reassured.
  - `say` on a label must be copied WORD FOR WORD from that step's narration.
    The label flies in at the moment Neo speaks those words, so it has to
    match. Give every figure label a `say`."""

# Shapes that can be built from ANY prose, so there is always something to fall
# back to that is still a figure rather than a sentence on a black screen.
SAFE_SHAPES = ("process", "versus", "grid", "number")

# What each shape is FOR, as words that actually appear in narration. Order
# inside a tuple does not matter; the weights do.
_SHAPE_SIGNALS = {
    "process": (3, ("first", "then", "next", "step", "stage", "begins",
                    "starts with", "after that", "finally", "followed by",
                    "leads to", "once", "before", "sequence", "phase")),
    "versus": (3, ("versus", " vs ", "compared", "unlike", "whereas",
                   "difference between", "on the other hand", "instead of",
                   "two approaches", "either", "rather than", "trade-off",
                   "tradeoff", "advantage", "disadvantage", "pros and cons")),
    "chart": (3, ("grew", "rose", "fell", "increase", "decline", "declined",
                  "per year", "over time", "trend", "percent", "%", "doubled",
                  "tripled", "growth", "dropped", "peaked")),
    "crosssection": (3, ("layer", "layers", "inside", "outer", "inner", "wall",
                         "surface", "core", "beneath", "cross-section",
                         "cross section", "membrane", "surrounds")),
    "flow": (2, ("causes", "leads to", "triggers", "flows", "signal",
                 "travels", "pathway", "loop", "feedback", "input", "output",
                 "sends", "carries", "propagates", "chain reaction")),
    "grid": (2, ("four types", "three types", "categories", "kinds of",
                 "types of", "each of", "classes of", "families of",
                 "compare across", "properties")),
    "molecule": (4, ("molecule", "covalent", "bond", "h2o", "co2",
                     "lewis structure", "shares electrons")),
    "atom": (4, ("electron", "proton", "neutron", "nucleus", "shell",
                 "orbital", "isotope")),
    "number": (2, ("times more", "times faster", "only", "just", "billion",
                   "million", "thousand", "per second", "per day")),
    "figure": (2, ("anatomy", "organ", "structure of", "parts of", "located",
                   "sits", "shaped", "consists of", "made up of")),
}
# Subjects we hold a real silhouette for — a strong pull toward `figure`.
# Subjects with a real physical form. A verified photograph beats every drawn
# shape for these, so they pull hard toward `figure`.
_FIGURE_SUBJECTS = (
    "heart", "brain", "lung", "kidney", "cell", "atrium", "ventricle",
    "artery", "vein", "neuron", "muscle", "bone", "nephron", "liver",
    "stomach", "intestine", "eye", "ear", "skin", "spine", "vertebra",
    "joint", "tooth", "nerve", "gland", "membrane", "mitochondri",
    "chloroplast", "engine", "turbine", "valve", "pump", "chip",
    "transistor", "battery", "lens", "reactor", "wing", "leaf", "root",
    "flower", "seed", "volcano", "glacier", "fault", "atom", "molecule")


def choose_shape(narration, title="", specs=None, safe=None):
    """Which shape this step should be drawn as. Returns (primary, alternate).

    Pure, so it can be checked against real narration without a model.

    The model used to make this call and it made it badly — it has no memory of
    what the previous slide used, so it repeated shapes, and it reached for the
    one shape (`svg`) that this file already documents as unusable. Code has
    the whole deck in view and can simply decide.
    """
    signals = _SHAPE_SIGNALS if specs is None else specs
    safe = SAFE_SHAPES if safe is None else safe
    text = f"{title} {narration}".lower()
    scored = {}
    for kind, (weight, words) in signals.items():
        hits = sum(1 for w in words if w in text)
        if hits:
            scored[kind] = weight * hits
    if any(w in text for w in _FIGURE_SUBJECTS):
        scored["figure"] = scored.get("figure", 0) + 4
    # A bare number with a unit is a chart only if something MOVES; otherwise
    # it is one statistic, which is what `number` is for.
    if re.search(r"\d", text) and "chart" not in scored:
        scored["number"] = scored.get("number", 0) + 1

    primary = max(scored, key=scored.get) if scored else "process"
    # The alternate must be renderable from any prose, and must not be the
    # primary — it is what gets tried when the primary comes back unusable.
    alt = next((k for k in safe if k != primary), "process")
    return primary, alt


def plan_shapes(slides, chooser=None):
    """A (primary, alternate) pair for every step, avoiding three of the same
    shape in a row. Pure.

    Variety is a real property of a good walkthrough and the model could not
    see it: it answers one step at a time and has no idea what the others got.
    """
    chooser = choose_shape if chooser is None else chooser
    out = []
    for s in slides or ():
        primary, alt = chooser(s.get("narration", ""), s.get("title", ""))
        if len(out) >= 2 and out[-1][0] == out[-2][0] == primary:
            primary, alt = alt, primary      # break the run
        out.append((primary, alt))
    return out


def visuals_prompt(plan, offset=0, kinds=None):
    """The second call. It gets the finished narration, so the picture matches
    what is actually said, and now only the spec for the shape being asked for.
    """
    steps = "\n".join(
        f'{i + offset}. {s.get("narration","")}'
        for i, s in enumerate(plan.get("slides") or ()))
    wanted = [k for k in (kinds or ()) if k in SHAPE_SPECS] or list(SHAPE_SPECS)
    # Dedupe, keep order.
    seen, order = set(), []
    for k in wanted:
        if k not in seen:
            seen.add(k)
            order.append(k)
    catalogue = "\n\n".join(SHAPE_SPECS[k] for k in order)
    only = ""
    if len(order) == 1:
        only = (f'\n\nUse the "{order[0]}" shape for this step. It has been '
                f'chosen already — do not substitute another kind.')
    elif len(order) <= 3:
        only = ("\n\nUse ONE of the shapes above — whichever genuinely fits "
                "this step. Do not invent another kind.")
    return (PROMPT_HEADER.replace("{steps}", steps)
            + "\n\n" + catalogue + "\n\n" + PROMPT_RULES + only)


def _loads(text):
    """JSON out of a model answer. Fences, prose either side, trailing commas —
    all of it happens, none of it should cost the deck."""
    raw = (text or "").strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    body = raw[start:end + 1]
    for attempt in (body, re.sub(r",\s*([}\]])", r"\1", body)):
        try:
            return json.loads(attempt)
        except Exception:
            continue
    # Nothing parsed whole. raw_decode reads the first complete value and
    # ignores whatever trails it, which rescues the common case of a model
    # appending a sentence of commentary after the closing brace.
    try:
        return json.JSONDecoder().raw_decode(raw[start:])[0]
    except Exception:
        return None


def _objects_in(text):
    """Every complete {...} object in `text`, in order, skipping the broken ones.

    The point is TRUNCATION. A visuals answer that stops mid-object used to
    cost all three of its steps their artwork, because the outer array never
    closed and json.loads refused the lot — the real log line was
    `had no usable JSON (first 90 chars: '```json\\n{\\n  "visuals": [')` with
    two perfectly good figures sitting inside the part that did arrive.
    Scanning brace depth, and respecting strings so a `}` inside an SVG path or
    a label can't fool it, gets those two back.
    """
    out, stack, in_str, esc = [], [], False, False
    for i, ch in enumerate(text or ""):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            stack.append(i)
        elif ch == "}" and stack:
            start = stack.pop()
            depth = len(stack)
            # Objects are recorded at EVERY level, because the level that
            # matters depends on where the answer was cut. A whole answer has
            # the wrapper at depth 0 and the figures at depth 1; a truncated
            # one never closes the wrapper at all, and the figures — which are
            # the entire point — are the only complete objects there are.
            #
            # Depth 2 is the floor on purpose: it covers wrapper -> visual ->
            # a nested field, and stopping there keeps this linear on the
            # deeply nested markup a `svg` step can contain, instead of
            # re-parsing every enclosing object once per closing brace.
            if depth > 2 or len(out) > 400:
                continue
            chunk = text[start:i + 1]
            for attempt in (chunk, re.sub(r",\s*([}\]])", r"\1", chunk)):
                try:
                    out.append((depth, json.loads(attempt)))
                    break
                except Exception:
                    continue
    return out


def loads_visuals(text):
    """The visuals list out of an art answer, however badly it arrived.

    Whole JSON first. Failing that, salvage the complete objects — a batch that
    half-arrived is worth two figures, and dropping it silently turned every
    step in that batch into a line of plain text with no picture at all.
    """
    data = _loads(text)
    if isinstance(data, dict) and isinstance(data.get("visuals"), list):
        return data["visuals"]
    if isinstance(data, list):
        return data
    found = _objects_in(text or "")
    # A complete wrapper wins outright: it has every figure, in order.
    for _d, o in found:
        if isinstance(o, dict) and isinstance(o.get("visuals"), list):
            return o["visuals"]
    # Otherwise take the figures themselves, from the shallowest level that has
    # any. Going shallowest-first stops a nested object inside one figure (a
    # bond, a beat, an axis) being mistaken for a figure of its own.
    for depth in (1, 0, 2):
        got = [o for d, o in found
               if d == depth and isinstance(o, dict) and o.get("kind")]
        if got:
            return got
    return []


def parse_plan(text, topic=""):
    """Model answer -> a deck with narration but no art yet. Never raises."""
    data = _loads(text) or {}
    slides = []
    for s in (data.get("slides") or [])[:MAX_SLIDES]:
        if not isinstance(s, dict):
            continue
        narration = str(s.get("narration") or "").strip()
        if not narration:
            continue
        slides.append({"narration": narration,
                       "title": str(s.get("title") or "").strip()[:52],
                       "visual": None})
    if not slides:
        return None
    return {"title": str(data.get("title") or topic or "Neo").strip()[:70],
            # The closing beat. A walkthrough that stops dead on its last
            # figure has no ending — this is the one line worth keeping, held
            # on screen while the last of the audio plays out.
            "takeaway": str(data.get("takeaway") or "").strip()[:120],
            "slides": slides}


def _tone(value):
    return value if value in TONES else "blue"


def _txt(x, n=64):
    """Trim to n characters ON A WORD BOUNDARY. Pure.

    It used to be a bare slice, and the slice is what the user photographed:
        "~150 mm² silicon footprin"
        "converts directly to intense thermal dissipation ac"
        "or causes permanent sili"
    Three slides of words cut in half. A hard slice at 72 characters lands
    mid-word roughly six times in seven, and the result does not read as a
    length limit — it reads as a spelling mistake, which is exactly what they
    called it.
    """
    t = str(x or "").strip()
    if len(t) <= n:
        return t
    cut = t[:n]
    space = cut.rfind(" ")
    # Only back off to the last space if that keeps most of the room; a very
    # long single word has to be cut somewhere.
    if space >= n * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,;:-") + "\u2026"


def wrap_text(text, width, max_lines=3):
    """Greedy word wrap to `width` characters, at most `max_lines`. Pure.

    SVG <text> does not wrap — it draws one line, however long, straight out
    of the shape it belongs to and across whatever is next to it. That is the
    second thing in their screenshots: "Submerged blades in non-conductive
    fluid" running out of its box and through the box beside it, twice on one
    slide. Every multi-word string drawn into a fixed-width shape has to come
    through here first.
    """
    words = str(text or "").split()
    if not words:
        return []
    lines, line = [], ""
    for w in words:
        trial = f"{line} {w}".strip()
        if len(trial) <= width or not line:
            line = trial
        else:
            lines.append(line)
            line = w
            if len(lines) == max_lines:
                break
    if line and len(lines) < max_lines:
        lines.append(line)
    if len(lines) == max_lines:
        used = sum(len(x) for x in lines) + len(lines) - 1
        if used < len(" ".join(words)):
            lines[-1] = lines[-1].rstrip(" ,;:-") + "\u2026"
    return lines


def _pct(x, default=50, lo=6.0, hi=94.0):
    try:
        return max(lo, min(hi, float(x)))
    except (TypeError, ValueError):
        return float(default)


def _labels(raw, limit=6):
    out = []
    for l in _listy(raw, limit):
        if not isinstance(l, dict) or not _txt(l.get("text"), 40):
            continue
        out.append({"text": _txt(l.get("text"), 52),
                    "say": str(l.get("say") or "")[:160],
                    "x": _pct(l.get("x"), 50), "y": _pct(l.get("y"), 50)})
    return out


def _listy(x, limit):
    """A list of dicts, whatever the model actually sent. Pure.

    Every list field used to be sliced BEFORE its type was checked, so a model
    that answered `"labels": {...}` instead of `[...]` raised
    `TypeError: unhashable type: 'slice'` — and because merge_visuals folds a
    whole batch at once, one bad field silently cost the other two slides in
    that batch their artwork as well.
    """
    if isinstance(x, dict):
        x = list(x.values())          # a keyed object is a common model slip
    if not isinstance(x, (list, tuple)):
        return []
    return [i for i in list(x)[:limit] if isinstance(i, dict)]


def _ints(x, limit):
    """A list of ints, tolerantly. `"shells": 2` and `"shells": "2"` happen."""
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        x = [x]
    if isinstance(x, str):
        x = [x]
    if not isinstance(x, (list, tuple)):
        return []
    out = []
    for s in list(x)[:limit]:
        try:
            n = int(s)
        except (TypeError, ValueError, OverflowError):
            continue
        out.append(n)
    return out


def _clean_visual(v):
    """Never raises. See _safe_visual — this is wrapped."""
    """One visual spec, sanitised. Anything unusable returns None so the step
    falls back to something plain rather than rendering broken.

    The model is asked for six shapes and will occasionally invent a seventh,
    put a paragraph in a label, or place a node at x=4000. None of that should
    reach the screen, and none of it should raise.
    """
    if not isinstance(v, dict):
        return None
    kind = str(v.get("kind") or "").lower()

    if kind == "figure":
        # A figure is a PICTURE with labels on it. With no image and no shape
        # there is no picture — just dots and leader lines aimed at empty
        # space, which is exactly what reached the screen: three labels on the
        # right, three floating dots on the left, nothing in between.
        #
        # dress() strips `image` when the fetch failed, so this catches both
        # "the model never asked for one" and "Commons had nothing".
        if not str(v.get("image") or "").strip() and \
                (v.get("shape") or "none") == "none":
            return None
        shape = str(v.get("shape") or "none").lower()
        if shape not in SHAPES:
            shape = "none"
        search = _txt(v.get("image"), 60)
        labels = _labels(v.get("labels"))
        if not search and shape == "none" and not labels:
            return None
        out = {"kind": "figure", "image": search, "shape": shape,
               "labels": labels}
        # Carried through the cleaner, not invented by the model: these are
        # written by collect_images AFTER a picture has been fetched, judged
        # and verified. Dropping them here would silently turn a verified plate
        # back into a bare photograph with a word list beside it.
        for key in ("verified", "iw", "ih", "credit"):
            if v.get(key) is not None:
                out[key] = v[key]
        return out

    if kind == "flow":
        nodes, seen = [], set()
        for n in _listy(v.get("nodes"), 8):
            if not isinstance(n, dict):
                continue
            nid = _txt(n.get("id"), 24) or f"n{len(nodes)}"
            if nid in seen:
                continue
            seen.add(nid)
            nodes.append({"id": nid, "label": _txt(n.get("label"), 40),
                          "sub": _txt(n.get("sub"), 70),
                          "x": _pct(n.get("x"), 50, 10, 90),
                          "y": _pct(n.get("y"), 50, 10, 90),
                          "tone": _tone(n.get("tone"))})
        if len(nodes) < 2:
            return None
        edges = []
        for e in _listy(v.get("edges"), 12):
            if not isinstance(e, dict):
                continue
            a, b = _txt(e.get("from"), 24), _txt(e.get("to"), 24)
            if a in seen and b in seen and a != b:
                edges.append({"from": a, "to": b, "label": _txt(e.get("label"), 24),
                              "flow": bool(e.get("flow", True))})
        return {"kind": "flow", "nodes": nodes, "edges": edges}

    if kind == "crosssection":
        layers = [{"label": _txt(l.get("label"), 40), "sub": _txt(l.get("sub"), 72),
                   "tone": _tone(l.get("tone"))}
                  for l in _listy(v.get("layers"), 5)
                  if isinstance(l, dict) and _txt(l.get("label"))]
        if len(layers) < 2:
            return None
        style = "rings" if str(v.get("style") or "").lower() == "rings" else "stack"
        return {"kind": "crosssection", "layers": layers, "style": style}

    if kind == "process":
        steps = [{"label": _txt(s.get("label"), 40), "sub": _txt(s.get("sub"), 72)}
                 for s in _listy(v.get("steps"), 6)
                 if isinstance(s, dict) and _txt(s.get("label"))]
        return {"kind": "process", "steps": steps} if len(steps) >= 2 else None

    if kind == "versus":
        def side(raw, fallback_tone):
            if not isinstance(raw, dict) or not _txt(raw.get("title"), 34):
                return None
            pts = [_txt(p, 80) for p in (raw.get("points") or [])[:4] if _txt(p)]
            return {"title": _txt(raw.get("title"), 34), "points": pts,
                    "tone": _tone(raw.get("tone") or fallback_tone)}
        left, right = side(v.get("left"), "green"), side(v.get("right"), "rose")
        return ({"kind": "versus", "left": left, "right": right}
                if left and right else None)

    if kind == "svg":
        body = sanitize_svg(v.get("svg") or "")
        if len(body.strip()) < 40:
            return None                 # nothing survived: fall back to a line
        beats = []
        for b in _listy(v.get("beats"), 8):
            if not isinstance(b, dict):
                continue
            sets = {}
            _set = b.get("set")
            for pid, state in (_set.items() if isinstance(_set, dict) else ()):
                pid = _txt(pid, 24)
                # Only parts the drawing actually tagged can be animated.
                if not pid or f'data-part="{pid}"' not in body:
                    continue
                if not isinstance(state, dict):
                    continue
                clean = {}
                for k, lo, hi in (("dx", -60.0, 60.0), ("dy", -60.0, 60.0),
                                  ("scale", 0.2, 3.0), ("squash", 0.22, 3.0),
                                  ("opacity", 0.0, 1.0), ("rotate", -180.0, 180.0)):
                    if k in state:
                        try:
                            clean[k] = max(lo, min(hi, float(state[k])))
                        except (TypeError, ValueError):
                            pass
                if state.get("tone") in TONES:
                    clean["tone"] = TONES[state["tone"]]
                if state.get("flash"):
                    clean["flash"] = True
                if clean:
                    sets[pid] = clean
            say = str(b.get("say") or "")[:160]
            if sets or say:
                beats.append({"say": say, "set": sets,
                              "note": _txt(b.get("note"), 70)})
        return {"kind": "svg", "svg": body, "beats": beats,
                "caption": _txt(v.get("caption"), 70)}

    if kind == "scene":
        parts, seen = [], set()
        for p in _listy(v.get("parts"), 10):
            if not isinstance(p, dict):
                continue
            pid = _txt(p.get("id"), 24) or f"p{len(parts)}"
            if pid in seen:
                continue
            seen.add(pid)
            shape = str(p.get("shape") or "disc").lower()
            if shape not in SCENE_SHAPES:
                shape = "disc"
            parts.append({"id": pid, "shape": shape,
                          "x": _pct(p.get("x"), 50, 6, 94),
                          "y": _pct(p.get("y"), 50, 8, 90),
                          "w": _pct(p.get("w"), 16, 2, 80),
                          "h": _pct(p.get("h"), 8, 1, 70),
                          "tone": _tone(p.get("tone")),
                          "strands": max(3, min(12, int(p.get("strands") or 7)
                                                if str(p.get("strands") or 7).isdigit() else 7)),
                          "label": _txt(p.get("label"), 40)})
        if not parts:
            return None
        beats = []
        for b in (v.get("beats") or [])[:8]:
            if not isinstance(b, dict):
                continue
            sets = {}
            _set = b.get("set")
            for pid, state in (_set.items() if isinstance(_set, dict) else ()):
                if pid not in seen or not isinstance(state, dict):
                    continue
                clean = {}
                for k, lo, hi in (("dx", -60.0, 60.0), ("dy", -60.0, 60.0),
                                  ("scale", 0.2, 3.0), ("squash", 0.22, 3.0),
                                  ("opacity", 0.0, 1.0), ("rotate", -180.0, 180.0)):
                    if k in state:
                        try:
                            clean[k] = max(lo, min(hi, float(state[k])))
                        except (TypeError, ValueError):
                            pass
                if state.get("tone") in TONES:
                    clean["tone"] = TONES[state["tone"]]
                if state.get("flash"):
                    clean["flash"] = True
                if clean:
                    sets[pid] = clean
            say = str(b.get("say") or "")[:160]
            if sets or say:
                beats.append({"say": say, "set": sets,
                              "note": _txt(b.get("note"), 70)})
        return {"kind": "scene", "stage": ("body" if v.get("stage") == "body"
                                           else "plain"),
                "parts": parts, "beats": beats,
                "caption": _txt(v.get("caption"), 70)}

    if kind == "molecule":
        atoms = []
        for a in _listy(v.get("atoms"), 24):
            if not isinstance(a, dict) or not _txt(a.get("el"), 3):
                continue
            atoms.append({"el": _txt(a.get("el"), 3),
                          "x": _pct(a.get("x"), 50, 8, 92),
                          "y": _pct(a.get("y"), 50, 10, 88),
                          "lone": max(0, min(4, int(a.get("lone") or 0)
                                             if str(a.get("lone") or 0).isdigit() else 0)),
                          "charge": _txt(a.get("charge"), 3),
                          "label": _txt(a.get("label"), 26)})
        if len(atoms) < 2:
            return None
        bonds = []
        for b in _listy(v.get("bonds"), 32):
            if not isinstance(b, dict):
                continue
            try:
                i, j = int(b.get("from")), int(b.get("to"))
            except (TypeError, ValueError):
                continue
            if not (0 <= i < len(atoms) and 0 <= j < len(atoms)) or i == j:
                continue
            order = b.get("order", 1)
            # `True in (1,2,3)` is True in Python, so "order": true used to
            # survive into the renderer as a non-integer bond order.
            order = order if (isinstance(order, int) and not isinstance(order, bool)
                              and order in (1, 2, 3)) else 1
            bonds.append({"from": i, "to": j, "order": order,
                          "label": _txt(b.get("label"), 18)})
        return {"kind": "molecule", "atoms": atoms, "bonds": bonds,
                "caption": _txt(v.get("caption"), 70)}

    if kind == "atom":
        shells = []
        for s in _ints(v.get("shells"), 5):
            try:
                n = int(s)
            except (TypeError, ValueError, OverflowError):
                continue
            if 1 <= n <= 32:
                shells.append(n)
        sym = _txt(v.get("symbol"), 3)
        if not shells or not sym:
            return None
        def _int(x):
            try:
                return int(x)
            except (TypeError, ValueError, OverflowError):
                return 0
        return {"kind": "atom", "symbol": sym, "name": _txt(v.get("name"), 26),
                "protons": _int(v.get("protons")), "neutrons": _int(v.get("neutrons")),
                "shells": shells}

    if kind == "grid":
        cols = [{"title": _txt(c.get("title"), 30),
                 "tone": _tone(c.get("tone"))}
                for c in _listy(v.get("cols"), 5)
                if isinstance(c, dict) and _txt(c.get("title"))]
        rows = []
        for r in _listy(v.get("rows"), 6):
            if not isinstance(r, dict) or not _txt(r.get("label"), 30):
                continue
            _cells = r.get("cells")
            _cells = _cells if isinstance(_cells, (list, tuple)) else []
            cells = [_txt(c, 90) for c in _cells]
            while len(cells) < len(cols):
                cells.append("")
            rows.append({"label": _txt(r.get("label"), 30), "cells": cells})
        if len(cols) < 2 or len(rows) < 2:
            return None
        return {"kind": "grid", "cols": cols, "rows": rows}

    if kind == "chart":
        def _num(x):
            try:
                f = float(x)
            except (TypeError, ValueError):
                return None
            # NaN and infinity both survive float() and both poison the axis
            # maths downstream into an SVG full of "nan" coordinates, which a
            # browser renders as nothing at all.
            return f if -1e12 < f < 1e12 and f == f else None

        def _seq(x, limit):
            # NOT _listy: that keeps only dicts, and a chart's points arrive as
            # [x, y] pairs and its ticks as bare strings. Running them through
            # _listy silently emptied every chart.
            if isinstance(x, dict):
                x = list(x.values())
            if not isinstance(x, (list, tuple)):
                return []
            return list(x)[:limit]

        series = []
        for s in _listy(v.get("series"), 4):
            if not isinstance(s, dict):
                continue
            # The model writes whichever of these it feels like, and the
            # difference is not meaningful. Demanding exactly one of them threw
            # the ENTIRE chart away — the step then fell back to a bullet line,
            # which is how "a number over time" ended up as prose. Accept the
            # obvious synonyms and the obvious shapes instead.
            raw = None
            for key in ("points", "data", "values", "y"):
                got = s.get(key)
                if isinstance(got, (list, tuple, dict)) and len(got):
                    raw = got
                    break
            pts, bare = [], []
            for p in _seq(raw, 60):
                if isinstance(p, dict):
                    x, y = _num(p.get("x")), _num(p.get("y"))
                elif isinstance(p, (list, tuple)) and len(p) >= 2:
                    x, y = _num(p[0]), _num(p[1])
                else:
                    # A FLAT list of y-values — [0.2, 0.6, 2.2, ...] — is the
                    # most natural way to write a series and it used to parse
                    # to nothing at all. The x it implies is its position, and
                    # that is exactly what the tick labels are indexed by.
                    n = _num(p)
                    if n is not None:
                        bare.append(n)
                    continue
                if x is not None and y is not None:
                    pts.append((x, y))
            if bare and not pts:
                pts = [(float(i), yv) for i, yv in enumerate(bare)]
            if len(pts) >= 2 or (pts and (v.get("chart") == "bar")):
                series.append({"name": _txt(s.get("name") or s.get("label")
                                            or s.get("title"), 28),
                               "tone": _tone(s.get("tone")),
                               "points": pts})
        if not series:
            return None

        def _axis(raw, limit=12):
            raw = raw if isinstance(raw, dict) else {}
            return {"label": _txt(raw.get("label"), 34),
                    "min": _num(raw.get("min")), "max": _num(raw.get("max")),
                    "ticks": [_txt(t, 14) for t in _seq(raw.get("ticks"), limit)
                              if not isinstance(t, (dict, list, tuple))]}

        notes = []
        for nt in _listy(v.get("notes"), 5):
            if not isinstance(nt, dict) or not _txt(nt.get("text")):
                continue
            x, y = _num(nt.get("x")), _num(nt.get("y"))
            if x is None or y is None:
                continue
            notes.append({"x": x, "y": y, "text": _txt(nt.get("text"), 40),
                          "say": _txt(nt.get("say"), 120)})

        style = v.get("chart") if v.get("chart") in ("line", "bar", "area",
                                                     "scatter") else "line"
        return {"kind": "chart", "chart": style,
                "caption": _txt(v.get("caption"), 90),
                "x": _axis(v.get("x")), "y": _axis(v.get("y")),
                "series": series, "notes": notes}

    if kind == "number":
        value = _txt(v.get("value"), 16)
        return ({"kind": "number", "value": value, "label": _txt(v.get("label"), 46),
                 "sub": _txt(v.get("sub"), 70)} if value else None)

    return None


def safe_visual(v, log=None):
    """_clean_visual, but a crash costs ONE step instead of a whole batch.

    Measured: one malformed field at position 1 of a 3-slide batch raised out
    of merge_visuals and slides 1 AND 2 lost their artwork, while dress()
    logged "wouldn't parse" — which was a lie, the JSON parsed fine.
    """
    try:
        return _clean_visual(v)
    except Exception as e:
        if log:
            log(f"[deck] a visual was malformed ({type(e).__name__}: "
                f"{str(e)[:90]}) — that step falls back")
        return None


def merge_visuals(plan, text):
    """Fold the art call's answer into the plan. A step the model got wrong
    keeps its fallback — a partial walkthrough is fine, a crashed one isn't."""
    visuals = loads_visuals(text) or []
    for i, step in enumerate(plan.get("slides") or ()):
        cleaned = safe_visual(visuals[i] if i < len(visuals) else None)
        if cleaned:
            step["visual"] = cleaned
    return plan


# The most words a fallback may put on screen. Deliberately tiny.
KEYWORD_LIMIT = 4


def _key_line(narration):
    """The few WORDS a step is about — never a sentence.

    This used to be the longest clause of the narration, printed large. That is
    the thing the user actually saw for a whole presentation: a paragraph of prose
    on screen while the same paragraph was being read to them. You can read or
    you can listen; two different renderings of the same sentence, a second out
    of sync, is worse than either alone.

    So the fallback is now a LABEL, not a transcript. The subject of the step,
    in a few words, the way a title card works — something the eye takes in at
    once and then ignores while it listens.
    """
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9'\-]*", narration or "")]
    if not words:
        return ""
    # Proper nouns and technical terms carry the step; filler does not.
    keep = [w for w in words
            if w.lower() not in _STOP and len(w) > 2][:KEYWORD_LIMIT]
    if not keep:
        keep = words[:KEYWORD_LIMIT]
    out = " ".join(keep)
    return out[:1].upper() + out[1:]


def fallback_visuals(plan):
    """No art for this step. Say what it is ABOUT, in words that are words.

    The old fallback ran _key_line over the narration: take the first four
    non-filler tokens and print them large. What that put on their screen,
    verbatim, was

        BEFORE MODEL CAN ANYTHING
        ONCE TRAINED MODEL MOVES
        DURING INFERENCE PROVIDE TRAINED
        EFFICIENCY INFERENCE CRUCIAL BECAUSE

    — four slides of it, in 60pt type. Those are not phrases, they are the
    wreckage of a sentence with its grammar removed, and they read as broken
    software rather than as a design choice.

    Every step now has a real TITLE from the plan: a written, grammatical,
    2-to-5 word name for what is on screen. That is exactly what this needs and
    it costs nothing, so use it. _key_line stays only for a plan old enough to
    have no titles, and even then it is the worse answer.
    """
    for step in plan.get("slides") or ():
        if step.get("visual"):
            continue
        title = (step.get("title") or "").strip()
        step["visual"] = {"kind": "line",
                          "text": title or _key_line(step.get("narration", ""))}
    return plan


def narration_script(plan):
    """What Neo is told to say. Handed back through the tool result, so the
    model speaks the script the slides were built around."""
    lines = []
    for s in plan.get("slides") or ():
        lines.append(s["narration"].strip())
    return "\n\n".join(lines)


# --------------------------------------------------------------------------- #
# Rendering.
#
# A walkthrough, not a deck. What that means concretely, because "make it less
# like PowerPoint" is a look and a look is a set of decisions:
#
#   - No chrome. No slide number, no heading bar across the top, no progress
#     dots, no "Neo is speaking" badge. Every one of those is a thing that says
#     "you are watching a presentation" rather than "look at this".
#   - The figure IS the screen. It gets the whole frame, not a content well
#     under a title.
#   - Text on screen is a LABEL, attached by a leader line to the thing it
#     names. Prose belongs to the voice. A screen that paraphrases the narration
#     a beat out of sync is worse than a screen with nothing on it.
#   - Steps cross-fade and hold their centre, so it reads as one continuous
#     figure being developed rather than a stack of cards being flipped.
# --------------------------------------------------------------------------- #
TONES = {"blue": "#8fb6ff", "amber": "#f4c66a", "green": "#5fd0aa",
         "rose": "#f2a0b3", "slate": "#a3b0c6"}

# Simple organ silhouettes, drawn once, for when there's no photograph worth
# using. Deliberately schematic — a recognisable shape to hang labels on, not an
# attempt at anatomical accuracy that would be wrong in a more embarrassing way.
SHAPES = {
    "none": "",
    "circle": "M500,120 a260,260 0 1,0 1,0 z",
    "heart": ("M500,760 C300,640 250,470 262,360 C272,266 356,208 432,232 "
              "C470,244 492,276 500,306 C508,276 530,244 568,232 "
              "C644,208 728,266 738,360 C750,470 700,640 500,760 Z"),
    "brain": ("M340,300 C300,250 340,180 420,180 C450,130 570,130 600,180 "
              "C690,180 730,260 690,320 C730,380 700,470 620,480 "
              "C600,540 420,545 395,485 C310,470 285,380 340,300 Z"),
    "lung": ("M470,180 h60 v170 c60,-60 150,-40 165,60 c15,95 -10,240 -70,300 "
             "c-45,45 -100,20 -100,-45 v-260 h-50 v260 c0,65 -55,90 -100,45 "
             "c-60,-60 -85,-205 -70,-300 c15,-100 105,-120 165,-60 z"),
    "kidney": ("M620,200 C740,240 790,400 760,530 C730,660 620,760 500,740 "
               "C400,724 340,640 355,560 C372,470 470,470 500,420 "
               "C534,364 500,270 560,215 Z"),
    "cell": ("M500,140 C660,140 850,270 850,450 C850,650 680,780 500,780 "
             "C320,780 150,650 150,450 C150,270 340,140 500,140 Z"),
}

VISUAL_KINDS = ("figure", "flow", "crosssection", "process", "versus",
                "grid", "molecule", "atom", "chart", "scene", "svg",
                "number", "line")

VB_W, VB_H = 1000.0, 620.0
# A flow node's box, and how many characters fit across it at the label and
# sub font sizes. Measured against the rendered width rather than guessed: at
# 20px a label averages ~10.5 units per character and at 14px a sub ~7.2, so a
# 250-unit box holds about 22 and 32 respectively.
BOX_W = 250.0
LABEL_WRAP = 22
SUB_WRAP = 32

# The bounding box of each silhouette path, measured with getBBox in a browser
# rather than eyeballed from the path data — several of these use relative
# commands that no amount of reading gets right.
#
# This exists because a label's (x, y) is a percentage OF THE DRAWING, and the
# old code resolved it against the whole frame while the drawing itself was
# translated and scaled to somewhere else entirely. The dot therefore never
# landed on the organ; it landed in open space near it, with a leader line
# confidently joining a name to nothing. Mapping through the real box is what
# makes "the left atrium is up and to the right" point at the left atrium.
SHAPE_BOX = {
    "circle": (240.0, 120.0, 520.0, 520.0),
    "heart":  (260.0, 227.0, 479.0, 533.0),
    "brain":  (311.0, 143.0, 397.0, 385.0),
    "lung":   (301.0, 180.0, 399.0, 553.0),
    "kidney": (353.0, 200.0, 416.0, 543.0),
    "cell":   (150.0, 140.0, 700.0, 640.0),
}


def place_shape(shape, tx, ty, tw, th):
    """Fit `shape` inside the target rect and return (transform, box) where box
    is the rect the drawing actually occupies. Pure, so the anchor maths and
    the artwork can never disagree — they are the same numbers.
    """
    bx, by, bw, bh = SHAPE_BOX.get(shape, (0.0, 0.0, 1000.0, 900.0))
    if bw <= 0 or bh <= 0:
        return "", (tx, ty, tw, th)
    k = min(tw / bw, th / bh)
    dw, dh = bw * k, bh * k
    ox, oy = tx + (tw - dw) / 2, ty + (th - dh) / 2
    return (f"translate({ox - bx * k:.1f},{oy - by * k:.1f}) scale({k:.4f})",
            (ox, oy, dw, dh))


def _e(text):
    return html.escape(str(text or ""), quote=True)


def label_rows(labels, top=16.0, bottom=88.0, min_gap=11.0):
    """Stack the labels down the right-hand gutter, evenly and without overlap.

    Pure, and it replaced the thing that actually looked broken: labels placed
    at the model's own coordinates collided constantly, because two parts of an
    organ that are 6% apart on the picture need more than 6% of vertical room
    for their text.

    They are laid out in the order given, spread across the gutter — so the
    spacing is a property of the layout, not of the model's guesses.
    """
    n = len(labels or ())
    if not n:
        return []
    if n == 1:
        return [(bottom + top) / 2]
    span = bottom - top
    gap = max(min_gap, span / (n - 1)) if n > 1 else 0
    gap = min(gap, span / (n - 1))
    start = (top + bottom) / 2 - gap * (n - 1) / 2
    return [start + i * gap for i in range(n)]


def _figure(v, index):
    """A picture in the middle, its parts named down whichever side they are on.

    One composition for both cases, but the leader lines are conditional, and
    the condition is about honesty rather than taste.

    For a DRAWN silhouette the geometry is mine, so the model's "the left
    atrium is up and to the right" lands where it should, and a line is drawn
    to the spot.

    For a FETCHED PHOTOGRAPH there is no line, because the model has never seen
    that image — it wrote coordinates for a picture chosen after the fact, by a
    search, from six candidates. An arrow pointing confidently at the wrong part
    of a real heart is worse than no arrow: it teaches something false. So the
    photo carries the frame and the labels stay a named list beside it.

    LABELS GO ON THE SIDE THEY POINT AT. Every label used to stack in one
    right-hand gutter, so a part on the left of the drawing got a leader line
    dragged straight across the whole figure to reach its name — several of
    them at once, crossing each other and the artwork. That is what "the arrows
    never line up" was. A part on the left is named on the left.
    """
    labels = list(v.get("labels") or ())
    has_image = bool(v.get("image"))

    # A VERIFIED picture gets real markers ON the artwork, because we now know
    # where things are: every coordinate survived a second, blind look. The old
    # rule — never draw a leader line on a photograph — was right when the
    # coordinates were a guess by a model that had never seen the image. It is
    # wrong now, and keeping it would throw away the whole point.
    if has_image and v.get("verified") and labels:
        # WHERE THE PICTURE ACTUALLY IS. preserveAspectRatio="meet" letterboxes
        # the image inside the viewBox, so it does NOT occupy 0..1000 x 0..620
        # unless it happens to share that aspect. A marker placed against the
        # viewBox therefore drifts by however much letterboxing there is — on
        # the 857x1125 nephron plate that is most of the width. The parts are
        # fractions of the IMAGE, so they map into the rect it really lands in.
        iw, ih = float(v.get("iw") or 0), float(v.get("ih") or 0)
        if iw > 0 and ih > 0:
            k = min(VB_W / iw, VB_H / ih)
            dw, dh = iw * k, ih * k
            ox, oy = (VB_W - dw) / 2, (VB_H - dh) / 2
        else:
            dw, dh, ox, oy = VB_W, VB_H, 0.0, 0.0

        inner = [f'<image class="photo" href="/img/{index}" x="0" y="0" '
                 f'width="{VB_W:.0f}" height="{VB_H:.0f}" '
                 f'preserveAspectRatio="xMidYMid meet"/>',
                 f'<rect class="dimmer" x="0" y="0" width="{VB_W:.0f}" '
                 f'height="{VB_H:.0f}" fill="#04070b" opacity="0"/>']

        # THE DOT GOES ON THE PART, THE WORD GOES IN THE MARGIN. Printing the
        # name beside the dot put "Afferent arteriole" straight through
        # "Efferent arteriole" — two structures a few percent apart, which is
        # normal in anatomy and fatal for text. A portrait plate also leaves
        # both side gutters empty, so this uses the space that was already
        # being wasted. It is how a real textbook plate is laid out.
        marked = [(j, l) for j, l in enumerate(labels)]
        left = sorted([p for p in marked if p[1]["x"] < 50],
                      key=lambda p: p[1]["y"])
        right = sorted([p for p in marked if p[1]["x"] >= 50],
                       key=lambda p: p[1]["y"])
        rank = 0
        for side, group in (("l", left), ("r", right)):
            rows = label_rows(group, top=14.0, bottom=90.0, min_gap=9.0)
            gx = max(18.0, ox - 26) if side == "l" else min(VB_W - 18, ox + dw + 26)
            anchor = "end" if side == "l" else "start"
            for (j, l), ty in zip(group, rows):
                ax = ox + l["x"] / 100.0 * dw
                ay = oy + l["y"] / 100.0 * dh
                ly = ty / 100.0 * VB_H
                elbow = gx + (14 if side == "l" else -14)
                inner.append(
                    f'<g class="lead pin" data-c="{j}" '
                    f'style="--d:{0.25 + rank * 0.08:.2f}s" '
                    f'data-vx="{ax:.0f}" data-vy="{ay:.0f}">'
                    f'<circle cx="{ax:.0f}" cy="{ay:.0f}" r="6"/>'
                    f'<circle class="halo" cx="{ax:.0f}" cy="{ay:.0f}" r="6"/>'
                    f'<polyline points="{ax:.0f},{ay:.0f} '
                    f'{(ax + elbow) / 2:.0f},{ly:.0f} {elbow:.0f},{ly:.0f}"/>'
                    f'<text x="{gx:.0f}" y="{ly:.0f}" dy="6" '
                    f'text-anchor="{anchor}">{_e(l["text"])}</text></g>')
                rank += 1
        return (f'<svg class="fig verified" viewBox="0 0 {VB_W:.0f} {VB_H:.0f}" '
                f'preserveAspectRatio="xMidYMid meet">{"".join(inner)}</svg>')

    if has_image or not labels:
        # Unchanged: a picture we did not draw gets one honest column of names
        # and no arrows, and a figure with no labels gets the whole frame.
        art_w = 0.60 if labels else 1.0
        gx = VB_W * art_w + 34
        inner = []
        if has_image:
            inner.append(
                f'<image class="photo" href="/img/{index}" x="0" y="14" '
                f'width="{VB_W * art_w:.0f}" height="{VB_H - 28:.0f}" '
                f'preserveAspectRatio="xMidYMid meet"/>')
        else:
            shape = SHAPES.get(v.get("shape") or "none", "")
            if shape:
                scale = 0.78
                ox = (VB_W - 1000 * scale) / 2
                inner.append(
                    f'<g class="silhouette" transform="translate({ox:.0f},20) '
                    f'scale({scale})"><path d="{shape}"/></g>')
        rows = label_rows(labels)
        for rank, (l, ty) in enumerate(zip(labels, rows)):
            y = ty / 100.0 * VB_H
            j = labels.index(l)
            inner.append(
                f'<g class="lead" data-c="{j}" style="--d:{0.2 + rank * 0.07:.2f}s">'
                f'<line x1="{gx - 22:.0f}" y1="{y:.0f}" '
                f'x2="{gx - 8:.0f}" y2="{y:.0f}"/>'
                f'<text x="{gx:.0f}" y="{y:.0f}" dy="7" text-anchor="start">'
                f'{_e(l["text"])}</text></g>')
        return (f'<svg class="fig" viewBox="0 0 {VB_W:.0f} {VB_H:.0f}" '
                f'preserveAspectRatio="xMidYMid meet">{"".join(inner)}</svg>')

    # --- drawn silhouette with labels: gutters on BOTH sides ----------------
    ART_L, ART_R = 0.30, 0.70            # the band the drawing lives in
    LGX = VB_W * ART_L - 34              # left names end here (right-aligned)
    RGX = VB_W * ART_R + 34              # right names start here
    inner = []
    name = (v.get("shape") or "none")
    shape = SHAPES.get(name, "")
    art = (VB_W * ART_L, VB_H * 0.06,
           VB_W * (ART_R - ART_L), VB_H * 0.88)
    if shape:
        transform, art = place_shape(name, *art)
        inner.append(f'<g class="silhouette" transform="{transform}">'
                     f'<path d="{shape}"/></g>')
    bx, by, bw, bh = art

    # Split by which half of the drawing the anchor sits in, then stack each
    # side in its own gutter, sorted by height so leaders inside one side can
    # never cross each other either.
    left = [(j, l) for j, l in enumerate(labels) if l["x"] < 50]
    right = [(j, l) for j, l in enumerate(labels) if l["x"] >= 50]
    rank = 0
    for side, items in (("l", left), ("r", right)):
        items = sorted(items, key=lambda p: p[1]["y"])
        for (j, l), ty in zip(items, label_rows(items)):
            y = ty / 100.0 * VB_H
            ax = bx + l["x"] / 100.0 * bw
            ay = by + l["y"] / 100.0 * bh
            gx = LGX if side == "l" else RGX
            elbow = gx + (16 if side == "l" else -16)
            anchor = "end" if side == "l" else "start"
            inner.append(
                f'<g class="lead" data-c="{j}" style="--d:{0.2 + rank * 0.07:.2f}s">'
                f'<circle cx="{ax:.0f}" cy="{ay:.0f}" r="5"/>'
                f'<circle class="halo" cx="{ax:.0f}" cy="{ay:.0f}" r="5"/>'
                f'<polyline points="{ax:.0f},{ay:.0f} '
                f'{(ax + elbow) / 2:.0f},{ay:.0f} {elbow:.0f},{y:.0f}"/>'
                f'<text x="{gx:.0f}" y="{y:.0f}" dy="7" text-anchor="{anchor}">'
                f'{_e(l["text"])}</text></g>')
            rank += 1

    return (f'<svg class="fig" viewBox="0 0 {VB_W:.0f} {VB_H:.0f}" '
            f'preserveAspectRatio="xMidYMid meet">{"".join(inner)}</svg>')


def fit_nodes(nodes, lo=13.0, hi=87.0):
    """Rescale node positions so the diagram fills its frame. Pure.

    The model was told to use 10-90 and produced 26-66, which drew a perfectly
    correct diagram in the top two thirds of the screen with a band of nothing
    underneath. Rather than nag the prompt about it, normalise: the layout the
    model chose is preserved exactly, it just gets stretched to fit.
    """
    if len(nodes or ()) < 2:
        return nodes
    out = []
    for axis in ("x", "y"):
        vals = [n[axis] for n in nodes]
        span = max(vals) - min(vals)
        out.append((min(vals), span))
    scaled = []
    for n in nodes:
        m = dict(n)
        for (start, span), axis in zip(out, ("x", "y")):
            if span < 1e-6:                  # a single row or column: centre it
                m[axis] = (lo + hi) / 2
            else:
                m[axis] = lo + (n[axis] - start) / span * (hi - lo)
        scaled.append(m)
    return scaled


def _edge_of_box(cx, cy, tx, ty, w, h, pad=0.0):
    """Where the line from (cx,cy) toward (tx,ty) leaves the box centred on
    (cx,cy). Pure — the standard rectangle/ray clip, plus a little padding so
    an arrowhead does not touch the stroke.
    """
    dx, dy = tx - cx, ty - cy
    if dx == 0 and dy == 0:
        return cx, cy
    hw, hh = w / 2.0 + pad, h / 2.0 + pad
    # Scale the direction until it hits whichever side it reaches first.
    sx = hw / abs(dx) if dx else float("inf")
    sy = hh / abs(dy) if dy else float("inf")
    k = min(sx, sy)
    return cx + dx * k, cy + dy * k


def _flow(v):
    """Boxes and arrows, with pulses that keep travelling.

    The continuous motion is the point. A static box-and-arrow picture reads as
    a diagram of a thing; the same picture with something moving along the
    arrows reads as the thing itself working.
    """
    nodes = fit_nodes(v["nodes"])
    pos = {n["id"]: (n["x"] / 100.0 * VB_W, n["y"] / 100.0 * VB_H)
           for n in nodes}
    # Wrap first, because the wrapped line count is what decides a box's
    # height, and an arrow cannot know where a box ENDS until it has one.
    laid = {}
    for n in nodes:
        label = wrap_text(n["label"], LABEL_WRAP, 2)
        sub = wrap_text(n.get("sub", ""), SUB_WRAP, 2)
        laid[n["id"]] = (label, sub, BOX_W,
                         34 + len(label) * 25 + (len(sub) * 19 + 10 if sub else 0))
    parts = ['<defs><marker id="ah" viewBox="0 0 10 10" refX="8" refY="5" '
             'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
             '<path d="M0,0 L10,5 L0,10 z" fill="#6d7b93"/></marker></defs>']

    for i, e in enumerate(v.get("edges") or ()):
        x1, y1 = pos[e["from"]]
        x2, y2 = pos[e["to"]]
        # LAND THE ARROW ON THE BOX, not near it. This used to back off a flat
        # 40% of the centre-to-centre distance, which has nothing to do with
        # where the box actually ends — so arrows stopped in mid-air short of
        # small boxes and started inside large ones. Now each end is the point
        # where the centre line crosses that node's own rectangle.
        ax, ay = _edge_of_box(x1, y1, x2, y2, *laid[e["from"]][2:], pad=10)
        bx, by = _edge_of_box(x2, y2, x1, y1, *laid[e["to"]][2:], pad=14)
        dx, dy = bx - ax, by - ay
        cx, cy = (ax + bx) / 2 - dy * 0.09, (ay + by) / 2 + dx * 0.09
        d = f"M{ax:.0f},{ay:.0f} Q{cx:.0f},{cy:.0f} {bx:.0f},{by:.0f}"
        parts.append(f'<path class="wire" style="--d:{0.3 + i * 0.1:.2f}s" '
                     f'd="{d}" marker-end="url(#ah)"/>')
        if e.get("flow"):
            parts.append(f'<path class="pulse" style="--d:{i * 0.5:.2f}s" '
                         f'd="{d}"/>')
        if e.get("label"):
            parts.append(f'<text class="wire-label" x="{cx:.0f}" '
                         f'y="{cy - 10:.0f}" text-anchor="middle">'
                         f'{_e(_txt(e["label"], 22))}</text>')

    for i, n in enumerate(nodes):
        x, y = pos[n["id"]]
        c = TONES[n["tone"]]
        # THE BOX IS SIZED TO ITS TEXT, not the other way round. It used to be
        # a fixed 208 units wide with the `sub` drawn as one unwrapped <text>,
        # and SVG does not wrap: a 60-character sub ran roughly 420 units, so
        # it left its own box and crossed the one next to it. Two of their
        # three screenshots are that, and it makes a correct diagram look like
        # a rendering failure.
        label = wrap_text(n["label"], LABEL_WRAP, 2)
        sub = wrap_text(n.get("sub", ""), SUB_WRAP, 2)
        w = BOX_W
        h = 34 + len(label) * 25 + (len(sub) * 19 + 10 if sub else 0)
        ty = 34
        rows = []
        for line in label:
            rows.append(f'<text class="box-label" x="{w / 2:.0f}" y="{ty:.0f}" '
                        f'text-anchor="middle" fill="{c}">{_e(line)}</text>')
            ty += 25
        if sub:
            ty += 6
            for line in sub:
                rows.append(f'<text class="box-sub" x="{w / 2:.0f}" '
                            f'y="{ty:.0f}" text-anchor="middle">{_e(line)}'
                            f'</text>')
                ty += 19
        parts.append(
            f'<g class="node" data-node="{_e(n["id"])}" '
            f'data-say="{_e(normalize(n["label"] + " " + n.get("sub", ""))[0] if n["label"] else "")}" '
            f'data-words="{_e(" ".join(normalize(n["label"])))}"'
            f' transform="translate({x - w / 2:.0f},{y - h / 2:.0f})">'
            f'<g class="box" style="--d:{i * 0.09:.2f}s">'
            f'<rect width="{w:.0f}" height="{h:.0f}" rx="16" fill="#10151d" '
            f'stroke="{c}" stroke-opacity=".5" stroke-width="1.5"/>'
            + "".join(rows) + "</g></g>")
    return (f'<svg class="fig" viewBox="0 0 {VB_W:.0f} {VB_H:.0f}" '
            f'preserveAspectRatio="xMidYMid meet">{"".join(parts)}</svg>')


def _crosssection(v):
    """What's inside what. Rings for a thing with a core, bands for a thing with
    a top — both are the same idea drawn the way the subject actually is."""
    layers = v["layers"]
    if v.get("style") == "rings":
        parts, n = [], len(layers)
        # Left of centre on purpose: the labels live in a column to the right,
        # and a centred circle pushes that column off the canvas.
        cx, cy, outer = VB_W * 0.36, VB_H / 2, 262.0
        for i, l in enumerate(layers):
            r = outer * (1 - i / (n + 0.3))
            c = TONES[l["tone"]]
            parts.append(
                f'<g class="ring" style="--d:{i * 0.13:.2f}s">'
                f'<circle cx="{cx:.0f}" cy="{cy:.0f}" r="{r:.0f}" '
                f'fill="{c}" fill-opacity="{0.09 + i * 0.05:.2f}" '
                f'stroke="{c}" stroke-opacity=".55" stroke-width="1.5"/>'
                f'<line x1="{cx:.0f}" y1="{cy - r:.0f}" '
                f'x2="{VB_W * 0.70:.0f}" y2="{cy - r:.0f}" stroke="{c}" '
                f'stroke-opacity=".35" stroke-width="1"/>'
                f'<text class="ring-label" x="{VB_W * 0.715:.0f}" '
                f'y="{cy - r + 5:.0f}" fill="{c}">{_e(l["label"])}</text>'
                + (f'<text class="ring-sub" x="{VB_W * 0.715:.0f}" '
                   f'y="{cy - r + 26:.0f}">{_e(l["sub"])}</text>'
                   if l.get("sub") else "")
                + "</g>")
        return (f'<svg class="fig" viewBox="0 0 {VB_W:.0f} {VB_H:.0f}" '
                f'preserveAspectRatio="xMidYMid meet">{"".join(parts)}</svg>')

    rows = []
    for i, l in enumerate(layers):
        rows.append(
            f'<div class="band" style="--d:{i * 0.12:.2f}s;--c:{TONES[l["tone"]]}">'
            f'<b>{_e(l["label"])}</b>'
            + (f'<span>{_e(l["sub"])}</span>' if l.get("sub") else "")
            + "</div>")
    return f'<div class="bands">{"".join(rows)}</div>'


def _process(v):
    cells = []
    for i, s in enumerate(v["steps"]):
        cells.append(
            f'<div class="stage" style="--d:{i * 0.15:.2f}s">'
            f'<span class="pip">{i + 1}</span><b>{_e(s["label"])}</b>'
            + (f'<em>{_e(s["sub"])}</em>' if s.get("sub") else "")
            + "</div>")
    return (f'<div class="process" style="--n:{len(v["steps"])}">'
            f'<span class="track"></span>{"".join(cells)}</div>')


def _versus(v):
    def panel(side, cls, base):
        pts = "".join(
            f'<li style="--d:{base + j * 0.09:.2f}s">{_e(p)}</li>'
            for j, p in enumerate(side["points"]))
        return (f'<div class="panel {cls}" style="--d:{base:.2f}s;'
                f'--c:{TONES[side["tone"]]}">'
                f'<h3>{_e(side["title"])}</h3><ul>{pts}</ul></div>')
    return (f'<div class="versus">{panel(v["left"], "l", 0.0)}'
            f'<span class="split"></span>{panel(v["right"], "r", 0.14)}</div>')


def _grid(v):
    """A real comparison table: N things across M properties.

    This is the shape almost every good teaching graphic turns out to be —
    monomer/polymer tables, "four macromolecules and what each one does",
    before/during/after. A blob with three arrows does not teach; four columns
    of real content, read down or across, does. Column tone carries the
    grouping so the eye can follow one subject down the table.
    """
    cols = v["cols"]
    rows = v["rows"]
    head = "".join(
        f'<div class="gh" style="--c:{TONES[c.get("tone","blue")]}">'
        f'{_e(c["title"])}</div>' for c in cols)
    body = []
    for i, r in enumerate(rows):
        body.append(f'<div class="gr" style="--d:{0.18 + i * 0.09:.2f}s">')
        body.append(f'<div class="gl">{_e(r["label"])}</div>')
        for j, cell in enumerate(r["cells"][:len(cols)]):
            tone = TONES[cols[j].get("tone", "blue")]
            body.append(f'<div class="gc" style="--c:{tone}">{_e(cell)}</div>')
        body.append("</div>")
    n = len(cols)
    return (f'<div class="grid" style="--n:{n}">'
            f'<div class="gr gh-row"><div class="gl"></div>{head}</div>'
            + "".join(body) + "</div>")


# Standard element colouring, tuned for a dark screen. Chemists read these
# without thinking, so getting them right is most of what makes a structure
# look real rather than drawn by someone who has not seen one.
ELEMENTS = {
    "H": "#e8edf5", "C": "#8fa0b8", "N": "#7aa2f7", "O": "#f2708a",
    "P": "#f4a15a", "S": "#f4d35e", "F": "#7fd6a2", "Cl": "#6fcf7f",
    "Br": "#c98a5e", "I": "#b07de0", "Na": "#9d8cf0", "K": "#9d8cf0",
    "Mg": "#5fd0aa", "Ca": "#5fd0aa", "Fe": "#e08a5e", "Zn": "#93a3bd",
}


def element_color(sym):
    return ELEMENTS.get((sym or "").strip()[:2].capitalize(),
                        ELEMENTS.get((sym or "").strip()[:1].upper(), "#a3b0c6"))


def _lone_pairs(cx, cy, n, r=27.0):
    """Lone-pair dots around an atom — the thing that makes a Lewis structure a
    Lewis structure. Placed top, right, bottom, left in that order, which is
    the convention and also keeps them clear of most bonds."""
    out, spots = [], [(0, -1), (1, 0), (0, 1), (-1, 0)]
    for i in range(min(int(n or 0), 4)):
        ux, uy = spots[i]
        px, py = -uy, ux                    # perpendicular, to split the pair
        for s in (-1, 1):
            x = cx + ux * r + px * s * 6.5
            y = cy + uy * r + py * s * 6.5
            out.append(f'<circle class="lp" cx="{x:.1f}" cy="{y:.1f}" r="3.1"/>')
    return "".join(out)


def _molecule(v):
    """A real structural formula: atoms placed in 2D, bonds between them.

    This is the shape that was missing. Boxes and tables cannot draw water,
    methane, a glucose ring, an amino acid or a benzene ring — and those ARE
    the explanation in chemistry and biology. Single, double and triple bonds,
    lone-pair dots and formal charges are all here, so the same renderer draws
    a Lewis dot structure and a skeletal formula.
    """
    atoms = v["atoms"]
    pos = [(a["x"] / 100.0 * VB_W, a["y"] / 100.0 * VB_H) for a in atoms]
    parts = []

    for e in v.get("bonds") or ():
        (x1, y1), (x2, y2) = pos[e["from"]], pos[e["to"]]
        dx, dy = x2 - x1, y2 - y1
        d = max(1.0, (dx * dx + dy * dy) ** 0.5)
        ux, uy = dx / d, dy / d
        px, py = -uy, ux                     # perpendicular, for parallel lines
        gap = 26.0                           # keep the line off the letters
        ax, ay = x1 + ux * gap, y1 + uy * gap
        bx, by = x2 - ux * gap, y2 - uy * gap
        order = int(e.get("order", 1))
        offs = {1: (0.0,), 2: (-4.6, 4.6), 3: (-8.0, 0.0, 8.0)}.get(order, (0.0,))
        for k, o in enumerate(offs):
            parts.append(
                f'<line class="bond" style="--d:{0.15 + k * 0.05:.2f}s" '
                f'x1="{ax + px * o:.1f}" y1="{ay + py * o:.1f}" '
                f'x2="{bx + px * o:.1f}" y2="{by + py * o:.1f}"/>')
        if e.get("label"):
            parts.append(f'<text class="bond-label" x="{(ax + bx) / 2:.0f}" '
                         f'y="{(ay + by) / 2 - 11:.0f}" text-anchor="middle">'
                         f'{_e(e["label"])}</text>')

    for i, a in enumerate(atoms):
        cx, cy = pos[i]
        col = element_color(a["el"])
        parts.append(f'<g class="atom" style="--d:{0.25 + i * 0.05:.2f}s">')
        parts.append(_lone_pairs(cx, cy, a.get("lone", 0)))
        # a disc of background so bonds never run through the letters
        parts.append(f'<circle cx="{cx:.0f}" cy="{cy:.0f}" r="21" fill="#07090d"/>')
        parts.append(f'<text class="el" x="{cx:.0f}" y="{cy:.0f}" dy="10" '
                     f'text-anchor="middle" fill="{col}">{_e(a["el"])}</text>')
        if a.get("charge"):
            parts.append(f'<text class="chg" x="{cx + 20:.0f}" y="{cy - 16:.0f}">'
                         f'{_e(a["charge"])}</text>')
        if a.get("label"):
            parts.append(f'<text class="el-note" x="{cx:.0f}" y="{cy + 42:.0f}" '
                         f'text-anchor="middle">{_e(a["label"])}</text>')
        parts.append("</g>")

    cap = (f'<text class="fig-cap" x="{VB_W / 2:.0f}" y="{VB_H - 8:.0f}" '
           f'text-anchor="middle">{_e(v["caption"])}</text>') if v.get("caption") else ""
    return (f'<svg class="fig" viewBox="0 0 {VB_W:.0f} {VB_H:.0f}" '
            f'preserveAspectRatio="xMidYMid meet">{"".join(parts)}{cap}</svg>')


def _atom(v):
    """A Bohr atom: nucleus, labelled shells, electrons drawn in their shells.

    Deliberately the textbook picture rather than an orbital cloud — it is what
    a shell diagram in a syllabus looks like, and the electron COUNT per shell
    is the thing being taught.
    """
    import math
    cx, cy = VB_W * 0.38, VB_H / 2
    shells = v["shells"]
    parts = []
    for i, count in enumerate(shells):
        r = 62 + i * 52
        parts.append(f'<circle class="shell" style="--d:{0.2 + i * 0.12:.2f}s" '
                     f'cx="{cx:.0f}" cy="{cy:.0f}" r="{r}" />')
        for k in range(count):
            ang = (k / max(1, count)) * 2 * math.pi - math.pi / 2
            ex, ey = cx + r * math.cos(ang), cy + r * math.sin(ang)
            parts.append(f'<circle class="e" style="--d:{0.3 + i * 0.12 + k * 0.02:.2f}s" '
                         f'cx="{ex:.1f}" cy="{ey:.1f}" r="6.5"/>')
        parts.append(f'<text class="shell-n" x="{cx:.0f}" y="{cy - r - 9:.0f}" '
                     f'text-anchor="middle">{count}e⁻</text>')

    parts.append(f'<circle class="nucleus" cx="{cx:.0f}" cy="{cy:.0f}" r="40"/>')
    parts.append(f'<text class="nuc-sym" x="{cx:.0f}" y="{cy:.0f}" dy="11" '
                 f'text-anchor="middle">{_e(v["symbol"])}</text>')

    gx = VB_W * 0.66
    rows = [("Name", v.get("name", "")),
            ("Protons", str(v.get("protons", "")) if v.get("protons") else ""),
            ("Neutrons", str(v.get("neutrons", "")) if v.get("neutrons") else ""),
            ("Electrons", str(sum(shells))),
            ("Shells", " · ".join(str(s) for s in shells)),
            # Kept short on purpose: anything longer runs off the viewBox and
            # is silently clipped mid-word.
            ("Valence", f"{shells[-1]} outer e⁻" if shells else "")]
    y = VB_H / 2 - 22 * (len([r for r in rows if r[1]]) - 1) / 2 - 30
    for k, (lab, val) in enumerate(rows):
        if not val:
            continue
        parts.append(f'<g class="arow" style="--d:{0.4 + k * 0.07:.2f}s">'
                     f'<text class="ak" x="{gx:.0f}" y="{y:.0f}">{_e(lab)}</text>'
                     f'<text class="av" x="{gx + 148:.0f}" y="{y:.0f}">{_e(val)}</text></g>')
        y += 40
    return (f'<svg class="fig" viewBox="0 0 {VB_W:.0f} {VB_H:.0f}" '
            f'preserveAspectRatio="xMidYMid meet">{"".join(parts)}</svg>')


# A see-through human, for anything anatomical. Deliberately a soft outline —
# it exists to give the viewer a sense of WHERE in the body this is, not to be
# an anatomy plate.
# A see-through human. Simple on purpose: it exists to tell the viewer WHERE in
# the body this is, and a lumpy attempt at realism reads worse than a clean
# schematic. Head, shoulders, torso, legs — drawn from primitives so it is
# symmetrical, which hand-written bezier curves never quite are.
BODY_PATH = (
    "M500,86 a52,52 0 1,0 .1,0 z "                       # head
    "M500,150 c-46,0 -84,16 -104,44 c-16,22 -24,54 -26,96 "
    "c-2,34 -2,66 0,96 l24,2 c2,-40 6,-72 12,-96 "
    "l0,240 c0,26 4,60 8,96 l40,0 c2,-40 4,-76 6,-112 "
    "l40,0 c2,36 4,72 6,112 l40,0 c4,-36 8,-70 8,-96 "
    "l0,-240 c6,24 10,56 12,96 l24,-2 c2,-30 2,-62 0,-96 "
    "c-2,-42 -10,-74 -26,-96 c-20,-28 -58,-44 -104,-44 z")

STAGES = {"body": BODY_PATH, "plain": ""}
SCENE_SHAPES = ("disc", "tube", "blob", "bone", "arrow", "dot", "bar")


def _scene_part(p, i):
    """One movable thing on the stage.

    Every part is wrapped in a <g> whose transform and colour are driven by CSS
    custom properties. A beat writes new values into those properties and the
    browser tweens between them — which is why an animation here is a state
    change rather than a hand-written keyframe. Anything the model can describe
    as "this moves there and squashes" just works.
    """
    # .get, not [], on every field. These normally arrive from _clean_visual
    # with everything filled in, but render_html is a pure function that gets
    # called on raw specs too — and a KeyError here does not cost one part or
    # one step, it raises out of render_html and there is no page at all.
    x, y = _pct(p.get("x")) / 100.0 * VB_W, _pct(p.get("y")) / 100.0 * VB_H
    w, h = _pct(p.get("w"), 10) / 100.0 * VB_W, _pct(p.get("h"), 6) / 100.0 * VB_H
    c = TONES[_tone(p.get("tone"))]
    body = []
    shape = p.get("shape") or "disc"

    # Every primitive is drawn with a gradient and a highlight rather than a
    # flat fill. A flat ellipse at 30% opacity is the single thing that made a
    # `scene` read as clip art no matter what it was labelled.
    gid = f"g{abs(hash(str(p.get('id')))) % 100000}"
    grad = (f'<defs><linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1">'
            f'<stop offset="0" stop-color="{c}" stop-opacity=".46"/>'
            f'<stop offset="1" stop-color="{c}" stop-opacity=".08"/>'
            f'</linearGradient></defs>')
    body.append(grad)

    if shape == "disc":
        # Two arcs and a rim, not a circle: a spinal disc seen from behind is
        # a kidney-ish lens with a nucleus in it, and drawing the nucleus is
        # the difference between "an oval" and "a disc".
        body.append(f'<ellipse rx="{w/2:.0f}" ry="{h/2:.0f}" fill="url(#{gid})" '
                    f'stroke="{c}" stroke-width="2.2"/>')
        body.append(f'<ellipse rx="{w/3.2:.0f}" ry="{h/3.2:.0f}" fill="{c}" '
                    f'fill-opacity=".34" stroke="{c}" stroke-opacity=".55" '
                    f'stroke-width="1.2"/>')
        body.append(f'<path d="M{-w/2.6:.0f},{-h/5:.0f} Q0,{-h/2.2:.0f} '
                    f'{w/2.6:.0f},{-h/5:.0f}" fill="none" stroke="#eef2f8" '
                    f'stroke-opacity=".30" stroke-width="1.4"/>')
    elif shape == "bone":
        body.append(f'<rect x="{-w/2:.0f}" y="{-h/2:.0f}" width="{w:.0f}" '
                    f'height="{h:.0f}" rx="{min(w,h)*0.28:.0f}" '
                    f'fill="url(#{gid})" stroke="{c}" stroke-width="2.2"/>')
        body.append(f'<line x1="{-w/2 + 6:.0f}" y1="{-h/2 + 5:.0f}" '
                    f'x2="{w/2 - 6:.0f}" y2="{-h/2 + 5:.0f}" stroke="#eef2f8" '
                    f'stroke-opacity=".22" stroke-width="1.4"/>')
    elif shape == "tube":
        # A bundle of strands — nerves, vessels, fibres, wires. Squashing this
        # is the single most useful animation in the whole vocabulary.
        n = max(3, min(12, int(p.get("strands", 7))))
        # A faint envelope behind the strands. Without it, squashing the bundle
        # just makes the lines converge into what looks like one line; with it
        # you can see the whole bundle being pinched.
        body.append(f'<rect class="tube-hull" x="{-w/2:.0f}" y="{-h/2:.0f}" '
                    f'width="{w:.0f}" height="{h:.0f}" rx="{h/2:.0f}" '
                    f'fill="{c}" fill-opacity=".10" stroke="{c}" '
                    f'stroke-opacity=".35" stroke-width="1.4"/>')
        # Curved, fanning, varied. Straight parallel lines are the reason the
        # cauda equina came out looking like a barcode: real strands splay,
        # cross and taper, and a bundle only reads as a bundle when they do.
        for k in range(n):
            f = k / max(1, n - 1)
            ty = -h / 2 + h * f
            sag = (0.5 - abs(f - 0.5)) * h * 0.42
            end = ty + (f - 0.5) * h * 0.30
            body.append(
                f'<path class="strand" d="M{-w/2:.0f},{ty:.1f} '
                f'Q{-w*0.05:.1f},{ty + sag:.1f} {w/2:.0f},{end:.1f}" '
                f'fill="none" stroke="{c}" stroke-opacity="{0.55 + 0.45*(1-abs(f-0.5)*2):.2f}" '
                f'stroke-width="{1.7 + 1.1 * (1 - abs(f - 0.5) * 2):.1f}" '
                f'stroke-linecap="round"/>')
    elif shape == "blob":
        # An actual blob: four curves with unequal control points, so it has no
        # axis of symmetry. An ellipse with a dashed stroke is still an ellipse.
        rx, ry = w / 2, h / 2
        body.append(
            f'<path d="M{-rx:.0f},{-ry*0.15:.0f} '
            f'C{-rx*1.02:.0f},{-ry*0.86:.0f} {-rx*0.35:.0f},{-ry:.0f} {rx*0.12:.0f},{-ry*0.92:.0f} '
            f'C{rx*0.78:.0f},{-ry*0.84:.0f} {rx:.0f},{-ry*0.28:.0f} {rx*0.94:.0f},{ry*0.22:.0f} '
            f'C{rx*0.88:.0f},{ry*0.82:.0f} {rx*0.24:.0f},{ry:.0f} {-rx*0.3:.0f},{ry*0.9:.0f} '
            f'C{-rx*0.82:.0f},{ry*0.8:.0f} {-rx*1.02:.0f},{ry*0.4:.0f} {-rx:.0f},{-ry*0.15:.0f} Z" '
            f'fill="url(#{gid})" stroke="{c}" stroke-width="1.9"/>')
    elif shape == "arrow":
        body.append(f'<line x1="{-w/2:.0f}" y1="0" x2="{w/2 - 12:.0f}" y2="0" '
                    f'stroke="{c}" stroke-width="3.2" stroke-linecap="round"/>')
        body.append(f'<path d="M{w/2 - 16:.0f},-9 L{w/2:.0f},0 '
                    f'L{w/2 - 16:.0f},9 z" fill="{c}"/>')
    elif shape == "bar":
        body.append(f'<rect x="{-w/2:.0f}" y="{-h/2:.0f}" width="{w:.0f}" '
                    f'height="{h:.0f}" rx="4" fill="{c}" fill-opacity=".5"/>')
    else:                                     # dot
        body.append(f'<circle r="{max(4, h/2):.0f}" fill="{c}"/>')

    lab = ""
    if p.get("label"):
        lab = (f'<text class="pt-label" x="0" y="{h/2 + 26:.0f}" '
               f'text-anchor="middle" fill="{c}">{_e(p["label"])}</text>')
    return (f'<g class="part" data-part="{_e(p["id"])}" '
            f'style="--px:{x:.0f}px; --py:{y:.0f}px; --d:{0.2 + i*0.08:.2f}s">'
            f'<g class="part-in">{"".join(body)}{lab}</g></g>')


def _scene(v):
    """A stage where things move as Neo talks.

    The point of this shape, and the reason it is worth the complexity: an
    explanation like "the disc bulges backwards and compresses the nerve roots"
    is a CHANGE, and no static picture carries a change. Here the disc actually
    moves and the nerve bundle actually squashes, on the words that describe it.
    """
    stage = STAGES.get(v.get("stage") or "plain", "")
    # The human is a BACKDROP — it says where in the body this is and nothing
    # else. Flat #0e141f at 55% made it the most solid thing on screen, which
    # is why the eye went to the stick figure instead of the anatomy. Now it is
    # a soft vertical wash that fades out at the feet, sitting behind the art.
    parts = ([f'<defs><linearGradient id="stage-fill" x1="0" y1="0" x2="0" y2="1">'
              f'<stop offset="0" stop-color="#8fb6ff" stop-opacity=".085"/>'
              f'<stop offset="1" stop-color="#8fb6ff" stop-opacity=".015"/>'
              f'</linearGradient></defs>'
              f'<path class="stage-body" d="{stage}"/>'] if stage else [])
    for i, p in enumerate(v["parts"]):
        parts.append(_scene_part(p, i))
    beats = json.dumps(v.get("beats") or [])
    # At the TOP. At VB_H-6 it was drawn straight through the figure's legs —
    # the caption and the subject were competing for the same pixels.
    cap = (f'<text class="fig-cap" x="{VB_W/2:.0f}" y="26" '
           f'text-anchor="middle">{_e(v["caption"])}</text>') if v.get("caption") else ""
    return (f'<svg class="fig scene" viewBox="0 0 {VB_W:.0f} {VB_H:.0f}" '
            f'preserveAspectRatio="xMidYMid meet" '
            f'data-beats="{_e(beats)}">{"".join(parts)}{cap}</svg>')


# --------------------------------------------------------------------------- #
# Model-authored SVG.
#
# Every fixed vocabulary hits the same wall: an ellipse beside some parallel
# lines is not a spine, and no amount of prompt tuning makes it one. Accuracy
# and detail need the model to DRAW, not to pick from a box of six shapes.
#
# The first attempt at this failed and the reason is worth writing down: raw
# SVG with no frame produced off-canvas paths, overlapping text and a different
# visual language on every step. The fix is not to take the pen away — it is to
# hand over a strict frame (fixed viewBox, fixed palette, fixed type scale) and
# then sanitise what comes back.
#
# The sanitiser is the load-bearing part. This markup came from a model that
# just read a web page, so it is UNTRUSTED: an allowlist of drawing tags and
# attributes, every event handler dropped, every external reference dropped.
# --------------------------------------------------------------------------- #
SVG_TAGS = {
    "g", "path", "circle", "ellipse", "rect", "line", "polyline", "polygon",
    "text", "tspan", "defs", "lineargradient", "radialgradient", "stop",
    "marker", "clippath", "mask", "pattern", "symbol", "use", "title", "desc",
    "animate", "animatetransform", "animatemotion", "mpath", "set", "textpath",
}
SVG_ATTRS = {
    "d", "cx", "cy", "r", "rx", "ry", "x", "y", "x1", "y1", "x2", "y2",
    "width", "height", "points", "transform", "fill", "stroke", "opacity",
    "fill-opacity", "stroke-opacity", "stroke-width", "stroke-linecap",
    "stroke-linejoin", "stroke-dasharray", "stroke-dashoffset", "stroke-miterlimit",
    "font-size", "font-weight", "font-family", "font-style", "text-anchor",
    "dominant-baseline", "alignment-baseline", "letter-spacing", "dx", "dy",
    "class", "id", "data-part", "offset", "stop-color", "stop-opacity",
    "gradientunits", "gradienttransform", "patternunits", "spreadmethod",
    "marker-end", "marker-start", "marker-mid", "orient", "refx", "refy",
    "markerwidth", "markerheight", "viewbox", "preserveaspectratio",
    "clip-path", "mask", "paint-order", "fr", "fx", "fy", "filter",
    "attributename", "from", "to", "values", "dur", "begin", "repeatcount",
    "keytimes", "calcmode", "type", "additive", "accumulate", "path", "rotate",
    "vector-effect", "shape-rendering", "text-rendering", "visibility",
}
# Attribute values that can reach outside the document, whatever the tag.
_URL_RE = re.compile(r"(?:javascript|data|vbscript)\s*:", re.I)
# No whitespace allowed between "<" and the tag name, and an attribute chunk
# may contain ">" inside quotes.
#
# Both matter. With "<\s*", the text `a < b</text>` was read as a TAG — dropped
# for not being in the allowlist, taking the following </text> with it — so
# every later shape ended up nested inside an unclosed <text> and the figure
# vanished except the word "a". And `[^>]*` stopped at a ">" inside a quoted
# value, losing the self-close and corrupting the id.
# The attribute chunk is bounded ({0,4000}) because the alternation is a
# classic catastrophic-backtracking shape: 5000 copies of `<circle r="` — an
# unbalanced quote — took 18 seconds. A real attribute list is never 4KB.
_TAG_RE = re.compile(
    r"""<(/?)([a-zA-Z][a-zA-Z0-9:-]*)((?:"[^"]*"|'[^']*'|[^>"']){0,800})>""",
    re.S)
_ATTR_RE = re.compile(
    r"""([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""",
    re.S)


def _esc_text(s):
    """Escape for markup WITHOUT mangling entities that are already there.

    html.escape turned `Na&#8594;K &amp; Cl&#8315;` into
    `Na&amp;#8594;K &amp;amp; Cl&amp;#8315;`, so the slide read the raw entity
    source instead of `Na→K & Cl⁻`. Models emit &#8594;, &rarr;, &#176; in SVG
    text constantly. Unescape first, then escape once.
    """
    return html.escape(html.unescape(str(s or "")), quote=False)


def sanitize_svg(markup, limit=90_000):
    """Model-authored SVG -> markup that is safe to inline. Pure and total.

    Allowlist only. Anything not recognised is dropped rather than escaped,
    because a half-understood tag rendering as literal text on the slide is
    worse than it simply not being there.
    """
    # str() first: `"svg": 12345` is an ordinary model slip and used to raise
    # TypeError: 'int' object is not subscriptable.
    raw = str(markup or "")[:limit]
    # Bounded so a pathological input can't spin. Measured: 18k unclosed "<!--"
    # took 10.5s, 11k unclosed "<image " took 17.6s, and CPython won't deliver
    # a signal inside re.sub. Cheap containment: drop every bare "<!--" first
    # so the non-greedy scan always terminates.
    if raw.count("<!--") > 200 or raw.count("<script") > 60 \
            or raw.count("<image") > 200:
        raw = raw.replace("<!--", " ").replace("<script", " ") \
                 .replace("<image", " ").replace("<img", " ")
    # An odd number of quotes means every "..." alternation in _TAG_RE has to
    # backtrack across the rest of the document. Strip them rather than scan.
    # Far more openers than closers means this is not markup, it is noise —
    # and every unterminated "<tag" makes the attribute scan run to its bound
    # from that position. 5000 copies of `<circle r="` took 4.5 seconds even
    # after the quotes were stripped. Refuse it instead of grinding.
    opens, closes = raw.count("<"), raw.count(">")
    if opens > 40 and opens > closes * 3 + 40:
        return ""
    if raw.count('"') % 2 or raw.count('"') > 4000:
        raw = raw.replace('"', " ")
    if raw.count("'") % 2 or raw.count("'") > 4000:
        raw = raw.replace("'", " ")
    raw = re.sub(r"<!--.*?-->", "", raw, flags=re.S)
    # A <script> or <style> body must go WITH its content, not just its tags.
    raw = re.sub(r"<\s*(script|style|foreignObject|iframe|object|embed|image|img)"
                 r"\b.*?(?:</\s*\1\s*>|/>)", "", raw, flags=re.S | re.I)
    raw = re.sub(r"<\s*/?\s*(script|style|foreignObject|iframe|object|embed|"
                 r"image|img)\b[^>]*>", "", raw, flags=re.I)

    out, depth = [], []

    def _attrs(chunk, tag):
        keep = []
        for m in _ATTR_RE.finditer(chunk or ""):
            name = m.group(1).lower()
            val = m.group(2) or m.group(3) or m.group(4) or ""
            if name.startswith("on") or name in ("href", "xlink:href", "src",
                                                 "style", "xmlns:xlink"):
                continue            # events and every route off this document
            if name not in SVG_ATTRS:
                continue
            if _URL_RE.search(val) or "<" in val:
                continue
            keep.append(f'{name}="'
                        f'{html.escape(html.unescape(val), quote=True)}"')
        return (" " + " ".join(keep)) if keep else ""

    pos = 0
    for m in _TAG_RE.finditer(raw):
        text = raw[pos:m.start()]
        if text.strip():
            out.append(_esc_text(text))
        pos = m.end()
        closing, tag, rest = m.group(1) == "/", m.group(2).lower(), m.group(3)
        if tag == "svg":
            continue                # we own the outer element and its viewBox
        if tag not in SVG_TAGS:
            continue
        if closing:
            if tag in depth:
                # Close what is actually open. This used to emit </g> for
                # whatever it unwound, so </text> closed as </g>, a stray
                # unmatched </g> followed, and every later element escaped the
                # group's transform and data-part.
                while depth:
                    open_tag = depth.pop()
                    out.append(f"</{open_tag}>")
                    if open_tag == tag:
                        break
            continue
        selfclose = rest.rstrip().endswith("/")
        out.append(f"<{tag}{_attrs(rest, tag)}{' /' if selfclose else ''}>")
        if not selfclose:
            depth.append(tag)
    tail = raw[pos:]
    if tail.strip():
        out.append(_esc_text(tail))
    while depth:
        out.append(f"</{depth.pop()}>")
    return "".join(out)


def _svg(v):
    """A drawing the model made itself, inside our frame."""
    cap = (f'<text class="fig-cap" x="{VB_W/2:.0f}" y="{VB_H - 6:.0f}" '
           f'text-anchor="middle">{_e(v["caption"])}</text>') if v.get("caption") else ""
    beats = json.dumps(v.get("beats") or [])
    cls = "fig scene" if v.get("beats") else "fig"
    return (f'<svg class="{cls} drawn" viewBox="0 0 {VB_W:.0f} {VB_H:.0f}" '
            f'preserveAspectRatio="xMidYMid meet" '
            f'data-beats="{_e(beats)}">{v["svg"]}{cap}</svg>')


def _chart(v):
    """A real plotted graph — axes, gridlines, ticks, a drawn line.

    There was no chart shape at all, which meant that when a step genuinely was
    a number over time (a share price, a dose response, a population curve) the
    model's only options were to describe it in a `number` tile or to hand-draw
    a chart as raw `svg` — and the raw-SVG attempt is the batch whose JSON came
    back unparseable in the log. Giving it a first-class shape means the axes
    are drawn by code that can count, and the model only supplies the data.
    """
    style = v.get("chart", "line")
    series, notes = v.get("series") or [], v.get("notes") or []
    ax, ay = v.get("x") or {}, v.get("y") or {}
    # The plot box. The top band is reserved for the caption and the two axis
    # names, and the bottom for the tick row — laid out as three separate lines
    # because putting the caption and the x-axis name at the same height wrote
    # one on top of the other.
    L, R = 96.0, VB_W - 54.0          # plot box, leaving room for tick labels
    T, B = 78.0, VB_H - 72.0

    xs = [p[0] for s in series for p in s["points"]]
    ys = [p[1] for s in series for p in s["points"]]

    def _tick_range(axis):
        """The value axis implied by its own tick labels, when they are numbers.

        Ticks are drawn evenly from bottom to top, so "0 / 2k / 4k" is a claim
        about the scale. Ignoring that claim and scaling to the data instead is
        how a 5k bar ended up drawn above a gridline labelled 4k — the chart
        was internally inconsistent, which is worse than an ugly one.
        """
        lo, hi = _tick_value(axis["ticks"][0]), _tick_value(axis["ticks"][-1])
        if lo is None or hi is None or lo == hi:
            return None, None
        return (lo, hi) if hi > lo else (hi, lo)

    ty0, ty1 = _tick_range(ay) if len(ay.get("ticks") or ()) >= 2 else (None, None)
    x0 = ax.get("min") if ax.get("min") is not None else min(xs)
    x1 = ax.get("max") if ax.get("max") is not None else max(xs)
    y0 = ay.get("min") if ay.get("min") is not None else (
        ty0 if ty0 is not None else min(ys))
    y1 = ay.get("max") if ay.get("max") is not None else (
        ty1 if ty1 is not None else max(ys))
    # ...but the data still has to fit inside the frame. A tick row that
    # undersells the numbers gets widened rather than clipping a bar.
    y0, y1 = min(y0, min(ys)), max(y1, max(ys))
    if y1 == y0:                       # a flat series would divide by zero
        y1 = y0 + (abs(y0) or 1.0)
    if x1 == x0:
        x1 = x0 + 1.0
    # A line that touches the top of the frame reads as clipped, so leave the
    # value axis a little headroom — unless the model set the bounds itself, or
    # the axis is about to be snapped to round numbers below, which supplies
    # its own headroom and would otherwise be padding padding.
    if ay.get("max") is None and len(ay.get("ticks") or ()) >= 2:
        y1 += (y1 - y0) * 0.08
    if ay.get("min") is None and y0 > 0 and y0 < (y1 - y0):
        y0 = 0.0                       # a bar chart that doesn't start at zero lies

    def px(x):
        return L + (x - x0) / (x1 - x0) * (R - L)

    def py(y):
        return B - (y - y0) / (y1 - y0) * (B - T)

    parts = []
    yticks = ay.get("ticks") or []
    if len(yticks) < 2:
        # Round numbers, not fifths of whatever the data happened to reach.
        # Slicing the range into five gave gridlines at 837, 1.7k, 2.5k — an
        # axis nobody can read a value off, which is most of what an axis is
        # for. Snap the step to 1, 2, 2.5 or 5 times a power of ten and let the
        # top of the range move up to the next one.
        span = y1 - y0
        mag = 10.0 ** math.floor(math.log10(span / 4.0)) if span > 0 else 1.0
        for mult in (1, 2, 2.5, 5, 10):
            step = mult * mag
            if span / step <= 6:
                break
        y0 = math.floor(y0 / step) * step
        y1 = math.ceil(y1 / step) * step
        n = int(round((y1 - y0) / step))
        yticks = [_fmt_num(y0 + i * step) for i in range(n + 1)]
    rows = len(yticks) if len(yticks) >= 2 else 5
    for k in range(rows):
        frac = k / (rows - 1)
        text = (yticks[k] if k < len(yticks)
                else _fmt_num(y0 + frac * (y1 - y0)))
        # A numeric tick is drawn AT ITS VALUE, not at its position in the
        # list. Spacing them evenly is only correct when the range happens to
        # end on the last tick — and when the data overshoots it (a 5k bar on a
        # 0/2k/4k axis) evenly spaced labels put "4k" on the top gridline with
        # a taller bar beside it. The chart then contradicts itself, which is
        # the one thing a chart must never do.
        val = _tick_value(text)
        gy = py(val) if val is not None and y1 != y0 else B - frac * (B - T)
        if not (T - 2 <= gy <= B + 2):
            continue                       # off the frame: don't draw a lie
        parts.append(f'<line class="grid" x1="{L:.1f}" y1="{gy:.1f}" '
                     f'x2="{R:.1f}" y2="{gy:.1f}"/>')
        parts.append(f'<text class="tick" x="{L - 14:.1f}" y="{gy:.1f}" dy="5" '
                     f'text-anchor="end">{_e(text)}</text>')

    xticks = ax.get("ticks") or []
    for k, t in enumerate(xticks):
        gx = L + (k / max(1, len(xticks) - 1)) * (R - L) if len(xticks) > 1 else (L + R) / 2
        parts.append(f'<text class="tick" x="{gx:.1f}" y="{B + 30:.1f}" '
                     f'text-anchor="middle">{_e(t)}</text>')

    parts.append(f'<line class="axis" x1="{L:.1f}" y1="{T:.1f}" '
                 f'x2="{L:.1f}" y2="{B:.1f}"/>')
    parts.append(f'<line class="axis" x1="{L:.1f}" y1="{B:.1f}" '
                 f'x2="{R:.1f}" y2="{B:.1f}"/>')
    # Anchored to the START of the value axis, not the end of it. Right-aligned
    # at x = L - 14 meant a label of any length ran off the left edge of the
    # viewBox and was clipped — "% recovering" reached the screen as
    # "recovering", with the unit, which is the informative half, cut off.
    if ay.get("label"):
        parts.append(f'<text class="axis-label" x="{L:.1f}" y="{T - 18:.1f}" '
                     f'text-anchor="start">{_e(ay["label"])}</text>')
    if ax.get("label"):
        parts.append(f'<text class="axis-label" x="{R:.1f}" y="{T - 18:.1f}" '
                     f'text-anchor="end">{_e(ax["label"])}</text>')

    # Bars sit on their own x value, exactly like every other mark, so they line
    # up with the tick labels underneath them. They used to be laid out on a
    # separate "slot" scale of their own, which put every bar except the middle
    # one beside the month it was supposed to be over.
    n_slots = max(1, max((len(s["points"]) for s in series), default=1))
    group = (R - L) / max(1, n_slots) if n_slots > 1 else (R - L)
    bar_w = min(64.0, group * 0.55 / max(1, len(series)))
    slot = 0
    for si, s in enumerate(series):
        col = TONES[s["tone"]]
        pts = s["points"]
        if style == "bar":
            for (x, y) in pts:
                # Series side by side within one x position, centred on it.
                cx = px(x) + (si - (len(series) - 1) / 2.0) * (bar_w + 4)
                top, base = py(y), py(max(y0, 0.0))
                parts.append(
                    f'<rect class="bar" style="--d:{0.15 + slot * 0.07:.2f}s" '
                    f'x="{cx - bar_w / 2:.1f}" y="{min(top, base):.1f}" '
                    f'width="{bar_w:.1f}" height="{max(2.0, abs(base - top)):.1f}" '
                    f'rx="3" fill="{col}" fill-opacity=".55" stroke="{col}"/>')
                parts.append(f'<text class="pt-v" x="{cx:.1f}" y="{min(top, base) - 10:.1f}" '
                             f'text-anchor="middle" fill="{col}">{_e(_fmt_num(y))}</text>')
                slot += 1
            continue

        d = " ".join(f'{"M" if i == 0 else "L"}{px(x):.1f},{py(y):.1f}'
                     for i, (x, y) in enumerate(pts))
        if style == "area":
            parts.append(f'<path class="plot-fill" style="--d:{0.2 + si * 0.15:.2f}s" '
                         f'd="{d} L{px(pts[-1][0]):.1f},{B:.1f} '
                         f'L{px(pts[0][0]):.1f},{B:.1f} Z" fill="{col}" '
                         f'fill-opacity=".16"/>')
        if style != "scatter":
            parts.append(f'<path class="plot" style="--d:{0.15 + si * 0.15:.2f}s" '
                         f'd="{d}" fill="none" stroke="{col}" stroke-width="2.6" '
                         f'stroke-linejoin="round" stroke-linecap="round"/>')
        for i, (x, y) in enumerate(pts):
            parts.append(f'<circle class="dot" style="--d:{0.5 + si * 0.15 + i * 0.04:.2f}s" '
                         f'cx="{px(x):.1f}" cy="{py(y):.1f}" r="4.6" fill="{col}"/>')
    # Series names go in a legend under the tick row, not next to the last
    # point. Inline names sit ON the plot, and the last point is exactly where
    # a line ends up — so the label was drawn straight through the end of its
    # own line every time the series ran to the right edge, which is most of
    # them. Below the axis nothing can collide with anything.
    named = [s for s in series if s["name"]]
    if named:
        widths = [len(s["name"]) * 9.0 + 46 for s in named]
        x = VB_W / 2 - sum(widths) / 2
        for s, w in zip(named, widths):
            col = TONES[s["tone"]]
            parts.append(
                f'<g class="legend">'
                f'<rect x="{x:.1f}" y="{VB_H - 26:.1f}" width="16" height="4" '
                f'rx="2" fill="{col}"/>'
                f'<text x="{x + 24:.1f}" y="{VB_H - 18:.1f}" '
                f'text-anchor="start" fill="{col}">{_e(s["name"])}</text></g>')
            x += w

    # Annotations are ordinary callouts: same data-c index, same cue matching,
    # so they arrive on the words that describe them.
    for j, nt in enumerate(notes):
        nx, ny = px(nt["x"]), py(nt["y"])
        up = ny > T + 70
        ty = ny - 34 if up else ny + 44
        parts.append(
            f'<g class="lead" data-c="{j}">'
            f'<line x1="{nx:.1f}" y1="{ny:.1f}" x2="{nx:.1f}" y2="{ty + (10 if up else -12):.1f}"/>'
            f'<circle class="halo" cx="{nx:.1f}" cy="{ny:.1f}" r="9"/>'
            f'<circle cx="{nx:.1f}" cy="{ny:.1f}" r="4"/>'
            f'<text x="{nx:.1f}" y="{ty:.1f}" text-anchor="middle">{_e(nt["text"])}</text>'
            f'</g>')

    # Above the figure, on its own line. It used to sit centred at the very
    # bottom, at the same height as the x-axis name, so a chart with both wrote
    # two labels through each other.
    cap = (f'<text class="cap" x="{VB_W / 2:.0f}" y="30" '
           f'text-anchor="middle">{_e(v["caption"])}</text>') if v.get("caption") else ""
    return (f'<svg class="fig" viewBox="0 0 {VB_W:.0f} {VB_H:.0f}" '
            f'preserveAspectRatio="xMidYMid meet">{"".join(parts)}{cap}</svg>')


def _tick_value(text):
    """A tick label back to a number: "4k" -> 4000, "$35" -> 35, "12%" -> 12.
    None when it isn't a number at all ("Jun"), which is the normal case for a
    category axis."""
    m = re.search(r"-?\d[\d,]*\.?\d*", str(text or ""))
    if not m:
        return None
    try:
        val = float(m.group(0).replace(",", ""))
    except ValueError:
        return None
    suffix = str(text)[m.end():m.end() + 1].lower()
    return val * {"k": 1e3, "m": 1e6, "b": 1e9}.get(suffix, 1.0)


def _fmt_num(x):
    """An axis number a person would write. 1200000 -> 1.2M, 0.5 -> 0.5, 37.0 -> 37."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return ""
    for cut, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(x) >= cut:
            return f"{x / cut:.1f}".rstrip("0").rstrip(".") + suffix
    if abs(x) >= 100 or x == int(x):
        return f"{int(round(x))}"
    return f"{x:.2f}".rstrip("0").rstrip(".")


def _number(v):
    return (f'<div class="number"><div class="value">{_e(v["value"])}</div>'
            f'<div class="value-label">{_e(v["label"])}</div>'
            + (f'<div class="value-sub">{_e(v["sub"])}</div>'
               if v.get("sub") else "") + "</div>")


def _line(v):
    """The no-figure card. A label, framed — never the narration."""
    text = str(v.get("text") or "")
    if not text:
        return ""     # nothing to say is better than an empty frame
    return f'<div class="keyline">{_e(text)}</div>'


def _render_visual(v, index=0):
    """One step's markup. NEVER raises.

    safe_visual already stops a malformed spec reaching here, but render_html
    is a pure function that anything may call with anything, and a renderer
    that raises does not cost one figure — it raises out of render_html and
    there is no page. One step degrading to a line is a worse slide; an
    exception is no walkthrough.
    """
    try:
        return _render_one(v, index)
    except Exception:
        return _line({"text": ""})


def _render_one(v, index=0):
    kind = (v or {}).get("kind")
    if kind == "figure":
        return _figure(v, index)
    if kind == "flow":
        return _flow(v)
    if kind == "crosssection":
        return _crosssection(v)
    if kind == "process":
        return _process(v)
    if kind == "versus":
        return _versus(v)
    if kind == "svg":
        return _svg(v)
    if kind == "scene":
        return _scene(v)
    if kind == "molecule":
        return _molecule(v)
    if kind == "atom":
        return _atom(v)
    if kind == "chart":
        return _chart(v)
    if kind == "grid":
        return _grid(v)
    if kind == "number":
        return _number(v)
    return _line(v or {})


def _credit(text):
    return f'<div class="credit">{_e(text)}</div>' if text else ""


def steps_html(deck):
    """Just the slide sections, so the finished artwork can be swapped into a
    page that is already on screen. Pure, and shared with render_html so the
    live page and the first paint can never render a step differently."""
    steps = deck.get("slides") or []
    body = [f'<section class="step opening" data-i="-1">'
            f'<div class="mark"></div>'
            f'<h1>{_e(deck.get("title"))}</h1></section>']
    for i, s in enumerate(steps):
        v = s.get("visual") or {}
        # The caption. This file used to argue that a walkthrough should carry
        # no heading at all — "the figure IS the screen" — and that is a fine
        # principle for a figure that names its own parts. It is a bad one for
        # everything else: a versus is two lists, a crosssection is four
        # coloured bands, a process is five dots. Without a heading none of
        # them says what it is ABOUT, so the screen is a graphic with no
        # subject and the viewer has only the voice to go on. It is a caption
        # in the corner, not a title bar across the top, so the figure still
        # owns the frame.
        cap = (f'<div class="cap">{_e(s.get("title"))}</div>'
               if s.get("title") else "")
        body.append(
            f'<section class="step" data-i="{i}">'
            f'{cap}'
            f'<div class="frame">{_render_visual(v, i)}</div>'
            f'{_credit(v.get("credit"))}</section>')
    # The closing beat, shown after the last figure while the audio drains.
    # Its index is one past the last slide, so nothing the Tracker emits can
    # reach it by accident — deck.py shows it deliberately, once, at the end.
    if deck.get("takeaway"):
        body.append(
            f'<section class="step closing" data-i="{len(steps)}">'
            f'<div class="mark"></div>'
            f'<h1>{_e(deck["takeaway"])}</h1></section>')
    return "".join(body)


def render_html(deck, stream_path="/events", start=-1):
    """The whole walkthrough as one page. Pure — no GUI, no network — so the
    tests can render one and assert on it."""
    body = [steps_html(deck)]

    return f"""<!doctype html>
<meta charset="utf-8">
<title>{_e(deck.get("title"))}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box }}
:root {{
  /* A projection, not a slide. Cyan-white light on near-black, the way a
     heads-up display reads: the image looks EMITTED rather than printed. */
  --ink:#dff3ff; --dim:#7fa8c4; --faint:#3d5a70;
  --ground:#04070b; --panel:#0a1219; --accent:#5fd8ff; --mark:#f4c66a;
  --glow:#5fd8ff;
  --sans:-apple-system,BlinkMacSystemFont,"SF Pro Text",system-ui,sans-serif;
  --serif:ui-serif,"New York",Iowan Old Style,Georgia,serif;
}}
html,body {{ height:100%; background:var(--ground); color:var(--ink);
  overflow:hidden; font:400 16px/1.5 var(--sans);
  -webkit-font-smoothing:antialiased }}
/* The room the figure sits in. Three layers, because one flat radial reads as
   a gradient rather than as light: a cool key from top centre, a warmer bounce
   from the lower left, and a vignette that pulls the corners down so the eye
   lands in the middle. Flat black behind a drawing is what makes the drawing
   look like clip art. */
body::before {{ content:""; position:fixed; inset:0; pointer-events:none;
  background:
    radial-gradient(120% 78% at 50% -8%, #1b2740 0%, #0d1320 42%, #07090d 74%),
    radial-gradient(58% 44% at 14% 96%, rgba(143,182,255,.10) 0%, transparent 70%),
    radial-gradient(46% 40% at 88% 8%, rgba(244,198,106,.07) 0%, transparent 68%) }}
/* Vignette, over the light and under the figure. */
body::after {{ content:""; position:fixed; inset:0; pointer-events:none; z-index:1;
  background:
    /* projector scanlines — 3px, barely there. This is the single cheapest
       thing that turns a dark web page into a projected image. */
    repeating-linear-gradient(0deg, rgba(95,216,255,.030) 0 1px,
                                    transparent 1px 3px),
    /* a faint measurement grid, like something is being surveyed */
    linear-gradient(rgba(95,216,255,.022) 1px, transparent 1px) 0 0/72px 72px,
    linear-gradient(90deg, rgba(95,216,255,.022) 1px, transparent 1px) 0 0/72px 72px,
    radial-gradient(78% 66% at 50% 46%, transparent 38%, rgba(0,0,0,.62) 100%) }}
.step {{ z-index:2 }}

/* A drawing needs to sit ON something. This is a very soft floor glow under
   the figure — not a visible shape, just enough that the art has weight. */
.frame::before {{ content:""; position:absolute; left:50%; top:50%;
  width:min(92vw,1260px); height:min(80vh,820px); transform:translate(-50%,-50%);
  border-radius:50%; pointer-events:none; z-index:-1;
  background:radial-gradient(closest-side, rgba(95,216,255,.13), transparent 70%);
  animation:emit 6s ease-in-out infinite }}
@keyframes emit {{ 50% {{ opacity:.55; transform:translate(-50%,-50%) scale(1.04) }} }}
.frame {{ position:relative }}
/* BLOOM. Light-emitting line art needs the glow to come off the strokes
   themselves, which is what separates a hologram from a dark-mode diagram.
   Three stacked shadows: a tight core, a wide halo, and a drop for depth. */
.fig {{ filter:drop-shadow(0 0 2px rgba(95,216,255,.55))
               drop-shadow(0 0 14px rgba(95,216,255,.30))
               drop-shadow(0 0 44px rgba(95,216,255,.14))
               drop-shadow(0 10px 30px rgba(0,0,0,.6)) }}
/* The projection settles as it arrives: a hologram resolving, not a slide
   sliding in. */
.step.on .fig {{ animation:resolve .85s cubic-bezier(.2,.8,.3,1) both }}
@keyframes resolve {{
  from {{ opacity:0; transform:scale(.965) translateY(10px); filter:blur(7px) }}
  to   {{ opacity:1; transform:none; filter:blur(0) }} }}

.step {{ position:absolute; inset:0; display:flex; align-items:center;
  justify-content:center; padding:6vh 6vw; opacity:0; visibility:hidden;
  transform:scale(.985); transition:opacity .55s ease, transform .55s ease }}
.cap {{ position:absolute; left:6vw; top:5vh; max-width:60ch;
  font:500 clamp(17px,1.6vw,23px)/1.3 var(--sans); letter-spacing:.01em;
  color:var(--dim); opacity:0 }}
.cap::after {{ content:""; display:block; width:38px; height:1px;
  margin-top:11px; background:var(--accent); opacity:.55 }}
.step.on .cap {{ animation:rise .6s cubic-bezier(.2,.8,.3,1) .1s both }}
.step.on {{ opacity:1; visibility:visible; transform:none }}
.frame {{ width:100%; height:100%; display:flex; align-items:center;
  justify-content:center }}
.fig {{ width:100%; height:100%; max-height:78vh }}

/* opening — a held beat, not a title slide */
.opening, .closing {{ flex-direction:column; gap:26px }}
.mark {{ width:44px; height:44px; border-radius:50%;
  border:1.5px solid var(--accent); position:relative }}
.mark::after {{ content:""; position:absolute; inset:13px; border-radius:50%;
  background:var(--accent); animation:breathe 3.4s ease-in-out infinite }}
@keyframes breathe {{ 50% {{ transform:scale(.55); opacity:.5 }} }}
h1 {{ font:400 clamp(30px,4.4vw,62px)/1.15 var(--serif); letter-spacing:-.01em;
  text-align:center; text-wrap:balance; max-width:18ch; color:var(--ink) }}

/* ---- figure: photograph or silhouette, with leader lines ---- */
.photo {{ opacity:0 }}
.step.on .photo {{ animation:fade .8s ease .05s both }}
.silhouette path {{ fill:#141c28; stroke:var(--accent); stroke-opacity:.45;
  stroke-width:2.5; opacity:0 }}
.step.on .silhouette path {{ animation:trace .9s ease .1s both }}
@keyframes trace {{ from {{ opacity:0; transform:translateY(12px) }}
  to {{ opacity:1; transform:none }} }}
@keyframes fade {{ from {{ opacity:0 }} to {{ opacity:1 }} }}

/* Every label is on screen as soon as its slide is, staggered by --d.
   It used to be opacity:0 until a callout fired, and a callout only fires when
   the cue matcher finds those words in what Neo actually said. Miss the cue —
   which is the common case, because a two-sentence narration does not name
   every part of the figure — and the label never appeared AT ALL. What reached
   the screen was a bare silhouette with nothing named on it. A label is the
   content of a figure, so it may not depend on a fuzzy text match; the callout
   now HIGHLIGHTS the part Neo is naming instead of being the only thing that
   makes it exist. */
/* ---- a verified picture: the marker Neo is naming is spotlit ---- */
.fig.verified .photo {{ transition:filter .5s ease }}
.step.on .fig.verified.focusing .photo {{ filter:brightness(.34) saturate(.7) }}
.fig.verified .pin text {{ opacity:0; transition:opacity .4s ease }}
.step.on .fig.verified .pin text {{ animation:fade .5s ease var(--d) both }}
.fig.verified.focusing .pin {{ opacity:.25 }}
.fig.verified.focusing .pin.live {{ opacity:1 }}
.fig.verified .pin.live circle {{ fill:var(--mark) }}
.fig.verified .pin.live text {{ fill:#fff; font-weight:700 }}
/* the lit part keeps its brightness while everything round it drops */
.spotlight {{ pointer-events:none }}

.lead {{ opacity:0; --d:0s }}
.step.on .lead {{ animation:fade .5s ease var(--d) both }}
.lead.on text {{ fill:var(--mark); stroke-width:6 }}
.lead.on circle {{ r:7 }}
.lead.on line, .lead.on polyline {{ stroke-opacity:1; stroke-width:2.2 }}
.lead text, .lead circle, .lead line, .lead polyline {{
  transition:fill .35s ease, r .35s ease, stroke-opacity .35s ease,
             stroke-width .35s ease }}
.lead circle {{ fill:var(--mark) }}
.lead .halo {{ fill:none; stroke:var(--mark); stroke-width:1.5; opacity:0 }}
.lead.on .halo {{ animation:ping 2.6s ease-out infinite }}
@keyframes ping {{ from {{ r:5; opacity:.9 }} to {{ r:22; opacity:0 }} }}
.lead line, .lead polyline {{ fill:none; stroke:var(--mark); stroke-width:1.5;
  stroke-opacity:.8 }}
/* The dark stroke behind the glyphs is what keeps a label readable when it
   lands on the bright part of a photograph. paint-order puts it behind. */
.lead text {{ font:600 19px/1 var(--sans); fill:#fff; stroke:#04060a;
  stroke-width:5.5; paint-order:stroke; stroke-linejoin:round }}

/* ---- FOCUS. Which part is Neo talking about RIGHT NOW? ----
   the user: "highlight, enlarge or do something to each part while the audio is
   talking about it, so it's easy to know what the main focus is."
   They are right, and it is the difference between a diagram that sits there and
   one that is being explained. The page listens for the words it already has
   on screen and lifts the part that owns them; everything else drops back so
   the lift reads as attention rather than as a colour change. */
.fig .node, .fig .lead, .stage, .band, .panel, .gr {{
  transition:opacity .45s ease, filter .45s ease, transform .45s ease }}
.focusing .node, .focusing .lead, .focusing .stage,
.focusing .band, .focusing .panel, .focusing .gr {{ opacity:.34 }}
.node.live, .lead.live, .stage.live, .band.live, .panel.live, .gr.live {{
  opacity:1 !important }}
.node.live {{ transform:scale(1.06); filter:drop-shadow(0 0 18px var(--glow)) }}
.stage.live, .band.live, .panel.live, .gr.live {{
  transform:translateY(-3px) scale(1.02);
  filter:drop-shadow(0 0 16px rgba(95,216,255,.28)) }}
.node.live rect {{ stroke-opacity:1; stroke-width:2.4 }}

/* ---- flow ---- */
.wire {{ fill:none; stroke:#6d7b93; stroke-width:2; stroke-opacity:.7;
  stroke-dasharray:700; stroke-dashoffset:700 }}
.step.on .wire {{ animation:draw .8s ease var(--d) forwards }}
@keyframes draw {{ to {{ stroke-dashoffset:0 }} }}
.pulse {{ fill:none; stroke:var(--accent); stroke-width:3.5;
  stroke-linecap:round; stroke-dasharray:1 46; opacity:0 }}
.step.on .pulse {{ animation:travel 2.2s linear var(--d) infinite,
  fade .6s ease 1s both }}
@keyframes travel {{ from {{ stroke-dashoffset:47 }} to {{ stroke-dashoffset:0 }} }}
.wire-label {{ font:500 14px/1 var(--sans); fill:#93a0b6; stroke:var(--ground);
  stroke-width:5; paint-order:stroke; stroke-linejoin:round }}
.box {{ opacity:0 }}
.step.on .box {{ animation:rise .5s cubic-bezier(.2,.8,.3,1) var(--d) both }}
.box-label {{ font:600 20px/1 var(--sans) }}
.box-sub {{ font:400 14px/1 var(--sans); fill:#8b98af }}

/* ---- cross-section ---- */
.ring {{ opacity:0 }}
.step.on .ring {{ animation:fade .55s ease var(--d) both }}
.ring-label {{ font:600 19px/1 var(--sans) }}
.ring-sub {{ font:400 15px/1 var(--sans); fill:#8b98af }}
.bands {{ width:min(70vw,860px); display:flex; flex-direction:column; gap:10px }}
.band {{ padding:22px 28px; border-radius:18px; background:var(--panel);
  border:1px solid color-mix(in srgb,var(--c) 40%,transparent); opacity:0;
  box-shadow:inset 0 0 60px color-mix(in srgb,var(--c) 8%,transparent) }}
.step.on .band {{ animation:rise .55s cubic-bezier(.2,.7,.3,1) var(--d) both }}
.band b {{ display:block; font:620 clamp(20px,1.8vw,26px)/1.2 var(--sans);
  color:var(--c) }}
.band span {{ display:block; margin-top:5px; font-size:clamp(15px,1.2vw,18px);
  color:var(--dim) }}

/* ---- process ---- */
/* The rail runs from the FIRST pip to the LAST pip and stops there. It used
   to be left:8px right:8px — the full width of the row — so it shot past the
   final dot and died in empty space on the right, which reads as a diagram
   that was cut off. The last pip sits one cell in from the right edge. */
.process {{ position:relative; width:100%; display:flex; gap:2.5%;
  --cell:calc((100% - (var(--n) - 1) * 2.5%) / var(--n)) }}
.track {{ position:absolute; left:17px; right:calc(var(--cell) - 17px);
  top:17px; height:1.5px; background:#3b4759 }}
.stage {{ flex:1; opacity:0 }}
.step.on .stage {{ animation:rise .5s cubic-bezier(.2,.8,.3,1) var(--d) both }}
.pip {{ display:flex; align-items:center; justify-content:center;
  width:34px; height:34px; border-radius:50%; background:var(--ground);
  border:1.5px solid var(--accent); color:var(--accent);
  font:600 15px/1 var(--sans); box-shadow:0 0 0 6px var(--ground),
  0 0 26px #5fd8ff55; margin-bottom:24px }}
.stage b {{ display:block; font:620 clamp(19px,1.7vw,25px)/1.25 var(--sans);
  text-wrap:balance }}
.stage em {{ display:block; margin-top:9px; font-style:normal;
  font-size:clamp(15px,1.2vw,18px); color:var(--dim); padding-right:9% }}

/* ---- versus ---- */
/* Two columns of bare bullets floating on black is not a comparison, it is
   a text dump — and that is literally what this rendered: no card, no edge,
   nothing to say the two sides are being weighed against each other. Give
   each side a real panel in its own tone. */
.versus {{ display:flex; align-items:stretch; gap:clamp(18px,2.4vw,34px);
  width:min(88vw,1180px) }}
.split {{ display:none }}
.panel {{ flex:1; opacity:0; padding:clamp(22px,2.4vw,34px);
  border-radius:20px; background:var(--panel);
  border:1px solid color-mix(in srgb,var(--c) 34%,transparent);
  box-shadow:inset 0 0 90px color-mix(in srgb,var(--c) 7%,transparent),
             0 12px 40px rgba(0,0,0,.45) }}
.step.on .panel {{ animation:rise .55s cubic-bezier(.2,.7,.3,1) var(--d) both }}
.panel h3 {{ font:620 clamp(21px,2vw,29px)/1.2 var(--sans); color:var(--c);
  margin-bottom:14px; padding-bottom:14px;
  border-bottom:1px solid color-mix(in srgb,var(--c) 22%,transparent) }}
.panel ul {{ list-style:none }}
.panel li {{ position:relative; padding:13px 0 13px 24px;
  font-size:clamp(17px,1.5vw,22px); color:#cdd6e5; opacity:0 }}
.step.on .panel li {{ animation:rise .45s ease var(--d) both }}
.panel li::before {{ content:""; position:absolute; left:0; top:calc(50% - 3px);
  width:8px; height:8px; border-radius:50%; background:var(--c); opacity:.8 }}

/* ---- scene: the stage where things move as Neo talks ---- */
/* Every part is positioned by CSS variables and TRANSITIONS between them, so
   a beat is a state change rather than a hand-written keyframe. That is what
   makes an arbitrary "this moves there and squashes" animatable at all. */
/* `stroke:#2b3purple` was sitting here — not a colour, so the browser dropped
   the declaration; the line below happened to repair it only because it came
   second. Removed rather than left as a trap. */
.stage-body {{ fill:url(#stage-fill); stroke:#2b3a52; stroke-width:1.6;
  opacity:0 }}
.step.on .stage-body {{ animation:fade 1s ease .05s both }}
.drawn [data-part] {{ --dx:0px; --dy:0px; --sc:1; --sq:1; --rot:0deg;
  transform-box:fill-box; transform-origin:center;
  transform:translate(var(--dx), var(--dy)) rotate(var(--rot))
            scale(var(--sc), calc(var(--sc) * var(--sq)));
  transition:transform 1.15s cubic-bezier(.33,.9,.3,1), opacity .6s ease }}
.drawn [data-part] * {{ transition:fill 1s ease, stroke 1s ease }}
.drawn [data-part].flash {{ animation:throb 1.1s ease-in-out 3 }}
.part {{ opacity:0;
  --dx:0px; --dy:0px; --sc:1; --sq:1; --rot:0deg;
  transform:translate(calc(var(--px) + var(--dx)), calc(var(--py) + var(--dy)))
            rotate(var(--rot)) scale(var(--sc), calc(var(--sc) * var(--sq)));
  transition:transform 1.15s cubic-bezier(.33,.9,.3,1), opacity .5s ease }}
.step.on .part {{ animation:fade .55s ease var(--d) both }}
.part-in {{ transition:opacity .6s ease }}
.part ellipse, .part rect, .part line, .part path, .part circle {{
  transition:stroke 1s ease, fill 1s ease }}
.tube-hull {{ transition:none }}
.pt-label {{ font:600 17px/1 var(--sans); paint-order:stroke; stroke:#04060a;
  stroke-width:5; stroke-linejoin:round }}
.part.flash .part-in {{ animation:throb 1.1s ease-in-out 3 }}
@keyframes throb {{ 0%,100% {{ opacity:1 }} 50% {{ opacity:.35 }} }}
.scene-note {{ position:fixed; left:50%; transform:translateX(-50%);
  bottom:7vh; font:400 clamp(17px,1.7vw,24px)/1.4 var(--sans); color:#c6d0e0;
  background:rgba(9,12,18,.82); padding:11px 22px; border-radius:13px;
  border:1px solid #1e2836; opacity:0; transition:opacity .45s ease;
  max-width:64vw; text-align:center; backdrop-filter:blur(9px) }}
.scene-note.on {{ opacity:1 }}

/* ---- molecule / Lewis structure ---- */
.bond {{ stroke:#93a3bd; stroke-width:2.6; stroke-linecap:round; opacity:0 }}
.step.on .bond {{ animation:fade .45s ease var(--d) both }}
.bond-label {{ font:500 15px/1 var(--sans); fill:#8b98af; stroke:var(--ground);
  stroke-width:5; paint-order:stroke }}
.atom {{ opacity:0 }}
.step.on .atom {{ animation:rise .45s cubic-bezier(.2,.8,.3,1) var(--d) both }}
.el {{ font:640 34px/1 var(--sans); letter-spacing:-.01em }}
.chg {{ font:600 17px/1 var(--sans); fill:#f4c66a }}
.el-note {{ font:400 15px/1 var(--sans); fill:#8b98af }}
.lp {{ fill:#cdd6e5; opacity:.85 }}
.fig-cap {{ font:400 17px/1 var(--sans); fill:#7b8799; letter-spacing:.02em }}

/* ---- Bohr atom ---- */
.shell {{ fill:none; stroke:#3d4a5f; stroke-width:1.4; opacity:0 }}
.step.on .shell {{ animation:fade .5s ease var(--d) both }}
.e {{ fill:var(--accent); opacity:0 }}
.step.on .e {{ animation:pop .4s cubic-bezier(.2,.9,.3,1.3) var(--d) both }}
@keyframes pop {{ from {{ opacity:0; transform:scale(.2) }}
  to {{ opacity:1; transform:none }} }}
.shell-n {{ font:500 14px/1 var(--sans); fill:#6d7b93 }}
.nucleus {{ fill:#f2708a; fill-opacity:.16; stroke:#f2708a; stroke-opacity:.6;
  stroke-width:2 }}
.nuc-sym {{ font:640 32px/1 var(--sans); fill:#f2a0b3 }}
.arow {{ opacity:0 }}
.step.on .arow {{ animation:rise .4s ease var(--d) both }}
.ak {{ font:500 16px/1 var(--sans); fill:#6d7b93; letter-spacing:.04em;
  text-transform:uppercase }}
.av {{ font:600 20px/1 var(--sans); fill:#dbe3ef }}

/* ---- comparison grid: dense on purpose ---- */
.grid {{ width:min(94vw,1500px); display:flex; flex-direction:column; gap:7px;
  font-variant-numeric:tabular-nums }}
.gr {{ display:grid; grid-template-columns:minmax(120px,1.05fr)
  repeat(var(--n),minmax(0,1.55fr)); gap:7px; opacity:0 }}
.step.on .gr {{ animation:rise .45s cubic-bezier(.2,.8,.3,1) var(--d) both }}
.gh-row {{ opacity:1; animation:none }}
.gh {{ padding:11px 15px; border-radius:11px 11px 0 0; font:640 clamp(15px,1.35vw,21px)/1.2
  var(--sans); color:var(--c); background:color-mix(in srgb,var(--c) 13%,transparent);
  border-bottom:2px solid color-mix(in srgb,var(--c) 55%,transparent) }}
.gl {{ padding:13px 15px; font:600 clamp(13px,1.15vw,17px)/1.3 var(--sans);
  color:var(--dim); display:flex; align-items:center; text-align:right;
  justify-content:flex-end; letter-spacing:.02em }}
.gc {{ padding:13px 15px; border-radius:9px; background:#0e131b;
  border:1px solid color-mix(in srgb,var(--c) 22%,transparent);
  font:400 clamp(13px,1.2vw,17px)/1.42 var(--sans); color:#d3dcea;
  display:flex; align-items:center }}
.gc:empty {{ background:transparent; border-color:transparent }}

/* ---- one number ---- */
.number {{ text-align:center }}
.value {{ font:400 clamp(80px,15vw,230px)/1.06 var(--serif);
  letter-spacing:-.03em; padding-bottom:.05em;
  background:linear-gradient(175deg,#f2f6ff 12%,#8fb6ff 92%);
  -webkit-background-clip:text; -webkit-text-fill-color:transparent;
  opacity:0 }}
.step.on .value {{ animation:rise .7s cubic-bezier(.2,.8,.3,1) both }}
.value-label {{ margin-top:4px; font-size:clamp(20px,2.4vw,32px); font-weight:560;
  opacity:0 }}
.step.on .value-label {{ animation:rise .5s ease .18s both }}
.value-sub {{ margin-top:16px; font-size:clamp(16px,1.4vw,20px); color:var(--dim);
  opacity:0 }}
.step.on .value-sub {{ animation:rise .5s ease .3s both }}

/* ---- chart: a real plotted graph ---- */
.grid {{ stroke:#1b2430; stroke-width:1 }}
.axis {{ stroke:#33404f; stroke-width:1.5 }}
.tick {{ font:500 15px/1 var(--sans); fill:var(--dim) }}
.axis-label {{ font:600 14px/1 var(--sans); fill:var(--faint);
  letter-spacing:.06em; text-transform:uppercase }}
.legend text {{ font:600 16px/1 var(--sans) }}
.legend {{ opacity:0 }}
.step.on .legend {{ animation:fade .5s ease .7s both }}
.pt-v {{ font:600 15px/1 var(--sans); opacity:0 }}
.step.on .pt-v {{ animation:fade .4s ease .5s both }}
/* The line DRAWS itself left to right. pathLength normalises every path to
   1000 units, so one dash rule works whatever the data's real length is —
   without it a short series flicks on instantly and a long one crawls. */
.plot {{ stroke-dasharray:1000; stroke-dashoffset:1000; pathLength:1000 }}
.step.on .plot {{ animation:plot 1.15s cubic-bezier(.4,0,.2,1) var(--d) forwards }}
@keyframes plot {{ to {{ stroke-dashoffset:0 }} }}
.plot-fill {{ opacity:0 }}
.step.on .plot-fill {{ animation:fade .8s ease var(--d) both }}
.dot {{ opacity:0 }}
.step.on .dot {{ animation:pop .38s cubic-bezier(.2,.9,.3,1.3) var(--d) both }}
.bar {{ opacity:0; transform-box:fill-box; transform-origin:bottom }}
.step.on .bar {{ animation:grow .5s cubic-bezier(.2,.8,.3,1) var(--d) both }}
@keyframes grow {{ from {{ opacity:0; transform:scaleY(.02) }}
  to {{ opacity:1; transform:none }} }}
.cap {{ font:500 15px/1 var(--sans); fill:var(--dim) }}

/* ---- the plain fallback ---- */
/* The plain fallback. It has to survive a long sentence WITHOUT running off
   the bottom of the screen, which is what it was doing: a fixed clamp with a
   20ch measure sends a 110-character line past the viewport on a laptop.
   Height is capped and the type scales down with the text (--len is set from
   the character count when the step renders). */
/* A few words, framed like a readout — not a paragraph of the script.
   It used to be up to 150 characters of the narration set in large serif,
   which is how a whole presentation came out as "text plastered on a
   background": the screen was reciting what the voice was saying. */
.keyline {{ position:relative; font:500 clamp(26px,3.4vw,52px)/1.2 var(--sans);
  letter-spacing:.02em; max-width:22ch; text-align:center; text-wrap:balance;
  color:var(--ink); padding:34px 52px; text-transform:uppercase;
  text-shadow:0 0 26px rgba(95,216,255,.35) }}
/* Corner brackets, the way a HUD frames a target. Two elements, four corners,
   no extra markup. */
.keyline::before, .keyline::after {{ content:""; position:absolute;
  width:34px; height:34px; border:1.5px solid var(--accent); opacity:.5 }}
.keyline::before {{ left:0; top:0; border-right:0; border-bottom:0 }}
.keyline::after {{ right:0; bottom:0; border-left:0; border-top:0 }}
.step.on .keyline {{ animation:resolve .7s cubic-bezier(.2,.8,.3,1) both }}

.credit {{ position:fixed; left:6vw; bottom:3.2vh; font-size:11px;
  color:var(--faint); letter-spacing:.04em; max-width:56vw }}

@keyframes rise {{ from {{ opacity:0; transform:translateY(14px) }}
  to {{ opacity:1; transform:none }} }}

@media (prefers-reduced-motion:reduce) {{
  *, *::before, *::after {{ animation-duration:.01ms !important;
    animation-iteration-count:1 !important; transition-duration:.01ms !important }}
}}
</style>
<main id="deck" style="display:contents">{"".join(body)}</main>
<script>
// display:contents on the wrapper means it generates no box at all, so every
// rule written against `.step` (position:absolute; inset:0) behaves exactly as
// it did when the sections were children of <body>. The wrapper exists only so
// the finished artwork can be swapped in WITHOUT reloading the page — see
// swap() below.
const root = document.getElementById('deck');
let steps = [...root.querySelectorAll('.step')];
const START = {int(start)};
let shown = -2;

function show(i) {{
  if (i === shown) return;
  shown = i;
  steps.forEach(s => s.classList.toggle('on', +s.dataset.i === i));
  // A new slide starts undimmed and unfocused: the old highlight belonged to
  // a figure that is no longer on screen.
  focused = null;
  steps.forEach(s => {{
    s.classList.remove('focusing');
    s.querySelectorAll('.live').forEach(el => el.classList.remove('live'));
  }});
}}

function swap(html) {{
  // The art call finishes AFTER Neo has started talking, so the real figures
  // arrive underneath a page that is already up and already on a slide. This
  // used to be location.reload(), which blanked the screen mid-sentence — the
  // seam the two-call design exists to hide, put back in the worst place.
  // Replacing the markup in place keeps the event stream open, keeps the
  // current slide up, and costs no flash at all.
  const at = shown;
  root.innerHTML = html;
  steps = [...root.querySelectorAll('.step')];
  shown = -2;
  show(at);
}}

// WHICH PART IS HE TALKING ABOUT? Score every candidate on the live slide by
// how much of its own text has just been spoken, and lift the winner. Using
// each element's textContent means this works for a flow node, a process
// stage, a comparison panel and a table row without any of them being told
// about it. A weak best score focuses nothing, which is the right answer for
// the linking sentences between parts.
const STOP = new Set(['the','a','an','and','or','of','to','in','is','are','it',
  'that','this','for','on','as','with','by','from','its','into','at','be']);
function words(s) {{
  return (s || '').toLowerCase().replace(/[^a-z0-9 ]+/g, ' ').split(/\\s+/)
    .filter(w => w.length > 2 && !STOP.has(w));
}}
let focused = null;
function focus(said) {{
  const s = steps.find(x => x.classList.contains('on'));
  if (!s || !said || !said.length) return;
  const tail = new Set(said);
  const cands = s.querySelectorAll(
    '.node, .lead, .stage, .band, .panel, .gr, .value');
  let best = null, bestScore = 0;
  cands.forEach(el => {{
    const own = words(el.dataset.words || el.textContent);
    if (!own.length) return;
    const hit = own.filter(w => tail.has(w)).length;
    const score = hit / Math.sqrt(own.length);      // short labels shouldn't win by default
    if (hit >= 1 && score > bestScore) {{ bestScore = score; best = el }}
  }});
  if (!best || bestScore < 0.5) return;             // between parts: leave it alone
  if (best === focused) return;
  cands.forEach(el => el.classList.remove('live'));
  best.classList.add('live');
  s.classList.add('focusing');
  focused = best;
}}

function callout(i, j) {{
  const s = steps.find(s => +s.dataset.i === i);
  if (!s) return;
  // A figure's callout reveals a label. A SCENE's callout is a beat: it moves
  // the parts. Same event, same cue matching, two different meanings — which
  // is why the tracker needs to know nothing about animation.
  const scene = s.querySelector('.scene');
  if (scene) return beat(s, scene, j);
  const c = s.querySelector('[data-c="' + j + '"]');
  if (!c) return;
  c.classList.add('on');
  // A VERIFIED PICTURE: dim the plate, light the one part being named, and
  // punch a soft hole in the dimming over it so the structure itself stays
  // bright. This is the "point at the thing you are talking about" that a
  // drawn schematic could only approximate.
  const fig = s.querySelector('.fig.verified');
  if (!fig) return;
  fig.classList.add('focusing');
  fig.querySelectorAll('.pin').forEach(p => p.classList.remove('live'));
  c.classList.add('live');
  // Dim the plate, then punch a soft hole over the part being named. A plain
  // dark rectangle dims the structure too, which defeats the point — so the
  // dimming is MASKED: white keeps it, black lets the picture through.
  const NS = 'http://www.w3.org/2000/svg';
  let hole = fig.querySelector('#spot-hole');
  if (!hole) {{
    const defs = document.createElementNS(NS, 'defs');
    defs.innerHTML =
      '<radialGradient id="sg">' +
        '<stop offset="45%" stop-color="#000"/>' +
        '<stop offset="100%" stop-color="#fff"/></radialGradient>' +
      '<mask id="spot-mask" maskUnits="userSpaceOnUse" x="0" y="0" ' +
        'width="1000" height="620">' +
        '<rect width="1000" height="620" fill="#fff"/>' +
        '<circle id="spot-hole" r="165" fill="url(#sg)"/></mask>';
    fig.insertBefore(defs, fig.firstChild);
    hole = fig.querySelector('#spot-hole');
    const dimmer = fig.querySelector('.dimmer');
    if (dimmer) {{
      dimmer.setAttribute('mask', 'url(#spot-mask)');
      dimmer.style.transition = 'opacity .5s ease';
    }}
    hole.style.transition = 'cx .55s cubic-bezier(.3,.8,.3,1), ' +
                            'cy .55s cubic-bezier(.3,.8,.3,1)';
  }}
  hole.setAttribute('cx', c.dataset.vx);
  hole.setAttribute('cy', c.dataset.vy);
  const dim = fig.querySelector('.dimmer');
  if (dim) dim.setAttribute('opacity', '0.72');
}}

function beat(section, scene, j) {{
  let beats;
  try {{ beats = JSON.parse(scene.dataset.beats || '[]'); }} catch (_) {{ return }}
  const b = beats[j];
  if (!b) return;
  for (const [id, st] of Object.entries(b.set || {{}})) {{
    const el = scene.querySelector('[data-part="' + CSS.escape(id) + '"]');
    if (!el) continue;
    // Writing a variable and letting CSS tween it — no keyframes to author,
    // so any combination of move / grow / squash / recolour just animates.
    if (st.dx      !== undefined) el.style.setProperty('--dx', (st.dx * 10) + 'px');
    if (st.dy      !== undefined) el.style.setProperty('--dy', (st.dy * 6.2) + 'px');
    if (st.scale   !== undefined) el.style.setProperty('--sc', st.scale);
    if (st.squash  !== undefined) el.style.setProperty('--sq', st.squash);
    if (st.rotate  !== undefined) el.style.setProperty('--rot', st.rotate + 'deg');
    // A composed scene wraps its art in .part-in; a drawing the model made has
    // no wrapper, so fade the group itself.
    if (st.opacity !== undefined)
      (el.querySelector('.part-in') || el).style.opacity = st.opacity;
    if (st.tone) el.querySelectorAll('ellipse,rect,line,path,circle').forEach(n => {{
      if (n.getAttribute('stroke')) n.setAttribute('stroke', st.tone);
      if (n.getAttribute('fill') && n.getAttribute('fill') !== 'none')
        n.setAttribute('fill', st.tone);
    }});
    if (st.flash) {{ el.classList.remove('flash'); void el.offsetWidth;
                    el.classList.add('flash'); }}
  }}
  if (b.note) {{
    let n = section.querySelector('.scene-note');
    if (!n) {{ n = document.createElement('div'); n.className = 'scene-note';
              section.appendChild(n); }}
    n.textContent = b.note;
    requestAnimationFrame(() => n.classList.add('on'));
  }}
}}
show(START);

// Server-sent events. The window outlives the answer, so a dropped stream just
// means the walkthrough stops advancing — it must never blank the page.
let es;
function connect() {{
  es = new EventSource('{stream_path}');
  es.onmessage = e => {{
    let m; try {{ m = JSON.parse(e.data) }} catch (_) {{ return }}
    if (m.t === 'slide')   show(m.i);
    if (m.t === 'callout') callout(m.i, m.j);
    if (m.t === 'say')     focus(m.w);
    if (m.t === 'deck')    m.html ? swap(m.html) : location.reload();
    if (m.t === 'bye')     window.close();
  }};
  es.onerror = () => {{ es.close(); setTimeout(connect, 1500) }};
}}
connect();

addEventListener('keydown', e => {{
  // Escape MUST work. window.close() is refused for a page the script didn't
  // open, which is how you end up trapped looking at a full-screen borderless
  // window with no titlebar and no way out. Tell the server instead — it owns
  // the process holding the window.
  if (e.key === 'Escape' || e.key === 'q') {{
    try {{ fetch('/quit', {{method: 'POST'}}); }} catch (_) {{}}
    window.close();
    document.body.innerHTML =
      '<div style="position:fixed;inset:0;display:flex;align-items:center;' +
      'justify-content:center;color:#5b6678;font:400 15px system-ui">closing…</div>';
    return;
  }}
  if (e.key === 'ArrowRight' || e.key === ' ') show(Math.min(shown + 1, steps.length - 2));
  if (e.key === 'ArrowLeft') show(Math.max(shown - 1, -1));
}});
</script>
"""


# --------------------------------------------------------------------------- #
# Real pictures, free.
#
# Wikimedia Commons: no key, no account, no quota worth worrying about, and it
# holds the medical illustrations and public-domain plates that are exactly what
# a walkthrough about a physical thing wants. Everything here is best-effort —
# a failed lookup means that step draws a silhouette instead, which is a worse
# figure and not a broken one.
#
# The bytes are fetched ONCE, when the walkthrough is built, and served from the
# deck's own localhost server. The page therefore has no external dependency at
# all: nothing pops in halfway through a sentence, nothing breaks offline, and
# no request leaves the machine while they're watching.
# --------------------------------------------------------------------------- #
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
# Wikimedia asks for a real User-Agent and returns 403 without one.
UA = "Neo/1.0 (personal assistant; contact via github)"
IMAGE_TIMEOUT = 7.0
IMAGE_MAX_BYTES = 6_000_000
# Below this, whatever came back is an icon or Commons' "no thumbnail"
# placeholder, never a medical illustration. See search_image.
IMAGE_MIN_BYTES = int(os.getenv("NEO_IMAGE_MIN_BYTES", "24000"))
_OK_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/svg+xml": "svg",
             "image/gif": "gif", "image/webp": "webp"}


def _strip_html(text):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", text or "")).strip()


def image_credit(meta):
    """Attribution line. Pure, and it is not optional — these are other
    people's photographs under licences that ask to be named."""
    artist = _strip_html((meta or {}).get("Artist", {}).get("value", ""))[:70]
    lic = _strip_html((meta or {}).get("LicenseShortName", {}).get("value", ""))[:40]
    bits = [b for b in (artist, lic, "via Wikimedia Commons") if b]
    return " · ".join(bits)


# Words in a Commons filename that predict a picture we should NOT layer our
# own labels onto, or that a learning figure is better off without.
#
# This exists because of what the search actually returns. Ask for "human heart
# anatomy" and the top hits are `Heart numlabels.svg` and `Heart diagram-en.svg`
# — both already carry their own labels, baked into the image. Put leader lines
# on top of those and you get two competing sets of annotations, which reads as
# a bug. The last group is a different judgement: a walkthrough that is teaching
# someone is better served by an illustration than by an autopsy photograph, and
# the third result for that same search is exactly that.
# Words that predict a picture NOT worth putting on screen.
#
# This list used to punish anything pre-labelled, on the theory that our own
# leader lines would collide with the file's. That was backwards. A published
# medical illustration with its own labels — which is most of the good ones —
# is exactly what teaches, and `_figure` already keeps our labels in a side
# column with no arrows whenever the picture is a real fetched image, so there
# is nothing to collide with. What actually needs avoiding is much narrower:
# pictures of a real body on a table, and non-English label sets.
_AVOID = (
    "autopsy", "cadaver", "dissection", "gross specimen", "postmortem",
    "logo", "icon", "stamp", "coat of arms",
    # Commons' own placeholder and boilerplate files. These match a medical
    # search often enough to win it, and they carry no information at all.
    "question book", "placeholder", "no image", "image missing", "nuvola",
    "crystal clear", "wiki letter", "ambox", "commons-logo", "symbol ",
    "disambig", "edit-clear", "text document",
)


# Commons is multilingual and its best-ranked diagrams are frequently the
# German, Spanish or Russian editions of the same plate — with the labels
# BAKED INTO THE IMAGE. Those went straight to their screen: a perfectly good
# figure of the right subject, annotated in a language they do not read.
#
# The old guard was a hand-written list of filename suffixes — "-de.", "-fr."
# and six more — which caught `Heart diagram-de.svg` and nothing else. It
# missed every file simply NAMED in another language, and every language not on
# the list. Two signals catch nearly all of it:
#   - a script that is not Latin at all, which is unambiguous, and
#   - a language tag in the filename, in any of the forms Commons uses.
_NON_LATIN = re.compile(
    "[\u0370-\u03ff\u0400-\u04ff\u0530-\u058f\u0590-\u05ff"
    "\u0600-\u06ff\u0900-\u097f\u0e00-\u0e7f\u3040-\u30ff"
    "\u3400-\u9fff\uac00-\ud7af]")
_LANG_CODES = ("de", "fr", "es", "it", "pt", "ru", "ja", "zh", "ko", "ar",
               "fa", "they", "hi", "nl", "pl", "tr", "sv", "da", "fi", "no",
               "cs", "hu", "ro", "uk", "vi", "id", "th", "el", "ca", "sr",
               "bg", "hr", "sk", "sl", "lt", "lv", "et", "eu", "gl", "ms")
# `heart-de.svg`, `heart_de.png`, `heart (de).jpg`, `heart.de.svg`
_LANG_TAG = re.compile(
    r"[-_. (\[]({})[-_. )\]]".format("|".join(_LANG_CODES)), re.I)


def foreign_language(title):
    """True when this Commons filename is very likely a non-English edition.
    Pure, so the judgement is testable without the network."""
    t = title or ""
    if _NON_LATIN.search(t):
        return True
    # Strip the "File:" prefix and the extension before looking for a tag, so
    # a real extension can never read as a language code.
    stem = re.sub(r"^file:", "", t, flags=re.I)
    stem = re.sub(r"\.[a-z0-9]{2,4}$", "", stem, flags=re.I)
    return bool(_LANG_TAG.search(stem + " "))


def rank_image(title, mime, index=0):
    """Score one search result. Lower is better; None means don't use it.

    Pure, so the judgement is testable without the network.
    """
    if mime not in _OK_TYPES:
        return None
    if foreign_language(title):
        # Not a penalty. A figure whose labels are in Russian teaches the user
        # nothing, however good the drawing is, so it is not a candidate.
        return None
    low = (title or "").lower()
    score = float(index)                       # search order is a real signal
    for bad in _AVOID:
        if bad in low:
            score += 12
    # Collections that are reliably teaching-grade. Being in one of these is
    # worth more than being first in a keyword search.
    for good, bonus in (("blausen", -9), ("openstax", -8), ("gray", -6),
                        ("anatomy", -3), ("illustration", -3), ("diagram", -2),
                        ("scheme", -2), ("cross section", -3), ("histolog", -2)):
        if good in low:
            score += bonus
    # A labelled teaching plate is the GOAL, not a hazard. Reward it.
    for good, bonus in (("label", -2), ("annotated", -2), ("numbered", -1)):
        if good in low:
            score += bonus
    return score


def pick_image(pages):
    """Best result out of a Commons response, or None. Pure."""
    best, best_score = None, None
    for page in pages or ():
        info = (page.get("imageinfo") or [{}])[0]
        url = info.get("thumburl") or info.get("url")
        if not url:
            continue
        score = rank_image(page.get("title", ""), info.get("mime"),
                           page.get("index", 99))
        if score is None:
            continue
        if best_score is None or score < best_score:
            best, best_score = page, score
    return best


def search_image(term, log=print):
    """Find a picture for `term`. Returns (bytes, credit) or None.

    Never raises: the whole feature has to survive no network, a slow mirror, a
    404, and a search that simply finds nothing relevant.
    """
    import json as _json
    import urllib.parse
    import urllib.request
    if not term:
        return None
    # Commons holds the Blausen medical set, OpenStax Anatomy & Physiology and
    # the Gray's plates — genuinely good teaching illustrations. A bare keyword
    # search buries them under snapshots and logos, so bias the query toward
    # the words those files actually carry, and ask for more candidates than we
    # need so the ranker has something to choose between.
    query = urllib.parse.urlencode({
        "action": "query", "format": "json", "generator": "search",
        # The search terms are ONLY their subject. An earlier version appended
        # "(diagram OR illustration OR anatomy OR medical OR scheme)" to bias
        # towards teaching plates, and it hijacked relevance completely: four
        # different queries — a herniated disc, a nephron, a bilayer — all came
        # back with the SAME generic medical illustration, because that OR
        # clause was doing the matching instead of the subject. Bias belongs in
        # the ranker, which sees the results, not in the query, which decides
        # what the results are.
        "gsrsearch": f"filetype:bitmap|drawing {term}",
        "gsrnamespace": "6", "gsrlimit": "25",
        "prop": "imageinfo", "iiprop": "url|mime|extmetadata|size",
        # A RENDERED thumbnail, always. Commons will hand back the original
        # otherwise, and originals are 3MB TIFFs and 40MB SVG-derived PNGs that
        # a browser may not even display — one came back at 3348KB with a type
        # this code could not sniff. 1280 is plenty for a full-screen figure.
        "iiurlwidth": "1280",
    })
    try:
        req = urllib.request.Request(f"{COMMONS_API}?{query}",
                                     headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=IMAGE_TIMEOUT) as r:
            data = _json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        log(f"[deck] image search failed ({type(e).__name__})")
        return None

    # Dicts here are keyed by page id, so ranked order has to be computed.
    pages = list(((data.get("query") or {}).get("pages") or {}).values())
    if not pages:
        log(f"[deck] Commons found nothing at all for '{term}'")
        return None

    # Work down the ranking until one actually loads. Eight, not three: a
    # search returning 25 candidates and giving up after three is why subjects
    # Commons plainly has — a phospholipid bilayer, a nephron — came back as
    # 'nothing usable'. Each rejection says WHY, so a miss is diagnosable
    # instead of mysterious.
    tried, why = set(), []
    for _ in range(8):
        page = pick_image([p for p in pages if p.get("title") not in tried])
        if page is None:
            break
        title = page.get("title", "?")
        tried.add(title)
        info = (page.get("imageinfo") or [{}])[0]
        url = info.get("thumburl")
        if not url:
            # No rendered thumbnail: the original is the only option and those
            # are the multi-megabyte TIFFs. Skip rather than ship one.
            why.append(f"{title[:34]}: no thumbnail")
            continue
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=IMAGE_TIMEOUT) as r:
                blob = r.read(IMAGE_MAX_BYTES + 1)
        except Exception as e:
            why.append(f"{title[:34]}: {type(e).__name__}")
            continue
        if not blob:
            why.append(f"{title[:34]}: empty")
            continue
        if len(blob) > IMAGE_MAX_BYTES:
            why.append(f"{title[:34]}: {len(blob)//1024}KB, too big")
            continue
        # TOO SMALL is the failure that actually reached the screen. Commons
        # serves a generic placeholder — a blue square with a question mark —
        # when a file has no renderable thumbnail, and it sails through every
        # other check here: it is a real PNG, it is small, it loads. Blown up
        # to fill a slide it becomes a giant "?" beside two labels pointing at
        # nothing, which is what the user got for "lumbar vertebrae".
        #
        # A real teaching plate rendered at 1280px is tens of kilobytes at
        # minimum. An icon is a couple. The threshold does not need to be
        # clever.
        if len(blob) < IMAGE_MIN_BYTES:
            why.append(f"{title[:34]}: {len(blob)//1024}KB — a placeholder, not a plate")
            continue
        if blob[:4] not in (b"\x89PNG",) and blob[:2] != b"\xff\xd8" \
                and blob[:3] != b"GIF" and blob[8:12] != b"WEBP" \
                and blob[:5] not in (b"<?xml", b"<svg "):
            # Whatever this is, a browser will not render it inline.
            why.append(f"{title[:34]}: not a web image")
            continue
        return blob, image_credit(info.get("extmetadata"))

    log(f"[deck] no usable image for '{term}' — tried {len(tried)}: "
        + "; ".join(why[:4]))
    return None


def image_selftest(terms=None, log=print):
    """`python3 -c "import deck; deck.image_selftest()"` — does the picture
    pipeline actually work on THIS machine?

    It exists because the sandbox this code is written in has no network at
    all, so the fetch path cannot be verified where it is written. Rather than
    guess, this runs the real searches and says plainly what came back.
    """
    terms = terms or ["lumbar disc herniation anatomy",
                      "cauda equina spinal nerve roots",
                      "phospholipid bilayer membrane",
                      "nephron kidney structure",
                      "human heart cross section"]
    ok, prints = 0, {}
    for t in terms:
        started = time.time()
        got = search_image(t, log=lambda m: None)
        took = time.time() - started
        if got:
            blob, credit = got
            ok += 1
            kind = ("PNG" if blob[:4] == b"\x89PNG" else
                    "SVG" if blob[:5] in (b"<?xml", b"<svg ") else
                    "JPEG" if blob[:2] == b"\xff\xd8" else "?")
            fp = image_fingerprint(blob)
            dupe = " <-- SAME FILE AS AN EARLIER SEARCH" if fp in prints else ""
            prints.setdefault(fp, t)
            log(f"  OK    {t[:32]:32} {took:4.1f}s {len(blob)//1024:5}KB "
                f"{kind:4} {fp[:8]}  {credit[:30]}{dupe}")
        else:
            log(f"  MISS  {t[:34]:34} {took:4.1f}s  nothing usable came back")
    log(f"\n  {ok}/{len(terms)} searches returned a picture, "
        f"{len(prints)} of them DIFFERENT.")
    if ok and len(prints) < ok:
        log("  Distinct results matter more than the hit rate: the same plate "
            "on every step is worse than no plate at all.")
    if not ok:
        log("  Pictures are NOT working here. Most likely: no network, or "
            "Wikimedia is refusing the User-Agent.")
    return ok


def image_fingerprint(blob):
    """Cheap identity for a picture. Pure.

    Exists because the failure above was invisible until someone read the byte
    counts and noticed four different searches returning 435KB each. Two steps
    showing the same plate is a bug the viewer notices immediately.
    """
    import hashlib
    return hashlib.sha1(blob or b"").hexdigest()[:16]


def collect_images(plan, log=print, fetch=None, client=None):
    """Pull every picture the walkthrough asked for, in parallel.

    Parallel because they're independent and serial fetching would put the whole
    art phase behind the slowest one. A step whose image doesn't arrive keeps
    its `shape`, so the figure still has something to label.
    """
    fetch = fetch or search_image
    steps = plan.get("slides") or []
    wanted = [(i, s["visual"]["image"]) for i, s in enumerate(steps)
              if (s.get("visual") or {}).get("kind") == "figure"
              and s["visual"].get("image")]
    if not wanted:
        return {}

    images, lock = {}, threading.Lock()

    seen_prints = {}

    def one(i, term):
        # THE VERIFIED PATH. When there is a client to see with, imagery.py
        # judges the picture and locates its parts, and nothing reaches the
        # screen unless a second, blind look agrees about where each part is.
        # That is what makes it safe to draw a marker on a photograph Neo did
        # not draw — the objection this file used to raise, and the reason it
        # showed a bare picture with a list of words beside it.
        if client is not None:
            try:
                import imagery
                shot = imagery.illustrate(term, client, log=log)
            except Exception as e:
                log(f"[img] {term!r} failed ({type(e).__name__}) — plain picture")
                shot = None
            if shot:
                blob, credit, parts = shot
                with lock:
                    if image_fingerprint(blob) in seen_prints:
                        steps[i]["visual"]["image"] = ""
                        return
                    seen_prints[image_fingerprint(blob)] = term
                    images[i] = blob
                v = steps[i]["visual"]
                v["credit"] = credit
                if parts:
                    # Verified parts BECOME the figure's labels, so the sync
                    # machinery that already exists — plan_cues, the Tracker,
                    # callouts — lights each one as Neo names it, with no new
                    # mechanism at all.
                    v["labels"] = [{"text": p["name"], "x": p["x"], "y": p["y"],
                                    "say": p["name"]} for p in parts]
                    v["verified"] = True
                    try:            # the renderer needs the real aspect ratio
                        from PIL import Image as _I
                        import io as _io
                        v["iw"], v["ih"] = _I.open(_io.BytesIO(blob)).size
                    except Exception:
                        pass
                return
        got = fetch(term, log)
        if not got:
            steps[i]["visual"]["image"] = ""     # fall back to the silhouette
            return
        blob, credit = got
        print_ = image_fingerprint(blob)
        with lock:
            if print_ in seen_prints:
                # The same plate on two steps reads as broken. Better to draw
                # the silhouette for this one than to repeat the picture.
                log(f"[deck] '{term}' returned the same picture as "
                    f"'{seen_prints[print_]}' — skipping the duplicate")
                steps[i]["visual"]["image"] = ""
                return
            seen_prints[print_] = term
            images[i] = blob
        steps[i]["visual"]["credit"] = credit

    workers = [threading.Thread(target=one, args=(i, t), daemon=True)
               for i, t in wanted]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=IMAGE_TIMEOUT + 3)
    log(f"[deck] {len(images)}/{len(wanted)} pictures")
    return images


# --------------------------------------------------------------------------- #
# Runtime. Everything below touches the network, the filesystem or a window.
# --------------------------------------------------------------------------- #
_current = None
_lock = threading.Lock()
# Neo starts narrating the moment present() returns, but the window takes a few
# more seconds to build. Without this, everything said in that gap is lost and
# the deck opens on step one while they are already on step three — which is
# exactly the "not tagged together at all" the user described. So while a deck is
# building, their words are kept here and replayed into the tracker the instant
# it appears.
_prebuffer = None
# Bumped every time a build starts. A worker that finishes after a newer build
# began throws its window away instead of stealing the screen.
_build_gen = 0


class Presentation:
    """A deck on screen, plus the machinery that keeps it in step with Neo.

    Owns a localhost HTTP server (the page, and an event stream) and a child
    process holding the window. Both are disposable: if either dies the worst
    case is a deck that stops moving, never a Neo that stops answering.
    """

    def __init__(self, deck, log=print, images=None, min_dwell=None):
        self.deck = deck
        self.log = log
        # index -> raw bytes, fetched once when the walkthrough was built.
        self.images = dict(images or {})
        # Exposed so a timing test can run the sync logic at speed without a
        # four-and-a-half-second floor on every slide.
        self.min_dwell = MIN_SLIDE_S if min_dwell is None else min_dwell
        self.tracker = Tracker(deck.get("slides") or [],
                               min_dwell=self.min_dwell)
        self._clients = []          # open SSE connections
        self._clients_lock = threading.Lock()
        self._backlog = []          # events sent before the page connected
        self.shown = -1             # which slide is up; -1 is the title card
        self._server = None
        self._proc = None
        self.port = None
        self.opened_at = time.time()
        self.closed = False
        # Slide changes waiting for the voice to catch up. See feed()/_emit().
        self._due = []
        self._due_lock = threading.Lock()
        self._last_due = 0.0
        self._pump = None

    # ---- the server ---------------------------------------------------- #
    def serve(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass          # the deck is not interesting enough to log

            def do_POST(self):
                # The only way out of a borderless full-screen window.
                if self.path.startswith("/quit"):
                    self.send_response(204)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    threading.Thread(target=outer.close, daemon=True).start()
                    return
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                if self.path.startswith("/events"):
                    return self._events()
                if self.path.startswith("/img/"):
                    return self._image()
                page = render_html(outer.deck, start=outer.shown).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(page)

            def _image(self):
                try:
                    idx = int(self.path.rsplit("/", 1)[-1].split("?")[0])
                except ValueError:
                    idx = -1
                blob = outer.images.get(idx)
                if not blob:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                # Sniffed, not trusted: the type came off the network. Only the
                # handful of formats a browser renders inline are served, so a
                # surprise payload can't become an active document.
                kind = ("image/png" if blob[:4] == b"\x89PNG" else
                        "image/svg+xml" if blob[:5] in (b"<?xml", b"<svg ") else
                        "image/gif" if blob[:3] == b"GIF" else
                        "image/webp" if blob[8:12] == b"WEBP" else "image/jpeg")
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(blob)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(blob)

            def _events(self):
                # Close-delimited on purpose. This response has no
                # Content-Length and BaseHTTPRequestHandler does not chunk, so
                # under HTTP/1.1 keep-alive it has no delimiter at all and the
                # browser is entitled to buffer it forever — which shows up as a
                # deck that never advances. EventSource reconnects by itself, so
                # ending the connection costs nothing.
                self.close_connection = True
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                q = outer._subscribe()
                try:
                    while not outer.closed:
                        try:
                            msg = q.get(timeout=12)
                        except Exception:
                            # A comment keeps the connection warm. Without it
                            # the socket is torn down between slides and the
                            # page spends the deck reconnecting.
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                            continue
                        self.wfile.write(
                            f"data: {json.dumps(msg)}\n\n".encode("utf-8"))
                        self.wfile.flush()
                except Exception:
                    pass          # they closed the window; that's a normal exit
                finally:
                    outer._unsubscribe(q)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True,
                         name="neo-deck-http").start()
        return self.port

    def _subscribe(self):
        import queue as _q
        q = _q.Queue()
        with self._clients_lock:
            for msg in self._backlog:       # a late page still catches up
                q.put(msg)
            self._clients.append(q)
        return q

    def _unsubscribe(self, q):
        with self._clients_lock:
            if q in self._clients:
                self._clients.remove(q)

    def push(self, msg):
        with self._clients_lock:
            if msg.get("t") in ("slide", "callout"):
                self._backlog.append(msg)
            if msg.get("t") == "slide":
                self.shown = msg.get("i", self.shown)
            for q in list(self._clients):
                try:
                    q.put_nowait(msg)
                except Exception:
                    pass

    # ---- the window ----------------------------------------------------- #
    def open_window(self):
        import subprocess
        import sys
        self._proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--window",
             f"http://127.0.0.1:{self.port}/"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return self._proc

    def alive(self):
        return self._proc is not None and self._proc.poll() is None

    # ---- keeping up with Neo -------------------------------------------- #
    def feed(self, text, lag=0.0):
        """Neo just GENERATED this. Show it when they have SAID it.

        `lag` is how many seconds of speech are queued but not yet played. The
        transcript runs ahead of the voice — measured, about three times ahead
        — so an event derived from the transcript has to wait that long before
        it means anything to the person watching. Without this the deck is a
        readout of the model's output stream, which is what tore through six
        slides in twenty seconds while the user was still hearing slide two.
        """
        if self.closed:
            return
        try:
            events = list(self.tracker.feed(text))
        except Exception as e:
            self.log(f"[deck] sync stopped: {e}")
            return
        for ev in events:
            msg = ({"t": "slide", "i": ev[1]} if ev[0] == "slide"
                   else {"t": "callout", "i": ev[1], "j": ev[2]})
            self._emit(msg, lag)
        # THE FOCUS STREAM. The last handful of words Neo has said, so the page
        # can light up whichever part of the figure owns them. Sent on the same
        # lag as everything else, or the highlight would run ahead of the voice
        # exactly like the slides used to.
        tail = self.tracker.spoken[-14:]
        if tail:
            self._emit({"t": "say", "w": tail}, lag)

    def _emit(self, msg, lag):
        """Send now, or hold it until the voice catches up. Order is kept."""
        if lag <= 0.08:
            self.push(msg)
            return
        with self._due_lock:
            # Monotonic: a later event can never be scheduled before an earlier
            # one, however the lag moves. A deck that jumps backwards is worse
            # than one that is slightly late.
            at = max(time.time() + min(lag, MAX_SYNC_LAG_S), self._last_due)
            self._last_due = at
            self._due.append((at, msg))
            if self._pump is None or not self._pump.is_alive():
                self._pump = threading.Thread(target=self._drain, daemon=True,
                                              name="neo-deck-sync")
                self._pump.start()

    def _drain(self):
        while not self.closed:
            with self._due_lock:
                if not self._due:
                    return
                at, msg = self._due[0]
                wait = at - time.time()
                if wait <= 0:
                    self._due.pop(0)
                else:
                    msg = None
            if msg is not None:
                try:
                    self.push(msg)
                except Exception:
                    pass
                continue
            time.sleep(min(0.12, max(0.02, wait)))

    def finished(self):
        """Neo has narrated the last slide and said enough of it to be done.

        The deck used to just sit there when they stopped talking — full screen,
        borderless, over everything, until it was closed by hand. A walkthrough
        that has been walked through is over.
        """
        plan = self.tracker.plan
        if not plan:
            return False
        if self.tracker.index < len(plan) - 1:
            return False
        spoken_here = len(self.tracker.spoken) - self.tracker._budget
        if spoken_here < plan[-1]["words"] * 0.85:
            return False
        # The tracker runs on TRANSCRIPT time, which is about three times ahead
        # of the voice. Closing on it alone took the window away while the user
        # was still listening to the last slide — the log has the deck closing
        # six seconds after the transcript ended and twenty seconds of speech
        # still queued. Wait for the display queue to drain too.
        with self._due_lock:
            if self._due:
                return False
        return self.shown >= len(plan) - 1

    def replace(self, deck, images=None):
        """Swap in the finished art while the deck is already on screen. This is
        what makes the two-call design invisible: Neo starts talking over the
        outline, and the real slides arrive underneath it.

        It used to reload the page, and a reload is a blank screen in the middle
        of a sentence — bad enough that the whole thing was changed to build
        everything first and open at the end, which traded the flash for a
        thirty-second wait. The log shows what that cost: `on screen at step 6`
        on a six-step deck, i.e. the window finally appeared after Neo had
        finished narrating it. Now the markup is swapped in place over the open
        event stream: no reload, no flash, and the window is up from the start.

        `images` arrives with the artwork because the pictures are fetched in
        the same phase, and /img/<i> reads straight out of this dict.
        """
        self.deck = deck
        if images:
            self.images.update(images)
        self.tracker.plan = plan_cues(deck.get("slides") or [])
        # The backlog exists so a page that connects late catches up. Its old
        # callouts point at labels that no longer exist on the new markup, so
        # they go; the slide the deck is on is re-stated instead.
        with self._clients_lock:
            self._backlog = []
        self.push({"t": "deck", "html": steps_html(deck)})
        if self.shown > -1:
            self.push({"t": "slide", "i": self.shown})

    def takeaway_index(self):
        """The index of the closing card, or None when the plan has no
        takeaway. One past the last slide on purpose: nothing the Tracker can
        emit reaches it, so it only ever appears because the outro asked."""
        deck = getattr(self, "deck", None) or {}
        if not deck.get("takeaway"):
            return None
        return len(deck.get("slides") or ())

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.push({"t": "bye"})
        except Exception:
            pass
        try:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
        except Exception:
            pass
        try:
            if self._server:
                srv = self._server

                def _shut():
                    # shutdown() stops serve_forever but LEAVES THE LISTENING
                    # SOCKET OPEN. Measured: 20 decks leaked 20 fds and 20
                    # bound ports, and an orphan window's EventSource retry
                    # connected to the dead socket and hung instead of failing.
                    try:
                        srv.shutdown()
                    except Exception:
                        pass
                    try:
                        srv.server_close()
                    except Exception:
                        pass
                threading.Thread(target=_shut, daemon=True).start()
        except Exception:
            pass


def current():
    with _lock:
        return _current


def feed(text, lag=0.0):
    """Called from neo.py every time Neo's transcript moves. Cheap and total —
    no presentation on screen means this does nothing at all.

    `lag` is seconds of speech already generated but not yet heard; neo.py
    reads it off the live session's playback buffer. See Presentation.feed.
    """
    p = current()
    if p is None or p.closed:
        with _lock:
            if _prebuffer is not None and text:
                _prebuffer.append(text)     # a deck is coming; don't lose this
        return
    if not p.alive() and time.time() - p.opened_at > 4:
        p.close()               # they closed the window; stop tracking
        _set(None)
        return
    p.feed(text, lag)

    # Done narrating? Give the last slide a couple of seconds to be looked at,
    # then take the window down by itself.
    if p.finished() and not getattr(p, "_bowing_out", False):
        p._bowing_out = True

        def _bow_out():
            # The audio still queued has to play out before anything changes,
            # or the closing card lands while Neo is mid-sentence.
            time.sleep(min(lag, MAX_SYNC_LAG_S))
            try:
                idx = p.takeaway_index()
                if idx is not None and current() is p:
                    p.push({"t": "slide", "i": idx})
            except Exception:
                pass
            # LINGER_S is time to read it.
            time.sleep(LINGER_S)
            if current() is p and not p.closed:
                p.log("[deck] narration finished — closing the walkthrough.")
                close("finished")

        threading.Thread(target=_bow_out, daemon=True,
                         name="neo-deck-outro").start()


def pace(text, wps=WORDS_PER_SECOND):
    """Sync for the FALLBACK path, where there is no live transcript.

    The turn-based pipeline synthesises audio from text and hands back sound, so
    there is no stream of "what Neo is saying right now" to follow. The next
    best thing is to feed the script at roughly the rate it's being spoken.
    It drifts — that's exactly why the live path doesn't do this — but a deck
    that drifts a little beats a deck frozen on slide one, and this path only
    runs when live mode is already unavailable.
    """
    p = current()
    if p is None or p.closed or not text:
        return None

    words = text.split()

    def _run():
        for i in range(0, len(words), 4):
            if p.closed or current() is not p:
                return
            p.feed(" ".join(words[i:i + 4]))
            time.sleep(4.0 / max(0.5, wps))

    t = threading.Thread(target=_run, daemon=True, name="neo-deck-pace")
    t.start()
    return t


def close(_reason=""):
    global _prebuffer
    p = current()
    if p is not None:
        p.close()
    with _lock:
        _prebuffer = None      # otherwise it grows forever after a failed build
    _set(None)


def _set(p):
    global _current
    with _lock:
        _current = p


def _install(p, gen):
    """Make `p` the deck on screen, but only if no newer build has started.

    Checking staleness and then installing as two separate steps leaves a real
    gap: a second present() can bump the generation and run its own close()
    (finding nothing, because this one hasn't installed yet) in between, after
    which this build installs anyway and its full-screen window is orphaned —
    close_presentation only ever closes `current`. One lock, one decision.
    """
    global _current
    with _lock:
        if gen != _build_gen:
            return False
        _current = p
        return True


def build(topic, client, log=print, context_note="", n=DEFAULT_SLIDES):
    """The narration call. Blocking and deliberately small — this is the only
    thing between the question and Neo's first word.

    Deliberately the CHAT model, not the heavy one. Chat is already resolved and
    warm, heavy is lazy and may have to probe first, and the difference in prose
    quality over six short paragraphs is not worth several seconds of silence.
    The heavy model gets the visuals call, where the latency is hidden.
    """
    import providers
    chat = providers.resolve("chat", client, log)
    heavy = providers.resolve("heavy", client, log)
    # (job, provider, model) — the PROVIDER travels with the model now, because
    # a candidate list can legitimately return a Groq id and only providers.py
    # knows how to call one.
    models = [(job, p, m) for job, (p, m) in (("chat", chat), ("heavy", heavy)) if m]
    if not models:
        return None

    # More than one model, because a plan that doesn't arrive is not a worse
    # presentation, it is NO presentation. Two things kill this call and both
    # are ordinary: a 503 on the model of the day, and a refusal — the user asks
    # about a condition their dad actually has, and a model that decides medical
    # explanation is off-limits answers in prose instead of JSON. Either way
    # the old code gave up on the first attempt and present() returned None,
    # which reached them as "I'm having trouble loading that visual."
    last = ""
    for attempt, (job, provider, model) in enumerate(models):
        try:
            r = providers.generate(client, provider, model,
                                   plan_prompt(topic, context_note, n), log=log)
        except Exception as e:
            # This was the whole mystery. The plan call was failing and NOTHING
            # wrote why: agent.py caught it and handed the model a bare class
            # name, so 3789 lines of neo.log contained not one [deck] entry and
            # there was no way to tell a quota error from a bad model name from
            # a timeout.
            last = f"{type(e).__name__}: {e}"
            log(f"[deck] plan call failed on {model}: {last}")
            if model_is_down(e):
                try:
                    providers.report_failure(job, provider, model, log)
                except Exception:
                    pass
            continue
        text = getattr(r, "text", "") or ""
        plan = parse_plan(text, topic)
        if plan:
            if attempt:
                log(f"[deck] the plan came from {model} (attempt {attempt + 1})")
            return plan
        last = f"nothing usable: {text[:160]!r}"
        log(f"[deck] {model} returned {last}")
    log(f"[deck] no model would plan this walkthrough — {last}")
    return None


def _finish_reason(resp):
    """Why the model stopped. A truncated answer is the failure that hurts most
    here — the JSON ends mid-object, nothing parses, and every step quietly
    becomes plain text with no indication why. Pure, and tolerant of whatever
    shape the SDK hands back."""
    try:
        cands = getattr(resp, "candidates", None) or ()
        if not cands:
            return ""
        fr = getattr(cands[0], "finish_reason", None)
        return str(getattr(fr, "name", fr) or "")
    except Exception:
        return ""


# Errors that mean "this model is not available right now", as opposed to
# "this model answered and I didn't like the answer". Only the first kind is
# worth telling providers.py about.
_MODEL_DOWN = ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "500",
               "INTERNAL", "DEADLINE", "404", "NOT_FOUND")


def model_is_down(err):
    """Pure, so it can be tested without a network."""
    text = f"{type(err).__name__}: {err}".upper()
    return any(sig in text for sig in _MODEL_DOWN)


# What "enough on the screen to be worth looking at" means, per shape. A figure
# that clears its schema can still be nearly empty — a two-node flow, a versus
# with one bullet a side, a chart with a single point. Those parse, render, and
# teach nothing, and they went out exactly as they arrived.
_MIN_CONTENT = {
    "flow": lambda v: len(v.get("nodes") or ()) >= 3 and bool(v.get("edges")),
    "process": lambda v: len(v.get("steps") or ()) >= 3,
    "crosssection": lambda v: len(v.get("layers") or ()) >= 3,
    "versus": lambda v: min(len((v.get(s) or {}).get("points") or ())
                            for s in ("left", "right")) >= 2,
    "grid": lambda v: len(v.get("rows") or ()) >= 2 and len(v.get("cols") or ()) >= 2,
    "figure": lambda v: bool(v.get("image")) or len(v.get("labels") or ()) >= 3,
    "chart": lambda v: any(len(sr.get("points") or ()) >= 2
                           for sr in (v.get("series") or ())),
    "number": lambda v: bool(v.get("value")) and bool(v.get("label")),
    "molecule": lambda v: len(v.get("atoms") or ()) >= 2,
    "atom": lambda v: bool(v.get("symbol")),
}


def too_thin(visual):
    """True when this visual is technically valid and visually empty. Pure.

    Deliberately generous: the job is to catch the placeholder, not to referee
    taste. Anything whose kind has no rule passes.
    """
    if not isinstance(visual, dict):
        return True
    rule = _MIN_CONTENT.get(visual.get("kind"))
    if rule is None:
        return False
    try:
        return not rule(visual)
    except Exception:
        return False        # a malformed spec is _clean_visual's problem


def visuals_batch(plan, client, model, offset, log=print, cfg=None,
                  provider="gemini", job="heavy", kinds=None):
    """One batch of steps -> their visuals, or None. Never raises."""
    import providers
    try:
        r = providers.generate(client, provider, model,
                               visuals_prompt(plan, offset, kinds=kinds),
                               max_output_tokens=MAX_OUTPUT_TOKENS, log=log)
    except Exception as e:
        log(f"[deck] visuals {offset}+ failed on {model}: "
            f"{type(e).__name__}: {str(e)[:180]}")
        # TELL providers.py. Nothing here ever did, so a heavy model that is
        # 503ing today stayed cached as the heavy model and every presentation
        # paid the same failed round trip before falling back — the log shows
        # both batches of a deck losing to the identical 503 inside one second,
        # and it would have done it again on the next deck, and the next.
        try:
            if providers.is_payment_required(e):
                # It wants a card. Never again this session, and say so — this
                # is the check that would have caught Groq drawing 26 figures
                # on a billable account.
                providers.mark_paywalled(provider, log=log)
            elif providers.is_daily_quota(e):
                # Out of quota for the DAY, not busy for a moment. Every
                # further attempt is a guaranteed-failing round trip, and a
                # five-figure deck fired five of them before trying anything
                # that could work. Take it out of circulation instead.
                providers.mark_exhausted(provider, model)
                log(f"[deck] {model} is out of quota for today — nothing will "
                    f"try it again until it resets")
            elif model_is_down(e):
                providers.report_failure(job, provider, model, log)
        except Exception:
            pass        # telling the cache is best-effort; the deck matters more
        return None
    why = _finish_reason(r)
    if why and why.upper() not in ("STOP", "FINISH_REASON_STOP"):
        log(f"[deck] visuals {offset}+ stopped early on {model}: {why}"
            + (" — the answer was cut off, so it will not parse"
               if "MAX_TOKEN" in why.upper() else ""))
    text = getattr(r, "text", "") or ""
    if not text.strip():
        log(f"[deck] visuals {offset}+ came back empty from {model}")
        return None
    # An answer with no JSON in it is a FAILURE, not a success. A refusal
    # ("I'm sorry, I can't help with creating medical diagrams") or a prose
    # preamble with no object counted as a win, so the second model was never
    # tried and the batch silently became plain text with no reason logged.
    # present() is aimed squarely at medical explanations, where refusals are
    # the common case.
    got = loads_visuals(text)
    if not got:
        log(f"[deck] visuals {offset}+ from {model} had no usable JSON "
            f"(first 90 chars: {text.strip()[:90]!r})")
        return None
    # THE QUALITY GATE. A visual that is valid but empty is worse than a
    # failure, because a failure gets retried and this used to go straight to
    # the screen. Treat it as a failure so the caller tries the other shape.
    solid = [g for g in got if not too_thin(safe_visual(g, log=None) or {})]
    if not solid:
        log(f"[deck] visuals {offset}+ from {model} came back too thin to "
            f"show ({[g.get('kind') for g in got]}) — trying another shape")
        return None
    want = len(plan.get("slides") or ())
    if want and len(got) < want:
        # Salvaged, not whole. Say so — a batch quietly returning two of three
        # is the difference between "the model is bad at this" and "the answer
        # was cut off", and only one of those is worth changing the prompt for.
        log(f"[deck] visuals {offset}+ recovered {len(got)}/{want} from a "
            f"partial answer on {model}")
    return text


def dress(plan, client, log=print, on_step=None):
    """The art call. Runs after the window is already up, so its latency is
    hidden behind Neo talking.

    Batched and parallel. The log showed `visuals failed (ServerError)` two
    seconds after every single request — one refusal from one model, and the
    entire feature degraded to plain text with no explanation. Now: small
    batches, two models to try, and every failure says what actually happened.
    """
    import providers
    slides = list(plan.get("slides") or ())
    if not slides:
        return fallback_visuals(plan), {}

    heavy = providers.resolve("heavy", client, log)
    chat = providers.resolve("chat", client, log)
    # CHAT FIRST for artwork, which is the opposite of what you'd expect and is
    # what the log actually says. On three separate decks the heavy model
    # (3.7-flash) returned 503, 503 and 504 while the chat model (2.5-flash)
    # answered every time — and because heavy went first it burned 17s, then
    # 44s, of a budget the working model never got to use.
    #
    # "Heavy" is a reasoning tier, and drawing an accurate figure is not a
    # reasoning problem: it is a long structured output, where finishing at all
    # beats thinking harder. Heavy stays as the second attempt, so a day when
    # it IS up still gets used.
    models = [(job, p, m) for job, (p, m) in (("chat", chat), ("heavy", heavy)) if m]
    # One more go at whichever model is left standing. The heavy model 503s
    # ("this model is currently experiencing high demand") often enough that
    # the log shows BOTH batches of a deck losing their artwork to it inside
    # the same second, and a 503 is by definition temporary — so a single
    # retry, on the model that is actually answering today, is the difference
    # between six drawn figures and six lines of plain text. The shared
    # ART_BUDGET_S still caps the whole phase, so this cannot run long.
    if models:
        models = models + [models[-1]]
    if not models:
        log("[deck] no model available for visuals — plain steps")
        return fallback_visuals(plan), {}

    cfg = None
    try:                      # an older SDK may not expose the config type
        from google.genai import types as _t
        cfg = _t.GenerateContentConfig(max_output_tokens=MAX_OUTPUT_TOKENS)
    except Exception:
        pass

    # The shape for every step, decided here rather than by the model. Each
    # call then carries only that shape's spec — 407 tokens instead of 4,029,
    # measured — which is both faster and five times less of a 20-a-day quota.
    shapes = plan_shapes(slides)
    log("[deck] shapes: " + ", ".join(p for p, _a in shapes))
    batches = [(i, slides[i:i + VISUALS_BATCH])
               for i in range(0, len(slides), VISUALS_BATCH)]
    answers, lock = {}, threading.Lock()
    deadline = time.time() + ART_BUDGET_S

    # A model that has already failed this build is not tried again for the
    # remaining figures. Measured on a real deck: six batches each spent 3-20
    # seconds failing on the same 429/503/504 before falling back, turning a
    # ~15s art phase into 51 seconds — long enough that the conversation timed
    # out before the deck was ready. One failure is enough evidence.
    dead = set()

    def run(offset, chunk):
        sub = {"title": plan.get("title", ""), "slides": chunk}
        primary, alternate = shapes[offset] if offset < len(shapes) \
            else ("process", "versus")
        for attempt, (job, provider, model) in enumerate(models):
            if model in dead:
                continue
            if attempt:
                time.sleep(0.6 * attempt)   # a 503 wants a beat, not a hammer
            if time.time() > deadline:
                return                      # the phase is over; don't start another
            # A second attempt asks for the SIMPLER shape. A figure that came
            # back unusable often did so because the shape was a stretch for
            # this step — retrying the identical request is the one thing
            # guaranteed not to help, and the old code's alternative was a line
            # of prose on a black screen.
            want = primary if attempt == 0 else alternate
            text = visuals_batch(sub, client, model, offset, log=log, cfg=cfg,
                                 provider=provider, job=job, kinds=[want])
            if not text:
                # Only quarantine when there is something else to fall back to,
                # or a single blip would leave the deck with no model at all.
                if len(models) - len(dead) > 1:
                    dead.add(model)
                continue
            if text:
                if attempt:
                    log(f"[deck] visuals {offset}+ came from {model} as "
                        f"{want} (attempt {attempt + 1})")
                with lock:
                    answers[offset] = text
                # LAND IT NOW. Every figure used to be held until all of them
                # were done, so one slow call meant the whole deck stayed as
                # plain text — and neo.log has a batch succeeding FOUR SECONDS
                # after the budget closed and being thrown away. Each figure
                # now appears on screen the moment it exists.
                if on_step:
                    try:
                        on_step(offset, text)
                    except Exception:
                        pass
                return

    workers = [threading.Thread(target=run, args=b, daemon=True)
               for b in batches]
    for w in workers:
        w.start()
    # ONE budget for all of them, not 45s each. Joining sequentially with a
    # per-thread timeout meant twelve slides could block for 45 x 4 = 180
    # seconds — Neo finishing the whole script and the window appearing
    # minutes later.
    for w in workers:
        w.join(timeout=max(0.1, deadline - time.time()))
    late = [w for w in workers if w.is_alive()]
    if late:
        log(f"[deck] {len(late)} visual batch(es) still running after "
            f"{ART_BUDGET_S:.0f}s — showing what arrived")

    if not answers:
        log("[deck] no visuals from any model — plain steps")
        return fallback_visuals(plan), {}

    # Fold each batch back onto the steps it actually describes.
    for offset, text in sorted(answers.items()):
        chunk = {"title": plan.get("title", ""),
                 "slides": slides[offset:offset + VISUALS_BATCH]}
        try:
            merge_visuals(chunk, text)
        except Exception as e:
            log(f"[deck] visuals {offset}+ wouldn't parse: "
                f"{type(e).__name__}: {str(e)[:140]}")

    drawn = sum(1 for s in slides if s.get("visual"))
    kinds = [(s.get("visual") or {}).get("kind") for s in slides if s.get("visual")]
    log(f"[deck] {drawn}/{len(slides)} steps got a real visual"
        + (f" ({', '.join(k for k in kinds if k)})" if kinds else ""))

    try:
        images = collect_images(plan, log=log, client=client)
    except Exception as e:
        log(f"[deck] pictures failed ({type(e).__name__}: {str(e)[:120]})")
        images = {}

    # A FIGURE WITHOUT ITS PICTURE MUST NOT ASK FOR ONE. _figure decides to
    # emit <image href="/img/N"> from whether the model wrote a SEARCH STRING,
    # not from whether that search found anything — so a failed fetch rendered
    # an empty frame with labels pointing into it. That is the blank slab with
    # "Spinal canal" and "Spinal cord" floating beside it.
    #
    # Dropping the field makes _figure draw its silhouette instead, which is a
    # worse figure and an honest one.
    stripped = 0
    for i, step in enumerate(plan.get("slides") or ()):
        v = step.get("visual") or {}
        if v.get("kind") == "figure" and v.get("image") and i not in images:
            v.pop("image", None)
            stripped += 1
    if stripped:
        log(f"[deck] {stripped} figure(s) had no picture — drawing them instead")
    return fallback_visuals(plan), images


def present(topic, client, log=print, context_note="", n=DEFAULT_SLIDES,
            on_ready=None):
    """Build the whole walkthrough on a worker; hand back the script only when
    it is ACTUALLY ON SCREEN.

    Nothing blocks here, and that is not a nicety. The plan call alone took
    nineteen seconds against a busy key, and this runs inside a LIVE TOOL
    DISPATCH — so the websocket sat with no traffic on it for nineteen seconds
    and the server hung up:

        21:19:33  [live] present
        21:19:52  could not return tool results: no close frame received
        21:19:52  session ended: APIError: 1006 abnormal closure

    The deck built perfectly and Neo never said a word, because by the time
    there was a script there was no session left to speak it.

    So: return in milliseconds, build everything behind Neo, and when the window
    is up with its figures in it and the tracker is sitting on slide one, call
    `on_ready(script)`. Neo then starts narrating a finished deck from the
    beginning, in step — which is what "show it when everything is done" means.
    """
    close()                                     # only ever one deck on screen
    global _build_gen
    with _lock:
        _build_gen += 1
        mine = _build_gen

    def _worker():
        global _prebuffer
        try:
            plan = build(topic, client, log=log, context_note=context_note, n=n)
        except Exception as e:
            log(f"[deck] plan failed: {type(e).__name__}: {str(e)[:140]}")
            plan = None
        if not plan:
            if on_ready:
                on_ready(None)
            return

        def _copy():
            return json.loads(json.dumps(plan))

        log(f"[deck] {plan['title']} — {len(plan['slides'])} slides, building art")

        # The window is built but NOT shown yet. the user asked for the finished
        # thing: no blank frames, no figures popping in while they listens.
        try:
            p = Presentation(fallback_visuals(_copy()), log=log)
            p.serve()
        except Exception as e:
            log(f"[deck] couldn't prepare the walkthrough: {e}")
            if on_ready:
                on_ready(None)
            return

        live = _copy()
        fallback_visuals(live)

        def _land(offset, text):
            try:
                chunk = {"title": live.get("title", ""),
                         "slides": live["slides"][offset:offset + VISUALS_BATCH]}
                merge_visuals(chunk, text)
                fallback_visuals(live)
            except Exception as e:
                log(f"[deck] couldn't fold in figure {offset}: "
                    f"{type(e).__name__}: {str(e)[:100]}")

        try:
            dressed, images = dress(_copy(), client, log=log, on_step=_land)
        except Exception as e:
            log(f"[deck] visuals stopped ({type(e).__name__}) — plain steps")
            dressed, images = fallback_visuals(_copy()), {}

        with _lock:
            stale = mine != _build_gen
        if stale:
            log("[deck] a newer presentation was asked for — dropping this one")
            p.close()
            return

        try:
            p.deck = dressed
            p.images = dict(images or {})
            p.tracker = Tracker(dressed.get("slides") or [],
                                min_dwell=getattr(p, "min_dwell", None))
            if not _install(p, mine):
                log("[deck] superseded before it opened — dropping it")
                p.close()
                return
            # Everything is ready. NOW show it.
            with _lock:
                _prebuffer = None      # nothing was narrated; there is nothing to catch up
            p.open_window()
            drawn = sum(1 for s in (dressed.get("slides") or ())
                        if (s.get("visual") or {}).get("kind") != "line")
            log(f"[deck] on screen, finished: "
                f"{drawn}/{len(dressed.get('slides') or ())} drawn, "
                f"{len(images)} picture(s)")
        except Exception as e:
            log(f"[deck] couldn't open the window: {e}")
            if on_ready:
                on_ready(None)
            return

        # The deck is up and waiting on slide one. Hand Neo their script.
        if on_ready:
            try:
                on_ready(narration_script(plan))
            except Exception as e:
                log(f"[deck] couldn't hand over the script: {e}")

    threading.Thread(target=_worker, daemon=True,
                     name="neo-deck-build").start()
    return {"building": True}


def _open_window(url):
    """Full-screen, borderless, dark. Its own process — a GUI run loop in Neo's
    process is a GUI run loop that can hang Neo."""
    import WebKit
    from Cocoa import (NSApplication, NSWindow, NSObject, NSMakeRect,
                       NSBackingStoreBuffered, NSScreen, NSColor,
                       NSWindowStyleMaskBorderless, NSFloatingWindowLevel)
    from Foundation import NSURL
    from PyObjCTools import AppHelper

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(0)

    class _D(NSObject):
        def applicationShouldTerminateAfterLastWindowClosed_(self, s):
            return True
        def windowWillClose_(self, n):
            NSApplication.sharedApplication().terminate_(None)

    deleg = _D.alloc().init()
    app.setDelegate_(deleg)

    frame = NSScreen.mainScreen().frame()
    win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        frame, NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False)
    win.setDelegate_(deleg)
    win.setLevel_(NSFloatingWindowLevel)
    win.setOpaque_(True)
    win.setBackgroundColor_(NSColor.blackColor())

    web = WebKit.WKWebView.alloc().initWithFrame_(
        NSMakeRect(0, 0, frame.size.width, frame.size.height))
    web.setAutoresizingMask_(18)
    try:
        web.setValue_forKey_(False, "drawsBackground")
    except Exception:
        pass
    win.setContentView_(web)
    web.loadRequest_(__import__("Foundation").NSURLRequest.requestWithURL_(
        NSURL.URLWithString_(url)))
    win.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)
    AppHelper.runEventLoop()


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "--window":
        try:
            _open_window(sys.argv[2])
        except Exception as e:
            print(f"[deck] native window failed ({e}); opening in a browser.")
            import subprocess
            subprocess.Popen(["open", sys.argv[2]])
    else:
        # demo: python deck.py — the renderer with no model and no network
        # involved, so you can see exactly what it does.
        demo = {"title": "How the walkthrough stays in sync", "slides": [
            {"narration": "Timing the steps drifts within thirty seconds.",
             "visual": {"kind": "number", "value": "30s",
                        "label": "before a timed walkthrough looks broken",
                        "sub": "the model never reads its script back verbatim"}},
            {"narration": "The live session already streams Neo's own words back.",
             "visual": {"kind": "flow", "nodes": [
                 {"id": "a", "label": "Neo speaks", "sub": "live audio",
                  "x": 18, "y": 28, "tone": "blue"},
                 {"id": "b", "label": "Transcript", "sub": "streamed back",
                  "x": 50, "y": 64, "tone": "green"},
                 {"id": "c", "label": "Figure moves", "sub": "on the cue",
                  "x": 82, "y": 28, "tone": "amber"}],
                 "edges": [{"from": "a", "to": "b", "flow": True},
                           {"from": "b", "to": "c", "label": "cue hit",
                            "flow": True}]}},
            {"narration": "Labels land on the word they belong to.",
             "visual": {"kind": "process", "steps": [
                 {"label": "Ask", "sub": "one held key"},
                 {"label": "Plan", "sub": "narration first"},
                 {"label": "Open", "sub": "Neo starts talking"},
                 {"label": "Dress", "sub": "figures arrive underneath"}]}}]}
        p = Presentation(demo)
        p.serve()
        print(f"http://127.0.0.1:{p.port}/")
        p.open_window()
        for i in range(len(demo["slides"])):
            time.sleep(3.5)
            p.push({"t": "slide", "i": i})
        time.sleep(30)
