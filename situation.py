"""
situation.py — the three lines that make "this" and "that" work.

The defining ritual of every chatbot is explaining your situation to it: copy
the error, paste the file, describe the app. Neo is already on the machine, so
it shouldn't need any of that. But the obvious implementation — staple a
screenshot and the clipboard onto every question — is wrong twice over. It
costs a vision call on "what's the weather", and it pipes whatever they last
copied into a third party on every single turn, forever.

So this is RETRIEVAL, not a dump. Each turn carries a few dozen tokens saying
what is *available*:

    Right now: Preview is in front, showing 'PCH Summer Review Packet.pdf'.
    They have something copied that looks like an error message.

Neo already has read_document, what_did_i_copy and look_at_screen. Told what is
there, it fetches the one thing the question needs and nothing else. "What does
this say" becomes answerable without a single wasted call, and "what's the
weather" costs forty tokens.

**No clipboard contents are ever put in the header.** It carries a SHAPE — a
URL, an error, some code, a paragraph — worked out locally by regex. Anything
that looks like a password, a key or a card number is not mentioned at all, not
even as a shape: the tool can fetch it if they actually asks, and silence is the
right default for something they may not realise is still on the clipboard.
"""

import re
import time

TTL = 4.0                  # a snapshot is reused inside one exchange
_CACHE = {"at": 0.0, "snap": None}

# Not apps they are using: system UI that happens to hold focus. Telling the model
# "UserNotificationCenter is in front of them" is worse than saying nothing —
# it invites an answer about a permission dialog they isn't asking about.
_NOT_AN_APP = {"UserNotificationCenter", "Finder", "loginwindow", "Dock",
               "SystemUIServer", "Spotlight", "universalaccessd", "coreautha",
               "Notification Centre", "Notification Center", "ScreenSaverEngine",
               "SecurityAgent", "CoreServicesUIAgent", "Install Assistant"}

# Apps where "what am I looking at" is answered by the document, not the pixels.
_DOC_APPS = {"Preview", "Adobe Acrobat", "TextEdit", "Pages", "Numbers",
             "Keynote", "Adobe Acrobat Reader"}

# Words that point at something without naming it. If one of these shows up,
# the situation is not decoration — it is the subject of the sentence.
_DEICTIC = re.compile(
    r"\b(this|that|these|those|it|here|the screen|on screen|my screen|"
    r"what i(?:'m| am) (?:looking at|seeing|on)|the page|the error|"
    r"what (?:did i|i)(?: just)? cop(?:y|ied)|the clipboard)\b", re.I)

# Things that must never be described, even in the abstract.
_SECRET = re.compile(
    r"(?i)(sk-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{20,}|ghp_[A-Za-z0-9]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY"
    r"|\b(?:password|passwd|api[_ -]?key|secret|token|bearer)\b\s*[:=]"
    r"|\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b)")


