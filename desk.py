"""
desk.py — the things sitting on their desk: the clipboard, their files, and
whatever document is open in front of them.

Neo could see the screen and drive apps, but it could not answer "what did I
just copy", "where's that PDF", or "what does this document say" — which are
three of the most ordinary things anyone asks at a computer. All three are
available on macOS for nothing:

    pbpaste / pbcopy    the clipboard
    mdfind              Spotlight, including full-text search inside documents
    CoreGraphics        renders a PDF page to an image, which vision can read

Reading a PDF by RENDERING IT is the interesting choice. Extracting text would
need a new dependency and would fail on anything scanned; rendering the page
and looking at it costs nothing extra, works on scans, and — because the same
vision call sees the layout — it can answer "what's the heading" and "what does
the table say", which raw text extraction is bad at.
"""

import os
import re
import subprocess
import time

MAX_PDF_PAGES = int(os.getenv("NEO_PDF_PAGES", "6"))
STAT_CAP = 600         # how many Spotlight hits get dated before sorting
VISION_MODEL = os.getenv("NEO_VISION_MODEL", "gemini-3.5-flash-lite")
_HOME = os.path.expanduser("~")


# --------------------------------------------------------------------------- #
# Clipboard.
# --------------------------------------------------------------------------- #
def clipboard(limit=4000):
    """Whatever was last copied, as text. '' when it is empty or not text."""
    try:
        r = subprocess.run(["pbpaste"], capture_output=True, timeout=5)
        return r.stdout.decode("utf-8", "replace")[:limit]
    except Exception:
        return ""


def set_clipboard(text):
    try:
        p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
        p.communicate(str(text).encode("utf-8"), timeout=5)
        return p.returncode == 0
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Finding a file.
# --------------------------------------------------------------------------- #
_KINDS = {
    "pdf": "kMDItemContentType == 'com.adobe.pdf'",
    "image": "kMDItemContentTypeTree == 'public.image'",
    "document": "kMDItemContentTypeTree == 'public.content'",
    "spreadsheet": "kMDItemContentTypeTree == 'public.spreadsheet'",
    "presentation": "kMDItemContentTypeTree == 'public.presentation'",
}
# Places a person's own files live. Spotlight indexes the whole disk, and
# without this a search for "review" returns eighty framework resources before
# it reaches anything the user has ever opened.
_MINE = (os.path.join(_HOME, "Desktop"), os.path.join(_HOME, "Documents"),
         os.path.join(_HOME, "Downloads"), os.path.join(_HOME, "Dropbox"),
         os.path.join(_HOME, "Library/Mobile Documents"))


def _is_mine(path):
    return any(path.startswith(p) for p in _MINE) or (
        path.startswith(_HOME) and "/Library/" not in path)


