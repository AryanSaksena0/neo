"""
mail.py — reading their email, and writing drafts.

**Neo drafts; the user sends.** Nothing here puts a message on the wire. Every
composed mail lands in the Drafts folder for them to look at and hit send on
themselves. That is a deliberate line, not a missing feature: an assistant that
can quietly email people on your behalf is one bad transcription away from
being a serious problem, and "it's in your drafts" costs one click.

Two backends, tried in this order:

  Mail.app   AppleScript. No credentials anywhere, works with whatever
             accounts are already signed in, and drafting is native.
  IMAP       Gmail app password from .env. Read via IMAP, draft via APPEND
             to [Gmail]/Drafts.

Neither is configured on this machine today — `NEO_GMAIL_APP_PASSWORD` is
rejected with AUTHENTICATIONFAILED (supervised Google accounts can't mint app
passwords, which mailwatch.py already knew), and Mail.app has never been set
up. So `backend()` returns None and every entry point returns a spoken
sentence naming the ONE thing that would fix it. A feature that says what it
needs beats one that says "I can't do that".
"""

import email
import email.header
import email.utils
import imaplib
import os
import re
import subprocess
import time

MAX_BODY = 6000
DRAFT_ONLY = True          # read this before changing it; see the docstring


# --------------------------------------------------------------------------- #
# Which backend, if any.
# --------------------------------------------------------------------------- #
def _osa(script, timeout=25):
    """Run AppleScript. (stdout, error). Never raises."""
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "", "timeout"
    except Exception as e:
        return "", type(e).__name__
    out = r.stdout.decode("utf-8", "replace").strip()
    err = r.stderr.decode("utf-8", "replace").strip()
    return out, (err if r.returncode else "")


def _mail_configured():
    """Has Mail ever been set up? Answered without asking macOS for anything.

    ~/Library/Mail looked like the obvious check and isn't: it is protected by
    Full Disk Access, so listing it raises PermissionError on a normal run and
    tells you nothing either way. Mail's preference domain is readable by
    anyone and only exists once an account has been added.

    "Is Mail running" was tried and is worse than useless. The AppleScript
    probe LAUNCHES Mail, so after one probe the answer is always yes — and
    then every later probe spends twelve seconds waiting on an automation
    dialog to be answered. The probe left Mail.app open on their screen and
    a permission sheet on top of it.
    """
    try:
        r = subprocess.run(["defaults", "read", "com.apple.mail"],
                           capture_output=True, timeout=8)
        if r.returncode == 0 and len(r.stdout.strip()) > 2:
            return True
    except Exception:
        pass
    return False


def mail_app_ready():
    """True only if Mail.app exists AND has at least one account.

    The account check is done on DISK first, on purpose. Asking Mail.app over
    AppleScript raises a permission dialog — "Neo wants access to control
    Mail" — and on a machine where Mail was never set up that dialog is pure
    noise: it interrupts them to ask about an app they doesn't use, to answer a
    question whose answer is no. ~/Library/Mail only exists once an account
    has been added, so checking for it settles the common case for free and
    silently.
    """
    if not os.path.isdir("/System/Applications/Mail.app") and \
       not os.path.isdir("/Applications/Mail.app"):
        return False
    if not _mail_configured():
        return False
    out, err = _osa('tell application "Mail" to get name of every account', 6)
    if err:
        return False
    return bool(out.strip())


def imap_ready():
    user = (os.getenv("NEO_GMAIL_USER") or "").strip()
    pw = (os.getenv("NEO_GMAIL_APP_PASSWORD") or "").replace(" ", "").strip()
    if not (user and pw):
        return False
    try:
        M = imaplib.IMAP4_SSL(os.getenv("NEO_IMAP_HOST", "imap.gmail.com"), 993)
    except Exception:
        return False
    try:
        M.login(user, pw)
        return True
    except Exception:
        return False
    finally:
        try:
            M.logout()
        except Exception:
            pass


_CACHE = {"which": None, "at": 0.0}


NEGATIVE_TTL = 1800        # "no mailbox" doesn't change on its own


def backend(ttl=300):
    """'mail_app' | 'imap' | None. Cached, because both probes cost a second.

    A negative answer is cached six times longer: adding a mail account is
    something they do deliberately, and re-probing every five minutes means
    re-paying an IMAP handshake all day to be told the same thing.
    """
    now = time.time()
    age = ttl if _CACHE["which"] else NEGATIVE_TTL
    if _CACHE["which"] is not None and now - _CACHE["at"] < age:
        return _CACHE["which"] or None
    which = "mail_app" if mail_app_ready() else ("imap" if imap_ready() else "")
    _CACHE.update(which=which, at=now)
    return which or None


