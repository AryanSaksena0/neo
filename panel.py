"""
panel.py — the answer panel. One component, bottom-right, gone when they're done.

The small counterpart to deck.py. A presentation is a big deliberate thing that
takes over the screen and gets narrated; this is a glance — the shape of the
answer Neo is already speaking, sitting quietly beside it.

    import panel, visuals
    panel.show(visuals.build("Heap sort", "build the heap | then pop the root"))
    panel.clear()

See it without touching Neo:   python panel.py --demo

DESIGN
The shell is Neo's, the interior is shadcn's.

the user asked for 21st.dev / shadcn components. The honest catch is that their
catalogue is APP furniture — buttons, dialogs, selects, forms — and what an
explainer needs is a process, a comparison, a stat, a timeline. Those are not in
it. So what is borrowed is the part that actually transfers: the token system
(its real slate values), the 0.5rem radius, the type scale, the muted-foreground
hierarchy that makes a shadcn card readable at a glance, and the anatomy of the
few components that do map — card, badge, separator, progress, table.

Wrapped, though, in the AURA glass that hud.py and overlay.py already
established, because a flat opaque shadcn card dropped on top of whatever the user
is looking at reads as a foreign object. Neo's windows are glass, blue-edged and
day/night aware, and this is one of Neo's windows.

CLICK-THROUGH, ALWAYS
setIgnoresMouseEvents_(True), like every other overlay here. A panel that can
eat a click is a panel that can cost them the thing they were doing.
"""

import json
import threading
import time

