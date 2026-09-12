"""
stealth.py — using Neo without a sound, and without anyone noticing.

Double-press fn and Neo goes quiet: no voice in, no voice out, nothing
conspicuous on screen. From then on a press of fn opens a small chat box in
the top-right corner — type, return, read the answer — and the thread stays
there like any chat window. Double-press again and the voice comes back.

WHAT CHANGES IN STEALTH
  - A press opens the box instead of a live conversation. Holding and
    whispering still works (the mic opens while held), but the answer comes
    back as text, never audio.
  - Everything Neo would have SAID is printed in the box instead. Fillers
    ("On it") are dropped; the step line ("Searching the web") shows as a
    quiet working line.
  - Neo's own browser never pops a window for a sign-in; the box says it
    needs one, for later.
  - It persists across restarts and expires after four hours, like whisper
    and bedtime, so a stealth left on at 11pm isn't still on at breakfast.

The double-press detector and the on/off state are pure and tested; the
window is the same pattern as the panel and the HUD (main thread, polled).
"""

import json
import os
import re
import threading
import time

TTL_H = 4.0
DOUBLE_S = 0.45        # two taps within this = a double-press
TAP_S = 0.35           # a press shorter than this, with nothing said, is a tap

_ON_RX = re.compile(r"\b(stealth|go quiet|quiet mode|silent mode|text mode)\b", re.I)
_OFF_RX = re.compile(r"\b(normal mode|voice back|talk to me again|stop stealth|stealth off|"
                     r"out of stealth|leave stealth)\b", re.I)


def wants_on(text):
    t = str(text or "")
    return bool(_ON_RX.search(t)) and not _OFF_RX.search(t)


def wants_off(text):
    return bool(_OFF_RX.search(str(text or "")))


class DoubleTap:
    """Pure. Feed it press/release times; it says when a double-press happened.

    A tap is a press shorter than TAP_S. Two taps whose presses are within
    DOUBLE_S of each other make a double. A hold (talking) resets it, so
    "tap, then hold and talk" is never mistaken for a double."""

    def __init__(self, double_s=DOUBLE_S, tap_s=TAP_S):
        self.double_s, self.tap_s = double_s, tap_s
        self._down = None
        self._last_tap = None

    def press(self, now=None):
        self._down = time.time() if now is None else now

    def release(self, now=None):
        """True exactly when this release completes a double-press."""
        now = time.time() if now is None else now
        down, self._down = self._down, None
        if down is None or (now - down) > self.tap_s:
            self._last_tap = None
            return False
        if self._last_tap is not None and (down - self._last_tap) <= self.double_s:
            self._last_tap = None
            return True
        self._last_tap = down
        return False


class Mode:
    """On/off with a TTL, persisted. Pure given `path` and `now`."""

    def __init__(self, path, ttl_h=TTL_H):
        self.path, self.ttl = path, ttl_h * 3600
        self._lock = threading.Lock()
        self._on, self._since = False, 0.0
        self._load()

    def _load(self):
        try:
            d = json.load(open(self.path))
            self._on, self._since = bool(d.get("on")), float(d.get("since", 0))
        except Exception:
            self._on, self._since = False, 0.0

    def _save(self):
        try:
            with open(self.path, "w") as f:
                json.dump({"on": self._on, "since": self._since}, f)
        except OSError:
            pass

    def active(self, now=None):
        now = time.time() if now is None else now
        with self._lock:
            if self._on and (now - self._since) > self.ttl:
                self._on = False
                self._save()
            return self._on

    def set(self, on, now=None):
        now = time.time() if now is None else now
        with self._lock:
            changed = bool(on) != self._on
            self._on, self._since = bool(on), (now if on else 0.0)
            self._save()
        return changed

    def toggle(self, now=None):
        self.set(not self.active(now), now)
        return self.active(now)


# --------------------------------------------------------------------------- #
# the box: shared state, polled by the window on the main thread
# --------------------------------------------------------------------------- #
class _Box:
    def __init__(self):
        self._lock = threading.Lock()
        self.visible = False
        self.lines = []            # [{"who": "You"|"Neo"|"sys", "text": ...}]
        self.working = ""
        self.dirty = False
        self.focus = False
        self.on_send = lambda text: None
        self.hide_at = 0.0
        self.notice = ""

    def add(self, who, text):
        with self._lock:
            self.lines.append({"who": who, "text": str(text or "").strip()})
            self.lines = self.lines[-60:]
            self.working = "" if who == "Neo" else self.working
            self.dirty = True

    def set_working(self, text):
        with self._lock:
            self.working = text or ""
            self.dirty = True

    def show(self, focus=True):
        with self._lock:
            self.visible, self.focus, self.dirty, self.hide_at = True, focus, True, 0.0
            self.notice = ""

    def hide(self):
        with self._lock:
            self.visible, self.dirty, self.hide_at = False, True, 0.0

    def flash(self, text, seconds=3.0):
        """A one-line notice that shows the box briefly, without focus. It is
        a banner, not a line in the thread — the thread is the conversation,
        and "Stealth on" is not part of it."""
        with self._lock:
            self.notice = text
            self.visible, self.focus, self.dirty = True, False, True
            self.hide_at = time.time() + seconds

    def snapshot(self):
        with self._lock:
            self.dirty = False
            f, self.focus = self.focus, False
            return {"visible": self.visible, "lines": list(self.lines),
                    "working": self.working, "focus": f, "notice": self.notice}


