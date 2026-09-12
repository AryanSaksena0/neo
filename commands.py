"""
commands.py — small, dependency-free helpers for Neo.

Kept separate from neo.py so they import cleanly without audio/ML deps (and so
they're easy to test). Covers:
  - clean_for_speech: make text sound right when spoken aloud
  - intent matchers: brain / memory-recap / forget
  - daily Gemini usage tracking (free tier is ~250 requests/day)
"""

import datetime
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
USAGE_PATH = os.path.join(HERE, "usage.json")
# Neo's own daily ceiling on brain calls. This was 250, copied from a free-tier
# table that no longer exists — Google stopped publishing per-model free limits
# and now allocates them per project. So the number was pure invention, and
# rationing against an invented number just made Neo refuse to think while real
# quota sat unused. It stays as a runaway guard (a loop that calls the API
# forever is a real failure mode) but set high enough that normal use never
# touches it, and overridable for anyone whose project limit really is lower.
DAILY_LIMIT = int(os.getenv("NEO_DAILY_LIMIT", "2000"))

# ---------------------------------------------------------------- speech ----
_URL = re.compile(r"https?://\S+|www\.\S+")
_CODE_BLOCK = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_EMOJI = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F000-\U0001F0FF"
    "\U00002190-\U000021FF"
    "\U00002B00-\U00002BFF"
    "\U0001F1E6-\U0001F1FF"
    "]",
    flags=re.UNICODE,
)


# Names the TTS mispronounces from spelling. NEO_NAME_SPOKEN in .env is a
# phonetic respelling of the user's own name, fed to the voice INSTEAD of the
# written form (never shown, only heard): NEO_NAME=Siobhan NEO_NAME_SPOKEN=Shiv-awn
_PRONOUNCE = {}
if os.getenv("NEO_NAME") and os.getenv("NEO_NAME_SPOKEN"):
    _PRONOUNCE[re.compile(r"\b" + re.escape(os.getenv("NEO_NAME")) + r"\b", re.I)] = \
        os.getenv("NEO_NAME_SPOKEN")


def clean_for_speech(text):
    """Strip things that sound wrong when read aloud (markdown, URLs, emoji, code)."""
    if not text:
        return ""
    # residual hidden tags ([[now:]], [[remember:]], [[show:]], [[visual:]], any
    # future ones) must NEVER be read aloud, wherever they leaked from
    t = re.sub(r"\[\[.*?\]\]", " ", text, flags=re.S)
    t = _CODE_BLOCK.sub(" ", t)
    # email addresses read aloud are pure gibberish — drop them (the person's
    # NAME is usually already in the sentence). Also kills "(x@y.com)" leftovers.
    t = re.sub(r"\(?\s*[\w.+-]+@[\w.-]+\.\w+\s*\)?", "", t)
    t = _URL.sub("a link", t)
    t = _INLINE_CODE.sub(r"\1", t)
    t = _EMOJI.sub("", t)
    t = _to_plain_english(t)               # symbols/paths/money -> spoken words
    # word_internal underscores -> spaces so 'auth_users' says "auth users",
    # not "authusers" (the old markdown-strip mangled these into gibberish)
    t = re.sub(r"(?<=\w)_(?=\w)", " ", t)
    t = re.sub(r"[*_#>`|~<^\\]", "", t)    # remaining markdown/code markers
    t = re.sub(r"\(\s*\)", "", t)          # empty parens left by removals
    for rx, spoken in _PRONOUNCE.items():  # say names RIGHT (spelling lies)
        t = rx.sub(spoken, t)
    t = _speech_pauses(t)                   # make Kokoro actually pause on prosody
    t = re.sub(r"\s+([,.!?])", r"\1", t)   # tidy spacing before punctuation
    t = re.sub(r"\s+", " ", t).strip()
    # give Kokoro a clean sentence boundary to land on so the last clause doesn't
    # trail into nothing (segmentation keys off end punctuation)
    if t and t[-1] not in ".!?":
        t += "."
    return t


_SCALE = {"k": "thousand", "m": "million", "b": "billion"}


def _to_plain_english(t):
    """Anything the voice would read as gibberish becomes English words or
    disappears. their rule: Neo talks in words, never symbols or code."""
    # file paths ("/Users/the user/Desktop/neo/db.py", "~/x/y") -> "a file"
    t = re.sub(r"(?:~|\.)?(?:/[\w.@-]+){2,}/?", " a file ", t)
    t = re.sub(r"(?:\ba file\b[ ,]*){2,}", "a file ", t)
    # hex / commit hashes are unpronounceable — drop (needs letters AND digits
    # so real numbers like 5000000 survive)
    t = re.sub(r"\b0x[0-9A-Fa-f]+\b", " ", t)
    t = re.sub(r"\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b", " ", t)
    # latin abbreviations -> the words people actually say
    t = re.sub(r"\be\.g\.,?\s*", "for example ", t, flags=re.I)
    t = re.sub(r"\bi\.e\.,?\s*", "that is ", t, flags=re.I)
    t = re.sub(r"\betc\.?(?=[\s,.!?]|$)", "and so on", t, flags=re.I)
    t = re.sub(r"\bvs\.?(?=\s)", "versus", t, flags=re.I)
    t = re.sub(r"\bapprox\.?\s", "about ", t, flags=re.I)
    # money: "$5k" -> "5 thousand dollars", "$5,000" -> "5,000 dollars"
    t = re.sub(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*([kKmMbB])\b",
               lambda m: f"{m.group(1)} {_SCALE[m.group(2).lower()]} dollars", t)
    t = re.sub(r"\$\s?(\d[\d,]*(?:\.\d+)?)", r"\1 dollars", t)
    # bare symbols -> words
    t = t.replace("%", " percent")
    t = re.sub(r"\s*&\s*", " and ", t)
    t = t.replace("°", " degrees ")
    t = re.sub(r"\+", " plus ", t)
    t = re.sub(r"\s*=+\s*", " equals ", t)
    t = re.sub(r"#(?=\d)", "number ", t)
    # leftover slashes ("24/7", "either/or") -> spaces (paths/URLs already gone)
    t = re.sub(r"\s*/\s*", " ", t)
    return t


