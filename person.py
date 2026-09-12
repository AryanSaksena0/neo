"""
person.py — who Neo works for. The second brain, structured.

memory.json is a list of sentences the model chose to keep. That is fine for
"Sam replied", and useless for the things an assistant has to know on day
one and keep current without being told: how this person likes answers, how
they write, what their week looks like, which browser is work and which is
personal, what is installed, what Neo is allowed to touch.

This file BUILDS those from the machine itself, on a schedule, and hands the
prompt one compact block. Nothing here asks a question or raises a permission
dialog: a source that isn't reachable is recorded as "needs X" so the
onboarding and the doctor can say exactly what would unlock it.

THE NON-NEGOTIABLES, one section each:
  person      name, first name, timezone, locale
  response    how they want answers: length, detail, spoken vs text  (LEARNED)
  writing     how they write: greeting, sign-off, sentence length, formality
              (from sent mail, and iMessage when Full Disk Access allows)
  routine     active hours, the recurring shape of the week
  accounts    browsers and their profiles, each tagged work/personal/school/
              family/unknown; the user corrects a tag by saying so
  work        what they're working on: recently used documents and folders
  apps        what's installed, and which permissions Neo has
  people      who comes up, with addresses when known
  boundaries  the lines Neo never crosses (draft-only mail, no em dashes...)
  needs       what would unlock more (Full Disk Access, Mail set up...)

Every builder is best-effort and silent. `refresh()` runs them all and is
called once at boot and once a day; `brief()` is what the model sees.
"""

import collections
import datetime as dt
import glob
import json
import os
import re
import sqlite3
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.getenv("NEO_PROFILE_PATH") or os.path.join(HERE, "profile.json")
STALE_S = 24 * 3600

PERSONAL_DOMAINS = ("gmail.com", "googlemail.com", "icloud.com", "me.com", "mac.com",
                    "yahoo.com", "hotmail.com", "outlook.com", "live.com", "proton.me",
                    "protonmail.com", "aol.com")

BOUNDARIES = [
    "Mail is draft-only: write it, open it, never send it.",
    "No em dashes in anything written for them.",
    "Nothing is confirmed that wasn't read back (calendar, reminders, drafts).",
    "The mic is only open while the key is held.",
    "Never spend money or switch on a paid provider.",
]


def _now():
    return dt.datetime.now()


def load():
    try:
        with open(PATH, encoding="utf-8") as f:
            p = json.load(f)
            if isinstance(p, dict):
                return p
    except (OSError, ValueError):
        pass
    return {"updated": {}}


def save(p):
    try:
        with open(PATH, "w", encoding="utf-8") as f:
            json.dump(p, f, indent=2, ensure_ascii=False)
    except OSError:
        pass


def stale(p=None, section=None, max_age=STALE_S):
    p = p or load()
    upd = p.get("updated", {})
    keys = [section] if section else list(SECTIONS)
    for k in keys:
        try:
            when = dt.datetime.fromisoformat(upd.get(k, "1970-01-01"))
        except ValueError:
            return True
        if (_now() - when).total_seconds() > max_age:
            return True
    return False


# --------------------------------------------------------------------------- #
# person
# --------------------------------------------------------------------------- #
def build_person():
    prev = load().get("person", {})
    name = (os.getenv("NEO_USER_NAME") or prev.get("name") or "").strip()
    if not name:
        try:
            name = subprocess.run(["id", "-F"], capture_output=True, text=True,
                                  timeout=3).stdout.strip()
        except Exception:
            name = ""
    tz = dt.datetime.now().astimezone().tzname() or ""
    loc = (os.getenv("LANG") or "").split(".")[0]
    return {"name": name, "first_name": prev.get("first_name") or (name.split()[0] if name else ""),
            "role": prev.get("role", ""), "timezone": tz, "locale": loc}


# --------------------------------------------------------------------------- #
# response — learned from what they say back. Pure matcher + merge.
# --------------------------------------------------------------------------- #
_PREF = [
    (re.compile(r"\b(too long|shorter|be brief|less talking|keep it short|one sentence|"
                r"short answers?|stop rambling|too much)\b", re.I), {"length": "short"}),
    (re.compile(r"\b(more detail|go deeper|longer|explain more|elaborate|"
                r"in depth|walk me through it)\b", re.I), {"length": "long"}),
    (re.compile(r"\b(just (show|put) it|on screen|don'?t (read|say) it out|"
                r"silent(ly)?|text only|write it (down|out))\b", re.I), {"mode": "text"}),
    (re.compile(r"\b(say it|read it (out|to me)|tell me out loud|talk to me)\b", re.I),
     {"mode": "voice"}),
    (re.compile(r"\b(no jokes?|stop joking|be serious|cut the (jokes|banter))\b", re.I),
     {"humour": "none"}),
    (re.compile(r"\b(bullet points|as a list|list them)\b", re.I), {"shape": "list"}),
]