_HTML = r"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
  /* ---- tokens ------------------------------------------------------------
     The interior values are shadcn's own slate scale, converted from its HSL
     custom properties so no Tailwind build has to run at boot. The shell
     values are Neo's, from hud.py, so this panel is recognisably the same
     product as the status HUD sitting above it. */
  :root{
    /* Neo's glass shell */
    --bg1:rgba(17,24,33,.95); --bg2:rgba(11,16,23,.94);
    --edge:rgba(120,170,225,.22); --glow:rgba(70,130,200,.10);
    --cyan:#6aa9ff; --amber:#f4b23e;
    /* shadcn slate, dark */
    --fg:#f8fafc;              /* foreground        210 40% 98%   */
    --muted-fg:#94a3b8;        /* muted-foreground  215 20% 65%   */
    --border:rgba(148,163,184,.16);
    --muted:rgba(148,163,184,.10);
    --radius:8px;              /* shadcn --radius: 0.5rem          */
    --radius-sm:5px;
  }
  body.day{
    --bg1:rgba(250,252,255,.96); --bg2:rgba(240,245,251,.95);
    --edge:rgba(90,140,195,.28); --glow:rgba(120,170,220,.12);
    --cyan:#2563eb; --amber:#b45309;
    --fg:#0f172a;              /* 222 47% 11% */
    --muted-fg:#64748b;        /* 215 16% 47% */
    --border:rgba(15,23,42,.12);
    --muted:rgba(15,23,42,.05);
  }
  html,body{margin:0;height:100%;background:transparent;overflow:hidden;
    font-family:-apple-system,"SF Pro Text",Helvetica,sans-serif;
    -webkit-font-smoothing:antialiased;color:var(--fg);}
  #root{position:absolute;inset:0;display:flex;align-items:flex-end;
    justify-content:flex-end;padding:10px;}

  .card{
    width:100%;max-height:100%;box-sizing:border-box;
    border-radius:15px;padding:15px 17px 15px;
    background:linear-gradient(180deg,var(--bg1),var(--bg2));
    border:1px solid var(--edge);
    box-shadow:0 14px 38px rgba(0,0,0,.38), 0 0 24px var(--glow),
      0 0 0 1px rgba(255,255,255,.04) inset;
    -webkit-backdrop-filter:blur(13px);backdrop-filter:blur(13px);
    overflow:hidden;display:flex;flex-direction:column;gap:11px;
    opacity:0;transform:translateY(9px);
    animation:rise .26s cubic-bezier(.16,1,.3,1) forwards;}
  .card.out{animation:sink .18s ease forwards;}
  @keyframes rise{to{opacity:1;transform:none}}
  @keyframes sink{to{opacity:0;transform:translateY(6px)}}
  @media (prefers-reduced-motion:reduce){
    .card,.card.out{animation:none;opacity:1;transform:none}
    .bar i{transition:none}
  }

  /* ---- header ---- */
  .hd{display:flex;flex-direction:column;gap:3px;}
  .kind{font-size:9.5px;font-weight:700;letter-spacing:.10em;
    text-transform:uppercase;color:var(--cyan);}
  h1{margin:0;font-size:15.5px;line-height:1.25;font-weight:600;
    letter-spacing:-.01em;}
  .sub{margin:0;font-size:11.5px;line-height:1.45;color:var(--muted-fg);}
  .rule{height:1px;background:var(--border);}

  /* ---- shared bits: shadcn badge + muted row ---- */
  .badge{display:inline-block;font-size:9.5px;font-weight:600;
    letter-spacing:.03em;padding:2px 7px;border-radius:99px;
    background:var(--muted);color:var(--muted-fg);
    border:1px solid var(--border);white-space:nowrap;}
  .lbl{font-size:12.5px;font-weight:600;line-height:1.3;}
  .det{font-size:11.5px;line-height:1.45;color:var(--muted-fg);}
  .body{display:flex;flex-direction:column;gap:9px;overflow:hidden;}

  /* ---- steps: a numbered rail ---- */
  .steps{display:flex;flex-direction:column;gap:0;}
  .step{display:grid;grid-template-columns:20px 1fr;gap:10px;
    padding:0 0 11px;position:relative;}
  .step:last-child{padding-bottom:0;}
  .step:not(:last-child):before{content:"";position:absolute;left:9.5px;
    top:21px;bottom:1px;width:1px;background:var(--border);}
  .step .n{width:20px;height:20px;border-radius:50%;display:flex;
    align-items:center;justify-content:center;font-size:10px;font-weight:700;
    background:var(--muted);color:var(--fg);border:1px solid var(--border);
    position:relative;z-index:1;}
  .step .tx{display:flex;flex-direction:column;gap:2px;padding-top:1px;}

  /* ---- compare: 2-4 columns ---- */
  .cmp{display:grid;gap:8px;}
  .cmp .col{background:var(--muted);border:1px solid var(--border);
    border-radius:var(--radius);padding:9px 10px;display:flex;
    flex-direction:column;gap:4px;min-width:0;}
  .cmp .col .lbl{display:flex;align-items:center;gap:6px;}

  /* ---- stat: headline numbers ---- */
  .stats{display:grid;gap:9px;}
  .stat{display:flex;flex-direction:column;gap:1px;}
  .stat .v{font-size:24px;line-height:1.05;font-weight:650;
    letter-spacing:-.02em;font-variant-numeric:tabular-nums;}
  .stat .k{font-size:11px;font-weight:600;color:var(--fg);}

  /* ---- timeline: a dotted rail ---- */
  .tl{display:flex;flex-direction:column;}
  .ev{display:grid;grid-template-columns:11px 1fr;gap:10px;
    padding-bottom:11px;position:relative;}
  .ev:last-child{padding-bottom:0;}
  .ev:not(:last-child):before{content:"";position:absolute;left:5px;top:12px;
    bottom:0;width:1px;background:var(--border);}
  .ev .dot{width:7px;height:7px;border-radius:50%;background:var(--cyan);
    margin:5px 0 0 2px;position:relative;z-index:1;
    box-shadow:0 0 0 3px var(--muted);}

  /* ---- breakdown: shadcn progress, one per row ---- */
  .brk{display:flex;flex-direction:column;gap:9px;}
  .row{display:flex;flex-direction:column;gap:4px;}
  .row .top{display:flex;justify-content:space-between;align-items:baseline;
    gap:10px;}
  .row .top b{font-size:12px;font-weight:600;}
  .row .top span{font-size:11px;color:var(--muted-fg);
    font-variant-numeric:tabular-nums;white-space:nowrap;}
  /* One colour. The bar's LENGTH is the information; a blue-to-orange ramp
     across every bar adds a second visual variable that means nothing, and
     made four rows look like four different kinds of thing. */
  .bar{height:5px;border-radius:99px;background:var(--muted);overflow:hidden;}
  .bar i{display:block;height:100%;border-radius:99px;background:var(--cyan);
    width:0;transition:width .5s cubic-bezier(.16,1,.3,1);}

  /* ---- hierarchy: indent with guides ---- */
  .hier{display:flex;flex-direction:column;gap:7px;}
  .node{display:flex;flex-direction:column;gap:1px;
    border-left:1px solid var(--border);padding-left:9px;}
  .node.l0{border-left-color:transparent;padding-left:0;}

  /* ---- table: shadcn table, trimmed ---- */
  .tbl{width:100%;border-collapse:collapse;font-size:11.5px;}
  .tbl th{text-align:left;font-size:9.5px;font-weight:600;
    letter-spacing:.07em;text-transform:uppercase;color:var(--muted-fg);
    padding:0 8px 6px 0;border-bottom:1px solid var(--border);}
  .tbl td{padding:7px 8px 7px 0;border-bottom:1px solid var(--border);
    vertical-align:top;}
  .tbl tr:last-child td{border-bottom:none;}
  .tbl td.v{font-variant-numeric:tabular-nums;white-space:nowrap;
    color:var(--fg);font-weight:600;}
  .tbl td.d{color:var(--muted-fg);}

  /* ---- define: term + properties ----
     This is the DEFAULT now — whatever a panel falls back to lands here — so
     it has to hold four rows without looking like an unformatted list. A hair
     rule and a small dot give it rhythm; deliberately not numbers, because
     numbering is the claim `steps` makes and this component exists precisely
     for content that has no order. */
  .def{display:flex;flex-direction:column;}
  .prop{display:grid;grid-template-columns:7px 1fr;gap:9px;
    padding:8px 0 8px;border-top:1px solid var(--border);align-items:start;}
  .prop:first-child{border-top:none;padding-top:2px;}
  .prop .pip{width:5px;height:5px;border-radius:50%;margin-top:6px;
    background:var(--cyan);opacity:.55;}
  .prop .tx{display:flex;flex-direction:column;gap:2px;min-width:0;}