def clipboard_shape(text):
    """What KIND of thing is on the clipboard. Never what it says."""
    if not text or not text.strip():
        return ""
    if _SECRET.search(text):
        return ""                      # deliberately silent; see the docstring
    t = text.strip()
    n = len(t)
    if n > 200000:
        return "something very long"
    if re.fullmatch(r"https?://\S+", t):
        return "a link"
    if re.search(r"(?m)^(Traceback|\s+File \"|[A-Za-z.]*(Error|Exception):)", t):
        return "an error message"
    if re.fullmatch(r"[^@\s]+@[^@\s]+\.\w{2,}", t):
        return "an email address"
    if n < 400 and re.fullmatch(r"[\d\s.,+\-*/()%$€£]+", t):
        return "some numbers"
    lines = t.splitlines()
    code = sum(bool(re.match(r"\s*(def |class |import |from |function |const |"
                             r"let |var |if |for |while |\}|<[a-z]+[ >]|#include)", ln))
               for ln in lines)
    if len(lines) > 2 and code >= max(2, len(lines) // 4):
        return "some code"
    if n < 90 and len(lines) == 1:
        return "a short bit of text"
    if len(lines) > 8 or n > 900:
        return "a long piece of text"
    return "a paragraph of text"


def snapshot(now=None, force=False):
    """What's in front of them. Cheap: no screenshot, no model call.

    Cached for TTL seconds so a quick back-and-forth doesn't re-run osascript
    between every sentence.
    """
    now = time.time() if now is None else now
    if not force and _CACHE["snap"] is not None and now - _CACHE["at"] < TTL:
        return _CACHE["snap"]
    snap = {"app": "", "document": "", "clipboard": ""}
    try:
        import desk
        snap["app"] = desk.frontmost_app()
        if snap["app"] in _DOC_APPS:
            found = desk.open_document()
            if found and found[1]:
                import os
                snap["document"] = os.path.basename(found[1])
        snap["clipboard"] = clipboard_shape(desk.clipboard(limit=200000))
    except Exception:
        pass
    _CACHE.update(at=now, snap=snap)
    return snap


def is_ambiguous(text):
    """Does this sentence point at something it doesn't name?"""
    return bool(_DEICTIC.search(text or ""))


def line(snap=None, text="", now=None):
    """The block that goes in the prompt. '' when there's nothing to say."""
    snap = snapshot(now) if snap is None else snap
    bits = []
    app, doc = snap.get("app", ""), snap.get("document", "")
    if doc:
        bits.append(f"{app} is in front of them, showing '{doc}'")
    elif app and app not in _NOT_AN_APP:
        bits.append(f"{app} is in front of them")
    clip = snap.get("clipboard", "")
    if clip:
        bits.append(f"they have {clip} on the clipboard")
    if not bits:
        return ""
    out = "RIGHT NOW: " + "; ".join(bits) + "."
    if is_ambiguous(text):
        # The nudge only appears when they actually said "this". The rest of the
        # time the facts are enough and the instruction is noise.
        out += ("\n  They said \"this\" or \"that\" without naming it — it is "
                "almost certainly one of the above. Fetch the ONE that fits "
                "(read_document, what_did_i_copy, look_at_screen) and answer. "
                "Do not ask them which they mean unless nothing above fits.")
    return out

# Apps that mean they are on a call or in front of people. A card popping up
# during a screen share is not a small annoyance — whatever it says is on
# everybody else's screen too.
_CALL_APPS = {"zoom.us", "zoom", "Microsoft Teams", "Webex", "Webex Meetings",
              "GoToMeeting", "BlueJeans", "Loom", "OBS Studio"}
# These only count when they have gone fullscreen, which for them means the
# slideshow is actually running rather than being edited.
_SLIDE_APPS = {"Keynote", "Microsoft PowerPoint", "QuickTime Player"}

_PRESENT_CACHE = {"at": 0.0, "value": False}
PRESENT_TTL = 5.0


def _fullscreen():
    """Is the front window covering the entire display, menu bar included?"""
    try:
        from Quartz import (CGWindowListCopyWindowInfo, CGMainDisplayID,
                            kCGWindowListOptionOnScreenOnly, kCGNullWindowID,
                            CGDisplayBounds)
        bounds = CGDisplayBounds(CGMainDisplayID())
        sw, sh = bounds.size.width, bounds.size.height
        if sw <= 0 or sh <= 0:
            return False
        for w in CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly,
                                            kCGNullWindowID) or []:
            if w.get("kCGWindowLayer") != 0:
                continue                      # menu bar, dock, overlays
            b = w.get("kCGWindowBounds") or {}
            if (b.get("Y", -1) == 0 and b.get("X", -1) == 0
                    and b.get("Width", 0) >= sw - 1
                    and b.get("Height", 0) >= sh - 1):
                return True
        return False
    except Exception:
        return False


def presenting(now=None):
    """Is they in front of an audience right now? Cheap, and CONSERVATIVE.

    Fullscreen on its own is deliberately NOT enough. the user works fullscreen
    most of the day, so treating that as presenting would silence Neo almost
    always — which is a different bug, not a fix. What counts is a
    conferencing app in front, or a slideshow app actually running its
    slideshow (which is what fullscreen means for Keynote and PowerPoint).

    Cached for a few seconds: the window list plus the frontmost app costs
    about half a second, and this is asked before every card.
    """
    now = time.time() if now is None else now
    if now - _PRESENT_CACHE["at"] < PRESENT_TTL:
        return _PRESENT_CACHE["value"]
    value = False
    try:
        import desk
        app = desk.frontmost_app()
        value = app in _CALL_APPS or (app in _SLIDE_APPS and _fullscreen())
    except Exception:
        value = False
    _PRESENT_CACHE.update(at=now, value=value)
    return value
