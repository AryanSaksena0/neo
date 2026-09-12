"""
canvas.py — Neo shows you things instead of only saying them.

A spec-driven visual renderer: Neo (or any module) hands over a small JSON
spec and gets a clean, dark, native window — same aesthetic as the brain and
the marketing dashboard. No clip-art, no CDN, no chart library. Everything is
hand-rolled HTML/CSS so it works offline and looks intentional.

Two ways a visual happens:
  1. Modules build specs from REAL data (status rundown -> revenue progress
     + pipeline funnel). Deterministic, zero Gemini cost.
  2. Gemini attaches a hidden [[show: {...}]] tag to a reply when a visual
     would genuinely land better than speech (numbers, comparisons, plans).
     neo.py strips the tag, speaks the sentence, opens the visual.

Spec format (a dict, or {"title","sections":[...]} for a multi-part page):
  {"kind":"progress","title":"Revenue","value":316,"target":5000,"unit":"$",
   "note":"4 paying"}
  {"kind":"bars","title":"Ambassadors","items":[{"label":"Maya","value":31}]}
  {"kind":"funnel","title":"Funnel","items":[{"label":"Clicks","value":214}]}
  {"kind":"steps","title":"This week","items":[{"label":"Send 10 pitches",
   "note":"counselors with emails ready"}]}
  {"kind":"compare","title":"X vs Y","columns":[{"title":"X","points":[...]},
   {"title":"Y","points":[...]}]}

Test hooks: extract_show() and render_html() are pure — no GUI imports.
"""

import json
import os
import re
import subprocess
import sys
import tempfile

ACCENT = {"blue": "#a9c7ff", "amber": "#f4c66a", "green": "#5fd0aa",
          "rose": "#f2a0b3", "slate": "#9aa7bd"}
_CYCLE = ["blue", "green", "amber", "rose", "slate"]

# Hidden tag Gemini can attach to a reply: [[show: {...json...}]]
SHOW_RE = re.compile(r"\[\[\s*show:\s*(\{.*\})\s*\]\]", re.IGNORECASE | re.DOTALL)

# Hidden tag for FREEFORM visuals: [[visual: what to draw]] — Neo then designs
# a full custom page for it (see visualize() below).
VISUAL_RE = re.compile(r"\[\[\s*visual:\s*(.*?)\s*\]\]", re.IGNORECASE | re.DOTALL)


def extract_visual(reply):
    """Split a reply into (spoken_text, visual_request_or_None)."""
    m = VISUAL_RE.search(reply or "")
    if not m:
        return (reply or "").strip(), None
    spoken = VISUAL_RE.sub("", reply).strip()
    req = m.group(1).strip()
    return spoken, (req or None)


def extract_show(reply):
    """Split a reply into (spoken_text, spec_or_None). Bad JSON -> ignored."""
    m = SHOW_RE.search(reply or "")
    if not m:
        return (reply or "").strip(), None
    spoken = SHOW_RE.sub("", reply).strip()
    try:
        spec = json.loads(m.group(1))
    except (ValueError, TypeError):
        return spoken, None
    return spoken, spec if isinstance(spec, dict) else None


# --------------------------------------------------------------------------- #
# rendering — pure: spec dict in, HTML string out
# --------------------------------------------------------------------------- #
def _esc(s):
    import html
    return html.escape(str(s))


def _fmt(v, unit=""):
    import math
    try:
        n = float(v)
        if not math.isfinite(n):          # inf / nan -> show raw, never crash
            return _esc(v)
        s = f"{n:,.0f}" if n == int(n) else f"{n:,.1f}"
    except (TypeError, ValueError, OverflowError):
        return _esc(v)
    return f"{unit}{s}" if unit == "$" else f"{s}{unit}" if unit else s


def _sec_progress(s):
    value, target = float(s.get("value", 0)), float(s.get("target", 0)) or 1.0
    pct = max(0.0, min(100.0, value / target * 100.0))
    unit = s.get("unit", "")
    note = f'<div class="note">{_esc(s["note"])}</div>' if s.get("note") else ""
    return f"""
    <div class="big">{_fmt(value, unit)}<span class="dim"> / {_fmt(target, unit)}</span></div>
    <div class="track"><div class="fill" style="width:{pct:.1f}%"></div></div>
    <div class="pct">{pct:.0f}% of target</div>{note}"""


