"""
spotlight.py — Neo draws on your actual screen.

A full-screen, click-through, always-on-top window that puts a ring around the
thing Neo is talking about. It is its OWN PROCESS, for the same reason canvas
and deck are: a GUI run loop in Neo's process is a run loop that can hang Neo,
and this one has to survive a whole multi-step walkthrough.

It reads a small JSON file and redraws whenever that file changes, so one
window follows an entire walkthrough — ring the Settings button, wait for the
click, ring the next thing — without ever being torn down and rebuilt. A window
that flickered between every step would be worse than no window.

Everything is positioned in PERCENTAGES of the screen, never pixels. The
screenshot Neo reasons about is in device pixels (2x on a retina display) and
the window is in points; keeping the wire format proportional means neither
side has to know the other's scale factor.

    python spotlight.py /path/to/spec.json

Spec:
    {"shapes": [{"kind": "ring"|"box"|"underline"|"band",
                 "x": 0-100, "y": 0-100,      # centre
                 "w": 0-100, "h": 0-100,      # size, for box/underline
                 "label": "Settings", "step": "2 of 4",
                 "label_x": 0-100, "label_y": 0-100}],   # optional
     "dim": true, "done": false}

Delete the file, or set done, and the window closes itself.
"""
import json
import os
import sys

POLL_MS = 0.25


def _html(initial=None):
    """The page, with the FIRST spec baked in.

    It used to load empty and wait for the watcher to push a spec in. The
    watcher fires a quarter of a second after launch, the WebView has not
    finished loading by then, and the evaluateJavaScript call lands on a page
    with no paint() on it — silently. Since the file's mtime only changes once,
    nothing ever tried again, and the window sat there perfectly transparent
    and completely empty. Baking the first state into the document means the
    window is correct the instant it appears, and the watcher only ever handles
    CHANGES, which is what it was for.
    """
    boot = ("<script>window.__boot=%s;</script>" % json.dumps(initial)
            if initial else "")
    return boot + r"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
  html,body{margin:0;height:100%;background:transparent;overflow:hidden;
    font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif}
  #dim{position:fixed;inset:0;background:#05070A;opacity:0;
    transition:opacity .45s ease;pointer-events:none}
  .mark{position:fixed;transform:translate(-50%,-50%);pointer-events:none;
    transition:left .42s cubic-bezier(.3,.9,.3,1),top .42s cubic-bezier(.3,.9,.3,1),
               width .42s ease,height .42s ease,opacity .3s ease}
  .ring{border:3px solid #F0A93C;border-radius:14px;
    box-shadow:0 0 0 3px rgba(5,7,10,.55),0 0 26px rgba(240,169,60,.75),
               inset 0 0 22px rgba(240,169,60,.28)}
  .ring::after{content:"";position:absolute;inset:-9px;border-radius:20px;
    border:2px solid rgba(240,169,60,.5);animation:pulse 1.9s ease-out infinite}
  @keyframes pulse{from{transform:scale(.96);opacity:.85}
                   to{transform:scale(1.14);opacity:0}}
  .underline{border-bottom:4px solid #F0A93C;border-radius:2px;
    box-shadow:0 3px 16px rgba(240,169,60,.6)}
  /* A highlighter pen, not a sticker: the words underneath have to stay
     readable, which is the whole difference between highlighting a sentence
     and hiding it.
     mix-blend-mode was the obvious way to do that and it CANNOT work here.
     Blending composites within a window's own stacking context; this window is
     transparent and the text is in a different application, so there is
     nothing behind it to blend with and multiply just paints a solid olive
     block over the sentence. (Screenshot taken, band drawn, sentence gone.)
     Plain alpha is composited by the window server against whatever is
     actually on screen, so that is what this uses — a low-alpha wash with a
     defined edge, which reads as a highlight and leaves the text legible. */
  /* The placement is already right — this is only about how the stroke
     LOOKS. What made it read as a rectangle rather than a highlight was the
     hard 1px border all the way round and the solid bar butted against the
     bottom edge: two crisp outlines say "box", and a real marker has neither.
     So: no border, a soft vertical gradient the way ink pools toward the
     bottom of a stroke, ends that taper off instead of stopping dead, and a
     left-to-right wipe on arrival so it reads as being DRAWN over the words
     rather than pasted on top of them. */
  .band{border-radius:7px;
    background:linear-gradient(180deg,
      rgba(255,224,96,.09) 0%, rgba(255,215,66,.25) 36%,
      rgba(255,204,48,.30) 74%, rgba(255,196,40,.15) 100%);
    box-shadow:0 0 0 1px rgba(255,214,64,.16),
               0 1px 12px rgba(255,201,46,.24);
    animation:inkin .34s cubic-bezier(.22,1,.36,1) both}
  .band::after{content:"";position:absolute;left:2.5%;right:2.5%;bottom:1px;
    height:2px;border-radius:2px;
    background:linear-gradient(90deg,rgba(255,190,32,0),
      rgba(255,188,30,.55) 10%,rgba(255,188,30,.55) 90%,
      rgba(255,190,32,0))}
  @keyframes inkin{from{clip-path:inset(0 100% 0 0);opacity:.35}
                   to{clip-path:inset(0 0 0 0);opacity:1}}
  .tag{position:fixed;transform:translate(-50%,0);pointer-events:none;
    background:#0B0E13;color:#F2EFE9;border:1px solid #33383F;
    border-radius:9px;padding:8px 13px;font-size:14px;font-weight:600;
    letter-spacing:-.01em;white-space:nowrap;
    box-shadow:0 10px 30px rgba(0,0,0,.6);transition:left .42s ease,top .42s ease}
  .tag em{font-style:normal;color:#F0A93C;font-weight:600;margin-right:9px;
    font-size:12px;letter-spacing:.08em;text-transform:uppercase}
  @media (prefers-reduced-motion:reduce){.mark,.tag{transition:none}
    .ring::after{animation:none}
    .band{animation:none;clip-path:none;opacity:1}}
