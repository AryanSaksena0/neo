"""
test_claude_live.py — does the Claude Code fallback ACTUALLY work?

Everything else simulates Claude with a fake CLI. This runs the REAL thing on
your Mac + Max plan, so it's the only true proof of the fallback. It dispatches
a couple of real jobs through Neo's actual ClaudeBridge and checks the results.

  cd ~/Desktop/neo && source .venv/bin/activate && python test_claude_live.py

Costs ~2 Claude jobs on your Max plan (NOT Gemini). Safe: the jobs only read
this folder / write one temp file. Takes a few minutes.
"""

# Suites live in tests/ but RUN from the repo root, so that the modules
# under test import and open("neo.py") still resolves. This makes the
# import work either way, so a suite can also be run directly.
import os as _bootstrap_os, sys as _bootstrap_sys
_bootstrap_sys.path.insert(0, _bootstrap_os.path.dirname(
    _bootstrap_os.path.dirname(_bootstrap_os.path.abspath(__file__))))

import os
import re
import time

import claude_bridge

RESULTS = []


def wait_for(bridge, minutes=6):
    t0 = time.time()
    while bridge.current is not None and time.time() - t0 < minutes * 60:
        left = int(minutes * 60 - (time.time() - t0))
        print(f"   ...working ({left}s budget left)", end="\r")
        time.sleep(8)
    print(" " * 50, end="\r")
    return bridge.last


def main():
    cli = claude_bridge.find_cli()
    print("claude CLI:", cli or "NOT FOUND")
    if not cli:
        print("\nCan't test the fallback — Claude Code isn't installed / on PATH.")
        print("Fix: npm install -g @anthropic-ai/claude-code, then run `claude` and log in.")
        return
    print("projects:", claude_bridge.projects())
    print("permission mode:", claude_bridge.PERMISSION_MODE, "(must allow execution)\n")

    # ---- TEST 1: can Claude actually RUN A COMMAND? (the execution fix) ------
    print("TEST 1 — Claude runs a shell command and reports the result...")
    got = {}
    b = claude_bridge.ClaudeBridge(on_done=lambda i: got.update(i))
    b.on_progress = lambda t: print("   ", t)
    real_count = len([f for f in os.listdir(".") if f.endswith(".py")])
    ack = b.start("Run the shell command  ls *.py | wc -l  in this directory and "
                  "reply with ONLY that number, nothing else.", "neo")
    print("   ack:", ack)
    if "on it" not in ack.lower() and "claude" not in ack.lower():
        RESULTS.append(("dispatch", False, ack));
    else:
        last = wait_for(b)
        summ = (last or {}).get("summary", "")
        found = re.search(r"\b(\d{1,3})\b", summ)
        n = int(found.group(1)) if found else -1
        okrun = last and last["ok"]
        okcount = abs(n - real_count) <= 3   # allow a little drift (new files, etc.)
        print(f"   result ok={okrun}  Claude said {n}, actual .py count ~{real_count}")
        print(f"   summary: {summ[:200]}")
        RESULTS.append(("job completes", bool(okrun), summ[:120]))
        RESULTS.append(("Claude actually ran the command (count matches)", okcount,
                        f"said {n} vs {real_count}"))

    # ---- TEST 2: reads + reasons about the codebase -------------------------
    print("\nTEST 2 — Claude reads the repo and answers a real question...")
    got2 = {}
    b2 = claude_bridge.ClaudeBridge(on_done=lambda i: got2.update(i))
    b2.on_progress = lambda t: print("   ", t)
    ack2 = b2.start("Read skills.py and tell me in one sentence what a Neo skill's "
                    "self_test function is for.", "neo")
    print("   ack:", ack2)
    last2 = wait_for(b2)
    summ2 = (last2 or {}).get("summary", "")
    okread = last2 and last2["ok"] and re.search(r"self.?test|load|pass|health|valid", summ2, re.I)
    print(f"   summary: {summ2[:200]}")
    RESULTS.append(("Claude read + understood the code", bool(okread), summ2[:120]))

    # ---- verdict ------------------------------------------------------------
    print("\n" + "=" * 54)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    for name, ok, detail in RESULTS:
        print(("PASS" if ok else "FAIL"), "-", name, "" if ok else f":: {detail}")
    if passed == len(RESULTS) and RESULTS:
        print(f"\n{passed}/{len(RESULTS)} — the Claude Code fallback WORKS end to end. "
              "Neo can hand off real work and get real results back.")
    else:
        print(f"\n{passed}/{len(RESULTS)} passed. If jobs failed: check `claude` is logged "
              "into YOUR Max account (run `claude` in a terminal, /login), and that "
              "permission mode allows execution (bypassPermissions).")


if __name__ == "__main__":
    main()
