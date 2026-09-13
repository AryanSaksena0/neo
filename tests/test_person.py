"""test_person.py — the second brain, the learner, and the background browser.

Everything here is pure or points at a temp file. Nothing touches the real
profile.json, memory.json, or a browser.

Run: python3 test_person.py
"""
# Suites live in tests/ but RUN from the repo root, so that the modules
# under test import and open("neo.py") still resolves. This makes the
# import work either way, so a suite can also be run directly.
import os as _bootstrap_os, sys as _bootstrap_sys
_bootstrap_sys.path.insert(0, _bootstrap_os.path.dirname(
    _bootstrap_os.path.dirname(_bootstrap_os.path.abspath(__file__))))

import os
import sys
import tempfile

os.environ["NEO_PROFILE_PATH"] = os.path.join(tempfile.mkdtemp(), "profile.json")
sys.path.insert(0, ".")
import person as profile
import learn
import webdrive
import mail
import agent

FAILED = []


def check(name, cond):
    print(("PASS - " if cond else "FAIL - ") + name)
    if not cond:
        FAILED.append(name)


# ---- response preferences: instant, from what they say ----
check("pref: 'that was too long' -> short", profile.preference_from("that was way too long") == {"length": "short"})
check("pref: 'go deeper' -> long", profile.preference_from("can you go deeper on that") == {"length": "long"})
check("pref: 'just show it on screen' -> text", profile.preference_from("just show it on screen, don't read it out")["mode"] == "text")
check("pref: 'no jokes' -> humour none", profile.preference_from("no jokes please").get("humour") == "none")
check("pref: an ordinary question says nothing", profile.preference_from("what time is it in Tokyo") == {})
p = {}
for _ in range(3):
    profile.note_preference(p, {"length": "short"})
profile.note_preference(p, {"length": "long"})
check("pref: one 'longer' doesn't erase three 'shorter'", p["response"]["length"] == "short")
check("pref: history is capped", len(profile.note_preference({"response": {"history": [{"at": "", "pref": {"length": "short"}}] * 40}}, {"length": "short"})["response"]["history"]) <= 30)

# ---- writing style from samples ----
st = profile.writing_stats(["hey! quick one, are we still on for thursday? lmk\nthanks\nA",
                            "hey, can't make it today. tomorrow works tho\ncheers\nA",
                            "yo did you see the doc? it's in the drive"])
check("writing: greeting and sign-off are the ones they use", st.get("greeting") == "hey" and st.get("signoff") in ("thanks", "cheers"))
check("writing: register is read as casual", st["formality"] == "casual")
formal = profile.writing_stats(["Dear Professor Lin,\n\nI am writing to ask whether the deadline for the second problem set could be extended by two days, as I have been unwell this week and have documentation from the health centre.\n\nSincerely,\nA"])
check("writing: a long formal note is read as formal", formal["formality"] == "formal" and formal.get("greeting") == "dear")
check("writing: nothing from nothing", profile.writing_stats([]) == {})

# ---- accounts: what each browser profile is for ----
check("accounts: gmail is personal", profile.classify_account("someone@gmail.com") == "personal")
check("accounts: .edu is school", profile.classify_account("s@stanford.edu") == "school")
check("accounts: a company domain is work", profile.classify_account("a@acme.com") == "work")
check("accounts: a profile NAMED Work beats a gmail address", profile.classify_account("x@gmail.com", "Work") == "work")
check("accounts: a profile named Mum is family", profile.classify_account("x@gmail.com", "Mum") == "family")
check("accounts: no address, no name -> unknown", profile.classify_account("", "") == "unknown")
pp = {"accounts": {"browsers": [{"app": "Chrome", "profile": "Person 2", "email": "m@gmail.com", "kind": "personal"}]}}
check("accounts: the user's word retags a profile", profile.retag_account(pp, "person 2", "family") and pp["accounts"]["browsers"][0]["kind"] == "family")
check("accounts: an unknown profile name is refused", not profile.retag_account(pp, "nope", "work"))

# ---- active hours: the day is a circle ----
fd, lp = tempfile.mkstemp(suffix=".log"); os.close(fd)
with open(lp, "w") as f:
    for h in list(range(9, 24)) + [0, 1]:
        for _ in range(4):
            f.write(f"[{h:02d}:10:00] (let go — thinking)\n")
ah = profile.active_hours(lp)
check("routine: up until 1am reads as 9:00 to 1:00, not 0:00 to 23:00", ah["first_hour"] == 9 and ah["last_hour"] == 1)
check("routine: no log, no claim", profile.active_hours("/nonexistent") == {})

