"""Skill: todo — their to-do list. Capture things hands-free so they stops
forgetting commitments (their actual weak spot: follow-through).

  add     "add email Taylor to my to-do list", "put buy the domain on my list"
  list    "what's on my to-do list", "what are my tasks", "read my to-dos"
  done    "cross off call Jordan", "check off buy the domain", "mark email Taylor
          off my list"

"Remind me to X at five" is NOT this skill any more: that goes to the brain,
whose set_reminder tool writes a real reminder into the Mac's Reminders app
(remind.py) — it shows on their phone and the Mac fires it. This list is for
things they want kept, not things they want to be told about at a time.

State is a JSON list of task objects next to this file. The sentinel reads the
same file to nudge the user when a timed to-do comes due (see sentinel.py) — so
the schema here is the contract: {id, text, created, due, done, done_at}.
`due` is an ISO datetime string or null. Everything the sentinel needs is a
plain field; no import of this module required."""

import datetime
import json
import os
import re

NAME = "todo"
DESCRIPTION = ("their to-do list (NOT timed reminders — those are "
               "set_reminder). Triggers: 'add X to my to-do list', "
               "'put X on my list', 'what's on my to-do list', 'read my tasks', "
               "'cross off X', 'check off X', 'mark X off my list'.")

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "todos_data.json")

# --------------------------------------------------------------------------- #
# intent matching — anchored so it never hijacks normal talk
# --------------------------------------------------------------------------- #
# "remind me" deliberately does NOT trigger this skill: the brain's
# set_reminder puts those in the Mac's Reminders app. Only list words do.
_REMIND = re.compile(r"\bremind me\b.*\b(?:list|to-?do)", re.I)
# a to-do context word, OR a phrase that only makes sense against a list
_CTX = re.compile(r"(to-?do|to do list|\btasks?\b|\breminders?\b|my list|"
                  r"checklist|cross (?:it |them )?off|check (?:it |them )?off|"
                  r"off my list)", re.I)
_LISTQ = re.compile(r"\b(what'?s?|whats|show|read|tell me|list|any(?:thing)?|"
                    r"check|go over|run through|give me)\b", re.I)
_ADDVERB = re.compile(r"\b(add|put|jot|note|create|new|remember to)\b", re.I)
_DONE = re.compile(r"\b(done|did|finish(?:ed)?|complete[d]?|cross(?:ed)? off|"
                   r"check(?:ed)? off|scratch|remove|delete|clear|"
                   r"got (?:it|that) done)\b", re.I)


def classify(text):
    """Which to-do action this utterance wants: 'add' | 'list' | 'done' | None.
    Pure. Requires a reminder cue or list context, so chit-chat never matches."""
    low = text.lower()
    remind = bool(_REMIND.search(low))
    if not (remind or _CTX.search(low)):
        return None
    if _DONE.search(low) and not _ADDVERB.search(low) and not remind:
        return "done"
    if remind or _ADDVERB.search(low):
        return "add"
    if _LISTQ.search(low) or _CTX.search(low):
        return "list"
    return "list"


def matches(text):
    return classify(text) is not None


# --------------------------------------------------------------------------- #
# parsing — pull the task text and any due time out of the utterance
# --------------------------------------------------------------------------- #
_STRIP_ADD = re.compile(
    r"^(?:neo\s+)?(?:please\s+)?"
    r"(?:remind me (?:to|about)|add(?: a)?(?: new)?(?: task| reminder| to-?do)?|"
    r"put|jot(?: down)?|note(?: to self)?|create(?: a)?(?: task| reminder)?|"
    r"remember to|new (?:task|reminder|to-?do))\s+", re.I)
_STRIP_TAIL = re.compile(
    r"\s+(?:to|on|in|onto)?\s*(?:my )?(?:to-?do list|to do list|task list|"
    r"list|tasks?|to-?dos?|reminders?)\.?$", re.I)
_STRIP_DONE = re.compile(
    r"^(?:neo\s+)?(?:please\s+)?"
    r"(?:cross(?:ed)? off|check(?:ed)? off|mark|remove|delete|clear|scratch|"
    r"i(?:'ve)? (?:finished|did|completed|done)|finished|complete[d]?)\s+", re.I)


def extract_add(text):
    """(task_text, due_iso_or_None) from an add/remind utterance. Pure given now
    via parse_due. Returns ('', None) if nothing usable is left after stripping."""
    body = text.strip().rstrip(".!")
    body = _STRIP_ADD.sub("", body, count=1)
    due, body = split_due(body)
    body = _STRIP_TAIL.sub("", body).strip(" ,")
    return body, due


def extract_done_phrase(text):
    """The task phrase to complete, stripped of the done verbs and list tail."""
    body = text.strip().rstrip(".!")
    body = _STRIP_DONE.sub("", body, count=1)
    body = _STRIP_TAIL.sub("", body).strip(" ,")
    # a trailing "done/off" ("cross the gabriel call off") — tidy it
    body = re.sub(r"\b(?:as )?(?:done|off|complete[d]?)$", "", body, flags=re.I).strip(" ,")
    return body


