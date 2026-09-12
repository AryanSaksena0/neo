"""
selfwatch.py — Neo reads its own log and notices when it is broken.

WHY THIS EXISTS
Every fault Neo had in the first week of September was found the same way:
the user noticed something wrong, said so, and someone read neo.log afterwards.
The tap that went deaf for eighty minutes, the two questions swallowed by a
device reset, the mode that undid itself five seconds after being set — all of
them wrote a clear line into the log at the moment they happened, and nothing
was reading it.

    04 Sep  two voices at once
    06 Sep  the fn tap said "enabled" for 80 minutes while delivering nothing
    08 Sep  a microphone opened on the thread feeding the speaker
    08 Sep  _terminate() ran while the mic was open, and two questions vanished
    08 Sep  one "bedtime mode" left Neo whispering on every boot

Neo has a screen, a card system and a bridge to Claude Code. It has never
pointed any of it at itself. This is the smallest thing that changes that.

WHAT IT IS NOT
It does not fix anything, and it does not talk. It raises a silent card, the
same channel the sentinel already uses for "you have three unanswered emails".
Deciding what to do about a fault is still their, and handing one to Claude
is still a sentence they say out loud. An assistant that edits its own source
while nobody is watching is a different and much larger proposal.

EVERY SIGNATURE CAME FROM A REAL FAULT
Nothing here is hypothetical. Each pattern below is a line that has actually
appeared in this repo's neo.log, with the date it first showed up. That is the
bar for adding another: it has to have happened.
"""

import os
import re
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "neo.log")

# How much of the tail to read on each pass. The log is append-only and this
# runs every POLL_S, so a window this size only matters after a long stall.
TAIL_BYTES = 200_000
POLL_S = float(os.getenv("NEO_SELFWATCH_POLL", "90"))
# The same fault must not raise a card twice in a morning.
REPEAT_S = float(os.getenv("NEO_SELFWATCH_REPEAT", "10800"))   # 3 hours


class Sig:
    """One known-bad thing, and what it means in their terms.

    `count` is how many times it has to appear in the window before it counts —
    some faults are only faults when they repeat. A single lost question is
    life; two in a row is the microphone not working.
    """

    def __init__(self, key, pattern, title, detail, count=1, urgency="medium"):
        self.key = key
        self.rx = re.compile(pattern)
        self.title = title
        self.detail = detail
        self.count = count
        self.urgency = urgency


