"""
webdrive.py — Neo's own browser, running in the background.

Neo drives a SEPARATE copy of the Chrome already on the Mac (its own profile
folder under ~/neo/.browser), over the DevTools protocol. Nothing to
download, nothing touches the person's own windows or tabs, and it runs
headless — they keep using the computer while Neo reads Gmail, opens a Doc,
fills a form. The one thing it can't do is sign in for them:

  LOGIN WALL -> the same profile is relaunched WITH a window, brought to the
  front, and Neo asks them to sign in. Cookies live in the profile, so the
  next time it's already in. When the cookies expire, the same thing again.

Why not the person's real Chrome? Driving it needs it launched with a debug
port, which it never is, and taking over their tabs is the thing this exists
to avoid. Why not Playwright? A 300MB download during setup, for what a
websocket and Chrome's own protocol do.

Pure pieces: parse target lists, login detection (chrome.looks_like_login),
page text trimming. Impure: launch, connect, evaluate.
"""

import json
import os
import re
import shutil
import subprocess
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(HERE, ".browser")
PORT = int(os.getenv("NEO_BROWSER_PORT", "9333"))
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_ALTS = ["/Applications/Chromium.app/Contents/MacOS/Chromium",
               "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
               "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
MAX_TEXT = 6000

_proc = None
_headed = False


def binary():
    for b in [CHROME] + CHROME_ALTS:
        if os.path.exists(b):
            return b
    return None


def available():
    # NEO_NO_AUDIO=1 is the test-mode flag across the codebase: a test must
    # never launch a browser window on the developer's screen.
    if os.getenv("NEO_NO_BROWSER") == "1" or os.getenv("NEO_NO_AUDIO") == "1":
        return False
    return binary() is not None


# --------------------------------------------------------------------------- #
# launching
# --------------------------------------------------------------------------- #
def _alive():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def launch(headed=False, log=print):
    """Start Neo's browser (or reuse it). headed=True shows a window."""
    global _proc, _headed
    if _alive() and _headed == headed:
        return True
    stop()
    b = binary()
    if not b:
        return False
    os.makedirs(PROFILE_DIR, exist_ok=True)
    args = [b, f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE_DIR}",
            "--no-first-run", "--no-default-browser-check", "--disable-sync",
            "--window-size=1200,900", "--disable-background-timer-throttling"]
    if not headed:
        args.append("--headless=new")
    try:
        _proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        log(f"[web] couldn't start the browser: {e}")
        return False
    _headed = headed
    for _ in range(60):
        if _alive():
            return True
        time.sleep(0.25)
    log("[web] the browser never answered on its debug port")
    return False


def stop():
    global _proc
    if _proc is not None:
        try:
            _proc.terminate()
            _proc.wait(timeout=5)
        except Exception:
            try:
                _proc.kill()
            except Exception:
                pass
    _proc = None


# --------------------------------------------------------------------------- #
# the protocol: one page, one socket
# --------------------------------------------------------------------------- #
class Page:
    def __init__(self, ws_url):
        from websockets.sync.client import connect
        self._ws = connect(ws_url, max_size=32 * 1024 * 1024, open_timeout=10)
        self._id = 0

    def call(self, method, **params):
        self._id += 1
        self._ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        while True:
            msg = json.loads(self._ws.recv(timeout=30))
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise RuntimeError(msg["error"].get("message", "cdp error"))
                return msg.get("result", {})

    def eval(self, js):
        r = self.call("Runtime.evaluate", expression=js, returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("value")

    def goto(self, url, wait=8.0):
        self.call("Page.navigate", url=url)
        end = time.time() + wait
        while time.time() < end:
            if self.eval("document.readyState") == "complete":
                break
            time.sleep(0.2)
        time.sleep(0.6)      # let the app's own JS paint

    def url(self):
        return self.eval("location.href") or ""

    def close(self):
        try:
            self._ws.close()
        except Exception:
            pass


def _targets():
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=3) as r:
        return json.load(r)


def page(log=print):
    """The page Neo works in (one tab, reused)."""
    for t in _targets():
        if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
            return Page(t["webSocketDebuggerUrl"])
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/json/new?about:blank", method="PUT")
    with urllib.request.urlopen(req, timeout=3) as r:
        t = json.load(r)
    return Page(t["webSocketDebuggerUrl"])


# --------------------------------------------------------------------------- #
# reading and acting, in JS
# --------------------------------------------------------------------------- #
_TEXT_JS = r"""
(() => {
  const sel = 'main, [role=main], article';
  const root = document.querySelector(sel) || document.body;
  const t = (root.innerText || '').replace(/\n{3,}/g, '\n\n').trim();
  return t;
})()"""

_CLICK_JS = r"""
((label) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const want = norm(label);
  const els = [...document.querySelectorAll('a, button, [role=button], [role=link], [role=tab], input[type=submit], [onclick], summary, [role=menuitem], [role=option], div[jsaction], span[jsaction]')];
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  let best = null, bestScore = 0;
  for (const e of els) {
    if (!vis(e)) continue;
    const t = norm(e.innerText || e.value || e.getAttribute('aria-label') || e.title);
    if (!t) continue;
    let s = 0;
    if (t === want) s = 3; else if (t.startsWith(want)) s = 2; else if (t.includes(want)) s = 1;
    if (s > bestScore) { best = e; bestScore = s; }
  }
  if (!best) return 'no control called ' + JSON.stringify(label);
  best.scrollIntoView({block: 'center'});
  best.click();
  return 'clicked ' + JSON.stringify((best.innerText || best.value || label).trim().slice(0, 60));
})"""

