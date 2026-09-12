"""
remind.py — "remind me to…", written into the Mac's own Reminders app.

Same discipline as agenda.py (the calendar): the model turns words into
fields, CODE decides whether they are good enough to act on, EventKit writes
the reminder, and it is READ BACK before Neo says it exists. Nothing is
confirmed that hasn't been seen.

WHEN NEO ASKS FIRST
Not always. A clarifying question on every request is worse than no
assistant. It asks in exactly two cases and acts otherwise:

  - no time at all              "remind me to call mum"           -> "When?"
  - a vague word for the time   "remind me later / sometime"      -> "When?"

"…at six", "…tomorrow morning", "…in twenty minutes", "…on Friday" are clear
enough: it writes the reminder, reads it back, and says when it is set for.
If a different time was meant, they'll say so and it gets moved.

The model gets the current date and weekday and returns a plain date and clock
time; the maths ("in twenty minutes", "a time in the past means tomorrow") is
done here, where it can be tested.
"""

import datetime as dt
import os
import re

TROUBLE = {
    "no_eventkit": "I can't reach Reminders on this machine.",
    "denied": "I don't have permission to touch your reminders yet. Say "
              "'connect reminders' and I'll ask for it.",
    "write_only": "I can only add reminders, not read them back, and I'm not "
                  "setting one I can't check.",
    "no_list": "There's no reminders list here I'm allowed to write to.",
    "no_model": "I couldn't work out the time for that just now.",
    "no_title": "What should the reminder say?",
    "no_time": "When do you want reminding?",
    "vague": "When exactly — a time, or a day?",
    "bad_time": "I couldn't work out what time you meant.",
    "past": "That time's already gone by — when did you mean?",
    "save_failed": "Reminders refused to save it.",
    "vanished": "I saved it but it isn't showing up, so I don't trust it.",
}

# Words that name a time without naming one. The whole reason the clarifying
# question exists.
VAGUE = ("later", "sometime", "some time", "at some point", "eventually",
         "when you can", "whenever", "soon", "in a bit", "in a while",
         "at some stage")

_SCHEMA = ('Return ONLY JSON, no prose, no code fence:\n'
           '{"title": "...", "date": "YYYY-MM-DD" or "", '
           '"time": "HH:MM" 24-hour or "", "minutes_from_now": 0}\n'
           '"title" is what the reminder should SAY — "Call mum", not '
           '"remind me to call mum". Leave "date" and "time" empty if no '
           'time was given at all. For "in N minutes/hours" set '
           '"minutes_from_now" instead of date/time. "tomorrow morning" is '
           '09:00, "afternoon" 15:00, "tonight"/"evening" 20:00.')

DAY_START, DAY_END = 7, 22


def _today():
    return dt.date.today()


def _now():
    return dt.datetime.now()


def is_vague(text):
    """Does the request give a time-shaped word that isn't a time?"""
    low = " " + re.sub(r"[^a-z ]", " ", str(text or "").lower()) + " "
    low = re.sub(r" +", " ", low)
    return any(f" {v} " in low for v in VAGUE)


def normalise(fields, text="", today=None, now=None):
    """Model fields -> (reminder_dict, problem). Pure.

    The reminder has title, when (a datetime, or None), all_day.
    A problem is one of the TROUBLE keys; "no_time" and "vague" are the two
    that mean "ask them", the rest mean "couldn't".
    """
    today = today or _today()
    now = now or _now()
    title = str(fields.get("title") or "").strip().rstrip(".")
    if not title:
        return None, "no_title"
    if len(title) > 120:
        title = title[:117].rstrip() + "…"
    bare = {"title": title, "when": None, "all_day": False}

    # The vague check is on the WORDS, so a model that helpfully invents
    # "15:00" for "later" can't hide the ambiguity.
    if is_vague(text):
        return bare, "vague"

    try:
        rel = int(fields.get("minutes_from_now") or 0)
    except (TypeError, ValueError):
        rel = 0
    if rel > 0:
        when = (now + dt.timedelta(minutes=rel)).replace(second=0, microsecond=0)
        return {"title": title, "when": when, "all_day": False}, None

    raw_date = str(fields.get("date") or "").strip()
    raw_time = str(fields.get("time") or "").strip()
    if not raw_date and not raw_time:
        return bare, "no_time"

    if raw_date:
        try:
            day = dt.date.fromisoformat(raw_date)
        except ValueError:
            return bare, "bad_time"
        if day < today:
            return bare, "past"
    else:
        day = today

    if not raw_time:
        # "on Friday" — a day with no clock. Reminders shows it on that day.
        return {"title": title, "when": dt.datetime.combine(day, dt.time(9, 0)),
                "all_day": True}, None

    m = re.match(r"^(\d{1,2}):(\d{2})$", raw_time)
    if not m:
        return bare, "bad_time"
    hh, mm = int(m.group(1)), int(m.group(2))
    if hh > 23 or mm > 59:
        return bare, "bad_time"
    # "at one" is 13:00 to a person. Nobody wants reminding at 4am.
    if hh < DAY_START and hh + 12 <= DAY_END:
        hh += 12
    when = dt.datetime.combine(day, dt.time(hh, mm))
    # "remind me at six" said at nine in the evening means six tomorrow.
    if not raw_date and when < now - dt.timedelta(minutes=1):
        when += dt.timedelta(days=1)
    elif when < now - dt.timedelta(minutes=1):
        return bare, "past"
    return {"title": title, "when": when, "all_day": False}, None


