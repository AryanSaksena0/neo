"""
notify.py — Neo's on-screen notification card.

When the sentinel spots something, a small frosted card slides in under the
overlay pill (top-right): urgency chip, one-line title, and two buttons —
"Tell me" (Neo speaks the full thing) or "Dismiss" (drops it, won't re-nag).

Same visual language as overlay.py: transparent WebKit view in a borderless
non-activating panel, so it floats over everything without stealing focus.
Unlike the overlay it DOES take clicks — but only on the card itself.

Cards queue: one at a time, highest urgency first (the sentinel pre-sorts).
Low/medium cards fade out on their own if ignored (the sentinel re-raises
them later); high-urgency cards wait longer.

Build the Notifier on the MAIN thread (like Overlay). Hand it insights from
any thread via AppHelper.callAfter(notifier.notify, insight).
"""

import json

from Cocoa import (
    NSPanel, NSColor, NSScreen, NSMakeRect, NSBackingStoreBuffered, NSObject, NSTimer,
    NSWindowStyleMaskBorderless, NSWindowStyleMaskNonactivatingPanel,
    NSWindowCollectionBehaviorCanJoinAllSpaces, NSWindowCollectionBehaviorStationary,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
)
from Quartz import kCGMaximumWindowLevel
import WebKit

CARD_W = 340
CARD_H = 158
MARGIN_TOP = 40           # just under the menu bar; the island is top-centre now
MARGIN_RIGHT = 16

# ignored-card timeout by urgency (seconds)
TIMEOUT_S = {"low": 25, "medium": 45, "high": 150}

