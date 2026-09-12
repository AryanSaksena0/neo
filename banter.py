"""
banter.py — Neo's conversational reflexes: instant acks, time honesty,
greetings. The JARVIS layer.

Two latency truths drive this module:
  1. Real thinking (Gemini, web, DB) takes seconds. You can't cheat physics,
     but you CAN acknowledge instantly — every ack phrase here gets
     pre-synthesized to audio at warmup, so Neo's voice lands in
     milliseconds while the real work runs.
  2. Long jobs (lead hunts, Claude builds) need time honesty up front —
     say how long, then go. Nobody sits clueless.

Everything here is pure text logic — fully testable, no audio imports.
neo.py owns turning these into sound.
"""

import datetime
import os
import random

# --------------------------------------------------------------------------- #
# acks — grouped by how long the task really takes.
# Every phrase in ACKS gets cached as audio at warmup: keep them short,
# speakable, and don't churn them without reason (each one costs boot time).
# --------------------------------------------------------------------------- #
# WRITTEN TO BE HEARD, NOT READ. Two rules, both from the user listening to it:
#
# No abbreviations and no slurred forms. "One sec." comes out of a speech
# engine as something between "seck" and "sek" and reads as sloppy; "Let me
# check" gets run together into "lemme check". Everything here is spelled the
# way it should sound, in full words, so there is nothing for the synthesiser
# to elide.
#
# And the whole list has to be REACHABLE. Only the first few lines of each
# bucket are ever synthesised in Neo's real voice, so pick() choosing freely
# from twelve of them meant ten times out of twelve it fell through to the
# local voice — which is both the "wrong voice" and the "they only ever says one
# thing", from the same cause. pick() now chooses among the cached ones.
ACKS = {
    # a beat or two: a quick check, a fast lookup, reading the screen
    "quick": [
        "On it.",
        "One moment.",
        "Give me a second.",
        "Checking that now.",
        "Right, let me see.",
        "Hold on a moment.",
        "Let me pull that up.",
        "Of course, one moment.",
        "Looking at it now.",
        "Yes, give me a second.",
        "Just a moment.",
        "Let me find that for you.",
        "Sure. Two seconds.",
        "Let me have a look.",
        "Coming right up.",
        "Give me half a second.",
        "Let me sort that out.",
        "Right you are. One moment.",
        "Working on it.",
        "Let me get that.",
        "Bear with me a second.",
        "Almost nothing, hold on.",
        "Let me take a look at that.",
        "Sure thing. One moment.",
        "Give me a moment here.",
        "Let me handle that.",
        "Just checking something.",
        "Right, hold on.",
        "Let me see what I can find.",
        "One second and I will have it.",
    ],
    # ~10-40 seconds: research, drafting, anything with real work behind it
    "medium": [
        "On it. Call it half a minute.",
        "Give me about twenty seconds.",
        "Working on it. Roughly half a minute.",
        "This one will take a moment, so bear with me.",
        "Digging into that now. Give me a little.",
        "Of course. This one needs a minute of thinking.",
        "On it. Bear with me for a few seconds.",
        "Let me work through that. It will not be long.",
        "Right, that is a proper question. One moment.",
        "Give me twenty seconds and I will have it.",
        "Let me think about that properly.",
        "That deserves a real look. One moment.",
        "Working through it now. Not long.",
        "Let me pull this together. Half a minute.",
        "On it. Give me a moment to do it properly.",
    ],
    # a presentation: a plan call, then artwork. Several seconds, every time,
    # and the user is watching a blank screen for all of it. Say what's happening.
    "present": [
        "Give me a second. I am putting that together for you.",
        "Let me prepare that for you. One moment.",
        "Better seen than heard. Building it now.",
        "On it. A few seconds and it will be on your screen.",
        "Let me put together a quick walkthrough. One moment.",
        "I will show you this one. Give me a second.",
        "Worth a picture. Building it now, hang tight.",
        "Let me draw that out for you. Just a moment.",
        "Putting a walkthrough on screen. Two seconds.",
        "This one is easier shown. Making it now.",
        "Let me get this on screen for you.",
        "Give me a moment and you will see it.",
    ],
    "lookup": [
        "Let me check.",
        "Looking that up now.",
        "One moment, checking.",
        "Let me find out.",
        "Checking on that.",
        "Give me a second to look.",
        "Let me go and see.",
        "Pulling that up now.",
        "Looking into it.",
        "Let me go and check that.",
        "Give me a second, I will look.",
        "Let me look that up properly.",
        "Checking now.",
        "One moment while I look.",
        "Let me see what is out there.",
        "Off to check.",
        "Let me confirm that.",
        "I will go and find out.",
        "Hold on, let me look.",
        "Let me verify that before I answer.",
        "Give me a moment to check properly.",
        "Let me get you the real number.",
        "Checking that for you now.",
        "I will look rather than guess.",
        "Let me see what it actually says.",
    ],
    "hands": [
        "Doing that now.",
        "On it.",
        "Right, opening that.",
        "Having a look.",
        "Let me take a look.",
        "Getting to it.",
        "One moment.",
        "Opening it now.",
        "Let me get that open.",
        "Right, doing that.",
        "Let me sort that.",
        "Getting that for you.",
        "On it now.",
        "Let me take care of that.",
        "Give me a second to do that.",
    ],
    # Every one of these says roughly how long, on purpose. A long job with no
    # estimate leaves them refreshing the screen wondering whether it started.
    "long": [
        "Handing that to Claude now. It will be a few minutes.",
        "Starting that off. Give it a few minutes and I will tell you.",
        "That is a real job, so give it a couple of minutes.",
        "Passing it over. A few minutes, and I will come back to you.",
        "Setting that running. Go and do something else for a minute or two.",
        "On it. This one runs in the background for several minutes.",
        "That is a proper build. Give it a few minutes.",
        "Kicking that off now. A few minutes, then I will tell you.",
        "Running that in the background. Come back in a couple of minutes.",
        "This one takes minutes, not seconds. I will let you know.",
    ],
}


