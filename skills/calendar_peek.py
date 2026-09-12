"""Skill: calendar_peek — reads the macOS Calendar and answers the user.
Two modes:
  DAY:    "check my calendar", "what do I have tomorrow" -> that day's events.
  SEARCH: "when's my flight back", "tell me the dates of my India trip" ->
          finds matching events across the next ~4 months and speaks the DATES.
          (The original skill only knew today/tomorrow, so "when's my flight"
          got "your calendar's clear today" — wrong scope, now fixed.)

Read-only: never creates or deletes events. Uses EventKit (milliseconds; the
AppleScript 'whose' filter took ~55s). EventKit imports are lazy and guarded so
the skill always loads and fails honest, never crashes.
NOTE: EventKit sees calendars synced INTO macOS Calendar. If a Google calendar
isn't synced on this Mac, search says so honestly instead of "nothing found".
"""

import datetime
import re

NAME = "calendar_peek"
DESCRIPTION = ("Reads the calendar aloud: today/tomorrow's events, or searches "
               "the next months by name ('when is my flight back', 'dates of "
               "my India trip', 'check my calendar').")

SEARCH_DAYS = 130   # how far ahead search mode looks

# the calendar-ish nouns that clearly mean "my schedule"
_CALWORD = re.compile(r"\b(calendar|schedule|agenda|meetings?|appointments?)\b", re.I)
# opening the app or making/removing/fixing events is NOT a "check"
# Words that mean "change my calendar", which this skill deliberately does not
# do. It only refused add/create/put, so "book a dentist appointment next
# Tuesday" and "schedule lunch Thursday" were caught here and answered as if
# they were questions about the day — the skill read the calendar out and
# nothing got booked. Neo can genuinely create events now (agenda.py, via the
# add_to_calendar tool), so anything that means WRITE has to fall through to
# the brain.
_NOTCHECK = re.compile(
    r"\b(add|create|put|delete|remove|new|make|set up|fix|"
    r"pencil( it)? in|block (out|off)|move|reschedule|cancel|invite)\b", re.I)

# "book" and "schedule" are verbs here and nouns two words away: "schedule
# lunch on Thursday" is a write, "what's on my schedule" is a read, and one
# blunt word list treats them the same — which broke the skill's own self_test
# and, because the loader skips a skill that fails it, silently removed the
# calendar from Neo entirely. So these two only count when something owns them
# (a possessive or article in front means noun) AND a thing to book follows.
_WRITE_VERB = re.compile(
    r"(?<!my )(?<!the )(?<!your )(?<!their )(?<!her )(?<!our )"
    r"\b(book|schedule)\b\s+\w", re.I)
# "sleep schedule", "workout schedule" etc. are habits, not the calendar
_NOTCAL = re.compile(r"\b(sleep|workout|gym|study|practice|training|posting|"
                     r"content|release|upload) schedule\b", re.I)
# day-oriented phrasings that mean plans without the word "calendar"
# "what do I have tomorrow" was covered and "DO I have anything tomorrow" was
# not, which is the more natural way to ask it — it fell through to the brain,
# which has no way to read a calendar, so the answer was a shrug.
_HAVE = re.compile(r"\b(?:what (?:do i have|have i got|am i doing|do i got)"
                   r"|do i have (?:anything|much|any(?:thing)? on|plans)"
                   r"|have i got (?:anything|much|plans)"
                   r"|am i (?:free|busy|doing anything))\b"
                   r".{0,20}\b(today|tomorrow|later|this (?:morning|afternoon|evening)|tonight)\b", re.I | re.S)
_ON = re.compile(r"\b(?:anything|what'?s)\s+on\b"
                 r".{0,15}\b(today|tomorrow|later|this (?:morning|afternoon|evening)|tonight)\b", re.I | re.S)
# "when is my flight", "what day do i fly out", "which date am i leaving"
_WHEN = re.compile(r"\bwhen(?:'?s| is| are| am i| do i)\b", re.I)
_WHATDAY = re.compile(r"\b(?:what|which)\s+(?:day|date|dates)\b", re.I)