def _sec_bars(s):
    items = s.get("items", [])[:12]
    top = max((float(i.get("value", 0)) for i in items), default=0) or 1.0
    rows = []
    for n, i in enumerate(items):
        v = float(i.get("value", 0))
        c = ACCENT[_CYCLE[n % len(_CYCLE)]]
        rows.append(
            f'<div class="brow"><div class="blabel">{_esc(i.get("label",""))}</div>'
            f'<div class="btrack"><div class="bfill" style="width:{max(2.5, v/top*100):.1f}%;'
            f'background:{c};box-shadow:0 0 12px {c}55"></div></div>'
            f'<div class="bval">{_fmt(v, s.get("unit",""))}</div></div>')
    return "".join(rows)


def _sec_funnel(s):
    items = s.get("items", [])[:8]
    top = max((float(i.get("value", 0)) for i in items), default=0) or 1.0
    rows = []
    for n, i in enumerate(items):
        v = float(i.get("value", 0))
        w = max(14.0, v / top * 100.0)
        c = ACCENT[_CYCLE[n % len(_CYCLE)]]
        rows.append(
            f'<div class="fstage"><div class="fbar" style="width:{w:.1f}%;'
            f'background:linear-gradient(90deg,{c}33,{c}18);border:1px solid {c}66">'
            f'<span>{_esc(i.get("label",""))}</span><b>{_fmt(v)}</b></div></div>')
    return "".join(rows)


def _sec_steps(s):
    rows = []
    for n, i in enumerate(s.get("items", [])[:8], 1):
        note = f'<div class="snote">{_esc(i["note"])}</div>' if i.get("note") else ""
        rows.append(f'<div class="step"><div class="snum">{n}</div>'
                    f'<div><div class="slabel">{_esc(i.get("label",""))}</div>{note}</div></div>')
    return "".join(rows)


def _sec_compare(s):
    cols = []
    for n, c in enumerate(s.get("columns", [])[:3]):
        color = ACCENT[_CYCLE[n % len(_CYCLE)]]
        pts = "".join(f'<div class="cpt">{_esc(p)}</div>' for p in c.get("points", [])[:8])
        cols.append(f'<div class="ccol" style="border-top:2px solid {color}">'
                    f'<div class="ctitle" style="color:{color}">{_esc(c.get("title",""))}</div>{pts}</div>')
    return f'<div class="cwrap">{"".join(cols)}</div>'


_RENDER = {"progress": _sec_progress, "bars": _sec_bars, "funnel": _sec_funnel,
           "steps": _sec_steps, "compare": _sec_compare}

_CSS = r"""
  :root{color-scheme:dark;}
  html,body{margin:0;min-height:100%;background:#0c0e13;
    font-family:-apple-system,'SF Pro Display',Helvetica,sans-serif;color:#e8ecf4;}
  body{background:radial-gradient(1100px 500px at 75% -10%,#141a26 0%,#0c0e13 55%);}
  #page{max-width:880px;margin:0 auto;padding:52px 40px 60px;}
  h1{font-size:26px;font-weight:650;letter-spacing:-0.2px;margin:0 0 4px;}
  #sub{color:#8b95a8;font-size:13px;margin-bottom:30px;}
  .card{background:rgba(255,255,255,0.035);border:1px solid rgba(255,255,255,0.09);
    border-radius:18px;padding:22px 24px;margin-bottom:18px;}
  h2{font-size:12px;font-weight:700;letter-spacing:1.4px;text-transform:uppercase;
    color:#8b95a8;margin:0 0 16px;}
  .big{font-size:40px;font-weight:700;letter-spacing:-1px;}
  .dim{color:#5d6678;font-size:22px;font-weight:500;}
  .track{height:10px;border-radius:999px;background:rgba(255,255,255,0.07);
    margin:14px 0 8px;overflow:hidden;}
  .fill{height:100%;border-radius:999px;background:linear-gradient(90deg,#5fd0aa,#a9c7ff);
    box-shadow:0 0 14px rgba(95,208,170,0.5);transition:width 1s ease;}
  .pct{color:#8b95a8;font-size:12.5px;}
  .note{color:#aeb8ca;font-size:13.5px;margin-top:8px;}
  .brow{display:flex;align-items:center;gap:12px;margin:9px 0;}
  .blabel{width:180px;font-size:13.5px;color:#c6cede;white-space:nowrap;
    overflow:hidden;text-overflow:ellipsis;}
  .btrack{flex:1;height:9px;border-radius:999px;background:rgba(255,255,255,0.06);}
  .bfill{height:100%;border-radius:999px;}
  .bval{width:70px;text-align:right;font-size:13px;font-weight:600;}
  .fstage{margin:7px 0;}
  .fbar{display:flex;justify-content:space-between;align-items:center;
    border-radius:10px;padding:9px 14px;font-size:13.5px;min-width:120px;}
  .fbar b{margin-left:14px;}
  .step{display:flex;gap:14px;margin:13px 0;align-items:flex-start;}
  .snum{width:26px;height:26px;border-radius:50%;background:rgba(169,199,255,0.14);
    color:#a9c7ff;font-weight:700;font-size:13px;display:flex;align-items:center;
    justify-content:center;flex:none;margin-top:1px;}
  .slabel{font-size:15px;font-weight:600;}
  .snote{color:#8b95a8;font-size:13px;margin-top:3px;line-height:1.45;}
  .cwrap{display:flex;gap:14px;}
  .ccol{flex:1;background:rgba(255,255,255,0.03);border-radius:12px;padding:14px 16px;}
  .ctitle{font-weight:700;font-size:14px;margin-bottom:10px;}
  .cpt{font-size:13.5px;color:#c6cede;padding:6px 0;line-height:1.4;
    border-top:1px solid rgba(255,255,255,0.05);}
  .cpt:first-of-type{border-top:0;}
  #foot{color:#4d5668;font-size:11px;letter-spacing:2px;margin-top:26px;}
"""


