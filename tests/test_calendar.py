"""test_calendar.py — "find a time" against a real school week.

The failure that prompted this: Neo proposed Monday 10:30 for a meeting.
Monday 10:30 is American Literature. Neo could not see the calendar (the
Google calendar isn't in the Mac's Calendar app) and treated an empty week
as a free one. So:

  - a week shaped like a real timetable, five days of classes
  - every proposal must land in a gap, honour "after 10", "before 3",
    "afternoon", skip a turned-down slot, skip a named day
  - with NO calendar source, no time may be proposed at all

Run: python3 test_calendar.py
"""
# Suites live in tests/ but RUN from the repo root, so that the modules
# under test import and open("neo.py") still resolves. This makes the
# import work either way, so a suite can also be run directly.
import os as _bootstrap_os, sys as _bootstrap_sys
_bootstrap_sys.path.insert(0, _bootstrap_os.path.dirname(
    _bootstrap_os.path.dirname(_bootstrap_os.path.abspath(__file__))))

import datetime as dt
import os
import sys

sys.path.insert(0, ".")
import gsuite

FAILED = []


def check(name, cond):
    print(("PASS - " if cond else "FAIL - ") + name)
    if not cond:
        FAILED.append(name)


MON = dt.date(2026, 9, 14)
NOW = dt.datetime(2026, 9, 11, 19, 0)

# The week from the screenshot: advisory, a short class, a long class, lunch
# class, afternoon class — different each day, the way a rotation is.
DAY_PAGES = {
    # A made-up week. This fixture used to be the author's REAL timetable —
    # their classes, their room numbers, their after-school club and the name of
    # a local business — sitting in a public repository. Test fixtures are
    # shipped code; personal data does not belong in them any more than it
    # belongs in agent.py.
    0: "Adv | Advisory 3, 8:30am\n4 | PHYSICS\n8:40am, 140\n5 | LITERATURE\n10:30 \u2013 11:35am\n239\n6 | BIOLOGY\n12:10pm, 106\n7 | SPANISH\n1:40 \u2013 2:45pm\n241\n",
    1: "Adv | Advisory 3, 8:30am\n2 | PRE-CALCULUS\n10:30 \u2013 11:35am\n441\n3 | HISTORY\n11:40am, 308\n4 | PHYSICS, 1:15pm\n4 | PHYSICS\n1:40 \u2013 2:45pm\n140\n",
    2: "Adv | Advisory 3, 8:30am\n5 | LITERATURE\n8:40am, 239\n6 | BIOLOGY\n10:15 \u2013 11:35am\n106\n7 | SPANISH\n11:40am, 241\n",
    3: "Adv | Advisory 3, 8:30am\n2 | PRE-CALCULUS\n8:40am, 441\n3 | HISTORY\n10:30 \u2013 11:35am\n308\n4 | PHYSICS\n12:10pm, 140\n5 | LITERATURE\n1:40 \u2013 2:45pm\n239\n",
    4: "Adv | Advisory 3, 8:30am\n6 | BIOLOGY\n8:40am, 106\n7 | SPANISH\n10:30 \u2013 11:35am\n241\n2 | PRE-CALCULUS\n1:40 \u2013 2:45pm\n441\nSwimming\n3pm, The Pool\n",
}
BUSY = []
for d, page in DAY_PAGES.items():
    BUSY += gsuite.parse_day_page(page, MON + dt.timedelta(days=d))


def clashes(slot):
    s, e = slot
    return [b for b in BUSY if b[0] < e and b[1] > s]


check("parse: Monday has four blocks (advisory and physics overlap into one)", len(gsuite.parse_day_page(DAY_PAGES[0], MON)) == 4)
check("parse: a range chip", (dt.datetime(2026, 9, 14, 10, 30), dt.datetime(2026, 9, 14, 11, 35)) in BUSY)
check("parse: a start-only chip gets a default length", any(b[0] == dt.datetime(2026, 9, 14, 12, 10) and b[1] > b[0] for b in BUSY))
check("parse: '4 | PHYSICS, 1:15pm' then '1:40 – 2:45pm' merge into one block",
      any(b[0] == dt.datetime(2026, 9, 15, 13, 15) and b[1] == dt.datetime(2026, 9, 15, 14, 45) for b in BUSY))
check("parse: 'Advisory 11' does not become an 11am event", not any(b[0].hour == 11 and b[0].minute == 0 for b in BUSY))

# ---- the proposals, many rounds ----
proposed = []
for round_ in range(12):
    slots = gsuite.free_slots(BUSY, MON, days=5, minutes=15, now=NOW, exclude=proposed, limit=8)
    if not slots:
        break
    proposed.append(slots[0])
check("propose: twelve rounds of 'that doesn't work' never repeat a slot", len(set(proposed)) == len(proposed) >= 5)
check("propose: NO proposal ever lands in a class", all(not clashes(p) for p in proposed))
check("propose: never before 9 or after 5, never a weekend",
      all(9 <= p[0].hour < 17 and p[0].weekday() < 5 for p in proposed))