def split_for_tts(text, limit=380):
    """Kokoro's prosody degrades past ~500 chars per call — long replies come
    out rushed and mumbly. Pack whole sentences into chunks under `limit` so
    every chunk gets clean pacing. Returns [] for empty text."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    parts = re.split(r"(?<=[.!?])\s+", text)
    chunks, cur = [], ""
    for p in parts:
        if cur and len(cur) + len(p) + 1 > limit:
            chunks.append(cur)
            cur = p
        else:
            cur = (cur + " " + p).strip() if cur else p
        while len(cur) > limit:          # one monster sentence: split at a pause
            cut = cur.rfind(", ", 0, limit)
            if cut < limit // 2:
                cut = cur.rfind(" ", 0, limit)
            if cut <= 0:
                cut = limit
            chunks.append(cur[:cut].strip())
            cur = cur[cut:].strip(" ,")
    if cur:
        chunks.append(cur)
    return chunks


def _speech_pauses(t):
    """Kokoro reads straight through em dashes, ellipses, semicolons and colons —
    so clauses that should breathe run together. Convert them to punctuation it
    actually pauses on. Word-internal hyphens ('voice-first') and clock times
    ('3:30') are left alone."""
    t = re.sub(r"\s*[—–]\s*", ", ", t)          # em/en dash -> comma pause
    t = re.sub(r"\s+-{1,2}\s+", ", ", t)         # spaced -/-- as a dash -> comma
    t = re.sub(r"\s*(?:\.{3,}|…)\s*", ", ", t)   # ellipsis -> soft pause
    t = re.sub(r"\s*;\s*", ", ", t)              # semicolon -> comma
    t = re.sub(r"(?<!\d)\s*:\s*(?!\d)", ", ", t) # colon (not 3:30) -> comma
    t = re.sub(r",\s*([,.!?])", r"\1", t)        # ", ." -> "." (drop doubled)
    t = re.sub(r"([.!?])\s*,", r"\1", t)         # ". ," -> "."
    return t


# Leading filler/address words Whisper hears before the actual command:
# "Um, Neo, open Safari." -> "open Safari." Case of the remainder is kept
# (proper-name detection needs it). If stripping eats everything, the
# original comes back untouched.
_ADDRESS_RE = re.compile(
    r"^(?:\s*(?:um+|uh+|erm+|hmm+|hey|hi|yo|ok|okay|alright|so|please|neo)(?:[,.!\s]+|$))+",
    re.IGNORECASE)


def strip_address(text):
    out = _ADDRESS_RE.sub("", text or "").strip()
    return out if out else (text or "").strip()


# ---------------------------------------------------------------- intents ---
def _norm(text):
    # drop apostrophes BEFORE stripping punctuation so "how's" -> "hows"
    # (matches how the phrase lists are written; Whisper emits apostrophes)
    return re.sub(r"[^a-z ]", " ", text.lower().replace("'", "").replace("’", ""))


def wants_brain(text):
    low = _norm(text)
    target = any(w in low for w in (" brain", " memory", " mind"))
    verb = any(w in low for w in ("show", "open", "see", "pull up", "display"))
    return target and verb


def is_computer_task(text):
    """A rich, multi-step computer/code/data GOAL that belongs to the reasoning
    brain (which hands it to Claude Code), NOT the literal 'open X' matcher.
    Guards against 'open terminal, get into the project, open the database' being
    reduced to just launching Terminal.app."""
    low = _norm(text)
    techy = any(w in low for w in (
        "database", "the code", "codebase", "repo", "repository", "the project",
        "the app", "app ", "server", "backend", "frontend", "sql", "query", "table",
        "signups", "sign ups", "the db", "terminal", "run the", "the script",
        "screen time", "screentime",
        # things the user asks Neo to BUILD — these are real Claude jobs, not chat.
        # Missing them is how "make me a screensaver" became a promise with no
        # job behind it.
        "screensaver", "screen saver", "wallpaper", "widget", "a website",
        "a web app", "webapp", "a program", "a script", "an app", "extension",
        "dashboard for", "landing page",
        "function", "the bug", "deploy", "migration", "endpoint", "api", "commit",
        "build", "compile", "the log", "stack trace", "console"))
    if not techy:
        return False
    # trouble words strongly imply a real dev task even without a classic verb
    trouble = any(w in low for w in (
        "why", "error", "broken", "failing", "not working", "crash", "500",
        "404", "bug", "throwing", "wont", "won't", "doesnt", "doesn't"))
    verbs = sum(1 for v in (
        "open", "get into", "go to", "run", "fix", "write", "check", "make",
        "show", "pull", "find", "look", "dig", "figure", "debug", "investigate",
        "explore", "diagnose", "trace", "inspect", "set up", "get", "jump into",
        "add", "change", "update", "refactor", "test") if v in low)
    return len(low.split()) >= 5 and (verbs >= 1 or trouble)


def wants_close(text):
    """'close the dashboard / that window / your brain' -> kill Neo's display
    windows. Checked BEFORE the open-matchers so 'close the dashboard' can
    never OPEN the thing it's asking to close."""
    low = _norm(text)
    if not any(v in low for v in ("close", "hide", "dismiss", "get rid of")):
        return False
    return any(t in low for t in (
        "dashboard", "window", "windows", "the brain", "your brain",
        "your memory", "the chart", "the visual", "the canvas", "the graphic"))


