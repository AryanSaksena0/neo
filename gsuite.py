"""
gsuite.py — Google Docs, Slides, Sheets, Gmail and Calendar, with no API.

Every one of these is a URL Google itself publishes. Opened in the person's
own browser they land in the person's own account, already signed in, with
nothing to verify and no key to mint:

  docs.google.com/document/create?title=…        a new Doc, named
  docs.google.com/presentation/create?title=…    a new Slides deck
  docs.google.com/spreadsheets/create?title=…    a new Sheet
  mail.google.com/mail/?view=cm&to=…&su=…&body=… a Gmail compose, filled in
  calendar.google.com/calendar/r/eventedit?…     an event editor with the
                                                 guests added — and Google's
                                                 own "Find a time" tab, which
                                                 sees THEIR calendars

Nothing here sends, saves or invites on its own: the person is looking at
the filled-in thing in their own browser and presses the button. That is the
same line mail.py draws, for the same reason.

The pure parts (the URL builders, the free-slot finder) are tested; the
impure part is one `open`.
"""

import datetime as dt
import urllib.parse

WORK_START, WORK_END = 9, 17           # "within the school/work day"


def _q(params):
    return urllib.parse.urlencode({k: v for k, v in params.items() if v}, quote_via=urllib.parse.quote)


def doc_url(kind, title=""):
    """kind: doc | slides | sheet. Pure."""
    path = {"doc": "document", "slides": "presentation", "sheet": "spreadsheets"}[kind]
    q = _q({"title": title[:200]}) if title else ""
    return f"https://docs.google.com/{path}/create" + (f"?{q}" if q else "")


def gmail_compose_url(to="", subject="", body="", cc=""):
    """A Gmail compose window, filled in. Pure."""
    return "https://mail.google.com/mail/?view=cm&fs=1&" + _q(
        {"to": to, "cc": cc, "su": subject[:250], "body": body[:6000]})


def _gcal_stamp(t):
    return t.strftime("%Y%m%dT%H%M%S")


def calendar_event_url(title, start, end, guests=(), details="", location=""):
    """Google Calendar's event editor with guests prefilled. Pure."""
    return "https://calendar.google.com/calendar/u/0/r/eventedit?" + _q({
        "text": title[:200],
        "dates": f"{_gcal_stamp(start)}/{_gcal_stamp(end)}",
        "add": ",".join(g for g in guests if g),
        "details": details[:1000],
        "location": location[:200],
    })


import re as _re


def parse_hour_bound(text):
    """'after 10', 'after 10am', 'not before 10:30', 'before 3pm', 'afternoon',
    'morning' -> (earliest_hour_float or None, latest_hour_float or None). Pure."""
    t = str(text or "").lower()
    lo = hi = None

    def hour_of(m):
        h = int(m.group(1)); mnt = int(m.group(2) or 0); ap = (m.group(3) or "").replace(".", "")
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        if not ap and h < 7:          # "after 3" in a working day means 3pm
            h += 12
        return h + mnt / 60.0

    m = _re.search(r"(?:after|from|not before|later than|past)\s+(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?", t)
    if m:
        lo = hour_of(m)
    m = _re.search(r"(?:before|by|no later than|until|not after)\s+(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?", t)
    if m:
        hi = hour_of(m)
    if "afternoon" in t and lo is None:
        lo = 12.0
    if "morning" in t and hi is None:
        hi = 12.0
    # "within the school day" / "during school" — a school day, not an office
    if _re.search(r"\b(school day|during school|school hours|in school)\b", t):
        lo = 8.0 if lo is None else lo
        hi = 15.5 if hi is None else min(hi, 15.5)
    if _re.search(r"\b(work day|working day|work hours|office hours|business hours)\b", t):
        lo = 9.0 if lo is None else lo
        hi = 17.0 if hi is None else min(hi, 17.0)
    return lo, hi


def free_slots(busy, day_from, days=5, minutes=15, start_hour=WORK_START,
               end_hour=WORK_END, now=None, limit=5, exclude=()):
    """Slots of `minutes` inside working hours on weekdays where THIS person
    is free, given their busy intervals [(start, end)]. Pure.

    Other people's calendars are not on this Mac; Google's editor shows
    those once the guests are added. So the slot is a proposal, not a claim."""
    now = now or dt.datetime.now()
    busy = sorted((s, e) for s, e in busy if e > s)
    # a slot they already turned down is busy, as far as proposing goes
    busy += [(s, e) for s, e in exclude]
    busy.sort()
    out = []
    for d in range(days):
        day = day_from + dt.timedelta(days=d)
        if day.weekday() >= 5:
            continue
        sh, sm = int(start_hour), int(round((start_hour - int(start_hour)) * 60))
        eh, em = int(end_hour), int(round((end_hour - int(end_hour)) * 60))
        t = dt.datetime.combine(day, dt.time(sh, sm))
        end_of_day = dt.datetime.combine(day, dt.time(eh, em))
        while t + dt.timedelta(minutes=minutes) <= end_of_day:
            slot_end = t + dt.timedelta(minutes=minutes)
            if slot_end <= now:
                t += dt.timedelta(minutes=15)
                continue
            clash = next((e for s, e in busy if s < slot_end and e > t), None)
            if clash:
                # jump to the end of the clash, rounded up to the quarter hour
                m = (clash.minute + 14) // 15 * 15
                t = clash.replace(minute=0, second=0, microsecond=0) + dt.timedelta(minutes=m)
                continue
            out.append((t, slot_end))
            if len(out) >= limit:
                return out
            t = end_of_day       # one slot per day is enough to propose
    return out