_TYPE_JS = r"""
((label, text) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const want = norm(label);
  const fields = [...document.querySelectorAll('input:not([type=hidden]):not([type=submit]), textarea, [contenteditable=true], [role=textbox]')];
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const labelOf = e => {
    const id = e.id; let l = '';
    if (id) { const lab = document.querySelector('label[for="' + CSS.escape(id) + '"]'); if (lab) l += ' ' + lab.innerText; }
    l += ' ' + (e.placeholder || '') + ' ' + (e.getAttribute('aria-label') || '') + ' ' + (e.name || '') + ' ' + (e.closest('label')?.innerText || '');
    return norm(l);
  };
  let best = null, bestScore = 0;
  for (const e of fields) {
    if (!vis(e)) continue;
    const t = labelOf(e);
    let s = 0;
    if (t === want) s = 3; else if (t.includes(want)) s = 2; else if (!want) s = 1;
    if (s > bestScore) { best = e; bestScore = s; }
  }
  if (!best && fields.length === 1 && vis(fields[0])) best = fields[0];
  if (!best) return 'no field called ' + JSON.stringify(label);
  best.focus();
  if (best.isContentEditable) { best.textContent = text; }
  else { const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(best), 'value')?.set; setter ? setter.call(best, text) : (best.value = text); }
  best.dispatchEvent(new Event('input', {bubbles: true}));
  best.dispatchEvent(new Event('change', {bubbles: true}));
  return 'typed into ' + JSON.stringify(labelOf(best).slice(0, 50) || 'the field');
})"""


def trim(text, limit=MAX_TEXT):
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit("\n", 1)[0] + f"\n…(cut at {limit} characters)"


# --------------------------------------------------------------------------- #
# the login handoff
# --------------------------------------------------------------------------- #
def needs_login(url):
    import chrome
    return chrome.looks_like_login(url)


def service_name(url):
    """"https://mail.google.com/..." -> "Gmail". The spoken sign-in message has
    to name the thing the person actually asked for: they said "check my email"
    and "this page wants a sign-in" answers a question nobody asked."""
    host = re.sub(r"^https?://", "", (url or "")).split("/")[0].lower()
    for frag, name in (("mail.google", "Gmail"), ("calendar.google", "Google Calendar"),
                       ("drive.google", "Google Drive"), ("docs.google", "Google Docs"),
                       ("contacts.google", "Google Contacts"),
                       ("accounts.google", "your Google account"),
                       ("google.", "Google")):
        if frag in host:
            return name
    host = re.sub(r"^www\.", "", host)
    return host or "that page"


def show_for_login(url, log=print, ask=None):
    """Relaunch WITH a window on the same profile, bring it forward, ask them
    to sign in. Returns the message to say, naming the service. In stealth
    nothing pops up: the sign-in waits for later."""
    what = service_name(url)
    if _stealth_on():
        return (f"{what} wants a sign-in, and I'm in stealth so I haven't "
                "put a window up. Ask again when you're out of stealth and "
                "I'll bring the sign-in to you.")
    launch(headed=True, log=log)
    try:
        pg = page(log)
        pg.goto(url)
        pg.close()
    except Exception as e:
        log(f"[web] couldn't open the sign-in page: {e}")
    subprocess.run(["osascript", "-e", 'tell application "Google Chrome" to activate'],
                   capture_output=True, timeout=5)
    return (f"I need you to sign in to {what} — I don't do passwords. I've put "
            "my browser window in front of you; sign in there once and I'll be "
            "in from then on. Tell me when you're done.")


def _stealth_on():
    try:
        import json as _j
        d = _j.load(open(os.path.join(HERE, "stealth.json")))
        return bool(d.get("on")) and (time.time() - float(d.get("since", 0))) < 4 * 3600
    except Exception:
        return False


def hide(log=print):
    """After they've signed in: back to headless, same cookies."""
    launch(headed=False, log=log)


# --------------------------------------------------------------------------- #
# the operations the tools call
# --------------------------------------------------------------------------- #
def open_and_read(url, log=print):
    """(text, problem). problem is 'login' with the url, 'no_browser', or None."""
    if not available():
        return None, "no_browser"
    if not launch(headed=_headed, log=log):
        return None, "no_browser"
    try:
        pg = page(log)
        pg.goto(url)
        landed = pg.url()
        if needs_login(landed):
            pg.close()
            return landed, "login"
        text = trim(pg.eval(_TEXT_JS) or "")
        pg.close()
        return text, None
    except Exception as e:
        log(f"[web] {type(e).__name__}: {e}")
        return None, "error"


def act(kind, label, text="", log=print):
    """click / type on the current page, then read what it says now."""
    if not _alive():
        return None, "no_page"
    try:
        pg = page(log)
        if kind == "click":
            out = pg.eval(f"({_CLICK_JS})({json.dumps(label)})")
        else:
            out = pg.eval(f"({_TYPE_JS})({json.dumps(label)}, {json.dumps(text)})")
        time.sleep(1.0)
        landed = pg.url()
        if needs_login(landed):
            pg.close()
            return landed, "login"
        now = trim(pg.eval(_TEXT_JS) or "", 2500)
        pg.close()
        return f"{out}\nURL now: {landed}\n\n{now}", None
    except Exception as e:
        log(f"[web] {type(e).__name__}: {e}")
        return None, "error"


def current_url():
    if not _alive():
        return ""
    try:
        pg = page()
        u = pg.url()
        pg.close()
        return u
    except Exception:
        return ""


TROUBLE = {
    "no_browser": "I need Google Chrome on this Mac to browse in the background, and I can't find it.",
    "no_page": "I don't have a page open yet — tell me where to go first.",
    "error": "The page didn't cooperate — try again, or tell me another way in.",
}
