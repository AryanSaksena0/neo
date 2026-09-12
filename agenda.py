"""
agenda.py — putting things ON the calendar.

`skills/calendar_peek.py` reads the calendar and deliberately refuses to write:
its matcher rejects "add", "create", "new", "set up". So "put lunch with
Priya on Thursday at one" fell through to the brain, which had no way to do
it and said something vague. This is the other half.

Two decisions worth knowing:

**The model turns words into a time; it does not do the arithmetic.** It gets
today's date and weekday and returns a plain date and clock time. Everything
after that — resolving "next Tuesday", applying a default duration, deciding
that "at 1" means the afternoon — is done here in Python where it can be
tested. Models are as bad at date arithmetic as they are at geometry, and this
is the same fix that worked there: make the model make a CHOICE, and do the
maths in code.

**Nothing is saved without reading it back.** `create()` saves the event, then
fetches it out of EventKit by identifier and reports what the calendar actually
holds. If Neo says "Thursday at one", it is because the calendar says Thursday
at one — not because that is what was asked for.
"""

import datetime
import json
import os
import re

DEFAULT_MINUTES = int(os.getenv("NEO_EVENT_MINUTES", "60"))
# A working day. "At 8" means the morning, "at 1" means the afternoon.
DAY_START, DAY_END = 7, 22


# --------------------------------------------------------------------------- #
# Access. Same shape as calendar_peek's, but asking for WRITE.
# --------------------------------------------------------------------------- #
def _store():
    """(store, EKEntityTypeEvent) or (None, reason)."""
    try:
        from EventKit import EKEventStore, EKEntityTypeEvent
    except Exception:
        return None, "no_eventkit"
    store = EKEventStore.alloc().init()
    status = EKEventStore.authorizationStatusForEntityType_(EKEntityTypeEvent)
    if status == 4:
        # writeOnly: can add, cannot read back. Refusing is the honest move —
        # an unverified save is exactly what this module exists to avoid.
        return None, "write_only"
    if status != 3:
        if status != 0:
            return None, "denied"
        import access
        if not access.request_eventkit(0):        # the prompt, on the main thread
            return None, "denied"
    return store, EKEntityTypeEvent


def writable_calendars():
    store, kind = _store()
    if store is None:
        return []
    return [c for c in (store.calendarsForEntityType_(kind) or [])
            if c.allowsContentModifications()]


# --------------------------------------------------------------------------- #
# Words -> a real datetime. Pure, so it is testable without a calendar.
# --------------------------------------------------------------------------- #
_SCHEMA = ("Return ONLY JSON, no prose, no code fence:\n"
           '{"title": "...", "date": "YYYY-MM-DD" or "", '
           '"time": "HH:MM" 24-hour or "", "minutes": 60, '
           '"all_day": false, "location": "", "clear": true}\n'
           'Set "clear" false if the request does not actually name an event.\n'
           'Leave "date" empty if no day is given at all. Leave "time" empty '
           'for an all-day thing. "title" is what goes ON the calendar — '
           '"Lunch with Priya", not "add lunch with Priya to my calendar".')


def _today():
    return datetime.date.today()


def normalise(fields, today=None, default_minutes=DEFAULT_MINUTES):
    """Take the model's fields and make a real (title, start, end, all_day).

    Returns (event_dict, problem). Exactly one of them is None.
    """
    today = today or _today()
    title = (fields.get("title") or "").strip()
    if not title or not fields.get("clear", True):
        return None, "unclear"
    if len(title) > 120:
        title = title[:117].rstrip() + "…"

    raw_date = (fields.get("date") or "").strip()
    if not raw_date:
        return None, "no_date"
    try:
        day = datetime.date.fromisoformat(raw_date)
    except ValueError:
        return None, "bad_date"
    # A model that is asked for "next Tuesday" and gives last Tuesday has
    # dropped a year or got the weekday wrong. Refusing beats a wrong entry.
    if day < today - datetime.timedelta(days=1):
        return None, "past"
    if day > today + datetime.timedelta(days=730):
        return None, "far"

    all_day = bool(fields.get("all_day"))
    raw_time = (fields.get("time") or "").strip()
    if all_day or not raw_time:
        return ({"title": title, "start": datetime.datetime.combine(
            day, datetime.time(0, 0)), "end": datetime.datetime.combine(
            day, datetime.time(0, 0)) + datetime.timedelta(days=1),
            "all_day": True,
            "location": (fields.get("location") or "").strip()}, None)

    m = re.match(r"^(\d{1,2}):(\d{2})$", raw_time)
    if not m:
        return None, "bad_time"
    hh, mm = int(m.group(1)), int(m.group(2))
    if hh > 23 or mm > 59:
        return None, "bad_time"
    # "one o'clock" is 13:00 to a person and 01:00 to a careless parser.
    # Nobody means 4am when they say "put lunch at four".
    if hh < DAY_START and hh + 12 <= DAY_END:
        hh += 12

    try:
        minutes = int(fields.get("minutes") or default_minutes)
    except (TypeError, ValueError):
        minutes = default_minutes
    minutes = max(5, min(minutes, 12 * 60))

    start = datetime.datetime.combine(day, datetime.time(hh, mm))
    return ({"title": title, "start": start,
             "end": start + datetime.timedelta(minutes=minutes),
             "all_day": False,
             "location": (fields.get("location") or "").strip()}, None)