def parse(text, client, model=None, today=None, now=None, log=print):
    """Words -> (reminder, problem), via the model for the fields."""
    today = today or _today()
    now = now or _now()
    prompt = (f"Now: {now.strftime('%Y-%m-%d %H:%M')}, {today.strftime('%A')}.\n"
              f"Turn this into a reminder: {text!r}\n\n{_SCHEMA}")
    import providers
    raw, _m = providers.generate_text(client, "chat", prompt, log=log)
    if not raw:
        log("[remind] parse failed (no model answered)")
        return None, "no_model"
    try:
        import deck
        fields = deck._loads(raw)
    except Exception:
        fields = None
    if not isinstance(fields, dict):
        return None, "no_model"
    return normalise(fields, text=text, today=today, now=now)


# --------------------------------------------------------------------------- #
# EventKit. Reminders are entity type 1; the calendar (agenda.py) is 0.
# --------------------------------------------------------------------------- #
def _store():
    """(store, EKEntityTypeReminder) or (None, reason)."""
    try:
        from EventKit import EKEventStore, EKEntityTypeReminder
    except Exception:
        return None, "no_eventkit"
    store = EKEventStore.alloc().init()
    status = EKEventStore.authorizationStatusForEntityType_(EKEntityTypeReminder)
    if status == 4:
        return None, "write_only"
    if status != 3:
        if status != 0:
            return None, "denied"
        import access
        if not access.request_eventkit(1):        # the prompt, on the main thread
            return None, "denied"
    return store, EKEntityTypeReminder


def _components(when):
    from Foundation import NSDateComponents
    c = NSDateComponents.alloc().init()
    c.setYear_(when.year); c.setMonth_(when.month); c.setDay_(when.day)
    c.setHour_(when.hour); c.setMinute_(when.minute); c.setSecond_(0)
    return c


def create(rem, log=print):
    """Save it, then read it back. (saved_dict, problem)."""
    store, kind = _store()
    if store is None:
        return None, kind
    try:
        from EventKit import EKReminder, EKAlarm
        from Foundation import NSDate
    except Exception:
        return None, "no_eventkit"

    cal = store.defaultCalendarForNewReminders()
    if cal is None or not cal.allowsContentModifications():
        writable = [c for c in (store.calendarsForEntityType_(kind) or [])
                    if c.allowsContentModifications()]
        if not writable:
            return None, "no_list"
        cal = writable[0]

    r = EKReminder.reminderWithEventStore_(store)
    r.setTitle_(rem["title"])
    r.setCalendar_(cal)
    when = rem.get("when")
    if when is not None:
        r.setDueDateComponents_(_components(when))
        if not rem.get("all_day"):
            # The due date alone shows in the list; the alarm is what makes
            # the Mac actually say something at that time.
            r.addAlarm_(EKAlarm.alarmWithAbsoluteDate_(
                NSDate.dateWithTimeIntervalSince1970_(when.timestamp())))

    ok, err = store.saveReminder_commit_error_(r, True, None)
    if not ok:
        log(f"[remind] save refused: {err}")
        return None, "save_failed"

    ident = r.calendarItemIdentifier()
    back = store.calendarItemWithIdentifier_(ident) if ident else None
    if back is None:
        return None, "vanished"
    comps = back.dueDateComponents()
    back_when = None
    if comps is not None:
        try:
            back_when = dt.datetime(comps.year(), comps.month(), comps.day(),
                                    comps.hour() if comps.hour() >= 0 else 0,
                                    comps.minute() if comps.minute() >= 0 else 0)
        except (ValueError, TypeError):
            back_when = when
    return ({"title": back.title(), "when": back_when,
             "all_day": bool(rem.get("all_day")),
             "list": back.calendar().title(), "id": ident}, None)


def speak(saved, today=None, now=None):
    """'Call mum, tomorrow at 6 in the evening' / '…in 20 minutes'."""
    from agenda import speak_when
    when = saved.get("when")
    if when is None:
        return saved["title"]
    now = now or _now()
    mins = int(round((when - now).total_seconds() / 60))
    if not saved.get("all_day") and 0 < mins <= 90:
        return f"{saved['title']}, in {mins} minute{'s' if mins != 1 else ''}"
    return f"{saved['title']}, {speak_when(when, saved.get('all_day'), today)}"