NOT_SET_UP = ("I can't get at your email yet. Say 'connect mail' to add the "
              "account to the Mail app, or 'connect google' and I'll read Gmail "
              "through my own browser — either way, no password ever reaches me.")


# --------------------------------------------------------------------------- #
# The shape of a draft. The model writes the substance; CODE guarantees the
# form — a greeting, real paragraphs, a sign-off with their name, and no dashes
# of the kind that make a message read as machine-written. A draft that opens
# looking like a text message is one they have to rewrite before sending.
# --------------------------------------------------------------------------- #
_GREET = re.compile(r"^\s*(hi|hello|hey|dear|good (morning|afternoon|evening))\b", re.I)
_CLOSE = re.compile(r"^\s*(best|best regards|kind regards|regards|thanks|thank you|"
                    r"many thanks|cheers|sincerely|warmly|all the best|talk soon)[,.!]?\s*$", re.I)
_FLUFF = re.compile(r"^\s*i hope (this|you)[^.!\n]*[.!]\s*", re.I)


def signature_name():
    """Whose name goes at the bottom. NEO_USER_NAME, else the Mac's own idea of
    who is logged in, else nothing (a sign-off with no name is still a sign-off)."""
    name = (os.getenv("NEO_USER_NAME") or "").strip()
    if name:
        return name
    try:
        r = subprocess.run(["id", "-F"], capture_output=True, text=True, timeout=3)
        return (r.stdout or "").strip()
    except Exception:
        return ""


_GROUP = re.compile(r"\b(team|support|admissions|office|committee|organi[sz]ers|"
                    r"staff|department|dept|crew|folks|everyone|all|hello|info|"
                    r"careers|recruiting|hr)\b", re.I)


def first_name(to_name):
    """'Priya Rao <r@x.com>' -> 'Priya'. A group ('Hack Princeton team')
    is kept whole, because 'Hi Hack,' is what a machine would write. An
    address alone gives ''."""
    raw = re.sub(r"<[^>]*>", "", str(to_name or "")).strip(' ",')
    if "@" in raw or not raw:
        return ""
    if _GROUP.search(raw) or len(raw.split()) >= 3:
        return raw
    return raw.split()[0].strip(",")


def no_dashes(text):
    """Em and en dashes out; what replaces them depends on where they sit.
    'A — B' becomes 'A, B'; a dash with no space around it becomes a hyphen
    (a date range, a compound); a trailing one just goes."""
    text = re.sub(r"\s*[—–]\s*$", "", text, flags=re.M)
    text = re.sub(r"\s+[—–]\s+", ", ", text)
    text = re.sub(r"[—–]", "-", text)
    return text.replace("--", ", ")


def paragraphs(text):
    """Break a wall of text into paragraphs of two or three sentences. Text
    that already has blank lines is left alone."""
    text = text.strip()
    if "\n\n" in text or len(text) < 240:
        return text
    sents = re.split(r"(?<=[.!?])\s+", text)
    out, cur = [], []
    for sn in sents:
        cur.append(sn)
        if len(cur) >= 3 or sum(len(x) for x in cur) > 320:
            out.append(" ".join(cur)); cur = []
    if cur:
        out.append(" ".join(cur))
    return "\n\n".join(out)


def _their_style():
    """Greeting word and sign-off they actually use (person.py, from sent
    mail), else Neo's defaults."""
    try:
        import person as profile
        w = profile.load().get("writing", {})
        ws = w.get("mail") or {}
        g = (ws.get("greeting") or "hi").capitalize()
        c = (ws.get("signoff") or "best").capitalize()
        return g if g in ("Hi", "Hey", "Hello", "Dear") else "Hi", c
    except Exception:
        return "Hi", "Best"


def format_body(body, to_name="", closing=None, sender=None, greeting=None):
    """The finished draft. Pure given sender/closing/greeting; without them
    the greeting and sign-off are the ones THEY use, learned from sent mail."""
    sender = signature_name() if sender is None else sender
    g_word, c_word = _their_style()
    closing = closing or c_word
    greeting = greeting or g_word
    text = no_dashes(str(body or "")).replace("\r", "").strip()
    lines = text.split("\n")
    # greeting: keep the model's if it wrote one, else add one
    if lines and _GREET.match(lines[0]):
        greet, rest = lines[0].strip().rstrip(",") + ",", "\n".join(lines[1:]).strip()
    else:
        who = first_name(to_name)
        greet, rest = (f"{greeting} {who}," if who else "Hello,"), text
    rest = _FLUFF.sub("", rest).strip()
    # sign-off: keep the model's if it ended with one, else add ours
    tail = [l for l in rest.split("\n") if l.strip()]
    if len(tail) >= 2 and _CLOSE.match(tail[-2]) and len(tail[-1]) <= 60:
        body_part, close = "\n".join(rest.split("\n")[:-2]).strip(), f"{tail[-2].strip()}\n{tail[-1].strip()}"
    elif tail and _CLOSE.match(tail[-1]):
        body_part, close = "\n".join(rest.split("\n")[:-1]).strip(), f"{tail[-1].strip().rstrip(',.')},\n{sender}".rstrip()
    else:
        body_part, close = rest, f"{closing},\n{sender}".rstrip()
    return f"{greet}\n\n{paragraphs(body_part)}\n\n{close}\n"


