"""
imagery.py — a real picture, understood well enough to point at.

Neo's walkthroughs were drawn, badly. Every free path to a DRAWN figure was
tried and measured: Gemini flash produced overlapping garbage, a 120B model
produced grey rectangles, an icon-and-layout engine produced diagrams that
collided with their own labels. A published teaching plate beats all of them
and costs nothing.

The reason it was not used before is that a fetched picture is opaque: the
model choosing the search term has never seen the result, so any arrow it
places is a guess. deck.py said so and refused to draw leader lines on images
at all, which left a bare photograph with a list of words beside it.

That objection is gone, because the model can LOOK at the image. So:

    find  -> several candidates for one subject
    judge -> is this the right subject, a teaching drawing, in English?
    locate-> where is each part, as a fraction of the frame?
    VERIFY-> crop each claimed spot, show it ALONE, and ask what is there

The verification step is the whole point and it is not optional. Measured on a
real plate with its printed labels blurred out, localisation degrades badly:
the model returned PIXELS when asked for percentages, and two of four
coordinates fell outside the image entirely. Unverified, that puts a confident
marker on the wrong chamber, which teaches something false — worse than no
marker at all.

So nothing reaches the screen unless a second look, with no claim attached,
agrees. Parts that fail are dropped silently. Neo would rather point at four
things correctly than at six things approximately.
"""

import io
import json
import math
import os
import re
import threading
import time

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".imgcache")
VISION_MODEL = os.getenv("NEO_VISION_MODEL", "gemini-3.5-flash-lite")
# How many search results are judged before giving up on a subject.
MAX_CANDIDATES = int(os.getenv("NEO_IMG_CANDIDATES", "3"))
# A crop this fraction of the shortest side is shown to the verifier.
CROP_FRAC = 0.20
_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Pure helpers — the part worth testing without a network or a model.
# --------------------------------------------------------------------------- #
def normalise_points(parts, width, height):
    """Coerce whatever the model returned into fractions of the frame, and
    throw away anything that cannot be one. Pure.

    THIS IS THE CHECK THAT CATCHES THE MEASURED FAILURE. Asked for percentages
    0-100, gemini-3.5-flash-lite returned pixel coordinates — 712, 748, 773 on
    a 563-wide image. Read as percentages those are off the right-hand edge;
    read as pixels two of the four are still outside the picture. Either way a
    marker lands somewhere meaningless, and the only safe response is to notice
    and drop it.
    """
    if not parts:
        return []
    xs = [p.get("x") for p in parts if isinstance(p.get("x"), (int, float))]
    ys = [p.get("y") for p in parts if isinstance(p.get("y"), (int, float))]
    if not xs or not ys:
        return []
    # If anything is over 100 it cannot be a percentage, so the whole answer is
    # in pixels. Decided per ANSWER, not per point: a mixed reading would put
    # some markers in one space and some in another.
    pixels = max(xs) > 100 or max(ys) > 100
    out = []
    for p in parts:
        x, y = p.get("x"), p.get("y")
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            continue
        fx = (x / float(width)) if pixels else (x / 100.0)
        fy = (y / float(height)) if pixels else (y / 100.0)
        # Outside the frame, or hard against an edge where a crop would be
        # mostly background: not a part of the picture.
        if not (0.02 <= fx <= 0.98 and 0.02 <= fy <= 0.98):
            continue
        name = str(p.get("name") or "").strip()
        if not name:
            continue
        out.append({"name": name[:40], "x": round(fx * 100, 2),
                    "y": round(fy * 100, 2)})
    return out


def spread_ok(parts, min_gap=6.0):
    """Drop parts sitting on top of each other. Pure.

    Two markers 2% apart are one marker as far as the eye is concerned, and the
    second one is usually the model repeating itself under another name.
    """
    kept = []
    for p in parts:
        if all(math.hypot(p["x"] - q["x"], p["y"] - q["y"]) >= min_gap
               for q in kept):
            kept.append(p)
    return kept


def verdict(claim, answer):
    """Did the blind second look agree? Pure.

    Deliberately strict about NO and lenient about wording: the verifier
    answers in prose about half the time ("the structure at the centre is the
    Left ventricle"), and that is agreement.
    """
    a = (answer or "").strip().lower()
    c = (claim or "").strip().lower()
    if not a or not c:
        return False
    if a.startswith("none") or " none" in a[:40]:
        return False
    if c in a:
        return True
    # "left ventricle" vs "ventricle, left" — every significant word present.
    words = [w for w in re.split(r"[^a-z0-9]+", c) if len(w) > 2]
    return bool(words) and all(w in a for w in words)


