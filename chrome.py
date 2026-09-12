"""
chrome.py — Neo drives Google Chrome, with the RIGHT profile.

"Open Schoology in my school profile", "google the French Revolution in my
personal account", "pull up gmail on my work profile". Neo opens the page in
the Chrome profile you name — using that profile's real logged-in session, so
things you're already signed into just work. If a page drops you on a login or
OAuth wall, Neo doesn't try to fake it — it CALLS YOU: speaks up and drops a
card so you come punch it in.

How it works (macOS):
  - Chrome stores its profiles as folders ("Default", "Profile 1", ...) and
    maps them to the friendly names you see ("the user School") in its
    `Local State` JSON. We read that so you can talk in friendly names.
  - Opening a URL in a specific profile:
        /Applications/Google Chrome.app/.../Google Chrome
            --args --profile-directory="Profile 1" "https://..."
    Works whether or not Chrome is already running.
  - Login-wall detection: after opening, we read the active tab's URL via
    AppleScript (no special Chrome setting needed) and check it against known
    sign-in / OAuth patterns.

Pure, testable pieces: load_profiles, resolve_profile, parse_request,
looks_like_login, target_url. The two impure calls (launch, read-tab) are
isolated at the bottom.
"""

import json
import os
import re
import subprocess
import time
import urllib.parse

CHROME_APP = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_SUPPORT = os.path.expanduser("~/Library/Application Support/Google/Chrome")
LOCAL_STATE = os.path.join(CHROME_SUPPORT, "Local State")

# friendly site words -> a URL. Everything else falls back to a Google search
# or, if it looks like a domain, the domain itself.
SITES = {
    "schoology": "https://app.schoology.com",
    "gmail": "https://mail.google.com",
    "email": "https://mail.google.com",
    "classroom": "https://classroom.google.com",
    "google classroom": "https://classroom.google.com",
    "canvas": "https://canvas.instructure.com",
    "drive": "https://drive.google.com",
    "google drive": "https://drive.google.com",
    "docs": "https://docs.google.com",
    "calendar": "https://calendar.google.com",
    "youtube": "https://youtube.com",
    "github": "https://github.com",
    "notion": "https://notion.so",
    "chatgpt": "https://chat.openai.com",
    "linkedin": "https://linkedin.com",
    "instagram": "https://instagram.com",
}

# a tab sitting on one of these is a sign-in / OAuth wall -> call the user.
# Host match OR a login WORD as a whole URL segment (so '/authors' and
# 'lessons' don't false-trigger on 'auth'/'sso').
_LOGIN_HOSTS = ("accounts.google.com", "login.microsoftonline.com", "okta.com",
                "auth0.com", "clever.com", "id.schoology.com", "login.live.com",
                "login.microsoft.com", "signin.")
_LOGIN_SEG = re.compile(
    r"(?:^|[/?#&=.])(?:sign-?in|log-?in|oauth2?|sso|authorize|auth|session/new)"
    r"(?:[/?#&=]|$)", re.I)


def installed():
    return os.path.exists(CHROME_APP)


# --------------------------------------------------------------------------- #
# profiles (pure given the Local State text)
# --------------------------------------------------------------------------- #
def load_profiles(local_state_text=None):
    """{friendly_name_lower: profile_dir}. Also indexes the dir name itself so
    'profile 1' works. Empty dict if Chrome data isn't readable."""
    try:
        if local_state_text is None:
            with open(LOCAL_STATE, encoding="utf-8") as f:
                local_state_text = f.read()
        cache = json.loads(local_state_text).get("profile", {}).get("info_cache", {})
    except (OSError, ValueError):
        return {}
    out = {}
    for dir_name, info in cache.items():
        out[dir_name.lower()] = dir_name
        name = (info.get("name") or "").strip().lower()
        if name:
            out.setdefault(name, dir_name)
    return out


def resolve_profile(spoken, profiles):
    """Map 'school' / 'the user school' / 'personal' to a profile dir, fuzzily."""
    if not spoken or not profiles:
        return None
    s = re.sub(r"\b(my|the|profile|account|chrome)\b", " ", spoken.lower()).strip()
    if not s:
        return None
    if s in profiles:
        return profiles[s]
    for name, d in profiles.items():                 # substring either way
        if s in name or name in s:
            return d
    sw = set(s.split())                              # word overlap
    for name, d in profiles.items():
        if sw & set(name.split()):
            return d
    return None


