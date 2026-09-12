"""
overlay.py — Neo's floating indicator: the island.

A frosted capsule that drops down under the menu bar, centred, only while
something is happening. Hairline bars follow the mic while you talk and Neo's
voice while they do; one dot carries the state colour. It's click-through and
non-activating, so it never steals focus from what you're doing.

Rendered with a transparent WebKit view (so it can do real glass, glow, and
smooth animation), hosted in a borderless non-activating panel. Python pushes the
current state into the page live.

Driven by neo.py through the thread-safe State object:
    state.set("listening" | "thinking" | "speaking" | "idle")
    state.set_level(0.0..1.0)   # voice loudness while speaking
"""

import threading

from Cocoa import (
    NSPanel, NSColor, NSScreen, NSMakeRect, NSBackingStoreBuffered, NSTimer, NSObject,
    NSWindowStyleMaskBorderless, NSWindowStyleMaskNonactivatingPanel,
    NSWindowCollectionBehaviorCanJoinAllSpaces, NSWindowCollectionBehaviorStationary,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
)
from Quartz import kCGMaximumWindowLevel
import WebKit


# ----- thread-safe shared state between neo.py workers and the UI -----
class State:
    def __init__(self):
        self._lock = threading.Lock()
        self._state = "idle"
        self._level = 0.0

    def set(self, s):
        with self._lock:
            self._state = s

    def get(self):
        with self._lock:
            return self._state

    def set_level(self, v):
        with self._lock:
            self._level = max(0.0, min(1.0, float(v)))

    def get_level(self):
        with self._lock:
            return self._level

    def set_step(self, text):
        """The short line inside the capsule: "Searching the web",
        "Setting the reminder". Empty clears it."""
        with self._lock:
            self._step = (text or "")[:40]

    def get_step(self):
        with self._lock:
            return getattr(self, "_step", "")


PANEL_W = 300
PANEL_H = 56
# Just under the menu bar, centred — where macOS itself talks to you (the
# notch, Now Playing). Under a notch the capsule hangs beneath it.
MARGIN_TOP = 2