box = _Box()


_HTML = r"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
  html,body{margin:0;height:100%;background:transparent;overflow:hidden;
    font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text",Helvetica,sans-serif;-webkit-font-smoothing:antialiased}
  #card{position:absolute;inset:8px;border-radius:16px;display:flex;flex-direction:column;
    background:rgba(28,28,32,.84);border:1px solid rgba(255,255,255,.14);
    box-shadow:0 14px 40px rgba(0,0,0,.45);
    -webkit-backdrop-filter:blur(24px) saturate(1.4);backdrop-filter:blur(24px) saturate(1.4);
    opacity:0;transform:translateY(-10px) scale(.98);transition:opacity .22s cubic-bezier(.2,.8,.2,1),transform .28s cubic-bezier(.2,.8,.2,1)}
  #card.on{opacity:1;transform:none}
  #head{display:flex;align-items:center;gap:8px;padding:10px 14px 6px;font-size:11px;color:rgba(255,255,255,.5);letter-spacing:.06em;text-transform:uppercase;font-weight:600}
  #head i{width:6px;height:6px;border-radius:50%;background:#fff;opacity:.6}
  #thread{flex:1;overflow-y:auto;padding:4px 14px 8px;display:flex;flex-direction:column;gap:8px}
  .m{max-width:92%;font-size:13px;line-height:1.45;padding:7px 11px;border-radius:12px;white-space:pre-wrap;word-wrap:break-word}
  .you{align-self:flex-end;background:rgba(255,255,255,.16);color:#fff;border-bottom-right-radius:4px}
  .neo{align-self:flex-start;background:rgba(255,255,255,.07);color:#F2F2F4;border-bottom-left-radius:4px}
  .sys{align-self:center;background:transparent;color:rgba(255,255,255,.55);font-size:12px;padding:2px 0}
  #notice{font-size:12.5px;color:rgba(255,255,255,.7);text-align:center;padding:6px 14px 8px;
    border-top:1px solid rgba(255,255,255,.08)}
  #notice:empty{display:none}
  #working{font-size:12px;color:rgba(255,255,255,.5);padding:0 14px 6px;min-height:16px}
  #working:empty{display:none}
  #in{display:flex;align-items:center;gap:8px;padding:8px 10px 10px}
  #box{flex:1;font:13.5px -apple-system,BlinkMacSystemFont,sans-serif;color:#fff;background:rgba(255,255,255,.08);
    border:1px solid rgba(255,255,255,.12);border-radius:10px;padding:8px 11px;outline:none;resize:none;height:36px;line-height:18px}
  #box:focus{border-color:rgba(255,255,255,.35)}
  #box::placeholder{color:rgba(255,255,255,.35)}
  #thread::-webkit-scrollbar{width:0}
</style></head><body>
<div id="card">
  <div id="head"><i></i>Neo · stealth</div>
  <div id="thread"></div>
  <div id="working"></div>
  <div id="notice"></div>
  <div id="in"><textarea id="box" placeholder="Ask anything. Return to send, Esc to close." rows="1"></textarea></div>
</div>
<script>
  var card=document.getElementById('card'), thread=document.getElementById('thread'), box=document.getElementById('box'), working=document.getElementById('working');
  var n=0;
  function send(m){ try{ window.webkit.messageHandlers.neo.postMessage(JSON.stringify(m)); }catch(e){} }
  function esc(s){ return String(s==null?'':s).replace(/[&<>]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c];}); }
  window.render=function(p){
    var s=typeof p==='string'?JSON.parse(p):p;
    card.classList.toggle('on', !!s.visible);
    if (s.lines.length!==n){
      thread.innerHTML=s.lines.map(function(l){ return '<div class="m '+(l.who==='You'?'you':l.who==='Neo'?'neo':'sys')+'">'+esc(l.text)+'</div>'; }).join('');
      n=s.lines.length; thread.scrollTop=thread.scrollHeight;
    }
    working.textContent=s.working||'';
    document.getElementById('notice').textContent=s.notice||'';
    if (s.focus){ setTimeout(function(){ box.focus(); }, 60); }
  };
  box.addEventListener('keydown', function(e){
    if (e.key==='Escape'){ send({action:'close'}); return; }
    if (e.key==='Enter' && !e.shiftKey){ e.preventDefault(); var t=box.value.trim(); if(!t) return; box.value=''; send({action:'send', text:t}); }
  });
  document.addEventListener('keydown', function(e){ if (e.key==='Escape') send({action:'close'}); });
