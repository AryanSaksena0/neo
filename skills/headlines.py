"""Skill: headlines — 'what's in the news?' -> Neo reads the top stories.
Also the reference implementation of the skill contract (see skills.py)."""

import re

NAME = "headlines"
DESCRIPTION = ("Reads the top news headlines. Triggers: 'what's in the news', "
               "'give me the headlines', 'news today'.")

_RX = re.compile(r"\b(?:headlines|the news|news today|today s news|whats in the news)\b")


def _low(text):
    return re.sub(r"[^a-z ]", " ", text.lower())


def matches(text):
    low = _low(text)
    if "newsletter" in low:
        return False
    return bool(_RX.search(low))


def handle(text, ctx):
    results = ctx.search("top news headlines right now", 6)
    titles = []
    for r in results:
        t = re.sub(r"\s*[-|–].{0,40}$", "", r.get("title", "")).strip()
        if len(t) > 15 and t not in titles:
            titles.append(t)
        if len(titles) == 3:
            break
    if not titles:
        return "I couldn't pull headlines just now. Try me again in a minute."
    return "Here's what's happening. " + ". ".join(titles) + "."


def self_test():
    ok = matches("give me the headlines") and matches("what's in the news?")
    ok = ok and matches("news today")
    ok = ok and not matches("draft the newsletter")
    ok = ok and not matches("what should I do this week")
    # handle() logic with search stubbed — no network in tests
    class _Ctx:
        def search(self, q, n):
            return [{"title": "Something long enough happened in the world today", "url": "u"}]
    out = handle("headlines", _Ctx())
    return ok and "happening" in out.lower()