_NEGATED = re.compile(r"\b(don'?t|do not|stop|no need to|without)\b[^.!?]{0,20}$", re.I)


def preference_from(text):
    """What a sentence says about how they want answers, if anything.
    Pure. {"length": "short"} for 'that was too long', {} for 'what time is it'.
    "Don't read it out" is NOT a request for voice: a match preceded by a
    negation is dropped."""
    text = str(text or "")
    out = {}
    for rx, pref in _PREF:
        m = rx.search(text)
        if not m:
            continue
        if _NEGATED.search(text[:m.start()]):
            continue
        out.update(pref)
    return out


def note_preference(p, pref, source="said"):
    """Merge a learned preference in, keeping a short history so a one-off
    'longer' doesn't erase a month of 'shorter'."""
    if not pref:
        return p
    r = p.setdefault("response", {})
    hist = r.setdefault("history", [])
    hist.append({"at": _now().isoformat(timespec="seconds"), "pref": pref, "source": source})
    r["history"] = hist[-30:]
    # the setting is the majority of the last five signals per key
    for key in {k for h in r["history"] for k in h["pref"]}:
        last = [h["pref"][key] for h in r["history"] if key in h["pref"]][-5:]
        r[key] = collections.Counter(last).most_common(1)[0][0]
    p.setdefault("updated", {})["response"] = _now().isoformat(timespec="seconds")
    return p


# --------------------------------------------------------------------------- #
# writing — from what they actually sent
# --------------------------------------------------------------------------- #
_GREETS = re.compile(r"^\s*(hi|hello|hey|dear|good (?:morning|afternoon|evening)|yo)\b[^\n]*", re.I | re.M)
_SIGNS = re.compile(r"^\s*(best|best regards|kind regards|regards|thanks|thank you|many thanks|"
                    r"cheers|sincerely|warmly|all the best|talk soon|later|ttyl)[,.!]?\s*$", re.I | re.M)
_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")


def writing_stats(samples):
    """Pure. A style profile from a list of texts the person wrote."""
    samples = [s for s in (samples or []) if s and len(s.strip()) > 8]
    if not samples:
        return {}
    greets = collections.Counter()
    signs = collections.Counter()
    sent_lens, words, excl, lower_starts, emoji = [], 0, 0, 0, 0
    for s in samples:
        g = _GREETS.search(s)
        if g:
            greets[g.group(1).lower()] += 1
        for m in _SIGNS.finditer(s):
            signs[m.group(1).lower()] += 1
        core = _SIGNS.sub("", _GREETS.sub("", s, count=1))       # the body, not the frame
        sents = [x for x in re.split(r"(?<=[.!?])\s+|\n+", core) if x.strip()]
        sent_lens += [len(x.split()) for x in sents]
        words += len(s.split())
        excl += s.count("!")
        lower_starts += sum(1 for x in sents if x[:1].islower())
        emoji += len(_EMOJI.findall(s))
    n = len(samples)
    avg = sum(sent_lens) / max(len(sent_lens), 1)
    contractions = sum(len(re.findall(r"\b\w+'(?:s|re|ve|ll|d|m|t)\b", s)) for s in samples)
    dear = greets.get("dear", 0) > n / 2
    formality = "formal" if (dear or avg > 18) else \
                ("casual" if (avg < 11 or contractions / max(words, 1) > 0.02 or emoji) else "plain")
    out = {
        "samples_seen": n,
        "avg_sentence_words": round(avg, 1),
        "avg_message_words": round(words / n),
        "formality": formality,
        "exclamation_per_msg": round(excl / n, 2),
        "lowercase_starts": round(lower_starts / max(len(sent_lens), 1), 2),
        "emoji": emoji > 0,
    }
    if greets:
        out["greeting"] = greets.most_common(1)[0][0]
    if signs:
        out["signoff"] = signs.most_common(1)[0][0]
    return out


