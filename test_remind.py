"""test_remind.py — "remind me to…", checked without touching Reminders.

The behaviour that matters most is WHEN NEO ASKS. He should ask exactly when
the time is missing or a vague word, and never otherwise — a question on
every reminder is the thing that makes people stop using the feature.

Run: python3 test_remind.py
"""
import datetime as dt
import sys

sys.path.insert(0, ".")
import remind
import agent

FAILED = []


def check(name, cond):
    print(("PASS - " if cond else "FAIL - ") + name)
    if not cond:
        FAILED.append(name)


TODAY = dt.date(2026, 9, 11)                    # a Friday
NOW = dt.datetime(2026, 9, 11, 14, 0)


def norm(fields, text=""):
    return remind.normalise(fields, text=text, today=TODAY, now=NOW)


# ---- the two cases that ASK ----
r, p = norm({"title": "Call mum"}, "remind me to call mum")
check("asks: no time at all -> no_time, title kept", p == "no_time" and r["title"] == "Call mum")
r, p = norm({"title": "Call mum", "date": "2026-09-11", "time": "15:00"},
            "remind me to call mum later")
check("asks: 'later' is vague even when the model invents a time", p == "vague")
check("asks: 'sometime' is vague", norm({"title": "x", "time": "10:00"}, "remind me sometime to x")[1] == "vague")
check("asks: 'at some point' is vague", remind.is_vague("remind me at some point to pay"))
check("asks: 'in a bit' is vague", remind.is_vague("remind me in a bit"))
check("asks: 'soon' is vague", remind.is_vague("remind me soon to call"))
check("asks: 'later' inside a longer word is NOT vague", not remind.is_vague("remind me about the laterals drill at 5"))
check("asks: no title -> no_title", norm({"title": "", "time": "10:00"})[1] == "no_title")
check("the two ask keys have questions", remind.TROUBLE["no_time"].endswith("?") and remind.TROUBLE["vague"].endswith("?"))

# ---- the cases that JUST DO IT ----
r, p = norm({"title": "Call mum", "date": "", "time": "18:00"}, "remind me to call mum at six")
check("acts: 'at six' today, no question", p is None and r["when"] == dt.datetime(2026, 9, 11, 18, 0))
r, p = norm({"title": "Call mum", "time": "6:00"}, "remind me to call mum at six")
check("acts: 'at six' said at 2pm means 6pm, not 6am", p is None and r["when"].hour == 18)
r, p = norm({"title": "Call mum", "time": "10:00"}, "remind me to call mum at ten")
check("acts: 'at ten' said at 2pm rolls to tomorrow 10am", p is None and r["when"] == dt.datetime(2026, 9, 12, 10, 0))
r, p = norm({"title": "Submit the form", "date": "2026-09-12", "time": "09:00"}, "tomorrow morning")
check("acts: tomorrow morning", p is None and r["when"] == dt.datetime(2026, 9, 12, 9, 0) and not r["all_day"])
r, p = norm({"title": "Take the bins out", "minutes_from_now": 20}, "in twenty minutes")
check("acts: 'in twenty minutes' is relative to now", p is None and r["when"] == dt.datetime(2026, 9, 11, 14, 20))
r, p = norm({"title": "Pay rent", "date": "2026-09-18"}, "remind me to pay rent on Friday")
check("acts: a day with no clock is an all-day reminder", p is None and r["all_day"] and r["when"].date() == dt.date(2026, 9, 18))
r, p = norm({"title": "x", "date": "2026-09-01", "time": "10:00"}, "on the first")
check("refuses: a dated time in the past", p == "past")
check("refuses: nonsense time", norm({"title": "x", "time": "half past"}, "x")[1] == "bad_time")
r, p = norm({"title": "x" * 200, "time": "18:00"}, "x")
check("a runaway title is trimmed", p is None and len(r["title"]) <= 120)

# ---- what he hears ----
s = remind.speak({"title": "Call mum", "when": dt.datetime(2026, 9, 11, 18, 0), "all_day": False},
                 today=TODAY, now=NOW)
check("speak: 'Call mum, today at 6 in the evening'", s == "Call mum, today at 6 in the evening")
s = remind.speak({"title": "Take the bins out", "when": dt.datetime(2026, 9, 11, 14, 20), "all_day": False},
                 today=TODAY, now=NOW)
check("speak: a near one is said in minutes", s == "Take the bins out, in 20 minutes")
s = remind.speak({"title": "Pay rent", "when": dt.datetime(2026, 9, 18, 9, 0), "all_day": True},
                 today=TODAY, now=NOW)
check("speak: an all-day one names the day only", s == "Pay rent, next Friday")

# ---- the tool: asks only through the tool's answer, never on its own ----
check("tool: set_reminder is in the toolbox", agent.set_reminder in agent.TOOLS)
doc = agent.set_reminder.__doc__
check("tool: the docstring forbids asking before calling", "do NOT ask a clarifying question first" in doc)


class _FakeResp:
    def __init__(self, text): self.text = text


class _FakeModels:
    def __init__(self, text): self._t = text
    def generate_content(self, model, contents, config=None): return _FakeResp(self._t)


class _FakeClient:
    def __init__(self, text): self.models = _FakeModels(text)


_old = agent._client
try:
    agent._client = _FakeClient('{"title": "Call mum", "date": "", "time": ""}')
    out = agent.set_reminder("remind me to call mum")
    check("tool: a missing time comes back as the ONE question, not a failure",
          "NOT SET YET" in out and remind.TROUBLE["no_time"] in out and "NOTHING HAPPENED" not in out)
    agent._client = _FakeClient('{"title": "Call mum", "date": "", "time": "15:00"}')
    out = agent.set_reminder("remind me to call mum later")
    check("tool: 'later' asks 'when exactly'", "NOT SET YET" in out and remind.TROUBLE["vague"] in out)
    agent._client = _FakeClient('not json at all')
    out = agent.set_reminder("remind me to call mum at six")
    check("tool: a broken model answer is a plain failure", "NOTHING HAPPENED" in out)
finally:
    agent._client = _old

# ---- the skill that used to grab 'remind me' no longer does ----
import skills
skills.load_all()
check("routing: 'remind me to call mum at six' is not claimed by any skill",
      skills.find("remind me to call mum at six") is None)
check("routing: 'add call mum to my list' is still the to-do skill",
      getattr(skills.find("add call mum to my to-do list"), "NAME", None) == "todo")

# ---- the app declares the permission it will ask for ----
plist = open("make_app.sh").read()
check("app: NSRemindersUsageDescription is in Info.plist (macOS kills the app otherwise)",
      "NSRemindersUsageDescription" in plist and "NSRemindersFullAccessUsageDescription" in plist)
import perms
check("perms: Reminders is listed as an optional permission",
      any(k == "reminders" for k, *_ in perms.PERMISSIONS) and "reminders" not in perms.REQUIRED)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED:")
    for f in FAILED:
        print("  -", f)
    sys.exit(1)
print("All reminder tests pass.")