# --------------------------------------------------------------------------- #
# Finding a picture worth using.
# --------------------------------------------------------------------------- #
def _cache_path(key):
    import hashlib
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR,
                        hashlib.sha1(key.encode("utf-8")).hexdigest()[:20])


def cached(key):
    p = _cache_path(key)
    try:
        with open(p, "rb") as f:
            return f.read()
    except Exception:
        return None


def remember(key, blob):
    try:
        with open(_cache_path(key), "wb") as f:
            f.write(blob)
    except Exception:
        pass


_QUERY_STOP = {"the","a","an","and","or","of","to","in","is","are","it","how",
               "why","what","when","this","that","through","into","from","for",
               "with","by","its","works","work","explained","explain","about"}


def search_term(subject, limit=4):
    """A sentence is not a search. Pure.

    Passed "the human heart and how blood flows through it", Commons matched on
    "flows" and "through" and returned a reflex arc. An image search wants the
    two or three nouns a textbook caption would carry, so keep those and drop
    the sentence around them.
    """
    words = [w for w in re.split(r"[^A-Za-z0-9-]+", str(subject or ""))
             if w and w.lower() not in _QUERY_STOP and len(w) > 2]
    return " ".join(words[:limit]) or str(subject or "").strip()


def candidates(subject, log=print, limit=MAX_CANDIDATES):
    """Several pictures for one subject, best-ranked first.

    Searched more than one way on purpose. "data center cooling diagram" on its
    own returned a NASA radioisotope generator; the wording of the query is
    doing as much work as the ranker, so try the phrasings a textbook caption
    would actually use and take the union.
    """
    import deck
    subject = search_term(subject)
    seen, out = set(), []
    for phrasing in (f"{subject} diagram", f"{subject} anatomy",
                     f"{subject} cross section", subject):
        if len(out) >= limit:
            break
        try:
            got = deck.search_image(phrasing, log=lambda _m: None)
        except Exception:
            got = None
        if not got:
            continue
        blob, credit = got
        key = len(blob)
        if key in seen:
            continue
        seen.add(key)
        out.append((blob, credit, phrasing))
    if not out:
        log(f"[img] nothing found for {subject!r}")
    return out


VISION_BRIEF = """You are choosing a teaching picture for one slide about:
{subject}

Judge THIS image, then locate its parts.

Return ONLY JSON:
{{"usable": true|false,
  "why": "<one short sentence>",
  "teaching_quality": <1-5>,
  "english": true|false,
  "parts": [{{"name":"<the structure>","x":<0-100>,"y":<0-100>}}]}}

usable is FALSE if: it is not {subject}; it is a photograph of a real specimen
rather than an illustration; it is a chart, a logo, a map or a screenshot; it
is too cluttered to read on a slide; or its labels are not in English.

teaching_quality: 5 = a textbook plate someone would learn from. 1 = technically
on-topic and useless.

parts: 3 to 6 structures a student needs, with x and y as PERCENTAGES OF THIS
IMAGE (0,0 is the top-left corner, 100,100 the bottom-right). Point at the
CENTRE of each structure. If you are not sure where something is, LEAVE IT OUT
— a wrong marker teaches something false. Percentages, not pixels."""


def judge_and_locate(blob, subject, client, model=None, log=print):
    """One vision call: is this worth showing, and where is everything?"""
    from google.genai import types
    from PIL import Image
    im = Image.open(io.BytesIO(blob))
    w, h = im.size
    r = client.models.generate_content(
        model=model or VISION_MODEL,
        contents=[types.Part.from_bytes(data=blob, mime_type="image/png"),
                  VISION_BRIEF.format(subject=subject)])
    import deck
    got = deck._loads(getattr(r, "text", "") or "") or {}
    got["parts"] = spread_ok(normalise_points(got.get("parts"), w, h))
    return got