# words that carry no search meaning — what's left after these ARE the query
_STOP = set("""a an and are back can check coming date dates day days do for
from go going google i im in is it leaving like me my of on our out please so
some stuff tell that the them then there this to trip us what whats when where
which you your neo hey ok okay just really also duration those two numbers
based""".split())
_CALSTOP = set("""calendar schedule agenda meeting meetings appointment
appointments today tomorrow tonight week month""".split())
# travel-speak -> the words that actually appear in event titles
_TERM_MAP = {"fly": "flight", "flying": "flight", "flew": "flight",
             "depart": "flight", "departing": "flight", "departure": "flight",
             "return": "flight", "returning": "flight", "flights": "flight"}


def _when(text):
    return "tomorrow" if "tomorrow" in text.lower() else "today"


def _terms(text):
    """Meaningful search words, travel-speak normalized: 'what day do I fly
    out to India' -> ['flight', 'india']. Empty means plain day-check."""
    words = re.findall(r"[a-z]+", text.lower())
    out = []
    for w in words:
        w = _TERM_MAP.get(w, w)
        if w not in _STOP and w not in _CALSTOP and len(w) > 2 and w not in out:
            out.append(w)
    return out


def _is_day_ask(text):
    low = text.lower()
    return ("today" in low or "tomorrow" in low or "tonight" in low
            or _HAVE.search(low) or _ON.search(low))


def matches(text):
    low = text.lower()
    if _NOTCHECK.search(low) or _WRITE_VERB.search(low) or _NOTCAL.search(low):
        return False
    if _CALWORD.search(low):
        return True
    if _HAVE.search(low) or _ON.search(low):
        return True
    # "when is my flight back" / "what day do i fly out" — a when/what-day
    # question with real content words
    return bool(_WHEN.search(low) or _WHATDAY.search(low)) and bool(_terms(low))


def _sort_events(events):
    """[(time_str, title)] -> sorted copy: all-day first, then by clock time.
    Pure and testable."""
    return sorted(events, key=lambda e: (0, "") if e[0] == "all-day" else (1, e[0]))


def _speak_time(t):
    hh, mm = t.split(":")
    h, m = int(hh), int(mm)
    ampm = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12} {ampm}" if m == 0 else f"{h12}:{mm} {ampm}"


def _speak_date(d, today=None):
    """datetime.date -> 'Tuesday, August 4th' (with 'today'/'tomorrow' shortcuts)."""
    today = today or datetime.date.today()
    if d == today:
        return "today"
    if d == today + datetime.timedelta(days=1):
        return "tomorrow"
    day = d.day
    suf = "th" if 11 <= day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return d.strftime("%A, %B ") + f"{day}{suf}"


def _phrase(event):
    t, title = event
    if t == "all-day":
        return f"{title}, all day"
    return f"{title} at {_speak_time(t)}"


def _summarize(events, when):
    events = _sort_events(events)
    if not events:
        return f"Your calendar's clear {when}. Nothing scheduled."
    n = len(events)
    if n == 1:
        return f"One thing {when}: {_phrase(events[0])}."
    head = "; ".join(_phrase(e) for e in events[:3])
    extra = "" if n <= 3 else f", plus {n - 3} more"
    return f"You've got {n} things {when}: {head}{extra}."


def _match_events(events, terms, today=None):
    """Filter (date, time, title) rows whose title hits any search term. Pure."""
    out = []
    for d, t, title in events:
        low = title.lower()
        if any(term in low for term in terms):
            out.append((d, t, title))
    return sorted(out, key=lambda e: e[0])


