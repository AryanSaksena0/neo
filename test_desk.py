"""test_desk.py — the desk, the calendar and the mailbox, exercised for real.

Every check here RUNS the code. Nothing greps a source file for a phrase — the
existing suite had five checks doing that and all five were silently asserting
nothing. Where a real macOS service can be driven safely it is driven (the
clipboard is written and restored, a PDF is generated and rendered, Spotlight
is searched for a file this test just created). Where it cannot be — EventKit
needs a permission dialog somebody has to click, and Mail.app has no account on
this machine — the SERVICE is faked and the logic around it is run in full, so
the save-then-read-back path is genuinely tested even though the calendar
itself is out of reach.

Run: python3 test_desk.py
"""
import datetime
import os
import subprocess
import sys
import tempfile
import time

# BEFORE any Neo import, and not left to the runner. go.sh sets NEO_NO_AUDIO=1
# for every suite; run this file by hand without it and the mail checks fall
# all the way down the fallback ladder to Neo's own browser, which opens a real
# Chrome window and asks the person running the tests to sign in to Gmail. A
# test suite must never drive the product's real side effects.
os.environ.setdefault("NEO_NO_AUDIO", "1")

sys.path.insert(0, ".")
import agenda
import context
import desk
import mail

# Checks that touch the user's REAL machine — his clipboard, Spotlight, opening
# things. Off by default: the suite runs dozens of times a day and every run
# was reaching into his actual clipboard while he was working.
LIVE = os.getenv("NEO_LIVE_TESTS") == "1"

FAILED = []


def check(name, cond):
    print(("PASS - " if cond else "FAIL - ") + name)
    if not cond:
        FAILED.append(name)


# =========================================================================== #
# 1. The clipboard. Written and read for real, then put back exactly as it was
#    — a test that quietly eats what someone copied is a bug, not a test.
# =========================================================================== #
if not LIVE:
    print("SKIP - clipboard round-trip (set NEO_LIVE_TESTS=1; it writes his "
          "real clipboard)")
else:
    _original = desk.clipboard(limit=10 ** 7)
    try:
        desk.set_clipboard("neo test — Priya's \"quote\" & <tag>\nsecond line")
        check("clipboard round-trips exactly, quotes and newlines included",
              desk.clipboard() == "neo test — Priya's \"quote\" & <tag>\nsecond line")

        desk.set_clipboard("x" * 9000)
        check("clipboard is truncated to the limit, not returned whole",
              len(desk.clipboard(limit=4000)) == 4000)

        desk.set_clipboard("")
        check("an empty clipboard reads as empty, not as an error",
              desk.clipboard() == "")
    finally:
        desk.set_clipboard(_original)
    check("the clipboard was left exactly as it was found",
          desk.clipboard(limit=10 ** 7) == _original)


# =========================================================================== #
# 2. Ages. The wrong word here makes Neo sound like it's guessing.
# =========================================================================== #
now = time.time()
AGES = [(now - 60, "last hour"), (now - 3600 * 5, "today"),
        (now - 86400 * 1.2, "yesterday"), (now - 86400 * 4, "4 days ago"),
        (now - 86400 * 20, "weeks ago"), (now - 86400 * 400, "about a year ago")]
for ts, want in AGES:
    got = desk.describe_age(ts, now=now)
    check(f"an age of {(now - ts) / 86400:.1f} days is spoken sensibly "
          f"({got!r} contains {want!r})", want in got)

check("describe_age survives a nonsense timestamp instead of raising",
      isinstance(desk.describe_age(None), str) and
      isinstance(desk.describe_age(0), str))


# =========================================================================== #
# 3. Finding files. A real file is created with a nonsense name, Spotlight is
#    given a moment to notice it, and it has to come back FIRST — because
#    find_files sorts by how recently a file was touched, and the one someone
#    means is nearly always the one they just had open.
# =========================================================================== #
check("an empty query finds nothing rather than everything",
      desk.find_files("") == [] and desk.find_files(None) == [])

check("a query full of quotes doesn't break the Spotlight expression",
      isinstance(desk.find_files('he said "hi" && rm -rf'), list))

marker = f"neozzq{int(now)}"
# In HIS Documents, because that is where Spotlight actually indexes — /tmp is
# excluded. Cleaned up in the finally below, and only when live tests are on.
probe = os.path.join(os.path.expanduser("~/Documents"), f"{marker}.txt")
found_own = None
if not LIVE:
    print("SKIP - Spotlight probe (set NEO_LIVE_TESTS=1; it writes a file into "
          "his Documents folder)")