def _sent_mail_samples(limit=40):
    """Bodies of recent messages they sent, via Mail.app — only if Mail is
    set up (mail.py's disk check), so no automation dialog on a Mac without it."""
    try:
        import mail
        if not mail._mail_configured():
            return [], "mail not set up"
        script = f'''
tell application "Mail"
  set outl to {{}}
  set ms to messages 1 thru {limit} of sent mailbox
  repeat with m in ms
    set end of outl to (content of m)
    set end of outl to "<<<>>>"
  end repeat
  return outl as string
end tell'''
        out, err = mail._osa(script, 30)
        if err:
            return [], "mail access"
        parts = [x.strip() for x in out.split("<<<>>>") if x.strip()]
        # drop quoted replies
        parts = [re.split(r"\n\s*(On .+wrote:|From:|-{3,}|>)", x)[0].strip() for x in parts]
        return parts, None
    except Exception:
        return [], "mail access"


def _imessage_samples(limit=400):
    """What they typed in Messages. chat.db is behind Full Disk Access; without
    it this returns the need rather than a sample."""
    db = os.path.expanduser("~/Library/Messages/chat.db")
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = con.execute("select text from message where is_from_me=1 and text is not null "
                           "and length(text) > 8 order by date desc limit ?", (limit,)).fetchall()
        con.close()
        return [r[0] for r in rows], None
    except Exception:
        return [], "Full Disk Access (for Messages)"


def build_writing():
    mail_s, need1 = _sent_mail_samples()
    msg_s, need2 = _imessage_samples()
    out = {}
    if mail_s:
        out["mail"] = writing_stats(mail_s)
        out["mail"]["examples"] = [s[:220] for s in mail_s[:3]]
    if msg_s:
        out["messages"] = writing_stats(msg_s)
    needs = [n for n in (need1, need2) if n]
    return out, needs


# --------------------------------------------------------------------------- #
# routine — the shape of the week
# --------------------------------------------------------------------------- #
def active_hours(log_path=None, days=14):
    """When they actually use Neo: hours of the day with presses, from the log.
    Pure given the text."""
    log_path = log_path or os.path.join(HERE, "neo.log")
    hours = collections.Counter()
    try:
        with open(log_path, errors="ignore") as f:
            for line in f:
                if "(let go" in line and line.startswith("["):
                    try:
                        hours[int(line[1:3])] += 1
                    except ValueError:
                        pass
    except OSError:
        return {}
    if not hours:
        return {}
    # The day is a circle. Find the longest quiet stretch (hours with next
    # to nothing) and the active window is everything else — so someone up
    # until 2am reads as "8:00 to 2:00", not "0:00 to 23:00".
    total = sum(hours.values())
    quiet = [hours.get(h, 0) < max(1, total * 0.01) for h in range(24)]
    best, cur_len, cur_start, best_start = 0, 0, 0, 0
    for i in range(48):
        h = i % 24
        if quiet[h]:
            if cur_len == 0:
                cur_start = h
            cur_len += 1
            if cur_len > best:
                best, best_start = cur_len, cur_start
        else:
            cur_len = 0
    if best == 0:
        first, last = 0, 23
    else:
        first, last = (best_start + best) % 24, (best_start - 1) % 24
    return {"first_hour": first, "last_hour": last,
            "peak_hours": [h for h, _ in hours.most_common(3)]}


def _calendar_recurring(weeks=4):
    """Recurring titles by weekday, from EventKit — only when access is already
    granted (never prompts here)."""
    try:
        import perms
        if perms.calendar() is not True:
            return {}, "Calendars"
        from EventKit import EKEventStore
        from Foundation import NSDate
        store = EKEventStore.alloc().init()
        start = NSDate.dateWithTimeIntervalSinceNow_(-weeks * 7 * 86400)
        end = NSDate.dateWithTimeIntervalSinceNow_(7 * 86400)
        pred = store.predicateForEventsWithStartDate_endDate_calendars_(start, end, None)
        events = store.eventsMatchingPredicate_(pred) or []
        seen = collections.Counter()
        for e in events:
            try:
                t = (e.title() or "").strip()
                d = dt.datetime.fromtimestamp(e.startDate().timeIntervalSince1970())
                if t:
                    seen[(d.strftime("%A"), t, d.strftime("%H:%M") if not e.isAllDay() else "")] += 1
            except Exception:
                continue
        rec = [{"day": d, "title": t, "time": tm, "times": c}
               for (d, t, tm), c in seen.most_common(40) if c >= 2]
        return {"recurring": rec[:15], "events_seen": len(events)}, None
    except Exception:
        return {}, "Calendars"


