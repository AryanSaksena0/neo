"""
selfrepair.py — Neo notices it is broken and files the bug itself.

their list, verbatim: "it cant self report bugs to claude code". Until now the
only way a defect got fixed was them noticing it, remembering it, and telling
someone. Every failure Neo hit in between evaporated into an apology.

    something fails  ->  gather the EVIDENCE (log tail, tool, what they asked)
                     ->  hand it to Claude Code in the neo folder
                     ->  Claude diagnoses, fixes, and runs the suites

Three things keep this from being a nuisance:

**Evidence, not vibes.** A report says which tool failed, what the user had asked
for, and carries the tail of neo.log around the failure. "Something went wrong"
is not a bug report and Claude cannot act on it.

**Deduplicated.** The same fault hit five times in an evening is one report, not
five. The signature is the tool plus the shape of the error, and a filed bug
stays filed for a day.

**Never in front of them.** Filing is silent and runs in the background. They asked
for a thing; hearing about Neo's internals instead is worse than the bug.
"""

import datetime
import hashlib
import json
import os
import re
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "neo.log")
FILED_PATH = os.path.join(HERE, "filed_bugs.json")
QUIET_H = float(os.getenv("NEO_BUG_QUIET_H", "24"))   # same bug, once a day
LOG_LINES = 60


def signature(tool, problem):
    """A stable key for 'this same fault again'.

    Numbers, paths and quoted strings are stripped first, so the same bug with
    a different filename in the message is still the same bug.
    """
    # ORDER MATTERS. Replacing digits first turns 2026_ppr.md into #_ppr.md,
    # and "#" is not a path character — so the path pattern then matched only
    # part of it and two reports of the same fault got different signatures.
    # Paths and quoted strings go first, digits last.
    shape = f"{tool}|{problem}".lower()
    shape = re.sub(r"/[\w./#-]+", "/p", shape)
    shape = re.sub(r"['\"`][^'\"`]*['\"`]", "'x'", shape)
    shape = re.sub(r"\d+", "#", shape)
    shape = re.sub(r"\s+", " ", shape).strip()[:200]
    return hashlib.sha1(shape.encode("utf-8")).hexdigest()[:16]


def _load():
    try:
        with open(FILED_PATH, encoding="utf-8") as f:
            got = json.load(f)
        return got if isinstance(got, dict) else {}
    except Exception:
        return {}


def already_filed(sig, now=None, quiet_h=QUIET_H, filed=None):
    """Have we reported this recently? Pure when `filed` is passed."""
    now = time.time() if now is None else now
    filed = _load() if filed is None else filed
    when = filed.get(sig)
    try:
        return when is not None and (now - float(when)) < quiet_h * 3600
    except (TypeError, ValueError):
        return False


def mark_filed(sig, now=None):
    filed = _load()
    filed[sig] = time.time() if now is None else now
    # Keep it small: anything older than a week has nothing left to suppress.
    cutoff = (time.time() if now is None else now) - 7 * 86400
    filed = {k: v for k, v in filed.items()
             if isinstance(v, (int, float)) and v > cutoff}
    try:
        with open(FILED_PATH, "w", encoding="utf-8") as f:
            json.dump(filed, f)
    except OSError as e:
        print(f"[selfrepair] couldn't record the report: {e}")


def log_tail(lines=LOG_LINES, path=None):
    """The end of neo.log — the single most useful thing in a bug report."""
    path = path or LOG_PATH
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-lines:]
    except OSError:
        return ""
    # Warnings from torch and friends are noise that would crowd out the part
    # that matters.
    keep = [l for l in tail
            if not re.search(r"UserWarning|FutureWarning|warnings\.warn|"
                             r"resource_tracker|weight_norm|HF Hub", l)]
    return "".join(keep)[-6000:]


def brief(tool, problem, asked_for="", extra=""):
    """The engineering brief handed to Claude Code. Evidence first."""
    when = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""A DEFECT IN NEO ITSELF, reported by Neo at {when}. You are in the
neo folder. Fix the cause, do not paper over the symptom.

WHAT FAILED
  tool     : {tool or "(not a tool — something internal)"}
  problem  : {problem}
  the user had asked for: {asked_for or "(not recorded)"}
{("  also: " + extra) if extra else ""}

THE TAIL OF neo.log AROUND IT
```
{log_tail()}
```

HOW TO WORK THIS
- Read CLAUDE.md first. It records what has already been tried and which
  mistakes cost real time; several of them look exactly like a good idea.
- Reproduce it before you change anything. If you cannot reproduce it, say so
  and stop rather than guessing — a speculative fix to a voice assistant is
  worse than a known bug.
- Fix the CAUSE. This same defect has been reported once already if you are
  seeing it again, so a patch that only silences the message will bring it
  straight back.
- Add a check to the matching suite that FAILS before your fix and passes
  after. Every test in this repo was written from a real defect; make yours
  the same.
- Then run: python test_neo.py, python test_routing.py, and whichever suite
  covers the file you touched. All must pass.
- Report back in ONE sentence saying what was actually wrong. the user hears it.
"""


def file_bug(tool, problem, asked_for="", extra="", start=None, log=print,
             now=None):
    """Hand this defect to Claude Code. (filed, why_not).

    `start(brief)` is injected so the whole path is testable without Claude
    anywhere near it.
    """
    problem = (problem or "").strip()
    if len(problem) < 8:
        return False, "there wasn't enough detail to report"
    sig = signature(tool, problem)
    if already_filed(sig, now=now):
        log(f"[selfrepair] already reported this one today ({sig})")
        return False, "already reported today"
    if start is None:
        return False, "no way to reach Claude Code"
    text = brief(tool, problem, asked_for=asked_for, extra=extra)
    try:
        start(text)
    except Exception as e:
        return False, f"couldn't start the fix ({type(e).__name__})"
    mark_filed(sig, now=now)
    log(f"[selfrepair] filed: {tool or 'internal'} — {problem[:70]}")
    return True, ""