def _rescue_text(ans):
    """Pure: pick the spoken line after a browser-rescue. A real vision answer
    passes through; anything weak falls back to an honest 'it's on screen'."""
    a = (ans or "").strip()
    bad = ("couldn't", "can't see", "cannot see", "unable", "not sure what",
           "don't see", "unclear")
    if len(a) > 15 and not any(b in a.lower() for b in bad):
        return a
    return ("I've opened Google Calendar on your screen — the dates are right "
            "there. Say 'have Claude install the EventKit package in the neo "
            "project' and I'll read it directly next time, no browser.")


def _browser_rescue(ctx, text):
    """The no-dead-end path: calendar unreadable here (EventKit missing,
    denied, or nothing synced) -> open Google Calendar and READ IT with Neo's
    eyes, so the user still gets their answer instead of a 'can't'."""
    import subprocess
    subprocess.run(["open", "https://calendar.google.com"], capture_output=True)
    ans = ""
    try:
        client = getattr(ctx, "client", None)
        if client is not None:
            import time
            time.sleep(8)                     # let the page render
            import hands
            q = ("Google Calendar is open on the screen. the user asked: " + text +
                 " — answer their question with the specific dates you can see. "
                 "If the relevant events aren't visible, say which month is "
                 "shown and what IS visible.")
            ans = hands.describe_screen(client, getattr(ctx, "model", ""), q)
    except Exception as e:
        print(f"[calendar_peek] rescue vision failed: {e}")
    return _rescue_text(ans)


def _clean_title(t):
    """'Flight to London (BA 184)' -> 'Flight to London' — codes in parens
    are gibberish out loud."""
    return re.sub(r"\s*\([^)]*\)", "", t).strip()


# the ask wants a TRIP SUMMARY, not an event list
_SPAN = re.compile(r"\b(duration|how long|how many days|overall|total|"
                   r"com(?:e|ing) back|get(?:ting)? back|fly(?:ing)? back|"
                   r"return)\b", re.I)


def _span_summary(found, today=None):
    """First event = leaving, last = coming home, and do the math FOR them:
    'You fly out tomorrow..., you're back Sunday, August 16th... 19 days.'"""
    d0, t0, ti0 = found[0]
    d1, t1, ti1 = found[-1]
    days = (d1 - d0).days
    mid = f", with {len(found) - 2} connecting flights along the way" if len(found) > 2 else ""
    out = f"You fly out {_speak_date(d0, today)} on the {_clean_title(ti0).lower()}"
    if t0 != "all-day":
        out += f" at {_speak_time(t0)}"
    out += f", and you're back {_speak_date(d1, today)} on the {_clean_title(ti1).lower()}"
    out += f"{mid}. All in, you're gone about {days} days."
    return out


def _summarize_search(found, terms, today=None, span=False):
    """Speak the DATES of matching events. span=True -> the trip summary."""
    if span and len(found) >= 2 and found[0][0] != found[-1][0]:
        return _span_summary(found, today)
    bits = []
    for d, t, title in found[:4]:
        when = _speak_date(d, today)
        prep = "" if when in ("today", "tomorrow") else "on "   # never "on tomorrow"
        bits.append(f"{_clean_title(title)} {prep}{when}"
                    + ("" if t == "all-day" else f" at {_speak_time(t)}"))
    extra = "" if len(found) <= 4 else f", plus {len(found) - 4} more"
    return "Here's what I found: " + "; ".join(bits) + extra + "."


def _ensure_access(store, EKEntityTypeEvent):
    """Return True if we can read events, requesting permission once if the
    user hasn't decided yet. Bounded so it never hangs Neo."""
    from EventKit import EKEventStore
    status = EKEventStore.authorizationStatusForEntityType_(EKEntityTypeEvent)
    if status in (3, 4):          # authorized / full access
        return True
    if status != 0:               # denied or restricted — no dialog will help
        return False
    # notDetermined: ask through access.py, which puts the prompt on the
    # main thread and waits for the click (a request from this worker
    # thread never showed a prompt at all).
    import access
    return bool(access.request_eventkit(0))