def build_routine():
    cal, need = _calendar_recurring()
    out = {"active": active_hours()}
    out.update(cal)
    return out, ([need] if need else [])


# --------------------------------------------------------------------------- #
# accounts — browsers, their profiles, and what each one is FOR
# --------------------------------------------------------------------------- #
def classify_account(email, profile_name=""):
    """work / personal / school / family / unknown. Pure. Family can't be
    inferred from an address; a profile literally named after a relation
    ('Mum', 'Dad', 'Kids') is the one signal."""
    name = (profile_name or "").lower()
    if re.search(r"\b(mum|mom|dad|mother|father|kids?|family|sister|brother|wife|husband|partner)\b", name):
        return "family"
    # the name they GAVE the profile beats the address: a profile called
    # "Work" on a gmail address is a work profile
    if re.search(r"\b(work|business|company|office|startup|biz)\b", name):
        return "work"
    if re.search(r"\b(school|student|uni|college|class)\b", name):
        return "school"
    if re.search(r"\b(personal|home|me|main)\b", name):
        return "personal"
    email = (email or "").lower()
    dom = email.split("@")[-1] if "@" in email else ""
    if not dom:
        return "unknown"
    if dom.endswith(".edu") or ".edu." in dom or re.search(r"\b(school|student|academy|college|university|school)\b", dom + " " + name) or dom.endswith((".org", ".ac.uk")):
        return "school"
    if dom in PERSONAL_DOMAINS:
        return "personal"
    return "work"


def _chromium_profiles(support_dir, app):
    out = []
    ls = os.path.join(support_dir, "Local State")
    try:
        cache = json.load(open(ls, encoding="utf-8")).get("profile", {}).get("info_cache", {})
    except Exception:
        return out
    for d, info in cache.items():
        email = info.get("user_name") or ""
        if not email:
            try:
                prefs = json.load(open(os.path.join(support_dir, d, "Preferences"), encoding="utf-8"))
                acc = (prefs.get("account_info") or [{}])[0]
                email = acc.get("email", "")
            except Exception:
                pass
        name = info.get("name") or d
        out.append({"app": app, "profile": name, "dir": d, "email": email,
                    "kind": classify_account(email, name)})
    return out


def build_accounts():
    home = os.path.expanduser("~")
    browsers = []
    for app, sub in (("Chrome", "Google/Chrome"), ("Arc", "Arc/User Data"),
                     ("Brave", "BraveSoftware/Brave-Browser"), ("Edge", "Microsoft Edge"),
                     ("Chromium", "Chromium")):
        d = os.path.join(home, "Library/Application Support", sub)
        if os.path.isdir(d):
            browsers += _chromium_profiles(d, app)
    if os.path.isdir("/Applications/Safari.app") or os.path.isdir("/System/Applications/Safari.app"):
        browsers.append({"app": "Safari", "profile": "Safari", "dir": "", "email": "", "kind": "unknown"})
    ff = os.path.join(home, "Library/Application Support/Firefox/profiles.ini")
    if os.path.exists(ff):
        for m in re.finditer(r"^Name=(.+)$", open(ff, errors="ignore").read(), re.M):
            browsers.append({"app": "Firefox", "profile": m.group(1).strip(), "dir": "",
                             "email": "", "kind": classify_account("", m.group(1))})
    default = ""
    try:
        r = subprocess.run(["defaults", "read", "com.apple.LaunchServices/com.apple.launchservices.secure",
                            "LSHandlers"], capture_output=True, text=True, timeout=5)
        m = re.search(r'LSHandlerURLScheme = https;[^}]*?LSHandlerRoleAll = "([^"]+)"', r.stdout, re.S) or \
            re.search(r'LSHandlerRoleAll = "([^"]+)";[^}]*?LSHandlerURLScheme = https', r.stdout, re.S)
        if m:
            default = m.group(1)
    except Exception:
        pass
    return {"browsers": browsers, "default_browser": default}


def retag_account(p, profile_name, kind):
    """'my Chrome profile Mum is my mum's' -> kind=family. Returns True if a
    profile matched."""
    kind = kind.lower().strip()
    hit = False
    for b in p.get("accounts", {}).get("browsers", []):
        if profile_name.lower().strip() in (b.get("profile", "").lower(), b.get("email", "").lower()):
            b["kind"] = kind
            b["tagged_by_user"] = True
            hit = True
    return hit