SIGNATURES = [
    # ---- 6 Sept: the tap reported itself enabled and delivered nothing ----
    # Matches both wordings: the original "has delivered nothing" and the
    # count-based rewrite ("N key events went past it"). A signature that only
    # matches the message you happen to have today is a signature that goes
    # quiet the next time someone improves the wording — which is exactly what
    # happened between writing this file and testing it.
    Sig("tap-deaf", r"fn tap says it is enabled but",
        "The fn key went dead and Neo rebuilt it",
        "Sleep leaves the key listener alive but deaf. Neo caught it and "
        "rebuilt it — but if this keeps happening the rebuild is treating a "
        "symptom.", urgency="high"),
    Sig("tap-disabled", r"WATCHDOG: the fn event tap was disabled",
        "macOS switched the fn key listener off",
        "Usually a slow callback or a burst of input. Harmless once; a "
        "pattern means something is blocking the main thread."),

    # ---- the original freeze this whole watchdog family exists for ----
    Sig("stuck-listening", r"WATCHDOG: stuck on 'listening'",
        "A hold ended without Neo noticing",
        "The key-up was lost and the recorder was left running. Recovered "
        "automatically, but each one is a swallowed question.", count=2),
    Sig("runaway-turn", r"WATCHDOG: a turn has been running (\d+)s",
        "A turn ran far longer than it should have",
        "Something in the pipeline hung. While it does, every press does "
        "nothing.", urgency="high"),

    # ---- 8 Sept: the device reset that ate two questions ----
    Sig("nothing-said", r"let go, but nothing was said",
        "Neo heard nothing on two holds in a row",
        "The microphone opened but captured silence. Either the wrong input "
        "device, or a stream that was invalidated underneath it.",
        count=2, urgency="high"),
    Sig("speaker-open", r"speaker wouldn't open|could not open audio",
        "The speaker wouldn't open",
        "PortAudio refused the output device. Neo retries, but a repeat means "
        "the audio path is genuinely wrong.", urgency="high"),

    # ---- things that silently degrade rather than fail ----
    Sig("tts-dry", r"the day's TTS allowance is gone",
        "The cloud voice ran out for the day",
        "Neo falls back to the local voice. Adding GEMINI_API_KEY_3 is a "
        "third full allowance and takes a minute.", urgency="low"),
    Sig("mail-dead", r"credentials rejected 3x|AUTHENTICATIONFAILED",
        "Mail is switched off — the password is dead",
        "NEO_GMAIL_APP_PASSWORD no longer works, so Neo cannot read the "
        "inbox at all. It stopped retrying rather than spamming the log."),
    Sig("skill-skipped", r"\[skills\] skipped (\S+)",
        "A skill failed to load",
        "It is on disk but broken, so Neo cannot use it. 'Repair the X skill' "
        "hands it to Claude."),
    Sig("unsupervised", r"launchd is NOT supervising this process",
        "Neo can't restart itself right now",
        "The LaunchAgent isn't loaded, so a code change would leave Neo dead "
        "instead of reloading it. ./make_app.sh --start puts it back.",
        urgency="high"),
    Sig("boot-stalled", r"STARTUP STALLED",
        "Neo took far too long to start",
        "A model load or download hung. Neo could not hear anything until it "
        "cleared.", urgency="high"),

    # ---- a highlight that refused to draw is a promise not kept ----
    Sig("point-refused", r"\[point\] wanted .*not showing it",
        "Neo declined to point at something it wasn't sure about",
        "The verifier caught a ring on the wrong control and dropped it. "
        "Correct behaviour, but it means the walkthrough could not finish.",
        count=2, urgency="low"),

    # ---- anything that actually raised ----
    Sig("traceback", r"^Traceback \(most recent call last\)",
        "Something raised an unhandled exception",
        "A traceback reached the log, which means a code path failed in a way "
        "nobody planned for.", urgency="high"),
]


def _norm(line):
    """Drop the timestamp so the same fault matches whenever it happened."""
    return re.sub(r"^\[\d\d:\d\d:\d\d\]\s*", "", line)


def scan(lines, signatures=None):
    """Which known faults appear in these lines. Pure, so the whole judgement
    is testable against real log excerpts instead of argued about.

    Returns [{key, title, detail, urgency, hits, sample}], worst first.
    """
    signatures = SIGNATURES if signatures is None else signatures
    stripped = drop_tap_noise([_norm(l) for l in lines])
    order = {"high": 0, "medium": 1, "low": 2}
    found = []
    for sig in signatures:
        hits = [l for l in stripped if sig.rx.search(l)]
        if len(hits) < sig.count:
            continue
        found.append({
            "key": sig.key,
            "title": sig.title,
            "detail": sig.detail,
            "urgency": sig.urgency,
            "hits": len(hits),
            "sample": hits[-1][:160],
        })
    found.sort(key=lambda f: (order.get(f["urgency"], 1), -f["hits"]))
    return found


def drop_tap_noise(lines, window=4):
    """A double-press of fn toggles stealth. In voice mode each tap of the
    pair opens a conversation and closes it having heard nothing, which
    writes "let go, but nothing was said" twice — the exact shape of the
    deaf-mic signature. Those lines sit within a few lines BEFORE a
    "[stealth] on/off" line; drop them, and only them. Pure."""
    out = list(lines)
    for i, l in enumerate(lines):
        if "[stealth] on" in l or "[stealth] off" in l:
            for j in range(max(0, i - window), i):
                if "nothing was said" in out[j]:
                    out[j] = ""
    return [l for l in out if l]


def read_tail(path=None, nbytes=TAIL_BYTES):
    """The last chunk of the log as lines. Never raises."""
    path = LOG_PATH if path is None else path
    try:
        size = os.path.getsize(path)
        with open(path, "r", errors="replace") as f:
            if size > nbytes:
                f.seek(size - nbytes)
                f.readline()          # drop the half line the seek landed in
            return f.read().splitlines()
    except OSError:
        return []