def find_files(query, kind=None, limit=8, roots=None):
    """Spotlight, biased hard toward files that are actually their.

    Searches the NAME and the CONTENTS, because "the packet about limits" is
    how people refer to documents, not by filename.
    """
    query = (query or "").strip()
    if not query:
        return []
    esc = query.replace('"', '')
    # A spoken filename loses its punctuation: "2026 PPR draft strategy" is how
    # anyone says 2026_ppr_draft_strategy.md. Search the spaced form AND the
    # separator-joined ones, or the file they are literally naming is unfindable.
    spoken = re.sub(r"[\s_\-]+", " ", esc).strip()
    forms = {esc, spoken, spoken.replace(" ", "_"), spoken.replace(" ", "-"),
             spoken.replace(" ", "")}
    names = " || ".join(f'kMDItemDisplayName == "*{f}*"cd'
                        for f in sorted(forms) if f)
    clause = f'({names} || kMDItemTextContent == "*{esc}*"cd)'
    if kind and kind.lower() in _KINDS:
        clause = f"{clause} && {_KINDS[kind.lower()]}"
    out = []
    try:
        r = subprocess.run(["mdfind", clause], capture_output=True, timeout=20)
        paths = r.stdout.decode("utf-8", "replace").splitlines()
    except Exception:
        return []
    mine = [p for p in paths if _is_mine(p)]
    # Spotlight returns matches in ITS order, which is not recency. Stopping
    # after a couple of dozen and then sorting sorted the wrong two dozen —
    # a content search for a common word could bury the file they had open this
    # morning behind twenty from 2019. Stat a wide slice and sort all of it;
    # 600 stat calls cost about 3ms.
    for p in (mine or paths)[:STAT_CAP]:
        if not os.path.exists(p):
            continue
        try:
            st = os.stat(p)
        except OSError:
            continue
        out.append({"path": p, "name": os.path.basename(p),
                    "modified": st.st_mtime, "size": st.st_size})
    # NAME MATCHES FIRST, then recency inside each group.
    #
    # Sorting purely by recency meant a file that merely MENTIONS the words beat
    # the file actually called them. Asked to open "2026 PPR draft strategy",
    # Spotlight returned neo.log first — because the phrase appears in the log —
    # and the document itself never surfaced at all.
    # They often says the extension out loud ("the draft strategy dot m d"), so
    # strip it from the QUERY as well — otherwise "2026 PPR draft strategy.md"
    # never matches the stem "2026_ppr_draft_strategy".
    asked = re.sub(r"\.(md|txt|csv|json|pdf|html?|py|docx?|xlsx?|pptx?|png|jpe?g)$",
                   "", query.strip(), flags=re.I)
    want = re.sub(r"[\s_\-]+", "", norm_name(asked))
    for f in out:
        stem = re.sub(r"[\s_\-]+", "", norm_name(os.path.splitext(f["name"])[0]))
        f["named"] = 2 if stem == want else (1 if want and want in stem else 0)
    out.sort(key=lambda f: (-f["named"], -f["modified"]))
    return out[:limit]


def norm_name(text):
    """Lowercased, punctuation-free — for comparing a spoken name to a file."""
    return re.sub(r"[^a-z0-9 _\-]+", "", (text or "").lower()).strip()


def describe_age(ts, now=None):
    """'yesterday', 'last week' — how a person refers to a file. Pure."""
    now = time.time() if now is None else now
    try:
        d = max(0.0, now - float(ts))
    except (TypeError, ValueError):
        return "at some point"
    if d < 3600:
        return "in the last hour"
    if d < 86400:
        return "today"
    if d < 172800:
        return "yesterday"
    if d < 86400 * 8:
        return f"{int(d // 86400)} days ago"
    if d < 86400 * 60:
        return f"{max(1, int(d // 604800))} weeks ago"
    if d < 86400 * 365:
        return f"{max(2, int(d // 2592000))} months ago"
    # Past a year nobody counts in months. "Thirteen months ago" is a number,
    # not an answer.
    years = int(d // (86400 * 365))
    return "about a year ago" if years == 1 else f"about {years} years ago"


# Apps that will definitely SHOW a file, by kind. Plain `open` returns success
# and does nothing at all when a type has no handler registered — which is what
# happens to markdown on this Mac, and it is why Neo said "it's on your screen
# now" three times while nothing was on their screen.
_OPENERS = {
    ".md": "TextEdit", ".txt": "TextEdit", ".csv": "TextEdit",
    ".json": "TextEdit", ".log": "TextEdit", ".py": "TextEdit",
    ".pdf": "Preview", ".png": "Preview", ".jpg": "Preview",
    ".jpeg": "Preview", ".svg": "Preview", ".html": "Google Chrome",
}


def looks_like(said, path):
    """Is this the file they just described? Loose, on purpose.

    "The fantasy football one" has nothing in common with
    2026_ppr_draft_strategy.md as a string, so a literal comparison says no and
    Neo tells them the document does not exist while holding its path. This
    checks the filename AND the first part of the file, because what a document
    is ABOUT is how anyone refers to it.
    """
    words = [w for w in re.split(r"[^a-z0-9]+", (said or "").lower())
             if len(w) > 2 and w not in _STOPWORDS]
    if not words:
        return True                    # no description is not a mismatch
    hay = os.path.basename(path).lower()
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            hay += " " + f.read(4000).lower()
    except OSError:
        pass
    return any(w in hay for w in words)


_STOPWORDS = {"the", "file", "doc", "document", "one", "that", "this", "you",
              "just", "made", "for", "and", "open", "show", "with", "about",
              "please", "can", "get", "put", "was", "were", "what", "which"}


def window_showing(path):
    """The app whose FRONT window is this file, or "" if none is.

    "A window exists somewhere" is not the same as "they can see it", and the
    difference is the whole bug: Accio already had
    2026_ppr_draft_strategy.md open in a background window, Neo saw the app
    come forward, called that success and said "it's up in Accio" — while they
    was looking at something else entirely.

    So this asks the stricter question: is the file titling a window that
    belongs to the app currently in front? macOS lists windows front to back,
    so the first layer-zero window is the one they are actually looking at.
    """
    stem = os.path.basename(path).lower()
    bare = os.path.splitext(stem)[0]
    try:
        from Quartz import (CGWindowListCopyWindowInfo, kCGNullWindowID,
                            kCGWindowListOptionOnScreenOnly)
        wins = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly,
                                          kCGNullWindowID) or []
    except Exception:
        return ""
    # Anchor on the frontmost APP, not on window order. "First layer-zero
    # window" looked equivalent and is not: an app can be frontmost with no
    # window at all — Finder was, and the first window in the list belonged to
    # Accio behind it, so a file open in Accio read as "on screen" while they were
    # looking at Finder.
    front = frontmost_app()
    if not front:
        return ""
    for w in wins:
        if w.get("kCGWindowLayer") != 0:
            continue                      # menu bar, dock, overlays
        if str(w.get("kCGWindowOwnerName") or "") != front:
            continue                      # a window behind the app they are in
        title = str(w.get("kCGWindowName") or "").lower()
        if stem in title or (bare and bare in title):
            return front
        return ""                         # their front window is something else
    return ""