def render_html(spec):
    """Spec dict -> full HTML page string. Hostile/garbage specs render as
    much as possible and never raise (bad sections are skipped)."""
    sections = [s for s in (spec.get("sections") or [spec]) if isinstance(s, dict)]
    title = spec.get("title") or (sections[0].get("title") if sections else "") or "Neo"
    sub = spec.get("subtitle", "")
    body = []
    for s in sections:
        fn = _RENDER.get(s.get("kind"))
        if fn is None:
            continue
        head = f"<h2>{_esc(s['title'])}</h2>" if s.get("title") and len(sections) > 1 else ""
        try:
            body.append(f'<div class="card">{head}{fn(s)}</div>')
        except Exception as e:
            print(f"[canvas] section failed: {e}")
    subline = f'<div id="sub">{_esc(sub)}</div>' if sub else '<div id="sub"></div>'
    return (f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<style>{_CSS}</style></head><body><div id='page'>"
            f"<h1>{_esc(title)}</h1>{subline}{''.join(body)}"
            f"<div id='foot'>NEO</div></div></body></html>")


def valid(spec):
    """Is there anything renderable in this spec?"""
    if not isinstance(spec, dict):
        return False
    return any(isinstance(s, dict) and s.get("kind") in _RENDER
               for s in (spec.get("sections") or [spec]))


# --------------------------------------------------------------------------- #
# freeform visuals — Gemini designs the page, on demand
# --------------------------------------------------------------------------- #
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$", re.MULTILINE)


def strip_fences(text):
    return _FENCE_RE.sub("", (text or "").strip()).strip()


def render_page(body_html, title="", subtitle=""):
    """Wrap generated body HTML in Neo's dark page shell."""
    head = f"<h1>{_esc(title)}</h1>" if title else ""
    sub = f'<div id="sub">{_esc(subtitle)}</div>' if subtitle else ""
    return (f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<style>{_CSS}</style></head><body><div id='page'>"
            f"{head}{sub}{body_html}<div id='foot'>NEO</div></div></body></html>")


_VISUAL_PROMPT = """You are Neo's visual designer. Design ONE dark-themed visual that makes this genuinely easier to understand at a glance. Not decoration — explanation.

REQUEST: {request}
{context}

Output rules — follow exactly:
- Return ONLY raw HTML to be injected inside an existing dark page (background #0c0e13 is already set). No markdown fences, no <html>/<head>/<body> tags, no commentary.
- You may use <style> (scope selectors under a wrapper div with a unique class), inline SVG, and <script> for drawing/animation on an inline <canvas>.
- ABSOLUTELY NO external resources: no URLs, no images, no fonts, no CDNs. Must work fully offline.
- Palette: text #e8ecf4, muted #8b95a8, panels rgba(255,255,255,0.035) with 1px rgba(255,255,255,0.09) borders, radius 16-18px. Accents (use sparingly): #a9c7ff blue, #5fd0aa green, #f4c66a amber, #f2a0b3 rose.
- Typography: -apple-system. Generous spacing. No emoji, no clip-art, no stock-photo energy. Think Apple keynote diagram, not PowerPoint 2003.
- Real numbers ONLY if they appear in the request/context. If you don't have data, make a structural/conceptual visual (flow, architecture, timeline, anatomy) — never invent statistics.
- One clear headline inside your HTML. Everything readable at arm's length."""