# --------------------------------------------------------------------------- #
# work — what they're on right now
# --------------------------------------------------------------------------- #
_DOC_EXT = (".pdf", ".docx", ".doc", ".pages", ".key", ".pptx", ".xlsx", ".numbers",
            ".md", ".txt", ".gdoc", ".gsheet", ".gslides", ".ipynb", ".tex", ".csv")


def build_work(days=14, limit=30):
    home = os.path.expanduser("~")
    roots = [os.path.join(home, d) for d in ("Documents", "Desktop", "Downloads")]
    roots += glob.glob(os.path.join(home, "Library/CloudStorage/*"))
    roots = [r for r in roots if os.path.isdir(r)]
    docs = []
    try:
        cmd = ["mdfind", f"kMDItemLastUsedDate >= $time.now(-{days * 86400})"]
        for r in roots:
            cmd += ["-onlyin", r]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout
        for path in out.splitlines():
            if path.lower().endswith(_DOC_EXT) and "/." not in path:
                try:
                    docs.append((os.path.getatime(path), path))
                except OSError:
                    pass
    except Exception:
        pass
    docs.sort(reverse=True)
    recent = [p for _, p in docs[:limit]]
    folders = collections.Counter(os.path.dirname(p).replace(home, "~") for p in recent)
    drive = [os.path.basename(r) for r in roots if "CloudStorage" in r]
    return {"recent_docs": [p.replace(home, "~") for p in recent],
            "folders": [f for f, _ in folders.most_common(6)],
            "cloud_drives": drive}


# --------------------------------------------------------------------------- #
# apps + permissions
# --------------------------------------------------------------------------- #
def build_apps():
    names = set()
    for root in ("/Applications", os.path.expanduser("~/Applications"), "/System/Applications"):
        try:
            for n in os.listdir(root):
                if n.endswith(".app"):
                    names.add(n[:-4])
        except OSError:
            pass
    try:
        import perms
        status = perms.status()
    except Exception:
        status = {}
    return {"installed": sorted(names), "permissions": status,
            "missing_required": [k for k, v in status.items() if v is not True
                                 and k in getattr(__import__("perms"), "REQUIRED", ())]}


# --------------------------------------------------------------------------- #
# people
# --------------------------------------------------------------------------- #
def build_people():
    people = {}
    try:
        import contacts
        for c in (contacts._all_contacts() or [])[:400]:
            if c.get("emails"):
                people[c["name"]] = {"email": c["emails"][0]}
    except Exception:
        pass
    try:
        import memory
        for f in memory.load_memory().get("facts", []):
            text = f.get("text", "") if isinstance(f, dict) else str(f)
            m = re.search(r"([A-Z][a-z]+)[^.]{0,30}?email\s+(?:is|=|:)\s*([^\s,]+@[^\s,]+)", text)
            if m:
                people.setdefault(m.group(1), {})["email"] = m.group(2)
            m = re.search(r"^([A-Z][a-z]+) is (?:their|her|their|my|their) (\w+)", text)
            if m:
                people.setdefault(m.group(1), {})["relation"] = m.group(2)
    except Exception:
        pass
    return people


# --------------------------------------------------------------------------- #
# all together
# --------------------------------------------------------------------------- #
SECTIONS = ("person", "writing", "routine", "accounts", "work", "apps", "people")


def refresh(log=print, only=None):
    p = load()
    needs = set(p.get("needs", []))
    stamp = _now().isoformat(timespec="seconds")
    builders = {
        "person": lambda: (build_person(), []),
        "writing": build_writing,
        "routine": build_routine,
        "accounts": lambda: (build_accounts(), []),
        "work": lambda: (build_work(), []),
        "apps": lambda: (build_apps(), []),
        "people": lambda: (build_people(), []),
    }
    for name, fn in builders.items():
        if only and name not in only:
            continue
        try:
            data, need = fn()
            p[name] = data
            for n in need:
                needs.add(n)
            for n in (set(needs) - set(need)) & {"mail not set up", "mail access",
                                                  "Full Disk Access (for Messages)", "Calendars"}:
                pass
            p.setdefault("updated", {})[name] = stamp
        except Exception as e:
            log(f"[profile] {name}: {type(e).__name__}: {e}")
    p["needs"] = sorted(needs)
    p.setdefault("boundaries", BOUNDARIES)
    save(p)
    log(f"[profile] refreshed: " + ", ".join(k for k in SECTIONS if k in p))
    return p


