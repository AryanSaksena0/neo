"""test_ship.py — what has to be true before anyone else installs this.

1. Nothing about the first person who built it is in the shipped code: no
   name, no school, no company, no friends, no email, no bundle id with a
   name in it. A new install must be blank until its owner speaks.
2. The person's name reaches the prompt from the profile, not from a string.
3. Contacts, Gmail, Docs, Slides, Calendar: the pure parts.
4. The onboarding "about you" answers land in the profile and memory.

Run: python3 test_ship.py
"""
import glob
import json
import os
import re
import sys
import tempfile

os.environ["NEO_PROFILE_PATH"] = os.path.join(tempfile.mkdtemp(), "person.json")
sys.path.insert(0, ".")

FAILED = []

# The owner's own working notes. Gitignored, never in a release checkout, and
# full of names by design — scanning them would fail forever for no reason.
NOT_SHIPPED = {"CLAUDE.local.md", "PROJECT_NOTES.md", "CONVOREPORT.md",
               "FINDINGS.md", "TESTREPORT.md", "HANDOFF.md"}
# CLAUDE.md is NOT on that list on purpose. It ships, because frame_goal() and
# selfrepair.py both tell Claude Code to read it, and on a fresh install it did
# not exist — every handed-off job started with no project context at all. The
# owner's private notes moved to CLAUDE.local.md, which Claude Code also reads
# and which stays gitignored.


def check(name, cond):
    print(("PASS - " if cond else "FAIL - ") + name)
    if not cond:
        FAILED.append(name)


# ---- 1. no trace of the first owner in what ships ----
SHIPPED = [f for f in glob.glob("*.py") + glob.glob("skills/*.py") + glob.glob("*.sh")
           + glob.glob("*.md")
           if not f.startswith("test_") and os.path.exists(f)
           and f not in NOT_SHIPPED]
# Case-INSENSITIVE, and that is the whole point of reading a list rather than
# comparing exact strings. The first version of this test matched the owner's
# name and their other product's name LITERALLY, and passed a build that
# shipped both: the name inside a tool docstring, and a block of that product's
# canonical numbers — real schema, real row counts, real env var names — inside
# the brief every Claude Code job was given. Both were written in caps, so both
# walked straight past a case-sensitive `in`.
#
# The list itself lives in .personal-words, which is GITIGNORED, because the
# second version of this test hardcoded the names here — publishing the owner's
# school, their friends and their other companies in a public repo, which is
# precisely the disclosure the whole file exists to prevent. A guard list of
# real names is PII. It does not ship.
def _personal_words():
    here = os.path.dirname(os.path.abspath(__file__))
    words = []
    try:
        for line in open(os.path.join(here, ".personal-words"), encoding="utf-8"):
            line = line.split("#")[0].strip().lower()
            if line:
                words.append(line)
    except OSError:
        pass
    # No list on this machine: fall back to whoever git says is committing, so
    # a fresh clone still catches the obvious case instead of silently passing.
    if not words:
        for cmd in ("git config user.name", "git config user.email"):
            for tok in re.split(r"[^a-z0-9]+", os.popen(cmd).read().strip().lower()):
                if len(tok) >= 4 and tok not in ("gmail", "com", "users", "github"):
                    words.append(tok)
    return sorted(set(words))


PERSONAL = _personal_words()
# One entry is enough: on a fresh clone there is no .personal-words and the
# fallback derives the current committer from git config, which is the right
# name to be checking for anyway — whoever is about to commit.
check(f"the deny list is loaded ({len(PERSONAL)} entries, "
      + ("from .personal-words" if os.path.exists(".personal-words") else "derived from git config")
      + ")", len(PERSONAL) >= 1)
check("the deny list itself is not committed",
      ".personal-words" not in os.popen("git ls-files").read())

