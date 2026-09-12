"""
workflows.py — things that take several steps, done as one.

A skill is one thing. A workflow is the chain a person would otherwise
have to ask for piece by piece. Each one below composes parts that already
exist and work, reads back what it found, and says plainly what it could
not reach. Pure formatting is separated from the impure gathering so the
words can be tested without a calendar.
"""

import datetime as dt
import re
import subprocess


# --------------------------------------------------------------------------- #
# Morning brief: today's calendar, what's due, the weather, the inbox.
# --------------------------------------------------------------------------- #
def gather_brief(now=None, log=print):
    """Everything the brief can know, each part best-effort and labelled."""
    now = now or dt.datetime.now()
    out = {"now": now, "events": None, "todos": None, "weather": None, "mail": None}
    try:
        import importlib.util as ilu, os
        sp = ilu.spec_from_file_location("calendar_peek", os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "skills", "calendar_peek.py"))
        cp = ilu.module_from_spec(sp); sp.loader.exec_module(cp)
        events, err = cp._collect_range(0, 1)
        out["events"] = None if err else [(t, title) for d, t, title in events]
    except Exception as e:
        log(f"[brief] calendar: {e}")
    try:
        import importlib.util as ilu, os
        sp = ilu.spec_from_file_location("todo", os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "skills", "todo.py"))
        td = ilu.module_from_spec(sp); sp.loader.exec_module(td)
        tasks = td.open_tasks(td._load())
        due = [t["text"] for t in tasks if t.get("due") and t["due"][:10] <= now.strftime("%Y-%m-%d")]
        out["todos"] = {"open": len(tasks), "due": due[:5]}
    except Exception as e:
        log(f"[brief] todos: {e}")
    try:
        import agent
        w = agent.get_weather("")
        out["weather"] = w if w and "couldn't" not in w.lower() else None
    except Exception as e:
        log(f"[brief] weather: {e}")
    try:
        import mail
        if mail.backend():
            rows = mail.recent(5)
            out["mail"] = [(r["from"], r["subject"]) for r in rows]
    except Exception as e:
        log(f"[brief] mail: {e}")
    return out


def format_brief(data):
    """One spoken paragraph. Pure."""
    now = data["now"]
    bits = [f"It's {now.strftime('%A')}, {now.strftime('%-d %B')}."]
    ev = data.get("events")
    if ev is None:
        bits.append("I can't see the calendar (say 'connect calendar').")
    elif not ev:
        bits.append("Nothing on the calendar today.")
    else:
        timed = [(t, title) for t, title in ev if t != "all-day"]
        allday = [title for t, title in ev if t == "all-day"]
        if timed:
            first = timed[0]
            bits.append(f"{len(timed)} thing{'s' if len(timed) != 1 else ''} on the calendar, first is "
                        f"{first[1]} at {_spoken_time(first[0])}.")
        if allday:
            bits.append("All day: " + ", ".join(allday[:3]) + ".")
    td = data.get("todos")
    if td and td["due"]:
        bits.append(f"Due today: {', '.join(td['due'][:3])}.")
    elif td and td["open"]:
        bits.append(f"{td['open']} open to-dos, none due today.")
    if data.get("weather"):
        bits.append(data["weather"].split(". ")[0].rstrip(".") + ".")
    m = data.get("mail")
    if m:
        bits.append(f"Latest email is from {m[0][0]} about {m[0][1]}.")
    return " ".join(bits)


def _spoken_time(hhmm):
    try:
        h, m = [int(x) for x in hhmm.split(":")]
    except ValueError:
        return hhmm
    ap = "in the morning" if h < 12 else ("in the afternoon" if h < 18 else "in the evening")
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {ap}" if m else f"{h12} {ap}"


