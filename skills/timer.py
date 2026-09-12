"""Skill: timer — "set a timer for 10 minutes" -> big on-screen countdown
window + Neo speaks when time's up. Supports stop ("stop the timer") and
status ("how much time is left?").

Hardened by live use: repeating the duration in one sentence must not sum
("10 minutes... I mean 10 minutes" is 10, not 20), mentioning the word timer
in a complaint must not START one, and stop/status must actually work."""

import os
import re
import subprocess
import sys
import tempfile
import time

NAME = "timer"
DESCRIPTION = ("Countdown timers: on-screen clock + spoken time's-up. Triggers: "
               "'set a timer for 10 minutes', 'stop the timer', 'how much time is left'.")

# starting needs an ACTION VERB — "I wanted the timer..." is a complaint, not a command
_ACTION = re.compile(
    r"\b(?:set|start|make|create|put|open|run|give|begin|do)\b.{0,40}?\b(?:timer|countdown)\b"
    r"|^\s*(?:a |an )?(?:timer|countdown)\b.{0,20}\bfor\b", re.I | re.S)
# "Take the timer down" and "get rid of it" are how people actually cancel
# things, and neither contained a word this knew. handle() then fell through to
# "How long?", so asking Neo to REMOVE a timer got a request for its duration —
# which is what the user heard as "it's still asking for the time duration".
_STOP = re.compile(
    r"\b(?:stop|cancel|kill|end|clear|remove|delete|dismiss|close|scrap|ditch)\b"
    r".{0,24}\b(?:timer|countdown|it|that|this)\b"
    r"|\bget\s+rid\s+of\b|\btake\s+(?:it|the\s+\w+)\s+down\b"
    r"|\btimer\b.{0,12}\b(?:off|away|gone)\b", re.I)
# Pause and resume did not exist at all. "Pause the timer very quickly" fell
# straight through matches() and the skill declined, which the model reported
# as "I can't create or control it properly".
_PAUSE = re.compile(r"\b(?:pause|hold|freeze|suspend|halt)\b.{0,24}"
                    r"\b(?:timer|countdown|it|that)\b", re.I)
_RESUME = re.compile(r"\b(?:resume|unpause|un-pause|continue|restart|"
                     r"start\s+again|keep\s+going)\b.{0,24}"
                     r"\b(?:timer|countdown|it|that)\b", re.I)
_LEFT = re.compile(r"\b(?:how (?:much(?: time)?|long)|time)\b.{0,20}\bleft\b", re.I)

# SPELLED-OUT NUMBERS, and a hyphen counts as a space.
#
# Two failures in one line. "1-hour timer" did not parse, because the pattern
# allowed only whitespace between the number and the unit. And "ten minutes"
# did not parse either — there were no word-numbers at all — while the skill's
# OWN error message told the user to say 'set a timer for ten minutes'. It asked
# for a phrase it could not read.
_NUMWORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "twentyfive": 25, "thirty": 30, "forty": 40, "fortyfive": 45,
    "fifty": 50, "sixty": 60, "ninety": 90,
    "a": 1, "an": 1, "half a": 0.5, "half an": 0.5, "quarter": 0.25,
    "quarter of an": 0.25, "a quarter of an": 0.25,
}
_DUR = re.compile(
    r"(\d+(?:\.\d+)?|quarter of an|a quarter of an|half an?|quarter|"
    + "|".join(sorted((w for w in _NUMWORDS if w.isalpha() and len(w) > 1),
                      key=len, reverse=True))
    + r"|a|an)"
    r"[\s\-\u2010-\u2015]*"          # "1-hour", "ten-minute", "1 hour"
    r"(hour|hr|minute|min|second|sec)s?\b", re.I)
_WORDS = _NUMWORDS
_UNIT_S = {"hour": 3600, "hr": 3600, "minute": 60, "min": 60, "second": 1, "sec": 1}

_ACTIVE = []      # [{"end": ts, "timer": threading.Timer, "proc": Popen|None, "label": str}]
_PAUSED = []      # [{"left": seconds, "label": str}] — stopped, not forgotten


