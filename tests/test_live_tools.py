"""test_live_tools.py — the tools, RUN, against the real Mac and the real
network. Not a string check: each one is called the way the model calls it
and judged on what came back.

Skips anything with a side effect on the person (sending, typing into an
app, opening windows) and anything that needs a prompt. Needs a Gemini key
for the model-backed ones; those are marked and cost a handful of requests.

Run: cd ~/Desktop/neo && .venv/bin/python test_live_tools.py
"""
# Suites live in tests/ but RUN from the repo root, so that the modules
# under test import and open("neo.py") still resolves. This makes the
# import work either way, so a suite can also be run directly.
import os as _bootstrap_os, sys as _bootstrap_sys
_bootstrap_sys.path.insert(0, _bootstrap_os.path.dirname(
    _bootstrap_os.path.dirname(_bootstrap_os.path.abspath(__file__))))

import os
import re
import sys
import time

sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import agent
import providers
import workflows

FAILED, TIMES = [], {}


def check(name, cond, note=""):
    print(("PASS - " if cond else "FAIL - ") + name + (f"  [{note}]" if note else ""))
    if not cond:
        FAILED.append(name)


def timed(name, fn, *a, **k):
    t = time.time()
    try:
        out = fn(*a, **k)
    except Exception as e:
        out = f"EXCEPTION {type(e).__name__}: {e}"
    TIMES[name] = time.time() - t
    return out


# ---- bind a real client, like neo.py does ----
keys = providers.gemini_keys()
client = None
if keys:
    from google import genai
    client = genai.Client(api_key=keys[0])
    _p, chat = providers.resolve("chat", client, print)
    agent.bind(client=client, model=chat or "gemini-3.8-flash", logger=print)
    print(f"model: {chat}")

# ---- no model needed ----
out = timed("get_time", agent.get_time)
check("get_time: a real clock", re.search(r"\d", out) and ("AM" in out or "PM" in out or ":" in out), out[:60])
out = timed("calculate", agent.calculate, "17% of 2350")
check("calculate: 17% of 2350 = 399.5", "399.5" in out, out[:60])
out = timed("find_file", agent.find_file, "docs/FEATURES.md")
check("find_file: finds a file that exists", "docs/FEATURES.md" in out, out[:80])
out = timed("what_did_i_copy", agent.what_did_i_copy)
check("what_did_i_copy: answers (clipboard may be empty)", isinstance(out, str) and out, out[:60])
out = timed("get_weather", agent.get_weather, "")
check("get_weather: a temperature comes back", re.search(r"\d+\s*(°|degrees)", out) is not None, out[:80])
out = timed("search_web", agent.search_web, "who won the 2024 US presidential election")
check("search_web: returns content, not an error", len(out) > 80 and "NOTHING HAPPENED" not in out, out[:80].replace("\n", " "))
out = timed("read_webpage", agent.read_webpage, "https://example.com")
check("read_webpage: reads a page", "example" in out.lower() and "illustrative" in out.lower() or "documentation" in out.lower(), out[:60].replace("\n", " "))
out = timed("check_market", agent.check_market, "Apple")
check("check_market: a price", re.search(r"\$?\d{2,4}\.\d{2}", out) is not None, out[:80])
out = timed("connect_service", agent.connect_service, "status")
check("connect_service: lists connections", "CONNECTIONS" in out and "Calendar" in out, "")
n = timed("set_volume", workflows.set_volume, "50")
check("set_volume: sets and reads back", isinstance(n, int) and 0 <= n <= 100, f"now {n}")
words = timed("copy_screen_text", workflows.screen_text_to_clipboard)
check("copy_screen_text: OCR reads the screen into the clipboard", words > 5, f"{words} words")
brief = timed("morning_brief", workflows.gather_brief)
check("morning_brief: assembles without crashing, says what it can't see", "It's" in workflows.format_brief(brief), workflows.format_brief(brief)[:100])
import gsuite
busy, source = timed("busy_week", gsuite.busy_week, gsuite.next_monday(), 5)
check("busy_week: a real source or an honest None", source in ("mac", "google", None), f"source={source} blocks={len(busy)}")
import heavy
hs = timed("heavy.status", heavy.status)
check("heavy engine: one is detected", hs["engine"] in ("claude", "codex"), str(hs))

# ---- model-backed ----
if client:
    out = timed("think_hard", agent.think_hard, "Is it better to revise the night before or the morning of a test? One decision.")
    check("think_hard: the brain answers with a decision", "THE BRAIN SAYS" in out and len(out) > 60, out[:90])
    out = timed("look_at_screen", agent.look_at_screen, "In one sentence, what app is in front?")
    check("look_at_screen: describes the screen", len(out) > 20 and "couldn't" not in out.lower(), out[:90])
    import remind
    rem, problem = timed("remind.parse", remind.parse, "remind me to call mum at six", client)
    check("remind.parse: a real time from the model", problem is None and rem and rem["when"].hour == 18, str(rem))
    rem, problem = timed("remind.parse-vague", remind.parse, "remind me later to call mum", client)
    check("remind.parse: 'later' comes back as the one clarifying case", problem == "vague", str(problem))
    import agenda
    ev, problem = timed("agenda.parse", agenda.parse, "lunch with Priya next Thursday at one", client)
    check("agenda.parse: next Thursday 13:00", problem is None and ev and ev["start"].hour == 13 and ev["start"].weekday() == 3, str(ev and ev["start"]))
    out = timed("free_time_on_screen", agent.free_time_on_screen, 15, "school day")
    check("free_time_on_screen: reads the screen or says it isn't a calendar", "FROM THE SCREEN" in out or "NO GAP" in out or "doesn't look like a calendar" in out, out[:100])

print()
slow = sorted(TIMES.items(), key=lambda kv: -kv[1])[:6]
print("slowest: " + ", ".join(f"{k} {v:.1f}s" for k, v in slow))
if FAILED:
    print(f"{len(FAILED)} FAILED:")
    for f in FAILED:
        print("  -", f)
    sys.exit(1)
print("Live tools clean.")
