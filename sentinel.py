"""
sentinel.py — Neo notices things before you ask.

A background watcher that reads the data Neo already sits on (the calendar,
the to-do list, your git repos) and raises an insight when something
deserves your attention:

  - an event is about to start
  - a to-do is overdue, and an evening roundup of what's left
  - unpushed / uncommitted work at the end of the day

Detection costs ZERO Gemini calls — it's all local JSON, one read-only DB
query, and git. Insights surface as an on-screen card (notify.py) with the
urgency listed; you accept ("tell me") or dismiss it.

Design notes:
  - compute_insights() is PURE: data in, (insights, new_state) out. All the
    OS/network reading lives in gather(). That keeps every rule testable.
  - State (what's been seen/snoozed/delivered) lives in sentinel.json so Neo
    doesn't re-nag across restarts.
  - Nothing in here imports PyObjC/audio/ML, so it runs (and tests) anywhere.

Config (.env or environment):
  NEO_SENTINEL=0            turn the whole thing off
  NEO_SENTINEL_TICK=60      seconds between checks (JSON is cheap)
  NEO_WATCH_REPOS=~/a,~/b   git repos to watch for unpushed work (optional)
"""

import datetime
import json
import os
import subprocess
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "sentinel.json")

# How long something can sit before Neo says something.
DB_CHECK_MIN = 10         # (kept: the loop's slow-poll cadence)
GIT_AFTER_HOUR = 17       # only nag about unpushed work in the evening
GATHER_TIMEOUT_S = 30     # a wedged DB/git/IMAP call must never freeze the loop
EVENT_LEAD_MIN = 30       # remind this many minutes before a calendar event
TODOS_PATH = os.path.join(HERE, "skills", "todos_data.json")   # the todo skill's store

# How long an insight stays quiet after you interact with the card.
SNOOZE_H = {"accept": 24, "dismiss": 24, "ignore": 4}
SNOOZE_H_HIGH_IGNORE = 1  # ignored HIGH-urgency cards come back sooner

URGENCY_RANK = {"low": 0, "medium": 1, "high": 2}


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #
def load_state(path=None):
    try:
        with open(path or STATE_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"first_seen": {}, "snoozed": {}, "db": {}}


def save_state(state, path=None):
    try:
        with open(path or STATE_PATH, "w") as f:
            json.dump(state, f, indent=2)
    except OSError as e:
        print(f"[sentinel] couldn't save state: {e}")


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def _parse(s):
    try:
        return datetime.datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _hours_since(state_key_map, key, now):
    t = _parse(state_key_map.get(key))
    return (now - t).total_seconds() / 3600.0 if t else None