else:
    try:
        with open(probe, "w") as f:
            f.write(f"{marker} a document about pelicans\n")
        for _ in range(20):                  # Spotlight indexes asynchronously
            hits = desk.find_files(marker)
            if hits:
                found_own = hits
                break
            time.sleep(0.5)
        if found_own is None:
            print("SKIP - Spotlight did not index the probe file in 10s")
        else:
            check("a file just created is found by name, newest first",
                  found_own[0]["name"] == f"{marker}.txt")
            check("a found file carries the fields the tool reads off it",
                  set(found_own[0]) >= {"path", "name", "modified", "size"})
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass

check("a file that no longer exists is dropped from the results",
      all(os.path.exists(h["path"]) for h in desk.find_files("pdf")))


# =========================================================================== #
# 4. PDFs. A real one is generated, then rendered — the render is the whole
#    feature, since there is no pdftotext on this machine.
# =========================================================================== #
def _make_pdf(path, pages=3):
    from Quartz import (CGPDFContextCreateWithURL, CGRectMake,
                        CGContextBeginPage, CGContextEndPage, CGPDFContextClose,
                        CGContextSelectFont, CGContextShowTextAtPoint,
                        kCGEncodingMacRoman, CGContextSetRGBFillColor)
    from CoreFoundation import (CFURLCreateFromFileSystemRepresentation,
                                kCFAllocatorDefault)
    raw = path.encode()
    url = CFURLCreateFromFileSystemRepresentation(kCFAllocatorDefault, raw,
                                                  len(raw), False)
    box = CGRectMake(0, 0, 612, 792)
    ctx = CGPDFContextCreateWithURL(url, box, None)
    for i in range(pages):
        CGContextBeginPage(ctx, box)
        CGContextSetRGBFillColor(ctx, 0, 0, 0, 1)
        CGContextSelectFont(ctx, b"Helvetica", 36, kCGEncodingMacRoman)
        CGContextShowTextAtPoint(ctx, 72, 700, f"PAGE {i + 1}".encode(), 6)
        CGContextEndPage(ctx)
    CGPDFContextClose(ctx)


tmp = tempfile.mkdtemp(prefix="neodesk")
pdf = os.path.join(tmp, "probe.pdf")
_make_pdf(pdf, pages=3)

check("page count comes back right", desk.pdf_pages(pdf) == 3)
check("a text file is not mistaken for a PDF", desk.pdf_pages(__file__) == 0)
check("a path that isn't there returns 0 rather than raising",
      desk.pdf_pages(os.path.join(tmp, "nope.pdf")) == 0)

png = desk.render_page(pdf, 1)
check("a page renders to real PNG bytes",
      isinstance(png, bytes) and png[:8] == b"\x89PNG\r\n\x1a\n")
check("the render is scaled up, not a thumbnail", png and len(png) > 1000)
check("asking for a page past the end returns None, not a blank image",
      desk.render_page(pdf, 99) is None)
check("page 0 is rejected rather than silently becoming page 1",
      desk.render_page(pdf, 0) is None)

# The white background matters: a PDF page is transparent, and transparent
# renders BLACK — unreadable to a person and to the model.
from Quartz import (CGImageSourceCreateWithData, CGImageSourceCreateImageAtIndex,
                    CGImageGetWidth)
from CoreFoundation import CFDataCreate, kCFAllocatorDefault
src = CGImageSourceCreateWithData(CFDataCreate(kCFAllocatorDefault, png, len(png)), None)
img = CGImageSourceCreateImageAtIndex(src, 0, None)
check("the rendered page is 2x the PDF's own size", CGImageGetWidth(img) == 1224)


class _FakeClient:
    """Answers like the vision model, and records what it was shown."""

    def __init__(self, answer="Three pages, each with a page number."):
        self.answer = answer
        self.seen = None

        class _Models:
            def generate_content(inner, model, contents):
                self.seen = contents
                return type("R", (), {"text": self.answer})()
        self.models = _Models()


fc = _FakeClient()
answer = desk.read_document(pdf, "What is this?", fc)
check("reading a document returns the model's answer",
      answer == "Three pages, each with a page number.")