def brief(p=None, first_name_only=True):
    """The block the model reads. Compact: every line is something that
    changes what Neo does."""
    p = p or load()
    if not p or not p.get("person"):
        return ""
    L = []
    per = p.get("person", {})
    if per.get("name") or per.get("first_name"):
        L.append(f"They are {per.get('name') or per.get('first_name')}; call them "
                 f"{per.get('first_name') or per['name']}."
                 + (f" They are a {per['role']}." if per.get("role") else "")
                 + f" Timezone {per.get('timezone', '')}.")
    r = p.get("response", {})
    prefs = [f"{k}: {v}" for k, v in r.items() if k in ("length", "mode", "humour", "shape")]
    if prefs:
        L.append("HOW THEY WANT ANSWERS (learned from what they said): " + ", ".join(prefs) + ".")
    w = p.get("writing", {})
    ws = w.get("mail") or w.get("messages")
    if ws:
        bits = [f"{ws.get('formality', 'plain')} register",
                f"about {ws.get('avg_sentence_words')} words a sentence"]
        if ws.get("greeting"):
            bits.append(f"opens with '{ws['greeting']}'")
        if ws.get("signoff"):
            bits.append(f"signs off '{ws['signoff']}'")
        if ws.get("emoji"):
            bits.append("uses emoji")
        L.append("HOW THEY WRITE (match it in drafts): " + ", ".join(bits) + ".")
    rt = p.get("routine", {})
    act = rt.get("active", {})
    if act:
        L.append(f"Usually around from {act.get('first_hour')}:00 until about {act.get('last_hour')}:00.")
    rec = rt.get("recurring") or []
    if rec:
        L.append("Regular: " + "; ".join(f"{x['title']} {x['day']}s{(' ' + x['time']) if x['time'] else ''}"
                                        for x in rec[:6]) + ".")
    acc = p.get("accounts", {}).get("browsers", [])
    tagged = [b for b in acc if b.get("email") or b.get("kind") not in ("unknown", "")]
    if tagged:
        L.append("Browser profiles: " + "; ".join(
            f"{b['app']} '{b['profile']}' = {b['kind']}" + (f" ({b['email']})" if b.get("email") else "")
            for b in tagged[:6]) + ". Use the right one; ask if unsure which.")
    wk = p.get("work", {})
    if wk.get("recent_docs"):
        L.append("Recently working in: " + ", ".join(os.path.basename(d) for d in wk["recent_docs"][:6]) + ".")
    apps = p.get("apps", {})
    if apps.get("installed"):
        notable = [a for a in apps["installed"] if a in _NOTABLE]
        if notable:
            L.append("Installed: " + ", ".join(notable[:14]) + ".")
    miss = apps.get("missing_required") or []
    if miss:
        L.append("Permissions still missing: " + ", ".join(miss) + " (say so if it blocks something).")
    ppl = p.get("people", {})
    if ppl:
        L.append("People: " + ", ".join(
            f"{n}" + (f" ({d.get('relation')})" if d.get("relation") else "") for n, d in list(ppl.items())[:10]) + ".")
    if p.get("needs"):
        L.append("Would learn more with: " + ", ".join(p["needs"]) + ".")
    L.append("Lines never crossed: " + " ".join(p.get("boundaries", BOUNDARIES)))
    return "WHO YOU WORK FOR (kept current automatically):\n- " + "\n- ".join(L)


_NOTABLE = {"Google Chrome", "Safari", "Arc", "Firefox", "Slack", "Discord", "Notion", "Obsidian",
            "Spotify", "Music", "Zoom", "Microsoft Teams", "Xcode", "Visual Studio Code", "Cursor",
            "Terminal", "iTerm", "Figma", "Notes", "Reminders", "Calendar", "Mail", "Messages",
            "WhatsApp", "Telegram", "Pages", "Numbers", "Keynote", "Microsoft Word", "Microsoft Excel",
            "Microsoft PowerPoint", "Photoshop", "Final Cut Pro", "Logic Pro", "ChatGPT", "Claude"}


if __name__ == "__main__":
    import sys
    p = refresh() if "--refresh" in sys.argv or not os.path.exists(PATH) else load()
    print(brief(p))
