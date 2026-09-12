"""
hud.py — Neo's status HUD: a glass panel, top-right, that appears when Neo is
DOING something and fades when it's idle.

Not the tiny glow pill — this shows what Neo's actually up to ("Claude's
building the signups feature", "opening Schoology", "thinking") plus what's
waiting on you (the sentinel's next-actions). Translucent glass in the AURA
style; theme auto-switches light by day / dark by night so it never washes
out against your screen.

Design (matches overlay.py's proven recipe): a borderless non-activating
WKWebView panel, click-through, top-right. Python pushes state; the page
renders + animates it and handles show/hide + day-night theme itself.

    hud.set_activity("working", "Claude's building the signups feature", eta="4 min")
    hud.set_actions([{"label":"Jordan replied — answer them","meta":"2 days"}])
    hud.clear()               # nothing happening -> fades out

See it without touching Neo:   python hud.py --demo
"""

import json
import threading

_HTML = r"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
  /* A QUIET PROGRESS LIST. Redesigned after the user saw the old one on a
     fantasy-football job and said it did not look good — and they were right:
     the title wrapped to two lines and cut mid-phrase, three web searches all
     rendered as the identical words "Looking online", an internal tool name
     ("ToolSearch") leaked onto their screen, and every finished step was struck
     through, which put a line through most of the card.

     What changed: strikethrough is gone (done steps recede by weight and
     colour instead, which is quieter and easier to scan), the current step
     leads with a soft pulsing dot rather than an outlined ring, older steps
     collapse into one muted line instead of scrolling away, and the elapsed
     time sits in the chip so a long job never looks stalled. */
  /* ONE MATERIAL. The same glass as the island and the stealth box: near-
     black, blurred, a hairline light edge, no coloured glow. State is one
     dot and one word; the only colour is amber for "waiting on you". */
  :root{
    --ink:#F2F2F4; --muted:#A1A1A6; --faint:#6E6E73;
    --line:rgba(255,255,255,.1); --blue:#2997FF; --amber:#F4B23E;
  }
  body.day{ --ink:#F2F2F4; --muted:#A1A1A6; --faint:#6E6E73; }
  html,body{margin:0;height:100%;background:transparent;overflow:hidden;
    font-family:-apple-system,"SF Pro Text",Helvetica,sans-serif;
    -webkit-font-smoothing:antialiased;-webkit-user-select:none;}
  #hud{position:absolute;top:10px;right:12px;width:320px;
    display:flex;flex-direction:column;gap:8px;
    opacity:0;transform:translateY(-10px) scale(.98);
    transition:opacity .3s cubic-bezier(.2,.8,.2,1),transform .36s cubic-bezier(.2,.8,.2,1);}
  #hud.show{opacity:1;transform:none;}
  .card{border-radius:18px;padding:14px 16px 13px;
    background:rgba(28,28,32,.84);border:1px solid rgba(255,255,255,.14);
    box-shadow:0 14px 40px rgba(0,0,0,.45),inset 0 1px 0 rgba(255,255,255,.06);
    -webkit-backdrop-filter:blur(24px) saturate(1.4);backdrop-filter:blur(24px) saturate(1.4);}
  .card.amber{border-color:rgba(244,178,62,.35);}
  .top{display:flex;align-items:center;gap:8px;margin-bottom:10px;}
  .who{font-size:10px;letter-spacing:.14em;color:var(--faint);font-weight:600;text-transform:uppercase;}
  .who:before{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;background:#fff;opacity:.6;margin-right:7px;vertical-align:1px;}
  .chip{margin-left:auto;font-size:10px;letter-spacing:.08em;
    text-transform:uppercase;padding:3px 9px;border-radius:99px;
    background:rgba(255,255,255,.1);color:var(--muted);font-weight:600;
    white-space:nowrap;}
  .amber .chip{background:rgba(244,178,62,.16);color:var(--amber);}
  .title{font-size:14px;font-weight:600;line-height:1.32;color:var(--ink);
    letter-spacing:-.01em;margin-bottom:10px;
    display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;
    overflow:hidden;overflow-wrap:break-word;}
  .step{display:flex;align-items:flex-start;gap:10px;padding:3px 0;}
  .ic{width:13px;height:13px;flex:none;margin-top:2px;position:relative;
    display:flex;align-items:center;justify-content:center;}
  .done .ic:before{content:"";width:7px;height:4px;border-left:1.6px solid var(--faint);
    border-bottom:1.6px solid var(--faint);transform:rotate(-45deg) translate(1px,-1px);}
  .done .lb{color:var(--muted);font-weight:400;}
  .cur .ic:before{content:"";width:7px;height:7px;border-radius:50%;
    background:var(--blue);box-shadow:0 0 8px rgba(41,151,255,.6);animation:pulse 1.5s ease-in-out infinite;}
  @keyframes pulse{0%,100%{opacity:1;transform:scale(1)}
    50%{opacity:.45;transform:scale(.72)}}
  .cur .lb{color:var(--ink);font-weight:500;}
  .lb{font-size:12.5px;line-height:1.4;overflow:hidden;text-overflow:ellipsis;
    white-space:nowrap;flex:1;}
  .earlier{font-size:11px;color:var(--faint);padding:2px 0 4px 23px;}
  h3{font-size:10px;letter-spacing:.14em;text-transform:uppercase;
    color:var(--amber);font-weight:600;margin:0 0 8px;}
  .act{display:flex;align-items:flex-start;gap:10px;padding:7px 0;
    border-top:1px solid var(--line);}
  .act:first-of-type{border-top:0;padding-top:0;}
  .n{width:18px;height:18px;border-radius:6px;background:rgba(244,178,62,.16);
    color:var(--amber);font-size:10.5px;font-weight:700;display:flex;
    align-items:center;justify-content:center;flex:none;margin-top:1px;}
  .al{flex:1;font-size:12.5px;color:var(--ink);line-height:1.35;}
  .am{font-size:11px;color:var(--muted);margin-top:1px;}
  @media (prefers-reduced-motion:reduce){.cur .ic:before{animation:none}}
</style></head><body>
<div id="hud">
  <div class="card" id="cur">
    <div class="top"><span class="who">NEO</span>
      <span class="chip" id="chip">WORKING</span></div>
    <div class="title" id="title"></div>
    <div id="steps"></div>
  </div>
  <div class="card amber" id="next" style="display:none">
    <h3>Waiting on you</h3>
    <div id="acts"></div>
  </div>
</div>
<script>
  function theme(){
    var h=new Date().getHours();
    document.body.className = (h>=7 && h<19) ? "day" : "";
  }
  theme(); setInterval(theme, 60000);
  var hud=document.getElementById('hud');
  window.render=function(j){
    var s=JSON.parse(j);
    var chip=(s.chip||'working').toUpperCase();
    if(s.eta) chip += ' \u00b7 ' + s.eta;
    document.getElementById('chip').textContent=chip;
    document.getElementById('title').textContent=s.title||'';
    // The step list. Only the last few are shown in full; anything older
    // collapses to one muted line so a long job does not scroll its own
    // history past them. Consecutive duplicates are already merged in Python.
    var L=document.getElementById('steps'); L.innerHTML='';
    var all=(s.lines||[]), SHOW=4;
    var lines=all.slice(-SHOW);
    if(all.length>SHOW){
      var e=document.createElement('div'); e.className='earlier';
      e.textContent=(all.length-SHOW)+' earlier step'+((all.length-SHOW)>1?'s':'');
      L.appendChild(e);
    }
    lines.forEach(function(t,i){
      var live = s.busy && i===lines.length-1;
      var d=document.createElement('div');
      d.className='step '+(live?'cur':'done');
      var ic=document.createElement('div'); ic.className='ic';
      var lb=document.createElement('div'); lb.className='lb';
      lb.textContent=t; lb.title=t;
      d.appendChild(ic); d.appendChild(lb); L.appendChild(d);
    });
    // next actions (only ever visible during a task)
    var nx=document.getElementById('next'), A=document.getElementById('acts'); A.innerHTML='';
    if(s.actions&&s.actions.length){
      nx.style.display='block';
      s.actions.slice(0,3).forEach(function(a,i){
        var row=document.createElement('div');row.className='act';
        var n=document.createElement('div');n.className='n';n.textContent=(i+1);
        var al=document.createElement('div');al.className='al';al.textContent=a.label||'';
        if(a.meta){var am=document.createElement('div');am.className='am';
          am.textContent=a.meta;al.appendChild(am);}
        row.appendChild(n);row.appendChild(al);A.appendChild(row);});
    }else nx.style.display='none';
    if(s.visible)hud.classList.add('show');else hud.classList.remove('show');
  };
</script></body></html>"""


class HUDState:
    """Thread-safe store the pusher syncs to the webview."""
    def __init__(self):
        self._lock = threading.Lock()
        self.data = {"visible": False, "chip": "active", "title": "", "lines": [],
                     "progress": None, "eta": "", "busy": False, "actions": []}
        self.dirty = True

    def update(self, **kw):
        with self._lock:
            self.data.update(kw)
            self.dirty = True

    def snapshot(self):
        with self._lock:
            self.dirty = False
            return dict(self.data)


def render_html():
    return _HTML


# --------------------------------------------------------------------------- #
# public API used by neo.py (all no-ops if the panel failed to build)
# --------------------------------------------------------------------------- #
_state = HUDState()


_has_activity = {"v": False}


def _visible():
    # ONLY while Neo is actively doing a task. Pending nudges show as a
    # secondary panel WHILE a task runs, but never keep the HUD up on their
    # own — the user wants it gone the moment nothing's in progress.
    return _has_activity["v"]


def set_activity(chip, title, lines=None, progress=None, eta="", busy=True):
    _has_activity["v"] = True
    _state.update(visible=True, chip=chip, title=title, lines=lines or [],
                  progress=progress, eta=eta, busy=busy)


def end_activity():
    """Neo finished what it was doing — drop the top card, but keep the HUD up
    if there are still things waiting on the user."""
    _has_activity["v"] = False
    _state.update(title="", lines=[], progress=None, eta="", busy=False,
                  visible=_visible())


def set_actions(actions):
    """actions: [{'label':..., 'meta':...}]. The amber 'waiting on you' panel."""
    _state.update(actions=actions or [])
    _state.update(visible=_visible())


def clear():
    _has_activity["v"] = False
    _state.update(visible=False, busy=False, title="", lines=[], progress=None,
                  eta="", actions=[])


# --------------------------------------------------------------------------- #
# the panel (macOS; same recipe as overlay.py). Build on the MAIN thread.
# --------------------------------------------------------------------------- #
class HUD:
    def __init__(self):
        from Cocoa import (
            NSPanel, NSColor, NSScreen, NSMakeRect, NSBackingStoreBuffered, NSTimer,
            NSObject, NSWindowStyleMaskBorderless, NSWindowStyleMaskNonactivatingPanel,
            NSWindowCollectionBehaviorCanJoinAllSpaces, NSWindowCollectionBehaviorStationary,
            NSWindowCollectionBehaviorFullScreenAuxiliary,
        )
        from Quartz import kCGMaximumWindowLevel
        import WebKit

        W, H = 370, 360
        screen = NSScreen.mainScreen().frame()
        x = screen.origin.x + screen.size.width - W - 6
        y = screen.origin.y + screen.size.height - H - 8
        rect = NSMakeRect(x, y, W, H)
        mask = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, mask, NSBackingStoreBuffered, False)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(False)
        panel.setIgnoresMouseEvents_(True)
        panel.setLevel_(kCGMaximumWindowLevel)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorFullScreenAuxiliary)

        cfg = WebKit.WKWebViewConfiguration.alloc().init()
        web = WebKit.WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, W, H), cfg)
        try:
            web.setValue_forKey_(False, "drawsBackground")
        except Exception:
            pass
        web.loadHTMLString_baseURL_(_HTML, None)
        panel.setContentView_(web)
        panel.orderFrontRegardless()
        self.panel, self.web = panel, web

        # NOTE: class name must be UNIQUE across the whole process — overlay.py
        # also has a pusher, and PyObjC registers Obj-C classes by name globally
        # (a duplicate name silently disabled the whole HUD before this).
        class _HudPusher(NSObject):
            def tick_(self, timer):
                try:
                    if _state.dirty:
                        js = "window.render && render(%s)" % json.dumps(json.dumps(_state.snapshot()))
                        self.web.evaluateJavaScript_completionHandler_(js, None)
                except Exception as e:
                    print(f"[hud] push: {e}")
        p = _HudPusher.alloc().init()
        p.web = web
        self._pusher = p
        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.2, p, "tick:", None, True)


if __name__ == "__main__":
    import sys
    if "--demo" in sys.argv:
        # open the panel with sample data so you can SEE it on your Mac
        from PyObjCTools import AppHelper
        from Cocoa import NSApplication
        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(1)
        HUD()
        set_activity("working", "Claude's building the signups feature",
                     lines=["Reading the schema", "Wiring the query",
                            "Testing it"], busy=True, eta="about 4 minutes")
        set_actions([{"label": "Jordan replied — answer them", "meta": "waiting 2 days"},
                     {"label": "Send the 10 counselor pitches", "meta": "drafts ready"},
                     {"label": "Two chem practice sets", "meta": "final Thursday"}])
        print("HUD demo open. Ctrl-C to quit.")
        AppHelper.runEventLoop()
    else:
        # write a standalone preview with baked data (for opening in a browser)
        import re
        demo = _HTML.replace("</body>",
            "<script>render(JSON.stringify({visible:true,chip:'working',"
            "title:\"Claude's building the signups feature\","
            "lines:['Reading the schema','Wiring the query','Testing it'],"
            "busy:true,eta:'about 4 minutes',actions:["
            "{label:'Jordan replied — answer them',meta:'waiting 2 days'},"
            "{label:'Send the 10 counselor pitches',meta:'drafts ready'},"
            "{label:'Two chem practice sets',meta:'final Thursday'}]}));"
            "document.getElementById('hud').classList.add('show');</script></body>")
        with open("hud_preview.html", "w") as f:
            f.write(demo)
        print("wrote hud_preview.html")
