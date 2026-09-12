"""convo.py — Neo's short-term conversation memory.

The persistent Gemini chat holds context WITHIN a session, but Neo restarts
constantly (file sync, crashes, relaunches) and a fresh chat forgets everything —
so "any update on that?" or "keep going" fell on the floor after a restart. Here
we mirror the last few exchanges to disk and reseed the chat on boot, so the
thread survives.

This is the CONVERSATION (recent turns), NOT the long-term facts about the user
(those live in memory.py). Keep the two separate: the conversation is always-on
and small; the facts are recalled only when relevant. Pure + stdlib so it's
testable; neo.py turns these dicts into Gemini history at chat creation."""

import json
import os
import time

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "convo.json")
KEEP_TURNS = 16          # exchanges (a user + a model entry) reseeded on boot


def trim(turns, keep=KEEP_TURNS):
    """Keep the last `keep` exchanges. History must begin with a user turn
    (Gemini requirement), so drop any leading model entries. Pure."""
    kept = turns[-keep * 2:] if turns else []
    while kept and kept[0].get("role") != "user":
        kept = kept[1:]
    return kept


def for_replay(turns):
    """History safe to seed a NEW live session with. Pure.

    A turn only gets recorded as a pair when Neo actually answered. When a turn
    fails — a crash, a timeout, a socket reset — `record` still stores what
    the user said, so the tail of the conversation is a question with no reply.

    Seed that into a fresh session and the model does the reasonable thing with
    an unanswered question: it answers it. the user says "hello", the model sees a
    stale question sitting underneath, and replies to THAT — with whatever tools
    that old question needed. It reads as Neo answering something from hours
    ago, because it is.

    So replay only COMPLETE exchanges.
    Only stripping the TAIL was not enough, and the logs proved it: turns are
    committed one at a time, so `user, user` pairs appear all through the
    history, not just at the end. The one Neo answered from hours ago sat at
    position 1 of 6 — nowhere near the tail. So keep only COMPLETE adjacent
    pairs, and the result is strictly alternating by construction.
    """
    turns = list(turns or ())
    kept, i = [], 0
    while i < len(turns):
        nxt = turns[i + 1] if i + 1 < len(turns) else None
        if turns[i].get("role") == "user" and nxt and nxt.get("role") == "model":
            kept.append(turns[i])
            kept.append(nxt)
            i += 2
        else:
            i += 1          # an unanswered question, or a stray reply: drop it
    return kept


def record(turns, user_text, reply, now=None):
    """Append one exchange (user then model), trimmed. Empty sides are skipped.
    Pure — returns a new list, doesn't mutate the input.

    Each turn is stamped so stale ones can be dropped later. A conversation
    from yesterday reseeded into this morning's first question is not memory,
    it is Neo picking up a thread nobody is on any more.
    """
    out = list(turns)
    stamp = time.time() if now is None else now
    u = (user_text or "").strip()
    r = (reply or "").strip()
    if u:
        out.append({"role": "user", "text": u, "at": stamp})
    if r:
        out.append({"role": "model", "text": r, "at": stamp})
    return trim(out)


# How long a conversation stays warm. Past this it is history, not context:
# reseeding it makes Neo answer into a thread the user has long since left, and
# the first question of the day arrives on top of last night's draft strategy.
# Long enough to survive a restart, lunch, or a lesson; short enough that
# "morning" doesn't land in the middle of yesterday.
STALE_AFTER_S = float(os.getenv("NEO_CONVO_STALE_S", str(3 * 3600)))


def fresh(turns, now=None, stale_after=STALE_AFTER_S):
    """Only the turns still worth carrying into a new session. Pure.

    Turns with no stamp are from before this existed; they are treated as old,
    because the alternative is keeping them forever.
    """
    now = time.time() if now is None else now

    def age(t):
        """Seconds since this turn, or forever if it can't be read."""
        try:
            return now - float(t.get("at") or 0)
        except (TypeError, ValueError):
            return float("inf")

    kept = [t for t in turns if isinstance(t, dict) and age(t) <= stale_after]
    while kept and kept[0].get("role") != "user":
        kept = kept[1:]
    return kept


def _clean(turns):
    ok = []
    for t in turns:
        if isinstance(t, dict) and t.get("text") and t.get("role") in ("user", "model"):
            # The stamp comes through. Dropping it here is what would make
            # every turn look ancient the moment it was written to disk, so
            # fresh() would throw away the conversation on every restart.
            kept = {"role": t["role"], "text": str(t["text"])}
            try:
                if t.get("at"):
                    kept["at"] = float(t["at"])
            except (TypeError, ValueError):
                pass
            ok.append(kept)
    return ok


def load(keep=KEEP_TURNS):
    """Recent exchanges from disk, sanitized and trimmed. [] on any problem."""
    try:
        with open(PATH, encoding="utf-8") as f:
            data = json.load(f)
        turns = data.get("turns", []) if isinstance(data, dict) else []
        # Age it out BEFORE trimming: a conversation that ended hours ago is
        # history, not context, and reseeding it makes Neo pick up a thread
        # the user left last night.
        return trim(fresh(_clean(turns)), keep)
    except Exception:
        return []


def save(turns, keep=KEEP_TURNS):
    try:
        with open(PATH, "w", encoding="utf-8") as f:
            json.dump({"turns": trim(_clean(turns), keep)}, f, indent=1)
        return True
    except Exception as e:
        print(f"[convo] save failed: {e}")
        return False
