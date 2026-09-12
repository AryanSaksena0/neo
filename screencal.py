"""
screencal.py — the free time on the calendar that is ON SCREEN.

"Look at my screen and find a 15-minute gap for the three of us." The
screen already has everything: Google Calendar's week view with "Meet with"
overlays for the other people (their busy blocks, in other colours). Nothing
on this Mac knows those people's calendars — but the pixels do.

One vision call with a tight brief: return the busy blocks per weekday, ANY
colour, as JSON. Code then finds the gaps in the working day. The model
reads; the code decides. The answer names the day and time and says where
it came from ("from what's on your screen"), and if the screenshot isn't a
calendar it says that instead of inventing a slot.
"""

import datetime as dt
import json
import re

WORK_START, WORK_END = 9, 17
MARGIN_MIN = 10          # vision reads edges a few minutes off; keep clear of them

_PROMPT = """This screenshot should show a calendar in WEEK view, possibly with
several people's busy blocks overlaid in different colours ("busy", classes,
meetings). Read the grid carefully using the hour labels on the left.

Return ONLY JSON, no prose, no code fence:
{"is_calendar": true,
 "week_start": "YYYY-MM-DD" (the Sunday or Monday shown, from the header; null if unreadable),
 "visible_days": ["Mon", ...] (ONLY the weekday columns actually visible and unobstructed — a column hidden behind another window is NOT visible),
 "days": {"Mon": [["HH:MM","HH:MM"], ...], "Tue": [...], "Wed": [...], "Thu": [...], "Fri": [...]}}

Rules: 24-hour times. Include EVERY block of ANY colour, including thin ones
and ones labelled only "busy". A block showing only a start time is 45
minutes long. When a block's edge is unclear, ROUND OUTWARD to the next
quarter hour (make blocks longer, never shorter). Do not merge different
days. If this is not a calendar, return {"is_calendar": false}."""

_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"]


def parse(raw):
    """Model text -> (days dict of (start,end) times per weekday, week_start
    date or None, is_calendar). Pure, tolerant of fences."""
    m = re.search(r"\{.*\}", str(raw or ""), re.S)
    if not m:
        return {}, None, False
    try:
        d = json.loads(m.group(1) if False else m.group(0))
    except ValueError:
        return {}, None, False
    if not d.get("is_calendar"):
        return {}, None, False
    week = None
    try:
        week = dt.date.fromisoformat(str(d.get("week_start"))[:10])
    except Exception:
        week = None
    visible = [str(x)[:3].title() for x in (d.get("visible_days") or _DAYS)]
    days = {}
    for name in _DAYS:
        if name not in visible:
            continue                     # unseen is unknown, never free
        out = []
        for pair in (d.get("days") or {}).get(name, []) or []:
            try:
                a, b = pair[0], pair[1]
                h1, m1 = [int(x) for x in str(a).split(":")[:2]]
                h2, m2 = [int(x) for x in str(b).split(":")[:2]]
                s, e = dt.time(h1 % 24, m1), dt.time(h2 % 24, m2)
                if e > s:
                    out.append((s, e))
            except Exception:
                continue
        days[name] = sorted(out)
    return days, week, True


def gaps(days, minutes, start_hour=WORK_START, end_hour=WORK_END, exclude=()):
    """[(day_name, start_time, end_time)] gaps of at least `minutes` inside the
    working day, in week order. Only days present in `days` (the visible
    ones) are considered: a day that wasn't on screen is unknown. Pure."""
    out = []
    for name in _DAYS:
        if name not in days:
            continue
        blocks = sorted(list(days.get(name, [])) + [(s, e) for d, s, e in exclude if d == name])
        cur = dt.time(start_hour, 0)
        end = dt.time(end_hour, 0)
        today = dt.date.today()

        def _pad(a, b):
            """The usable part of a free stretch: MARGIN_MIN in from each block edge."""
            a2 = dt.datetime.combine(today, a) + (dt.timedelta(minutes=MARGIN_MIN) if a != dt.time(start_hour, 0) else dt.timedelta())
            b2 = dt.datetime.combine(today, b) - (dt.timedelta(minutes=MARGIN_MIN) if b != end else dt.timedelta())
            if (b2 - a2).total_seconds() / 60 >= minutes:
                out.append((name, a2.time(), b2.time()))
        for s, e in blocks:
            if s > cur:
                _pad(cur, s)
            if e > cur:
                cur = e
        if cur < end:
            _pad(cur, end)
    return out


