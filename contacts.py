"""
contacts.py — who someone IS, without anyone typing it in.

"Draft an email to Dean" needs an address. Where it actually lives, in the
order Neo looks:

  1. Contacts   The Mac's people store. Honest caveat: for most people this
                holds NAMES AND PHONE NUMBERS (it is the iPhone's list) and
                no email at all, so a hit without an address does not stop
                the search. It is the source for "call mum".
  2. Memory     "Remember that Priya's email is ..." — kept, and searched.
  3. Mail       Everyone who has ever written to this inbox (Mail.app).
  4. Google     Google Contacts plus the school/work DIRECTORY, through
                contacts.google.com — where the emails really are. Read
                silently by Neo's own browser when it is signed in
                (directory.py); otherwise the find_person tool opens it in
                the person's own Chrome and reads the screen.

Nothing is guessed. A name that isn't anywhere comes back as unknown, and
Neo asks — once — and remembers.

Pure: match(), score(). Impure: the three sources, each best-effort.
"""

import re

_NAME_RX = re.compile(r"[A-Za-z][A-Za-z'\-]+")


def _norm(s):
    return re.sub(r"[^a-z ]", " ", str(s or "").lower()).split()


def score(query, name):
    """How well a spoken name matches a contact's name. Pure, 0..3.
    3 exact, 2 every query word is a whole word of the name (first name,
    or first + last), 1 a query word starts a name word ("Dean" for
    "Deanna")."""
    q, n = _norm(query), _norm(name)
    if not q or not n:
        return 0
    if q == n:
        return 3
    if all(w in n for w in q):
        return 2
    if all(any(x.startswith(w) for x in n) for w in q):
        return 1
    return 0


def match(query, people, min_score=1):
    """Best people for a spoken name. Pure. people: [{name, emails, ...}].
    Returns [(score, person)] best first; ties keep list order."""
    hits = [(score(query, p.get("name", "")), p) for p in people]
    hits = [h for h in hits if h[0] >= min_score]
    hits.sort(key=lambda h: -h[0])
    return hits


# --------------------------------------------------------------------------- #
# 1. Contacts
# --------------------------------------------------------------------------- #
def contacts_status():
    """3 = authorized, 0 = not asked yet, else denied/restricted; None = no framework."""
    try:
        import Contacts
        return int(Contacts.CNContactStore.authorizationStatusForEntityType_(0))
    except Exception:
        return None


def from_contacts(limit=3000):
    """Every contact with a name. Only when access is granted or undecided
    (the first call prompts, once). Returns ([], reason) if it can't."""
    try:
        import Contacts
    except Exception:
        return [], "no_framework"
    st = contacts_status()
    if st not in (0, 3):
        return [], "denied"
    try:
        store = Contacts.CNContactStore.alloc().init()
        if st == 0:
            import access
            if not access.request_contacts():        # the prompt, on the main thread
                return [], "denied"
        keys = [Contacts.CNContactGivenNameKey, Contacts.CNContactFamilyNameKey,
                Contacts.CNContactEmailAddressesKey, Contacts.CNContactPhoneNumbersKey,
                Contacts.CNContactOrganizationNameKey, Contacts.CNContactNicknameKey]
        req = Contacts.CNContactFetchRequest.alloc().initWithKeysToFetch_(keys)
        people = []

        def each(c, stop):
            name = " ".join(x for x in (c.givenName(), c.familyName()) if x).strip()
            if not name:
                name = c.organizationName() or ""
            if not name:
                return
            emails = [str(e.value()) for e in (c.emailAddresses() or [])]
            phones = [str(p.value().stringValue()) for p in (c.phoneNumbers() or [])]
            people.append({"name": name, "nick": c.nickname() or "", "emails": emails,
                           "phones": phones, "org": c.organizationName() or "",
                           "source": "contacts"})
            if len(people) >= limit:
                stop[0] = True
        ok, err = store.enumerateContactsWithFetchRequest_error_usingBlock_(req, None, each)
        return people, None
    except Exception as e:
        return [], f"error:{type(e).__name__}"