def parse_duration(text):
    """Seconds from '10 minutes' / '1 hour 30 minutes'. FIRST mention per unit
    only — a repeated '10 minutes' is emphasis, not addition."""
    seen, total = set(), 0.0
    for qty, unit in _DUR.findall(text):
        u = _UNIT_S[unit.lower()]
        if u in seen:
            continue
        seen.add(u)
        key = re.sub(r"[\s\-]+", " ", qty.lower()).strip()
        n = _WORDS.get(key, _WORDS.get(key.replace(" ", "")))
        try:
            total += (float(qty) if n is None else n) * u
        except ValueError:
            continue        # a word we do not know: not a duration
    return int(total) if total > 0 else None


def _spoken(seconds):
    """A duration the way a person says it.

    This only knew EXACT hours and EXACT minutes and fell back to raw seconds
    for everything else, which was fine while durations were only ever typed in
    round numbers. Pausing produces arbitrary ones, and a paused hour reported
    itself as "3599 seconds left" — technically true, and nothing a person
    would ever say.
    """
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    mins = int(round(seconds / 60.0))          # past a minute, seconds are noise
    h, m = divmod(mins, 60)
    if h and m:
        return (f"{h} hour{'s' if h != 1 else ''} "
                f"{m} minute{'s' if m != 1 else ''}")
    if h:
        return f"{h} hour{'s' if h != 1 else ''}"
    return f"{m} minute{'s' if m != 1 else ''}"


def _prune():
    global _ACTIVE
    _ACTIVE = [t for t in _ACTIVE if t["end"] > time.time()]


def matches(text):
    low = text.lower()
    has_word = "timer" in low or "countdown" in low
    # A timer is RUNNING, so "pause it" or "get rid of it" mean this skill even
    # without the word "timer" in them. That is how anyone talks about a thing
    # already on their screen.
    if (_ACTIVE or _PAUSED) and (_PAUSE.search(low) or _RESUME.search(low)
                                 or _STOP.search(low)):
        return True
    if not has_word and not _LEFT.search(low):
        return False
    if (_PAUSE.search(low) or _RESUME.search(low)) and has_word:
        return True
    if _STOP.search(low) and has_word:
        return True
    if _LEFT.search(low):
        return bool(_ACTIVE) or "timer" in low or "countdown" in low
    return bool(_ACTION.search(low)) and parse_duration(low) is not None


def _countdown_html(seconds, label):
    """Self-contained countdown page in Neo's dark aesthetic (JS ticks locally)."""
    return f"""
<div class="nt-wrap"><style>
  .nt-wrap{{display:flex;flex-direction:column;align-items:center;justify-content:center;
    min-height:70vh;gap:18px;}}
  .nt-time{{font-size:clamp(40px,24vw,120px);font-weight:700;letter-spacing:-2px;
    color:#e8ecf4;font-variant-numeric:tabular-nums;}}
  .nt-label{{color:#8b95a8;font-size:15px;letter-spacing:2px;text-transform:uppercase;}}
  .nt-bar{{width:min(560px,80%);height:10px;border-radius:999px;background:rgba(255,255,255,0.07);
    overflow:hidden;}}
  .nt-fill{{height:100%;border-radius:999px;background:linear-gradient(90deg,#5fd0aa,#a9c7ff);
    box-shadow:0 0 14px rgba(95,208,170,0.5);transition:width 1s linear;}}
  .nt-done .nt-time{{color:#5fd0aa;animation:ntpulse 1s ease infinite alternate;}}
  @keyframes ntpulse{{from{{opacity:1}}to{{opacity:0.45}}}}
</style>
<div class="nt-label">{label}</div>
<div class="nt-time" id="nt-time">--:--</div>
<div class="nt-bar"><div class="nt-fill" id="nt-fill" style="width:100%"></div></div>
<script>
  var total={seconds}, end=Date.now()+total*1000;
  function fmt(s){{var h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=Math.floor(s%60);
    return (h?h+":":"")+(h?String(m).padStart(2,"0"):m)+":"+String(x).padStart(2,"0");}}
  function tick(){{var left=Math.max(0,Math.round((end-Date.now())/1000));
    document.getElementById("nt-time").textContent=left>0?fmt(left):"DONE";
    document.getElementById("nt-fill").style.width=(left/total*100)+"%";
    if(left<=0){{document.querySelector(".nt-wrap").classList.add("nt-done");return;}}
    setTimeout(tick,250);}}
  tick();
</script></div>"""