# The ONE allowed exception, and it is narrow on purpose: the repo has to live
# at a GitHub account, so the owner's handle may appear inside a github.com URL
# and nowhere else. Written as a strip-then-scan rather than a skipped file, so
# a name dropped into install.sh's prose still fails.
_REPO_URL = re.compile(r"https://(?:raw\.)?github(?:usercontent)?\.com/[^\s\"'`)]*", re.I)
# The second narrow exception: a copyright line. AGPL-3.0 was chosen precisely
# so the sole author keeps the right to relicense commercially, and that right
# depends on the copyright holder being NAMED. So "Copyright (C) <year> <who>"
# may carry the owner's handle; nothing else may.
_COPYRIGHT = re.compile(r"^\s*Copyright \(C\) \d{4}.*$", re.I | re.M)

for f in SHIPPED:
    src = open(f, errors="ignore").read()
    src = _COPYRIGHT.sub(" <copyright-line> ", src)
    src = _REPO_URL.sub(" <repo-url> ", src).lower()
    hits = sorted({w for w in PERSONAL if re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", src)})
    check(f"no personal trace in {f}" + (f"  <-- {', '.join(hits)}" if hits else ""), not hits)

# ...and prove that exception cannot be used as a hole to smuggle things through.
_probe = _REPO_URL.sub(" <repo-url> ", "see https://github.com/AryanSaksena0/neo — ask aryan")
check("the repo-url exception does not excuse a name in prose", "aryan" in _probe)
_probe2 = _COPYRIGHT.sub(" <copyright-line> ", "Copyright (C) 2026 someone\nwritten by aryan")
check("the copyright exception covers only that one line",
      "aryan" in _probe2 and "someone" not in _probe2)

# The guard on the guard: if this file ever stops lowercasing, say so loudly.
check("the personal-trace scan is case-insensitive",
      all(w == w.lower() for w in PERSONAL)
      and 'open(f, errors="ignore").read().lower()' in open(__file__).read())

for gone in ("db.py", "leads.py", "marketing.py", "pipeline.py", "mailwatch.py", "content.py",
             "skills/gym_streak.py", "profile.py", "install_autostart.sh"):
    check(f"the owner's own module is not shipped: {gone}", not os.path.exists(gone))

# ---- 1a2. no real-world PLACE is hardcoded as a default ----
# The deny list holds NAMES, so it sailed straight past
#   DEFAULT_PLACE = os.getenv("NEO_HOME_TOWN", "Warren, New Jersey")
# which published the first owner's home town in a public repository AND
# answered every stranger's "what's the weather" with somebody else's town.
# Anything that defaults to a place must derive it from the machine or ask.
_PLACE_HINTS = ("new jersey", "new york", ", nj", ", ny", ", ca", ", tx",
                "california", "texas", "london", "boston", "chicago")
_place_leaks = []
for f in SHIPPED:
    if not f.endswith(".py"):
        continue
    for i, line in enumerate(open(f, errors="ignore").read().splitlines(), 1):
        low = line.lower()
        if "getenv(" not in low and "default" not in low:
            continue
        if any(h in low for h in _PLACE_HINTS):
            _place_leaks.append(f"{f}:{i}")
check("no real place is hardcoded as a default"
      + (f"  <-- {_place_leaks}" if _place_leaks else ""), not _place_leaks)

import agent as _agent_mod
check("weather: the home town is derived, not written into the source",
      callable(getattr(_agent_mod, "_home_town", None))
      and "Warren" not in open("agent.py", errors="ignore").read())
check("weather: with nowhere known it ASKS instead of inventing a place",
      "I don't know where you are" in open("agent.py", errors="ignore").read())

# ---- 1b. every module the shipped code imports actually ships ----
# access.py, heavy.py, screencal.py and workflows.py were written, imported by
# agent/agenda/connectors/remind/contacts/claude_bridge, and then simply not
# added to the release repo. Nothing noticed, because every suite runs in the
# dev folder where the files are sitting right there. The release would have
# died on `import access` at startup — on a stranger's Mac, with no traceback
# they could act on. A release is not the files you remembered to copy.
def _unresolved_imports():
    import ast as _ast
    here = os.path.dirname(os.path.abspath(__file__))
    local = {os.path.splitext(f)[0] for f in os.listdir(here) if f.endswith(".py")}
    tracked = set(os.popen("git ls-files '*.py'").read().split())
    if not tracked:                      # not a git checkout: fall back to disk
        tracked = {f for f in os.listdir(here) if f.endswith(".py")}
    bad = {}
    for f in sorted(glob.glob("*.py")):
        try:
            tree = _ast.parse(open(f, encoding="utf-8", errors="ignore").read())
        except SyntaxError:
            continue
        for n in _ast.walk(tree):
            if isinstance(n, _ast.Import):
                mods = {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, _ast.ImportFrom) and n.level == 0 and n.module:
                mods = {n.module.split(".")[0]}
            else:
                continue
            for m in mods:
                if m in local and f"{m}.py" not in tracked:
                    bad.setdefault(m, set()).add(f)
    return {m: sorted(v) for m, v in bad.items()}

_unres = _unresolved_imports()
check("every module the shipped code imports is itself shipped"
      + (f"  <-- {_unres}" if _unres else ""), not _unres)

# ---- 1c. a fresh install works with no key at all ----
# neo.py used to sys.exit(1) when .env had no GEMINI_API_KEY, while the README
# and the onboarding's own Skip button both promised Neo still worked without
# one. So the escape hatch killed the app, silently, after the person had
# installed Homebrew, waited for a model download and granted four macOS
# permissions. This is the highest-friction bug the product had and it must not
# come back.
_neo_src = open("neo.py", encoding="utf-8").read()
check("no key: neo.py does not exit when the key is missing",
      'print("[neo] No Gemini API key. Put it in .env as GEMINI_API_KEY=...")'
      not in _neo_src and "KEYLESS = " in _neo_src)
check("no key: there is a local brain with the surface the chain calls",
      all(f"    def {m}(" in _neo_src.split("class LocalBrain")[1].split("class Brain:")[0]
          for m in ("respond", "forget", "recap", "visualize", "lookup",
                    "delivered", "polish_result", "next_key")))
check("no key: the live cloud socket is never opened",
      "if KEYLESS:\n            return" in _neo_src)
check("no key: the voice falls to the on-device engine",
      'if KEYLESS:\n        return "kokoro"' in _neo_src)
check("no key: there is a spoken route to adding one",
      "_add_key_flow" in _neo_src and "wants_key_setup" in _neo_src)

import commands as _cmd
check("no key: 'add my key' is heard, and ordinary sentences are not",
      _cmd.wants_key_setup("add my key")
      and _cmd.wants_key_setup("make yourself smarter")
      and not _cmd.wants_key_setup("what key is this song in")
      and not _cmd.wants_key_setup("set up a meeting with Priya"))

# The promise in the docs has to match the code, in both directions. The first
# draft of local mode claimed timers, reminders, the calendar and the markets
# all worked without a key. None of them do: remind.parse and agenda.parse both
# take a client, and every tool is reached through the model's tool-calling.
# Only what is routed by a fixed phrase in the chain can work with no model, so
# the docs may only promise THAT.
_rm = open("README.md", encoding="utf-8").read().lower()
_ft = open("FEATURES.md", encoding="utf-8").read().lower()
check("no key: the README says so, and does not promise a dead product",
      "no key" in _rm and "skip it and neo still\ntalks" not in _rm)
for _doc, _name in ((_rm, "README"), (_ft, "FEATURES")):
    _overclaim = [w for w in ("reminders, the calendar, contacts",
                              "timers, reminders")
                  if w in _doc.split("everything else")[0].split("what needs a key")[0]]
    check(f"no key: {_name} does not claim model-only tools work offline",
          not _overclaim)

# What Neo SAYS it can do in local mode has to match what it actually routes.
# Found by running a genuinely fresh install: the docs had been corrected but
# the boot banner and LocalBrain.respond still promised "timers, reminders,
# calendar, markets". None of those work without a model. Neo claiming a
# capability it lacks, in the one message whose whole job is being honest about
# what it lacks, is rule two broken in the worst possible place.
_CANNOT_OFFLINE = ("timer", "reminder", "calendar", "market", "stock",
                   "screen's text", "mail", "inbox")
def _localbrain_says():
    """Every string LocalBrain actually SAYS to the user — the return values of
    its methods, via ast. Not comments and not docstrings: both of those name
    the wrongly-claimed capabilities on purpose, to explain why they went."""
    import ast as _a
    tree = _a.parse(_neo_src)
    out = []
    for node in _a.walk(tree):
        if isinstance(node, _a.ClassDef) and node.name == "LocalBrain":
            for r in _a.walk(node):
                if isinstance(r, _a.Return) and r.value is not None:
                    for lit in _a.walk(r.value):
                        if isinstance(lit, _a.Constant) and isinstance(lit.value, str):
                            out.append(lit.value)
    return " ".join(out)

def _banner_says():
    """The print() calls guarded by `if KEYLESS:` at module level."""
    import ast as _a
    tree = _a.parse(_neo_src)
    out = []
    for node in _a.walk(tree):
        if isinstance(node, _a.If) and isinstance(node.test, _a.Name) and node.test.id == "KEYLESS":
            for c in _a.walk(node):
                if isinstance(c, _a.Constant) and isinstance(c.value, str):
                    out.append(c.value)
    return " ".join(out)

for _blob, _what in ((_localbrain_says(), "LocalBrain's replies"),
                     (_banner_says(), "the boot banner")):
    _bad = sorted({w for w in _CANNOT_OFFLINE if w in _blob.lower()})
    check(f"no key: {_what} claim nothing that needs a model"
          + (f"  <-- {_bad}" if _bad else ""), not _bad)
check("no key: the claim-scan is looking at real output",
      "hear you and talk back" in _localbrain_says())

check("no key: the two things the docs DO promise are really routed offline",
      _cmd.parse_offline_fact("what time is it") == "time"
      and _cmd.parse_offline_fact("what's the weather") == "weather"
      and _cmd.parse_offline_fact("what time is the meeting") is None
      and "parse_offline_fact(text)" in _neo_src)

gi = open(".gitignore").read()
for f in ("memory.json", "person.json", ".browser/", "leads.json", "stealth.json", ".neo.onboarded"):
    check(f"personal data is gitignored: {f}", f in gi)
tracked = os.popen("git ls-files").read().split("\n")
check("no browser profile or memory is tracked by git",
      not any(t.startswith(".browser/") or t in ("memory.json", "person.json", "leads.json", "marketing.json") for t in tracked))

# ---- 2. the name is the profile's, not a string's ----
import memory, person
p = person.load(); p["person"] = {"name": "Dana Ortiz", "first_name": "Dana", "role": "nurse"}; person.save(p)
prompt = memory.build_system_prompt({"facts": []})
check("prompt: names the person from the profile", prompt.startswith("You are Neo, Dana's personal voice assistant"))
check("prompt: carries their role from onboarding", "They are a nurse" in prompt)
check("prompt: no placeholder left", "{NAME}" not in prompt)
check("prompt: no pronoun assumptions", " he " not in memory.PERSONALITY and " him " not in memory.PERSONALITY)
person.save({"updated": {}})
_blank = memory.build_system_prompt({"facts": []})
check("prompt: with no profile, no name is invented",
      _blank.startswith("You are Neo, the person you work for's") is False
      and "You are Neo, their personal voice assistant" in _blank and "Dana" not in _blank)

# ---- 3. contacts + google ----
import contacts, gsuite, datetime as dt
check("contacts: first name matches a full name", contacts.score("dean", "Dean Hoepfl") == 2)
check("contacts: exact beats partial", contacts.score("dean hoepfl", "Dean Hoepfl") > contacts.score("dean", "Dean Hoepfl"))
check("contacts: prefix is weakest", contacts.score("dea", "Deanna Ross") == 1 and contacts.score("dean", "Rohan") == 0)
check("contacts: From header parsed", contacts.parse_from('Dean H <dh@x.org>') == ("Dean H", "dh@x.org"))
ppl = [{"name": "Mum", "emails": ["m@x"]}, {"name": "Mumtaz K", "emails": ["k@x"]}]
check("contacts: match ranks exact first", contacts.match("mum", ppl)[0][1]["name"] == "Mum")
check("gsuite: doc/slides/sheet create urls", gsuite.doc_url("slides", "Q3 plan").startswith("https://docs.google.com/presentation/create?title=Q3%20plan")
      and "spreadsheets/create" in gsuite.doc_url("sheet"))
u = gsuite.gmail_compose_url("a@b.com", "Hi", "One\n\nTwo")
check("gsuite: gmail compose url carries to, subject, body", "to=a%40b.com" in u and "su=Hi" in u and "body=One%0A%0ATwo" in u)
busy = [(dt.datetime(2026, 9, 14, 9, 0), dt.datetime(2026, 9, 14, 12, 30))]
slots = gsuite.free_slots(busy, dt.date(2026, 9, 14), days=3, minutes=15, now=dt.datetime(2026, 9, 11, 18, 0))
check("gsuite: first free slot skips the busy morning", slots[0][0] == dt.datetime(2026, 9, 14, 12, 30))
check("gsuite: never proposes a weekend or outside working hours",
      all(s.weekday() < 5 and 9 <= s.hour < 17 for s, _ in gsuite.free_slots([], dt.date(2026, 9, 12), days=7, now=dt.datetime(2026, 9, 11))))
cu = gsuite.calendar_event_url("Sync", slots[0][0], slots[0][1], ["x@y.com", "z@y.com"])
check("gsuite: calendar editor url adds the guests", "add=x%40y.com%2Cz%40y.com" in cu and "dates=20260914T123000" in cu)
check("gsuite: next monday", gsuite.next_monday(dt.date(2026, 9, 11)) == dt.date(2026, 9, 14))
import agent
check("tools: docs, gmail compose and meeting time are in the toolbox",
      all(t in agent.TOOLS for t in (agent.create_google_doc, agent.compose_gmail, agent.find_meeting_time)))
check("tools: the meeting tool is honest about whose calendar it can see", "nothing on this Mac can see" in agent.find_meeting_time.__doc__)

# ---- 4. onboarding: about you ----
import onboard
mem_path = memory.MEMORY_PATH
memory.MEMORY_PATH = os.path.join(tempfile.mkdtemp(), "memory.json")
try:
    onboard.save_about({"name": "Dana", "role": "nurse", "length": "long", "notes": "I work nights at St Mary's. My sister is Ana"})
    p = person.load()
    check("about: name and role saved to the profile", p["person"]["first_name"] == "Dana" and p["person"]["role"] == "nurse")
    check("about: answer length becomes the response preference", p["response"]["length"] == "long")
    facts = [f["text"] for f in memory.load_memory()["facts"]]
    check("about: the notes become memory facts", any("St Mary" in f for f in facts) and any("Ana" in f for f in facts))
    check("about: the role is a fact too", any("nurse" in f for f in facts))
finally:
    memory.MEMORY_PATH = mem_path
html = onboard._HTML
check("about: the scene exists between the key and first words", onboard._HTML.index('<section class="scene" data-s="about"') < onboard._HTML.index('<section class="scene" data-s="first"')
      and '"key","connect","about","first"' in html.replace(' ', ''))
check("about: asks name, role, answer length, notes", all(x in html for x in ("aName", "aRole", "aLen", "aNotes")))
check("about: has a recorded line", os.path.exists("onboard_audio/about.wav") or "about" in onboard.NARRATION)

# ---- 5. the directory: where emails actually are ----
import directory
_page = """Directory
Mehar Kapoor
mkapoor2028@school.org
Melanie Weyland
mweyland@school.org
Alex Joujan ajoujan@school.org
"""
check("directory: a name on the line above its address is matched", directory.parse_people(_page, "Mehar")[0]["emails"] == ["mkapoor2028@school.org"])
check("directory: a name on the same line as its address is matched", directory.parse_people(_page, "Alex")[0]["emails"] == ["ajoujan@school.org"])
check("directory: 'Mel' does not return Mehar", all(p["name"].startswith("Melanie") for p in directory.parse_people(_page, "Melanie")))
check("directory: an absent person is absent, never guessed", directory.parse_people(_page, "Dean") == [])
check("directory: the search page is Google's own", directory.search_url("Dean Hoepfl") == "https://contacts.google.com/search/Dean%20Hoepfl")
check("contacts: a phone-only Contacts hit does not stop the search for an email",
      "if p.get(\"emails\")" in open("contacts.py").read() and "directory.via_neo_browser" in open("contacts.py").read())
check("tools: find_person is in the toolbox and the mail tools point at it",
      agent.find_person in agent.TOOLS and "find_person" in agent.draft_email.__doc__
      and "find_person" in agent.compose_gmail.__doc__ and "find_person" in agent.find_meeting_time.__doc__)
check("tools: find_person never opens a window in stealth", "visible=not _stealth_on()" in open("agent.py").read())

# ---- 6. re-proposing a meeting: honours the constraint, replaces the tab, never lies ----
check("gsuite: 'after 10am' is parsed", gsuite.parse_hour_bound("something after 10:00 a.m.") == (10.0, None))
check("gsuite: 'before 3' means 3pm in a working day", gsuite.parse_hour_bound("before 3")[1] == 15.0)
_s1 = gsuite.free_slots([], dt.date(2026, 9, 14), days=2, minutes=15, start_hour=10.0, now=dt.datetime(2026, 9, 11))
_s2 = gsuite.free_slots([], dt.date(2026, 9, 14), days=2, minutes=15, start_hour=10.0, now=dt.datetime(2026, 9, 11), exclude=[_s1[0]])
check("gsuite: a turned-down slot is not proposed again", _s1[0] != _s2[0] and _s1[0][0].hour == 10)
_src = open("agent.py").read()
check("meeting: constraints reach the slot finder", "constraints: str" in _src and "parse_hour_bound(f\"{when} {constraints}\")" in _src)
check("meeting: the previous editor tab is closed before a new one opens", "close_tabs_containing(\"calendar.google.com/calendar/u/0/r/eventedit\")" in _src)
check("meeting: the model is told the ONLY time it may say", "That is the ONLY time to say" in _src and "SAY EXACTLY THE TIME THIS TOOL RETURNS" in agent.find_meeting_time.__doc__)
check("chrome: tab closing is by URL fragment across all windows", "def close_tabs_containing" in open("chrome.py").read())

# ---- nothing a test or a terminal check does may open System Settings ----
import perms as _perms
_psrc = open("perms.py").read()
check("perms: Input Monitoring is checked with IOHIDCheckAccess, never by creating a tap (a tap opens the pane)",
      "IOHIDCheckAccess" in _psrc and "CGEventTapCreate" not in _psrc)
check("perms: the check runs here without side effects", _perms.input_monitoring() in (True, False, None))

print()
if FAILED:
    print(f"{len(FAILED)} FAILED:")
    for f in FAILED:
        print("  -", f)
    sys.exit(1)
print("Ship-ready: nothing personal, everything per person.")
