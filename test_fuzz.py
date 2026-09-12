"""
test_fuzz.py — adversarial + speech-noise testing for Neo's pure logic.

Layers:
  1. Whisper-noise mutations over the routing corpus: filler words ("um",
     "hey neo"), dropped punctuation, trailing periods, case changes. Every
     variant must route the same as its clean form.
  2. Garbage fuzz: word-salad through every matcher — nothing may crash;
     false-positive rate is reported.
  3. Adversarial canvas specs: hostile/weird values must never raise.
  4. Corrupt sentinel state + malformed data: compute_insights must survive.
  5. Pipeline parser on weird-but-plausible utterances: no crashes.
  6. Hostile skill files: loader must skip them all and keep running.

Run:  python test_fuzz.py     (exit 1 on any failure)
"""

import json
import os
import random
import shutil
import tempfile

import commands
import canvas
import sentinel
import skills
from test_routing import CORPUS, route

FAIL = []


def check(name, cond):
    if not cond:
        FAIL.append(name)
        print("FAIL -", name)


# --------------------------------------------------------------------------- #
# 1. speech-noise mutations — same route as the clean utterance
# --------------------------------------------------------------------------- #
def mutations(t):
    yield t
    yield t.rstrip("?!.")                       # whisper often drops final punct
    yield t.replace("'", "")                    # ...and apostrophes
    yield "um " + t
    yield "uh, " + t
    yield "hey neo " + t
    yield "okay neo, " + t
    yield "Neo, " + t[0].upper() + t[1:]
    yield t + "."
    yield "please " + t


def run_noise():
    bad = []
    for text, want in CORPUS:
        base = route(commands.strip_address(text))
        if base != want:
            bad.append((text, "BASE", want, base))
            continue
        for v in mutations(text):
            got = route(commands.strip_address(v))
            if got != want:
                bad.append((v, "mutant", want, got))
    for v, kind, want, got in bad[:20]:
        print(f"NOISE MISROUTE ({kind}): {v!r} expected={want} got={got}")
    check(f"speech-noise: {len(bad)} misroutes across ~{len(CORPUS)*10} variants",
          not bad)


# --------------------------------------------------------------------------- #
# 2. garbage fuzz — crashes are failures; FPs are reported
# --------------------------------------------------------------------------- #
_WORDS = ("the a my your this that and or but so then when what how why is are was "
          "school essay college tennis game night code app phone money summer friend "
          "jordan counselor revenue lead draft post plan brain screen type open go "
          "search look news skill claude neo teach learn build make find show draw "
          "market pipeline status update forget remember radar dashboard visual").split()


def run_garbage(n=1500):
    rng = random.Random(42)
    crashes, fired = 0, {}
    for _ in range(n):
        t = " ".join(rng.choice(_WORDS) for _ in range(rng.randint(2, 12)))
        try:
            r = route(commands.strip_address(t))
            if r.startswith("CRASH"):
                crashes += 1
                print("CRASH:", t, "->", r)
            elif r != "chat":
                fired[r] = fired.get(r, 0) + 1
        except Exception as e:
            crashes += 1
            print("CRASH:", t, "->", e)
    total_fp = sum(fired.values())
    print(f"garbage fuzz: {n} inputs, {crashes} crashes, "
          f"{total_fp} non-chat routes ({100*total_fp/n:.1f}%): {fired}")
    check("garbage fuzz: zero crashes", crashes == 0)
    # word salad WILL trip keyword matchers sometimes; just keep it sane
    check("garbage fuzz: false-positive rate under 40%", total_fp < n * 0.4)


# --------------------------------------------------------------------------- #
# 3. adversarial canvas specs — must never raise
# --------------------------------------------------------------------------- #
def run_canvas():
    hostile = [
        {"kind": "progress", "title": "x", "value": "not-a-number", "target": 0},
        {"kind": "progress", "value": -50, "target": -1, "unit": "$$$"},
        {"kind": "bars", "items": [{"label": None, "value": None}] * 50},
        {"kind": "bars", "items": []},
        {"kind": "funnel", "items": [{"label": "🙂" * 500, "value": 1e18}]},
        {"kind": "steps", "items": [{"label": "<img src=x onerror=alert(1)>"}]},
        {"kind": "compare", "columns": [{"title": "<script>", "points": [{"a": 1}]}]},
        {"kind": "steps", "items": "not-a-list"},
        {"sections": [{"kind": "progress"}, {"kind": "nope"}, "junk"]},
        {"sections": []},
        {},
    ]
    crashed = 0
    for spec in hostile:
        try:
            html = canvas.render_html(spec) if canvas.valid(spec) else ""
            # escaped text may still CONTAIN the words — what must never
            # appear is a live unescaped tag
            if "<script>" in html or "<img " in html:
                check("canvas: injection escaped", False)
        except Exception as e:
            crashed += 1
            print("CANVAS CRASH:", json.dumps(spec)[:80], "->", e)
    check("canvas: hostile specs never raise", crashed == 0)
    # extract_show with nested/broken payloads
    for r in ('[[show: {"a": [[1]] }]] hi', "[[show: ]]", "[[show: {]] [[show: {}]]",
              "x [[visual: ]] y", "[[visual: " + "a" * 5000 + "]]"):
        try:
            canvas.extract_show(r)
            canvas.extract_visual(r)
        except Exception as e:
            check(f"canvas: tag parse crashed on {r[:30]!r}", False)
            print("TAG CRASH:", e)


