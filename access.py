"""
access.py — asking macOS for a permission, so the prompt actually appears.

EventKit and Contacts deliver their "may Neo…?" prompt through the MAIN
run loop. Every tool runs on a worker thread, and the old code requested
access from there, spun a private run loop for twelve seconds, heard
nothing, and reported "denied" — then opened System Settings to "help".
Nobody ever saw a prompt; System Settings kept popping up; the permission
never changed. (11 Sept, the calendar pane opened four times in a row.)

Here the request is hopped to the main thread (PyObjC's AppHelper) and the
worker waits — up to a minute, because a person has to click — for the
real answer. System Settings is opened only when macOS says DENIED
outright, and only once in ten minutes.
"""

import threading
import time

WAIT_S = 60.0
_last_settings = {}


def _on_main(fn):
    """Run fn on the main thread if there is an app run loop; else inline."""
    try:
        from PyObjCTools import AppHelper
        from Foundation import NSThread
        if NSThread.isMainThread():
            fn()
        else:
            AppHelper.callAfter(fn)
    except Exception:
        fn()


def request_eventkit(kind):
    """kind 0 = events, 1 = reminders. Returns True/False (granted), or None
    if the framework isn't there. Prompts only when undecided."""
    try:
        from EventKit import EKEventStore
    except Exception:
        return None
    status = int(EKEventStore.authorizationStatusForEntityType_(kind))
    if status in (3, 4):
        return True
    if status != 0:
        return False
    done = threading.Event()
    box = {"ok": False}

    def ask():
        try:
            store = EKEventStore.alloc().init()
            _keep.append(store)             # the store must outlive the prompt

            def cb(granted, err):
                box["ok"] = bool(granted)
                done.set()
            if kind == 0 and hasattr(store, "requestFullAccessToEventsWithCompletion_"):
                store.requestFullAccessToEventsWithCompletion_(cb)
            elif kind == 1 and hasattr(store, "requestFullAccessToRemindersWithCompletion_"):
                store.requestFullAccessToRemindersWithCompletion_(cb)
            else:
                store.requestAccessToEntityType_completion_(kind, cb)
        except Exception:
            done.set()
    _on_main(ask)
    done.wait(WAIT_S)
    return box["ok"] or int(EKEventStore.authorizationStatusForEntityType_(kind)) in (3, 4)


def request_contacts():
    try:
        import Contacts
    except Exception:
        return None
    status = int(Contacts.CNContactStore.authorizationStatusForEntityType_(0))
    if status == 3:
        return True
    if status != 0:
        return False
    done = threading.Event()
    box = {"ok": False}

    def ask():
        try:
            store = Contacts.CNContactStore.alloc().init()
            _keep.append(store)

            def cb(ok, err):
                box["ok"] = bool(ok)
                done.set()
            store.requestAccessForEntityType_completionHandler_(0, cb)
        except Exception:
            done.set()
    _on_main(ask)
    done.wait(WAIT_S)
    return box["ok"] or int(Contacts.CNContactStore.authorizationStatusForEntityType_(0)) == 3


_keep = []


def maybe_open_settings(key, every_s=600):
    """Open the pane at most once per ten minutes, and only for a real
    denial — never as a reflex."""
    now = time.time()
    if now - _last_settings.get(key, 0) < every_s:
        return False
    _last_settings[key] = now
    try:
        import perms
        return perms.open_settings(key)
    except Exception:
        return False