def is_personal_data(text):
    """HIS OWN accounts and data (calendar, email, drive, school portals) —
    never answerable by a web search. The brain reaches these with browser
    hands in their Chrome profile; a search returns generic help articles and
    a dead-end 'I can't access that' (the 16:46 calendar bug)."""
    low = _norm(text)
    return any(p in low for p in (
        "my calendar", "google calendar", "my gmail", "my email", "my inbox",
        "my mail", "my drive", "google drive", "my docs", "my schedule",
        "my schoology", "schoology", "my account", "my photos", "my classes",
        "my grades", "my assignments"))


# ---- music / media control (Spotify or Apple Music via hands.music_do) ------
# Keeps its digits (song titles have numbers), so it uses its OWN normalizer,
# not _norm. Conservative on the play-a-song branch so idioms ("play it cool",
# "play devils advocate", "play along") and the timer skill ("stop the timer")
# stay out of it.
_MUSIC_CTX = ("music", "song", "track", "spotify", "apple music", "itunes",
              "playlist", "album", "tune")


def _music_app(low):
    if "spotify" in low:
        return "spotify"
    if "apple music" in low or "itunes" in low:
        return "music"
    return None


def parse_music(text):
    """Control whatever's playing. Returns (action, query, app) or None.

    Actions: now | play | pause | toggle | next | previous | vol_up | vol_down.
    'play' carries a query for a specific song ('play feel no ways by drake');
    a bare 'play the music' resumes. app is 'spotify'/'music' if they named one,
    else None (hands picks the active player)."""
    low = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ",
                 text.lower().replace("'", "").replace("’", ""))).strip()
    if not low:
        return None
    app = _music_app(low)
    ctx = any(w in low for w in _MUSIC_CTX)

    # "what's playing / what song is this"
    if any(p in low for p in (
            "whats playing", "what is playing", "what song is this",
            "what is this song", "whats this song", "name of this song",
            "what song is playing", "current song", "song is this",
            "what are we listening to", "what am i listening to",
            "who sings this song", "who made this song")):
        return ("now", "", app)

    # next / previous (checked before play, so "play the next song" -> next)
    if any(p in low for p in ("next song", "next track", "skip this", "skip song",
            "skip the song", "next one", "skip this song", "play the next")) \
            or (ctx and "skip" in low.split()):
        return ("next", "", app)
    if any(p in low for p in ("previous song", "previous track", "last song",
            "go back a song", "back a track", "play the last", "restart the song",
            "start the song over")):
        return ("previous", "", app)

    # pause / stop — REQUIRES music context so "stop the timer" never hits this
    if ctx and any(p in low for p in ("pause", "stop", "hold on the")):
        return ("pause", "", app)

    # resume
    if any(p in low for p in ("resume", "unpause", "keep playing",
            "continue playing", "continue the music", "play it again")):
        return ("play", "", app)

    # volume — needs context so a bare "turn it up" stays chat
    if ctx and (bool(re.search(r"\bturn\b.*\bup\b", low))
                or any(p in low for p in ("louder", "volume up", "crank it", "pump it"))):
        return ("vol_up", "", app)
    if ctx and (bool(re.search(r"\bturn\b.*\bdown\b", low))
                or any(p in low for p in ("quieter", "softer", "volume down",
                                          "lower the"))):
        return ("vol_down", "", app)

    # play a song by name — the conservative branch. `search` (not match) so it
    # also catches a trailing "... and play my music"; \b keeps it off "display".
    m = re.search(r"\b(play|put on|throw on|start playing)\s+(.+)$", low)
    if m:
        obj = m.group(2)
        obj = re.sub(r"\b(for me|please|right now|again|some more)\b", " ", obj)
        obj = re.sub(r"\b(on|in|through|using|over)\s+(spotify|apple music|itunes|music)\b",
                     " ", obj)
        obj = re.sub(r"\bon my (music|spotify)\b", " ", obj)
        obj = re.sub(r"\s+", " ", obj).strip()
        generic = obj in ("", "music", "my music", "some music", "the music",
                          "something", "a song", "some songs", "my songs",
                          "my playlist", "my tunes", "tunes")
        if generic:
            # only when it's clearly about music (named player or a music word)
            return ("play", "", app) if (app or ctx) else None
        # real song request: needs a named player, a "by <artist>", or a music
        # noun — this is what keeps "play it cool" / "play along" as chat.
        # "play some <x>" / "put on some <x>" reads as music, not an idiom
        some = re.search(r"\b(play|put on|throw on|start playing) some\b", low)
        song_shape = (app is not None
                      or " by " in f" {obj} "
                      or bool(re.search(r"\b(song|track|playlist|album)\b", obj))
                      or m.group(1) in ("put on", "throw on")
                      or some is not None)
        if song_shape:
            q = re.sub(r"\b(song|track)\b", " ", obj).strip()
            q = re.sub(r"^some ", "", q)
            q = re.sub(r"\s+", " ", q).strip()
            return ("play", q or obj, app)
    return None