def app_with_file(path):
    """Any app holding a window titled after this file, front or not."""
    stem = os.path.basename(path).lower()
    bare = os.path.splitext(stem)[0]
    try:
        from Quartz import (CGWindowListCopyWindowInfo, kCGNullWindowID,
                            kCGWindowListOptionOnScreenOnly)
        wins = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly,
                                          kCGNullWindowID) or []
    except Exception:
        return ""
    for w in wins:
        if w.get("kCGWindowLayer") != 0:
            continue
        title = str(w.get("kCGWindowName") or "").lower()
        if stem in title or (bare and bare in title):
            return str(w.get("kCGWindowOwnerName") or "")
    return ""


def raise_app(app):
    """Bring an app to the front. `open -a` will NOT do this.

    That is the whole bug behind "it's up in Accio" while they were looking at
    something else: when the app is already running with the file already
    open, `open -a` is a no-op — it does not raise the window, and it still
    exits zero. Accio had the document open behind Chrome for the entire
    exchange. Only an explicit activate moves it.
    """
    if not app:
        return False
    try:
        subprocess.run(["osascript", "-e",
                        f'tell application "{app}" to activate'],
                       capture_output=True, timeout=8)
    except Exception:
        return False
    # CHECK IT. osascript exits zero and prints nothing whether or not the app
    # actually came forward: macOS only lets a process raise another app if it
    # has been granted automation rights, and a denial looks exactly like a
    # success from here. Returning True regardless was the same silent-failure
    # bug this whole run is about, written fresh an hour ago.
    deadline = time.time() + 2.5
    want = app.lower()
    while time.time() < deadline:
        now = (frontmost_app() or "").lower()
        if now and (want in now or now in want):
            return True
        time.sleep(0.2)
    return False


def opened_ok(path, timeout=4.0):
    """Is this file ACTUALLY in front of them now? Verified, not assumed.

    If some app has it open but is sitting behind, this raises that app rather
    than reporting failure — having it open somewhere they cannot see is not the
    same as it being open, and it is not a reason to give up either.
    """
    deadline = time.time() + timeout
    raised = False
    while time.time() < deadline:
        who = window_showing(path)
        if who:
            return who
        holder = app_with_file(path)
        if holder and not raised:
            raised = True
            raise_app(holder)
        time.sleep(0.3)
    return ""


