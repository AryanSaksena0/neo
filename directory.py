"""
directory.py — finding a person you have never emailed.

Contacts and the inbox cover people you already know. A classmate, a
teacher, a colleague you have never written to is in the ORGANISATION'S
directory — and Google exposes that directory as a plain page:

    https://contacts.google.com/search/<name>

Signed in to a school or work Google account, that page lists the matching
people with their addresses. Two ways to read it, tried in order:

  1. Neo's own background browser (webdrive) — silent, DOM text, no window.
     Works once the person has signed Neo's browser into that account.
  2. The person's own Chrome, in the profile that belongs to that
     organisation — the page opens on screen for a few seconds and Neo READS
     it (OCR), the way it reads anything else. Nothing to set up; this is the
     "Neo in Chrome" route.

Both end the same way: (name, email) or nothing. Never a guess.
"""

import re
import time

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.I)
_URL = "https://contacts.google.com/search/{q}"


def search_url(name):
    import urllib.parse
    return _URL.format(q=urllib.parse.quote(name.strip()))


def parse_people(text, name):
    """Pure. From the text of a directory/contacts page, the people whose
    line carries an address and whose name matches. Best first."""
    import contacts
    out, seen = [], set()
    lines = [l.strip() for l in str(text or "").splitlines() if l.strip()]
    for i, line in enumerate(lines):
        for m in _EMAIL.finditer(line):
            addr = m.group(0).lower()
            if addr in seen or addr.endswith((".png", ".jpg")):
                continue
            # the name is on this line before the address, or the line above
            before = line[:m.start()].strip(" -–·|:")
            cand = before if len(before) > 1 else (lines[i - 1] if i else "")
            cand = _EMAIL.sub("", cand).strip(" -–·|:")
            sc = contacts.score(name, cand) if cand else 0
            if sc == 0 and name.lower().split()[0] in addr.split("@")[0]:
                sc = 1
            if sc:
                seen.add(addr)
                out.append((sc, {"name": cand or addr, "emails": [addr], "source": "directory"}))
    out.sort(key=lambda x: -x[0])
    return [p for _, p in out]


def via_neo_browser(name, log=print):
    """Silent: Neo's own browser, if it is signed in. ([] if not)."""
    try:
        import webdrive
        if not webdrive.available():
            return []
        text, problem = webdrive.open_and_read(search_url(name), log=log)
        if problem:
            return []
        return parse_people(text, name)
    except Exception:
        return []


def _org_profile(name_hint=""):
    """Which of their Chrome profiles is the school/work one (person.py)."""
    try:
        import person
        browsers = person.load().get("accounts", {}).get("browsers", [])
        for want in ("school", "work"):
            for b in browsers:
                if b.get("app") == "Chrome" and b.get("kind") == want and b.get("dir"):
                    return b["dir"], b.get("email", "")
    except Exception:
        pass
    return None, ""


def via_user_chrome(name, profile_dir=None, log=print, settle=4.5):
    """On screen: open the directory search in THEIR Chrome (the org profile),
    read the page, come back with what it says."""
    import chrome, act
    if not chrome.installed():
        return [], "no_chrome"
    if profile_dir is None:
        profile_dir, _ = _org_profile()
    ok = chrome.open_in_profile(search_url(name), profile_dir)
    if ok is not True:
        return [], "no_chrome"
    time.sleep(settle)
    try:
        text = act.screen_text(log=log)
    except Exception as e:
        log(f"[directory] couldn't read the screen: {e}")
        return [], "no_read"
    people = parse_people(text, name)
    return people, (None if people else "not_found")


def find(name, log=print, visible=True):
    """(person or None, how). Silent route first; the on-screen one only when
    allowed (not in stealth, and the caller wants it)."""
    name = (name or "").strip()
    if not name:
        return None, "no_name"
    hits = via_neo_browser(name, log=log)
    if hits:
        return hits[0], "neo_browser"
    if not visible:
        return None, "needs_window"
    hits, problem = via_user_chrome(name, log=log)
    if hits:
        return hits[0], "chrome"
    return None, problem or "not_found"