def wants_music(text):
    return parse_music(text) is not None


# ---- HUD gating: is this trivial enough to keep the working tab hidden? ------
# Greetings, small talk, and short single-fact questions (time, date, weather)
# should NEVER raise the working HUD — the user finds it distracting on simple
# stuff. Heavy/multi-step work is routed to its own branches and is unaffected;
# anything not flagged here still shows the HUD ONLY if it turns out to be slow
# (neo.py's latency gate). So this only needs to catch the OBVIOUSLY-simple.
_SMALL_TALK = (
    "how are you", "how you doing", "how are things", "hows it going",
    "how is it going", "how is everything", "whats up", "what is up", "sup",
    "good morning", "good afternoon", "good evening", "morning neo", "hey neo",
    "hi neo", "hello", "yo neo", "how was your day", "hows your day",
    "tell me a joke", "you good", "how do you feel", "whats good", "wassup",
    "thank you", "thanks", "youre the best", "good night", "goodnight")
_SIMPLE_FACT = (
    "what time", "whats the time", "what is the time", "time is it",
    "what day", "whats the date", "what is the date", "whats todays date",
    "what year", "whats the weather", "what is the weather", "hows the weather",
    "weather like", "weather today", "how hot", "how cold", "temperature outside")


def parse_offline_fact(text):
    """"what time is it" -> "time"; "what's the weather" -> "weather"; else None.

    These two are the only single-fact questions Neo can answer with NO model
    at all: the clock is the machine's own, and Open-Meteo needs no key and no
    account. They matter out of all proportion to their size, because they are
    what a person asks first to find out whether the thing they just installed
    is real. In local mode the model is not there to route them, so they are
    routed here instead.

    Deliberately narrow. "What time is the meeting" is a calendar question and
    must NOT be answered with the wall clock, so a match needs one of the plain
    time phrasings and nothing that points at an event.
    """
    low = re.sub(r"\s+", " ", _norm(text)).strip()
    if not low:
        return None
    # anything pointing at an event, a place in the future, or another zone is
    # not the wall clock / local weather question
    if any(w in low for w in (" meeting", " appointment", " class", " flight",
                              " game", " tomorrow", " yesterday", " on monday",
                              " on tuesday", " on wednesday", " on thursday",
                              " on friday", " on saturday", " on sunday",
                              " next week", " this weekend", " in london",
                              " in paris", " in tokyo", " in new york")):
        return None
    if any(p in low for p in ("what time", "whats the time", "what is the time",
                              "time is it", "got the time", "have the time")):
        return "time"
    if any(p in low for p in ("whats the weather", "what is the weather",
                              "hows the weather", "how is the weather",
                              "weather like", "weather today", "weather outside",
                              "how hot is it", "how cold is it",
                              "temperature outside", "is it raining",
                              "is it going to rain")):
        return "weather"
    return None


def is_simple_request(text):
    """True for trivial/conversational asks that shouldn't pop the working HUD."""
    low = re.sub(r"\s+", " ", _norm(text)).strip()
    if not low:
        return True
    if any(p in low for p in _SMALL_TALK):
        return True
    if any(low.startswith(p) or (" " + p + " ") in (" " + low + " ")
           for p in _SIMPLE_FACT):
        return True
    return False


def wants_lookup(text):
    low = _norm(text)
    if is_personal_data(low):
        return False   # their data lives behind logins, not on the open web
    return any(p in low for p in (
        "look up", "look it up", "search for", "search the web",
        "google it", "google the", "google for",
        "the latest", "latest on", "look into", "find out about",
        "happening with", "new with", "search up")) \
        or low.startswith(("search ", "google "))


def wants_radar(text):
    """'Anything on my radar?' — pending sentinel insights, spoken on demand."""
    low = " " + _norm(text).strip() + " "
    return any(p in low for p in (
        "on my radar", "on the radar", "anything for me", "any updates for me",
        "what did i miss", "did i miss anything", "waiting on me",
        "anything i should know", "anything i need to know", "whats pending",
        "what is pending", "any nudges"))


def wants_tidy_memory(text):
    """'clean up your memory' -> consolidation pass (generative-agents reflection)."""
    low = _norm(text)
    if "memory" not in low and "memories" not in low:
        return False
    return any(v in low for v in ("clean up", "cleanup", "tidy", "consolidate",
                                  "organize", "organise", "declutter"))


def wants_speed(text):
    """'how fast are you' -> spoken latency report from real logged turns."""
    low = _norm(text)
    return any(p in low for p in (
        "how fast are you", "how quick are you", "speed report", "latency report",
        "whats your latency", "what is your latency", "how slow are you"))