KIND_BUCKET = {
    "lookup": "quick", "screen": "quick", "refresh": "quick",
    "leads": "medium", "research": "medium", "draft": "medium",
    "plan": "medium", "carousel": "medium", "post": "medium",
    "visual": "medium",
    "claude": "long", "skill": "long",
    # tools map straight onto their own buckets
    "present": "present", "hands": "hands",
}

# The last line spoken from each bucket, so the next pick is never the same one
# twice running. Hearing the identical phrase back to back is the single
# clearest tell that you're talking to a machine.
_last = {}


# Fewer cached lines than this in a bucket and Neo has a catchphrase, not a
# personality. Borrow from a neighbouring bucket until there are enough.
# Five, not three: "lookup" sits in front of every single web search, so it is
# the bucket they hear most and the one where repetition shows first.
MIN_VARIETY = 5

# Which bucket a thin one may borrow from, in order. Everything can fall back
# on "quick": a short acknowledgement is never wrong, it is only ever less
# specific than it could have been.
NEIGHBOUR = {
    "lookup": ("quick",),
    "hands": ("quick",),
    "medium": ("quick",),
    "long": ("medium", "quick"),
    "present": ("medium", "quick"),
    "quick": (),
}


def pick(kind, rng=random, available=None):
    """An ack phrase suited to how long this kind of task takes.

    `available` is the set of phrases that actually exist in Neo's real voice.
    Choosing outside it is what made them sound like a different person half
    the time: only the first couple of lines per bucket are ever synthesised
    in the cloud voice, so picking freely from twelve of them fell through to
    the local voice ten times out of twelve. Same cause as "they only ever says
    one thing" — the two lines that WERE cached were the only two they heard in
    the right voice.

    Never returns the same phrase twice in a row for a bucket.
    """
    bucket = kind if kind in ACKS else KIND_BUCKET.get(kind, "quick")
    options = ACKS[bucket]
    if available:
        ready = [p for p in options if p in available]
        # A bucket with one cached line is not variety, it is a catchphrase —
        # and "lookup" was exactly that: one line, "Let me check.", in front of
        # every single web search. Cloud TTS is rate limited to a handful of
        # lines a day, so a thin bucket borrows from a compatible one rather
        # than repeating itself. A "quick" line fits a lookup perfectly.
        if len(ready) < MIN_VARIETY:
            for near in NEIGHBOUR.get(bucket, ()):
                ready += [p for p in ACKS.get(near, ()) if p in available
                          and p not in ready]
                if len(ready) >= MIN_VARIETY:
                    break
        if ready:
            options = ready
    if len(options) > 1:
        options = [p for p in options if p != _last.get(bucket)] or options
    choice = rng.choice(options)
    _last[bucket] = choice
    return choice