</script></body></html>"""


class Chat:
    """The window. Main thread only, like the panel."""

    def __init__(self):
        from Cocoa import (NSPanel, NSColor, NSScreen, NSMakeRect, NSBackingStoreBuffered,
                           NSTimer, NSObject, NSWindowStyleMaskBorderless,
                           NSWindowStyleMaskNonactivatingPanel,
                           NSWindowCollectionBehaviorCanJoinAllSpaces,
                           NSWindowCollectionBehaviorStationary,
                           NSWindowCollectionBehaviorFullScreenAuxiliary)
        from Quartz import kCGMaximumWindowLevel
        import WebKit

        W, H = 380, 420
        sf = NSScreen.mainScreen().frame()
        vf = NSScreen.mainScreen().visibleFrame()
        top = vf.origin.y + vf.size.height
        rect = NSMakeRect(sf.origin.x + sf.size.width - W - 6, top - H - 4, W, H)

        class _StealthPanel(NSPanel):
            def canBecomeKeyWindow(self):
                return True       # the box needs the keyboard, without activating Neo

        mask = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        p = _StealthPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, mask, NSBackingStoreBuffered, False)
        p.setOpaque_(False)
        p.setBackgroundColor_(NSColor.clearColor())
        p.setHasShadow_(False)
        p.setLevel_(kCGMaximumWindowLevel)
        p.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces
                                 | NSWindowCollectionBehaviorStationary
                                 | NSWindowCollectionBehaviorFullScreenAuxiliary)
        p.setIgnoresMouseEvents_(True)

        outer = self

        class _StealthBridge(NSObject):
            def userContentController_didReceiveScriptMessage_(self, c, message):
                try:
                    m = json.loads(str(message.body()))
                except Exception:
                    return
                if m.get("action") == "close":
                    box.hide()
                elif m.get("action") == "send" and m.get("text"):
                    box.add("You", m["text"])
                    box.set_working("…")
                    try:
                        box.on_send(m["text"])
                    except Exception as e:
                        box.add("sys", f"couldn't send: {e}")

        cfg = WebKit.WKWebViewConfiguration.alloc().init()
        bridge = _StealthBridge.alloc().init()
        cfg.userContentController().addScriptMessageHandler_name_(bridge, "neo")
        web = WebKit.WKWebView.alloc().initWithFrame_configuration_(NSMakeRect(0, 0, W, H), cfg)
        try:
            web.setValue_forKey_(False, "drawsBackground")
        except Exception:
            pass
        web.loadHTMLString_baseURL_(_HTML, None)
        p.setContentView_(web)
        self.panel, self.web, self._bridge = p, web, bridge
        self._shown = False

        class _StealthTick(NSObject):
            def tick_(self, timer):
                try:
                    outer._tick()
                except Exception as e:
                    print(f"[stealth] tick: {e}")
        # ESC FROM ANYWHERE. The webview only sees keys while it is first
        # responder; once they click back into their document, Esc went to that
        # app and the box stayed. A global monitor sees Esc whichever app is
        # in front (Neo already has the Input Monitoring grant); the local one
        # covers the case where Neo's own panel is key.
        try:
            from Cocoa import NSEvent, NSEventMaskKeyDown

            def _esc(ev):
                try:
                    if ev.keyCode() == 53 and box.visible:
                        box.hide()
                except Exception:
                    pass
            self._esc_global = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                NSEventMaskKeyDown, _esc)

            def _esc_local(ev):
                _esc(ev)
                return ev
            self._esc_local = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
                NSEventMaskKeyDown, _esc_local)
        except Exception as e:
            print(f"[stealth] no key monitor ({e}); Esc works from the field only")

        self._ticker = _StealthTick.alloc().init()
        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.08, self._ticker, "tick:", None, True)

    def _tick(self):
        if box.hide_at and time.time() > box.hide_at and not box.focus:
            box.hide()
        if not box.dirty:
            return
        s = box.snapshot()
        if s["visible"] and not self._shown:
            self.panel.setIgnoresMouseEvents_(False)
            self.panel.orderFrontRegardless()
            self._shown = True
        if s["visible"] and s["focus"]:
            self.panel.makeKeyAndOrderFront_(None)
            self.web.becomeFirstResponder()
        if not s["visible"] and self._shown:
            self.panel.setIgnoresMouseEvents_(True)
            self._shown = False
        js = "window.render && render(%s)" % json.dumps(json.dumps(s))
        self.web.evaluateJavaScript_completionHandler_(js, None)
        if not s["visible"]:
            # let the fade finish, then really drop the window so it never
            # sits invisible in front of a click
            def _out():
                if not box.visible:
                    self.panel.orderOut_(None)
            from PyObjCTools import AppHelper
            AppHelper.callLater(0.3, _out)