_TIME = re.compile(r"\bat (\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.I)
_IN = re.compile(r"\bin (\d{1,3})\s*(min(?:ute)?s?|h(?:ou)?rs?)\b", re.I)


def split_due(body):
    """Split a trailing/embedded time cue off the task text. Returns
    (due_iso_or_None, cleaned_body). Uses parse_due for the actual datetime."""
    due = parse_due(body, datetime.datetime.now())
    cleaned = body
    for rx in (_TIME, _IN):
        cleaned = rx.sub("", cleaned)
    cleaned = re.sub(r"\b(tomorrow|tonight|today|this evening|this afternoon)\b",
                     "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ,")
    return due, cleaned


def parse_due(text, now):
    """Best-effort due datetime as an ISO string, or None. Pure (now is passed).
    Handles: 'in N min/hours', 'at 5(:30)(pm)', 'tonight', 'tomorrow (at ...)'."""
    low = text.lower()
    m = _IN.search(low)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        delta = datetime.timedelta(minutes=n) if unit.startswith("min") \
            else datetime.timedelta(hours=n)
        return (now + delta).isoformat(timespec="seconds")

    tomorrow = "tomorrow" in low
    base = now + datetime.timedelta(days=1) if tomorrow else now
    m = _TIME.search(low)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        ap = (m.group(3) or "").lower()
        if ap == "pm" and hour < 12:
            hour += 12
        elif ap == "am" and hour == 12:
            hour = 0
        elif not ap and hour < 8:      # "at 5" with no am/pm -> assume evening
            hour += 12
        due = base.replace(hour=min(hour, 23), minute=minute, second=0, microsecond=0)
        if not tomorrow and due <= now:    # already past today -> tomorrow
            due += datetime.timedelta(days=1)
        return due.isoformat(timespec="seconds")
    if "tonight" in low:
        return now.replace(hour=20, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
    if tomorrow:                          # "tomorrow" with no clock time -> 9am
        return base.replace(hour=9, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
    return None


# --------------------------------------------------------------------------- #
# task-list operations — pure, so self_test covers them without touching disk
# --------------------------------------------------------------------------- #
def open_tasks(tasks):
    return [t for t in tasks if not t.get("done")]


def add_task(tasks, text, due, now):
    """Append a task, return (tasks, new_task). Ignores an empty text."""
    if not text:
        return tasks, None
    nid = 1 + max([t.get("id", 0) for t in tasks], default=0)
    t = {"id": nid, "text": text, "created": now.isoformat(timespec="seconds"),
         "due": due, "done": False, "done_at": None}
    tasks.append(t)
    return tasks, t


def _score(task_text, phrase):
    """Word-overlap score of a phrase against a task's text (both lowercased)."""
    a = set(re.findall(r"[a-z0-9]+", task_text.lower()))
    b = set(re.findall(r"[a-z0-9]+", phrase.lower()))
    if not b:
        return 0
    return len(a & b)


def find_open(tasks, phrase):
    """The open task best matching a phrase (word overlap), or None if nothing
    meaningfully overlaps. Pure."""
    best, best_score = None, 0
    for t in open_tasks(tasks):
        s = _score(t["text"], phrase)
        if s > best_score:
            best, best_score = t, s
    return best if best_score > 0 else None


def complete_task(tasks, phrase, now):
    """Mark the best-matching open task done. Returns (tasks, task_or_None)."""
    t = find_open(tasks, phrase)
    if t is not None:
        t["done"] = True
        t["done_at"] = now.isoformat(timespec="seconds")
    return tasks, t


def _due_phrase(iso, now):
    """A short spoken 'by 5 PM' / 'tomorrow' tail for a due time, or ''."""
    if not iso:
        return ""
    try:
        d = datetime.datetime.fromisoformat(iso)
    except Exception:
        return ""
    if d.date() == now.date():
        return d.strftime(" by %-I:%M %p").replace(":00", "")
    if d.date() == (now + datetime.timedelta(days=1)).date():
        return d.strftime(" tomorrow at %-I:%M %p").replace(":00", "")
    return d.strftime(" on %b %-d")


def format_list(tasks, now):
    """Spoken rundown of open tasks (1-2 breaths). Pure."""
    todo = open_tasks(tasks)
    if not todo:
        return "Your to-do list is clear. Nothing on it."
    n = len(todo)
    head = f"You've got {n} thing{'s' if n != 1 else ''} on your list: "
    items = [t["text"] + _due_phrase(t.get("due"), now) for t in todo[:6]]
    tail = "" if n <= 6 else f", and {n - 6} more"
    return head + "; ".join(items) + tail + "."


# --------------------------------------------------------------------------- #
# disk IO — thin wrappers; all logic above is pure
# --------------------------------------------------------------------------- #
def _load():
    try:
        with open(_DATA, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("tasks", []) if isinstance(data, dict) else []
    except Exception:
        return []


def _save(tasks):
    try:
        with open(_DATA, "w", encoding="utf-8") as f:
            json.dump({"tasks": tasks}, f, indent=1)
        return True
    except Exception as e:
        print(f"[todo] save failed: {e}")
        return False


def handle(text, ctx):
    try:
        now = datetime.datetime.now()
        action = classify(text)
        if action == "list":
            return format_list(_load(), now)
        if action == "done":
            tasks = _load()
            phrase = extract_done_phrase(text)
            tasks, t = complete_task(tasks, phrase, now)
            if t is None:
                return ("I couldn't find that on your list. Say 'what's on my "
                        "to-do list' and I'll read it back.")
            _save(tasks)
            left = len(open_tasks(tasks))
            tail = f"{left} left." if left else "That was the last one — clear list."
            return f"Done — crossed off {t['text']}. {tail}"
        # default: add
        task_text, due = extract_add(text)
        if not task_text:
            return "What should I add? Say 'remind me to' and the thing."
        tasks = _load()
        # schedule the spoken reminder too, if it's due soon and voice is wired
        tasks, t = add_task(tasks, task_text, due, now)
        _save(tasks)
        if due:
            secs = (datetime.datetime.fromisoformat(due) - now).total_seconds()
            if 0 < secs <= 6 * 3600:      # within 6h: Neo will speak it out loud
                ctx.later(secs, f"Reminder: {task_text}.")
            return f"Got it. I'll remind you to {task_text}{_due_phrase(due, now)}."
        return f"Added {task_text} to your list."
    except Exception as e:
        print(f"[todo] handle error: {e}")
        return "Something went wrong with your to-do list. Try again in a moment."


def self_test():
    now = datetime.datetime(2026, 7, 28, 14, 0, 0)     # a Tuesday 2 PM
    ok = True

    # classify: positives
    ok = ok and classify("remind me to call Jordan at 5") is None   # -> Reminders app
    ok = ok and classify("remind me to add call Jordan to my list") == "add"
    ok = ok and classify("add email Taylor to my to-do list") == "add"
    ok = ok and classify("put buy the domain on my list") == "add"
    ok = ok and classify("what's on my to-do list") == "list"
    ok = ok and classify("read my tasks") == "list"
    ok = ok and classify("cross off buy the domain") == "done"
    ok = ok and classify("check off call Jordan") == "done"
    # classify: negatives (must not hijack normal talk)
    ok = ok and classify("what's the weather today") is None
    ok = ok and classify("tell me a joke") is None
    ok = ok and classify("how are you") is None
    ok = ok and classify("") is None

    # add + list
    tasks = []
    tasks, t = add_task(tasks, "call Jordan", None, now)
    ok = ok and t["id"] == 1 and len(open_tasks(tasks)) == 1
    tasks, t2 = add_task(tasks, "email Taylor", None, now)
    ok = ok and t2["id"] == 2
    ok = ok and "call Jordan" in format_list(tasks, now)
    ok = ok and add_task([], "", None, now)[1] is None      # empty ignored
    ok = ok and format_list([], now).lower().startswith("your to-do list is clear")

    # complete by fuzzy phrase
    tasks, done = complete_task(tasks, "jordan", now)
    ok = ok and done is not None and done["text"] == "call Jordan"
    ok = ok and len(open_tasks(tasks)) == 1
    ok = ok and complete_task(tasks, "nonexistent thing", now)[1] is None

    # parsing: task text + due
    body, due = extract_add("add call Jordan at 5 to my list")
    ok = ok and body == "call Jordan" and due is not None
    ok = ok and datetime.datetime.fromisoformat(due).hour == 17     # "5" -> 5 PM
    body2, _ = extract_add("add email Taylor to my to-do list")
    ok = ok and body2 == "email Taylor"
    body3, _ = extract_add("put buy the domain on my list")
    ok = ok and body3 == "buy the domain"
    ok = ok and extract_done_phrase("cross off buy the domain") == "buy the domain"

    # due parsing cases (pure, now-relative)
    ok = ok and parse_due("in 30 minutes", now) == (now + datetime.timedelta(minutes=30)).isoformat(timespec="seconds")
    ok = ok and datetime.datetime.fromisoformat(parse_due("tonight", now)).hour == 20
    tm = parse_due("tomorrow", now)
    ok = ok and datetime.datetime.fromisoformat(tm).date() == (now + datetime.timedelta(days=1)).date()
    ok = ok and parse_due("no time here", now) is None
    # "at 3" earlier today rolls to tomorrow
    at3 = datetime.datetime.fromisoformat(parse_due("at 3", now))
    ok = ok and at3 > now

    return bool(ok)


if __name__ == "__main__":
    print("self_test:", self_test())
