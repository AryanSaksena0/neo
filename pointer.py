"""
pointer.py — Neo shows you where to click, one step at a time.

Not "the setting is under Preferences, then Privacy". A ring, on the actual
button, on the actual screen — and when you click it, the ring moves to the
next one, until you are where you wanted to be.

    look at the screen  ->  what is the NEXT single action toward the goal?
                        ->  VERIFY it before drawing anything
                        ->  ring it, say it out loud
                        ->  wait for the screen to change
                        ->  repeat, until the goal is reached

WHY THE VERIFY STEP IS NOT OPTIONAL. A model asked for coordinates will give
you coordinates whether or not it knows. Measured while building the diagram
work: asked for percentages it returned pixels, and two of four points landed
outside the image entirely. A ring drawn on the wrong button is worse than no
ring — it is confident, and someone will click it. So every step is checked by
drawing the ring and asking a fresh look what is inside it, with the claim
withheld. Steps that fail are not shown.

Cost is two vision calls per step, on the cheapest model, so a four-step
walkthrough is eight calls.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
VISION_MODEL = os.getenv("NEO_VISION_MODEL", "gemini-3.5-flash-lite")
MAX_STEPS = int(os.getenv("NEO_POINT_STEPS", "8"))
# How long to wait for the screen to change after pointing at something.
STEP_WAIT_S = float(os.getenv("NEO_POINT_WAIT", "45"))
CHANGE_POLL_S = 0.6
# Mean per-pixel difference on a 48x48 grey thumbnail that counts as "the
# screen changed". Low enough to catch a menu opening, high enough to ignore a
# blinking cursor or a clock ticking over.
CHANGE_THRESHOLD = float(os.getenv("NEO_POINT_CHANGE", "3.0"))

_active = {"proc": None, "spec": None}
_lock = threading.RLock()


# --------------------------------------------------------------------------- #
# Pure helpers.
# --------------------------------------------------------------------------- #
def fingerprint(image):
    """A tiny greyscale thumbnail — enough to tell 'the screen changed'."""
    return list(image.convert("L").resize((48, 48)).getdata())


def changed(before, after, threshold=CHANGE_THRESHOLD):
    """Mean per-pixel difference between two fingerprints. Pure."""
    if not before or not after or len(before) != len(after):
        return True
    total = sum(abs(a - b) for a, b in zip(before, after))
    return (total / float(len(before))) >= threshold


# Words that appear in half the controls on any screen and so distinguish
# nothing. "Settings button" and "Bookmarks button" must not look alike.
_UI_NOISE = {"button", "icon", "menu", "item", "link", "field", "tab", "the",
             "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
             "control", "toggle", "option", "bar", "box", "click", "press"}


def same_control(claim, answer):
    """Is the ring on the control we said it was? Pure.

    Deliberately looser than the anatomy check this borrows from. There, a part
    has one correct name; here the same button is honestly "New tab", "plus
    icon" or "the + button" depending on who is looking, and demanding every
    word match would reject correct rings constantly. One distinctive word in
    common is the bar — enough to separate Settings from Bookmarks, which is
    the mistake that actually matters.
    """
    def words(t):
        import re as _re
        return {w for w in _re.split(r"[^a-z0-9]+", (t or "").lower())
                if len(w) > 1 and w not in _UI_NOISE}
    a, b = words(claim), words(answer)
    if not a or not b:
        return False
    if (answer or "").strip().lower().startswith("none"):
        return False
    return bool(a & b)


def as_box(step, w, h):
    """One model answer -> a ring in PERCENTAGES, or None if it cannot be one.

    The same guard the diagram work needed: an answer in pixels, or off the
    edge of the screen, is not a place to draw. Pure.
    """
    try:
        x, y = float(step.get("x")), float(step.get("y"))
    except (TypeError, ValueError):
        return None
    bw = float(step.get("w") or 0) or None
    bh = float(step.get("h") or 0) or None
    # Over 100 cannot be a percentage, so the whole answer is in pixels.
    if x > 100 or y > 100 or (bw or 0) > 100 or (bh or 0) > 100:
        if not w or not h:
            return None
        x, y = x / w * 100.0, y / h * 100.0
        bw = (bw / w * 100.0) if bw else None
        bh = (bh / h * 100.0) if bh else None
    if not (0.5 <= x <= 99.5 and 0.5 <= y <= 99.5):
        return None
    return {"x": round(x, 2), "y": round(y, 2),
            "w": round(min(max(bw or 9.0, 3.0), 60.0), 2),
            "h": round(min(max(bh or 5.0, 2.5), 40.0), 2)}


# --------------------------------------------------------------------------- #
# Looking at the screen.
# --------------------------------------------------------------------------- #
def _own_window_ids():
    """Every on-screen window that belongs to Neo: this process (orb, HUD,
    answer panel, onboarding) and the spotlight subprocess (rings, bands)."""
    import Quartz
    mine = {os.getpid()}
    with _lock:
        proc = _active.get("proc")
    if proc is not None and proc.poll() is None:
        mine.add(proc.pid)
    ids = []
    try:
        for w in Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID):
            if int(w.get("kCGWindowOwnerPID", -1)) in mine:
                ids.append(int(w["kCGWindowNumber"]))
    except Exception:
        pass
    return ids


def capture():
    """The screen, as a PIL image, WITHOUT Neo's own windows. None if the
    capture is not permitted.

    `screencapture -x` photographs everything, and everything included Neo:
    the HUD, the answer panel, a ring from the last walkthrough, and on 11
    Sept the onboarding demo sitting behind a Google Form. OCR read "Start
    using Neo" off that window and click_on_screen dutifully clicked it — on
    the form. Neo was reading its own furniture as the user's screen.

    So the capture is composed from every on-screen window EXCEPT ours, by
    window id. If that path fails for any reason, the old full-screen capture
    is the fallback — a picture that includes Neo beats no picture.
    """
    from PIL import Image
    try:
        import Quartz
        import ctypes
        mine = set(_own_window_ids())
        wins = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID)
        keep = [int(w["kCGWindowNumber"]) for w in wins
                if int(w["kCGWindowNumber"]) not in mine
                and int(w.get("kCGWindowLayer", 0)) <= 0]   # no menu bar/dock
        if keep:
            img = Quartz.CGWindowListCreateImageFromArray(
                Quartz.CGRectInfinite, keep,
                Quartz.kCGWindowImageDefault)
            if img is not None:
                w, h = Quartz.CGImageGetWidth(img), Quartz.CGImageGetHeight(img)
                bpr = Quartz.CGImageGetBytesPerRow(img)
                data = Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(img))
                buf = bytes(data)
                pil = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", bpr, 1)
                return pil.convert("RGB")
    except Exception:
        pass
    path = os.path.join(tempfile.gettempdir(), "neo_point.png")
    r = subprocess.run(["screencapture", "-x", path], capture_output=True)
    if r.returncode != 0 or not os.path.exists(path):
        return None
    try:
        return Image.open(path).convert("RGB")
    except Exception:
        return None


def _shrink(image, longest=1400):
    """Vision does not need 2880 pixels of retina, and the smaller the image
    the faster and cheaper every step is. Coordinates come back as
    percentages, so nothing downstream cares about the size."""
    im = image.copy()
    im.thumbnail((longest, longest))
    return im


def _png(image):
    import io
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


STEP_BRIEF = """You are guiding someone through their own computer screen,
one click at a time. Their goal:

    {goal}