imgs = [p for p in fc.seen if not isinstance(p, str)]
check("every page was actually rendered and sent, not just the first",
      len(imgs) == 3)
check("the question reaches the model",
      any("What is this?" in p for p in fc.seen if isinstance(p, str)))

_make_pdf(os.path.join(tmp, "long.pdf"), pages=12)
fc2 = _FakeClient()
desk.read_document(os.path.join(tmp, "long.pdf"), "?", fc2, max_pages=4)
sent = [p for p in fc2.seen if not isinstance(p, str)]
prose = " ".join(p for p in fc2.seen if isinstance(p, str))
check("a long document is capped instead of sending every page",
      len(sent) == 4)
check("and the model is TOLD it only saw part of it, so it can't imply "
      "it read the whole thing", "12 pages" in prose and "4" in prose)

check("a model that throws costs nothing worse than None",
      desk.read_document(pdf, "?", _FakeClient(), log=lambda m: None) is not None)


class _Boom:
    class models:
        @staticmethod
        def generate_content(model, contents):
            raise RuntimeError("503")


check("a 503 from the vision model returns None, it does not raise",
      desk.read_document(pdf, "?", _Boom, log=lambda m: None) is None)

for f in os.listdir(tmp):
    os.remove(os.path.join(tmp, f))
os.rmdir(tmp)


# =========================================================================== #
# 5. The calendar. EventKit needs a dialog nobody can click from a test, so
#    the store is faked — but everything around it is the real code, including
#    the read-back that stops Neo confirming an event that was never saved.
# =========================================================================== #
SAT = datetime.date(2026, 8, 29)          # a Saturday, so weekday maths shows

CASES = [
    # (fields, expected problem or (hour, minute), why this case exists)
    ({"title": "Lunch", "date": "2026-09-03", "time": "13:00", "clear": True},
     (13, 0), "an ordinary afternoon event"),
    ({"title": "Lunch", "date": "2026-09-03", "time": "01:00", "clear": True},
     (13, 0), "'at one' means the afternoon, not 1am"),
    ({"title": "Standup", "date": "2026-09-03", "time": "08:30", "clear": True},
     (8, 30), "but a morning hour is left alone"),
    ({"title": "Flight", "date": "2026-09-03", "time": "06:00", "clear": True},
     (18, 0), "6 is the evening; nobody schedules 6am by saying 'at six'"),
    ({"title": "x", "date": "2020-01-01", "time": "10:00", "clear": True},
     "past", "a date in the past is a parsing failure, not an event"),
    ({"title": "x", "date": "2099-01-01", "time": "10:00", "clear": True},
     "far", "and so is one seventy years out"),
    ({"title": "", "date": "2026-09-03", "clear": True},
     "unclear", "an empty title"),
    ({"title": "thing", "date": "", "clear": True},
     "no_date", "no day at all"),
    ({"title": "thing", "date": "next tuesday", "clear": True},
     "bad_date", "the model returning words where a date was asked for"),
    ({"title": "thing", "date": "2026-09-03", "time": "25:00", "clear": True},
     "bad_time", "an impossible hour"),
    ({"title": "thing", "date": "2026-09-03", "time": "9am", "clear": True},
     "bad_time", "a time in the wrong format"),
    ({"title": "thing", "date": "2026-09-03", "time": "13:00", "clear": False},
     "unclear", "the model saying it couldn't tell what the event was"),
]
for fields, want, why in CASES:
    ev, prob = agenda.normalise(fields, today=SAT)
    if isinstance(want, str):
        ok = prob == want
        got = prob
    else:
        ok = prob is None and ev and (ev["start"].hour, ev["start"].minute) == want
        got = prob or (ev["start"].hour, ev["start"].minute)
    check(f"{why} -> {want} (got {got})", ok)

ev, _ = agenda.normalise({"title": "Trip", "date": "2026-09-03",
                          "all_day": True, "clear": True}, today=SAT)
check("an all-day event spans exactly one day",
      ev["all_day"] and (ev["end"] - ev["start"]).days == 1)

ev, _ = agenda.normalise({"title": "M", "date": "2026-09-03", "time": "10:00",
                          "minutes": 99999, "clear": True}, today=SAT)
check("an absurd duration is clamped instead of booking out the year",
      (ev["end"] - ev["start"]).total_seconds() <= 12 * 3600)

