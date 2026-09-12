"""
connectors.py — what Neo is connected to, asked for as you go.

Not Slack and YouTube: the things on this Mac that make Neo useful —
Calendar, Reminders, Contacts, Mail, and a Google sign-in for Neo's own
browser. Each connector has three real operations:

  status()   is it connected, read from macOS or the browser, never assumed
  connect()  trigger the actual permission prompt / sign-in, right now
  test()     read something back and SAY what was read — "3 calendars:
             School, Holidays, Birthdays, 22 events next week" — so a
             connection that is granted but empty (the Google calendar not
             synced to the Mac) is caught here, not when a meeting lands
             in a class.

Nothing is assumed from a permission being granted. Granted and empty is
reported as empty, with the fix.
"""

import datetime as dt
import os
import subprocess

INTERNET_ACCOUNTS = "x-apple.systempreferences:com.apple.Internet-Accounts-Settings.extension"

# (key, name, what it's for, recommended, how). Recommended = the ones that
# change what Neo can do every day.
#
# `how` is the honest one, and it exists because the Connect scene used to
# present all seven identically, as if each were one tap. Three of them are:
# macOS raises its own prompt and you press Allow. The other four are not, and
# pretending otherwise is what made this screen feel broken — you press Connect
# expecting a prompt, a System Settings pane opens instead, and you are left to
# work out what to do with it.
#
#   "tap"    macOS raises its own prompt. Press Allow. Genuinely one tap.
#   "signin" a browser or terminal sign-in with your own account.
#   "setup"  needs something configured on this Mac before it can work at all.
#
# Ordered so every one-tap connector comes first: the scene should open with
# the things that just work.
CONNECTORS = [
    ("calendar",  "Calendar",    "What's on your week; finding a time.", True, "tap"),
    ("reminders", "Reminders",   "Remind me to… lands in Reminders.", True, "tap"),
    ("contacts",  "Contacts",    "Who people are: phone numbers, and emails if they're there.", False, "tap"),
    ("claude",    "Claude Code", "The heavy engine: real code, data and multi-step computer work, on your Claude plan.", True, "signin"),
    ("chatgpt",   "ChatGPT",     "The same heavy engine on a ChatGPT plan (OpenAI's Codex). Pick this or Claude.", False, "signin"),
    ("google",    "Google",      "Gmail, Google Calendar, Drive and your directory, through Neo's own browser.", True, "signin"),
    ("mail",      "Mail",        "Reading the inbox and opening drafts in the Mail app.", False, "setup"),
]
RECOMMENDED = [c[0] for c in CONNECTORS if c[3]]
HOW = {c[0]: c[4] for c in CONNECTORS}
HOW_LABEL = {"tap": "one tap", "signin": "sign in", "setup": "needs setup"}


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
def _ek_status(kind):
    try:
        from EventKit import EKEventStore
        return int(EKEventStore.authorizationStatusForEntityType_(kind))
    except Exception:
        return None


def google_signed_in():
    """True/False/None. None = Neo's browser isn't available to ask."""
    try:
        import webdrive
        if not webdrive.available():
            return None
        text, problem = webdrive.open_and_read("https://calendar.google.com/calendar/u/0/r", log=lambda *a: None)
        if problem == "login":
            return False
        return problem is None
    except Exception:
        return None


def status():
    """{key: True | False | None}. None = couldn't tell (framework missing)."""
    import perms
    out = {
        "calendar": _ek_status(0) in (3, 4),
        "reminders": _ek_status(1) in (3, 4),
        "contacts": perms.contacts(),
    }
    try:
        import mail
        out["mail"] = mail.backend() == "mail_app"
    except Exception:
        out["mail"] = None
    out["google"] = False    # never probe the browser on a status call; test() does
    try:
        import heavy
        h = heavy.status()
        out["claude"], out["chatgpt"] = h["claude"], h["codex"]
    except Exception:
        out["claude"] = out["chatgpt"] = None
    return out


# --------------------------------------------------------------------------- #
# connect: the real prompt, now
# --------------------------------------------------------------------------- #
def connect(key, log=print):
    """Trigger the permission prompt / sign-in for one connector. Returns a
    short spoken sentence about what happened (the test() line follows)."""
    key = (key or "").lower().strip()
    if key == "calendar":
        import agenda
        store, why = agenda._store()            # prompts if undecided
        if store is None:
            return _denied("Calendars", why)
        return "Calendar connected."
    if key == "reminders":
        import remind
        store, why = remind._store()
        if store is None:
            return _denied("Reminders", why)
        return "Reminders connected."
    if key == "contacts":
        import contacts
        people, why = contacts.from_contacts()  # prompts if undecided
        if why == "denied":
            return _denied("Contacts", why)
        return "Contacts connected."
    if key == "mail":
        import mail
        if mail.backend() == "mail_app":
            return "Mail is connected."
        subprocess.Popen(["open", INTERNET_ACCOUNTS])
        return ("Mail isn't set up on this Mac. I've opened Internet Accounts: add your "
                "Google or work account there with Mail ticked, and I'm in with no password.")
    if key == "google":
        import webdrive
        if not webdrive.available():
            return "I need Google Chrome on this Mac for that, and I can't find it."
        return webdrive.show_for_login("https://calendar.google.com/calendar/u/0/r", log=log)
    if key in ("claude", "chatgpt"):
        import heavy
        return heavy.connect("claude" if key == "claude" else "codex", log=log)
    return f"I don't have a connector called {key!r}."