# --------------------------------------------------------------------------- #
# Prep for a meeting with someone: who they are, what they last wrote, when.
# --------------------------------------------------------------------------- #
def prep(person, log=print):
    out = {"person": person, "contact": None, "mail": [], "events": []}
    try:
        import contacts
        addr, who = contacts.resolve(person)
        out["contact"] = {"email": addr, "name": (who or {}).get("name") if isinstance(who, dict) else None}
    except Exception as e:
        log(f"[prep] contact: {e}")
    try:
        import mail
        if mail.backend():
            out["mail"] = [(r["from"], r["subject"], (r.get("body") or "")[:300]) for r in mail.recent(3, person)]
    except Exception as e:
        log(f"[prep] mail: {e}")
    try:
        import importlib.util as ilu, os
        sp = ilu.spec_from_file_location("calendar_peek", os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "skills", "calendar_peek.py"))
        cp = ilu.module_from_spec(sp); sp.loader.exec_module(cp)
        events, err = cp._collect_range(0, 14)
        if not err:
            low = person.lower().split()[0]
            out["events"] = [(str(d), t, title) for d, t, title in events if low in title.lower()][:3]
    except Exception as e:
        log(f"[prep] calendar: {e}")
    return out


def format_prep(p):
    """Pure."""
    bits = []
    c = p.get("contact") or {}
    if c.get("email"):
        bits.append(f"{c.get('name') or p['person']}: {c['email']}.")
    else:
        bits.append(f"I don't have an address for {p['person']} yet.")
    if p["mail"]:
        f, subj, body = p["mail"][0]
        bits.append(f"Last email from them was about {subj}" + (f": {body[:140].strip()}" if body else "") + ".")
    else:
        bits.append("No recent email from them that I can see.")
    if p["events"]:
        d, t, title = p["events"][0]
        bits.append(f"On the calendar: {title} on {d}" + (f" at {_spoken_time(t)}" if t != "all-day" else "") + ".")
    return " ".join(bits)


# --------------------------------------------------------------------------- #
# The text on the screen, into the clipboard.
# --------------------------------------------------------------------------- #
def screen_text_to_clipboard(log=print):
    import act, desk
    text = act.screen_text(log=log)
    text = re.sub(r"\n{3,}", "\n\n", text or "").strip()
    if not text:
        return 0
    desk.set_clipboard(text)
    return len(text.split())


# --------------------------------------------------------------------------- #
# A message in Messages, written but NOT sent (the same line as mail).
# --------------------------------------------------------------------------- #
def open_message_to(name, text, log=print):
    """Open the Messages conversation for `name` and type the text into the
    field, leaving Send to them. Returns (ok, note)."""
    import contacts, hands
    number = contacts.phone(name)
    addr, who = contacts.resolve(name) if not number else (None, None)
    target = number or addr
    if not target:
        return False, f"I don't have a number or address for {name}."
    import urllib.parse
    subprocess.run(["open", "imessage://" + urllib.parse.quote(target)], capture_output=True)
    import time
    time.sleep(2.0)
    hands.type_text(text)
    return True, f"Messages is open to {who.get('name') if isinstance(who, dict) and who else name} with the text typed; you press send."


# --------------------------------------------------------------------------- #
# Volume, in words
# --------------------------------------------------------------------------- #
def set_volume(level):
    """0-100, or words: mute, quiet, half, loud, max. Returns the level set."""
    words = {"mute": 0, "muted": 0, "silent": 0, "quiet": 20, "low": 25, "half": 50,
             "medium": 50, "loud": 80, "high": 80, "max": 100, "full": 100}
    try:
        n = int(level)
    except (TypeError, ValueError):
        n = words.get(str(level or "").lower().strip(), None)
    if n is None:
        m = re.search(r"\d+", str(level or ""))
        n = int(m.group(0)) if m else 50
    n = max(0, min(100, n))
    subprocess.run(["osascript", "-e", f"set volume output volume {n}"], capture_output=True, timeout=5)
    r = subprocess.run(["osascript", "-e", "output volume of (get volume settings)"],
                       capture_output=True, text=True, timeout=5)
    try:
        return int(r.stdout.strip())
    except ValueError:
        return n