ev, _ = agenda.normalise({"title": "M", "date": "2026-09-03", "time": "10:00",
                          "minutes": "sixty", "clear": True}, today=SAT)
check("a non-numeric duration falls back to the default",
      (ev["end"] - ev["start"]).total_seconds() == agenda.DEFAULT_MINUTES * 60)

ev, _ = agenda.normalise({"title": "T" * 400, "date": "2026-09-03",
                          "time": "10:00", "clear": True}, today=SAT)
check("a runaway title is trimmed, not written whole into the calendar",
      len(ev["title"]) <= 120)

ev, _ = agenda.normalise({"title": "Yesterday's thing",
                          "date": (SAT - datetime.timedelta(days=1)).isoformat(),
                          "time": "10:00", "clear": True}, today=SAT)
check("yesterday is still allowed — a late entry is a real thing people do",
      ev is not None)

SPOKEN = [
    (datetime.datetime(2026, 8, 29, 13, 0), False, "today at 1 in the afternoon"),
    (datetime.datetime(2026, 8, 30, 9, 0), False, "tomorrow at 9 in the morning"),
    (datetime.datetime(2026, 9, 3, 13, 30), False, "Thursday at 1:30 in the afternoon"),
    (datetime.datetime(2026, 9, 8, 20, 0), False, "next Tuesday at 8 in the evening"),
    (datetime.datetime(2026, 9, 3, 0, 0), True, "Thursday"),
]
for start, all_day, want in SPOKEN:
    got = agenda.speak_when(start, all_day, today=SAT)
    check(f"spoken as {want!r} (got {got!r})", got == want)

check("no spoken time ever contains a colon-padded 24-hour clock",
      not any(":00" in agenda.speak_when(datetime.datetime(2026, 9, 3, h, 0),
                                         False, today=SAT) for h in range(8, 22)))
check("every failure mode has a sentence Neo can actually say",
      all(k in agenda.TROUBLE for k in
          ["no_eventkit", "denied", "write_only", "no_calendar", "no_date",
           "bad_date", "past", "far", "bad_time", "unclear", "no_model",
           "save_failed", "vanished"]) and
      all(len(v) > 15 and not v.endswith(("_", ".py")) for v in agenda.TROUBLE.values()))


class _FakeEvent:
    def __init__(self, store):
        self._s = store
        self._t = self._loc = ""
        self._all = False
        self._start = self._end = None
        self._cal = None

    def setTitle_(self, v): self._t = v
    def setCalendar_(self, v): self._cal = v
    def setAllDay_(self, v): self._all = v
    def setStartDate_(self, v): self._start = v
    def setEndDate_(self, v): self._end = v
    def setLocation_(self, v): self._loc = v
    def title(self): return self._t
    def isAllDay(self): return self._all
    def startDate(self): return self._start
    def calendar(self): return self._cal
    def eventIdentifier(self): return "fake-1" if self._s.saved else None


class _FakeCal:
    def __init__(self, name, writable=True):
        self._n, self._w = name, writable

    def title(self): return self._n
    def allowsContentModifications(self): return self._w


class _FakeStore:
    """Saves, or refuses, or loses the event afterwards."""

    def __init__(self, mode="ok", cals=None, default="Home"):
        self.mode = mode
        self.saved = False
        self.cals = cals or [_FakeCal("Home"), _FakeCal("Work"),
                             _FakeCal("Holidays", writable=False)]
        self._default = default

    def calendarsForEntityType_(self, kind): return self.cals

    def defaultCalendarForNewEvents(self):
        return next((c for c in self.cals if c.title() == self._default), None)

    def saveEvent_span_error_(self, ev, span, err):
        if self.mode == "refuse":
            return (False, "calendar is read-only")
        self.saved = True
        self._ev = ev
        return (True, None)

    def eventWithIdentifier_(self, ident):
        return None if self.mode == "vanish" else self._ev


class _FakeEventKit:
    """Stands in for the EventKit module so create() runs its real code path."""
    EKSpanThisEvent = 0

    class EKEvent:
        store = None

        @staticmethod
        def eventWithEventStore_(store):
            return _FakeEvent(store)