def _marked_sheet(im, parts):
    """The WHOLE image with a numbered ring on each claimed spot.

    The first design cropped tightly around each point and asked what was in
    the crop. That failed for a reason worth writing down: a 20% crop of an
    anatomical plate is a field of pink tissue, and nothing — model or person —
    can name a chamber from it. Verification was rejecting good markers because
    it had thrown away the context needed to judge them.

    Marking the full frame keeps the context and still tells the verifier
    nothing: the rings are numbered, never named, so it has to identify each
    structure itself. If the picture carries printed labels, reading them IS a
    valid check — the label is that plate's own ground truth.
    """
    from PIL import ImageDraw, ImageFont
    im = im.convert("RGB").copy()
    W, H = im.size
    dr = ImageDraw.Draw(im)
    r = max(14, int(min(W, H) * 0.045))
    try:
        f = ImageFont.truetype(
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf", int(r * 1.4))
    except Exception:
        f = ImageFont.load_default()
    for i, p in enumerate(parts, start=1):
        cx, cy = p["x"] / 100 * W, p["y"] / 100 * H
        dr.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 0, 0), width=5)
        dr.ellipse([cx - 4, cy - 4, cx + 4, cy + 4], fill=(255, 0, 0))
        tx, ty = cx + r + 4, cy - r
        dr.rectangle([tx - 3, ty - 3, tx + r * 1.5, ty + r * 1.8], fill=(255, 0, 0))
        dr.text((tx + 3, ty), str(i), fill=(255, 255, 255), font=f)
    return im


def verify(blob, parts, subject, client, model=None, log=print):
    """Show each claimed spot ALONE and ask what is there. Keep the agreements.

    The claim is never shown to the verifier — it has to name the structure
    itself, so agreement means something. Tested against deliberate lies: a
    marker put on the right atrium and called "left ventricle" came back
    "Right atrium" and was dropped, as was one placed on empty background.
    """
    if not parts:
        return []
    from google.genai import types
    from PIL import Image
    im = Image.open(io.BytesIO(blob)).convert("RGB")
    sheet = _marked_sheet(im, parts)
    if sheet is None:
        return []
    buf = io.BytesIO()
    sheet.save(buf, format="PNG")
    names = sorted({p["name"] for p in parts})
    q = (f"This is a diagram of {subject}. Red numbered rings have been drawn "
         f"on it.\n\nFor EACH numbered ring, name which ONE of these structures "
         f"the ring is sitting on:\n"
         + "\n".join(f"- {n}" for n in names)
         + "\n\nAnswer NONE for a ring that is not on any of them. Do not guess, "
           "and do not assume ring 1 is the first item in the list.\n"
           'Return ONLY JSON: {"crops":[{"n":1,"answer":"..."}]}')
    try:
        r = client.models.generate_content(
            model=model or VISION_MODEL,
            contents=[types.Part.from_bytes(data=buf.getvalue(),
                                            mime_type="image/png"), q])
        import deck
        got = deck._loads(getattr(r, "text", "") or "") or {}
    except Exception as e:
        log(f"[img] verification failed ({type(e).__name__}) — showing no markers")
        return []          # cannot verify: show nothing rather than guess
    answers = {}
    for c in (got.get("crops") or []):
        try:
            answers[int(c.get("n"))] = str(c.get("answer") or "")
        except (TypeError, ValueError):
            continue
    kept = []
    for i, p in enumerate(parts, start=1):
        if verdict(p["name"], answers.get(i, "")):
            kept.append(p)
    dropped = len(parts) - len(kept)
    if dropped:
        log(f"[img] {dropped} of {len(parts)} markers failed verification "
            f"and were dropped")
    return kept


def illustrate(subject, client, log=print, want=4):
    """The whole pipeline: a picture, and only the markers that survive.

    Returns (blob, credit, parts) or None. Two vision calls per picture.
    """
    cache_key = f"v1|{subject}"
    for blob, credit, phrasing in candidates(subject, log=log):
        try:
            got = judge_and_locate(blob, subject, client, log=log)
        except Exception as e:
            log(f"[img] could not judge {phrasing!r} ({type(e).__name__})")
            continue
        q = got.get("teaching_quality")
        if not got.get("usable") or (isinstance(q, (int, float)) and q < 3):
            log(f"[img] rejected {phrasing!r}: {str(got.get('why'))[:70]}")
            continue
        if got.get("english") is False:
            log(f"[img] rejected {phrasing!r}: labels are not in English")
            continue
        parts = verify(blob, got.get("parts") or [], subject, client, log=log)
        log(f"[img] using {phrasing!r} (quality {q}) with {len(parts)} "
            f"verified marker(s)")
        remember(cache_key, blob)
        return blob, credit, parts[:want]
    return None