def _denied(name, why):
    """Said once, plainly. System Settings opens only for a real denial, and
    at most once in ten minutes — never as a reflex on every failed call."""
    import access
    key = {"Calendars": "calendar", "Reminders": "reminders", "Contacts": "contacts"}[name]
    opened = access.maybe_open_settings(key) if why == "denied" else False
    if opened:
        return (f"macOS has {name} switched off for me. I've opened that pane in System "
                f"Settings; tick Neo there and say 'connect {key}' again.")
    return (f"macOS didn't let me into {name}. It's under Privacy and Security, {name}; "
            f"tick Neo there and say 'connect {key}' again.")


# --------------------------------------------------------------------------- #
# test: read something back
# --------------------------------------------------------------------------- #
def test(key, log=print):
    """What Neo can actually see through this connector, as one honest
    sentence. Never 'connected' without a count."""
    key = (key or "").lower().strip()
    try:
        if key == "calendar":
            return _test_calendar()
        if key == "reminders":
            return _test_reminders()
        if key == "contacts":
            return _test_contacts()
        if key == "mail":
            return _test_mail()
        if key == "google":
            return _test_google(log)
        if key in ("claude", "chatgpt"):
            import heavy
            return heavy.test("claude" if key == "claude" else "codex")
    except Exception as e:
        return f"The {key} check crashed: {type(e).__name__}."
    return ""


def _test_calendar():
    from EventKit import EKEventStore
    from Foundation import NSDate
    if _ek_status(0) not in (3, 4):
        return "Calendar isn't connected."
    store = EKEventStore.alloc().init()
    cals = [c.title() for c in (store.calendarsForEntityType_(0) or [])]
    start = NSDate.dateWithTimeIntervalSinceNow_(0)
    end = NSDate.dateWithTimeIntervalSinceNow_(7 * 86400)
    pred = store.predicateForEventsWithStartDate_endDate_calendars_(start, end, None)
    n = len(store.eventsMatchingPredicate_(pred) or [])
    names = ", ".join(cals[:6]) + ("…" if len(cals) > 6 else "")
    if n == 0:
        return (f"Calendar is connected but EMPTY for the next seven days across {len(cals)} "
                f"calendar(s): {names}. If your real calendar is Google or a school account, "
                "add it in Internet Accounts with Calendars ticked, or say 'connect Google'.")
    return f"Calendar connected: {len(cals)} calendar(s) — {names} — {n} events in the next seven days."


def _test_reminders():
    from EventKit import EKEventStore
    if _ek_status(1) not in (3, 4):
        return "Reminders isn't connected."
    store = EKEventStore.alloc().init()
    lists = [c.title() for c in (store.calendarsForEntityType_(1) or [])]
    return f"Reminders connected: {len(lists)} list(s) — {', '.join(lists[:6])}."


def _test_contacts():
    import contacts
    people, why = contacts.from_contacts()
    if why:
        return f"Contacts isn't connected ({why})."
    with_email = sum(1 for p in people if p.get("emails"))
    return (f"Contacts connected: {len(people)} people, {with_email} with an email address"
            + (". Most are phone-only, so emails will come from Google and the inbox." if with_email < len(people) / 3 else "."))


def _test_mail():
    import mail
    if mail.backend() != "mail_app":
        return "Mail isn't set up on this Mac."
    rows = mail.recent(5)
    return f"Mail connected: I can see the inbox ({len(rows)} recent messages read back)."


def _test_google(log):
    import gsuite
    signed = google_signed_in()
    if signed is None:
        return "Google: I need Chrome on this Mac to sign my own browser in."
    if signed is False:
        return "Google: my browser isn't signed in yet. Say 'connect Google' and I'll bring the sign-in up."
    monday = gsuite.next_monday()
    busy, ok = gsuite.google_busy(monday, 5, log=log)
    if not ok:
        return "Google: signed in, but I couldn't read the calendar page."
    return f"Google connected: I can read your Google Calendar — {len(busy)} busy blocks next week."


# --------------------------------------------------------------------------- #
# Asking as you go. A tool that hits a wall calls need(key, why, resume=text):
# a card appears — "Neo needs your Calendar — Approve / Not now". Approve
# runs connect() (the real prompt), test(), speaks the result, and re-runs
# the request that hit the wall. neo.py installs the two hooks.
# --------------------------------------------------------------------------- #
on_need = None          # neo.py: fn(insight) -> shows the card
_pending = {}           # key -> the request text to re-run after approval


def need(key, why="", resume=""):
    """Raise the card. Returns the sentence for the model to say."""
    name = dict((c[0], c[1]) for c in CONNECTORS).get(key, key)
    if resume:
        _pending[key] = resume
    card = {"key": f"need:{key}", "kind": "need", "urgency": "high",
            "title": f"Neo needs your {name}" + (f" to {why}" if why else ""),
            "detail": f"Approve to connect {name} now.", "connector": key,
            "ok": "Approve", "no": "Not now"}
    try:
        if on_need:
            on_need(card)
    except Exception:
        pass
    purpose = f" to {why}" if why else ""
    return (f"I need access to your {name}{purpose}. There's a card on screen — "
            f"press Approve and I'll carry on. Or say 'connect {key}'.")


def approve(key, log=print):
    """Approve was pressed: connect, test, and hand back (what_to_say, resume_text)."""
    first = connect(key, log=log)
    seen = test(key, log=log)
    ok = status().get(key) is True or "connected" in (seen or "").lower().split(":")[0]
    resume = _pending.pop(key, "") if ok else ""
    return f"{first} {seen}".strip(), resume


def describe():
    """One line per connector, for the doctor and the onboarding."""
    st = status()
    return "\n".join(f"{n:<12} {'connected' if st.get(k) else ('unknown' if st.get(k) is None else 'not connected')}"
                     + ("  (recommended)" if r and not st.get(k) else "")
                     for k, n, _, r, _how in CONNECTORS)