# How many lines per bucket are ever synthesised through the cloud voice.
#
# It was ALL of them — 53 — and the free TTS tier cannot do that: the log is a
# wall of `gemini tts failed: 429 RESOURCE_EXHAUSTED`, 50 of 53 lines never got
# made, and every one of those fell through to the local voice. Which is the
# "wrong voice" the user keeps hearing.
#
# Two per bucket was the fix for that, and it went too far the other way: two
# phrases is not variety, and pick() was still choosing from all twelve, so the
# other ten came out in the local voice anyway. The real fix is that pick()
# only ever chooses a phrase that IS cached — so this number is now simply how
# much variety Neo has, and it can grow. The cache is on disk and permanent, so
# a bucket fills in over a few days of ordinary use and never costs anything
# again. Nothing here is ever spoken in the wrong voice while it fills.
# A hard ceiling on how many lines are ever paid for. Set high on
# purpose: the cache is permanent, nothing uncached is ever spoken, and
# the user asked for enough variety that a phrase never comes round twice.
CLOUD_LINES = int(os.getenv("NEO_VOICE_LINES", "120"))


def all_phrases():
    """Every phrase worth spending cloud TTS quota on, best-first.

    ROUND ROBIN across buckets, and that ordering is the whole point. The
    allowance is about twenty lines a day, the list is over a hundred, and
    _synth_into works through it in order until the quota runs out. Taking
    each bucket in turn would have meant every "quick" line cached before a
    single "lookup" one — and lookup sits in front of every web search, so it
    would have stayed a catchphrase for days while an already-varied bucket
    got more variety it did not need.

    This way each bucket gains a line, then a second, then a third. Nothing
    outside the cache is ever spoken (see pick), so the list is free to be
    long and fill in over a week.
    """
    out, seen = [], set()
    lists = list(ACKS.values())
    for i in range(max(len(l) for l in lists) if lists else 0):
        for lines in lists:
            if i >= len(lines):
                continue
            p = lines[i]
            # "On it." belongs to more than one bucket. Listing it twice does
            # not synthesise it twice (the cache is checked first), but it does
            # make the list lie about how many distinct lines Neo has.
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out[:CLOUD_LINES]


def core_phrases(per_bucket=2):
    """The few that MUST be ready before Neo says it's ready.

    Synthesising all of them costs ~0.35s each, which turned a 15-second boot
    into a 34-second one — and Neo can't hear you until warmup finishes, so
    that is 19 seconds of the key doing nothing. Two per bucket is enough to
    always have something to say; the rest arrive a few seconds later while
    they're already able to talk.
    """
    out = []
    for lines in ACKS.values():
        out.extend(lines[:per_bucket])
    return out


def rest_phrases(per_bucket=2):
    """Everything core_phrases left behind, filled in on a background thread."""
    out = []
    for lines in ACKS.values():
        out.extend(lines[per_bucket:])
    return out


# --------------------------------------------------------------------------- #
# greetings — boot lines and welcome-backs. Time-aware, no Gemini cost.
# --------------------------------------------------------------------------- #
def _daypart(now):
    h = now.hour
    if h < 5:
        return "night"
    if h < 12:
        return "morning"
    if h < 17:
        return "afternoon"
    return "evening"


_BOOT = {
    "morning": ["Morning. Ears on, everything's green.",
                "Good morning. I'm up — hold fn whenever."],
    "afternoon": ["Afternoon. All systems up.",
                  "Back online. Ears on."],
    "evening": ["Evening. I'm up and listening.",
                "Good evening. Everything's green on my end."],
    "night": ["Up late again. I'm on — quietly judging, mostly listening.",
              "Night shift, huh. Ears on."],
}


def boot_line(now=None, rng=random):
    """What Neo says when it finishes waking up."""
    now = now or datetime.datetime.now()
    return rng.choice(_BOOT[_daypart(now)])


def gap_hint(last_ts, now=None):
    """
    Hidden-context hint for Gemini when a conversation resumes after a gap,
    so Neo greets like a person instead of answering cold. Returns '' when
    the conversation is already warm.
    """
    if last_ts is None:
        return ""
    now = now or datetime.datetime.now()
    hours = (now - last_ts).total_seconds() / 3600.0
    if hours < 5:
        return ""
    if hours < 16:
        return (f"[[note: first words in about {int(hours)} hours, "
                f"this {_daypart(now)} — greet them briefly and naturally first, "
                "then answer]] ")
    return ("[[note: first conversation since yesterday or longer — "
            "greet them properly, one warm line, then answer]] ")


# --------------------------------------------------------------------------- #
# progress — the "still working" line for genuinely long jobs
# --------------------------------------------------------------------------- #
def progress_line(task, minutes, rng=random):
    """Spoken mid-job check-in (Claude builds that run long)."""
    t = (task or "the job")[:60]
    lines = [
        f"Still on it — Claude's about {minutes} minutes into {t}.",
        f"Quick update: {t} is still cooking, {minutes} minutes in. No news is normal.",
        f"Claude's still grinding on {t}. {minutes} minutes so far — I'll tell you the second it lands.",
    ]
    return rng.choice(lines)