{done_so_far}
Look at this screenshot and decide the SINGLE NEXT THING they should click or
open to get closer. One step. Not a plan. Do NOT give coordinates — you are
only naming it.

Return ONLY JSON:
{{"done": true|false,
  "say": "<one short spoken sentence, under 14 words>",
  "label": "<the control, in 2-3 words>"}}

`label` is the words printed on the control — "Privacy & Security", "Save" — or
what the icon is if it has no text: "plus icon", "gear icon", "three dots".
Never a sentence.

Set done:true when the goal is already satisfied on this screen; then `say` is
what you would tell them and `label` is ignored.

If the next control is genuinely not on this screen, set label:"" — we will say
so rather than guess."""

# --------------------------------------------------------------------------- #
# WHERE, decided as a CHOICE rather than a coordinate.
#
# Asked outright for x and y, the model put the same button at y=27.8 on one
# call and y=71.0 on the next, and confidently placed a "New Tab button" on a
# screen with no browser on it at all. This is the same wall the diagram work
# hit: models reason about pictures well and about coordinates badly.
#
# So the screen is covered in a labelled grid and the question becomes "which
# cell?", which is a multiple choice. Two passes — a coarse grid to find the
# region, then a fine grid inside it for precision, because one 12x8 cell can
# hold four different sidebar items.
#
# The other thing this buys is the ability to say NO. Offered a null cell, the
# model correctly answered null for a button that did not exist, where the
# coordinate form had invented a location for it.
# --------------------------------------------------------------------------- #
COARSE = (12, 8)
# The fine pass has to resolve ADJACENT MENU ITEMS. Measured on a real sidebar,
# "Artifacts" and "Customize" sit about 30 px apart; a 6x6 fine grid over a
# two-cell crop gave ~40 px cells and could not tell them apart, landing
# between the two. 9x9 over a tighter crop gets under the gap.
FINE = (9, 9)
FINE_MARGIN = 0.3


def _grid(image, cols, rows):
    from PIL import ImageDraw, ImageFont
    im = image.convert("RGB").copy()
    W, H = im.size
    dr = ImageDraw.Draw(im, "RGBA")
    size = max(13, int(min(W, H) / 60))
    try:
        f = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                               size)
    except Exception:
        f = ImageFont.load_default()
    for i in range(cols + 1):
        dr.line([i * W / cols, 0, i * W / cols, H], fill=(255, 0, 0, 110))
    for j in range(rows + 1):
        dr.line([0, j * H / rows, W, j * H / rows], fill=(255, 0, 0, 110))
    for j in range(rows):
        for i in range(cols):
            x, y = i * W / cols + 3, j * H / rows + 2
            dr.rectangle([x - 1, y - 1, x + size * 1.7, y + size + 3],
                         fill=(0, 0, 0, 150))
            dr.text((x + 1, y), f"{chr(65 + i)}{j + 1}", fill=(255, 90, 90),
                    font=f)
    return im


def _ask_cell(image, label, cols, rows, client, log=print):
    """Which cell holds `label`? Returns (col, row) or None. Never guesses."""
    from google.genai import types
    import deck
    last = f"{chr(64 + cols)}{rows}"
    q = (f"A labelled grid covers this screenshot (A1 top-left, {last} "
         f"bottom-right).\nWhich ONE cell contains: {label}?\n"
         f"If it is NOT visible here, return cell:null — do not guess.\n"
         'Return ONLY JSON: {"cell":"C2"|null,"what":"<what is in that cell>"}')
    try:
        r = client.models.generate_content(
            model=VISION_MODEL,
            contents=[types.Part.from_bytes(data=_png(_grid(image, cols, rows)),
                                            mime_type="image/png"), q])
        got = deck._loads(getattr(r, "text", "") or "") or {}
    except Exception as e:
        log(f"[point] could not read the screen ({type(e).__name__})")
        return None
    cell = got.get("cell")
    if not isinstance(cell, str) or len(cell) < 2 or not cell[0].isalpha():
        return None
    try:
        ci, ri = ord(cell[0].upper()) - 65, int(cell[1:]) - 1
    except ValueError:
        return None
    if not (0 <= ci < cols and 0 <= ri < rows):
        return None
    return ci, ri


def locate(image, label, client, log=print):
    """Where is `label` on this screen, in percentages? None if it is not."""
    shot = _shrink(image)
    W, H = shot.size
    coarse = _ask_cell(shot, label, *COARSE, client, log=log)
    if coarse is None:
        return None
    ci, ri = coarse
    cw, ch = W / COARSE[0], H / COARSE[1]
    # A coarse cell can hold four sidebar items, so look again inside it — with
    # a margin, because a control often straddles a grid line.
    mx, my = cw * FINE_MARGIN, ch * FINE_MARGIN
    box = (max(0, ci * cw - mx), max(0, ri * ch - my),
           min(W, (ci + 1) * cw + mx), min(H, (ri + 1) * ch + my))
    crop = shot.crop(box)
    fine = _ask_cell(crop, label, *FINE, client, log=log)
    if fine is None:            # visible in the region but not pinned: centre it
        px, py = (ci + 0.5) * cw, (ri + 0.5) * ch
        return {"x": round(px / W * 100, 2), "y": round(py / H * 100, 2),
                "w": round(cw / W * 100, 2), "h": round(ch / H * 100, 2)}
    fi, fj = fine
    fw, fh = (box[2] - box[0]) / FINE[0], (box[3] - box[1]) / FINE[1]
    px = box[0] + (fi + 0.5) * fw
    py = box[1] + (fj + 0.5) * fh
    return {"x": round(px / W * 100, 2), "y": round(py / H * 100, 2),
            "w": round(max(fw / W * 100, 3.0), 2),
            "h": round(max(fh / H * 100, 2.5), 2)}


def next_step(image, goal, history, client, log=print):
    """One vision call: what should they do next? No coordinates involved."""
    from google.genai import types
    done_so_far = ("Already done: " + " -> ".join(history) + "\n"
                   if history else "They have not started yet.\n")
    small = _shrink(image)
    r = client.models.generate_content(
        model=VISION_MODEL,
        contents=[types.Part.from_bytes(data=_png(small), mime_type="image/png"),
                  STEP_BRIEF.format(goal=goal, done_so_far=done_so_far)])
    import deck
    return (deck._loads(getattr(r, "text", "") or "") or {}), small.size


def verify_step(image, step, box, client, log=print):
    """Draw the ring, show it back, and ask what is inside it.

    The claim is never shown. The model has to name the control itself, which
    is the only way agreement means anything. This is the same check that
    caught a marker placed on the wrong chamber of a heart, and the reason it
    is safe to point at a real screen at all.
    """
    from google.genai import types
    from PIL import ImageDraw
    label = str(step.get("label") or "").strip()
    if not label:
        return False
    shot = _shrink(image)
    W, H = shot.size
    marked = shot.copy()
    dr = ImageDraw.Draw(marked)
    cx, cy = box["x"] / 100 * W, box["y"] / 100 * H
    bw, bh = box["w"] / 100 * W / 2, box["h"] / 100 * H / 2
    dr.rectangle([cx - bw, cy - bh, cx + bw, cy + bh],
                 outline=(255, 0, 0), width=5)
    q = ("A red rectangle has been drawn on this screenshot. Name the control "
         "inside it in a few words — the text printed on it if it has any, or "
         "what the icon is (\"plus icon\", \"gear icon\") if it has none. "
         "Answer with just that, or NONE if the rectangle is not on a control.")
    try:
        r = client.models.generate_content(
            model=VISION_MODEL,
            contents=[types.Part.from_bytes(data=_png(marked),
                                            mime_type="image/png"), q])
        said = (getattr(r, "text", "") or "").strip()
    except Exception as e:
        log(f"[point] could not verify the step ({type(e).__name__})")
        return False
    ok = same_control(label, said)
    if not ok:
        log(f"[point] wanted {label!r}, the ring is on {said[:40]!r} — not "
            f"showing it")
    return ok


# --------------------------------------------------------------------------- #
# The window, and the walkthrough.
# --------------------------------------------------------------------------- #
# Which walkthrough is allowed to draw. close() bumps it, so any walkthrough
# still running in its background thread finds itself superseded and stops.
#
# Without this, "get these off my screen" closed the window and the walkthrough
# — which is fire-and-forget on its own thread, waiting for a click that never
# came — simply drew the next step a moment later. The ring came back, Neo had
# already said "done, they're gone", and the user had to ask twice.
_generation = {"n": 0}


def current_generation():
    with _lock:
        return _generation["n"]


def supersede():
    """Invalidate whatever is drawing now. Returns the new generation."""
    with _lock:
        _generation["n"] += 1
        return _generation["n"]


def _write(spec):
    with _lock:
        path = _active.get("spec")
    if not path:
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(spec, f)
    os.replace(tmp, path)      # atomic: the watcher never reads half a file


def _open_window(spec):
    """One window for the WHOLE walkthrough. Reopening it per step would
    flicker, and a ring that blinks between steps is harder to follow than one
    that slides."""
    with _lock:
        if _active["proc"] and _active["proc"].poll() is None:
            _write(spec)
            return
        fd, path = tempfile.mkstemp(suffix=".json", prefix="neo_point_")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(spec, f)
        _active["spec"] = path
        _active["proc"] = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "spotlight.py"), path])


# How long a mark stays before taking itself off. A highlight is for reading a
# line, not for living on the screen: left alone it becomes a smear over
# whatever the user does next, and they have to ask twice to get rid of it. Long
# enough to read a sentence twice, short enough that a forgotten one is gone.
HOLD_S = float(os.getenv("NEO_HIGHLIGHT_HOLD", "20"))


def draw(spec, hold=None):
    """Put shapes on the screen. They clear themselves after `hold` seconds.

    The walkthrough owns the window while it runs; this is the entry point for
    everything that just wants to mark something — a highlighted sentence, a
    single ring — without stepping anywhere.

    A new mark SUPERSEDES a running walkthrough, which is what makes the
    expiry safe: the generation it captures is unique to this drawing, so a
    timer can never take down something drawn after it.
    """
    hold = HOLD_S if hold is None else hold
    mine = supersede()
    _open_window(spec)
    if hold and hold > 0:
        def _expire():
            if current_generation() == mine:
                close("timed out")
        t = threading.Timer(hold, _expire)
        t.daemon = True
        t.start()
        _active["timer"] = t
    return mine


def close(_reason=""):
    """Take the ring off the screen, and stop anything that would put it back."""
    supersede()
    with _lock:
        proc, path = _active.get("proc"), _active.get("spec")
        _active["proc"] = _active["spec"] = None
    if path:
        try:
            _write_done(path)
        except Exception:
            pass
        try:
            os.unlink(path)
        except OSError:
            pass
    if proc and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass


def _write_done(path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"done": True}, f)


def wait_for_change(before, limit=STEP_WAIT_S):
    """Block until the screen looks different, or we give up. Returns the new
    screen, or None if nothing happened."""
    deadline = time.time() + limit
    while time.time() < deadline:
        time.sleep(CHANGE_POLL_S)
        now = capture()
        if now is None:
            return None
        if changed(before, fingerprint(now)):
            time.sleep(0.45)          # let the new view settle before looking
            return capture() or now
    return None


def guide(goal, client, say=None, log=print, max_steps=MAX_STEPS):
    """Walk someone to `goal`, one ring at a time. Returns a spoken summary."""
    say = say or (lambda _t: None)
    shot = capture()
    if shot is None:
        return ("I can't see your screen — Neo needs Screen Recording "
                "permission in System Settings.")
    history, shown = [], 0
    mine = current_generation()

    def _superseded():
        """They said stop, or another walkthrough started. Either way this one is
        no longer the thing on screen and must not draw again."""
        return current_generation() != mine

    try:
        for n in range(max_steps):
            if _superseded():
                log("[point] stopped — superseded")
                return ""
            step, size = next_step(shot, goal, history, client, log=log)
            if not step:
                log("[point] no readable answer for this screen")
                break
            if step.get("done"):
                spoken = str(step.get("say") or "That's it — you're there.")
                say(spoken)
                return spoken
            label = str(step.get("label") or "").strip()
            if not label:
                log("[point] the next control isn't on this screen")
                break
            box = locate(shot, label, client, log=log)
            if not box:
                log(f"[point] couldn't find {label!r} on this screen")
                break
            if not verify_step(shot, step, box, client, log=log):
                break
            # Re-check immediately before drawing: locate() and verify_step()
            # are two model round trips, several seconds in which they can easily
            # have said "stop".
            if _superseded():
                log("[point] stopped — superseded")
                return ""
            shown += 1
            _open_window({"dim": False, "shapes": [dict(
                box, kind="ring", label=label,
                step=f"step {shown}")]})
            spoken = str(step.get("say") or f"Click {label}.")
            say(spoken)
            log(f"[point] step {shown}: {label}")
            history.append(label)
            nxt = wait_for_change(fingerprint(shot))
            if nxt is None:
                return (f"I've got {label} highlighted — tell me when you've "
                        f"clicked it and I'll show you the next one.")
            shot = nxt
        if shown:
            return ("That's as far as I can take you from what's on screen. "
                    "Tell me what you see now and I'll pick it up.")
        return ("I couldn't find that on your screen. Tell me which app it's "
                "in and I'll look again.")
    finally:
        # Only clear the screen if this walkthrough still owns it. Closing
        # unconditionally meant a walkthrough finishing its last step could
        # wipe the window a NEWER one had just opened.
        if not _superseded():
            close("finished")