def open_file(path, log=print):
    """Open a file so they can SEE it. (ok, app_used).

    Tries the system handler, checks whether anything actually appeared, and
    falls back to an app that is guaranteed to show it. The check is the point:
    `open` exits zero for a type with no handler, so trusting it means telling
    them a document is on screen when it is not.
    """
    if not path or not os.path.isfile(path):
        return False, ""
    # DEEP-WORK MODE: open it where they can get to it, and do not take the
    # screen. `open -g` was measured doing exactly this — the file opened and
    # the frontmost app never changed.
    try:
        import quiet
        if quiet.is_on():
            app = _OPENERS.get(os.path.splitext(path)[1].lower())
            cmd = ["open", "-g"] + (["-a", app] if app else []) + [path]
            try:
                subprocess.run(cmd, capture_output=True, timeout=10)
            except Exception:
                return False, ""
            time.sleep(0.8)
            where = app_with_file(path)
            if where:
                log(f"[desk] opened {os.path.basename(path)} in {where}, "
                    "quietly — focus mode is on")
                return True, where
            return False, ""
    except ImportError:
        pass

    # Already open somewhere behind? Just raise it — reopening a document that
    # is already open is how you end up with two copies of it.
    holder = app_with_file(path)
    if holder:
        if raise_app(holder):
            who = opened_ok(path, timeout=4.0)
            if who:
                return True, who
        else:
            # It IS open — just not in front, and this process was not allowed
            # to raise it. Say that rather than "I couldn't open it", which
            # would send them looking for a file that is already there.
            log(f"[desk] {os.path.basename(path)} is open in {holder} but I "
                "could not bring it forward")
            return False, holder
    known = _OPENERS.get(os.path.splitext(path)[1].lower())
    # Named app FIRST for the types we know. Plain `open` exits zero for a type
    # with no handler and shows nothing, so trying it first on markdown just
    # spends three seconds proving that again.
    tries = ([["open", "-a", known, path], ["open", path]] if known
             else [["open", path], ["open", "-a", "TextEdit", path]])
    for i, cmd in enumerate(tries):
        try:
            subprocess.run(cmd, capture_output=True, timeout=10)
        except Exception:
            continue
        # A cold app takes real time to launch and come forward — several
        # seconds if it was quit. Six was not always enough under load.
        # `open -a` does not raise an already-running app, so ask explicitly.
        if known and i == 0:
            time.sleep(0.6)
            raise_app(known)
        who = opened_ok(path, timeout=10.0)
        if who:
            return True, who
        if i == 0:
            log(f"[desk] {os.path.basename(path)} did not come to the front — "
                "trying another way")
    return False, ""


# --------------------------------------------------------------------------- #
# Reading a document.
# --------------------------------------------------------------------------- #
def pdf_pages(path):
    """How many pages, or 0 if it is not a readable PDF."""
    try:
        from Quartz import (CGPDFDocumentCreateWithURL,
                            CGPDFDocumentGetNumberOfPages)
        from CoreFoundation import (CFURLCreateFromFileSystemRepresentation,
                                    kCFAllocatorDefault)
        raw = path.encode("utf-8")
        url = CFURLCreateFromFileSystemRepresentation(
            kCFAllocatorDefault, raw, len(raw), False)
        doc = CGPDFDocumentCreateWithURL(url)
        return int(CGPDFDocumentGetNumberOfPages(doc)) if doc else 0
    except Exception:
        return 0