def say_gap(gap, minutes):
    name, s, e = gap
    return f"{name} {s.strftime('%-I:%M %p')} (free until {e.strftime('%-I:%M %p')})"


# --------------------------------------------------------------------------- #
# The deterministic read: OCR with geometry. The hour labels down the left
# give a pixel→time scale; the day headers give the columns; every chip's
# text gives its start (and often its end). No model guesses an edge — the
# only estimate is the length of a chip that shows a title and no time
# (DEFAULT_CHIP_MIN), and the margin covers that. Vision is the fallback
# when the axis can't be read (a zoomed screenshot, a month view).
# --------------------------------------------------------------------------- #
_HOUR_LBL = re.compile(r"^(\d{1,2})\s?([AP]M)$", re.I)
_DAY_HDR = re.compile(r"^(SUN|MON|TUE|WED|THU|FRI|SAT)$", re.I)
_T = r"(\d{1,2})(?::(\d{2}))?\s*([ap]m?)?"
_RANGE_T = re.compile(_T + r"\s*[–\-]\s*" + _T, re.I)
_ONE_T = re.compile(r"(?<![\d:])(\d{1,2})(?::(\d{2}))\s*([ap]m?)?|(?<![\d:])(\d{1,2})\s*([ap]m)", re.I)
DEFAULT_CHIP_MIN = 45
UNKNOWN_CHIP_MIN = 30


def _hm(h, m, ap, hint=None):
    h = int(h); m = int(m or 0); ap = (ap or hint or "").lower()[:1]
    if ap == "p" and h < 12:
        h += 12
    if ap == "a" and h == 12:
        h = 0
    if not ap and h < 7:
        h += 12
    return h % 24, m


def axis(lines):
    """(slope, intercept) mapping y% -> hour (float), from the hour labels;
    None with fewer than three. Pure."""
    pts = []
    for l in lines:
        m = _HOUR_LBL.match(l["text"].strip())
        if m:
            h, _ = _hm(m.group(1), 0, m.group(2))
            pts.append((l["y"] + l["h"] / 2.0, h))
    if len(pts) < 3:
        return None
    n = len(pts)
    sx = sum(p[0] for p in pts); sy = sum(p[1] for p in pts)
    sxx = sum(p[0] * p[0] for p in pts); sxy = sum(p[0] * p[1] for p in pts)
    den = n * sxx - sx * sx
    if abs(den) < 1e-9:
        return None
    a = (n * sxy - sx * sy) / den
    b = (sy - a * sx) / n
    return a, b


def columns(lines):
    """[(day_name, x_left, x_right)] from the day headers. Pure."""
    hdr = sorted(((l["x"] + l["w"] / 2.0, l["text"].strip().title(), l["y"]) for l in lines
                  if _DAY_HDR.match(l["text"].strip())), key=lambda t: t[0])
    if len(hdr) < 3:
        return [], 0.0
    cols = []
    for i, (cx, name, y) in enumerate(hdr):
        left = (hdr[i - 1][0] + cx) / 2 if i else cx - (hdr[i + 1][0] - cx) / 2
        right = (cx + hdr[i + 1][0]) / 2 if i + 1 < len(hdr) else cx + (cx - hdr[i - 1][0]) / 2
        cols.append((name, left, right))
    header_y = max(h[2] for h in hdr)
    return cols, header_y