# --------------------------------------------------------------------------- #
# request parsing (pure)
# --------------------------------------------------------------------------- #
_PROFILE_RE = re.compile(
    r"\b(?:in|on|using|with|from)\s+(?:my\s+)?([a-z0-9][a-z0-9 ]*?)\s+"
    r"(?:profile|account|chrome)\b", re.I)

# "check my email", "any new emails", "open my inbox" — the supervised-account
# workaround: Neo can't read mail over IMAP, but it can OPEN Gmail in the right
# profile with the real logged-in session.
_EMAIL_TRIGGER = re.compile(
    r"\b(?:check|open|read|show|see|pull up|any|new|got)\b[^.]*?\b(?:e-?mails?|inbox|gmail)\b",
    re.I)
_EMAIL_PROFILE = re.compile(
    r"\bmy\s+([a-z0-9]+)\s+(?:e-?mails?|inbox|gmail|account)\b", re.I)


# 'email' as a DATA FIELD, not your inbox — these mean it's NOT an inbox check
# ("check the database for users and their email addresses" is a Claude task).
_NOT_INBOX = re.compile(
    r"\b(database|db|users?|signups?|sign ups|verified|records?|table|query|"
    r"column|rows?|their e-?mail|e-?mail address(?:es)?|schema|migration|"
    r"backend|the code)\b", re.I)


def is_email(text):
    """A genuine 'open my inbox' request — not a DB query that mentions email."""
    if _NOT_INBOX.search(text):
        return False
    return bool(_EMAIL_TRIGGER.search(text))


def _profile_phrase(text):
    m = _PROFILE_RE.search(text)
    if m:
        return m.group(1).strip()
    m = _EMAIL_PROFILE.search(text)      # "my school email" -> "school"
    return m.group(1).strip() if m else None


def target_url(text):
    """What page does the request want? (site alias | domain | google search)"""
    low = text.lower()
    if is_email(text):                    # "check my email" -> Gmail
        return SITES["gmail"]
    # strip the profile clause so it doesn't pollute the query
    low = _PROFILE_RE.sub(" ", low)
    # explicit google/search
    m = re.search(r"\b(?:google|search(?:\s+for|\s+up)?)\s+(.+)$", low)
    if m:
        q = m.group(1).strip(" .?")
        # "search for X" where X is a known site -> the site, else a query
        if q in SITES:
            return SITES[q]
        return "https://www.google.com/search?q=" + urllib.parse.quote_plus(q)
    # open/go-to a named site or domain
    m = re.search(r"\b(?:open|go to|goto|pull up|bring up|launch|visit)\s+(.+)$", low)
    tail = (m.group(1) if m else low).strip(" .?")
    for name in sorted(SITES, key=len, reverse=True):
        if re.search(r"\b" + re.escape(name) + r"\b", tail):
            return SITES[name]
    if re.search(r"[a-z0-9-]+\.[a-z]{2,}", tail):    # looks like a domain
        dom = re.search(r"([a-z0-9.-]+\.[a-z]{2,}\S*)", tail).group(1)
        return dom if dom.startswith("http") else "https://" + dom
    return None


def parse_request(text):
    """-> (url, profile_phrase) or None. profile_phrase may be None."""
    low = text.lower()
    if not (is_email(text) or any(w in low for w in (
            "chrome", "profile", "account", "open ", "go to", "google",
            "search", "pull up", "schoology", "inbox", "gmail"))):
        return None
    url = target_url(text)
    if not url:
        return None
    return url, _profile_phrase(text)


def looks_like_login(url):
    if not url:
        return False
    u = url.lower()
    if any(h in u for h in _LOGIN_HOSTS):
        return True
    parts = urllib.parse.urlparse(u)
    return bool(_LOGIN_SEG.search(parts.path + "?" + (parts.query or "")))


def wants_chrome(text):
    """Route hint. Fires for a named PROFILE request ('in my X profile'), OR
    any email/inbox request (the supervised-account fix — open Gmail in the
    browser instead of reading it over IMAP)."""
    if is_email(text):
        return True
    return _profile_phrase(text) is not None and parse_request(text) is not None


# --------------------------------------------------------------------------- #
# the two impure calls (macOS only)
# --------------------------------------------------------------------------- #
def open_in_profile(url, profile_dir=None):
    """Launch the URL, optionally in a specific profile dir. Returns True/err."""
    if not installed():
        return "Chrome isn't installed where I expect it."
    args = [CHROME_APP, "--args"]
    if profile_dir:
        args.append(f"--profile-directory={profile_dir}")
    args.append(url)
    try:
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        return f"Couldn't launch Chrome: {e}"