def parse(text, client, model=None, today=None, log=print):
    """Words -> (event_dict, problem)."""
    today = today or _today()
    prompt = (f"Today is {today.isoformat()}, a {today.strftime('%A')}.\n"
              f"Turn this into a calendar event: {text!r}\n\n{_SCHEMA}")
    import providers
    raw, _m = providers.generate_text(client, "chat", prompt, log=log)
    if not raw:
        log("[agenda] couldn't parse the time: no model answered")
        return None, "no_model"
    try:
        import deck
        fields = deck._loads(raw)
    except Exception:
        fields = None
    if not isinstance(fields, dict):
        return None, "unclear"
    return normalise(fields, today=today)


# --------------------------------------------------------------------------- #
# Saving, and reading it back.
# --------------------------------------------------------------------------- #
def speak_when(start, all_day, today=None):
    """'Thursday the fourth at one' — the way it would be said out loud."""
    today = today or _today()
    d = start.date()
    delta = (d - today).days
    if delta == 0:
        day = "today"
    elif delta == 1:
        day = "tomorrow"
    elif 2 <= delta < 7:
        day = start.strftime("%A")
    elif 7 <= delta < 14:
        day = "next " + start.strftime("%A")
    else:
        day = start.strftime("%A the %d").replace(" 0", " ")
    if all_day:
        return day
    h = start.hour % 12 or 12
    clock = f"{h}:{start.minute:02d}" if start.minute else f"{h}"
    part = "in the morning" if start.hour < 12 else (
        "in the afternoon" if start.hour < 18 else "in the evening")
    return f"{day} at {clock} {part}"


def create(event, calendar=None, log=print):
    """Save it, then read it back. (saved_dict, problem)."""
    store, kind = _store()
    if store is None:
        return None, kind
    try:
        from EventKit import EKEvent, EKSpanThisEvent
        from Foundation import NSDate
    except Exception:
        return None, "no_eventkit"

    cal = None
    if calendar:
        want = calendar.strip().lower()
        for c in (store.calendarsForEntityType_(kind) or []):
            if c.allowsContentModifications() and want in c.title().lower():
                cal = c
                break
    cal = cal or store.defaultCalendarForNewEvents()
    if cal is None or not cal.allowsContentModifications():
        writable = writable_calendars()
        if not writable:
            return None, "no_calendar"
        cal = writable[0]

    ev = EKEvent.eventWithEventStore_(store)
    ev.setTitle_(event["title"])
    ev.setCalendar_(cal)
    ev.setAllDay_(bool(event["all_day"]))
    ev.setStartDate_(NSDate.dateWithTimeIntervalSince1970_(event["start"].timestamp()))
    ev.setEndDate_(NSDate.dateWithTimeIntervalSince1970_(event["end"].timestamp()))
    if event.get("location"):
        ev.setLocation_(event["location"])

    ok, err = store.saveEvent_span_error_(ev, EKSpanThisEvent, None)
    if not ok:
        log(f"[agenda] save refused: {err}")
        return None, "save_failed"

    # Read it back out of the calendar. Reporting the request instead of the
    # record is how an assistant ends up confidently describing an event that
    # is not there.
    ident = ev.eventIdentifier()
    back = store.eventWithIdentifier_(ident) if ident else None
    if back is None:
        return None, "vanished"
    start = datetime.datetime.fromtimestamp(back.startDate().timeIntervalSince1970())
    return ({"title": back.title(), "start": start,
             "all_day": bool(back.isAllDay()),
             "calendar": back.calendar().title(),
             "id": ident}, None)


TROUBLE = {
    "no_eventkit": "I can't reach the calendar on this machine.",
    "denied": "I don't have permission to touch your calendar yet. Say "
              "'connect calendar' and I'll ask for it.",
    "write_only": "I can only add to your calendar, not read it back, and I'm "
                  "not putting something in that I can't check.",
    "no_calendar": "There's no calendar here I'm allowed to write to.",
    "no_date": "I need a day for that one.",
    "bad_date": "I couldn't work out what day you meant.",
    "past": "That date's already gone by — which day did you mean?",
    "far": "That's further out than I think you meant.",
    "bad_time": "I couldn't work out what time you meant.",
    "unclear": "I'm not sure what to call that one — what should it say?",
    "no_model": "I couldn't work out the time for that just now.",
    "save_failed": "The calendar refused to save it.",
    "vanished": "I saved it but it isn't showing up, so I don't trust it.",
}