_HTML = r"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
  html,body{margin:0;height:100%;background:transparent;overflow:hidden;
    font-family:-apple-system,'SF Pro Text',Helvetica,sans-serif;
    -webkit-user-select:none;user-select:none;}
  #card{position:absolute;inset:6px;border-radius:18px;padding:14px 16px 12px;
    background:rgba(28,28,32,.84);border:1px solid rgba(255,255,255,.14);
    box-shadow:0 14px 40px rgba(0,0,0,.45),inset 0 1px 0 rgba(255,255,255,.06);
    -webkit-backdrop-filter:blur(24px) saturate(1.4);backdrop-filter:blur(24px) saturate(1.4);
    -webkit-font-smoothing:antialiased;
    opacity:0;transform:translateY(-10px) scale(0.98);
    transition:opacity .3s cubic-bezier(.2,.8,.2,1),transform .36s cubic-bezier(.2,.8,.2,1);box-sizing:border-box;
    display:flex;flex-direction:column;gap:9px;}
  #card.show{opacity:1;transform:none;}
  #top{display:flex;align-items:center;gap:8px;}
  #who{font-size:10px;color:#6E6E73;letter-spacing:.14em;font-weight:600;text-transform:uppercase;}
  #who:before{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;background:#fff;opacity:.6;margin-right:7px;vertical-align:1px;}
  #chip{margin-left:auto;font-size:10px;font-weight:600;letter-spacing:.08em;padding:3px 9px;
    border-radius:999px;text-transform:uppercase;}
  .low   {background:rgba(255,255,255,.1);color:#A1A1A6;}
  .medium{background:rgba(244,178,62,.16);color:#F4B23E;}
  .high  {background:rgba(48,209,88,.16);color:#30D158;}
  #title{font-size:14px;line-height:1.35;color:#F2F2F4;letter-spacing:-.01em;
    font-weight:600;display:-webkit-box;-webkit-line-clamp:2;
    -webkit-box-orient:vertical;overflow:hidden;}
  #btns{display:flex;gap:8px;margin-top:auto;}
  button{flex:1;border:0;border-radius:999px;padding:7px 0;font-size:12.5px;
    font-weight:500;cursor:pointer;font-family:inherit;
    transition:filter .15s ease, transform .1s ease;}
  button:active{transform:scale(0.97);}
  #ok{background:#2997FF;color:#fff;}
  #ok:hover{background:#4AA8FF;}
  #no{background:rgba(255,255,255,.1);color:#F2F2F4;border:1px solid rgba(255,255,255,.14);}
  #no:hover{background:rgba(255,255,255,.16);}
</style></head><body>
<div id="card">
  <div id="top"><span id="who">NEO NOTICED</span><span id="chip" class="medium">MEDIUM</span></div>
  <div id="title"></div>
  <div id="btns">
    <button id="ok" onclick="window.webkit.messageHandlers.neo.postMessage('accept')">Tell me</button>
    <button id="no" onclick="window.webkit.messageHandlers.neo.postMessage('dismiss')">Dismiss</button>
  </div>
</div>
<script>
  window.setCard=function(j){
    const c=JSON.parse(j), chip=document.getElementById('chip');
    chip.textContent=(c.chip||c.urgency).toUpperCase();
    chip.className=c.urgency;
    document.getElementById('title').textContent=c.title;
    document.getElementById('ok').textContent=c.ok||'Tell me';
    document.getElementById('no').textContent=c.no||'Dismiss';
    document.getElementById('who').textContent=c.who||'NEO NOTICED';
    document.getElementById('card').classList.add('show');
  };
  window.hideCard=function(){document.getElementById('card').classList.remove('show');};
</script></body></html>"""


class _Bridge(NSObject):
    """Receives the button clicks from the card's JavaScript."""

    def userContentController_didReceiveScriptMessage_(self, controller, message):
        try:
            owner = self.owner
            if owner is not None:
                owner._clicked(str(message.body()))
        except Exception as e:
            print(f"[notify] click error: {e}")


class _TimerTarget(NSObject):
    def fire_(self, timer):
        try:
            if self.owner is not None:
                self.owner._timed_out()
        except Exception as e:
            print(f"[notify] timer error: {e}")


class Notifier:
    """
    Owns the card panel. Build on the MAIN thread.

    on_action(insight, action) is called on the main thread with action in
    {"accept", "dismiss", "ignore"} — neo.py routes accept -> speak, and
    tells the sentinel to snooze the key either way.
    """

    def __init__(self, on_action):
        self.on_action = on_action
        self._queue = []
        self._current = None
        self._timer = None

        screen = NSScreen.mainScreen()
        sf = screen.frame()
        x = sf.origin.x + sf.size.width - CARD_W - MARGIN_RIGHT
        y = sf.origin.y + sf.size.height - CARD_H - MARGIN_TOP
        rect = NSMakeRect(x, y, CARD_W, CARD_H)

        mask = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, mask, NSBackingStoreBuffered, False)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(False)
        panel.setIgnoresMouseEvents_(False)          # the card takes clicks
        panel.setLevel_(kCGMaximumWindowLevel - 1)   # just under the overlay
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorFullScreenAuxiliary)

        bridge = _Bridge.alloc().init()
        bridge.owner = self
        config = WebKit.WKWebViewConfiguration.alloc().init()
        config.userContentController().addScriptMessageHandler_name_(bridge, "neo")

        web = WebKit.WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, CARD_W, CARD_H), config)
        try:
            web.setValue_forKey_(False, "drawsBackground")
        except Exception:
            pass
        web.loadHTMLString_baseURL_(_HTML, None)
        panel.setContentView_(web)

        self.panel = panel
        self.web = web
        self._bridge = bridge
        self._timer_target = _TimerTarget.alloc().init()
        self._timer_target.owner = self

    # ---- called from the main thread (via AppHelper.callAfter) --------------
    def notify(self, insight):
        # replace a queued card for the same key (fresher data wins)
        self._queue = [i for i in self._queue if i["key"] != insight["key"]]
        if self._current and self._current["key"] == insight["key"]:
            return
        self._queue.append(insight)
        if self._current is None:
            self._show_next()

    # ---- internals (all main-thread) ----------------------------------------
    def _show_next(self):
        self._cancel_timer()
        if not self._queue:
            self._current = None
            self.panel.orderOut_(None)
            return
        self._current = self._queue.pop(0)
        cur = self._current
        payload = json.dumps({"title": cur["title"], "urgency": cur["urgency"],
                              "ok": cur.get("ok"), "no": cur.get("no"),
                              "chip": cur.get("chip") or ("needs you" if cur.get("kind") == "need" else None),
                              "who": "NEO NEEDS ACCESS" if cur.get("kind") == "need" else None})
        self.panel.orderFrontRegardless()
        self._js(f"window.setCard && setCard({json.dumps(payload)})")
        secs = TIMEOUT_S.get(self._current["urgency"], 45)
        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            secs, self._timer_target, "fire:", None, False)

    def _finish(self, action):
        insight, self._current = self._current, None
        self._js("window.hideCard && hideCard()")
        if insight is not None:
            try:
                self.on_action(insight, action)
            except Exception as e:
                print(f"[notify] action error: {e}")
        # small delay before the next card so the fade-out reads
        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.55, self._timer_target, "fire:", {"advance": True}, False)

    def _clicked(self, action):
        if self._current is None:
            return
        self._cancel_timer()
        if action in ("accept", "dismiss"):
            self._finish(action)

    def _timed_out(self):
        # fires both for real timeouts and for the short "advance" delay
        if self._current is not None:
            self._finish("ignore")
        else:
            self._show_next()

    def _cancel_timer(self):
        if self._timer is not None:
            try:
                self._timer.invalidate()
            except Exception:
                pass
            self._timer = None

    def _js(self, script):
        try:
            self.web.evaluateJavaScript_completionHandler_(script, None)
        except Exception as e:
            print(f"[notify] js error: {e}")