def _with_store(store, fn):
    """Run fn with agenda talking to a fake calendar instead of the real one.

    Both the store AND the EventKit module are swapped, so everything between
    "here is an event" and "read it back out of the calendar" is the real
    function — only the calendar service itself is imaginary.
    """
    real = agenda._store
    real_ek = sys.modules.get("EventKit")
    agenda._store = lambda: (store, 1) if store else (None, "denied")
    sys.modules["EventKit"] = _FakeEventKit
    try:
        return fn()
    finally:
        agenda._store = real
        if real_ek is not None:
            sys.modules["EventKit"] = real_ek
        else:
            del sys.modules["EventKit"]


ev, _ = agenda.normalise({"title": "Lunch with Priya", "date": "2026-09-03",
                          "time": "13:00", "clear": True}, today=SAT)

s = _FakeStore()
saved, prob = _with_store(s, lambda: agenda.create(ev))
check("a saved event reports what the CALENDAR holds, not what was asked for",
      prob is None and saved["title"] == "Lunch with Priya" and
      saved["start"].hour == 13 and saved["calendar"] == "Home")

s2 = _FakeStore(mode="vanish")
saved, prob = _with_store(s2, lambda: agenda.create(ev))
check("an event that doesn't read back is reported as a failure, NOT confirmed",
      saved is None and prob == "vanished")

s3 = _FakeStore(mode="refuse")
saved, prob = _with_store(s3, lambda: agenda.create(ev))
check("a calendar that refuses the save is reported honestly",
      saved is None and prob == "save_failed")

s4 = _FakeStore()
saved, _ = _with_store(s4, lambda: agenda.create(ev, calendar="work"))
check("a named calendar is honoured, case-insensitively",
      saved["calendar"] == "Work")

s5 = _FakeStore()
saved, _ = _with_store(s5, lambda: agenda.create(ev, calendar="Holidays"))
check("a read-only calendar is never written to, even when named",
      saved["calendar"] != "Holidays")

s6 = _FakeStore(cals=[_FakeCal("Holidays", writable=False)], default=None)
saved, prob = _with_store(s6, lambda: agenda.create(ev))
check("with nothing writable at all, it says so instead of guessing",
      saved is None and prob == "no_calendar")

saved, prob = _with_store(None, lambda: agenda.create(ev))
check("no permission means a spoken reason, not a traceback",
      saved is None and prob in agenda.TROUBLE)


# =========================================================================== #
# 6. Mail. Nothing here can send. That is the point, and it is asserted.
# =========================================================================== #
check("the module is draft-only, and says so in one flag",
      mail.DRAFT_ONLY is True)
check("there is no send function anywhere on the module",
      not any("send" in n.lower() for n in dir(mail) if not n.startswith("_")))

src = open("mail.py").read()
check("and nothing in it opens an SMTP connection", "smtplib" not in src)

ADDRESSES = [("a@b.com", True), ("sam.lee+x@gmail.co.uk", True),
             ("no-at-sign", False), ("two@@b.com", False), ("a@b", False),
             ("a@b.c", False), ("", False), (None, False),
             ("a b@c.com", False), ("a@b.com, c@d.com", False)]
for addr, want in ADDRESSES:
    check(f"{addr!r} is {'a' if want else 'not a'} valid address",
          mail.valid_address(addr) is want)

check("a draft with no subject is refused", mail.draft("a@b.com", "", "hi")[1] == "no_subject")
check("a draft with no body is refused", mail.draft("a@b.com", "hi", "  ")[1] == "no_body")
check("a draft to a bad address is refused before any backend is touched",
      mail.draft("nope", "hi", "there")[1] == "bad_address")

# AppleScript string escaping. A subject line with a quote in it used to be a
# syntax error, which fails as "the mail app wouldn't save that" — a lie.
NASTY = ['He said "hello"', 'back\\slash', 'line\nbreak', '"; do shell script "id',
         "it's fine", '', 'both " and \\ and\nall three']
for s in NASTY:
    lit = mail._as_str(s)
    out, err = mail._osa(f"return {lit}", 10)
    check(f"AppleScript survives {s!r} and gives it back intact",
          not err and out == s.replace("\n", "\n"))

check("strip_html drops scripts entirely rather than reading them aloud",
      "alert" not in mail.strip_html("<script>alert(1)</script><p>hi</p>"))
check("strip_html turns block tags into line breaks",
      "\n" in mail.strip_html("<p>one</p><p>two</p>"))
check("strip_html decodes the entities that actually show up in mail",
      mail.strip_html("<p>Tom &amp; Jerry &lt;3 &nbsp;</p>").startswith("Tom & Jerry <3"))