def wants_repeat(text):
    """'say that again' — replay Neo's last reply (top repair request in VA research)."""
    low = " " + _norm(text).strip() + " "
    return any(p in low for p in (
        " say that again ", " say it again ", " what did you say ", " what was that ",
        " repeat that ", " come again ", " didnt catch that ", " didnt hear you ",
        " one more time "))


def wants_key_setup(text):
    """'add my key' / 'set up my key' — the way out of local mode.

    Neo runs with no API key at all: the fixed phrases, timers, reminders,
    the calendar, markets, the weather and everything that talks to macOS all
    work on an install that has never seen a key. What it cannot do is THINK.
    So local mode has to offer a route rather than dead-end the person, and
    this is that route — said out loud, no terminal, no file to edit.
    """
    low = " " + _norm(text).strip() + " "
    if not any(w in low for w in (" key ", " keys ", " api ", " smarter ",
                                  " smart ", " brain ", " think ", " thinking ")):
        return False
    return any(p in low for p in (
        " add my key ", " add a key ", " add the key ", " add my api key ",
        " add an api key ", " set up my key ", " set up a key ", " setup my key ",
        " enter my key ", " put in my key ", " give you my key ", " give you a key ",
        " i have a key ", " ive got a key ", " use my key ", " connect my key ",
        " get me a key ", " get a key ", " make me smarter ", " make you smarter ",
        " be smarter ", " get smarter ", " turn on your brain ",
        " make yourself smarter ", " you smarter ", " smarter version ",
        " why cant you think ", " why can you not think ", " learn to think "))


def wants_visual(text):
    """"visualize X" / "draw me X" / "make a diagram of X" -> freeform visual."""
    low = _norm(text)
    return any(p in low for p in (
        "visualize", "visualise", "make a visual", "make a diagram", "make a graphic",
        "make a chart", "draw me", "draw a", "draw the", "draw up",
        "show me a diagram", "show me a visual", "show me a chart", "show me a graphic",
        "show this visually", "show me visually", "diagram of", "as a visual", "as a diagram"))


def wants_recap(text):
    low = " " + _norm(text) + " "
    return any(p in low for p in (
        "what do you know about me", "what do you remember", "what you know about me",
        "whats on your mind", "what is on your mind", "remember about me",
        "what do you know me",
    ))


def wants_forget(text):
    low = _norm(text).strip()
    return (low.startswith("forget that")
            or low.startswith("forget what")
            or low.startswith("forget the last")
            or low in ("forget it", "scratch that", "never mind that"))


# ---------------------------------------------------------------- usage -----
def _today():
    return datetime.date.today().isoformat()


def bump_usage():
    """Increment today's Gemini call count and return it."""
    u = {}
    try:
        with open(USAGE_PATH) as f:
            u = json.load(f)
    except (OSError, json.JSONDecodeError):
        u = {}
    if u.get("date") != _today():
        u = {"date": _today(), "count": 0}
    u["count"] = int(u.get("count", 0)) + 1
    try:
        with open(USAGE_PATH, "w") as f:
            json.dump(u, f)
    except OSError:
        pass
    return u["count"]


def remaining_today():
    try:
        with open(USAGE_PATH) as f:
            u = json.load(f)
        if u.get("date") == _today():
            return max(0, DAILY_LIMIT - int(u.get("count", 0)))
    except (OSError, json.JSONDecodeError):
        pass
    return DAILY_LIMIT


# Thresholds (replies LEFT) at which Neo warns the user, largest first. Each fires
# once a day — we remember which have gone off in the usage file so a warning
# isn't repeated on every turn once you're under the line.
USAGE_WARN_AT = (50, 20, 5)


# --------------------------------------------------------------- thinking ---
# How hard the brain should think about this turn.
#
# The old code set thinking_budget=0 globally to save a couple of seconds, which
# meant a reasoning model ran with reasoning switched off on every request. That
# is a fine trade for "what time is it" and completely wrong for "work out why
# the deploy failed" — and it is the single line most responsible for Neo
# feeling like it can't do anything.
#
# Budgets are Gemini's units: 0 disables thinking, -1 lets the model decide, and
# a positive number is a ceiling.
#
# These values are MEASURED, on the real key, on one hard scheduling question:
#
#     budget 0     545 ms     budget 1024   3888 ms
#     budget 512  2619 ms     budget 2048   6974 ms     dynamic  9298 ms
#
# Thinking bought latency and nothing else. All five runs disagreed with each
# other AND all five got the question wrong — so on this model, for this kind of
# question, paying seven seconds of silence buys no correctness at all.
#
# That flips the obvious policy. A voice assistant's budget ceiling isn't about
# how hard the question is, it's about how long a person will sit in silence.
# Genuinely hard work doesn't get more thinking here; it gets ESCALATED — to the
# heavy route or to Claude Code — where waiting is understood and the answer is
# actually better. Reasoning is on where it helps and capped where it doesn't.
THINK_OFF = 0
THINK_DYNAMIC = -1      # uncapped: escalation paths only, never a spoken turn
THINK_LIGHT = 512       # ~2.6s, the most silence a conversation tolerates
THINK_TYPED = 2048      # a typed turn: nobody is waiting in silence, so think