# --------------------------------------------------------------------------- #
# 2. Mail — the From headers of whoever wrote to this inbox
# --------------------------------------------------------------------------- #
_FROM_RX = re.compile(r'^\s*"?([^"<]+?)"?\s*<([^>]+@[^>]+)>\s*$')


def parse_from(header):
    """'Dean Hoepfl <dhoepfl@school.org>' -> ('Dean Hoepfl', 'dhoepfl@school.org').
    A bare address gives ('', address). Pure."""
    h = str(header or "").strip()
    m = _FROM_RX.match(h)
    if m:
        return m.group(1).strip(), m.group(2).strip().lower()
    if "@" in h:
        return "", h.strip("<> ").lower()
    return h, ""


def from_mail(name, count=8):
    """People matching `name` who have written to the inbox, via mail.py's
    own search. [] without a mailbox."""
    try:
        import mail
        if mail.backend() is None:
            return []
        rows = mail.recent(count, name)
    except Exception:
        return []
    out, seen = [], set()
    for r in rows or []:
        nm, addr = r.get("from") or "", (r.get("address") or "").lower()
        if addr and addr not in seen and score(name, nm or addr.split("@")[0]) >= 1:
            seen.add(addr)
            out.append({"name": nm or addr, "emails": [addr], "phones": [], "source": "mail"})
    return out


# --------------------------------------------------------------------------- #
# 3. Memory
# --------------------------------------------------------------------------- #
def from_memory():
    out = []
    try:
        import memory as _m
        for f in _m.load_memory().get("facts", []):
            text = f.get("text", "") if isinstance(f, dict) else str(f)
            m = re.search(r"([A-Z][a-z]+(?: [A-Z][a-z]+)?)[^.]{0,30}?(?:email|address)\s+(?:is|=|:)\s*"
                          r"([\w.+-]+@[\w.-]+\.\w+)", text)
            if m:
                out.append({"name": m.group(1), "emails": [m.group(2).lower()], "phones": [],
                            "source": "memory"})
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------- #
# all together
# --------------------------------------------------------------------------- #
_cache = {"people": None, "at": 0.0}


def _all_contacts(ttl=600):
    import time
    if _cache["people"] is None or time.time() - _cache["at"] > ttl:
        people, _ = from_contacts()
        _cache["people"], _cache["at"] = people, time.time()
    return _cache["people"] or []


def resolve(name):
    """One spoken name -> (address, person) or (None, candidates).
    Contacts first, then memory, then the inbox. A single clear winner is
    returned; several equal ones come back as candidates to ask about."""
    name = str(name or "").strip()
    if not name:
        return None, []
    if "@" in name:
        return name, {"name": name, "emails": [name]}
    for source in (_all_contacts(), from_memory()):
        hits = [(sc, p) for sc, p in match(name, source) if p.get("emails")]
        if not hits:
            continue
        top = hits[0][0]
        best = [p for sc, p in hits if sc == top]
        if len(best) == 1 or top == 3:
            return best[0]["emails"][0], best[0]
        # several people share the first name: only ambiguous if their
        # addresses differ
        addrs = {p["emails"][0] for p in best}
        if len(addrs) == 1:
            return best[0]["emails"][0], best[0]
        return None, best[:4]
    hits = from_mail(name)
    if len(hits) == 1:
        return hits[0]["emails"][0], hits[0]
    if hits:
        return None, hits[:4]
    # Google — silently, only if Neo's own browser is already signed in.
    try:
        import directory
        people = directory.via_neo_browser(name)
        if people:
            return people[0]["emails"][0], people[0]
    except Exception:
        pass
    return None, []


def phone(name):
    """A phone number for 'call mum' — Contacts only."""
    hits = [(sc, p) for sc, p in match(name, _all_contacts()) if p.get("phones")]
    return hits[0][1]["phones"][0] if hits and (len(hits) == 1 or hits[0][0] == 3) else None