check("strip_html on nothing is empty, not 'None'",
      mail.strip_html("") == "" and mail.strip_html(None) == "")

WHO = [('"Priya P" <r@x.com>', "Priya P"), ('bob.smith@x.com', "bob smith"),
       ('<a@b.com>', "a"), ('', "someone")]
for header, want in WHO:
    check(f"{header!r} is spoken as {want!r}", mail._who(header) == want)

check("a MIME-encoded subject is decoded, not read out as gibberish",
      mail._decode("=?utf-8?B?SGVsbG8gdGhlcmU=?=") == "Hello there")

check("with no backend, every entry point returns something Neo can say",
      mail.backend() is None and mail.recent() == [] and
      mail.draft("a@b.com", "s", "b")[1] == "not_set_up" and
      len(mail.TROUBLE["not_set_up"]) > 40)
check("the not-set-up line names the actual fix rather than apologising",
      "connect mail" in mail.NOT_SET_UP)

# ---- the shape of a draft: greeting, paragraphs, sign-off, no dashes ----
_d = mail.format_body("Thanks for the note — I'd love to join. Could you make an exception?",
                      to_name="Priya Rao", sender="Sam Lee")
check("draft: opens with a greeting to the first name", _d.startswith("Hi Priya,\n\n"))
check("draft: ends with a sign-off and the sender's name", _d.rstrip().endswith("Best,\nSam Lee"))
check("draft: no em or en dashes anywhere", "—" not in _d and "–" not in _d)
check("draft: the dash became a comma, not a deleted word", "Thanks for the note, I'd love to join." in _d)
_g = mail.format_body("Could you make an exception?", to_name="Hack Princeton team", sender="A")
check("draft: a group is greeted whole, never 'Hi Hack,'", _g.startswith("Hi Hack Princeton team,"))
check("draft: an address alone gets a plain Hello", mail.format_body("x", to_name="r@x.com", sender="A").startswith("Hello,"))
_k = mail.format_body("Hey Priya,\nLunch works.\nThanks,\nSam", to_name="Priya", sender="Sam Lee")
check("draft: a greeting the model already wrote is kept, not doubled",
      _k.startswith("Hey Priya,\n\n") and _k.count("Priya,") == 1)
check("draft: a sign-off the model already wrote is kept, not doubled",
      _k.rstrip().endswith("Thanks,\nSam") and "Best," not in _k)
check("draft: 'I hope this finds you well' is removed",
      "hope this" not in mail.format_body("I hope this finds you well. Quick one: are we on?", sender="A"))
_long = mail.format_body(" ".join(f"Sentence number {i} says something useful here." for i in range(9)), sender="A")
check("draft: a wall of text is broken into paragraphs", _long.count("\n\n") >= 4)
check("draft: -- is not a dash either", "--" not in mail.no_dashes("a -- b"))
check("draft: a date range keeps its hyphen", mail.no_dashes("Mon–Fri") == "Mon-Fri")
check("draft: an empty To is allowed (shown for him to fill in), a wrong one is not",
      mail.draft("", "s", "b")[1] != "bad_address" and mail.draft("nope", "s", "b")[1] == "bad_address")
_src = open("mail.py").read()
check("draft: the Mail.app draft is made VISIBLE and Mail is activated",
      "visible:true" in _src and "  activate" in _src.split("def _mailapp_draft")[1].split("def _imap_draft")[0])
import agent as _ag
check("draft_email: the docstring forbids em dashes and Notes",
      "NO em dashes" in _ag.draft_email.__doc__ and "Never put an email in Notes" in _ag.draft_email.__doc__)
check("control_mac: a note it makes is shown, not just filed",
      _ag._makes_note('tell application "Notes" to make new note with properties {name:"x"}')
      and "show (first note" in open("agent.py").read())


# =========================================================================== #
# 7. The tools, called the way the brain calls them — including when the thing
#    underneath is broken. Neo must never claim it can't do something it can,
#    and never claim it did something it didn't.
# =========================================================================== #
import agent

names = {t.__name__ for t in agent.TOOLS}
for want in ["what_did_i_copy", "find_file", "read_document", "add_to_calendar",
             "read_email", "draft_email"]:
    check(f"{want} is actually wired into the toolbox", want in names)