# THE ISLAND. A frosted capsule, hidden when idle, that drops down under the
# menu bar when something is happening. One shape; the states are told apart
# by what MOVES, plus one dot that carries the same three colours the orb
# did, on their say-so both times: "the blob should still follow the same
# different colours, otherwise it's too hard to tell the other modes" and,
# for this one, "color code the little black dot same way".
#
#   listening — five hairline bars follow the MIC; the dot is blue.
#   thinking  — the bars fall flat and a word appears; the dot is amber and
#               breathes.
#   speaking  — the bars follow NEO'S VOICE; the dot is green and a touch
#               larger.
#
# The bars stay monochrome — white on a dark ground, near-black on a light
# one — so the capsule reads as part of macOS on any wallpaper. Glass does
# the rest: it takes the colour of whatever is behind it.
_HTML = r"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
  html,body{margin:0;height:100%;background:transparent;overflow:hidden;
    font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text",Helvetica,sans-serif;
    -webkit-font-smoothing:antialiased;user-select:none;-webkit-user-select:none}
  #wrap{position:absolute;inset:0;display:flex;align-items:flex-start;justify-content:center;padding-top:4px}
  #cap{height:36px;padding:0 15px 0 13px;border-radius:18px;display:flex;align-items:center;gap:11px;
    background:rgba(255,255,255,.7);border:1px solid rgba(255,255,255,.9);
    box-shadow:0 8px 26px rgba(0,0,0,.22),0 0 0 .5px rgba(0,0,0,.12),inset 0 1px 0 rgba(255,255,255,.6);
    -webkit-backdrop-filter:blur(20px) saturate(1.5);backdrop-filter:blur(20px) saturate(1.5);
    opacity:0;transform:translateY(-16px) scale(.96);
    transition:opacity .28s cubic-bezier(.2,.8,.2,1),transform .34s cubic-bezier(.2,.8,.2,1)}
  #cap.on{opacity:1;transform:none}
  #cap.dark{background:rgba(30,30,36,.74);border-color:rgba(255,255,255,.2);box-shadow:0 8px 26px rgba(0,0,0,.5),0 0 0 .5px rgba(0,0,0,.4)}
  #dot{width:9px;height:9px;border-radius:50%;background:#2997FF;flex:none;
    box-shadow:0 0 10px rgba(41,151,255,.55);
    transition:background .35s,transform .3s cubic-bezier(.2,.8,.2,1);
    box-shadow:0 0 0 0 rgba(0,0,0,0)}
  #bars{display:flex;align-items:center;gap:3px;height:22px}
  #bars i{display:block;width:3px;height:4px;border-radius:1.5px;background:#111;transition:height .07s linear}
  .dark #bars i{background:#fff}
  #txt{font-size:13px;font-weight:500;letter-spacing:.005em;color:#111;white-space:nowrap;
    max-width:0;overflow:hidden;opacity:0;transition:max-width .35s cubic-bezier(.2,.8,.2,1),opacity .25s}
  .dark #txt{color:#fff}
  #txt.on{max-width:180px;opacity:.85}
  /* thinking */
  .thinking #dot{background:#F4B23E;box-shadow:0 0 10px rgba(244,178,62,.6);animation:breath 1.5s ease-in-out infinite}
  .thinking #bars i{height:3px!important}
  /* speaking */
  .speaking #dot{background:#30D158;box-shadow:0 0 12px rgba(48,209,88,.6);transform:scale(1.3)}
  @keyframes breath{50%{opacity:.3}}
  @media (prefers-reduced-motion:reduce){#cap{transition:none}#dot{animation:none!important}}
</style></head>
<body><div id="wrap"><div id="cap"><span id="dot"></span><span id="bars"><i></i><i></i><i></i><i></i><i></i></span><span id="txt"></span></div></div>
<script>
  var cap=document.getElementById('cap'), bars=[...document.querySelectorAll('#bars i')], txt=document.getElementById('txt');
  var state='idle', level=0, target=0, step='', hideT=null, t0=performance.now();
  var WORDS={thinking:'Thinking'};
  window.setState=function(s){
    state=s;
    cap.classList.remove('listening','thinking','speaking');
    if(s==='idle'){ clearTimeout(hideT); hideT=setTimeout(function(){cap.classList.remove('on');},220); return; }
    clearTimeout(hideT); cap.classList.add('on', s);
    setStep(step);
  };
  window.setLevel=function(v){ target=Math.max(0,Math.min(1,+v||0)); };
  window.setStep=function(t){
    step=t||'';
    var show = step || WORDS[state] || '';
    txt.textContent=show; txt.classList.toggle('on', !!show && state!=='idle');
  };
  window.setDark=function(d){ cap.classList.toggle('dark', !!d); };
  function frame(now){
    var t=now-t0;
    level += (target-level)*(target>level?0.55:0.18);
    if(state==='listening'||state==='speaking'){
      for(var i=0;i<5;i++){
        var w=Math.sin(t/120+i*1.3)*0.5+0.5, edge=1-Math.abs(i-2)/4;
        var h=4+level*(state==='speaking'?18:15)*(0.55+0.45*w)*edge;
        bars[i].style.height=h.toFixed(1)+'px';
      }
    } else { for(var j=0;j<5;j++) bars[j].style.height='4px'; }
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
</script></body></html>"""


class _Pusher(NSObject):
    def tick_(self, timer):
        try:
            st = self.state.get()
            if st != self.last:
                self.web.evaluateJavaScript_completionHandler_(
                    "window.setState && setState('%s')" % st, None)
                self.last = st
                if st != "idle":
                    self._dark_check()
            if st in ("speaking", "listening"):
                self.web.evaluateJavaScript_completionHandler_(
                    "window.setLevel && setLevel(%.3f)" % self.state.get_level(), None)
            step = self.state.get_step()
            if step != self.last_step:
                self.web.evaluateJavaScript_completionHandler_(
                    "window.setStep && setStep(%s)" % _js_str(step), None)
                self.last_step = step
        except Exception as e:
            print(f"[overlay] push error: {e}")

    def _dark_check(self):
        """Light or dark capsule, by whatever is behind it right now."""
        try:
            self.web.evaluateJavaScript_completionHandler_(
                "window.setDark && setDark(%s)" % ("true" if _ground_is_dark() else "false"), None)
        except Exception:
            pass


def _js_str(s):
    import json
    return json.dumps(str(s or ""))


def _ground_is_dark():
    """Sample the screen just under the menu bar, centre. One tiny capture;
    only on a state change, never per frame."""
    try:
        import Quartz
        sf = NSScreen.mainScreen().frame()
        x = sf.size.width / 2 - 60
        y = 26
        img = Quartz.CGWindowListCreateImage(
            Quartz.CGRectMake(x, y, 120, 30), Quartz.kCGWindowListOptionOnScreenOnly,
            Quartz.kCGNullWindowID, Quartz.kCGWindowImageDefault)
        if img is None:
            return False
        prov = Quartz.CGImageGetDataProvider(img)
        data = Quartz.CGDataProviderCopyData(prov)
        b = bytes(data)
        if not b:
            return False
        step = max(4, (len(b) // 4) // 200 * 4)
        tot = n = 0
        for i in range(0, len(b) - 3, step):
            tot += (b[i] + b[i + 1] + b[i + 2]) / 3
            n += 1
        return (tot / max(n, 1)) < 110
    except Exception:
        return False


class Overlay:
    """Owns the floating panel. Build it on the MAIN thread before the run loop."""

    def __init__(self, state):
        self.state = state
        screen = NSScreen.mainScreen()
        sf = screen.frame()
        vf = screen.visibleFrame()          # excludes the menu bar
        menubar = (sf.origin.y + sf.size.height) - (vf.origin.y + vf.size.height)
        x = sf.origin.x + (sf.size.width - PANEL_W) / 2
        y = sf.origin.y + sf.size.height - menubar - PANEL_H - MARGIN_TOP
        rect = NSMakeRect(x, y, PANEL_W, PANEL_H)

        mask = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, mask, NSBackingStoreBuffered, False)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(False)
        panel.setIgnoresMouseEvents_(True)       # click-through
        panel.setLevel_(kCGMaximumWindowLevel)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorFullScreenAuxiliary)

        config = WebKit.WKWebViewConfiguration.alloc().init()
        web = WebKit.WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, PANEL_W, PANEL_H), config)
        try:
            web.setValue_forKey_(False, "drawsBackground")   # transparent web view
        except Exception:
            pass
        web.loadHTMLString_baseURL_(_HTML, None)
        panel.setContentView_(web)
        panel.orderFrontRegardless()

        self.panel = panel
        self.web = web

        pusher = _Pusher.alloc().init()
        pusher.web = web
        pusher.state = state
        pusher.last = None
        pusher.last_step = None
        self.pusher = pusher
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.05, pusher, "tick:", None, True)