def due(key, seen, now=None, repeat_s=REPEAT_S):
    """Has this fault been quiet long enough to be worth raising again? Pure.

    Never seen before is always due — otherwise the very first occurrence of a
    fault is silently swallowed on any machine whose clock is younger than the
    repeat window, which is every machine for the first three hours.
    """
    if key not in seen:
        return True
    now = time.time() if now is None else now
    return (now - seen[key]) >= repeat_s


class SelfWatch:
    """Reads the log on a slow timer and raises silent cards.

    Deliberately dumb and deliberately quiet. It never speaks, never edits
    anything, and never looks at a window older than the current run.
    """

    def __init__(self, notify, log=print, path=None, poll=POLL_S,
                 since_start=True):
        self._notify = notify
        self._log = log
        self._path = LOG_PATH if path is None else path
        self._poll = poll
        self._seen = {}
        self._thread = None
        # ONLY THIS RUN'S LOG, from here on.
        #
        # The first version scanned the last 200 KB, which on its very first
        # boot meant reporting four days of history — including 134 rebuilds
        # caused by a bug that had been fixed an hour earlier. A fault watcher
        # that reports faults you already fixed is a fault watcher you learn to
        # ignore. Remembering where the log was when Neo started means every
        # card is about something happening NOW.
        self._offset = 0
        if since_start:
            try:
                self._offset = os.path.getsize(self._path)
            except OSError:
                self._offset = 0

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="neo-selfwatch")
        self._thread.start()

    def _loop(self):
        # Let the boot settle. Every start writes warnings that are not faults,
        # and raising a card about Neo's own warm-up would be its first act.
        time.sleep(min(self._poll, 45))
        while True:
            try:
                self.check()
            except Exception as e:
                self._log(f"[selfwatch] {type(e).__name__}: {e}")
            time.sleep(self._poll)

    def _new_lines(self):
        """Everything written since the last pass. A truncated or rotated log
        resets the offset rather than reading garbage."""
        try:
            size = os.path.getsize(self._path)
        except OSError:
            return []
        if size < self._offset:
            self._offset = 0            # the log was rotated or cleared
        if size == self._offset:
            return []
        try:
            with open(self._path, "r", errors="replace") as f:
                f.seek(self._offset)
                text = f.read()
            self._offset = size
            return text.splitlines()
        except OSError:
            return []

    def check(self, now=None):
        """One pass. Returns what it raised, for the tests."""
        now = time.time() if now is None else now
        raised = []
        for finding in scan(self._new_lines()):
            if not due(finding["key"], self._seen, now):
                continue
            self._seen[finding["key"]] = now
            self._log(f"[selfwatch] {finding['key']}: {finding['title']} "
                      f"(x{finding['hits']})")
            try:
                self._notify({
                    "key": f"selfwatch-{finding['key']}",
                    "kind": "warn",
                    "urgency": finding["urgency"],
                    "title": finding["title"],
                    "detail": finding["detail"],
                })
            except Exception as e:
                self._log(f"[selfwatch] couldn't raise a card: {e}")
            raised.append(finding)
        return raised


# --------------------------------------------------------------------------- #
# What went wrong lately — the spoken answer
# --------------------------------------------------------------------------- #
# What "lately" means when they ask out loud. Smaller than TAIL_BYTES on
# purpose: the card watcher reports what is happening NOW, and this answers
# "how have you been today" — neither should be reciting last week.
SPOKEN_WINDOW_BYTES = 60_000


def summary(lines=None, limit=4):
    """One or two plain sentences for "what's been going wrong?". Pure given
    lines, so the phrasing is testable."""
    findings = scan(read_tail(nbytes=SPOKEN_WINDOW_BYTES)
                    if lines is None else lines)
    if not findings:
        return "Nothing's gone wrong that I've noticed."
    worst = findings[:limit]
    parts = [f["title"][0].lower() + f["title"][1:] for f in worst]
    if len(parts) == 1:
        return f"One thing: {parts[0]}."
    return (f"{len(findings)} things. The main ones: "
            + "; ".join(parts[:limit]) + ".")