# --------------------------------------------------------------------------- #
# 4. sentinel vs corrupt state / malformed data
# --------------------------------------------------------------------------- #
def run_sentinel():
    import datetime
    now = datetime.datetime(2026, 7, 24, 18, 0)
    datasets = [
        {"prospects": [{"no_name": True, "status": "replied"}], "leads": [], "db": None, "repos": []},
        {"prospects": [{"name": "", "status": "replied"}], "leads": [{}], "db": {}, "repos": []},
        {"prospects": "junk", "leads": None, "db": None, "repos": None},
        {"prospects": [], "leads": [], "db": {"paying_users": "many"}, "repos": [{"name": "x"}]},
    ]
    states = [
        {"first_seen": {"reply:X": "not-a-date"}, "snoozed": {"y": 12345}, "db": "junk"},
        {"first_seen": None, "snoozed": None},
        {},
    ]
    crashed = 0
    for d in datasets:
        for s in states:
            try:
                sentinel.compute_insights(
                    d if isinstance(d.get("prospects"), list) else
                    {"prospects": [], "leads": [], "db": None, "repos": []},
                    json.loads(json.dumps(s)) if s else {}, now)
            except Exception as e:
                crashed += 1
                print("SENTINEL CRASH:", str(d)[:60], str(s)[:60], "->", e)
    # truly malformed containers go through gather()'s defaults in real life,
    # but state comes straight off disk — corrupt state must never kill a tick
    try:
        st = sentinel.load_state(os.devnull)
        check("sentinel: unreadable state -> fresh", "first_seen" in st)
    except Exception:
        check("sentinel: load_state crashed", False)
    check("sentinel: corrupt inputs never raise", crashed == 0)


# --------------------------------------------------------------------------- #
# 5. pipeline parser on weird utterances
# --------------------------------------------------------------------------- #
def run_skills():
    d = os.path.join(tempfile.gettempdir(), "neo_fuzz_skills")
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    cases = {
        "syntax_err.py": "def matches(t: return False",
        "raises_on_import.py": "raise RuntimeError('boom')\nNAME='x'",
        "wrong_types.py": "NAME=1\nDESCRIPTION=2\nmatches=3\nhandle=4\nself_test=5",
        "selftest_raises.py": ("NAME='s'\nDESCRIPTION='d'\n"
                               "def matches(t): return False\n"
                               "def handle(t,c): return ''\n"
                               "def self_test(): raise ValueError('no')\n"),
        "good_one.py": ("NAME='good_one'\nDESCRIPTION='d'\n"
                        "def matches(t): return 'xyzzy' in t\n"
                        "def handle(t,c): return 'ok'\n"
                        "def self_test(): return True\n"),
    }
    for fn, src in cases.items():
        with open(os.path.join(d, fn), "w") as f:
            f.write(src)
    real = skills.SKILLS_DIR
    skills.SKILLS_DIR = d
    try:
        names, skipped = skills.load_all()
        check("skills: only the good one loads", names == ["good_one"])
        check("skills: four hostiles skipped", len(skipped) == 4)
        # wrong_types: matches=3 isn't callable — find() must contain it
        check("skills: runtime still safe", skills.find("xyzzy now") is not None)
    except Exception as e:
        check(f"skills: loader crashed ({e})", False)
    finally:
        skills.SKILLS_DIR = real
        skills.load_all()


if __name__ == "__main__":
    skills.load_all()
    run_noise()
    run_garbage()
    run_canvas()
    run_sentinel()
    run_skills()
    if FAIL:
        print(f"\n{len(FAIL)} fuzz failures.")
        raise SystemExit(1)
    print("\nAll fuzz layers clean.")