# --------------------------------------------------------------------------- #
# Reading.
# --------------------------------------------------------------------------- #
def _decode(raw):
    """A MIME-encoded header as plain text."""
    if not raw:
        return ""
    try:
        parts = email.header.decode_header(raw)
    except Exception:
        return str(raw)
    out = []
    for chunk, enc in parts:
        if isinstance(chunk, bytes):
            out.append(chunk.decode(enc or "utf-8", "replace"))
        else:
            out.append(chunk)
    return "".join(out).strip()


def _who(from_header):
    """'Priya' out of 'Priya P <p@x.com>' — the name if there is one."""
    name, addr = email.utils.parseaddr(_decode(from_header))
    name = (name or "").strip().strip('"')
    if name:
        return name
    return (addr.split("@")[0] if addr else "someone").replace(".", " ")


def _plain(msg):
    """The readable text of a message, HTML stripped if that's all there is."""
    body, html = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() != "text" or part.get_filename():
                continue
            try:
                text = part.get_payload(decode=True)
                text = text.decode(part.get_content_charset() or "utf-8", "replace")
            except Exception:
                continue
            if part.get_content_subtype() == "plain" and not body:
                body = text
            elif part.get_content_subtype() == "html" and not html:
                html = text
    else:
        try:
            body = msg.get_payload(decode=True).decode(
                msg.get_content_charset() or "utf-8", "replace")
        except Exception:
            body = str(msg.get_payload())
    text = body or strip_html(html)
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:MAX_BODY]


def strip_html(html):
    """Good enough to read out loud. Not a parser, and not trying to be."""
    if not html:
        return ""
    html = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                 ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        html = html.replace(a, b)
    html = re.sub(r"[ \t]{2,}", " ", html)
    return re.sub(r"\n{3,}", "\n\n", html).strip()


def _imap_conn():
    user = (os.getenv("NEO_GMAIL_USER") or "").strip()
    pw = (os.getenv("NEO_GMAIL_APP_PASSWORD") or "").replace(" ", "").strip()
    M = imaplib.IMAP4_SSL(os.getenv("NEO_IMAP_HOST", "imap.gmail.com"), 993)
    M.login(user, pw)
    return M


def _imap_recent(count, query=None):
    M = _imap_conn()
    try:
        M.select("INBOX", readonly=True)
        if query:
            safe = query.replace('"', "")
            ok, data = M.search(None, "TEXT", f'"{safe}"')
        else:
            ok, data = M.search(None, "ALL")
        ids = (data[0].split() if ok == "OK" and data and data[0] else [])
        out = []
        for mid in reversed(ids[-count * 2:][-count:]):
            ok, msg = M.fetch(mid, "(BODY.PEEK[])")
            if ok != "OK" or not msg or not isinstance(msg[0], tuple):
                continue
            m = email.message_from_bytes(msg[0][1])
            out.append({
                "id": mid.decode(),
                "from": _who(m.get("From")),
                "address": email.utils.parseaddr(_decode(m.get("From")))[1],
                "subject": _decode(m.get("Subject")) or "(no subject)",
                "date": _decode(m.get("Date")),
                "body": _plain(m),
            })
        return out
    finally:
        try:
            M.logout()
        except Exception:
            pass


_MAILAPP_LIST = '''
tell application "Mail"
  set out to ""
  set msgs to (messages of inbox)
  set n to count of msgs
  set top to n
  if top > %(count)d then set top to %(count)d
  repeat with i from 1 to top
    set m to item i of msgs
    try
      set s to subject of m
    on error
      set s to "(no subject)"
    end try
    try
      set f to sender of m
    on error
      set f to "unknown"
    end try
    set out to out & (id of m) & "\\t" & f & "\\t" & s & "\\t" & (date received of m as string) & linefeed
  end repeat
  return out
end tell
'''


def _mailapp_recent(count):
    out, err = _osa(_MAILAPP_LIST % {"count": int(count)}, 30)
    if err:
        return []
    rows = []
    for line in out.splitlines():
        bits = line.split("\t")
        if len(bits) < 4:
            continue
        rows.append({"id": bits[0], "from": _who(bits[1]),
                     "address": email.utils.parseaddr(bits[1])[1],
                     "subject": bits[2] or "(no subject)",
                     "date": bits[3], "body": ""})
    return rows