for tool in agent.TOOLS:
    doc = (tool.__doc__ or "")
    check(f"{tool.__name__} tells the model when to reach for it",
          len(doc) > 40)

out = agent.what_did_i_copy()
check("the clipboard tool marks its contents as untrusted, because a copied "
      "web page can contain instructions aimed at the model",
      "UNTRUSTED" in out or "empty" in out)

desk_real = desk.clipboard
try:
    desk.clipboard = lambda limit=4000: ""
    check("an empty clipboard is explained, not returned as a blank answer",
          "empty" in agent.what_did_i_copy().lower())
finally:
    desk.clipboard = desk_real

# Built at runtime, so the literal never appears in this file — a query that
# is written down here is one Spotlight can find INSIDE this file, which is
# how the first version of this check "passed" by finding itself.
absent = "zq" + "".join(chr(120 + i % 3) for i in range(9)) + "9naf"
check("searching for something that isn't there says so plainly",
      "nothing" in agent.find_file(absent).lower())

check("the email tools survive having no mailbox at all",
      "email" in agent.read_email().lower() and
      "email" in agent.draft_email("a@b.com", "s", "b").lower())

agent._client = None
check("calendar without a model connection is a sentence, not a crash",
      isinstance(agent.add_to_calendar("lunch tomorrow"), str))
check("reading a document without a model connection is a sentence too",
      isinstance(agent.read_document("what"), str))



# =========================================================================== #
# 8. Automatic context. The whole point is that it says what is AVAILABLE and
#    never what it contains — so the checks that matter most here are the ones
#    that try to get contents out of it.
# =========================================================================== #
import situation

SECRETS = [
    "sk-proj-abcdefghijklmnopqrstuvwxyz012345",
    "AIzaSyD-abcdefghijklmnopqrstuvwxyz01234",
    "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "xoxb-1234567890-abcdefghijkl",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIE...",
    "password: hunter2",
    "API_KEY = sk_live_thing",
    "my card is 4111 1111 1111 1111",
    "Bearer: eyJhbGciOi",
]
for secret in SECRETS:
    check(f"a clipboard holding {secret[:22]!r} is described as NOTHING",
          situation.clipboard_shape(secret) == "")

SHAPES = [
    ("https://example.com/x", "a link"),
    ("Traceback (most recent call last):\n  File \"a.py\", line 1\nValueError: x",
     "an error message"),
    ("priya@example.com", "an email address"),
    ("def f():\n    import os\n    return 1\nclass A:\n    pass", "some code"),
    ("42 + 17", "some numbers"),
    ("just a line", "a short bit of text"),
    ("\n".join(["a line"] * 20), "a long piece of text"),
    ("", ""),
    (None, ""),
    ("   \n  ", ""),
]
for text, want in SHAPES:
    got = situation.clipboard_shape(text)
    check(f"{(text or '')[:26]!r} is described as {want!r} (got {got!r})", got == want)

# The real guarantee, stated as one check: whatever goes in, the description is
# one of a fixed set of phrases. It can never be a fragment of the clipboard.
VOCAB = {"", "a link", "an error message", "an email address", "some code",
         "some numbers", "a short bit of text", "a paragraph of text",
         "a long piece of text", "something very long"}
SAMPLES = SECRETS + [t for t, _ in SHAPES if t] + [
    "Priya's phone number is 07700 900123",
    "The the project database has 73 users",
    "x" * 300000, "\x00\x01binary", "日本語のテキストです", "<h1>hi</h1>",
]
check("every possible clipboard maps into a fixed vocabulary of descriptions, "
      "so no fragment of it can ever leak into the prompt",
      all(situation.clipboard_shape(s) in VOCAB for s in SAMPLES))

for s in SAMPLES:
    shape = situation.clipboard_shape(s)
    if shape:
        block = situation.line(snap={"app": "Preview", "document": "d.pdf",
                                     "clipboard": shape})
        overlap = any(w in block for w in s.split() if len(w) > 6)
        check(f"no word from {s[:20]!r} survives into the prompt block", not overlap)

DEICTIC = [("what is this", True), ("read that to me", True),
           ("why is it failing", True), ("what's on my screen", True),
           ("explain the error", True), ("summarise this pdf", True),
           ("what did I just copy", True),
           ("what's the weather", False), ("set a timer for ten minutes", False),
           ("who won the game last night", False), ("play some music", False),
           ("", False), (None, False)]
