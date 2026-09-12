"""
perms.py — what macOS has let Neo do, read directly, never guessed.

Every permission Neo needs, with a real check for each and the exact System
Settings pane that grants it. Nothing here prompts; it only reads. The
onboarding uses it to show a live checklist, and the doctor uses it to say
precisely what is missing instead of "grant everything and try again".

NO NEW DEPENDENCIES
Two of these frameworks aren't in the venv as PyObjC packages, and they don't
need to be: ApplicationServices is one ctypes call, and AVFoundation loads
through the objc runtime that is already here. Adding a pip package for a
single boolean would be the wrong trade.
"""

import ctypes
import ctypes.util
import os
import subprocess

# What each permission is FOR, in their words. Shown in the onboarding.
PERMISSIONS = [
    ("accessibility", "Accessibility",
     "So Neo can see the fn key being held. Without this, nothing works.",
     "Privacy_Accessibility"),
    ("input", "Input Monitoring",
     "The other half of hearing the key. macOS asks for both.",
     "Privacy_ListenEvent"),
    ("microphone", "Microphone",
     "Only ever open while you hold the key.",
     "Privacy_Microphone"),
    ("screen", "Screen Recording",
     "So Neo can read what's in front of you and point at it, "
     "only when you ask.",
     "Privacy_ScreenCapture"),
    ("calendar", "Calendars",
     "So \"what have I got tomorrow\" is a real answer. Optional.",
     "Privacy_Calendars"),
    ("contacts", "Contacts",
     "So \"email Dean\" knows who Dean is. Optional.",
     "Privacy_Contacts"),
    ("reminders", "Reminders",
     "So \"remind me to call mum at six\" lands in Reminders. Optional.",
     "Privacy_Reminders"),
]

REQUIRED = ("accessibility", "input", "microphone")
SETTINGS_URL = "x-apple.systempreferences:com.apple.preference.security?{pane}"


def accessibility():
    try:
        lib = ctypes.CDLL(ctypes.util.find_library("ApplicationServices"))
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(lib.AXIsProcessTrusted())
    except Exception:
        return None


def input_monitoring():
    """Input Monitoring, asked of IOKit WITHOUT creating an event tap.

    The old check created a listen-only tap. Creating a tap from a process
    that lacks the permission makes macOS open System Settings > Input
    Monitoring by itself — so every test run and every terminal diagnostic
    (python3, not Neo) popped the pane on the owner's screen. IOHIDCheckAccess
    answers the same question with no side effect."""
    try:
        lib = ctypes.CDLL(ctypes.util.find_library("IOKit"))
        lib.IOHIDCheckAccess.restype = ctypes.c_uint32
        lib.IOHIDCheckAccess.argtypes = [ctypes.c_uint32]
        return int(lib.IOHIDCheckAccess(1)) == 0      # 1 = listen events; 0 = granted
    except Exception:
        return None


_AV = None


def _capture_device():
    """AVCaptureDevice, loaded ONCE. The onboarding polls status() every
    second, and loading the bundle on each poll held the interpreter long
    enough to starve audio playback — the narration broke up the moment the
    permissions screen appeared."""
    global _AV
    if _AV is None:
        import objc
        objc.loadBundle("AVFoundation", globals(),
                        bundle_path="/System/Library/Frameworks/AVFoundation.framework")
        _AV = objc.lookUpClass("AVCaptureDevice")
    return _AV


def microphone():
    """AVCaptureDevice.authorizationStatusForMediaType_: 3 is authorized, 0 is
    not yet asked (macOS will prompt on first use), 1 restricted, 2 denied."""
    try:
        return int(_capture_device().authorizationStatusForMediaType_("soun")) == 3
    except Exception:
        return None


def microphone_undecided():
    try:
        return int(_capture_device().authorizationStatusForMediaType_("soun")) == 0
    except Exception:
        return False


def screen():
    try:
        import Quartz
        return bool(Quartz.CGPreflightScreenCaptureAccess())
    except Exception:
        return None


def calendar():
    """EventKit: 3 (authorized) and 4 (full access, macOS 14+) both count."""
    try:
        import EventKit
        return int(EventKit.EKEventStore.authorizationStatusForEntityType_(0)) in (3, 4)
    except Exception:
        return None


def contacts():
    try:
        import Contacts
        return int(Contacts.CNContactStore.authorizationStatusForEntityType_(0)) == 3
    except Exception:
        return None


def reminders():
    """Same store, entity type 1."""
    try:
        import EventKit
        return int(EventKit.EKEventStore.authorizationStatusForEntityType_(1)) in (3, 4)
    except Exception:
        return None


_CHECKS = {
    "accessibility": accessibility,
    "input": input_monitoring,
    "microphone": microphone,
    "screen": screen,
    "calendar": calendar,
    "reminders": reminders,
    "contacts": contacts,
}


def status():
    """{key: True | False | None} — None means macOS wouldn't say."""
    return {k: fn() for k, fn in _CHECKS.items()}


def missing(state=None):
    """The REQUIRED permissions that are not granted. Pure given state."""
    state = status() if state is None else state
    return [k for k in REQUIRED if state.get(k) is not True]


def ready(state=None):
    return not missing(state)


def open_settings(key):
    """Take them straight to the right pane — not to System Settings' front
    door with a paragraph about where to click next."""
    pane = next((p for k, _, _, p in PERMISSIONS if k == key), None)
    if not pane:
        return False
    try:
        subprocess.Popen(["open", SETTINGS_URL.format(pane=pane)])
        return True
    except Exception:
        return False


def describe(state=None):
    """One line per permission for the doctor."""
    state = status() if state is None else state
    out = []
    for key, name, _why, _pane in PERMISSIONS:
        v = state.get(key)
        word = "ok" if v is True else ("MISSING" if v is False else "unknown")
        if key not in REQUIRED and v is not True:
            word = "off (optional)"
        out.append(f"{name:<18} {word}")
    return "\n".join(out)