def render_page(path, page=1, scale=2.0):
    """One PDF page as PNG bytes. None on any failure.

    Rendered rather than text-extracted on purpose: it needs no new dependency,
    it works on scanned documents where there is no text to extract, and the
    model sees the LAYOUT — so it can answer "what does the table say" and
    "what's the heading", which plain text extraction is worst at.
    """
    try:
        from Quartz import (CGPDFDocumentCreateWithURL, CGPDFDocumentGetPage,
                            CGPDFPageGetBoxRect, kCGPDFMediaBox,
                            CGBitmapContextCreate, CGColorSpaceCreateDeviceRGB,
                            CGContextDrawPDFPage, CGBitmapContextCreateImage,
                            CGImageDestinationCreateWithData,
                            CGImageDestinationAddImage,
                            CGImageDestinationFinalize, CGContextScaleCTM,
                            CGContextSetRGBFillColor, CGContextFillRect,
                            CGRectMake)
        from CoreFoundation import (CFURLCreateFromFileSystemRepresentation,
                                    kCFAllocatorDefault, CFDataCreateMutable)
        raw = path.encode("utf-8")
        url = CFURLCreateFromFileSystemRepresentation(
            kCFAllocatorDefault, raw, len(raw), False)
        doc = CGPDFDocumentCreateWithURL(url)
        if not doc:
            return None
        pg = CGPDFDocumentGetPage(doc, int(page))
        if not pg:
            return None
        box = CGPDFPageGetBoxRect(pg, kCGPDFMediaBox)
        w, h = int(box.size.width), int(box.size.height)
        if w <= 0 or h <= 0:
            return None
        ctx = CGBitmapContextCreate(None, int(w * scale), int(h * scale), 8, 0,
                                    CGColorSpaceCreateDeviceRGB(), 1 << 14 | 2)
        # White first: a PDF page is transparent, and transparent renders black,
        # which is unreadable for both a person and a model.
        CGContextSetRGBFillColor(ctx, 1, 1, 1, 1)
        CGContextFillRect(ctx, CGRectMake(0, 0, w * scale, h * scale))
        CGContextScaleCTM(ctx, scale, scale)
        CGContextDrawPDFPage(ctx, pg)
        img = CGBitmapContextCreateImage(ctx)
        data = CFDataCreateMutable(kCFAllocatorDefault, 0)
        dest = CGImageDestinationCreateWithData(data, "public.png", 1, None)
        CGImageDestinationAddImage(dest, img, None)
        CGImageDestinationFinalize(dest)
        return bytes(data)
    except Exception:
        return None


def read_document(path, question, client, log=print, max_pages=MAX_PDF_PAGES):
    """Answer `question` about a document by looking at its pages."""
    from google.genai import types
    n = pdf_pages(path)
    if not n:
        return None
    pages = list(range(1, min(n, max_pages) + 1))
    parts = []
    for p in pages:
        png = render_page(path, p)
        if png:
            parts.append(types.Part.from_bytes(data=png, mime_type="image/png"))
    if not parts:
        return None
    tail = ("" if n <= max_pages else
            f"\n\nNOTE: this document has {n} pages and you are seeing the "
            f"first {len(parts)}. If the answer is not in these, say which "
            f"pages you would need rather than guessing.")
    parts.append(
        f"These are pages from '{os.path.basename(path)}'.\n\n{question}\n\n"
        f"Answer out loud, in two or three sentences. No markdown, no lists, "
        f"no reading the filename aloud." + tail)
    try:
        r = client.models.generate_content(model=VISION_MODEL, contents=parts)
        return (getattr(r, "text", "") or "").strip() or None
    except Exception as e:
        log(f"[desk] couldn't read {os.path.basename(path)}: {type(e).__name__}")
        return None


# --------------------------------------------------------------------------- #
# What is open right now.
# --------------------------------------------------------------------------- #
_DOC_APPS = {
    "Preview": 'tell application "Preview" to get POSIX path of (get path of front document)',
    "Adobe Acrobat": 'tell application "Adobe Acrobat" to get POSIX path of (get file alias of active doc)',
    "TextEdit": 'tell application "TextEdit" to get POSIX path of (get path of front document)',
    "Pages": 'tell application "Pages" to get POSIX path of (get file of front document)',
    "Numbers": 'tell application "Numbers" to get POSIX path of (get file of front document)',
    "Keynote": 'tell application "Keynote" to get POSIX path of (get file of front document)',
}


def frontmost_app():
    try:
        from AppKit import NSWorkspace
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return str(app.localizedName()) if app else ""
    except Exception:
        return ""


def open_document():
    """The file open in front of them, if the app will say. (app, path) or None.

    Asking the APP beats screenshotting the window and reading it: the answer
    is the real file, so Neo can read all twelve pages rather than the one that
    happens to be scrolled into view.
    """
    app = frontmost_app()
    script = _DOC_APPS.get(app)
    if not script:
        return (app, None) if app else None
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, timeout=8)
        path = r.stdout.decode("utf-8", "replace").strip()
        return (app, path if path and os.path.exists(path) else None)
    except Exception:
        return (app, None)