def _age_words(hours):
    if hours < 24:
        return "since yesterday" if hours >= 12 else f"for {int(hours)} hours"
    d = int(hours // 24)
    return "for a day" if d == 1 else f"for {d} days"


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _dicts(seq):
    """Only the dict entries of a possibly-corrupt list-ish value."""
    if not isinstance(seq, list):
        return []
    return [x for x in seq if isinstance(x, dict)]


def snoozed(state, key, now):
    until = _parse((state.get("snoozed") or {}).get(key))
    return until is not None and now < until


def snooze(state, key, action, urgency="medium", now=None):
    """Quiet a key after the card was accepted / dismissed / ignored."""
    now = now or datetime.datetime.now()
    hrs = SNOOZE_H.get(action, 4)
    if action == "ignore" and urgency == "high":
        hrs = SNOOZE_H_HIGH_IGNORE
    if not isinstance(state.get("snoozed"), dict):
        state["snoozed"] = {}
    state["snoozed"][key] = _iso(now + datetime.timedelta(hours=hrs))
    return state


# --------------------------------------------------------------------------- #
# the rules — pure functions of (data, state, now)
# --------------------------------------------------------------------------- #
# Which kinds of card Neo is allowed to interrupt the user with.
#
# It used to be all of them, and most of them were lies. The lead, money and
# Calendar and to-do cards come from data the person actually maintains.
#
# Set NEO_CARDS to a comma-separated list to change it; NEO_CARDS=all restores
# the old firehose.
_DEFAULT_CARDS = "calendar,todo,due"
CARD_KINDS = {k.strip().lower()
              for k in (os.getenv("NEO_CARDS") or _DEFAULT_CARDS).split(",")
              if k.strip()}


def card_allowed(kind, allowed=None):
    """True when a card of this kind may interrupt. Pure, so it's tested."""
    allowed = CARD_KINDS if allowed is None else allowed
    return "all" in allowed or (kind or "").lower() in allowed


def _insight(key, urgency, title, detail, kind):
    return {"key": key, "urgency": urgency, "title": title, "detail": detail, "kind": kind}


def _overdue_words(mins):
    """Short 'N minutes/hours/days ago' for an overdue task. Pure."""
    if mins < 60:
        return f"{int(mins)} minute{'s' if int(mins) != 1 else ''} ago"
    if mins < 24 * 60:
        h = int(mins // 60)
        return f"{h} hour{'s' if h != 1 else ''} ago"
    d = int(mins // (24 * 60))
    return f"{d} day{'s' if d != 1 else ''} ago"


def todo_insights(todos, now):
    """Nudges from the to-do skill's store (pure). Each overdue open task earns a
    card; unscheduled open tasks get one low evening roundup. Snooze dedups
    repeats, so this can safely re-emit the same keys every tick."""
    out = []
    undated = 0
    for t in _dicts(todos):
        if t.get("done"):
            continue
        due = _parse(t.get("due"))
        text = (t.get("text") or "something").strip()
        if due is None:
            undated += 1
            continue
        if due <= now:
            key = f"todo:{t.get('id')}:{t.get('due')}"
            ago = _overdue_words((now - due).total_seconds() / 60.0)
            out.append(_insight(key, "medium", f"Overdue: {text}",
                                f"You wanted to {text} — that was due {ago}. "
                                "Still open on your list.", "todo"))
    if undated and now.hour >= GIT_AFTER_HOUR:
        key = "todos-evening:" + now.date().isoformat()
        out.append(_insight(
            key, "low",
            f"{undated} open to-do{'s' if undated != 1 else ''}",
            (f"You've got {undated} thing{'s' if undated != 1 else ''} on your list "
             "with no time set. Knock one out or put a time on it."),
            "todo"))
    return out


def event_insights(events, now):
    """Remind about calendar events starting within EVENT_LEAD_MIN (pure). One
    card per event; snooze keeps it from repeating every tick."""
    out = []
    for e in _dicts(events):
        start = _parse(e.get("start"))
        if start is None:
            continue
        mins = (start - now).total_seconds() / 60.0
        if 0 <= mins <= EVENT_LEAD_MIN:
            title = (e.get("title") or "an event").strip()
            when = "right now" if mins < 1 else f"in {int(round(mins))} minutes"
            key = f"event:{e.get('start')}:{title[:30]}"
            urgency = "high" if mins <= 10 else "medium"
            out.append(_insight(key, urgency, f"{title} {when}",
                                f"Heads up — you have {title} {when}.", "calendar"))
    return out


def compute_insights(data, state, now):
    """
    data: {
      "repos":          [ {"name", "dirty": bool, "ahead": int} ],
      "todos":          [ ... ],
      "events":         [ ... ],
    }
    Returns (insights, state). Mutates/returns state (first_seen tracking,
    db snapshot). Snoozed keys are filtered out here.
    """
    out = []
    # state comes straight off disk and data files can be hand-edited —
    # normalize everything before trusting shapes
    if not isinstance(state, dict):
        state = {}
    if not isinstance(state.get("first_seen"), dict):
        state["first_seen"] = {}
    if not isinstance(state.get("snoozed"), dict):
        state["snoozed"] = {}
    fs = state["first_seen"]

    # -- 5. unpushed work in the evening --------------------------------------
    if now.hour >= GIT_AFTER_HOUR:
        for r in _dicts(data.get("repos")):
            if not r.get("name") or not (r.get("dirty") or r.get("ahead")):
                continue
            key = f"git:{r['name']}:{now.date().isoformat()}"
            what = []
            if r.get("dirty"):
                what.append("uncommitted changes")
            if r.get("ahead"):
                what.append(f"{r['ahead']} unpushed commit{'s' if r['ahead'] != 1 else ''}")
            out.append(_insight(
                key, "low", f"Unpushed work in {r['name']}",
                (f"{r['name']} has {' and '.join(what)}. You know the drill: "
                 "commit and push before you lose it."),
                "git"))

    # -- 6. to-do nudges (overdue tasks + an evening roundup) ----------------
    out += todo_insights(data.get("todos"), now)

    # -- 7. calendar: an event is about to start ------------------------------
    out += event_insights(data.get("events"), now)

    # filter snoozed, strongest first
    out = [i for i in out if not snoozed(state, i["key"], now)]
    out.sort(key=lambda i: URGENCY_RANK.get(i["urgency"], 0), reverse=True)

    # prune snoozes that expired over a week ago so state never grows forever
    sn = state.get("snoozed", {})
    week = datetime.timedelta(days=7)
    for k in [k for k, v in sn.items() if (_parse(v) or now) < now - week]:
        del sn[k]
    return out, state


def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def check_repo(path):
    """Return {'name','dirty','ahead'} for a git repo, or None if it isn't one."""
    path = os.path.expanduser(path.strip())
    if not os.path.isdir(os.path.join(path, ".git")):
        return None

    def _git(*args):
        return subprocess.run(("git", "-C", path) + args, capture_output=True,
                              text=True, timeout=10)

    try:
        dirty = bool(_git("status", "--porcelain").stdout.strip())
        r = _git("rev-list", "--count", "@{u}..HEAD")
        ahead = int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else 0
        return {"name": os.path.basename(path) or path, "dirty": dirty, "ahead": ahead}
    except Exception:
        return None


def _bounded(fn, timeout, default):
    """Run fn() but never block the caller longer than `timeout` seconds. If it
    overruns (a wedged network/git/IMAP call), return `default` and let the
    orphan thread finish on its own — the watch loop keeps its cadence instead
    of freezing. Normal calls finish in well under a second, so no added lag."""
    box = {"v": default}
    def run():
        try:
            box["v"] = fn()
        except Exception as e:
            print(f"[sentinel] bounded task failed: {e}")
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        print(f"[sentinel] a check exceeded {timeout}s — skipping it this tick")
    return box["v"]


def _upcoming_events(now, within_min):
    """Calendar events starting in the next `within_min` minutes, via EventKit.
    Read-only and non-interactive: if access isn't ALREADY granted we return []
    silently (calendar_peek handles the permission prompt when the user asks). Never
    raises. Returns [{'title','start': iso}]."""
    try:
        from EventKit import EKEventStore, EKEntityTypeEvent
        from Foundation import NSDate
    except Exception:
        return []
    try:
        status = EKEventStore.authorizationStatusForEntityType_(EKEntityTypeEvent)
        if status not in (3, 4):        # 3 authorized, 4 fullAccess (newer macOS)
            return []
        store = EKEventStore.alloc().init()
        start = NSDate.dateWithTimeIntervalSince1970_(now.timestamp())
        end = NSDate.dateWithTimeIntervalSince1970_(now.timestamp() + within_min * 60)
        pred = store.predicateForEventsWithStartDate_endDate_calendars_(start, end, None)
        out = []
        for ev in (store.eventsMatchingPredicate_(pred) or []):
            if ev.isAllDay():
                continue
            st = datetime.datetime.fromtimestamp(ev.startDate().timeIntervalSince1970())
            if st >= now:
                out.append({"title": (ev.title() or "an event").strip(),
                            "start": _iso(st)})
        return out
    except Exception as e:
        print(f"[sentinel] calendar read failed: {e}")
        return []


def gather(state, now, include_db):
    """Read everything the rules need. Never raises."""
    data = {"repos": [], "todos": [], "events": []}
    data["todos"] = _read_json(TODOS_PATH, {}).get("tasks", [])
    data["events"] = _upcoming_events(now, EVENT_LEAD_MIN)

    repos = os.getenv("NEO_WATCH_REPOS", "")
    if repos and now.hour >= GIT_AFTER_HOUR:
        for p in repos.split(","):
            if p.strip():
                r = check_repo(p)
                if r:
                    data["repos"].append(r)
    return data


# --------------------------------------------------------------------------- #
# the loop
# --------------------------------------------------------------------------- #
class Sentinel:
    """
    Background thread: gather -> compute -> hand insights to `deliver`.

    deliver(insight) is called from THIS thread — the caller is responsible
    for hopping to the main thread for UI (neo.py uses AppHelper.callAfter).
    Call mark(key, action, urgency) when the user handles a card.
    """

    def __init__(self, deliver):
        self.deliver = deliver
        self._lock = threading.Lock()
        self._state = load_state()
        self._stop = threading.Event()
        self._last_db = None       # datetime of last DB poll
        self._last_mail = None     # datetime of last inbox poll
        self.tick_s = max(15, int(os.getenv("NEO_SENTINEL_TICK", "60")))

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="neo-sentinel").start()

    def stop(self):
        self._stop.set()

    def mark(self, key, action, urgency="medium"):
        """User accepted / dismissed / ignored the card for `key`."""
        with self._lock:
            snooze(self._state, key, action, urgency)
            save_state(self._state)

    def pending_summary(self):
        """A short spoken line for 'anything on my radar?'. Zero Gemini calls."""
        now = datetime.datetime.now()
        with self._lock:
            data = gather(self._state, now, include_db=False)
            insights, self._state = compute_insights(data, self._state, now)
            save_state(self._state)
        if not insights:
            return "Radar's clear. Nothing waiting on you right now."
        lines = [i["detail"] for i in insights[:3]]
        return " ".join(lines)

    def _run(self):
        while not self._stop.wait(self.tick_s):
            try:
                now = datetime.datetime.now()
                poll_db = (self._last_db is None or
                           (now - self._last_db).total_seconds() >= DB_CHECK_MIN * 60)
                if poll_db:
                    self._last_db = now
                insights = []
                # Bound every blocking check so one hung call can't freeze the
                # loop (the old failure mode: a wedged IMAP/DB call stalls forever).
                data = _bounded(lambda: gather(self._state, now, include_db=poll_db),
                                GATHER_TIMEOUT_S,
                                {"repos": [], "todos": [], "events": []})
                with self._lock:
                    computed, self._state = compute_insights(data, self._state, now)
                    insights += computed
                    save_state(self._state)
                for i in insights:
                    if not card_allowed(i.get("kind")):
                        continue          # filtered: see CARD_KINDS above
                    try:
                        self.deliver(i)
                    except Exception as e:
                        print(f"[sentinel] deliver failed: {e}")
            except Exception as e:
                print(f"[sentinel] tick recovered from: {e}")

if __name__ == "__main__":
    # Dry run: print what Neo would surface right now (no UI, no snoozing writes).
    now = datetime.datetime.now()
    st = load_state()
    d = gather(st, now, include_db=True)
    ins, _ = compute_insights(d, st, now)
    if not ins:
        print("Nothing to surface right now.")
    for i in ins:
        print(f"[{i['urgency'].upper():6}] {i['title']}\n         {i['detail']}\n")