def _stop_all():
    _prune()
    if not _ACTIVE:
        return "No timer running."
    n = len(_ACTIVE)
    for t in _ACTIVE:
        try:
            if t["timer"] is not None:
                t["timer"].cancel()
        except Exception:
            pass
        try:
            if t["proc"] is not None:
                t["proc"].terminate()
        except Exception:
            pass
    _ACTIVE.clear()
    return ("Timer stopped." if n == 1
            else f"All {n} timers stopped. There were {n} running, by the way.")


def _pause():
    """Stop the countdown and REMEMBER what was left.

    The on-screen window ticks on its own clock, so it cannot be frozen from
    here — it is closed, and rebuilt on resume with the remaining time. From
    their side that is a pause; leaving the window counting while claiming it
    was stopped would be a lie they can see.
    """
    global _PAUSED
    _prune()
    if not _ACTIVE:
        return ("It's already paused." if _PAUSED
                else "Nothing's running to pause.")
    _PAUSED = [{"left": max(1, int(t["end"] - time.time())), "label": t["label"]}
               for t in _ACTIVE]
    _stop_all()
    return (f"Paused with {_spoken(_PAUSED[0]['left'])} left. "
            f"Say resume when you want it back.")


def _resume(ctx):
    global _PAUSED
    if not _PAUSED:
        return "Nothing's paused."
    secs = _PAUSED[0]["left"]
    _PAUSED = []
    _start(secs, ctx)
    return f"Back on, with {_spoken(secs)} left."


def _time_left():
    _prune()
    if not _ACTIVE:
        return "Nothing's counting down right now."
    t = min(_ACTIVE, key=lambda x: x["end"])
    left = int(t["end"] - time.time())
    m, s = divmod(max(0, left), 60)
    if m and s:
        return f"{m} minute{'s' if m != 1 else ''} {s} seconds left."
    if m:
        return f"{m} minute{'s' if m != 1 else ''} left."
    return f"{s} seconds left."


def handle(text, ctx):
    low = text.lower()
    if _STOP.search(low):
        return _stop_all()
    if _PAUSE.search(low):
        return _pause()
    if _RESUME.search(low):
        return _resume(ctx)
    if _LEFT.search(low):
        return _time_left()

    secs = parse_duration(low)
    if not secs:
        # SAY WHICH THING FAILED. Every unparsed sentence used to come back as
        # "how long?", so asking Neo to REMOVE a timer was answered with a
        # request for its duration — "it's still asking for the time duration",
        # in their words. If something is already running, the request was
        # not about starting one.
        _prune()
        if _ACTIVE or _PAUSED:
            return ("I couldn't tell what you wanted done with the timer. "
                    "You can pause it, resume it, cancel it, or ask how long "
                    "is left.")
        return "How long? Say something like: set a timer for ten minutes."
    if secs > 12 * 3600:
        return "That's more than twelve hours. That's not a timer, that's a calendar."
    return _start(secs, ctx)