</style></head><body><div id="root"></div>
<script>
  // Everything below renders MODEL OUTPUT. It is escaped on the way in, with
  // no exceptions: a panel is not worth a script-injection hole, and the text
  // arrives from a model that has been reading web pages.
  function esc(s){
    return String(s==null?"":s).replace(/[&<>"']/g, function(c){
      return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
    });
  }
  function det(i){ return i.detail ? '<div class="det">'+esc(i.detail)+'</div>' : ''; }
  function lbl(i){ return '<div class="lbl">'+esc(i.label)+'</div>'; }

  var KIND_NAME = {steps:"Process", compare:"Comparison", stat:"Numbers",
                   timeline:"Timeline", breakdown:"Breakdown",
                   hierarchy:"Structure", table:"Detail", define:"Definition"};

  var DRAW = {
    steps: function(it){
      return '<div class="steps">' + it.map(function(i,n){
        return '<div class="step"><div class="n">'+(n+1)+'</div>'+
               '<div class="tx">'+lbl(i)+det(i)+'</div></div>';
      }).join('') + '</div>';
    },
    compare: function(it){
      var cols = Math.min(it.length, 2);           // 2 across reads best at
      return '<div class="cmp" style="grid-template-columns:repeat('+cols+
             ',minmax(0,1fr))">' + it.map(function(i){  // this width
        return '<div class="col"><div class="lbl">'+esc(i.label)+
               (i.tag?' <span class="badge">'+esc(i.tag)+'</span>':'')+
               '</div>'+det(i)+'</div>';
      }).join('') + '</div>';
    },
    stat: function(it){
      var cols = it.length >= 3 ? 2 : it.length;
      return '<div class="stats" style="grid-template-columns:repeat('+
             Math.max(1,cols)+',minmax(0,1fr))">' + it.map(function(i){
        return '<div class="stat"><div class="v">'+esc(i.value||i.label)+
               '</div><div class="k">'+esc(i.value?i.label:'')+'</div>'+
               det(i)+'</div>';
      }).join('') + '</div>';
    },
    timeline: function(it){
      return '<div class="tl">' + it.map(function(i){
        return '<div class="ev"><div class="dot"></div>'+
               '<div class="tx">'+lbl(i)+det(i)+'</div></div>';
      }).join('') + '</div>';
    },
    breakdown: function(it){
      return '<div class="brk">' + it.map(function(i){
        // No number, no bar. An empty track is indistinguishable from one
        // that failed to render, and visuals.py should never send this —
        // but a panel that cannot be wrong by construction is better than
        // one that relies on the other file staying correct.
        var hasBar = i.pct !== undefined && i.pct !== null && i.value;
        return '<div class="row"><div class="top"><b>'+esc(i.label)+
               '</b><span>'+esc(i.value||'')+'</span></div>'+
               (hasBar ? '<div class="bar"><i data-pct="'+i.pct+'"></i></div>'
                       : '')+
               det(i)+'</div>';
      }).join('') + '</div>';
    },
    hierarchy: function(it){
      return '<div class="hier">' + it.map(function(i){
        var l = Math.min(3, i.level||0);
        return '<div class="node l'+l+'" style="margin-left:'+(l*13)+'px">'+
               lbl(i)+det(i)+'</div>';
      }).join('') + '</div>';
    },
    table: function(it){
      var anyVal = it.some(function(i){ return i.value; });
      var head = '<tr><th>'+esc(it.headLabel||'Item')+'</th>'+
                 (anyVal?'<th>Value</th>':'')+'<th>Detail</th></tr>';
      return '<table class="tbl"><thead>'+head+'</thead><tbody>'+
        it.map(function(i){
          return '<tr><td><b>'+esc(i.label)+'</b></td>'+
                 (anyVal?'<td class="v">'+esc(i.value||'—')+'</td>':'')+
                 '<td class="d">'+esc(i.detail||'')+'</td></tr>';
        }).join('') + '</tbody></table>';
    },
    define: function(it){
      return '<div class="def">' + it.map(function(i){
        return '<div class="prop"><div class="pip"></div>'+
               '<div class="tx">'+lbl(i)+det(i)+'</div></div>';
      }).join('') + '</div>';
    }
  };

  var current = null;
  function render(payload){
    var s = typeof payload === "string" ? JSON.parse(payload) : payload;
    var root = document.getElementById("root");
    // Day/night, same rule as the HUD: the panel sits over whatever they are
    // looking at, and a dark card on a white document at noon is a hole.
    var h = new Date().getHours();
    document.body.className = (h >= 7 && h < 19) ? "day" : "";

    if(!s || !s.kind){
      var old = root.querySelector(".card");
      if(old){ old.className = "card out"; setTimeout(function(){
        if(root.firstChild === old) root.innerHTML = ""; }, 200); }
      else root.innerHTML = "";
      current = null;
      return;
    }
    var it = s.items || [];
    var draw = DRAW[s.kind] || DRAW.define;
    root.innerHTML =
      '<div class="card"><div class="hd">'+
        '<div class="kind">'+esc(KIND_NAME[s.kind]||s.kind)+'</div>'+
        '<h1>'+esc(s.title)+'</h1>'+
        (s.subtitle?'<p class="sub">'+esc(s.subtitle)+'</p>':'')+
      '</div><div class="rule"></div>'+
      '<div class="body">'+draw(it)+'</div></div>';
    current = s.kind;

    // Bars animate from zero on the next frame, so the growth is visible
    // rather than already finished by the time the panel fades in.
    requestAnimationFrame(function(){
      var bars = root.querySelectorAll(".bar i");
      for(var i=0;i<bars.length;i++){
        bars[i].style.width = (bars[i].getAttribute("data-pct")||0) + "%";
      }
    });
  }
  window.render = render;