def _collect_range(offset_days, span_days):
    """Events from (today+offset) spanning span_days. Returns
    ([(date, time_str|'all-day', title)], error_code)."""
    try:
        from EventKit import EKEventStore, EKEntityTypeEvent
        from Foundation import NSDate
    except Exception:
        return [], "no_eventkit"
    store = EKEventStore.alloc().init()
    if not _ensure_access(store, EKEntityTypeEvent):
        return [], "denied"
    base = (datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            + datetime.timedelta(days=offset_days))
    start_ts = base.timestamp()
    start = NSDate.dateWithTimeIntervalSince1970_(start_ts)
    end = NSDate.dateWithTimeIntervalSince1970_(start_ts + span_days * 86400)
    pred = store.predicateForEventsWithStartDate_endDate_calendars_(start, end, None)
    matched = store.eventsMatchingPredicate_(pred) or []
    events = []
    for ev in matched:
        title = (ev.title() or "Untitled event").strip()
        lt = datetime.datetime.fromtimestamp(ev.startDate().timeIntervalSince1970())
        if ev.isAllDay():
            events.append((lt.date(), "all-day", title))
        else:
            events.append((lt.date(), lt.strftime("%H:%M"), title))
    return events, None


def handle(text, ctx):
    try:
        terms = _terms(text)
        # SEARCH: named things ("flight", "india trip") — dates matter, not today
        if terms and not _is_day_ask(text):
            events, err = _collect_range(0, SEARCH_DAYS)
            if err:                      # EventKit missing/denied -> eyes, not "can't"
                return _browser_rescue(ctx, text)
            found = _match_events(events, terms)
            if not found:                # nothing synced here -> eyes, not "can't"
                return _browser_rescue(ctx, text)
            return _summarize_search(found, terms, span=bool(_SPAN.search(text)))
        # DAY: today/tomorrow rundown
        when = _when(text)
        events, err = _collect_range(1 if when == "tomorrow" else 0, 1)
        if err:
            return _browser_rescue(ctx, text)
        return _summarize([(t, title) for _, t, title in events], when)
    except Exception as e:
        print(f"[calendar_peek] handle error: {e}")
        return "Something went wrong reading your calendar. Try again in a moment."