def visualize(client, model, request, context=""):
    """
    One Gemini call -> a custom page on screen. Returns the html path, or None.
    Caller is responsible for usage counting / quota handling.
    """
    ctx = f"CONTEXT (real data, use it):\n{context}" if context else ""
    prompt = _VISUAL_PROMPT.format(request=request, context=ctx)
    r = client.models.generate_content(model=model, contents=prompt)
    body = strip_fences(r.text)
    if not body or "<" not in body:
        return None
    # belt-and-suspenders: refuse anything that reaches for the network
    if re.search(r"\b(?:src|href)\s*=\s*[\"']?\s*https?:", body, re.I):
        body = re.sub(r"\b(?:src|href)\s*=\s*[\"'][^\"']*[\"']", "", body)
    html_text = render_page(body)
    fd, path = tempfile.mkstemp(suffix=".html", prefix="neo_visual_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(html_text)
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "--window", path])
    return path


# --------------------------------------------------------------------------- #
# showing — spawns its own process so the GUI loop never blocks Neo
# --------------------------------------------------------------------------- #
def show(spec):
    """Render and open a spec in a native window. Returns the html path or None."""
    if not valid(spec):
        return None
    html_text = render_html(spec)
    fd, path = tempfile.mkstemp(suffix=".html", prefix="neo_canvas_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(html_text)
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "--window", path])
    return path


def parse_corner(spec):
    """'340x220' -> (340, 220); junk -> sane default."""
    try:
        w, h = (int(x) for x in str(spec).lower().split("x"))
        return max(200, min(900, w)), max(140, min(700, h))
    except (ValueError, AttributeError):
        return 340, 220


def _open_window(path, corner=None):
    """Native WebKit window. Default: centered ~980x720, activated.
    corner='WxH': compact, pinned TOP-RIGHT, floats above other windows,
    and does NOT steal focus — built for timers/widgets you glance at."""
    import WebKit
    from Cocoa import (
        NSApplication, NSWindow, NSObject, NSMakeRect, NSBackingStoreBuffered,
        NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
        NSWindowStyleMaskResizable, NSWindowStyleMaskMiniaturizable,
        NSScreen, NSFloatingWindowLevel,
    )
    from Foundation import NSURL
    from PyObjCTools import AppHelper

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(1 if corner else 0)   # corner mode: no Dock icon

    class _Delegate(NSObject):
        def applicationShouldTerminateAfterLastWindowClosed_(self, s):
            return True
        def windowWillClose_(self, n):
            NSApplication.sharedApplication().terminate_(None)

    deleg = _Delegate.alloc().init()
    app.setDelegate_(deleg)

    if corner:
        w, h = parse_corner(corner)
        sf = NSScreen.mainScreen().frame()
        rect = NSMakeRect(sf.origin.x + sf.size.width - w - 16,
                          sf.origin.y + sf.size.height - h - 130, w, h)
        mask = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
    else:
        rect = NSMakeRect(0, 0, 980, 720)
        mask = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                | NSWindowStyleMaskResizable | NSWindowStyleMaskMiniaturizable)
    win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        rect, mask, NSBackingStoreBuffered, False)
    win.setTitle_("Neo")
    win.setDelegate_(deleg)

    web = WebKit.WKWebView.alloc().initWithFrame_(
        NSMakeRect(0, 0, rect.size.width, rect.size.height))
    web.setAutoresizingMask_(18)
    win.setContentView_(web)
    web.loadFileURL_allowingReadAccessToURL_(
        NSURL.fileURLWithPath_(path), NSURL.fileURLWithPath_("/"))

    if corner:
        win.setLevel_(NSFloatingWindowLevel)   # stays visible while they works
        win.orderFrontRegardless()             # ...without stealing focus
    else:
        win.center()
        win.makeKeyAndOrderFront_(None)
        app.activateIgnoringOtherApps_(True)
    AppHelper.runEventLoop()


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--window":
        _corner = (sys.argv[sys.argv.index("--corner") + 1]
                   if "--corner" in sys.argv else None)
        try:
            _open_window(sys.argv[2], _corner)
        except Exception as e:
            print(f"[canvas] native window failed ({e}); opening in browser.")
            subprocess.Popen(["open", sys.argv[2]])
    else:
        # demo: python canvas.py
        demo = {"title": "This quarter — where you stand", "subtitle": "live numbers",
                "sections": [
                    {"kind": "progress", "title": "Revenue", "value": 316,
                     "target": 5000, "unit": "$", "note": "4 paying customers"},
                    {"kind": "funnel", "title": "Ambassador funnel",
                     "items": [{"label": "Clicks", "value": 214},
                               {"label": "Signups", "value": 31},
                               {"label": "Paid", "value": 4}]},
                    {"kind": "steps", "title": "This week",
                     "items": [{"label": "Send 10 counselor pitches",
                                "note": "drafts are already written"},
                               {"label": "Follow up with Jordan",
                                "note": "they replied and is waiting"}]}]}
        print(show(demo) or "nothing to show")