def chip_blocks(lines, ax, cols, header_y):
    """OCR lines -> {day: [(start_time, end_time)]}. Pure."""
    a, b = ax
    out = {name: [] for name, _, _ in cols if name not in ("Sun", "Sat")}
    for l in lines:
        t = l["text"].strip()
        if _HOUR_LBL.match(t) or _DAY_HDR.match(t) or l["y"] <= header_y + 0.5:
            continue
        cx = l["x"] + l["w"] / 2.0
        day = next((n for n, lo, hi in cols if lo <= cx < hi), None)
        if day is None or day not in out:
            continue
        y_top = l["y"]
        hour_at_top = a * y_top + b
        blocks = []
        m = _RANGE_T.search(t)
        if m:
            h1, m1, a1, h2, m2, a2 = m.groups()
            H2, M2 = _hm(h2, m2, a2); H1, M1 = _hm(h1, m1, a1, hint=a2)
            s_, e_ = dt.time(H1, M1), dt.time(H2, M2)
            if e_ > s_:
                blocks.append((s_, e_))
        else:
            for m in _ONE_T.finditer(t):
                g = m.groups()
                if g[0] is not None:
                    H, M = _hm(g[0], g[1], g[2])
                else:
                    H, M = _hm(g[3], 0, g[4])
                s_ = dt.datetime.combine(dt.date.today(), dt.time(H, M))
                # sanity: the written time must sit near where the chip is drawn
                if abs((H + M / 60.0) - hour_at_top) > 1.5:
                    continue
                blocks.append((s_.time(), (s_ + dt.timedelta(minutes=DEFAULT_CHIP_MIN)).time()))
        if not blocks and re.search(r"[A-Za-z]{3}", t):
            # a title with no time: start where it is drawn, short by default
            hh = max(0, min(23.99, hour_at_top))
            H = int(hh); M = int(round((hh - H) * 60 / 5.0) * 5)
            if M >= 60:
                H, M = H + 1, 0
            s_ = dt.datetime.combine(dt.date.today(), dt.time(min(H, 23), M))
            blocks.append((s_.time(), (s_ + dt.timedelta(minutes=UNKNOWN_CHIP_MIN)).time()))
        out[day] += blocks
    for day in out:
        out[day] = _merge(sorted(out[day]))
    return out


def _merge(blocks):
    merged = []
    for s_, e_ in blocks:
        if merged and s_ <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e_))
        else:
            merged.append((s_, e_))
    return merged


def read_screen_ocr(log=print, lines=None):
    """(days, is_calendar) from OCR geometry. is_calendar is False when the
    hour axis or the day headers can't be read — then vision decides."""
    if lines is None:
        import highlight
        lines = highlight.read_screen(log=log)
    ax = axis(lines)
    cols, header_y = columns(lines)
    if ax is None or not cols:
        return {}, False
    return chip_blocks(lines, ax, cols, header_y), True


def read_screen(client, model, log=print):
    """(days, week_start, is_calendar). OCR geometry first — exact times from
    the chips' own text — and one vision call only when the grid can't be
    read that way."""
    days, ok = read_screen_ocr(log=log)
    if ok:
        log(f"[screencal] read by OCR: " + ", ".join(f"{d} {len(b)}" for d, b in days.items()))
        return days, None, True
    return read_screen_vision(client, model, log=log)


def read_screen_vision(client, model, log=print):
    """(days, week_start, is_calendar). One vision call on a fresh capture."""
    import hands
    path = hands.screenshot()
    if not path:
        return {}, None, False
    try:
        from google.genai import types
        with open(path, "rb") as f:
            img = types.Part.from_bytes(data=f.read(), mime_type="image/png")
        import providers
        text, _m = providers.generate_text(client, "heavy", [img, _PROMPT], log=log)
        return parse(text)
    except Exception as e:
        log(f"[screencal] vision failed: {e}")
        return {}, None, False
    finally:
        try:
            import os
            os.remove(path)
        except OSError:
            pass