def self_test():
    # matches — positives (day mode)
    ok = matches("check my calendar")
    ok = ok and matches("what's on my schedule")
    ok = ok and matches("any meetings today")
    ok = ok and matches("what do I have tomorrow")
    ok = ok and matches("what's on today")
    ok = ok and matches("what's my agenda")
    # matches — positives (search mode: the flight-dates bug, BOTH phrasings)
    ok = ok and matches("go to my google calendar and tell me the dates of my flight")
    ok = ok and matches("when is my flight back")
    ok = ok and matches("what day i fly out to india and what day i come back")
    ok = ok and matches("which date am i leaving for bangalore")
    ok = ok and matches("check my calendar for the india trip dates")
    # matches — negatives (creation, habits, unrelated speech)
    ok = ok and not matches("i need to fix my sleep schedule")
    ok = ok and not matches("help me plan my workout schedule")
    for read in ("do i have anything tomorrow",
                 "do i have much on today",
                 "am i free this afternoon",
                 "am i busy tomorrow",
                 "have i got anything on tonight"):
        ok = ok and matches(read)
    ok = ok and not matches("add a meeting to my calendar")
    # Writing the calendar belongs to the brain, not here. Every one of these
    # used to be swallowed and answered as a lookup.
    for write in ("book a dentist appointment next tuesday at three",
                  "schedule lunch with priya on thursday",
                  "block out friday morning",
                  "pencil in a call for tomorrow",
                  "move my meeting to wednesday",
                  "cancel the thing on friday",
                  "reschedule tuesday's appointment"):
        ok = ok and not matches(write)
    ok = ok and not matches("what's the weather today")
    ok = ok and not matches("set a timer for ten minutes")
    ok = ok and not matches("play some music")
    # term extraction: filler drops, content stays, travel-speak normalizes
    ok = ok and _terms("tell me the dates of my flight back") == ["flight"]
    ok = ok and "india" in _terms("when is the india trip")
    ok = ok and _terms("what day i fly out to india") == ["flight", "india"]
    ok = ok and "flight" in _terms("when am i returning")
    # search matching + date speech (pure, no EventKit)
    today = datetime.date(2026, 7, 26)
    rows = [(datetime.date(2026, 7, 28), "18:40", "Flight to Bangalore"),
            (datetime.date(2026, 8, 14), "02:10", "Flight home BLR-JFK"),
            (datetime.date(2026, 8, 1), "all-day", "Cousin's birthday")]
    hits = _match_events(rows, ["flight"])
    ok = ok and len(hits) == 2 and hits[0][2] == "Flight to Bangalore"
    said = _summarize_search(hits, ["flight"], today)
    ok = ok and "Flight to Bangalore on Tuesday, July 28th" in said
    ok = ok and "August 14th" in said
    # TRIP SUMMARY mode — their real 4-leg itinerary, synthesized not listed
    legs = [(datetime.date(2026, 7, 28), "17:50", "Flight to London (BA 184)"),
            (datetime.date(2026, 7, 29), "09:55", "Flight to Bengaluru (BA 131)"),
            (datetime.date(2026, 8, 16), "02:40", "Flight to London (BA 130)"),
            (datetime.date(2026, 8, 16), "11:35", "Flight to New York (BA 173)")]
    trip = _summarize_search(legs, ["flight"], datetime.date(2026, 7, 27), span=True)
    ok = ok and trip.startswith("You fly out tomorrow on the flight to london")
    ok = ok and "you're back Sunday, August 16th" in trip
    ok = ok and "about 19 days" in trip
    ok = ok and "BA" not in trip and "(" not in trip     # no codes read aloud
    ok = ok and "2 connecting flights" in trip
    ok = ok and _SPAN.search("what day am i getting back and the overall duration")
    ok = ok and not _SPAN.search("whats on my calendar")
    # list mode never says "on tomorrow" and strips codes
    one = _summarize_search([(datetime.date(2026, 7, 28), "17:50",
                              "Flight to London (BA 184)")], ["flight"],
                            datetime.date(2026, 7, 27))
    ok = ok and " on tomorrow" not in one and "BA" not in one
    # rescue text: real vision answers pass, weak ones fall back honest
    ok = ok and _rescue_text("Your flight to Bangalore is Tuesday July 28th.").startswith("Your flight")
    ok = ok and "opened Google Calendar" in _rescue_text("")
    ok = ok and "opened Google Calendar" in _rescue_text("I couldn't see anything relevant")
    ok = ok and _speak_date(datetime.date(2026, 7, 26), today) == "today"
    ok = ok and _speak_date(datetime.date(2026, 7, 27), today) == "tomorrow"
    ok = ok and _speak_date(datetime.date(2026, 8, 1), today) == "Saturday, August 1st"
    ok = ok and _speak_date(datetime.date(2026, 8, 3), today) == "Monday, August 3rd"
    # day-mode plumbing (unchanged behavior)
    unsorted = [("15:30", "Dentist"), ("all-day", "Birthday"), ("09:00", "Standup")]
    ok = ok and _sort_events(unsorted) == [("all-day", "Birthday"),
                                           ("09:00", "Standup"), ("15:30", "Dentist")]
    ok = ok and _speak_time("09:00") == "9 AM"
    ok = ok and _speak_time("15:30") == "3:30 PM"
    ok = ok and "clear today" in _summarize([], "today")
    ok = ok and _summarize([("09:00", "Standup")], "today") == "One thing today: Standup at 9 AM."
    many = [("09:00", "A"), ("10:00", "B"), ("11:00", "C"), ("12:00", "D")]
    ok = ok and "plus 1 more" in _summarize(many, "tomorrow")
    ok = ok and _when("what do I have tomorrow") == "tomorrow"
    return bool(ok)


if __name__ == "__main__":
    print("self_test:", self_test())