_ALL_TABS = ('tell application "Google Chrome"\n'
             '  set out to ""\n'
             '  repeat with w in windows\n'
             '    repeat with t in tabs of w\n'
             '      set out to out & (URL of t) & linefeed\n'
             '    end repeat\n'
             '  end repeat\n'
             '  return out\n'
             'end tell')


def all_tab_urls():
    """Every open tab URL across ALL windows/profiles. [] on failure.
    (Robust to many windows — 'front window' isn't reliable with 10 profiles.)"""
    try:
        r = subprocess.run(["osascript", "-e", _ALL_TABS],
                           capture_output=True, text=True, timeout=6)
        return [u.strip() for u in (r.stdout or "").splitlines() if u.strip()]
    except Exception:
        return []


_CLOSE_TABS = ('tell application "Google Chrome"\n'
               '  set n to 0\n'
               '  repeat with w in windows\n'
               '    repeat with t in (tabs of w)\n'
               '      if (URL of t) contains "%s" then\n'
               '        close t\n'
               '        set n to n + 1\n'
               '      end if\n'
               '    end repeat\n'
               '  end repeat\n'
               '  return n\n'
               'end tell')


def close_tabs_containing(fragment):
    """Close every tab whose URL contains `fragment`, in every window and
    profile. Returns how many. Used so a re-proposed calendar event REPLACES
    the editor that is open instead of piling a second one beside it."""
    frag = str(fragment or "").replace('"', "")
    if not frag:
        return 0
    try:
        r = subprocess.run(["osascript", "-e", _CLOSE_TABS % frag],
                           capture_output=True, text=True, timeout=8)
        return int((r.stdout or "0").strip() or 0)
    except Exception:
        return 0


def _host(url):
    try:
        return urllib.parse.urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def check_landing(target_url, timeout=7.0):
    """After opening target_url, find the tab that matters and return its URL.
    Prefers a tab on the target's host (or a login host we got redirected to)
    over whatever random window is frontmost. '' if nothing relevant found."""
    thost = _host(target_url)
    deadline = time.time() + timeout
    best = ""
    while time.time() < deadline:
        tabs = all_tab_urls()
        # a login wall on any tab is the strongest signal
        for u in tabs:
            if looks_like_login(u):
                return u
        # else the tab sitting on our target host
        for u in tabs:
            h = _host(u)
            if thost and (h == thost or h.endswith("." + thost) or thost.endswith("." + h)):
                best = u
        if best:
            return best
        time.sleep(0.6)
    return best


# --------------------------------------------------------------------------- #
# orchestration — what Neo calls
# --------------------------------------------------------------------------- #
def go(text, notify=None, say=None):
    """Open the requested page in the requested profile; call the user on a login
    wall. Returns the line Neo speaks. notify(title, detail, urgency) and
    say(text) are optional (Neo passes them in)."""
    parsed = parse_request(text)
    if not parsed:
        return "I couldn't tell what to open. Try: open Schoology in my school profile."
    url, prof_phrase = parsed
    profiles = load_profiles()
    profile_dir, prof_name = None, None
    if prof_phrase:
        profile_dir = resolve_profile(prof_phrase, profiles)
        prof_name = prof_phrase
        if profile_dir is None and profiles:
            # Name them. This list was built and then not used, so the answer
            # was "I don't see that profile — which one?" with no way for them
            # to know what the options are. Asking a question you already have
            # the answer to is worse than not asking.
            have = ", ".join(sorted({k for k in profiles if " " not in k or k.istitle()})) \
                   or "your profiles"
            return (f"I don't see a '{prof_phrase}' profile in Chrome. "
                    f"I've got {have}. Which one?")
    res = open_in_profile(url, profile_dir)
    if res is not True:
        return res
    where = f" in your {prof_name} profile" if prof_name else ""
    site = urllib.parse.urlparse(url).netloc.replace("www.", "") or "that"

    # find OUR tab (across all windows) and see if it hit a login wall
    landed = check_landing(url)
    if looks_like_login(landed):
        line = (f"I opened {site}{where}, but it wants you to sign in — "
                "that part's on you. Go log in and I'll take it from there.")
        if notify:
            notify(f"Sign-in needed: {site}", line, "high")
        if say:
            say(line)
        return line
    return f"Opened {site}{where}."


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "open schoology in my school profile"
    print("installed:", installed())
    print("profiles:", load_profiles())
    print("parse:", parse_request(q))
    print(go(q))