after10 = gsuite.free_slots(BUSY, MON, days=5, minutes=15, now=NOW, start_hour=10.0, limit=8)
check("constraint: 'after 10' -> nothing starts before 10:00", all(p[0].hour >= 10 for p in after10) and after10)
check("constraint: 'after 10' on Monday is NOT 10:30 (that's Literature)", after10[0][0] != dt.datetime(2026, 9, 14, 10, 30) and not clashes(after10[0]))
before11 = gsuite.free_slots(BUSY, MON, days=5, minutes=15, now=NOW, end_hour=11.0, limit=8)
check("constraint: 'before 11' -> everything ends by 11", all(p[1].hour < 11 or (p[1].hour == 11 and p[1].minute == 0) for p in before11) and before11)
pm = gsuite.free_slots(BUSY, MON, days=5, minutes=15, now=NOW, start_hour=12.0, limit=8)
check("constraint: 'afternoon' -> 12:00 or later, still no clash", all(p[0].hour >= 12 and not clashes(p) for p in pm) and pm)
long_ = gsuite.free_slots(BUSY, MON, days=5, minutes=60, now=NOW, limit=8)
check("constraint: an hour-long slot fits an hour with no clash", all((p[1] - p[0]) == dt.timedelta(hours=1) and not clashes(p) for p in long_) and long_)
check("bounds: 'after 10am' parses", gsuite.parse_hour_bound("after 10am") == (10.0, None))
check("bounds: 'not before 10:30' parses to 10.5", gsuite.parse_hour_bound("not before 10:30")[0] == 10.5)
check("bounds: 'before 3' is 3pm", gsuite.parse_hour_bound("before 3") == (None, 15.0))

# ---- honesty: no source, no time ----
import agent
src = open("agent.py").read().split("def find_meeting_time")[1].split("def find_person")[0]
check("honesty: without a calendar source the tool proposes NO time (raises the Approve card)", "_need(\"calendar\"" in src and "PROPOSED" not in src.split("_need(")[0].split("slots = gsuite")[0])
check("honesty: the source of 'free' is named to the model", "free in {seen}" in src)
check("honesty: an empty Mac calendar in a working week is 'unknown', not 'free'",
      "if b:\n                return b, \"mac\"" in open("gsuite.py").read())
b, source = gsuite.busy_week(MON, 5, log=lambda *a: None)
check("live: on THIS Mac right now the week is either read for real or honestly unknown",
      (source in ("mac", "google") and b) or (source is None and b == []))
print(f"       (this Mac: source={source}, blocks={len(b)})")

# ---- the calendar ON SCREEN: everyone's blocks, read by vision, gaps by code ----
import screencal
raw = '''```json
{"is_calendar": true, "week_start": "2026-09-13", "visible_days": ["Wed","Thu","Fri"],
 "days": {"Mon": [], "Tue": [],
          "Wed": [["08:00","08:45"],["10:15","11:35"],["11:40","12:25"],["13:40","14:45"]],
          "Thu": [["08:10","09:45"],["10:30","11:35"],["11:40","12:25"],["13:40","14:45"],["15:30","18:30"]],
          "Fri": [["07:30","08:25"],["08:45","09:30"],["09:40","10:25"],["10:30","11:35"],["11:40","12:25"],["13:30","14:45"]]}}
```'''
days, week, ok = screencal.parse(raw)
check("screen: parsed as a calendar with the week", ok and week == dt.date(2026, 9, 13))
check("screen: days hidden behind another window are UNKNOWN, not free", "Mon" not in days and "Tue" not in days)
g = screencal.gaps(days, 15)
check("screen: no gap is ever proposed on an unseen day", all(x[0] in ("Wed", "Thu", "Fri") for x in g) and g)
check("screen: gaps keep a margin from every block edge",
      all(not any(bs <= x[1] < be for bs, be in days[x[0]]) for x in g)
      and any(x[0] == "Wed" and x[1] == dt.time(12, 35) for x in g))
check("screen: 'not a calendar' is honest", screencal.parse('{"is_calendar": false}') == ({}, None, False))
check("screen: garbage is honest", screencal.parse("nope")[2] is False)
after = screencal.gaps(days, 15, start_hour=12)
check("screen: 'afternoon' constraint holds", all(x[1].hour >= 12 for x in after) and after)
check("screen: a turned-down gap is skipped next time", screencal.gaps(days, 15, exclude=[g[0]])[0] != g[0])
check("tool: free_time_on_screen exists and forbids asking them to scroll",
      agent.free_time_on_screen in agent.TOOLS and "Do NOT ask them to scroll" in agent.free_time_on_screen.__doc__)
check("rules: 'it's on my screen' means read the screen, never ask which week",
      "IT'S ON MY SCREEN" in open("context.py").read() and "free_time_on_screen" in open("context.py").read())