</script></body></html>"""


class _State:
    def __init__(self):
        self.spec = None
        self.dirty = False
        self.until = 0.0
        self._lock = threading.Lock()

    def set(self, spec, seconds):
        with self._lock:
            self.spec = spec
            self.dirty = True
            self.until = (time.time() + seconds) if spec else 0.0

    def snapshot(self):
        with self._lock:
            self.dirty = False
            return self.spec

    def expired(self, now=None):
        with self._lock:
            return bool(self.spec) and self.until and (now or time.time()) > self.until


_state = _State()

# How long a panel stays up on its own. Long enough to finish reading a
# comparison, short enough that a forgotten one isn't still there at dinner.
# It also clears on the next question and on "get that off my screen".
HOLD_S = 90.0


def show(spec, seconds=HOLD_S):
    """Put a component on screen. `spec` comes from visuals.build()."""
    if not spec:
        return False
    _state.set(spec, seconds)
    return True


def clear():
    _state.set(None, 0)


def showing():
    return _state.spec is not None


class Panel:
    """The window. Built on the main thread from neo.py, exactly like the HUD —
    Cocoa windows cannot be created anywhere else."""

    def __init__(self):
        from Cocoa import (
            NSPanel, NSColor, NSScreen, NSMakeRect, NSBackingStoreBuffered,
            NSTimer, NSObject, NSWindowStyleMaskBorderless,
            NSWindowStyleMaskNonactivatingPanel,
            NSWindowCollectionBehaviorCanJoinAllSpaces,
            NSWindowCollectionBehaviorStationary,
            NSWindowCollectionBehaviorFullScreenAuxiliary,
        )
        from Quartz import kCGMaximumWindowLevel
        import WebKit

        W, H = 430, 500
        screen = NSScreen.mainScreen().frame()
        # Bottom-right. The HUD owns the top-right corner and the two must
        # never overlap — a status card sitting on top of an explainer is
        # worse than either of them alone.
        x = screen.origin.x + screen.size.width - W - 6
        y = screen.origin.y + 26
        rect = NSMakeRect(x, y, W, H)
        mask = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        p = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, mask, NSBackingStoreBuffered, False)
        p.setOpaque_(False)
        p.setBackgroundColor_(NSColor.clearColor())
        p.setHasShadow_(False)
        p.setIgnoresMouseEvents_(True)      # never eats a click
        p.setLevel_(kCGMaximumWindowLevel)
        p.setCollectionBehavior_(
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
        p.setContentView_(web)
        p.orderFrontRegardless()
        self.panel, self.web = p, web

        # The class name must be unique across the whole process: PyObjC
        # registers Obj-C classes globally by name, and a duplicate silently
        # disabled the entire HUD once already. Hence _PanelPusher, not
        # _Pusher — overlay.py and hud.py have taken the obvious names.
        class _PanelPusher(NSObject):
            def tick_(self, timer):
                try:
                    if _state.expired():
                        _state.set(None, 0)
                    if _state.dirty:
                        js = ("window.render && render(%s)"
                              % json.dumps(json.dumps(_state.snapshot())))
                        self.web.evaluateJavaScript_completionHandler_(js, None)
                except Exception as e:
                    print(f"[panel] push: {e}")

        pusher = _PanelPusher.alloc().init()
        pusher.web = web
        self._pusher = pusher
        self._timer = (
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                0.2, pusher, "tick:", None, True))


if __name__ == "__main__":
    import sys
    if "--demo" in sys.argv:
        import visuals
        from PyObjCTools import AppHelper
        from Cocoa import NSApplication
        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(1)
        Panel()
        demos = [
            visuals.build(
                "Roth vs traditional IRA",
                "Roth | Taxed now, tax-free in retirement | \n"
                "Traditional | Deducted now, taxed on withdrawal |",
                kind="compare",
                subtitle="Which one wins depends on your tax rate later."),
            visuals.build(
                "How a heap sort runs",
                "Build the heap | Sift every parent down until the array obeys "
                "the heap property\n"
                "Swap the root | The largest element moves to the end\n"
                "Shrink and repeat | Re-heapify what is left, n-1 times",
                kind="steps"),
            visuals.build(
                "Where the signups came from",
                "Ambassadors | 41%\nOrganic search | 28%\nTikTok | 19%\n"
                "Direct | 12%", kind="breakdown"),
        ]
        i = {"n": 0}

        def cycle():
            show(demos[i["n"] % len(demos)])
            i["n"] += 1
            threading.Timer(4.0, cycle).start()
        cycle()
        AppHelper.runEventLoop()
    else:
        print(__doc__)