def _mailapp_body(msg_id):
    script = ('tell application "Mail" to return content of '
              f'(first message of inbox whose id is {int(msg_id)})')
    out, err = _osa(script, 30)
    return "" if err else out[:MAX_BODY]


def recent(count=5, query=None):
    """The latest messages, newest first. [] if there's no mail set up."""
    which = backend()
    if which == "imap":
        try:
            return _imap_recent(count, query)
        except Exception:
            return []
    if which == "mail_app":
        rows = _mailapp_recent(count if not query else count * 4)
        if query:
            q = query.lower()
            rows = [r for r in rows
                    if q in r["subject"].lower() or q in r["from"].lower()][:count]
        for r in rows[:2]:                      # bodies are slow; only the top
            r["body"] = _mailapp_body(r["id"])
        return rows
    return []


# --------------------------------------------------------------------------- #
# Drafting. Never sending.
# --------------------------------------------------------------------------- #
def valid_address(addr):
    addr = (addr or "").strip()
    return bool(re.match(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[A-Za-z]{2,}$", addr))


def _as_str(text):
    """An AppleScript string literal. Quotes and backslashes are the whole job;
    getting this wrong is how a subject line becomes a syntax error."""
    return '"' + (text or "").replace("\\", "\\\\").replace('"', '\\"') \
                             .replace("\n", '" & linefeed & "') + '"'


def _mailapp_draft(to, subject, body):
    # visible:true, and activate — the draft is SHOWN, not just filed. "I've
    # put it in your drafts" with nothing on screen is how a draft ended up
    # in a Notes note they couldn't find; now the compose window is in front
    # of them, address filled in when known, cursor in the To field when not.
    add_to = (f"  tell d to make new to recipient at end of to recipients "
              f"with properties {{address:{_as_str(to)}}}\n") if to else ""
    script = f'''
tell application "Mail"
  set d to make new outgoing message with properties {{subject:{_as_str(subject)}, content:{_as_str(body)}, visible:true}}
{add_to}  save d
  activate
  return "saved"
end tell
'''
    out, err = _osa(script, 30)
    if err or "saved" not in out:
        return None, (err or "no_draft")
    # Read it back: a draft that isn't in the drafts list didn't happen.
    check, err2 = _osa('tell application "Mail" to return count of '
                       f'(messages of drafts mailbox whose subject is {_as_str(subject)})', 25)
    if err2 or not check.strip().isdigit() or int(check.strip()) < 1:
        return None, "unverified"
    return {"to": to or "(no address yet)", "subject": subject, "where": "Mail"}, None


def _imap_draft(to, subject, body):
    from email.message import EmailMessage
    m = EmailMessage()
    if to:
        m["To"] = to
    m["Subject"] = subject
    m["From"] = (os.getenv("NEO_GMAIL_USER") or "").strip()
    m["Date"] = email.utils.formatdate(localtime=True)
    m.set_content(body)
    M = _imap_conn()
    try:
        box = "[Gmail]/Drafts"
        ok, _ = M.append(box, "\\Draft", imaplib.Time2Internaldate(time.time()),
                         m.as_bytes())
        if ok != "OK":
            return None, "no_draft"
        # Read it back out of Drafts.
        M.select(box, readonly=True)
        safe = subject.replace('"', "")
        ok, data = M.search(None, "HEADER", "Subject", f'"{safe}"')
        found = data[0].split() if ok == "OK" and data and data[0] else []
        if not found:
            return None, "unverified"
        return {"to": to or "(no address yet)", "subject": subject, "where": "Gmail drafts"}, None
    except Exception:
        return None, "no_draft"
    finally:
        try:
            M.logout()
        except Exception:
            pass


def draft(to, subject, body):
    """Save a draft and confirm it exists. (saved, problem).

    `to` may be empty: the draft is still written and shown, with the address
    left for them to fill in. What is refused is a WRONG address."""
    to = (to or "").strip()
    if to and not valid_address(to):
        return None, "bad_address"
    if not (subject or "").strip():
        return None, "no_subject"
    if not (body or "").strip():
        return None, "no_body"
    which = backend()
    if which == "mail_app":
        return _mailapp_draft(to, subject.strip(), body.strip())
    if which == "imap":
        return _imap_draft(to, subject.strip(), body.strip())
    return None, "not_set_up"


TROUBLE = {
    "not_set_up": NOT_SET_UP,
    "bad_address": "That doesn't look like a real email address — say it again?",
    "no_subject": "What should the subject line say?",
    "no_body": "There's nothing in it yet — what do you want it to say?",
    "no_draft": "The mail app wouldn't save that draft.",
    "unverified": "I saved it but it isn't showing in your drafts, so I don't "
                  "trust it — check before you rely on it.",
}