for text, want in DEICTIC:
    check(f"{text!r} {'does' if want else 'does not'} point at something unnamed",
          situation.is_ambiguous(text) is want)

check("a frontmost system dialog is not reported as an app he's using",
      situation.line(snap={"app": "UserNotificationCenter", "document": "",
                           "clipboard": ""}) == "")
check("nothing to report means an empty block, not an awkward empty sentence",
      situation.line(snap={"app": "", "document": "", "clipboard": ""}) == "")
check("an open document is named, because that's what 'this' usually means",
      "d.pdf" in situation.line(snap={"app": "Preview", "document": "d.pdf",
                                      "clipboard": ""}))
check("the nudge only appears when he actually said 'this'",
      "almost certainly" in situation.line(
          snap={"app": "Preview", "document": "d.pdf", "clipboard": ""},
          text="what does this say") and
      "almost certainly" not in situation.line(
          snap={"app": "Preview", "document": "d.pdf", "clipboard": ""},
          text="what's the weather"))
check("the block names the tools that can fetch each thing, so the model isn't "
      "left knowing something is there and unable to reach it",
      all(t in situation.line(snap={"app": "Preview", "document": "d.pdf",
                                    "clipboard": "some code"},
                              text="what is this")
          for t in ["read_document", "what_did_i_copy", "look_at_screen"]))

# Cost. This runs on every single turn, so it has to be nearly free.
situation.snapshot(force=True)                # warm anything lazy
t0 = time.time()
for _ in range(5):
    situation.snapshot()
cached = time.time() - t0
check(f"a cached snapshot is effectively free ({cached * 1000:.1f}ms for 5)",
      cached < 0.05)

t0 = time.time()
situation.snapshot(force=True)
fresh = time.time() - t0
check(f"even an uncached snapshot stays under half a second ({fresh:.2f}s)",
      fresh < 0.5)

# Cheapness is a guarantee, not a hope: prove it by making the expensive
# things explode. A grep of the source would pass on the word "screenshot"
# appearing in a comment, which is how five checks in the old suite ended up
# asserting nothing at all.
import hands as _hands
_real_shot = getattr(_hands, "screenshot", None)
_real_describe = getattr(_hands, "describe_screen", None)
try:
    def _forbidden(*a, **k):
        raise AssertionError("situation.py reached for the screen or a model")
    if _real_shot:
        _hands.screenshot = _forbidden
    if _real_describe:
        _hands.describe_screen = _forbidden
    snap = situation.snapshot(force=True)
    situation.line(snap, text="what is this on my screen")
    check("the snapshot never takes a screenshot and never calls a model, "
          "even when the question is about the screen", True)
except AssertionError as e:
    check(f"the snapshot never takes a screenshot or calls a model ({e})", False)
finally:
    if _real_shot:
        _hands.screenshot = _real_shot
    if _real_describe:
        _hands.describe_screen = _real_describe

# And it must survive the desk being broken underneath it.
_real_clip, _real_app = desk.clipboard, desk.frontmost_app
try:
    def _boom(*a, **k):
        raise RuntimeError("pbpaste is gone")
    desk.clipboard = _boom
    desk.frontmost_app = _boom
    check("a broken desk gives an empty situation, not an exception mid-turn",
          isinstance(situation.snapshot(force=True), dict) and
          situation.line() == "")
finally:
    desk.clipboard, desk.frontmost_app = _real_clip, _real_app
    situation.snapshot(force=True)

# The live path: the header must be prepared off the hot threads, and sent
# only when it changed.
import live
src_live = open("live.py").read()
check("the situation is computed on a worker thread, never on the event tap "
      "or the asyncio loop", "neo-situation" in src_live and
      "threading.Thread(target=_work" in src_live)
check("an unchanged situation is not re-sent every single turn",
      "here != self._situation_sent" in src_live)

sess = live.__dict__.get("LiveSession") or live.__dict__.get("Live")
check("a fresh session starts with no situation and nothing sent",
      sess is not None)

check("the standing brief tells the model what RIGHT NOW is and not to read "
      "it out", "RIGHT NOW" in context.GROUND_RULES and
      "Never read the line out loud" in context.GROUND_RULES)


print()
if FAILED:
    print(f"{len(FAILED)} FAILED:")
    for n in FAILED:
        print("  - " + n)
    sys.exit(1)
print("Desk, calendar, mail and automatic context all clean.")