# The most thinking any SPOKEN turn may ask for. Nothing in the voice path is
# allowed past this, however hard the question looks.
THINK_SPOKEN_MAX = THINK_LIGHT


def thinking_budget_for(text, has_job=False, pushback=None, simple=None,
                        actionable=None):
    """Pick a thinking budget for one SPOKEN turn.

    Small talk and single-fact lookups get none — latency is the whole product
    there and there is nothing to reason about. A correction ('no, that didn't
    work') gets the light budget, because the failure mode there is confidently
    repeating the same wrong thing and a couple of seconds is tolerable when
    they're already annoyed. Everything else gets none, and if it's genuinely hard
    the ladder escalates it rather than stalling mid-conversation.
    """
    if pushback is None:
        pushback = is_pushback(text)
    if pushback:
        return THINK_LIGHT
    if has_job:
        return THINK_LIGHT
    if simple is None:
        simple = is_simple_request(text)
    if simple:
        return THINK_OFF
    if actionable is None:
        actionable = is_actionable(text)
    if actionable:
        return THINK_LIGHT
    return THINK_OFF


def cap_spoken_budget(budget, ceiling=None):
    """Clamp a budget to what a spoken turn may spend. -1 (uncapped) is the
    dangerous one: measured at 9.3 seconds of silence."""
    ceiling = THINK_SPOKEN_MAX if ceiling is None else ceiling
    if budget is None:
        return None
    if budget < 0:
        return ceiling
    return min(budget, ceiling)


def usage_warning():
    """If today's remaining just crossed a warning threshold, return a short
    spoken heads-up (once per threshold per day); else ''. Free tier is capped,
    so the user wants to know before they hits the wall, not after."""
    left = remaining_today()
    tripped = [t for t in USAGE_WARN_AT if left <= t]
    if not tripped:
        return ""
    lowest = min(tripped)          # the tightest threshold now crossed
    try:
        with open(USAGE_PATH) as f:
            u = json.load(f)
    except (OSError, json.JSONDecodeError):
        u = {}
    if u.get("date") != _today():
        return ""                  # bump_usage resets the day; nothing to warn yet
    if int(u.get("warned", 0)) and int(u["warned"]) <= lowest:
        return ""                  # already warned at this level or tighter
    u["warned"] = lowest
    try:
        with open(USAGE_PATH, "w") as f:
            json.dump(u, f)
    except OSError:
        pass
    if left <= 0:
        return "Heads up — that's my free thinking used up for today."
    return f"Heads up — {left} replies left on the free plan today."


def is_quota_error(exc):
    msg = str(exc).lower()
    return any(s in msg for s in ("resource_exhausted", "429", "quota", "rate limit", "rate_limit"))


def is_transient_error(exc):
    msg = str(exc).lower()
    return any(s in msg for s in ("503", "unavailable", "overloaded", "deadline", "timeout", "500", "internal"))


# ------------------------------------------------------- capability ladder ---
# The brain must never dead-end a real request with "I can't" — the prompt
# says so, but prompts aren't enforcement. These two power the SAFETY NET in
# neo.py: a can't-answer to an actionable ask auto-escalates to Claude Code.

_CANT_IDIOMS = ("cant believe", "cant wait", "cant argue", "cant complain",
                "cant go wrong", "cant blame", "cant beat", "cant hurt")

_CANT_SIGNS = (
    "i cant", "i cannot", "im not able", "i am not able", "im unable",
    "i am unable", "cant quite", "not something i can", "beyond my",
    "no way for me", "i dont have a way", "i dont have access",
    "i dont have the ability", "wish i could", "cant see your",
    "cant do that", "cant reach", "cant access", "cant open",
    "cant get to", "outside my", "cant help with", "isnt showing me",
    "isnt something i can", "not able to see")


def sounds_like_cant(reply):
    """Did the brain just dead-end? (Harmless idioms like "can't beat that"
    are scrubbed first so banter doesn't trigger the ladder.)"""
    low = re.sub(r"[^a-z ]", "", (reply or "").lower().replace("'", ""))
    for p in _CANT_IDIOMS:
        low = low.replace(p, "")
    return any(p in low for p in _CANT_SIGNS)


# A reply that PROMISES work later. Said while no job is actually running, this
# is the worst failure Neo can produce: the user waits for something that will never
# arrive (the screensaver bug). Confident phrasing made it slip past every guard,
# so it now gets its own check.
_PROMISE_SIGNS = (
    "ill let you know", "ill tell you when", "ill ping you", "ill come get you",
    "as soon as its ready", "when its ready", "once its ready", "when its done",
    "once its done", "as soon as its done",
    "this will take me", "itll take me", "it will take me", "give me a few minutes",
    "give me a couple minutes", "in a few minutes", "in a couple minutes",
    "im working on it", "im on it now", "ill get started", "ill start working",
    "ill have it ready", "ill get that made", "ill get that built",
    "ill build", "ill create", "ill make you", "ill put together", "ill get to work",
    "working on that now", "getting started on",
)