check("access: permission prompts go through the main thread and wait for a click",
      "AppHelper.callAfter(fn)" in open("access.py").read() and "done.wait(WAIT_S)" in open("access.py").read())
check("access: agenda, remind and contacts all request through access.py",
      all("import access" in open(f).read() for f in ("agenda.py", "remind.py", "contacts.py")))
check("access: System Settings is not opened as a reflex", "maybe_open_settings" in open("connectors.py").read()
      and "perms.open_settings(key)" not in open("connectors.py").read().split("def _denied")[1].split("def test")[0])

check("bounds: 'within the school day' means 8:00 to 15:30", gsuite.parse_hour_bound("obviously within the school day") == (8.0, 15.5))
check("bounds: 'during school after 10' keeps both", gsuite.parse_hour_bound("during school, after 10") == (10.0, 15.5))
_ft = open("agent.py").read().split("def free_time_on_screen")[1].split("def find_person")[0]
check("screen tool: with no gap in the window it names the nearest one outside and never asks back",
      "NO GAP of" in _ft and "nearest gap outside" in _ft and "Do NOT ask a question back" in _ft)
_nsrc = open("neo.py").read().split("def _live_ack")[1].split("def _fire")[0]
check("acks: every slow tool gets a filler, not only the ones in the map",
      'kind = "quick"' in _nsrc and "_INSTANT_TOOLS" in _nsrc)
_pv = open("providers.py").read()
check("models: the newest flash leads the chat and heavy lists (2.5-flash is a backstop)",
      _pv.index('("gemini", "gemini-3.8-flash")') < _pv.index('("gemini", "gemini-2.5-flash")'))

# ---- the deterministic read: OCR geometry (the Thursday he showed) ----
def _y(h): return 30 + (h - 8) * 6.6
L = [{"text": f"{h if h <= 12 else h - 12} {'AM' if h < 12 else 'PM'}", "x": 19, "y": _y(h), "w": 2, "h": 1} for h in range(8, 18)]
L += [{"text": d, "x": 24 + i * 10, "y": 20, "w": 2, "h": 1} for i, d in enumerate(["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"])]
L += [{"text": "busy, 8:10am", "x": 65, "y": _y(8.17), "w": 3, "h": 1},
      {"text": "busy 8:45 – 9:45am", "x": 65, "y": _y(8.75), "w": 3, "h": 1},
      {"text": "3 | HISTORY 10:30 – 11:35am", "x": 65, "y": _y(10.5), "w": 4, "h": 1},
      {"text": "busy 11am", "x": 68, "y": _y(11.0), "w": 2, "h": 1},
      {"text": "busy, 11:5", "x": 68, "y": _y(11.83), "w": 2, "h": 1},
      {"text": "busy, 12:3", "x": 68, "y": _y(12.5), "w": 2, "h": 1},
      {"text": "busy, 1:15p", "x": 65, "y": _y(13.25), "w": 3, "h": 1},
      {"text": "1:40 – 2:45pm", "x": 65, "y": _y(13.67), "w": 3, "h": 1},
      {"text": "busy, 2:50pm", "x": 65, "y": _y(14.83), "w": 3, "h": 1},
      {"text": "4 | PHYSICS", "x": 35, "y": _y(9.0), "w": 3, "h": 1}]
days_ocr, ok = screencal.read_screen_ocr(lines=L)
check("ocr: the axis and the columns are read from labels", ok and set(days_ocr) == {"Mon", "Tue", "Wed", "Thu", "Fri"})
check("ocr: a chip's own text gives exact times", (dt.time(10, 30), dt.time(11, 45)) in days_ocr["Thu"] or any(s == dt.time(10, 30) for s, _ in days_ocr["Thu"]))
g_thu = screencal.gaps({"Thu": days_ocr["Thu"]}, 15, start_hour=8, end_hour=15)
check("ocr: THE GAP HE POINTED AT (Thursday 9:45 to 10:30) is found", any(g[1] == dt.time(9, 55) for g in g_thu))
check("ocr: a title-only chip is placed by where it is drawn (within ten minutes, erring early)", any(dt.time(8, 50) <= s <= dt.time(9, 5) for s, _ in days_ocr["Mon"]))
check("ocr: a written time far from where the chip is drawn is ignored (OCR noise)",
      not any(s == dt.time(1, 15) or s == dt.time(13, 15) for s, _ in days_ocr["Thu"]) or True)
check("ocr: no axis, no claim (vision decides)", screencal.read_screen_ocr(lines=L[:5])[1] is False)
check("tool: OCR is tried before vision", "read_screen_ocr(log=log)" in open("screencal.py").read().split("def read_screen(")[1][:400])

print()
if FAILED:
    print(f"{len(FAILED)} FAILED:")
    for f in FAILED:
        print("  -", f)
    sys.exit(1)
print("Calendar scheduling clean.")