def _start(secs, ctx):
    """Put one timer on screen and arm the spoken alarm.

    Extracted from handle() so resume can reuse it — a resumed timer has to be
    a real timer, with its own window and its own alarm, not a message saying
    it is running.
    """
    global _PAUSED
    _prune()
    replaced = ""
    if _ACTIVE:                       # one timer at a time — replace, don't stack
        _stop_all()
        replaced = "Replaced the old one. "
    _PAUSED = []                      # starting fresh clears any paused one
    label = f"{_spoken(secs)} timer"
    proc = None
    try:
        import canvas
        page = canvas.render_page(_countdown_html(secs, label))
        fd, path = tempfile.mkstemp(suffix=".html", prefix="neo_timer_")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(page)
        proc = subprocess.Popen([
            sys.executable,
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "canvas.py"), "--window", path,
            "--corner", "340x220"])   # compact, TOP-RIGHT, floats, no focus-steal
        on_screen = True
    except Exception as e:
        print(f"[timer] window failed: {e}")
        on_screen = False
    tm = ctx.later(secs, f"Time's up. Your {_spoken(secs)} are done.")
    _ACTIVE.append({"end": time.time() + secs, "timer": tm, "proc": proc,
                    "label": label})
    where = "It's on your screen, and I'll" if on_screen else "I'll"
    return f"{replaced}{_spoken(secs).capitalize()}, starting now. {where} tell you when it's done."


def self_test():
    ok = parse_duration("set a timer for 10 minutes") == 600
    ok = ok and parse_duration("timer for 90 seconds") == 90
    ok = ok and parse_duration("an hour timer") == 3600
    ok = ok and parse_duration("1 hour 30 minutes") == 5400
    # the live bug: repeated duration is emphasis, not addition
    ok = ok and parse_duration(
        "a timer for 10 minutes, i'm studying for 10 minutes") == 600
    ok = ok and parse_duration("no numbers here") is None
    ok = ok and matches("set a timer for 10 minutes")
    ok = ok and matches("put a 10 minute countdown on my screen")
    ok = ok and matches("stop the timer")
    ok = ok and matches("how much time is left on the timer")
    # complaints/questions must NOT start timers
    ok = ok and not matches("i wanted the timer for 10 minutes, did i not say 10 minutes?")
    ok = ok and not matches("what time is it")
    ok = ok and not matches("set a timer")            # no duration -> chat clarifies

    # ---- the four bugs from 2026-08-28, each checked -------------------- #
    # 1. "Can you start a 1-hour timer on my screen?" was answered with
    #    "I just need to know how long" — a hyphen between the number and the
    #    unit was not allowed.
    ok = ok and parse_duration("a 1-hour timer on my screen") == 3600
    ok = ok and matches("Can you start a 1-hour timer on my screen?")
    # 2. The skill's own error told them to say "set a timer for ten minutes",
    #    a phrase it could not itself parse. There were no word-numbers.
    ok = ok and parse_duration("set a timer for ten minutes") == 600
    ok = ok and parse_duration("timer for five minutes") == 300
    ok = ok and parse_duration("quarter of an hour timer") == 900
    # 3. "Pause the timer very quickly" fell through and the skill declined.
    ok = ok and matches("Pause the timer very quickly")
    ok = ok and matches("resume the timer")
    # 4. "We'll just take the timer down. Get rid of it." was answered by
    #    asking for a duration.
    ok = ok and matches("We'll just take the timer down. Get rid of it.")
    ok = ok and _STOP.search("we'll just take the timer down. get rid of it.")

    # stop with nothing running is honest
    _ACTIVE.clear()
    _PAUSED.clear()
    ok = ok and _stop_all() == "No timer running."
    ok = ok and "Nothing" in _time_left()
    ok = ok and "Nothing" in _pause()
    ok = ok and _resume(None) == "Nothing's paused."
    # a paused timer reports as paused, not as missing
    _PAUSED.append({"left": 300, "label": "x"})
    ok = ok and "already paused" in _pause()
    _PAUSED.clear()

    # An unrecognised sentence while a timer runs must NOT ask for a duration.
    _ACTIVE.append({"end": time.time() + 600, "timer": None, "proc": None,
                    "label": "x"})
    try:
        reply = handle("do the thing with the timer", None)
        ok = ok and "How long" not in reply and "cancel it" in reply
    finally:
        _ACTIVE.clear()
    return ok and "600" in _countdown_html(600, "x")


if __name__ == "__main__":
    print("self_test:", self_test())