# the user telling Neo it DIDN'T work. They are the one looking at the screen, so this
# is ground truth — not a debate. The failure this guards: Neo claimed a
# screensaver was up, the user said "I don't see anything", and Neo replied about
# the DATE being wrong instead of checking whether anything was on screen at all.
_PUSHBACK_SIGNS = (
    "i dont see", "i cant see", "dont see anything", "cant see anything",
    "nothing happened", "nothing is there", "nothing there", "its not there",
    "it isnt there", "not on my screen", "nothing on my screen",
    "that didnt work", "didnt work", "doesnt work", "not working",
    "you didnt", "you did not", "it didnt", "nothing opened", "nothing showed",
    "i dont hear", "cant hear anything", "i dont hear anything",
    "still nothing", "nothing changed", "thats not right", "thats wrong",
    "youre wrong", "no it isnt", "no its not", "im the user",
)


# ----------------------------------------------------------- live mode ------
# Phrases that open the real-time conversation (live.py) and phrases that close
# it. Deliberately narrow on the way IN — "let's talk about the essay" is a
# normal request and must not open a socket — and deliberately generous on the
# way OUT, because being unable to hang up on a live mic is the worst bug this
# feature could have.
_LIVE_ON = (
    "conversation mode", "conversational mode", "live mode", "talk mode",
    "let's have a conversation", "lets have a conversation",
    "let's talk properly", "lets talk properly",
    "start a conversation", "let's chat", "lets chat",
    "talk to me normally", "have a real conversation",
)
_LIVE_OFF = (
    "that's all", "thats all", "that is all", "hang up", "we're done",
    "were done", "we are done", "end the conversation", "end conversation",
    "stop talking", "exit conversation", "leave conversation",
    "back to normal", "normal mode", "goodbye neo", "bye neo",
)


def wants_live(text):
    """Open the real-time conversation."""
    t = (text or "").lower()
    return any(p in t for p in _LIVE_ON)


def wants_live_off(text):
    """Close it. Also matched inside a live session on the transcript."""
    t = (text or "").lower().strip(" .,!?")
    return any(p in t for p in _LIVE_OFF)


def is_pushback(text):
    """True when the user says the last thing Neo claimed didn't actually happen.
    Pure so it's testable."""
    low = re.sub(r"[^a-z ]", " ", (text or "").lower().replace("'", ""))
    low = " ".join(low.split())
    return any(p in low for p in _PUSHBACK_SIGNS)


def promises_future_work(reply):
    """True if the reply promises a deliverable LATER instead of producing it.
    Only meaningful when no job is actually running — the caller checks that.
    Pure so it's testable."""
    low = re.sub(r"[^a-z ]", "", (reply or "").lower().replace("'", ""))
    low = " ".join(low.split())
    return any(p in low for p in _PROMISE_SIGNS)


def needs_goal_check(reply):
    """Whether the paid goal-lock verify (an extra Gemini round-trip before the
    answer is even spoken) is worth running. A long, confident answer that
    doesn't hedge, refuse, or bounce the question back almost always delivered —
    trust it and skip the call. Short/hedgy replies still get verified. Pure."""
    r = (reply or "").strip()
    if len(r) < 60:
        return True                 # terse -> could be a deflection
    if r.endswith("?"):
        return True                 # bounced the question back instead of acting
    if sounds_like_cant(r):
        return True                 # hedgy -> verify (dead_end may have missed it)
    if promises_future_work(r):
        return True                 # "I'll let you know" is never a delivered answer
    return False


def is_actionable(text):
    """Does the request ask Neo to DO or FETCH something concrete (vs chat)?
    Deliberately broad: over-escalating to Claude beats dead-ending."""
    low = _norm(text)
    if len(low.split()) < 3:
        return False
    if re.match(r"(how (?:much|many|long)|whats my|what is my|whens |when is|"
                r"where is|where are)\b", low):
        return True
    # a request verb ANYWHERE is too loose ("do you ever GET tired") — the
    # verb must lead the command, optionally after politeness filler
    lead = re.sub(r"^(?:please |can you |could you |would you |will you |"
                  r"neo |just |now )+", "", low)
    verb_leads = any(lead.startswith(v) for v in (
        "go ", "open", "show", "get ", "find", "check", "make", "take",
        "pull", "turn", "set ", "run ", "fix ", "send", "close", "play",
        "look", "dig ", "figure", "put ", "bring", "search", "read ",
        "write", "build", "add ", "delete", "move", "change", "update",
        "install", "download", "start", "stop "))
    # "...and also tell me the workaround" — these are specific enough anywhere
    return verb_leads or any(p in low for p in ("give me", "tell me the", "tell me my"))


# -------------------------------------------------------------- task label ---
_HEDGES = re.compile(
    r"\b(or whatever|or something|basically|you know|kind of|kinda|"
    r"real quick|honestly)\b[,.]?\s*", re.I)
_LEAD_FILLER = re.compile(
    r"^(?:(?:hey|so|um+|uh+|ok(?:ay)?|neo|claude|please|just|now|then|"
    r"go (?:into|to) (?:the )?terminal|open (?:the )?terminal|"
    r"i want you to|i need you to|i want to|i want|i need|id like|"
    r"can you|could you|would you|will you)\b[,.\s]*)+", re.I)


