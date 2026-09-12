"""
metrics.py — Neo measures itself.

Every turn logs timing to neo_metrics.jsonl: how long transcription took,
when the first sound came back, total turn time. "How fast are you?" gets a
real answer from real numbers instead of vibes, and regressions show up in
data instead of feel. (Voice-AI engineering guidance is blunt about this:
instrument everything — partial timings, first-sound latency — or you're
debugging blind.)

Pure module: no audio/GUI imports, fully testable.
"""

import datetime
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "neo_metrics.jsonl")
MAX_BYTES = 400_000     # ~a few thousand turns; oldest half dropped beyond this


def record(entry, path=None):
    """Append one turn's timings. Never raises."""
    p = path or PATH
    try:
        entry = dict(entry)
        entry.setdefault("ts", datetime.datetime.now().isoformat(timespec="seconds"))
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        if os.path.getsize(p) > MAX_BYTES:
            with open(p, encoding="utf-8") as f:
                lines = f.readlines()
            with open(p, "w", encoding="utf-8") as f:
                f.writelines(lines[len(lines) // 2:])
    except OSError:
        pass


def _load(path=None, limit=400):
    try:
        with open(path or PATH, encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
        return out
    except OSError:
        return []


def _median(vals):
    vals = sorted(v for v in vals if isinstance(v, (int, float)) and v >= 0)
    return vals[len(vals) // 2] if vals else None


def _sec(ms):
    return f"{ms / 1000:.1f}" if ms is not None else "unknown"


def summarize(path=None):
    """Spoken speed report from real logged turns."""
    rows = _load(path)
    if len(rows) < 3:
        return "Not enough turns logged yet to give you honest numbers. Talk to me more."
    today = datetime.date.today().isoformat()
    todays = [r for r in rows if str(r.get("ts", "")).startswith(today)]
    sample = todays if len(todays) >= 3 else rows
    which = "today" if sample is todays else f"the last {len(sample)} turns"
    med_total = _median([r.get("total_ms") for r in sample])
    med_stt = _median([r.get("stt_ms") for r in sample])
    med_first = _median([r.get("first_sound_ms") for r in sample])
    parts = [f"Over {which}: median turn {_sec(med_total)} seconds"]
    if med_first is not None:
        parts.append(f"first sound in {_sec(med_first)}")
    if med_stt is not None:
        parts.append(f"transcription {_sec(med_stt)}")
    n_today = len(todays)
    tail = f" That's across {n_today} turn{'s' if n_today != 1 else ''} today." if n_today else ""
    return ", ".join(parts) + "." + tail