</style></head><body>
<div id="dim"></div><div id="layer"></div>
<script>
const layer=document.getElementById('layer'), dim=document.getElementById('dim');
window.paint=function(spec){
  dim.style.opacity = spec.dim ? .34 : 0;
  layer.innerHTML='';
  for(const s of (spec.shapes||[])){
    const w=(s.w||6), h=(s.h||4);
    const el=document.createElement('div');
    el.className='mark '+(s.kind==='underline'?'underline':
                          (s.kind==='band'?'band':'ring'));
    el.style.left=s.x+'%'; el.style.top=s.y+'%';
    el.style.width=w+'vw'; el.style.height=h+'vh';
    layer.appendChild(el);
    if(s.label){
      const t=document.createElement('div');
      t.className='tag';
      t.innerHTML=(s.step?'<em>'+s.step+'</em>':'')+s.label;
      // A label position worked out against every text box on screen beats
      // "just below", which is how a tag ends up sitting on the field they were
      // about to type into. Falls back to below only when none was supplied.
      t.style.left=(s.label_x!==undefined?s.label_x:s.x)+'%';
      t.style.top=(s.label_y!==undefined? s.label_y+'%'
                   : 'calc('+s.y+'% + '+(h/2)+'vh + 14px)');
      layer.appendChild(t);
    }
  }
};
if (window.__boot) paint(window.__boot);
</script></body></html>"""


def run(spec_path):
    from Cocoa import (NSApplication, NSPanel, NSColor, NSScreen, NSMakeRect,
                       NSBackingStoreBuffered, NSTimer, NSObject,
                       NSWindowStyleMaskBorderless,
                       NSWindowStyleMaskNonactivatingPanel,
                       NSWindowCollectionBehaviorCanJoinAllSpaces,
                       NSWindowCollectionBehaviorStationary,
                       NSWindowCollectionBehaviorFullScreenAuxiliary)
    from PyObjCTools import AppHelper
    from Quartz import kCGMaximumWindowLevel
    import WebKit

    app = NSApplication.sharedApplication()
    frame = NSScreen.mainScreen().frame()
    panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        frame,
        NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
        NSBackingStoreBuffered, False)
    panel.setOpaque_(False)
    panel.setBackgroundColor_(NSColor.clearColor())
    panel.setHasShadow_(False)
    # CLICK-THROUGH IS THE WHOLE POINT. Neo is pointing at a button the user then
    # has to press; a window that swallowed that click would make the feature
    # actively worse than not having it.
    panel.setIgnoresMouseEvents_(True)
    panel.setLevel_(kCGMaximumWindowLevel)
    panel.setCollectionBehavior_(
        NSWindowCollectionBehaviorCanJoinAllSpaces
        | NSWindowCollectionBehaviorStationary
        | NSWindowCollectionBehaviorFullScreenAuxiliary)

    config = WebKit.WKWebViewConfiguration.alloc().init()
    web = WebKit.WKWebView.alloc().initWithFrame_configuration_(
        NSMakeRect(0, 0, frame.size.width, frame.size.height), config)
    try:
        web.setValue_forKey_(False, "drawsBackground")
    except Exception:
        pass
    try:
        with open(spec_path, encoding="utf-8") as f:
            first = json.load(f)
    except Exception:
        first = None
    web.loadHTMLString_baseURL_(_html(first), None)
    panel.setContentView_(web)
    panel.orderFrontRegardless()

    class _Watch(NSObject):
        def tick_(self, timer):
            try:
                mtime = os.path.getmtime(spec_path)
            except OSError:
                AppHelper.stopEventLoop()       # file gone: we are done
                return
            if mtime == self.last:
                return
            self.last = mtime
            try:
                with open(spec_path, encoding="utf-8") as f:
                    spec = json.load(f)
            except Exception:
                return                          # half-written: try again next tick
            if spec.get("done"):
                AppHelper.stopEventLoop()
                return
            self.web.evaluateJavaScript_completionHandler_(
                "window.paint && paint(%s)" % json.dumps(spec), None)

    watcher = _Watch.alloc().init()
    watcher.web = web
    # Start from the mtime we just READ, so the watcher only reacts to real
    # changes rather than immediately re-painting what is already on screen.
    try:
        watcher.last = os.path.getmtime(spec_path)
    except OSError:
        watcher.last = None
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        POLL_MS, watcher, "tick:", None, True)
    AppHelper.runEventLoop()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python spotlight.py <spec.json>")
        sys.exit(1)
    run(sys.argv[1])