def task_label(text, max_len=70):
    """The raw voice transcript makes an ugly job title ('I want you to go
    into Terminal or whatever Basically, I want my grad'). Distill it: drop
    hedges and lead-in filler, then truncate at a WORD boundary. The full
    transcript still goes to Claude — this is only what the user sees/hears."""
    t = re.sub(r"\s+", " ", (text or "")).strip()
    t = _HEDGES.sub("", t)
    t = _LEAD_FILLER.sub("", t)
    t = re.sub(r"\s+", " ", t).strip(" ,.;:-")
    if not t:   # stripped everything ("open terminal") -> keep the original
        t = re.sub(r"\s+", " ", (text or "")).strip()
    if len(t) > max_len:
        cut = t.rfind(" ", 0, max_len)
        if cut < max_len // 2:
            cut = max_len
        t = t[:cut].rstrip(" ,.;:-") + "…"
    return (t[:1].upper() + t[1:]) if t else "the job"


# The card title is not the transcript. task_label() only trims and truncates,
# which on a long spoken request produces exactly what the user saw:
#
#   "Claude — Run draft simulations for a six-person, point-per-reception (PPR)…"
#
# — two lines of card, cut mid-phrase, and still not telling them which job it
# is. What they asked for is the REQUEST IN FIVE OR SIX WORDS: "Six person
# fantasy football draft".
_TITLE_PROMPT = (
    "Condense this spoken request into a card title of FOUR OR FIVE "
    "WORDS. Five is the ceiling, not the target.\n\n"
    "Name the thing, not the action. No leading verb, no 'Claude', no quotes, "
    "no full stop, no ellipsis. Capitalise like a headline. If it names a "
    "specific subject, that subject must be in the title.\n\n"
    "Example: a long request about simulating draft picks for a six-team "
    "point-per-reception fantasy league becomes: Six person fantasy draft"
    "\n\nRequest: {text}\n\nTitle:")


def short_title(text, client=None, model=None, max_words=6, log=print):
    """A card title of five or six words. Falls back to task_label().

    The model call is worth it here and nowhere near the critical path: this
    labels a job that is about to run for minutes, so under a second to make
    it readable is free. Any failure falls straight back to the old trim.
    """
    plain = task_label(text, 70)
    if client is None or not (text or "").strip():
        return plain
    try:
        r = client.models.generate_content(
            model=model or "gemini-3.5-flash-lite",
            contents=_TITLE_PROMPT.format(text=str(text)[:1500]))
        got = (getattr(r, "text", "") or "").strip()
    except Exception as e:
        log(f"[title] couldn't shorten it: {type(e).__name__}")
        return plain
    got = got.strip().strip('"\'').rstrip(".…").strip()
    got = re.sub(r"\s+", " ", got)
    if not got or len(got.split()) > max_words or len(got) > 40:
        # A model that ignored the instruction is not better than the trim.
        return plain
    return got[:1].upper() + got[1:]


# Deep-work mode. They say it in a dozen ways and none of them are "enable
# focus mode", so the matcher takes the natural ones.
_QUIET_ON = re.compile(
    r"\b(focus mode|deep work|do not disturb|don'?t disturb|"
    r"(i'?m|i am) (?:working|studying|busy|locked in|in the zone)|"
    r"leave me (?:alone|be)|stay out of my way|don'?t touch (?:my|the) (?:screen|"
    r"computer|mouse)|quiet mode|no distractions)\b", re.I)
_QUIET_OFF = re.compile(
    r"\b((?:focus|quiet|deep work) mode off|turn off (?:focus|quiet|deep work)"
    r"|(?:i'?m|i am) done (?:working|studying)?|out of (?:focus|the zone)"
    r"|stop focus mode|you can (?:move|use) (?:my )?(?:mouse|screen)"
    r"|go ahead)\b", re.I)


def wants_quiet(text):
    """'I'm working, stay out of my way' -> True. 'focus mode off' -> False.
    None when they said neither. Pure."""
    low = _norm(text or "")
    if _QUIET_OFF.search(low):
        return False
    if _QUIET_ON.search(low):
        return True
    return None


# ------------------------------------------------------------ voice picker ---
# Spoken name -> Kokoro voice id. First letter of the id picks the accent
# pipeline ('a' American, 'b' British).
VOICES = {
    "fable": "bm_fable", "george": "bm_george", "lewis": "bm_lewis",
    "emma": "bf_emma", "isabella": "bf_isabella",
    "heart": "af_heart", "bella": "af_bella", "nova": "af_nova",
    "nicole": "af_nicole", "sarah": "af_sarah", "sky": "af_sky",
    "alloy": "af_alloy", "jessica": "af_jessica",
    "michael": "am_michael", "eric": "am_eric", "adam": "am_adam",
    "echo": "am_echo", "liam": "am_liam", "onyx": "am_onyx",
}


def parse_voice_switch(text):
    """'switch your voice to fable' -> 'fable'; 'change the voice' -> ''
    (asked, but no name — Neo lists options); None when not a voice request.
    'voice memo'/'voicemail' never trigger."""
    low = _norm(text)
    m = re.search(r"\b(?:switch|change|swap|set)\s+(?:your |the |neos )?"
                  r"voice(?!\s*(?:memo|mail))(?:\s+(?:to|into)\s+(\w+))?", low)
    if m:
        return (m.group(1) or "").strip()
    m = re.search(r"\buse\s+the\s+(\w+)\s+voice\b", low)
    if m:
        return m.group(1)
    return None