# --------------------------------------------------------------------------- #
# Reading Google Calendar itself, through Neo's browser (day pages).
# --------------------------------------------------------------------------- #
_TIME = r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?"
_RANGE = _re.compile(_TIME + r"\s*(?:–|-|to)\s*" + _TIME, _re.I)
_SINGLE = _re.compile(r"(?<![\d:])" + _TIME + r"(?![\d:])", _re.I)
DEFAULT_CHIP_MIN = 45        # a chip that shows only a start time (a short class)


def _clock(h, m, ap, ap_hint=None):
    h = int(h); m = int(m or 0); ap = (ap or ap_hint or "").lower()
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if not ap and h < 7:
        h += 12
    return h, m


def parse_day_page(text, day):
    """Busy intervals for ONE day from the text of Google Calendar's day view.
    Pure. Handles "10:30 – 11:35am", "10:30am to 11:35am", and a chip that
    only shows "8:40am" (DEFAULT_CHIP_MIN long)."""
    out = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or "GMT" in line:
            continue
        m = _RANGE.search(line)
        if m:
            h1, m1, a1, h2, m2, a2 = m.groups()
            H2, M2 = _clock(h2, m2, a2)
            H1, M1 = _clock(h1, m1, a1, ap_hint=a2)
            s = dt.datetime.combine(day, dt.time(H1 % 24, M1)); e = dt.datetime.combine(day, dt.time(H2 % 24, M2))
            if e > s:
                out.append((s, e))
            continue
        # a chip showing only its start: the time itself must look like a
        # clock ("8:40am", "1:15pm"), never a bare number like a room "140"
        # or the "11" in "Advisory 11"
        m = next((x for x in _SINGLE.finditer(line) if x.group(2) or x.group(3)), None)
        if m and (m.group(3) or _re.search(r"[A-Za-z]{3}", line)):
            H, M = _clock(m.group(1), m.group(2), m.group(3))
            s = dt.datetime.combine(day, dt.time(H % 24, M))
            out.append((s, s + dt.timedelta(minutes=DEFAULT_CHIP_MIN)))
    # merge overlaps
    out.sort()
    merged = []
    for s, e in out:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def google_busy(day_from, days=5, log=print):
    """(busy, ok). Reads each weekday's day view in Neo's own browser. ok is
    False when the browser isn't signed in (a login wall) — then the caller
    must not pretend to know the week."""
    try:
        import webdrive
        if not webdrive.available():
            return [], False
    except Exception:
        return [], False
    busy = []
    for d in range(days):
        day = day_from + dt.timedelta(days=d)
        if day.weekday() >= 5:
            continue
        url = f"https://calendar.google.com/calendar/u/0/r/day/{day.year}/{day.month}/{day.day}"
        text, problem = webdrive.open_and_read(url, log=log)
        if problem:
            return [], False
        busy += parse_day_page(text, day)
    return busy, True


def my_busy(day_from, days=5):
    """This person's events as (start, end) from EventKit, only when access is
    already granted. [] otherwise — never prompts. See busy_week for the
    honest wrapper."""
    try:
        import perms
        if perms.calendar() is not True:
            return []
        from EventKit import EKEventStore
        from Foundation import NSDate
        store = EKEventStore.alloc().init()
        start = dt.datetime.combine(day_from, dt.time(0, 0))
        end = start + dt.timedelta(days=days + 1)
        pred = store.predicateForEventsWithStartDate_endDate_calendars_(
            NSDate.dateWithTimeIntervalSince1970_(start.timestamp()),
            NSDate.dateWithTimeIntervalSince1970_(end.timestamp()), None)
        out = []
        for e in (store.eventsMatchingPredicate_(pred) or []):
            if e.isAllDay():
                continue
            out.append((dt.datetime.fromtimestamp(e.startDate().timeIntervalSince1970()),
                        dt.datetime.fromtimestamp(e.endDate().timeIntervalSince1970())))
        return out
    except Exception:
        return []


def busy_week(day_from, days=5, log=print):
    """(busy, source). source: 'mac' (EventKit), 'google' (Neo's browser),
    or None — in which case NOTHING is known and no slot may be called
    'free'. An empty Mac calendar in a working week is treated as unknown:
    a person with zero events Monday to Friday is rarer than a calendar
    that simply isn't on this Mac."""
    try:
        import perms
        if perms.calendar() is True:
            b = my_busy(day_from, days)
            if b:
                return b, "mac"
    except Exception:
        pass
    b, ok = google_busy(day_from, days, log=log)
    if ok:
        return b, "google"
    return [], None


def next_monday(today=None):
    today = today or dt.date.today()
    return today + dt.timedelta(days=(7 - today.weekday()) % 7 or 7)


def open_in_browser(url):
    import subprocess
    subprocess.run(["open", url], capture_output=True)
    return True