# ---- the brief: compact, and only what changes behaviour ----
p2 = {"person": {"name": "Sam Lee", "first_name": "Sam", "timezone": "GMT"},
      "response": {"length": "short"},
      "writing": {"mail": {"formality": "casual", "avg_sentence_words": 9, "greeting": "hey", "signoff": "cheers"}},
      "accounts": {"browsers": [{"app": "Chrome", "profile": "Work", "email": "s@acme.com", "kind": "work"}]},
      "apps": {"installed": ["Slack", "Google Chrome", "SomeObscureApp"], "missing_required": []},
      "updated": {}}
b = profile.brief(p2)
check("brief: names them and how to address them", "Sam Lee" in b and "call them Sam" in b)
check("brief: carries the learned answer length", "length: short" in b)
check("brief: carries how they write", "casual register" in b and "opens with 'hey'" in b and "signs off 'cheers'" in b)
check("brief: names the work browser profile", "Chrome 'Work' = work" in b)
check("brief: lists notable apps only", "Slack" in b and "SomeObscureApp" not in b)
check("brief: the boundaries are always there", "draft-only" in b and "No em dashes" in b)
check("brief: an empty profile says nothing", profile.brief({"updated": {}}) == "")
check("brief: is compact", len(b) < 1400)

# ---- the learner ----
got = learn.parse('```json\n{"facts": ["They take the 8:15 train every weekday.", "Neo said the weather was fine", "x"], '
                  '"preferences": {"length": "short", "bogus": 1}, "people": [{"name": "Jordan", "relation_or_role": "friend"}], '
                  '"working_on": ["AP Bio video script"]}\n```')
check("learn: facts about them survive, facts about Neo and junk don't",
      got["facts"] == ["They take the 8:15 train every weekday."])
check("learn: only known preference keys", got["preferences"] == {"length": "short"})
check("learn: people and work carried", got["people"][0]["name"] == "Jordan" and got["working_on"] == ["AP Bio video script"])
check("learn: garbage in, empty out", learn.parse("not json") == {"facts": [], "preferences": {}, "people": [], "working_on": []})
check("learn: a session with no user turn is skipped", learn.from_session([{"role": "model", "text": "hello there friend"}], lambda p: "{}") is None)

# ---- drafts pick up their style ----
check("mail: the draft greeting comes from their style (profile) when known",
      mail.format_body("x", to_name="Priya", sender="A", greeting="Hey", closing="Cheers").startswith("Hey Priya,")
      and mail.format_body("x", to_name="Priya", sender="A", greeting="Hey", closing="Cheers").rstrip().endswith("Cheers,\nA"))

# ---- the background browser: pure pieces ----
check("web: login walls are recognised", webdrive.needs_login("https://accounts.google.com/v3/signin/identifier?continue=x")
      and not webdrive.needs_login("https://mail.google.com/mail/u/0/#inbox"))
check("web: page text is trimmed at a line, with a note", webdrive.trim("a\n" * 5000, 100).endswith("characters)") and len(webdrive.trim("short")) == 5)
check("web: the profile lives with Neo, not in his Chrome", webdrive.PROFILE_DIR.startswith(os.path.dirname(os.path.abspath("neo.py")))
      and "Library/Application Support/Google/Chrome" not in webdrive.PROFILE_DIR)
check("web: headless by default, a window only for sign-in",
      "--headless=new" in open("webdrive.py").read() and "launch(headed=True" in open("webdrive.py").read())
check("web: the tools exist", all(t in agent.TOOLS for t in (agent.browse, agent.browse_click, agent.browse_type, agent.browser_signed_in)))
check("web: browse never types a password", "password" not in agent.browse.__doc__.lower() or "don't do passwords" in open("webdrive.py").read())
check("web: read_email falls back to the browser when Mail isn't set up", "webdrive.available()" in open("agent.py").read().split("def read_email")[1].split("def resolve_recipients")[0])

# ---- the tool for corrections ----
check("update_profile: is a tool", agent.update_profile in agent.TOOLS)
out = agent.update_profile("response", "keep it short")
check("update_profile: 'keep it short' is kept", "short" in out and profile.load()["response"]["length"] == "short")
check("update_profile: an unknown browser profile is refused honestly", "NOTHING HAPPENED" in agent.update_profile("browser", "Zorp is family"))

# ---- the name matters: 'profile.py' shadowed the stdlib module and killed torch ----
import importlib.util as _ilu
check("person: no module in this folder shadows stdlib 'profile' (it broke cProfile -> torch -> the local voice)",
      not os.path.exists("profile.py") and "cProfile" in open(_ilu.find_spec("profile").origin).read() or not os.path.exists("profile.py"))
try:
    from transformers import AlbertModel as _A
    _albert = True
except Exception:
    _albert = False
check("person: transformers' AlbertModel (Kokoro's dependency) imports", _albert)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED:")
    for f in FAILED:
        print("  -", f)
    sys.exit(1)
print("Profile, learning and browser clean.")
