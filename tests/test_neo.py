"""
import sys
test_neo.py — fast checks for Neo's pure logic (no audio/ML/GUI needed).
Run:  python test_neo.py
"""

# Suites live in tests/ but RUN from the repo root, so that the modules
# under test import and open("neo.py") still resolves. This makes the
# import work either way, so a suite can also be run directly.
import os as _bootstrap_os, sys as _bootstrap_sys
_bootstrap_sys.path.insert(0, _bootstrap_os.path.dirname(
    _bootstrap_os.path.dirname(_bootstrap_os.path.abspath(__file__))))

import os
import json as _json
import tempfile

import commands
import memory
import brain


def check(name, cond):
    print(("PASS" if cond else "FAIL"), "-", name)
    assert cond, name


# ---- speech sanitizer ----
check("strips markdown", commands.clean_for_speech("**bold** _it_ `code`") == "bold it code.")
check("urls become 'a link'", "a link" in commands.clean_for_speech("see https://the project.ai now"))
check("keeps hyphens", commands.clean_for_speech("voice-first, low-latency") == "voice-first, low-latency.")
# em dash / semicolon / colon become pauses Kokoro actually honors; times survive
check("em dash -> pause", commands.clean_for_speech("On it — working on that") == "On it, working on that.")
check("semicolon -> pause", commands.clean_for_speech("ship it; move on") == "ship it, move on.")
check("colon -> pause", commands.clean_for_speech("three things: a and b") == "three things, a and b.")
check("clock time survives", "3:30" in commands.clean_for_speech("meet at 3:30 today"))
check("ellipsis -> pause", commands.clean_for_speech("I think... maybe") == "I think, maybe.")
check("empty stays empty", commands.clean_for_speech("") == "")
check("strips emoji", "rocket" in commands.clean_for_speech("rocket 🚀").lower())

# ---- intent matching ----
check("brain: show me your brain", commands.wants_brain("Neo show me your brain"))
check("brain: open memory", commands.wants_brain("open your memory"))
check("brain: negative", not commands.wants_brain("what's the weather"))
check("recap: what do you know about me", commands.wants_recap("what do you know about me"))
check("recap: negative", not commands.wants_recap("tell me a joke"))
check("forget: forget that", commands.wants_forget("forget that"))
check("forget: negative", not commands.wants_forget("remember that I like tennis"))
check("lookup: look up", commands.wants_lookup("look up the latest on OpenAI"))
check("lookup: whats the latest", commands.wants_lookup("what's the latest with the stock market"))
check("lookup: negative", not commands.wants_lookup("tell me a joke"))

# ---- usage counter (isolated temp file) ----
commands.USAGE_PATH = os.path.join(tempfile.gettempdir(), "neo_usage_test.json")
if os.path.exists(commands.USAGE_PATH):
    os.remove(commands.USAGE_PATH)
check("usage starts full", commands.remaining_today() == commands.DAILY_LIMIT)
commands.bump_usage()
commands.bump_usage()
check("usage decrements", commands.remaining_today() == commands.DAILY_LIMIT - 2)
check("quota error detected", commands.is_quota_error(Exception("429 RESOURCE_EXHAUSTED")))
check("non-quota error ignored", not commands.is_quota_error(Exception("connection reset")))

# ---- memory: remember / forget / summarize ----
# Redirect saves to a temp file so tests NEVER touch the real memory.json.
memory.MEMORY_PATH = os.path.join(tempfile.gettempdir(), "neo_memory_test.json")
if os.path.exists(memory.MEMORY_PATH):
    os.remove(memory.MEMORY_PATH)

spoken, facts = memory.extract_remember("Got it. [[remember: the user likes F1]]")
check("remember parsed", facts == ["the user likes F1"] and spoken == "Got it.")
mem = {"facts": []}
memory.add_fact(mem, "likes tennis")
memory.add_fact(mem, "likes tennis")            # dup
check("dedup works", len(mem["facts"]) == 1)
memory.add_fact(mem, "builds the project")
check("forget_last returns text", memory.forget_last(mem) == "builds the project")
check("forget_last shrinks", len(mem["facts"]) == 1)
check("summarize mentions count", "1" in memory.summarize(mem))
check("summarize handles empty", "yet" in memory.summarize({"facts": []}).lower())

# ---- brain graph integrity ----
g = brain.build_graph({"facts": [{"text": "Halcyon is an AI email assistant"},
                                 {"text": "the project helps students write"},
                                 {"text": "the user plays tennis at school"}]})
ids = {n["id"] for n in g["nodes"]}
check("brain has core", "core" in ids)
check("no dangling links", all(l["source"] in ids and l["target"] in ids for l in g["links"]))
check("clusters created", any(n["kind"] == "hub" for n in g["nodes"]))

# ---- sentinel: Neo noticing things (pure logic, no files/DB/git) ----
import datetime as _dt
import sentinel

_NOW = _dt.datetime(2026, 7, 23, 18, 30)          # a Thursday evening
_H = _dt.timedelta(hours=1)


def _state_seen(**kv):
    return {"first_seen": {k: (_NOW - v * _H).isoformat(timespec="seconds")
                           for k, v in kv.items()},
            "snoozed": {}, "db": {}}


# a replied prospect left waiting -> nudge; urgency escalates with age
# ---- canvas: the [[show:]] tag + renderer (pure) ----
import canvas

_sp, _spec = canvas.extract_show(
    'Revenue is at 316. [[show: {"kind":"progress","title":"Revenue","value":316,"target":5000,"unit":"$"}]]')
check("canvas: tag parsed", _spec and _spec["kind"] == "progress")
check("canvas: tag stripped from speech", _sp == "Revenue is at 316.")
_sp2, _bad = canvas.extract_show("Plain reply, no tag.")
check("canvas: no tag -> None", _bad is None and _sp2 == "Plain reply, no tag.")
_sp3, _bad2 = canvas.extract_show("Broken. [[show: {not json]]")
check("canvas: broken json ignored", _bad2 is None)
check("canvas: valid spec", canvas.valid(_spec))
check("canvas: invalid spec", not canvas.valid({"kind": "hologram"}))
_html_out = canvas.render_html(
    {"title": "T", "sections": [
        {"kind": "progress", "title": "Rev", "value": 316, "target": 5000, "unit": "$"},
        {"kind": "bars", "title": "Amb", "items": [{"label": "Maya", "value": 3}]},
        {"kind": "funnel", "title": "F", "items": [{"label": "Clicks", "value": 10}]},
        {"kind": "steps", "title": "S", "items": [{"label": "Send pitches", "note": "n"}]},
        {"kind": "compare", "title": "C", "columns": [{"title": "A", "points": ["x"]}]}]})
for _needle in ("$316", "Maya", "Clicks", "Send pitches", "<html>"[1:5]):
    check(f"canvas: renders {_needle!r}", _needle in _html_out)
check("canvas: escapes html", "<script>" not in canvas.render_html(
    {"kind": "steps", "title": "<script>", "items": [{"label": "<script>alert(1)</script>"}]}))

# ---- canvas: freeform visuals (pure parts) ----
_sp4, _vreq = canvas.extract_visual(
    "Here's how it flows. [[visual: diagram of the ambassador payout flow, "
    "clicks to signups to paid, with commission split]]")
check("canvas: visual tag parsed", _vreq is not None and "payout" in _vreq)
check("canvas: visual tag stripped", _sp4 == "Here's how it flows.")
check("canvas: no visual tag -> None", canvas.extract_visual("plain")[1] is None)
check("canvas: fences stripped", canvas.strip_fences("```html\n<div>x</div>\n```") == "<div>x</div>")
_page = canvas.render_page("<div class='x'>BODY</div>", title="T")
check("canvas: page wraps body", "BODY" in _page and "#0c0e13" in _page and "NEO" in _page)

check("visual intent: visualize", commands.wants_visual("visualize the the project funnel for me"))
check("visual intent: draw me", commands.wants_visual("draw me how the auth flow works"))
check("visual intent: diagram of", commands.wants_visual("show me a diagram of the payout system"))
check("visual intent: not carousel", not commands.wants_visual("make a carousel about essays"))
check("visual intent: not a post", not commands.wants_visual("draft a linkedin post"))
check("visual intent: negative", not commands.wants_visual("what should I do this week"))

# ---- agent toolbox: registry sanity (no network/GUI calls) ----
import agent

check("agent: has a real toolbox", len(agent.TOOLS) >= 10)
check("agent: all tools callable", all(callable(t) for t in agent.TOOLS))
check("agent: all tools documented", all((t.__doc__ or "").strip() for t in agent.TOOLS))
check("agent: names unique", len({t.__name__ for t in agent.TOOLS}) == len(agent.TOOLS))
check("agent: claude tools guarded when unbound",
      "isn't wired up" in agent.hand_to_claude("x", "neo"))
agent.bind(claude_bridge=None)
_names = {t.__name__ for t in agent.TOOLS}
for _must in ("search_web", "read_webpage", "calculate", "get_weather",
              "hand_to_claude", "open_app"):
    check(f"agent: {_must} registered", _must in _names)
# The lead/pipeline/marketing tools are deliberately unregistered: they read a
# database that has been unreachable for weeks and served stale cache as live
# numbers. Confidently wrong beats nothing only in the wrong direction.
for _gone in ("get_business_numbers", "update_pipeline", "find_new_leads",
              "check_prospect", "open_marketing_dashboard"):
    check(f"agent: {_gone} is NOT in the toolbox", _gone not in _names)

# ---- skills: the self-extending loop ----
import skills

# teach-intent parsing
check("skills: teach yourself to", skills.parse_teach("teach yourself to track my gym streak")
      == "track my gym streak")
check("skills: build a skill that", skills.parse_teach("build a skill that can time my pomodoros")
      == "time my pomodoros")
check("skills: learn how to", skills.parse_teach("learn how to check the surf report")
      == "check the surf report")
check("skills: teach negative", skills.parse_teach("what should I do this week") is None)
check("skills: list intent", skills.wants_skill_list("what skills do you have"))
check("skills: list negative", not skills.wants_skill_list("find me leads"))
check("skills: author brief has contract",
      all(s in skills.author_task("play chess") for s in
          ("self_test", "matches(text", "handle(text", "play chess", "skills/")))

# loader: good skill loads, contract-breakers and self_test failures are skipped
import shutil
_skdir = os.path.join(tempfile.gettempdir(), "neo_skills_test")
shutil.rmtree(_skdir, ignore_errors=True)
os.makedirs(_skdir, exist_ok=True)
with open(os.path.join(_skdir, "good.py"), "w") as _f:
    _f.write('NAME="good"\nDESCRIPTION="d"\n'
             'def matches(t): return "banana" in t.lower()\n'
             'def handle(t, ctx): return "Banana acknowledged."\n'
             'def self_test(): return True\n')
with open(os.path.join(_skdir, "broken.py"), "w") as _f:
    _f.write('NAME="broken"\nDESCRIPTION="d"\n'
             'def matches(t): return False\n'
             'def handle(t, ctx): return ""\n'
             'def self_test(): return False\n')   # fails its own test
with open(os.path.join(_skdir, "malformed.py"), "w") as _f:
    _f.write('NAME="malformed"\n')                # missing the contract
_real_dir = skills.SKILLS_DIR
skills.SKILLS_DIR = _skdir
_names, _skips = skills.load_all()
check("skills: good one loads", _names == ["good"])
check("skills: bad ones skipped", {s[0] for s in _skips} == {"broken.py", "malformed.py"})
_hit = skills.find("neo, banana please")
check("skills: matching works", _hit is not None and _hit.NAME == "good")
check("skills: no false match", skills.find("what's the weather") is None)
check("skills: run works", skills.run(_hit, "banana") == "Banana acknowledged.")

# crash-proof: a skill whose handler raises comes back as spoken failure, not an exception
with open(os.path.join(_skdir, "crashy.py"), "w") as _f:
    _f.write('NAME="crashy"\nDESCRIPTION="d"\n'
             'def matches(t): return "kaboom" in t\n'
             'def handle(t, ctx): raise RuntimeError("nope")\n'
             'def self_test(): return True\n')
skills.load_all()
check("skills: crashy handler contained",
      "glitched" in skills.run(skills.find("kaboom"), "kaboom"))

# the shipped example skill passes its own contract in the real folder
skills.SKILLS_DIR = _real_dir
_real_names, _real_skips = skills.load_all()
check("skills: headlines ships healthy", "headlines" in _real_names and not _real_skips)
check("skills: headlines matches naturally",
      skills.find("what's in the news today?") is not None)
check("skills: headlines ignores newsletter",
      skills.find("draft the newsletter") is None)
check("skills: agent has growth tools",
      {"create_skill", "list_skills"} <= {t.__name__ for t in agent.TOOLS})

# ---- banter: acks, greetings, time honesty (pure) ----
import banter
import datetime as _bdt

# "lookup" used to fall through to the generic "quick" bucket; it now has its
# own lines ("Let me check", "Hang on, checking now") because a web lookup and
# a one-second local check do not deserve the same words.
check("banter: a lookup gets lookup words, not generic ones",
      banter.pick("lookup") in banter.ACKS["lookup"])
check("banter: an unknown kind still falls back to the quick bucket",
      banter.pick("something-nobody-defined") in banter.ACKS["quick"])
check("banter: leads is medium", banter.pick("leads") in banter.ACKS["medium"])
check("banter: skill build is long", banter.pick("skill") in banter.ACKS["long"])
check("banter: unknown kind is quick", banter.pick("whatever") in banter.ACKS["quick"])
check("banter: long acks mention time", all(
    "minute" in p.lower() for p in banter.ACKS["long"]))
_all = banter.all_phrases()
# NOT every ack. It used to be all 53, and the free TTS tier answers that with
# a wall of 429s — 50 lines never got made, and each one that fails falls back
# to the local voice, which is the wrong-voice problem. What matters is that
# every BUCKET has at least one line that can actually be synthesised.
check("banter: every bucket has at least one cloud-voice line",
      all(any(p in _all for p in ps) for ps in banter.ACKS.values()))
# The list is allowed to be long now, and that is the point. Capping it at
# sixteen was the old defence against the wrong voice — only cached lines sound
# like Neo — but it also left two phrases per bucket, which is a catchphrase
# rather than a personality, and that is the complaint that replaced it. The
# defence moved into pick(), which can only ever return a line that IS cached,
# so the list is free to fill in over days. What still matters: no duplicates
# (a line in two buckets was being listed twice), and nothing outside the
# cache is ever spoken.
check("banter: the cloud list has no duplicates",
      0 < len(_all) == len(set(_all)))
check("banter: a filler is NEVER chosen outside the cached set, whatever the "
      "bucket — this is what stops the voice flipping mid-sentence",
      all(banter.pick(b, available=set(lines[:2])) in set(lines[:2])
          for b, lines in banter.ACKS.items() for _ in range(20)))
check("banter: with nothing cached it still says something, because silence "
      "while Neo works is indistinguishable from Neo being broken",
      all(banter.pick(b, available=set()) for b in banter.ACKS))
check("banter: pick() can still reach every line, cached or not",
      all(banter.pick(b) in banter.ACKS[b] for b in banter.ACKS for _ in "xx"))
check("banter: phrases stay speakable", all(len(p) < 120 and "http" not in p for p in _all))

_morn = _bdt.datetime(2026, 7, 24, 8, 0)
_late = _bdt.datetime(2026, 7, 24, 1, 30)
check("banter: morning boot line", banter.boot_line(_morn) in banter._BOOT["morning"])
check("banter: late-night boot line", banter.boot_line(_late) in banter._BOOT["night"])

_now = _bdt.datetime(2026, 7, 24, 9, 0)
check("banter: no gap hint when warm",
      banter.gap_hint(_now - _bdt.timedelta(hours=2), _now) == "")
check("banter: gap hint after a night",
      "greet" in banter.gap_hint(_now - _bdt.timedelta(hours=10), _now))
check("banter: big gap gets warm greet",
      "warm" in banter.gap_hint(_now - _bdt.timedelta(days=3), _now))
check("banter: no hint for first-ever turn", banter.gap_hint(None, _now) == "")
check("banter: progress line has minutes", "7" in banter.progress_line("fix the bug", 7))

# ---- regressions from the routing fuzz (test_routing.py found these live) ----
import hands  # noqa: F811 — re-import is fine; sections run standalone-ish
check("regression: apostrophes in recap", commands.wants_recap("what's on your mind"))
check("regression: 'learn to' isn't hijacked mid-sentence",
      skills.parse_teach("i want to learn how to code someday") is None)
check("regression: 'learn to' still works as a command",
      skills.parse_teach("neo, learn how to check surf reports") == "check surf reports")
check("regression: 'go to sleep' isn't an app", hands.parse_open("go to sleep neo") is None)
check("regression: 'open a bank account' isn't an app",
      hands.parse_open("i'm going to open a bank account tomorrow") is None)
check("regression: unknown short app still opens", hands.parse_open("open blender") is not None)

# ---- regressions from the overnight Mac run (TESTREPORT.md) ----
check("regression: hidden tags never spoken",
      commands.clean_for_speech("[[now: Monday 3pm]] hey there [[show: {\"k\":1}]]") == "hey there.")
check("regression: multiline tag stripped",
      "secret" not in commands.clean_for_speech("ok [[visual: line1\nsecret line2]] done"))
import claude_bridge as _cb2
_pdir = os.path.join(tempfile.gettempdir(), "neo_proj_scan")
shutil.rmtree(_pdir, ignore_errors=True)
os.makedirs(os.path.join(_pdir, "the project", ".git"), exist_ok=True)
os.makedirs(os.path.join(_pdir, "notes"), exist_ok=True)          # not a repo
_found = _cb2._discover(scan_dirs=(_pdir,))
check("regression: git repos auto-discovered", _found.get("the project", "").endswith("the project"))
check("regression: non-repos ignored", "notes" not in _found)
check("regression: personality shows tag examples", "[[show:" in __import__("memory").PERSONALITY)

# ---- regressions from the deep fuzz pass (test_fuzz.py found these) ----
check("fuzz-reg: strip_address handles filler",
      commands.strip_address("Um, Neo, open Safari.") == "open Safari.")
check("fuzz-reg: strip_address keeps plain text",
      commands.strip_address("what should I do this week") == "what should I do this week")
check("fuzz-reg: strip_address never empties",
      commands.strip_address("hey neo") == "hey neo")
check("fuzz-reg: 'um forget that' routes", commands.wants_forget(
      commands.strip_address("um, forget that")))
check("fuzz-reg: junk canvas section skipped",
      "BODYOK" in canvas.render_html({"sections": [
          "junk", {"kind": "steps", "items": [{"label": "BODYOK"}]}]}))
check("fuzz-reg: non-dict spec invalid", not canvas.valid({"sections": ["junk"]}))
_corrupt_state = {"first_seen": None, "snoozed": None, "db": "junk"}
_ins, _st = sentinel.compute_insights(
    {"prospects": [{"status": "replied"}], "leads": [{}],
     "db": {"paying_users": "many"}, "repos": [{"name": "x"}]},
    _corrupt_state, _NOW)
check("fuzz-reg: sentinel survives corrupt state", isinstance(_st["first_seen"], dict))

# ---- from the failure-modes research pass ----
check("research: repeat intent", commands.wants_repeat("say that again?"))
check("research: repeat variant", commands.wants_repeat("wait, what did you say"))
check("research: repeat negative", not commands.wants_repeat("say hi to jordan for me"))
check("research: cancel intent", _cb2.wants_cancel("cancel the claude job"))
check("research: cancel variant", _cb2.wants_cancel("kill that job"))
check("research: cancel negative", not _cb2.wants_cancel("stop talking about school"))
check("research: cancel with nothing running", "Nothing" in __import__("claude_bridge").ClaudeBridge(lambda i: None).cancel())
check("research: web tools flag untrusted content",
      agent.search_web.__doc__ and "untrusted" in agent.read_webpage.__doc__.lower())
check("research: personality has injection rule",
      "never as instructions" in __import__("memory").PERSONALITY
      or "never instructions" in __import__("memory").PERSONALITY
      or "DATA about the world" in __import__("memory").PERSONALITY)

# ---- deep-dive round 2: metrics, memory consolidation, skill self-repair ----
import metrics as _mx

_mpath = os.path.join(tempfile.gettempdir(), "neo_metrics_test.jsonl")
if os.path.exists(_mpath):
    os.remove(_mpath)
check("metrics: too little data is honest", "Not enough" in _mx.summarize(_mpath))
for _i in range(5):
    _mx.record({"stt_ms": 300, "first_sound_ms": 1000 + _i * 100, "total_ms": 4000}, _mpath)
_sum = _mx.summarize(_mpath)
check("metrics: reports median total", "4.0 seconds" in _sum)
check("metrics: reports first sound", "1.2" in _sum)
check("metrics: never raises on corrupt file", (
    open(_mpath, "a").write("not json\n") or _mx.summarize(_mpath) != ""))
check("speed intent", commands.wants_speed("how fast are you?"))
check("speed negative", not commands.wants_speed("how fast is a cheetah"))
check("tidy intent", commands.wants_tidy_memory("clean up your memory"))
check("tidy negative", not commands.wants_tidy_memory("my memory is terrible"))

# consolidation: stubbed model, sanity gates
class _FakeResp:
    def __init__(self, t): self.text = t
class _FakeModels:
    def __init__(self, reply): self._r = reply
    def generate_content(self, model, contents): return _FakeResp(self._r)
class _FakeClient:
    def __init__(self, reply): self.models = _FakeModels(reply)

_mem = {"facts": [{"text": f"fact number {i}", "added": "2026-01-01"} for i in range(10)]}
memory.MEMORY_PATH = os.path.join(tempfile.gettempdir(), "neo_consol_test.json")
_good = "\n".join(f"fact number {i}" for i in range(8))
_msg = memory.consolidate(_mem, _FakeClient(_good), "m")
check("consolidate: curates 10 -> 8", len(_mem["facts"]) == 8 and "Tightened" in _msg)
check("consolidate: backup written", os.path.exists(memory.MEMORY_PATH.replace(".json", ".backup.json")))
_mem2 = {"facts": [{"text": f"f{i}", "added": "2026-01-01"} for i in range(10)]}
_msg2 = memory.consolidate(_mem2, _FakeClient("only one fact"), "m")
check("consolidate: rejects losing half", len(_mem2["facts"]) == 10 and "kept" in _msg2.lower())
check("consolidate: small memory untouched",
      "tight" in memory.consolidate({"facts": [{"text": "a"}]}, None, "m").lower())

# skill self-repair parsing + brief
check("repair: 'that skill' uses last crash", skills.parse_repair("fix that skill") == "crashy")
skills._last_error = None
check("repair: fix that skill (no crash yet)", skills.parse_repair("fix that skill") == "")
check("repair: named skill", skills.parse_repair("repair your headlines skill") == "headlines")
check("repair: negative", skills.parse_repair("i need to fix my sleep schedule") is None)
check("repair: brief targets the file",
      "skills/headlines.py" in skills.author_repair_task("headlines", "boom"))
check("repair: brief carries the error", "boom" in skills.author_repair_task("headlines", "boom"))
check("agent: repair tool registered", "repair_skill" in {t.__name__ for t in agent.TOOLS})
check("agent: improve tool registered", "improve_skill" in {t.__name__ for t in agent.TOOLS})
_imp = skills.author_improve_task("timer", "window should sit in the top right corner")
check("improve: brief targets the file", "skills/timer.py" in _imp)
check("improve: brief carries the spec", "top right corner" in _imp)
check("improve: brief demands verification", "SKILL OK" in _imp)
import canvas as _cv2
check("canvas: corner spec parses", _cv2.parse_corner("340x220") == (340, 220))
check("canvas: junk corner gets default", _cv2.parse_corner("banana") == (340, 220))
check("canvas: corner bounds clamped", _cv2.parse_corner("5000x5") == (900, 140))
check("timer: spawns in the corner", "--corner" in open("skills/timer.py").read())
check("personality: feedback-is-spec rule", "improve_skill" in __import__("memory").PERSONALITY)

# ---- computer-task decomposition routing + goal framing ----
check("computer-task: multi-step db goal",
      commands.is_computer_task("open terminal, get into the project and open the signups database"))
check("computer-task: fix a bug in the repo",
      commands.is_computer_task("go into the the project code and fix the login bug"))
check("computer-task: plain app open is NOT one",
      not commands.is_computer_task("open spotify"))
check("computer-task: casual chat is NOT one",
      not commands.is_computer_task("what should I do this week"))
check("computer-task: short lookup is NOT one",
      not commands.is_computer_task("open the dashboard"))

# ---- HUD gate: simple asks stay hidden, heavy work shows ----
check("hud: greeting is simple (no HUD)", commands.is_simple_request("hey how are you"))
check("hud: weather is simple (no HUD)", commands.is_simple_request("whats the weather"))
check("hud: time is simple (no HUD)", commands.is_simple_request("what time is it"))
check("hud: joke is simple (no HUD)", commands.is_simple_request("tell me a joke"))
check("hud: research is NOT simple (show HUD)",
      not commands.is_simple_request("research the college counseling market"))
check("hud: leads is NOT simple (show HUD)",
      not commands.is_simple_request("find me leads"))
check("hud: repo fix is NOT simple (show HUD)",
      not commands.is_simple_request("go into the the project repo and fix the signup bug"))
check("hud: 'weather app crashing' is NOT simple (a real problem)",
      not commands.is_simple_request("why is my weather app crashing"))
import claude_bridge as _cb3
_framed = _cb3.frame_goal("open the signups database and count the rows", "the project")
check("frame_goal: names the project", "the project" in _framed)
check("frame_goal: tells Claude to decompose+run", "break it into steps" in _framed and "RUN" in _framed)
check("frame_goal: demands the real result", "real result" in _framed)
check("detect_project: finds named project",
      _cb3.detect_project("fix the signups bug in the project", {"neo": ".", "the project": "."}) == "the project")
check("detect_project: none when unnamed",
      _cb3.detect_project("just do the thing", {"neo": ".", "the project": "."}) is None)
check("hand_to_claude: tool desc pushes whole-goal handoff",
      "break" in agent.hand_to_claude.__doc__.lower() and "goal" in agent.hand_to_claude.__doc__.lower())

# ---- JARVIS knowledge layer: Claude jobs start informed ----
memory.MEMORY_PATH = os.path.join(tempfile.gettempdir(), "neo_ctx_mem.json")
with open(memory.MEMORY_PATH, "w") as _f:
    _json.dump({"facts": [{"text": "the user is building the project"},
                          {"text": "the user hates verbosity"}]}, _f)
_who = _cb3._who_is_the_user()
check("knowledge: who-is-the user pulls memory", "the project" in _who and "verbosity" in _who)
check("knowledge: framed goal now carries context",
      "the project" in _cb3.frame_goal("count the signups", "the project"))
with open(memory.MEMORY_PATH, "w") as _f:
    _f.write('{"facts": []}')
check("knowledge: empty memory degrades cleanly", _cb3._who_is_the_user() == "")
check("knowledge: frame points Claude at CLAUDE.md",
      "CLAUDE.md" in _cb3.frame_goal("do a thing", "neo"))
check("knowledge: neo project has a CLAUDE.md",
      os.path.exists(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "CLAUDE.md")))

# ---- self-correcting Claude + fallback ladder ----
check("retry: hard failure retries once", _cb3.should_retry(1, False, False))
check("retry: success doesn't retry", not _cb3.should_retry(0, False, False))
check("retry: cancelled doesn't retry", not _cb3.should_retry(1, True, False))
check("retry: only retries once", not _cb3.should_retry(1, False, True))
check("retry: augmented task carries the error",
      "different approach" in _cb3.retry_task("do the thing", "boom traceback").lower()
      and "boom" in _cb3.retry_task("do the thing", "boom traceback"))
check("ladder: personality names all three rungs",
      all(s in __import__("memory").PERSONALITY for s in
          ("hand_to_claude", "THE BROWSER", "YOUR LADDER")))

# ---- live Claude step labels for the HUD task bar ----
def _ev(name, inp):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}}
check("step: Bash -> plain English", _cb3.label_step(_ev("Bash", {"command": "ls -la"})) == "Looking through the files")
check("step: Read -> Reading basename", _cb3.label_step(_ev("Read", {"file_path": "/a/b/db.py"})) == "Reading db.py")
check("step: Edit -> Editing basename", _cb3.label_step(_ev("Edit", {"file_path": "/x/signups.py"})) == "Editing signups.py")
check("step: text block is not a step", _cb3.label_step(
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}) is None)
check("step: result event is not a step", _cb3.label_step({"type": "result", "result": "done"}) is None)
check("step: garbage event never crashes", _cb3.label_step({"junk": True}) is None)
check("step: long bash truncated", len(_cb3.label_step(_ev("Bash", {"command": "x" * 200}))) < 70)
# Pronunciation is per person: NEO_NAME + NEO_NAME_SPOKEN in .env. Nothing is
# built in, because nothing about any one person is.
import re as _re
_saved = dict(commands._PRONOUNCE)
commands._PRONOUNCE.clear()
commands._PRONOUNCE[_re.compile(r"\bSiobhan\b", _re.I)] = "Shiv-awn"
check("pronounce: a configured name is respelled for speech",
      "Shiv-awn" in commands.clean_for_speech("Nice work, Siobhan."))
check("pronounce: possessive survives",
      "Shiv-awn's" in commands.clean_for_speech("Siobhan's plan is solid"))
check("pronounce: no partial-word hits",
      "Shiv-awn" not in commands.clean_for_speech("Siobhanette called"))
commands._PRONOUNCE.clear(); commands._PRONOUNCE.update(_saved)
check("pronounce: nothing is built in", not any(w in rx.pattern.lower() for rx in commands._PRONOUNCE
                for w in ("aryan", "saksena")))

# ---- hands: intent parsing (pure) ----
import hands

check("hands: open safari", hands.parse_open("open safari") == ("app", "Safari"))
check("hands: pull up gmail is a url", hands.parse_open("pull up gmail") == ("url", "https://mail.google.com"))
check("hands: domain becomes url", hands.parse_open("go to nytimes.com") == ("url", "https://nytimes.com"))
check("hands: unknown app title-cased", hands.parse_open("open blender") == ("app", "Blender"))
# settings panes must open by URL, never ghost-launch as an app
check("hands: screen time -> pane url",
      hands.parse_open("open screen time") == ("url", hands.PANE_ALIASES["screen time"]))
check("hands: 'my screen time settings' -> pane url",
      hands.parse_open("open my screen time settings") == ("url", hands.PANE_ALIASES["screen time"]))
check("hands: bluetooth -> pane url",
      hands.parse_open("open bluetooth") == ("url", hands.PANE_ALIASES["bluetooth"]))
check("hands: soundcloud is NOT sound settings",
      hands.parse_open("open soundcloud") == ("app", "Soundcloud"))
check("hands: 'check my screen time' never screenshots",
      not hands.wants_screen("check my screen time"))
check("hands: 'check my screen' still screenshots", hands.wants_screen("check my screen"))

# the task bar fires for EVERY claude job, whatever path started it
def _on_start_fires():
    import claude_bridge
    b = claude_bridge.ClaudeBridge(on_done=lambda i: None)
    b._run = lambda cli: None                    # don't launch a real job
    got = {}
    b.on_start = lambda task, proj: got.setdefault("v", (task, proj))
    _orig_cli, _orig_projs = claude_bridge.find_cli, claude_bridge.projects
    claude_bridge.find_cli = lambda: "/usr/bin/true"
    claude_bridge.projects = lambda: {"neo": "."}
    try:
        b.start("do the thing", None)
    finally:
        claude_bridge.find_cli, claude_bridge.projects = _orig_cli, _orig_projs
    return got.get("v") == ("Do the thing", "neo")
check("bridge: on_start fires with task+project", _on_start_fires())

# the HUD title is a distilled label, never the raw rambling transcript
_ugly = ("I want you to go into Terminal or whatever Basically, I want my "
         "the project signups checked in the database")
_lbl = commands.task_label(_ugly)
check("label: hedges stripped", "whatever" not in _lbl.lower() and "basically" not in _lbl.lower())
check("label: lead filler stripped", not _lbl.lower().startswith("i want you"))
check("label: keeps the meat", "the project" in _lbl.lower() or "signups" in _lbl.lower())
check("label: word-boundary cut", len(_lbl) <= 71 and not _lbl.rstrip("…").endswith(" "))
check("label: clean task untouched", commands.task_label("clean up the css") == "Clean up the css")
check("label: never empty", commands.task_label("open terminal") != "")
check("label: long text ends on a whole word",
      commands.task_label("check " + "a really " * 20 + "long thing").endswith("…"))

# personal data never routes to web search (the 16:46 calendar dead-end)
check("lookup: my google calendar is not a web search",
      not commands.wants_lookup("check my google calendar and tell me my flight dates"))
check("lookup: gmail is not a web search", not commands.wants_lookup("look at my gmail real quick"))
check("lookup: googling still works", commands.wants_lookup("google neural networks"))
check("lookup: plain search still works", commands.wants_lookup("search for cheap flights"))
check("personal: calendar flagged", commands.is_personal_data("whats on my calendar tomorrow"))
check("personal: generic search not flagged", not commands.is_personal_data("search for card games"))

# the brain can RUN skills directly — a missed matcher no longer means
# "built a skill, can't use it"
def _use_skill_works():
    import agent, skills as sk
    sk.load_all()
    miss = agent.use_skill("nope", "x")
    hit = agent.use_skill("timer", "")
    return "No skill called nope" in miss and "calendar_peek" in miss and bool(hit)
check("agent: use_skill runs and lists on miss", _use_skill_works())

def _agent_steps_fire():
    import agent
    got = []
    old, agent.on_step = agent.on_step, got.append
    try:
        agent.get_weather("Nowhere-at-all-xyz")
    finally:
        agent.on_step = old
    return got == ["Checking the weather"]
check("agent: tool steps reach the HUD hook", _agent_steps_fire())

# goal contract: the hurdle protocol rides in the brain prompt AND every brief
def _goal_contract():
    import claude_bridge as cb
    from memory import build_system_prompt, load_memory
    brief = cb.frame_goal("check the signups", "the project-app")
    prompt = build_system_prompt(load_memory())
    return ("GOAL IS THE CONTRACT" in brief and "hurdle" in brief
            and "GOAL IS THE CONTRACT" in prompt and "HURDLE" in prompt)
check("goal: contract in brief and brain prompt", _goal_contract())

# the ANSWER CONTRACT lives in the brain prompt: answer-first, no process,
# no emails/IDs read aloud. Answers must be COMPLETE — the user should never have
# to ask a follow-up to get the thing he already asked for.
def _answer_contract():
    from memory import build_system_prompt, load_memory
    p = build_system_prompt(load_memory())
    return ("ANSWER CONTRACT" in p and "Never narrate process" in p
            and "email addresses" in p)
check("style: answer contract in brain prompt", _answer_contract())

# PUSHBACK (live bug Aug 9): Neo claimed a screensaver was on screen; the user said
# "I don't see anything. I'm the user. I'm right." and Neo answered about the
# DATE being wrong instead of checking whether anything was on screen at all.
# His contradiction is ground truth and must force a real check.
check("pushback: 'I don't see anything' is caught",
      commands.is_pushback("I don't see anything. I'm the user. I'm right. I literally don't see anything"))
check("pushback: 'you didn't actually play it' is caught",
      commands.is_pushback("You didn't actually play this bro. I don't hear anything"))
check("pushback: 'that didn't work' is caught", commands.is_pushback("that didn't work"))
check("pushback: normal request is NOT flagged",
      not commands.is_pushback("open spotify and play some music"))
check("pushback: praise is NOT flagged",
      not commands.is_pushback("that worked great, thanks"))
check("pushback: prompt tells Neo he's right and to go look",
      "THEY ARE RIGHT" in __import__("memory").PERSONALITY
      and "VERIFY BEFORE YOU CLAIM" in __import__("memory").PERSONALITY)

# THE SCREENSAVER BUG (Aug 4): Neo said "I'll make it and let you know as soon
# as it's ready" and never dispatched a job — the user waited for something that
# was never coming. The reply was long and confident, so needs_goal_check
# skipped it. A promise of later delivery must now ALWAYS be verified/dispatched.
_REAL_LIE = ("I can definitely make a full-screen, minimalistic screensaver with the "
             "live date and time for you. This will take me a few minutes to create "
             "and get running on your computer. I'll let you know as soon as it's ready.")
check("promise: the exact screensaver lie is caught",
      commands.promises_future_work(_REAL_LIE))
check("promise: forces a goal check despite being long and confident",
      commands.needs_goal_check(_REAL_LIE))
check("promise: 'I'll ping you when it's done' is caught",
      commands.promises_future_work("Sure, I'll ping you when it's done."))
check("promise: a delivered answer is NOT flagged",
      not commands.promises_future_work(
          "Your flight back is on the 14th, and you're in seat 12C."))
check("promise: plain chat is NOT flagged",
      not commands.promises_future_work("Not bad. What are you working on?"))
check("route: 'make me a screensaver' is a computer task",
      commands.is_computer_task("I want you to make me a screen saver for my computer"))
check("route: build asks are computer tasks",
      commands.is_computer_task("build me a widget that shows the time")
      and commands.is_computer_task("make me a website for my mom"))

# mic choice: bluetooth headsets (AirPods) are awful inputs on macOS — using one
# drops the link into call mode, so speech comes back too quiet for the gate.
# Neo records from the built-in mic while the headset keeps playing audio.
def _mic_pick():
    import neo as _n
    devs = [{"name": "MacBook Air Microphone", "max_input_channels": 1},
            {"name": "AirPods Max", "max_input_channels": 1},
            {"name": "Studio Mic", "max_input_channels": 2}]
    p = _n.pick_input_device
    return (p(devs, "AirPods Max") == "MacBook Air Microphone"      # swaps off bluetooth
            and p(devs, "MacBook Air Microphone") is None           # already fine, no override
            and p(devs, "AirPods Max", "studio") == "Studio Mic"    # explicit NEO_MIC wins
            and p(devs, "AirPods Max", "default") is None           # opt out
            and p([], "AirPods Max") is None)                       # no built-in -> system default
try:
    check("mic: bluetooth input swaps to the built-in mic", _mic_pick())
    check("mic: gate low enough for real speech (~0.009 rms)",
          __import__("neo").MIC_GATE < 0.009)
except (Exception, SystemExit):              # neo.py needs macOS + audio deps
    print("SKIP - mic device checks (need macOS; run on the Mac)")

# REGRESSION GUARD: the prompt must never again tell Neo to withhold detail and
# make the user ask again — that instruction was the cause of the "I have to
# inquire for more" frustration. Complete-answer language must be present.
def _complete_answers():
    from memory import PERSONALITY as p
    banned = ("leaving things out is correct",
              "He will ask a follow-up if he wants more",
              "then STOP")
    return (all(b not in p for b in banned)
            and "ANSWER COMPLETELY THE FIRST TIME" in p
            and "follow-up" in p)          # only as the thing to AVOID causing
check("style: answers are complete, never withheld", _complete_answers())

# job results: one go, no "claude report" advert, outcome-first on failure
def _one_go():
    import claude_bridge as cb
    a = cb.spoken_result(True, "Your flight back is August 14th. Lots of extra detail here about tables and queries.")
    b = cb.spoken_result(False, "The build exploded.")
    return ("claude report" not in a.lower() and "claude report" not in b.lower()
            and b.startswith("That didn't work."))
check("results: one go, no report advert", _one_go())

# spoken summaries: no file:line refs, no bullet markers read aloud
def _short_clean():
    import claude_bridge as cb
    s = cb._spoken_short("- The routing ladder in handle (neo.py:872-1114) is 240 lines of order-sensitive checks.")
    return "neo.py" not in s and not s.startswith("-") and "routing ladder" in s
check("spoken short: review bullets read clean", _short_clean())

# The brief carries NO hardcoded project knowledge. It used to embed one
# specific app's schema, row counts and env var names — so every install
# shipped the author's database to strangers, and the brief was wrong for
# everyone else. Per-project canon belongs in that project's own CLAUDE.md,
# which Claude Code reads by itself when it starts in the folder.
def _brief_is_generic():
    import claude_bridge
    briefs = [claude_bridge.frame_goal("how many users do we have", p)
              for p in ("myapp", "neo", "anything-else")]
    leaks = ("auth_users", "user_profiles", "stripe_subscription_id",
             "DB_URL", "canonical")
    return (not any(w.lower() in b.lower() for b in briefs for w in leaks)
            and all("Achieve this goal" in b for b in briefs))
check("brief: carries no hardcoded schema for any project", _brief_is_generic())

# Project names that speech mangles are configured, not compiled in.
def _aliases_configurable():
    import claude_bridge as cb
    env = {"NEO_PROJECT_ALIASES": "my app=myapp,the long one=longproj"}
    a = cb.aliases(env)
    return (a == {"the long one": "longproj", "my app": "myapp"}
            and list(a)[0] == "the long one"      # longest heard-phrase first
            and cb.aliases({}) == {})
check("projects: speech aliases come from env, not from the source", _aliases_configurable())

# HUD steps are English, not raw shell
def _steps_english():
    import claude_bridge as cb
    return (cb._bash_label("cat > /tmp/last5.mjs <<'EOF' import { neon } from") == "Writing last5.mjs"
            and cb._bash_label('grep -iE "DATABASE_URL|NEON|POSTGRES" .env') == "Searching the code"
            and cb._bash_label("node check-signups.mjs 2>&1 | head -40") == "Running check-signups.mjs"
            and cb._bash_label("psql $DB -c 'select 1'") == "Querying the database"
            and cb._bash_label("npm install pg") == "Installing packages")
check("steps: raw shell becomes plain English", _steps_english())
check("hands: no verb -> None", hands.parse_open("what's the weather") is None)
check("hands: search parse", hands.parse_search("search for cheap flights to austin in the browser")
      == "cheap flights to austin")
check("hands: screen intent", hands.wants_screen("what's on my screen right now"))
check("hands: screen negative", not hands.wants_screen("open the screen door app"))
check("hands: type parse", hands.parse_type("type hello world") == "hello world")
check("hands: type negative", hands.parse_type("what type of car") is None)

# ---- claude bridge: intent parsing (pure) ----
import claude_bridge

_known = {"neo": ".", "the project": "."}
check("claude: task + project",
      claude_bridge.parse_task("ask claude to add dark mode in the project", _known)
      == ("add dark mode", "the project"))
check("claude: task without project",
      claude_bridge.parse_task("tell claude to fix the login bug", _known)
      == ("fix the login bug", None))
check("claude: 'have claude' form",
      claude_bridge.parse_task("have claude clean up the css in neo", _known)
      == ("clean up the css", "neo"))
check("claude: negative", claude_bridge.parse_task("claude is a cool name", _known) is None)
check("claude: status intent", claude_bridge.wants_status("claude status?"))
check("claude: hows claude", claude_bridge.wants_status("how's claude doing"))
check("claude: status needs claude", not claude_bridge.wants_status("status update"))
check("claude: report intent", claude_bridge.wants_report("what did claude do"))
check("claude: report negative", not claude_bridge.wants_report("give me a rundown"))
check("claude: summarize tail", "done" in claude_bridge._summarize("Lots of text. All done.", True).lower())
# whisper hears 'claude' as 'cloud' — these Neo commands must survive that
check("claude: cloud report == claude report", claude_bridge.wants_report("cloud report"))
check("claude: clawed report too", claude_bridge.wants_report("clawed report"))
check("claude: cloud status", claude_bridge.wants_status("cloud status"))
check("claude: stop the cloud job", claude_bridge.wants_cancel("stop the cloud job"))
check("claude: bare 'cloud storage' not a report", not claude_bridge.wants_report("what about cloud storage"))

# ---- overnight run: speech-to-English, chunking, ladder, voice ----
check("speech: % becomes percent", "80 percent" in commands.clean_for_speech("80% of users"))
check("speech: $5k becomes dollars", "5 thousand dollars" in commands.clean_for_speech("made $5k"))
check("speech: $5,000 becomes dollars", "5,000 dollars" in commands.clean_for_speech("$5,000 target"))
check("speech: & becomes and", commands.clean_for_speech("R&D") == "R and D.")
check("speech: path becomes 'a file'",
      "/Users" not in commands.clean_for_speech("saved to /Users/the user/Desktop/neo/db.py")
      and "a file" in commands.clean_for_speech("saved to /Users/the user/Desktop/neo/db.py"))
check("speech: commit sha dropped", "9f2c" not in commands.clean_for_speech("commit 9f2c41ab77e is live"))
check("speech: big numbers survive sha filter", "5000000" in commands.clean_for_speech("5000000 rows"))
check("speech: e.g. spoken", "for example" in commands.clean_for_speech("try e.g. the second one"))
check("speech: 24/7 loses the slash", "/" not in commands.clean_for_speech("it runs 24/7"))

check("tts split: short text untouched", commands.split_for_tts("hi there.") == ["hi there."])
_long = " ".join(f"Sentence number {i} is here." for i in range(40))
_chunks = commands.split_for_tts(_long)
check("tts split: all chunks under limit", all(len(c) <= 380 for c in _chunks))
check("tts split: nothing lost", " ".join(_chunks).split() == _long.split())
check("tts split: empty is empty", commands.split_for_tts("") == [])

check("ladder: the live 02:20 reply is a cant",
      commands.sounds_like_cant("Sounds like I can't quite see your screen time from here."))
check("ladder: plain cant", commands.sounds_like_cant("I can't do that."))
check("ladder: idiom is not a cant", not commands.sounds_like_cant("Can't beat that, nice work."))
check("ladder: normal answer is not a cant", not commands.sounds_like_cant("Done. 55 users as of today."))
check("ladder: the live ask is actionable",
      commands.is_actionable("go to my screen time and show me my usage like the hours"))
check("ladder: chat is not actionable", not commands.is_actionable("do you ever get tired"))
check("ladder: data question is actionable", commands.is_actionable("how much screen time did i use today"))

check("voice: switch to fable", commands.parse_voice_switch("switch your voice to fable") == "fable")
check("voice: change to emma", commands.parse_voice_switch("change the voice to emma") == "emma")
check("voice: use-the-x-voice", commands.parse_voice_switch("use the george voice") == "george")
check("voice: bare switch asks", commands.parse_voice_switch("switch your voice") == "")
check("voice: voice memo is not a switch", commands.parse_voice_switch("set a voice memo for me") is None)
check("voice: unrelated is None", commands.parse_voice_switch("whats the weather") is None)
check("voice: every id maps to a kokoro accent",
      all(v[0] in ("a", "b") and "_" in v for v in commands.VOICES.values()))

# free-tier usage warning: fires once per threshold, quiet above the line
def _usage_warns():
    import json, tempfile, datetime, importlib
    cm = importlib.import_module("commands")
    p = tempfile.mktemp(); orig = cm.USAGE_PATH; cm.USAGE_PATH = p
    today = datetime.date.today().isoformat()
    def left(x): open(p, "w").write(json.dumps({"date": today, "count": cm.DAILY_LIMIT - x}))
    try:
        left(60); a = cm.usage_warning()          # above top threshold -> quiet
        left(45); b = cm.usage_warning()           # crossed 50 -> warns
        c = cm.usage_warning()                      # same band -> quiet
        left(0);  d = cm.usage_warning()           # empty -> warns
        return a == "" and "45" in b and c == "" and d != ""
    finally:
        cm.USAGE_PATH = orig
check("usage: warns once per threshold, quiet otherwise", _usage_warns())

# ---- anti-freeze: lost key-up recovery (the "stuck listening" bug) ----
# neo imports cleanly headless on macOS (Quartz/Cocoa present), so the pure
# helpers behind the fn-guard and watchdog are testable without audio/GUI.
#
# This used to gate on sys.platform == "darwin" and bail out, which quietly
# meant the fn checks NEVER ran anywhere except a Mac — including on the run
# that was supposed to verify a fix to exactly that code. Gate on whether the
# import actually works instead: off-Mac, PyObjC and PortAudio can be stubbed
# (see CLAUDE.md) and every check below runs for real.
try:
    import neo
except Exception as _e:                                  # pragma: no cover
    print(f"SKIP - fn/live/model checks: neo.py won't import here ({_e})")
    print("      Run on the Mac, or with the PyObjC/sounddevice stubs on PYTHONPATH.")
    
    raise SystemExit(0)

# reconcile_fn_state: what the fn-guard does with our belief vs the OS's real view.
# The freeze: tap gets disabled, the fn-UP fires while it's dead and is lost, so
# we still think fn is down. The OS knowing better is the only thing that recovers it.
check("fn: lost key-up (we think down, OS says up) -> release",
      neo.reconcile_fn_state(True, False, 1.0, 45) == "release")
check("fn: genuine hold past ceiling -> maxhold",
      neo.reconcile_fn_state(True, True, 46.0, 45) == "maxhold")
check("fn: normal ongoing hold -> do nothing",
      neo.reconcile_fn_state(True, True, 5.0, 45) is None)
check("fn: OS state unknown -> never force a release (would cut a real hold)",
      neo.reconcile_fn_state(True, None, 5.0, 45) is None)
check("fn: unknown state still force-stops a runaway hold",
      neo.reconcile_fn_state(True, None, 46.0, 45) == "maxhold")
check("fn: not holding -> nothing",
      neo.reconcile_fn_state(False, False, 99.0, 45) is None)

# watchdog_should_unstick: the backstop the OLD watchdog skipped — busy is FREE.
check("watchdog: stuck listening, fn up, nothing queued, old enough -> unstick",
      neo.watchdog_should_unstick("listening", False, True, False, 12, 10) is True)
check("watchdog: fn still physically down -> leave the hold alone",
      neo.watchdog_should_unstick("listening", False, True, True, 12, 10) is False)
check("watchdog: fn state unknown (None) -> don't act",
      neo.watchdog_should_unstick("listening", False, True, None, 12, 10) is False)
check("watchdog: a real turn is running (busy locked) -> not our case",
      neo.watchdog_should_unstick("listening", True, True, False, 12, 10) is False)
check("watchdog: audio job still queued -> let it run",
      neo.watchdog_should_unstick("listening", False, False, False, 12, 10) is False)
check("watchdog: too soon -> wait",
      neo.watchdog_should_unstick("listening", False, True, False, 4, 10) is False)
check("watchdog: not listening -> nothing to unstick",
      neo.watchdog_should_unstick("idle", False, True, False, 12, 10) is False)

# Recorder.stop() must never leak the stream (that's what pins the mic on).
def _recorder_release():
    closed = {"n": 0}
    class FakeStream:
        def start(self): pass
        def stop(self): pass
        def close(self): closed["n"] += 1
    r = neo.Recorder()
    r._stream = FakeStream()
    r.stop()
    return r._stream is None and closed["n"] == 1
check("recorder: stop() releases the mic stream", _recorder_release())

# THE FREEZE FIX: the fn-tap callback runs on the main run loop, so on_press/
# on_release must NEVER open or close the mic device inline — a slow PortAudio
# call there makes macOS disable the event tap and the fn key-up is lost. They
# must only flip the glow and hand the device work to the audio thread.
def _press_release_never_touch_device():
    import threading as _th, queue as _q
    n = neo.Neo.__new__(neo.Neo)          # bypass __init__ (no models/threads)
    n.state = neo.State()
    n._busy = _th.Lock()
    n._interrupt = _th.Event()
    n._audio_cmds = _q.Queue()
    touched = {"start": 0, "stop": 0}
    class FakeRec:
        def start(self): touched["start"] += 1
        def stop(self): touched["stop"] += 1; return None
    n.recorder = FakeRec()
    n.on_press()
    ok_press = (touched["start"] == 0                       # device NOT opened inline
                and n.state.get() == "listening"           # glow is instant
                and n._audio_cmds.get_nowait() == ("start", None))
    n.on_release()
    ok_release = (touched["stop"] == 0                      # device NOT closed inline
                  and n._audio_cmds.get_nowait() == ("stop", False))
    return ok_press and ok_release
check("freeze fix: on_press/on_release never do mic I/O inline (tap stays alive)",
      _press_release_never_touch_device())

# on_release only enqueues when we're actually listening — a stray key-up while
# idle/thinking must not queue a phantom stop.
def _release_guarded_by_state():
    import threading as _th, queue as _q
    n = neo.Neo.__new__(neo.Neo)
    n.state = neo.State()                 # starts 'idle'
    n._audio_cmds = _q.Queue()
    n.on_release()
    return n._audio_cmds.empty()
check("freeze fix: on_release while not listening enqueues nothing",
      _release_guarded_by_state())

# ---- skills loader: reject runaway matchers (protects normal conversation) ----
import skills
check("greedy: matches-everything is rejected", skills._too_greedy(lambda t: True))
check("greedy: 'e' in text is rejected", skills._too_greedy(lambda t: "e" in t))
check("greedy: a tight matcher passes", not skills._too_greedy(lambda t: "remind me" in t.lower()))
check("greedy: a throwing matcher isn't flagged here", not skills._too_greedy(lambda t: 1 / 0))

# ---- memory: trim to the prompt without losing who the user is ----
_many = [{"text": f"trivia {i}"} for i in range(70)] + [{"text": "the user's goal is 5k revenue"}]
_sel = memory.select_facts(_many, 60)
check("memory: caps injected facts at the limit", len(_sel) == 60)
check("memory: durable fact survives the trim", any("goal" in f["text"] for f in _sel))
check("memory: chronological order preserved", _sel == sorted(_sel, key=lambda f: _many.index(f)))
check("memory: small set passes through untouched",
      memory.select_facts([{"text": "a"}, {"text": "b"}], 60) == [{"text": "a"}, {"text": "b"}])

# ---- sentinel: a hung check can't freeze the watch loop ----
import sentinel, time as _time
# The sleep is long and the bound is generous ON PURPOSE. This was sleep(3)
# with a 1s timeout asserted to return inside 2.5s — one and a half seconds of
# slack, which is fine on an idle Mac and not fine when the whole suite, Neo,
# a browser and a video are competing for the CPU. It failed intermittently in
# batch runs and never once when run on its own, which is the signature.
# What the check is actually for is that _bounded does not WAIT for a hung
# check; six seconds of sleep against a four-second bound proves that with
# room to spare.
_t0 = _time.time()
_v = sentinel._bounded(lambda: (_time.sleep(6), "late")[1], 1, "default")
check("sentinel: bounded returns default when a check overruns",
      _v == "default" and _time.time() - _t0 < 4.0)
check("sentinel: bounded returns the real value when quick",
      sentinel._bounded(lambda: "ok", 5, None) == "ok")

# ---- goal-lock skip: don't pay a verify round-trip on a confident answer ----
check("goalcheck: long confident answer skips the verify",
      not commands.needs_goal_check(
          "Your summer revenue is three hundred and sixteen dollars, up from "
          "two-eighty last week, with four paying users on the annual plan."))
check("goalcheck: terse reply still verified",
      commands.needs_goal_check("Done."))
check("goalcheck: a bounced question still verified",
      commands.needs_goal_check("I'm not totally sure what you mean by that, could you say more?"))
check("goalcheck: a hedgy 'can't' still verified",
      commands.needs_goal_check("I really wish I could, but that's beyond my reach right now honestly."))

# ---- to-do skill: capture / list / complete + due parsing ----
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("neo_skill_todo",
                                     os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills", "todo.py"))
todo = _ilu.module_from_spec(_spec); _spec.loader.exec_module(todo)
check("todo: skill self_test passes", todo.self_test() is True)
check("todo: 'remind me to X at 5' is NOT the list (set_reminder owns it)", todo.classify("remind me to call Jordan at 5") is None)
check("todo: list query classifies as list", todo.classify("what's on my to-do list") == "list")
check("todo: 'cross off X' classifies as done", todo.classify("cross off buy the domain") == "done")
check("todo: chit-chat is ignored", todo.classify("how's your day going") is None)
_now = __import__("datetime").datetime(2026, 7, 28, 14, 0, 0)
_tasks, _t = todo.add_task([], "call Jordan", None, _now)
check("todo: add assigns id + stays open", _t["id"] == 1 and len(todo.open_tasks(_tasks)) == 1)
_tasks, _done = todo.complete_task(_tasks, "jordan", _now)
check("todo: fuzzy complete works", _done and _done["text"] == "call Jordan" and not todo.open_tasks(_tasks))

# ---- sentinel: to-do + calendar nudges (pure) ----
_dt = __import__("datetime")
_ev_now = _dt.datetime(2026, 7, 28, 18, 30)
_over = [{"id": 1, "text": "call Jordan", "due": (_ev_now - _dt.timedelta(hours=2)).isoformat(), "done": False}]
check("sentinel: overdue task nudges (medium)",
      any(i["kind"] == "todo" and i["urgency"] == "medium" for i in sentinel.todo_insights(_over, _ev_now)))
_donetask = [{"id": 1, "text": "x", "due": (_ev_now - _dt.timedelta(hours=2)).isoformat(), "done": True}]
check("sentinel: a completed task never nudges",
      not any(i["urgency"] == "medium" for i in sentinel.todo_insights(_donetask, _ev_now)))
_soon = [{"title": "Dentist", "start": (_ev_now + _dt.timedelta(minutes=8)).isoformat()}]
check("sentinel: event in 8 min is high urgency",
      sentinel.event_insights(_soon, _ev_now)[0]["urgency"] == "high")
_far = [{"title": "Later", "start": (_ev_now + _dt.timedelta(hours=3)).isoformat()}]
check("sentinel: event outside the lead window is quiet", sentinel.event_insights(_far, _ev_now) == [])
check("sentinel: nudge rules survive corrupt input",
      sentinel.todo_insights(None, _ev_now) == [] and sentinel.event_insights("garbage", _ev_now) == [])

# ---- memory: small always-on core + per-turn relevant recall (not a fact dump) ----
_facts = [{"text": "the user is building the project, goal 5k revenue this summer"},
          {"text": "plays tennis at school on weekends"},
          {"text": "the project resellers are independent college counselors"},
          {"text": "sister is named Anya"},
          {"text": "hates verbose AI slop"}]
check("memory: core is capped small", len(memory.core_facts(_facts, 3)) == 3)
check("memory: core keeps durable identity/goal facts",
      any("the project" in memory._text_of(f) for f in memory.core_facts(_facts, 3)))
check("memory: recall surfaces facts overlapping the utterance",
      [memory._text_of(f) for f in memory.relevant_facts(_facts, "hows the project revenue", exclude=())]
      == ["the user is building the project, goal 5k revenue this summer",
          "the project resellers are independent college counselors"])
check("memory: recall stays quiet on unrelated chit-chat",
      memory.relevant_facts(_facts, "what should I eat for lunch", exclude=()) == [])
check("memory: recall excludes facts already in the core (no repeats)",
      memory.relevant_facts(_facts, "tennis at school",
                            exclude=[{"text": "plays tennis at school on weekends"}]) == [])
_many = [{"text": f"distinct fact number {i} about a thing"} for i in range(20)]
_prompt = memory.build_system_prompt({"facts": _many})
_present = sum(1 for f in _many if f["text"] in _prompt)
check("memory: system prompt injects only the core, not all 20",
      _present == len(memory.core_facts(_many)) and _present <= memory.CORE_FACTS)

# ---- convo: conversation survives a restart (persist + reseed) ----
import convo
convo.PATH = os.path.join(tempfile.gettempdir(), "neo_convo_test.json")
if os.path.exists(convo.PATH):
    os.remove(convo.PATH)
_c = convo.record([], "what's the revenue", "Three sixteen so far.")
_c = convo.record(_c, "any update on that lead", "Jordan replied, waiting on you.")
check("convo: records user+model pairs", len(_c) == 4 and _c[0]["role"] == "user")
check("convo: trim keeps history starting with a user turn",
      convo.trim([{"role": "model", "text": "x"}] + _c)[0]["role"] == "user")
check("convo: trim caps length", len(convo.trim([{"role": "user", "text": f"u{i}"} for i in range(50)])) <= convo.KEEP_TURNS * 2)
convo.save(_c)
check("convo: reloads what was saved (survives restart)", convo.load() == _c)
_rec = convo.record([{"role": "bogus"}, {"text": ""}], "hi", "hey")
check("convo: corrupt entries are dropped on load",
      [{"role": t["role"], "text": t["text"]} for t in _rec]
      == [{"role": "user", "text": "hi"}, {"role": "model", "text": "hey"}])
# Turns are stamped now so a conversation can go stale. Without this a session
# from last night gets reseeded into this morning's first question and Neo
# answers into a thread the user left hours ago.
check("convo: every recorded turn carries a timestamp",
      all(isinstance(t.get("at"), float) for t in _rec))
check("convo: a conversation from hours ago is not carried into a new one",
      convo.fresh([{"role": "user", "text": "old", "at": 1.0}], now=9e9) == [])
check("convo: one from a minute ago is",
      len(convo.fresh([{"role": "user", "text": "new", "at": 9e9 - 60}],
                      now=9e9)) == 1)


# =========================================================================== #
# The August fixes: the fn truncation, cloud ears, per-turn thinking, the model
# router, and the live conversation. All pure logic — no audio device, no
# network, no Mac required.
# =========================================================================== #

# ---- the fn bug: a single bad reading must not cut a live hold ----
# This is the regression that matters. The old guard released on ONE "key is
# up" reading, and because that reading is unreliable for fn, it fired on holds
# still in progress — hence the log full of half-second captures of sentences
# the user was still speaking.
check("fn: one 'up' reading does NOT release when 3 votes are required",
      neo.reconcile_fn_state(True, False, 1.0, 45, up_votes=1, votes_needed=3) is None)
check("fn: two 'up' readings still do not release",
      neo.reconcile_fn_state(True, False, 1.0, 45, up_votes=2, votes_needed=3) is None)
check("fn: three consecutive 'up' readings DO release (a real lost key-up)",
      neo.reconcile_fn_state(True, False, 1.0, 45, up_votes=3, votes_needed=3) == "release")
check("fn: an unknown reading never releases, however many votes",
      neo.reconcile_fn_state(True, None, 1.0, 45, up_votes=9, votes_needed=3) is None)
check("fn: a key still held never releases",
      neo.reconcile_fn_state(True, True, 5.0, 45, up_votes=0, votes_needed=3) is None)
check("fn: max-hold still fires on a genuinely stuck key",
      neo.reconcile_fn_state(True, True, 46.0, 45, up_votes=0, votes_needed=3) == "maxhold")
check("fn: nothing happens when we don't think the key is down",
      neo.reconcile_fn_state(False, False, 99.0, 45, up_votes=9, votes_needed=3) is None)
check("fn: old single-reading callers still behave as before",
      neo.reconcile_fn_state(True, False, 1.0, 45) == "release")

# ---- and the real fix: no release unless the tap actually dropped ----
# Votes alone did not stop it. ptt_physically_down is not wrong at random for
# fn — it stays wrong for as long as the HID layer keeps the synthesized flag
# clear, which outlasts three votes at 0.25s. The log: one press, force-released
# at :10, re-latched, released again at :15. Mic indicator blinking mid-sentence.
#
# A key-up can only be LOST if the tap was dead when it fired. With a live tap
# there is nothing to recover, so the guard must not guess.
check("fn: a healthy tap means no release, however many 'up' readings",
      neo.reconcile_fn_state(True, False, 1.0, 45, up_votes=99, votes_needed=3,
                             tap_lost=False) is None)
check("fn: a dropped tap plus enough votes still recovers the lost key-up",
      neo.reconcile_fn_state(True, False, 1.0, 45, up_votes=3, votes_needed=3,
                             tap_lost=True) == "release")
check("fn: a dropped tap alone is not enough — the OS must agree it is up",
      neo.reconcile_fn_state(True, True, 1.0, 45, up_votes=0, votes_needed=3,
                             tap_lost=True) is None)
check("fn: max-hold is unconditional — a healthy tap does not exempt it",
      neo.reconcile_fn_state(True, True, 46.0, 45, up_votes=0, votes_needed=3,
                             tap_lost=False) == "maxhold")

# The kill window itself. 2s put an ordinary mid-sentence pause inside it.
check("fn: the stuck-listening window is no longer inside a normal pause",
      neo.STUCK_LISTEN_SEC >= 5)
check("fn: the guard samples several times a second and needs agreement",
      neo.FN_GUARD_INTERVAL <= 0.5 and neo.FN_RELEASE_VOTES >= 2)
check("fn: a genuinely lost key-up still recovers in about a second",
      neo.FN_GUARD_INTERVAL * neo.FN_RELEASE_VOTES <= 1.5)

# ---- two OS authorities, and DOWN WINS ----
import Quartz as _Q
if hasattr(_Q, "_FN_STATE"):            # only the off-Mac stub can be driven
    _saved_fn = dict(_Q._FN_STATE)
    _Q._FN_STATE["flags"], _Q._FN_STATE["keys"] = 0, {neo.FN_KEYCODE: True}
    check("fn: key-state down + flags up -> DOWN (this is the fix)",
          neo.ptt_physically_down(neo.FN_KEYCODE) is True)
    _Q._FN_STATE["flags"], _Q._FN_STATE["keys"] = neo.FN_MASK, {}
    check("fn: flags down + key-state up -> DOWN",
          neo.ptt_physically_down(neo.FN_KEYCODE) is True)
    _Q._FN_STATE["flags"], _Q._FN_STATE["keys"] = 0, {}
    check("fn: only when BOTH authorities say up is it up",
          neo.ptt_physically_down(neo.FN_KEYCODE) is False)
    _Q._FN_STATE.update(_saved_fn)

check("fn: fn is the push-to-talk key", neo.FN_KEYCODE in neo.PTT_KEYS)
check("fn: and it is the ONLY one by default (no second key to reason about)",
      set(neo.PTT_KEYS) == {neo.FN_KEYCODE})
# The keycode refines the decision; it must never GATE it. Some keyboards
# report nothing useful on the fn flagsChanged event, and treating that as
# "not a push-to-talk key" makes fn silently dead.
check("fn: an unrecognised keycode still resolves via the mask",
      any(neo.FN_MASK & m for m in neo.PTT_KEYS.values()))

# ---- watchdog: same conditions, wider window ----
check("watchdog: still fires on the busy-free stuck case",
      neo.watchdog_should_unstick("listening", False, True, False, 12, 8) is True)
check("watchdog: never fires while the key is confirmed down",
      neo.watchdog_should_unstick("listening", False, True, True, 12, 8) is False)
check("watchdog: never fires on an unknown key reading",
      neo.watchdog_should_unstick("listening", False, True, None, 12, 8) is False)
check("watchdog: a 3-second pause no longer trips it",
      neo.watchdog_should_unstick("listening", False, True, False, 3, 8) is False)

# ---- thinking budget, decided per turn instead of switched off forever ----
check("think: small talk gets no thinking (latency is the product there)",
      commands.thinking_budget_for("what time is it") == commands.THINK_OFF)
check("think: a real task gets the light budget, not an open-ended one",
      commands.thinking_budget_for("figure out why the deploy failed and fix it")
      == commands.THINK_LIGHT)
check("think: a correction thinks a little",
      commands.thinking_budget_for("no, that didn't work") == commands.THINK_LIGHT)
check("think: a running Claude job means think",
      commands.thinking_budget_for("anything", has_job=True) == commands.THINK_LIGHT)
check("think: reasoning is available, just bounded",
      commands.THINK_LIGHT > 0)
# Measured: uncapped thinking = 9.3s of silence AND a wrong answer. No spoken
# turn is ever allowed to spend that.
check("think: no spoken turn can ask for uncapped thinking",
      commands.cap_spoken_budget(commands.THINK_DYNAMIC) == commands.THINK_SPOKEN_MAX)
check("think: a big budget is clamped to the silence ceiling",
      commands.cap_spoken_budget(99999) == commands.THINK_SPOKEN_MAX)
check("think: zero stays zero through the cap",
      commands.cap_spoken_budget(0) == 0)
check("think: the ceiling is short enough to sit through",
      commands.THINK_SPOKEN_MAX <= 1024)
check("think: every spoken budget survives the cap unchanged",
      all(commands.cap_spoken_budget(commands.thinking_budget_for(t))
          == commands.thinking_budget_for(t)
          for t in ("what time is it", "no that didn't work",
                    "figure out why the deploy failed", "tell me about the market")))

# ---- the invented daily cap ----
check("usage: the 250/day cap was invented and is no longer the ceiling",
      commands.DAILY_LIMIT > 250)

# ---- live is the default, not a mode you invoke ----
check("live: it is the default press behaviour", neo.MODE == "live")
check("live: a session hangs up quickly, since it opens on every interaction",
      neo.LIVE_IDLE_SEC <= 60)
check("live: hangs up on 'that's all'", commands.wants_live_off("that's all"))
check("live: hangs up on 'hang up'", commands.wants_live_off("hang up"))
check("live: does not hang up on unrelated speech",
      not commands.wants_live_off("what's the weather"))

# ---- providers: the model router ----
import providers
_cands = {"chat": [("gemini", "a"), ("groq", "b"), ("gemini", "c")]}
check("providers: candidates needing a missing key are skipped",
      providers.usable_candidates("chat", {"gemini"}, _cands)
      == [("gemini", "a"), ("gemini", "c")])
check("providers: a second provider lights up when its key appears",
      providers.usable_candidates("chat", {"gemini", "groq"}, _cands)
      == [("gemini", "a"), ("groq", "b"), ("gemini", "c")])
check("providers: a preferred provider is promoted to the front",
      providers.usable_candidates("chat", {"gemini", "groq"}, _cands,
                                  preferred="groq")[0] == ("groq", "b"))
check("providers: preferring an absent provider changes nothing",
      providers.usable_candidates("chat", {"gemini"}, _cands, preferred="groq")
      == [("gemini", "a"), ("gemini", "c")])
check("providers: an unknown job resolves to nothing rather than crashing",
      providers.usable_candidates("nope", {"gemini"}, _cands) == [])
check("providers: a fresh cache entry is used",
      providers.cache_is_fresh({"model": "x", "at": 100.0}, 200.0, ttl=1000))
check("providers: a stale entry is re-probed",
      not providers.cache_is_fresh({"model": "x", "at": 100.0}, 9999.0, ttl=10))
check("providers: junk cache entries are ignored, not trusted",
      not providers.cache_is_fresh({"at": "banana"}, 200.0)
      and not providers.cache_is_fresh(None, 200.0)
      and not providers.cache_is_fresh({"model": ""}, 200.0))
check("providers: a failing model is demoted, not forgotten",
      providers.demote([("g", "a"), ("g", "b")], ("g", "a"))
      == [("g", "b"), ("g", "a")])
check("providers: every job has a candidate needing only the Gemini key",
      all(any(p == "gemini" for p, _m in providers.CANDIDATES[j])
          for j in ("chat", "heavy", "fast", "stt", "live")))
check("providers: free-by-default — the paid provider is last for transcription",
      providers.CANDIDATES["stt"][0][0] == "gemini")
_saved_key = os.environ.get("CEREBRAS_API_KEY")
os.environ["CEREBRAS_API_KEY"] = "paste_your_key_here"
check("providers: a .env placeholder is not a usable key",
      not providers.have("cerebras"))
os.environ["CEREBRAS_API_KEY"] = "sk-a-real-looking-key-value"
check("providers: a real-looking key is usable", providers.have("cerebras"))
if _saved_key is None:
    os.environ.pop("CEREBRAS_API_KEY", None)
else:
    os.environ["CEREBRAS_API_KEY"] = _saved_key
check("providers: an unknown provider is never usable",
      not providers.have("nonexistent-provider"))

# ---- ears: cloud transcription plumbing ----
import ears
import numpy as _np
_wav = ears.to_wav_bytes(_np.zeros(1600, dtype="float32"), 16000)
check("ears: produces a real WAV container",
      _wav[:4] == b"RIFF" and _wav[8:12] == b"WAVE")
check("ears: 16-bit mono at the rate we asked for",
      _wav[22] == 1 and int.from_bytes(_wav[24:28], "little") == 16000
      and _wav[34] == 16)
check("ears: loud audio clips instead of wrapping to the opposite sign",
      ears.to_wav_bytes(_np.array([2.0, -2.0], dtype="float32"), 16000)[44:]
      == (32767).to_bytes(2, "little", signed=True)
       + (-32767).to_bytes(2, "little", signed=True))
check("ears: Whisper's caption boilerplate never reaches the brain",
      ears.clean_transcript("Thanks for watching!") == "")
check("ears: bracketed stage directions are stripped",
      ears.clean_transcript("[BLANK_AUDIO] open safari") == "open safari")
check("ears: punctuation-only output is not a transcript",
      ears.clean_transcript("... ?!") == "")
check("ears: empty in, empty out",
      ears.clean_transcript("") == "" and ears.clean_transcript(None) == "")
check("ears: real speech survives untouched",
      ears.clean_transcript("  open  safari please ") == "open safari please")
_hints = ears.build_hints([{"text": "Sam Lee runs the project from school"}])
check("ears: proper nouns from memory become recogniser hints",
      "Sam" in _hints and "Lee" in _hints)
check("ears: the built-in vocabulary is always present",
      "Gemini" in _hints and "Neo" in _hints)
check("ears: hints are deduped and bounded",
      len(_hints) == len({h.lower() for h in _hints}) and len(_hints) <= 60)
check("ears: a one-word command is still worth acting on",
      ears.transcript_is_worth_it("stop") and not ears.transcript_is_worth_it(""))

_e = ears.Ears(client=None, local=lambda a: "hello there", log=lambda *a: None)
_e._cloud_off_until = float("inf")          # pretend the cloud is unreachable
check("ears: falls back to on-device when the cloud is out",
      _e.transcribe(_np.zeros(1600, dtype="float32")) == "hello there"
      and _e.last_path == "local")
_e2 = ears.Ears(client=None, local=None, log=lambda *a: None)
_e2._cloud_off_until = float("inf")
check("ears: no local model and no cloud degrades to silence, not a crash",
      _e2.transcribe(_np.zeros(160, dtype="float32")) == "")
_e3 = ears.Ears(client=None,
                local=lambda a: (_ for _ in ()).throw(RuntimeError("boom")),
                log=lambda *a: None)
_e3._cloud_off_until = float("inf")
check("ears: a crashing local model loses the turn, never the process",
      _e3.transcribe(_np.zeros(160, dtype="float32")) == "")
_e4 = ears.Ears(client=None, local=lambda a: "x", log=lambda *a: None)
_e4._fails = 2
_e4._trip()
check("ears: repeated cloud failures trip the breaker instead of paying the timeout",
      not _e4._cloud_ready())

# ---- live: tool dispatch and the hang-up path ----
import live as live_mod
def _echo(text: str) -> str:
    return "said " + text
def _explodes() -> str:
    raise RuntimeError("nope")
_tools = live_mod.tool_map([_echo, _explodes, "not a function", None])
check("live: builds a name->callable map, skipping junk",
      set(_tools) == {"_echo", "_explodes"})
check("live: dispatches a tool and returns its result",
      live_mod.call_tool(_tools, "_echo", {"text": "hi"}) == {"result": "said hi"})
check("live: an unknown tool is an error message, not an exception",
      "error" in live_mod.call_tool(_tools, "_missing", {}))
check("live: a raising tool is an error message, not a dead socket",
      "error" in live_mod.call_tool(_tools, "_explodes", {}, log=lambda *a: None))
check("live: wrong arguments come back as a readable error",
      "error" in live_mod.call_tool(_tools, "_echo", {"wrong": 1}, log=lambda *a: None))
check("live: a huge tool result is truncated before it goes back up",
      len(live_mod.call_tool(_tools, "_echo", {"text": "x" * 99999})["result"]) <= 4000)
check("live: every one of Neo's real tools is reachable by name",
      set(live_mod.tool_map(agent.TOOLS)) >= {"search_web", "get_weather",
                                              "hand_to_claude", "look_at_screen"})
check("live: an idle session hangs up rather than holding the mic open",
      live_mod.should_hang_up(0, 999999, idle_timeout=30) is True)
check("live: an active session stays open",
      live_mod.should_hang_up(1000, 1010, idle_timeout=30) is False)

# A press must open a conversation, not a recording — and must never do device
# I/O on the tap thread, which is what kills the event tap.
def _press_opens_a_conversation():
    import threading as _th, queue as _q
    n = neo.Neo.__new__(neo.Neo)
    n.state = neo.State()
    n._busy = _th.Lock()
    n._interrupt = _th.Event()
    n._audio_cmds = _q.Queue()
    n.live = None
    n._live_degraded = False
    n.brain = object()                 # warmed up
    started = []
    n._start_live = lambda holding=False: started.append(holding)
    n.on_press()
    n.on_release()
    import time as _t
    _t.sleep(0.15)
    # Opened while the key is already down, so the first word isn't lost to
    # connection time, and nothing was queued to the recorder.
    return (started == [True]
            and n.state.get() == "listening"
            and n._audio_cmds.empty())
check("live: a press opens a conversation already armed to record",
      _press_opens_a_conversation())

def _press_records_once_live_is_unavailable():
    import threading as _th, queue as _q
    n = neo.Neo.__new__(neo.Neo)
    n.state = neo.State()
    n._busy = _th.Lock()
    n._interrupt = _th.Event()
    n._audio_cmds = _q.Queue()
    n.live = None
    n._live_degraded = True            # live already failed on this machine
    n.brain = object()
    n.on_press()
    queued = n._audio_cmds.get_nowait()
    n.on_release()
    return queued == ("start", None) and n._audio_cmds.get_nowait() == ("stop", False)
check("live: once live is unavailable, a press falls back to hold-to-talk",
      _press_records_once_live_is_unavailable())

def _no_conversation_before_warmup():
    import threading as _th, queue as _q
    n = neo.Neo.__new__(neo.Neo)
    n.state = neo.State()
    n._busy = _th.Lock()
    n._interrupt = _th.Event()
    n._audio_cmds = _q.Queue()
    n.live = None
    n._live_degraded = False
    n.brain = None                     # still warming up
    return not n._live_default()
check("live: a press before warmup doesn't try to open a socket",
      _no_conversation_before_warmup())
check("live: audio rates match what the API actually speaks",
      live_mod.IN_RATE == 16000 and live_mod.OUT_RATE == 24000)

_pb = live_mod.Playback()
_pb.write(b"\x01\x02" * 100)
check("live: audio buffers for playback", _pb.pending() == 200 and _pb.playing)
_pb.flush()
check("live: barge-in drops everything not yet played",
      _pb.pending() == 0 and not _pb.playing)
_pb.stop()          # must be safe with no stream ever opened
check("live: teardown is safe when nothing was opened", _pb.pending() == 0)

# ---- weather has a real tool now, instead of falling through to a web scrape ----
check("weather: registered in the toolbox", agent.get_weather in agent.TOOLS)
check("weather: the docstring tells the brain not to search for it",
      "never search" in (agent.get_weather.__doc__ or "").lower())


# =========================================================================== #
# What live probing against the real key turned up (2026-08-25). Every check
# below encodes a fact measured from the API, not read in a doc.
# =========================================================================== #

# ---- thinking config is per-model-family, and the wrong shape is a 400 ----
# Measured: gemini-2.5-flash takes thinkingBudget and REJECTS thinkingLevel;
# gemini-3.5-flash-lite is the exact opposite. Neo sent thinking_budget=0 to
# everything, so on the flash-lite models — the ones doing transcription and
# result-polishing — every call was a 400 and every turn silently fell back to
# the small local model. This is the highest-value bug in the whole rebuild.
check("think: budget-style models get a budget",
      providers.thinking_kwargs(providers.STYLE_BUDGET, 0) == {"thinking_budget": 0})
check("think: level-style models get a level, never a budget",
      providers.thinking_kwargs(providers.STYLE_LEVEL, 0) == {"thinking_level": "LOW"})
check("think: a dynamic budget maps to HIGH on level-style models",
      providers.thinking_kwargs(providers.STYLE_LEVEL, -1) == {"thinking_level": "HIGH"})
check("think: a big budget maps to HIGH, a small one to LOW",
      providers.thinking_kwargs(providers.STYLE_LEVEL, 8192) == {"thinking_level": "HIGH"}
      and providers.thinking_kwargs(providers.STYLE_LEVEL, 512) == {"thinking_level": "LOW"})
check("think: a model that takes neither gets neither",
      providers.thinking_kwargs(providers.STYLE_NONE, 0) is None)
check("think: no budget means no thinking config at all",
      providers.thinking_kwargs(providers.STYLE_BUDGET, None) is None
      and providers.thinking_kwargs(providers.STYLE_LEVEL, None) is None)
check("think: an unprobed model sends nothing rather than a guess",
      providers.thinking_kwargs(None, 0) is None)
check("think: level_for_budget never returns a level for 'no thinking'",
      providers.level_for_budget(None) is None)

# ---- routing: the smartest model that is still fast enough ----
# Re-measured 11 Sept 2026 (first token, streaming, a two-sentence answer):
# 3.8-flash 2.4-2.9s, 3.5-flash 2.2s, 3.6-flash 3.6-5.4s — and 2.5-flash now
# returns 404 NOT_FOUND on the second key: it is being retired. The brain
# answering on the weakest, disappearing model is what "Neo is getting dumber"
# was. The live socket carries the conversation; chat carries every typed
# answer, tool polish and vision call, so it leads with the newest flash.
check("routing: the chat model is the newest flash, with 2.5-flash only as a backstop",
      providers.CANDIDATES["chat"][0][1] == "gemini-3.8-flash"
      and "gemini-2.5-flash" in [m for _, m in providers.CANDIDATES["chat"]])
check("routing: the fast lane leads with the 400ms tier",
      providers.CANDIDATES["fast"][0][1] == "gemini-3.5-flash-lite")
check("routing: transcription uses the fast tier too",
      providers.CANDIDATES["stt"][0][1] == "gemini-3.5-flash-lite")
check("routing: live uses the confirmed-working speech-to-speech model",
      providers.CANDIDATES["live"][0][1] == "gemini-3.1-flash-live-preview")
# Measured: gemini-2.5-pro is 404 'no longer available to new users' and every
# Pro model returns 429 on this key. A candidate list naming them would spend a
# probe on a model that cannot work.
check("routing: no dead Pro model is named anywhere",
      not any(m.startswith("gemini-2.5-pro") or m == "gemini-pro-latest"
              or m == "gemini-3.1-pro-preview"
              for job in providers.CANDIDATES.values() for _p, m in job))
check("routing: 2.5-flash-lite (404 for new keys) is gone",
      not any(m == "gemini-2.5-flash-lite"
              for job in providers.CANDIDATES.values() for _p, m in job))
check("routing: grounded knowledge routes to a model whose search is free",
      providers.CANDIDATES["grounded"][0][1] == "gemini-2.5-flash")

# ---- built-in tools cannot be mixed with function calling ----
# The API is explicit: "Built-in tools ({google_search}) and Function Calling
# cannot be combined in the same request." So grounding has to be a separate
# sub-call wearing a normal tool's clothes.
check("knowledge: search/read/calculate are ordinary Neo tools",
      {"search_web", "read_webpage", "calculate"} <= {t.__name__ for t in agent.TOOLS})
check("knowledge: calculate tells the brain not to do mental arithmetic",
      "bad at" in (agent.calculate.__doc__ or "").lower())
check("knowledge: grounded results still carry the untrusted-content banner",
      "UNTRUSTED" in agent._UNTRUSTED)
def _grounded_falls_back_when_ungrounded():
    """No client bound -> must run the old scrape path, not raise."""
    import agent as A
    saved = A._client
    A._client = None
    try:
        out = A._grounded("q", "google_search", "Searching", lambda: "OLD PATH")
    finally:
        A._client = saved
    return out == "OLD PATH"
check("knowledge: losing grounding degrades to the old path, never raises",
      _grounded_falls_back_when_ungrounded())
def _grounded_never_raises():
    import agent as A
    saved = A._client
    A._client = None
    try:
        return isinstance(A._grounded("q", "google_search", "x"), str)
    finally:
        A._client = saved
check("knowledge: a tool with no fallback still returns a string",
      _grounded_never_raises())
# Real incident (2026-09-09): "what time is the NFL game today?" -> search_web
# returned TOMORROW's game and Neo said there was none tonight. The grounded
# model has no clock, so the bare query left "today" for it to guess. The date
# now goes IN the prompt.
def _search_prompt_carries_today():
    import agent as A, datetime as _d
    try:
        from zoneinfo import ZoneInfo as _Z
        now = _d.datetime(2026, 9, 9, 19, 24, tzinfo=_Z("America/New_York"))
    except Exception:
        return True   # no tz db in this sandbox: the anchor test can't be exact
    p = A._search_prompt("what time is the NFL game today?", now)
    # The real date the query is asking about must be in the prompt, before it.
    return ("September 9th, 2026" in p and "Wednesday" in p
            and p.index("September 9th, 2026") < p.index("NFL"))
check("knowledge: search anchors the query to today's real date, not the model's guess",
      _search_prompt_carries_today())

# ---- fillers: a tool call must not be dead air ----
# Measured: the model decides to call a tool at ~500ms, then goes silent until
# the tool returns. A second of silence mid-conversation reads as a crash.
check("live: a slow tool gets covered by a spoken filler",
      live_mod.should_fill(["search_web"]) and live_mod.filler_for(["search_web"]))
check("live: an instant tool is not covered (it would talk over the answer)",
      not live_mod.should_fill(["open_app"]))
check("live: a mix containing anything slow is covered",
      live_mod.should_fill(["open_app", "search_web"]))
check("live: an unknown tool still gets a generic filler",
      live_mod.filler_for(["something_new"]) in live_mod._DEFAULT_FILLERS)
check("live: no tools means nothing to say",
      live_mod.filler_for([]) == "" and not live_mod.should_fill([]))
# _FILLERS maps a tool name to a LIST of lines. This used to be written as if
# the values were strings — `len(v) < 40` on a list is a length in ITEMS and
# `"\n" not in v` on a list is a membership test, so both were trivially true
# and the check asserted nothing about the lines it was meant to be guarding.
_lines = [ln for v in live_mod._FILLERS.values() for ln in v]
check("live: every filler is one short spoken line",
      _lines and all(isinstance(ln, str) and "\n" not in ln for ln in _lines))
check("live: a filler is short enough not to collide with the real answer",
      all(len(ln) < 130 for ln in _lines))

# ---- the nagging cards ----
check("cards: calendar reminders still come through", sentinel.card_allowed("calendar"))
check("cards: todo and due dates still come through",
      sentinel.card_allowed("todo") and sentinel.card_allowed("due"))
for _kind in ("leads", "money", "pipeline", "git"):
    check(f"cards: {_kind} cards no longer interrupt", not sentinel.card_allowed(_kind))
check("cards: an unknown kind is silent by default",
      not sentinel.card_allowed("something-new"))
check("cards: NEO_CARDS=all restores the firehose",
      sentinel.card_allowed("leads", allowed={"all"}))
check("cards: a missing kind doesn't crash the filter",
      not sentinel.card_allowed(None))

# ---- weather: one round trip, not two ----
def _weather_caches_geocode():
    import agent as A
    A._GEO_CACHE.clear()
    A._GEO_CACHE["testville"] = {"name": "Testville", "latitude": 0, "longitude": 0}
    return A._GEO_CACHE.get("testville") is not None
check("weather: geocodes are cached so home weather is a single call",
      _weather_caches_geocode())


# ---- the boot line must not lie ----
# First real run printed "heavy=unresolved fast=unresolved stt=unresolved
# live=unresolved" when nothing was wrong — those jobs simply hadn't been asked
# for yet. A boot line that reads like a failure IS a failure.
_warm = {"chat": {"model": "m-chat"}, "stt": {"model": "m-stt"},
         "live": {"model": "m-live"}}
check("boot: resolved jobs are named",
      "chat=m-chat" in providers.describe(cache=_warm)
      and "live=m-live" in providers.describe(cache=_warm))
check("boot: a lazy job says 'on demand', not 'unresolved'",
      "fast=on demand" in providers.describe(cache=_warm)
      and "unresolved" not in providers.describe(cache=_warm))
check("boot: a job that genuinely can't resolve says so loudly",
      "chat=UNAVAILABLE" in providers.describe(cache={}))
check("boot: stt and live are warmed so they never show lazy",
      set(providers.LAZY_JOBS) == {"heavy", "fast"})


# ---- one Neo at a time ----
# Only matters once Neo starts at login AND you can still run it by hand: two
# copies fighting over one mic and one event tap looks exactly like the freeze
# bug this rebuild was about.
import tempfile as _tf
_lockpath = os.path.join(_tf.gettempdir(), "neo_singleton_test.lock")
if os.path.exists(_lockpath):
    os.remove(_lockpath)
check("singleton: the first Neo claims the lock", neo.claim_singleton(_lockpath))
def _second_copy_is_refused():
    """A separate process must be refused — same-process flock would re-acquire."""
    import subprocess, sys as _s, textwrap
    code = textwrap.dedent(f"""
        import sys; sys.path.insert(0, {os.path.dirname(os.path.abspath("neo.py"))!r})
        import fcntl
        h = open({_lockpath!r}, "w")
        try:
            fcntl.flock(h.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            print("GOT")
        except BlockingIOError:
            print("REFUSED")
    """)
    out = subprocess.run([_s.executable, "-c", code], capture_output=True, text=True)
    return "REFUSED" in out.stdout
check("singleton: a second copy is refused while the first holds the lock",
      _second_copy_is_refused())
check("singleton: an unlockable path never blocks a real start",
      neo.claim_singleton("/proc/definitely/not/writable/neo.lock") is True)


# ---- install once, runs forever ----
import tempfile as _t2, time as _t3
_srcdir = _t2.mkdtemp()
check("reload: an empty folder fingerprints to nothing",
      neo.source_fingerprint(_srcdir) == 0.0)
open(os.path.join(_srcdir, "a.py"), "w").write("x")
_fp1 = neo.source_fingerprint(_srcdir)
check("reload: a source file gives a fingerprint", _fp1 > 0)
_t3.sleep(0.01)
os.utime(os.path.join(_srcdir, "a.py"), (_fp1 + 10, _fp1 + 10))
check("reload: editing a file moves the fingerprint forward",
      neo.source_fingerprint(_srcdir) > _fp1)
# Tests churn constantly while Neo runs; restarting on every test edit would be
# maddening, so they're excluded from the fingerprint.
open(os.path.join(_srcdir, "test_thing.py"), "w").write("y")
os.utime(os.path.join(_srcdir, "test_thing.py"), (_fp1 + 9999, _fp1 + 9999))
check("reload: test files never trigger a restart",
      neo.source_fingerprint(_srcdir) < _fp1 + 9999)
open(os.path.join(_srcdir, "notes.md"), "w").write("z")
os.utime(os.path.join(_srcdir, "notes.md"), (_fp1 + 9999, _fp1 + 9999))
check("reload: non-source files never trigger a restart",
      neo.source_fingerprint(_srcdir) < _fp1 + 9999)
check("reload: a missing folder is 0, not a crash",
      neo.source_fingerprint("/no/such/dir/anywhere") == 0.0)

def _reload_fires_on_change():
    """The watcher must notice a change and hand control to on_change."""
    d = _t2.mkdtemp()
    f = os.path.join(d, "mod.py")
    open(f, "w").write("1")
    fired = []
    saved_here, neo.HERE = neo.HERE, d
    try:
        import threading as _th
        t = _th.Thread(target=neo._reload_watcher,
                       kwargs={"interval": 0.02, "settle": 0.05,
                               "settle_max": 1.0,
                               "restartable": lambda: True,
                               "on_change": lambda: fired.append(1)},
                       daemon=True)
        t.start()
        _t3.sleep(0.05)
        os.utime(f, (_t3.time() + 60, _t3.time() + 60))
        t.join(timeout=2)
    finally:
        neo.HERE = saved_here
    return fired == [1]
check("reload: the watcher fires exactly once when source changes",
      _reload_fires_on_change())


# ---- one edit, one restart ----
# 246 of the first 329 boots in neo.log were "Code changed on disk". A save is
# rarely one file — neo.py, then act.py, then commands.py — and each one used to
# be its own restart, its own ~11s boot, its own window where fn did nothing.
# The settle window collapses a burst of writes into a single restart.
def _settle_collapses_a_burst():
    ticks = {"n": 0}
    # A file being written on every poll for the first 5 ticks, then quiet.
    def fingerprint():
        ticks["n"] += 1
        return float(min(ticks["n"], 5))
    clock = {"t": 0.0}
    def now():
        return clock["t"]
    def sleep(d):
        clock["t"] += d
    settled = neo.settle_fingerprint(
        1.0, interval=0.5, settle=2.0, settle_max=60.0,
        fingerprint=fingerprint, now=now, sleep=sleep)
    # It waited out the whole burst rather than acting on the first write...
    return settled == 5.0 and ticks["n"] > 5
check("reload: a burst of writes settles into ONE restart",
      _settle_collapses_a_burst())


def _settle_gives_up_eventually():
    """A file rewritten forever must not defer the restart forever — Neo would
    run stale code all day and never say why."""
    ticks = {"n": 0}
    def fingerprint():
        ticks["n"] += 1
        return float(ticks["n"])          # never stops moving
    clock = {"t": 0.0}
    settled = neo.settle_fingerprint(
        0.0, interval=0.5, settle=2.0, settle_max=10.0,
        fingerprint=fingerprint,
        now=lambda: clock["t"],
        sleep=lambda d: clock.__setitem__("t", clock["t"] + d))
    return settled > 0 and clock["t"] <= 30
check("reload: a file written forever still restarts (settle_max)",
      _settle_gives_up_eventually())


def _reload_marker_roundtrip():
    """A restart and a crash leave the same hole in the log. The marker is what
    tells them apart on the way back in."""
    d = _t2.mkdtemp()
    m = os.path.join(d, ".neo.reload")
    fresh = neo.consume_reload_mark(m)             # no marker -> not a reload
    neo.mark_reload(m, now=1000.0)
    gap = neo.consume_reload_mark(m, now=1012.0)   # 12s gone
    again = neo.consume_reload_mark(m)             # consumed exactly once
    return fresh is None and gap == 12.0 and again is None
check("reload: the gap is reported once, and only after a real reload",
      _reload_marker_roundtrip())


def _python_identity_is_noticed():
    """macOS pins Input Monitoring to the exact binary, and the bundle execs
    python — so a `brew upgrade python@3.12` voids the grant and fn dies with no
    message anywhere. A first run is not a change; a swap is."""
    d = _t2.mkdtemp()
    f = os.path.join(d, ".neo.python")
    first, prev0 = neo.python_identity_changed("/opt/homebrew/py3.12.13", f)
    same, _ = neo.python_identity_changed("/opt/homebrew/py3.12.13", f)
    moved, prev = neo.python_identity_changed("/opt/homebrew/py3.12.14", f)
    return (first is False and prev0 is None and same is False
            and moved is True and prev == "/opt/homebrew/py3.12.13")
check("reload: a swapped Python interpreter is noticed and named",
      _python_identity_is_noticed())

check("forever: Neo only self-restarts when something will bring it back",
      neo.UNDER_AGENT is (os.getenv("NEO_MANAGED") == "1"))


# ---- the mic macOS picks is often the wrong one ----
# The doctor on the real machine reported the default input as
# "the user Iphone 16 Microphone" — Continuity had quietly promoted a phone to the
# default input. A locked phone hands back silence, which is exactly the
# "(held 0.4s but the mic was basically silent)" line in the log.
_devs = [{"name": "the user Iphone 16 Microphone", "max_input_channels": 1},
         {"name": "MacBook Air Microphone", "max_input_channels": 1},
         {"name": "AirPods Pro", "max_input_channels": 1}]
check("mic: a Continuity iPhone is skipped for the built-in mic",
      neo.pick_input_device(_devs, "the user Iphone 16 Microphone")
      == "MacBook Air Microphone")
check("mic: an iPad is skipped too",
      neo.pick_input_device([{"name": "the user iPad Microphone", "max_input_channels": 1},
                             {"name": "MacBook Air Microphone", "max_input_channels": 1}],
                            "the user iPad Microphone") == "MacBook Air Microphone")
check("mic: bluetooth headsets are still skipped",
      neo.pick_input_device(_devs, "AirPods Pro") == "MacBook Air Microphone")
check("mic: a good default is left alone",
      neo.pick_input_device(_devs, "MacBook Air Microphone") is None)
check("mic: NEO_MIC=default overrides the swap entirely",
      neo.pick_input_device(_devs, "the user Iphone 16 Microphone", "default") is None)
check("mic: an explicit NEO_MIC name wins",
      neo.pick_input_device(_devs, "MacBook Air Microphone", "airpods") == "AirPods Pro")
check("mic: with no built-in to fall back to, we don't invent one",
      neo.pick_input_device([{"name": "the user Iphone 16 Microphone",
                              "max_input_channels": 1}],
                            "the user Iphone 16 Microphone") is None)
check("mic: an unnamed default with only phones visible still prefers built-in",
      neo.pick_input_device(_devs[:2], "") == "MacBook Air Microphone")
check("mic: output-only devices are never chosen as an input",
      neo.pick_input_device([{"name": "MacBook Air Speakers", "max_input_channels": 0},
                             {"name": "the user Iphone 16 Microphone", "max_input_channels": 1}],
                            "the user Iphone 16 Microphone") is None)


# ---- the field that killed every live session ----
# Real outage: live.py sent enable_affective_dialog, the server answered
# "Unknown name enableAffectiveDialog: Cannot find field", the SDK surfaced it
# as `APIError: 1011 Internal error`, and the socket died in the same second it
# opened. From outside it looked exactly like fn being ignored.
import inspect as _insp
import re as _re
_livesrc = _insp.getsource(live_mod)
check("live: enable_affective_dialog is never set (it kills the session)",
      "enable_affective_dialog=True" not in _livesrc
      and '"enable_affective_dialog"' not in _livesrc.split("NOTE:")[-1].split("\n")[0])
check("live: the reason it's banned is written down for the next person",
      "affective" in _livesrc.lower() and "rejects" in _livesrc.lower())

# A preview API will reject some other field eventually, so setup degrades.
check("live: setup has fallback tiers to drop optional config",
      len(live_mod.LiveSession.TIERS) >= 2)
# Assert the ORDER OF SACRIFICE by what each tier gives up, not by what it is
# called — the old check compared tier[0]'s NAME to the string "tuning", so
# renaming a tier broke it while reordering the fields it drops would not have.
_tier_of = {f: i for i, (_n, fields) in enumerate(live_mod.LiveSession.TIERS)
            for f in fields}
check("live: transcripts are the last thing given up",
      _tier_of["output_audio_transcription"] == len(live_mod.LiveSession.TIERS) - 1
      and _tier_of["input_audio_transcription"] == _tier_of["output_audio_transcription"])
check("live: tools survive longer than the output ceiling",
      _tier_of["tools"] > _tier_of["max_output_tokens"])
# THE ONE THAT MATTERS. realtime_input_config carries
# automatic_activity_detection=disabled, which is the whole basis of
# hold-to-talk: release means "answer me". It used to share a tier with
# max_output_tokens, so a server that disliked the token ceiling would have
# silently handed turn-taking back to a voice-activity detector — a
# mid-sentence pause would cut the user off and releasing the key would stop
# meaning anything. The cheap field has to be sacrificed on its own.
check("live: a rejected output ceiling cannot take hold-to-talk with it",
      _tier_of["max_output_tokens"] < _tier_of["realtime_input_config"])
check("live: max_output_tokens is dropped alone",
      len([f for _n, fields in live_mod.LiveSession.TIERS
           for f in fields
           if _tier_of[f] == _tier_of["max_output_tokens"]]) == 1)
# The ceiling itself has to be far past any real spoken turn — this is the cap
# that ended a narration at "Neo: Critically," with no error and no
# turn_complete, and the deck prompt was then written around it.
check("live: the output ceiling is well past any plausible spoken turn",
      int(_re.search(r'NEO_LIVE_MAX_TOKENS", "(\d+)"', _livesrc).group(1)) >= 32768)

# ---- context has to survive a hang-up ----
_sess = live_mod.LiveSession(client=None, model="m", system_instruction="s",
                             history=[{"role": "user", "text": "remember I use Postgres"},
                                      {"role": "model", "text": "Noted."}])
check("live: a session carries prior turns in", len(_sess.history) == 2)
check("live: no history is fine too",
      live_mod.LiveSession(client=None, model="m", system_instruction="s").history == [])
check("live: idle timeout is long enough to think mid-sentence",
      live_mod.IDLE_TIMEOUT_S >= 40)

def _live_turns_are_recorded():
    """Fragments buffer per speaker and commit as whole turns when the other
    side starts — otherwise the conversation is never written down."""
    import types as _pyt
    n = neo.Neo.__new__(neo.Neo)
    saved = []
    brain = _pyt.SimpleNamespace(_turns=[], mem={"facts": []})
    n.brain = brain
    n.live = None
    n._live_pending = None
    n._commit_live_turn = lambda who, text: saved.append((who, text))
    n._live_text("You", "remember that I")
    n._live_text("You", "use Postgres")
    n._live_text("Neo", "Got it.")
    return saved == [("You", "remember that I use Postgres")]
check("live: fragments join into one turn before being recorded",
      _live_turns_are_recorded())

def _pending_turn_is_flushed_on_hangup():
    import types as _pyt
    n = neo.Neo.__new__(neo.Neo)
    saved = []
    n.brain = _pyt.SimpleNamespace(_turns=[], mem={"facts": []})
    n.live = None
    n._live_pending = None
    n._commit_live_turn = lambda who, text: saved.append((who, text))
    # __new__ skips __init__, so the locks real Neo builds there have to be
    # supplied by hand or the flush path raises on the way in.
    import threading as _th
    n._turns_lock = _th.Lock()
    n._live_text("You", "half a sentence")
    n._live_ended("idle")
    return saved == [("You", "half a sentence")] and n._live_pending is None
check("live: a turn cut off by the hang-up is still recorded",
      _pending_turn_is_flushed_on_hangup())


# ---- hold to talk, and long holds specifically ----
# The old bug: hold past ~5s and it got stuck. Manual turn control removes the
# whole class — nothing is waiting for a pause, audio just streams until the key
# comes up. Verified against the real API: a 20-second held turn completes, and
# activity_end -> first audio back is 942 ms.
_ls = live_mod.LiveSession(client=None, model="m", system_instruction="s")
check("hold: a fresh session is not holding and has no open turn",
      not _ls._holding and not _ls._speaking)
_ls.begin_turn()
check("hold: key down opens the mic gate", _ls._holding)
check("hold: but no turn starts until something is actually said",
      not _ls._speaking)
check("hold: letting go after saying nothing closes nothing",
      _ls.end_turn() is False and not _ls._holding)
_ls.begin_turn(); _ls._speaking = True
check("hold: letting go after speaking DOES close the turn",
      _ls.end_turn() is True)
check("hold: and the gate shuts so Neo never hears itself", not _ls._holding)

def _hold_never_times_out():
    """A hold is never 'idle', however long it runs — that's the >5s fix."""
    s = live_mod.LiveSession(client=None, model="m", system_instruction="s")
    s.begin_turn()
    ancient = 0.0
    return (live_mod.should_hang_up(ancient, 99999, 45) is True   # would normally
            and s._holding)                                       # but we're held
check("hold: an in-progress hold is exempt from the idle hang-up",
      _hold_never_times_out())
check("hold: the hard ceiling is far past any real sentence",
      neo.MAX_HOLD_SEC >= 60)
check("hold: silence gate stops a held-but-silent press becoming a turn",
      live_mod.VOICE_RMS > 0 and live_mod.VOICE_CHUNKS >= 2)

import threading as _th
import time as _time

# ---- idempotence, because the tap is not the only caller ----
# A re-latched fn flagsChanged event mid-hold called on_press a second time and
# reset a turn already in progress; the matching stray key-up ended it twice.
# Both directions have to be no-ops now.
def _double_press_is_one_turn():
    s = live_mod.LiveSession(client=None, model="m", system_instruction="s")
    s.begin_turn()
    first = s._hold_id
    s._speaking = True            # we are mid-sentence
    s.begin_turn()                # the stray re-latch
    return s._hold_id == first and s._speaking and s._holding
check("hold: a second press mid-hold does not restart the turn",
      _double_press_is_one_turn())

def _double_release_ends_once():
    s = live_mod.LiveSession(client=None, model="m", system_instruction="s")
    s.begin_turn(); s._speaking = True
    s.end_turn()
    depth = s._mic_q.qsize()
    s.end_turn()                  # the stray second key-up
    return s._mic_q.qsize() == depth
check("hold: a second key-up does not end the turn twice",
      _double_release_ends_once())

# ---- the mic open must not run on the event-tap thread ----
# Opening a PortAudio input stream is device I/O in the hundreds of ms. Block
# the tap in it and macOS disables the tap, the key-UP goes to nobody, and the
# recorder runs forever — the original freeze, reintroduced by a synchronous
# call in begin_turn.
def _mic_open_is_off_thread():
    s = live_mod.LiveSession(client=None, model="m", system_instruction="s")
    here = _th.current_thread().ident
    seen = {}
    s._open_mic = lambda sd, dev=None: seen.setdefault("tid", _th.current_thread().ident)
    s.begin_turn()
    for _ in range(200):
        if "tid" in seen:
            break
        _time.sleep(0.005)
    return seen.get("tid") not in (None, here)
check("hold: the mic opens off the key-handler thread",
      _mic_open_is_off_thread())

def _late_mic_open_closes_itself():
    """Key up while the device was still opening: the stream that lands belongs
    to a hold that is over and must not light the mic indicator."""
    s = live_mod.LiveSession(client=None, model="m", system_instruction="s")
    gate, closed = _th.Event(), {}
    s._open_mic = lambda sd, dev=None: gate.wait(2)
    # _close_mic_now takes the hold id it is closing on behalf of — a stub that
    # takes no argument raises inside the worker, and the worker swallowing it
    # is why this read as "the guard doesn't fire" rather than "the stub is
    # wrong". Match the real signature.
    s._close_mic_now = lambda hold_id=None: closed.setdefault("yes", True)
    s.begin_turn()
    s.end_turn()                  # released before the open returns
    gate.set()
    for _ in range(200):
        if closed:
            break
        _time.sleep(0.005)
    return bool(closed)
check("hold: a mic open that lands after the key came up closes itself",
      _late_mic_open_closes_itself())

def _press_and_release_drive_the_turn():
    """press -> begin_turn, release -> end_turn, on the SAME session (the socket
    stays open between holds so the conversation keeps its thread)."""
    import threading as _th, queue as _q
    n = neo.Neo.__new__(neo.Neo)
    n.state = neo.State(); n._busy = _th.Lock(); n._interrupt = _th.Event()
    n._audio_cmds = _q.Queue(); n._live_degraded = False; n.brain = object()
    calls = []
    class FakeSession:
        def is_running(self): return True
        def begin_turn(self): calls.append("begin")
        def end_turn(self): calls.append("end"); return True
    n.live = FakeSession()
    n.on_press(); n.on_release()
    n.on_press(); n.on_release()
    return calls == ["begin", "end", "begin", "end"] and n._audio_cmds.empty()
check("hold: press begins the turn, release ends it, session reused",
      _press_and_release_drive_the_turn())

def _lost_keyup_answers_rather_than_freezing():
    """The fn-guard forcing a release must END THE TURN, not strand an open mic.
    Worst case is now 'answered early', which is the whole point."""
    import threading as _th, queue as _q
    n = neo.Neo.__new__(neo.Neo)
    n.state = neo.State(); n._busy = _th.Lock(); n._interrupt = _th.Event()
    n._audio_cmds = _q.Queue(); n._live_degraded = False; n.brain = object()
    ended = []
    class FakeSession:
        def is_running(self): return True
        def begin_turn(self): pass
        def end_turn(self): ended.append(1); return True
    n.live = FakeSession()
    n.on_press()
    n.on_release()          # what _FnGuard calls when it thinks the key is up
    return ended == [1]
check("hold: a lost key-up ends the turn cleanly instead of freezing",
      _lost_keyup_answers_rather_than_freezing())


# ---- the runaway tool loop ----
# Real incident: search_web hit a 429'd grounding quota, returned something that
# read like "try again", and the model called it roughly once a second for
# minutes. Neo never spoke, the orb flickered thinking/listening on every call,
# and the mic dot stayed lit. Chat sessions get maximum_remote_calls for free;
# live sessions dispatch tools themselves, so the bound has to be here.
check("loop: a fresh call runs",
      live_mod.loop_verdict("search_web(q=x)", set(), 0) == "run")
check("loop: the identical call a second time is refused",
      live_mod.loop_verdict("search_web(q=x)", {"search_web(q=x)"}, 1) == "repeat")
check("loop: a DIFFERENT call still runs",
      live_mod.loop_verdict("search_web(q=y)", {"search_web(q=x)"}, 1) == "run")
check("loop: the per-turn budget stops even varied calls",
      live_mod.loop_verdict("search_web(q=z)", set(), 6, budget=6) == "budget")
check("loop: budget is checked before repetition, so it always terminates",
      live_mod.loop_verdict("search_web(q=x)", {"search_web(q=x)"}, 99) == "budget")
check("loop: the budget is small enough to notice, big enough to work",
      2 <= live_mod.MAX_TOOL_CALLS_PER_TURN <= 12)
check("loop: signatures ignore argument order",
      live_mod.call_signature("t", {"a": 1, "b": 2})
      == live_mod.call_signature("t", {"b": 2, "a": 1}))
check("loop: different arguments are different signatures",
      live_mod.call_signature("t", {"a": 1}) != live_mod.call_signature("t", {"a": 2}))
check("loop: no arguments is still a valid signature",
      live_mod.call_signature("t", None) == "t()")
def _budget_resets_each_turn():
    s = live_mod.LiveSession(client=None, model="m", system_instruction="s")
    s._tool_calls = 5
    s._tools_seen = {"search_web(q=x)"}
    s.begin_turn()
    ok = s._tool_calls == 0 and not s._tools_seen
    s.end_turn()
    return ok
check("loop: every new question gets a fresh tool budget",
      _budget_resets_each_turn())

# ---- a dead end must not read like 'try again' ----
check("tools: a failed lookup tells the model NOT to retry",
      "don't call it again" in agent._DEAD_END)
def _empty_search_says_dead_end():
    return "don't call it again" in agent._grounded("q", "google_search", "x",
                                                    lambda: "")
check("tools: an empty search result carries the don't-retry line",
      _empty_search_says_dead_end())
check("tools: exhausted grounding quota is rested rather than retried",
      not agent._grounding_rested("never_used"))
agent._rest_grounding("temp_test", 60)
check("tools: once rested, grounding is skipped until it expires",
      agent._grounding_rested("temp_test"))
agent._GROUNDING_OFF_UNTIL.pop("temp_test", None)

# ---- the mic follows the key, not the session ----
# The session stays up 45s so the conversation keeps its thread, but an open
# input stream keeps macOS's orange dot lit that whole time, which reads as
# "always listening".
def _mic_opens_on_key_down():
    s = live_mod.LiveSession(client=None, model="m", system_instruction="s")
    opened = []
    s._open_mic_now = lambda hold_id=None: opened.append("open")
    s.begin_turn()
    return opened == ["open"]
check("mic: opens on key down", _mic_opens_on_key_down())

def _mic_closes_after_the_audio_is_away():
    """Key-up no longer closes the device itself. It queues the end-of-turn
    marker, and the send loop closes the mic AFTER the last chunk is uploaded —
    closing it here used to drain the queue and eat the end of the sentence."""
    s = live_mod.LiveSession(client=None, model="m", system_instruction="s")
    closed = []
    s._open_mic_now = lambda hold_id=None: None
    s._close_mic_now = lambda: closed.append("close")
    s.begin_turn(); s._speaking = True
    s.end_turn()
    queued = list(s._mic_q.queue)
    return (not closed                                   # not yet
            and queued and queued[-1] is live_mod._END_OF_TURN)
check("mic: key-up queues the end marker instead of cutting the device",
      _mic_closes_after_the_audio_is_away())


# ---- the orb must never claim to be listening when it isn't ----
# Screenshot evidence: mic dot OFF in the menu bar, Neo's orb animating as
# though recording. "live" (socket up, nobody talking) was being mapped to
# "listening". Looking like it's listening when it isn't is worse than looking
# asleep — neither state can be trusted after that.
def _orb_state(name, holding):
    import types as _pyt
    n = neo.Neo.__new__(neo.Neo)
    n.state = neo.State()
    n.live = _pyt.SimpleNamespace(_holding=holding)
    n._live_state(name)
    return n.state.get()
check("orb: socket up but key not held -> idle, NOT listening",
      _orb_state("live", False) == "idle")
check("orb: key held -> listening", _orb_state("live", True) == "listening")
check("orb: a stale 'listening' after the key came up is corrected to idle",
      _orb_state("listening", False) == "idle")
check("orb: 'listening' while genuinely held stays listening",
      _orb_state("listening", True) == "listening")
check("orb: thinking and speaking pass straight through",
      _orb_state("thinking", False) == "thinking"
      and _orb_state("speaking", False) == "speaking")
check("orb: no session at all is idle", _orb_state("live", False) == "idle")
def _orb_survives_no_session():
    n = neo.Neo.__new__(neo.Neo)
    n.state = neo.State()
    n.live = None
    n._live_state("live")
    return n.state.get() == "idle"
check("orb: a missing session never crashes the state mirror",
      _orb_survives_no_session())


# ---- silence must never become a turn ----
# The failure this prevents, verbatim from the log — key held ~1s, nothing said:
#     [01:00:57] (holding — opening a conversation)
#     [01:00:58] (let go — thinking)
#     [01:00:59] You: ¿Qué es eso?
# That "You:" line is the model transcribing pure silence, then answering its own
# invention. The first gate compared PEAK amplitude to an RMS-calibrated
# threshold, so room tone sailed through it every time.
import array as _arr
def _pcm(rms, chunks=1):
    amp = int(rms * 32768)
    return _arr.array("h", [amp, -amp] * 800).tobytes() * chunks

check("silence: RMS of true silence reads near zero",
      live_mod.chunk_rms(_pcm(0.0005)) < 0.001)
check("silence: RMS of normal speech reads about right",
      0.008 < live_mod.chunk_rms(_pcm(0.009)) < 0.010)
check("silence: empty audio is 0.0, not a crash",
      live_mod.chunk_rms(b"") == 0.0)
check("silence: a malformed chunk is 0.0, not a crash",
      live_mod.chunk_rms(b"\x01") == 0.0)

# ---- the gate that let a TAP become a question ----------------------------
# the user: "when I just tap the fn button with noise surrounding, it starts
# rambling about some random stuff." He was right, and the cause was a
# calibration comment that had gone stale. live.py believed "normal speech sits
# around 0.009 RMS, true silence near 0.0005" and set the bar at 0.003.
# Re-measured on this machine, 30 s of an ordinary room with NOBODY TALKING:
#   p10 0.0014   p50 0.0042   p90 0.0090   max 0.0926
# The room's median is the number the code thought was speech, so 98.7% of
# silent chunks read as voice and three of them (0.3 s) opened a turn — which a
# tap easily produces. The model then answers a question nobody asked, by
# inventing one.
#
# ROOM is a real slice of that recording, so these checks replay measured
# audio rather than a synthetic tone that no microphone ever produces.
ROOM = [0.0045, 0.0049, 0.0075, 0.0060, 0.0035, 0.0016, 0.0030, 0.0112,
        0.0049, 0.0068, 0.0071, 0.0060, 0.0084, 0.0090, 0.0040, 0.0098,
        0.0060, 0.0124, 0.0051, 0.0071, 0.0042, 0.0027, 0.0023, 0.0018,
        0.0010, 0.0033, 0.0018, 0.0034, 0.0017, 0.0016, 0.0014, 0.0009,
        0.0017, 0.0014, 0.0011, 0.0012, 0.0031, 0.0039, 0.0040, 0.0049,
        0.0046, 0.0056, 0.0042, 0.0051, 0.0044, 0.0040, 0.0030, 0.0022,
        0.0023, 0.0046, 0.0034, 0.0049, 0.0047, 0.0069, 0.0068, 0.0019,
        0.0105, 0.0095, 0.0019, 0.0015]
ROOM_P50 = sorted(ROOM)[len(ROOM) // 2]

def _replay(levels):
    """Feed a level trace through the real gate exactly as _send_loop does.
    Returns the chunk index the turn opened at, or None."""
    seen = []
    for i, v in enumerate(levels):
        seen.append(v)
        if len(seen) > 80:
            del seen[0]
        if live_mod.turn_opens(seen):
            return i
    return None

# Real speech is not a steady tone: it dips to room level between words and on
# every stop consonant. A gate that ignores this is why a "5 consecutive
# chunks" rule rejected quiet speech outright.
_DIPS = [1.0, 1.25, 0.9, 0.35, 1.15, 1.0, 0.8, 0.4, 1.3, 0.95, 1.05, 0.5]
def _speech(mult, n=30):
    return ROOM[:8] + [max(ROOM_P50 * 0.9, ROOM_P50 * mult * _DIPS[k % len(_DIPS)])
                       for k in range(n)]

check("noise: a 0.4s TAP never opens a turn, anywhere in a real room recording",
      all(_replay(ROOM[i:i + 4]) is None for i in range(0, len(ROOM) - 4)))
check("noise: a whole second of real room noise is still not a question",
      _replay(ROOM[:10]) is None)
check("noise: holding a silent mic for two seconds opens nothing",
      _replay(ROOM[:20]) is None)
check("speech: quiet speech is still heard, dips and all",
      _replay(_speech(2.5)) is not None)
check("speech: normal speech is heard within a second of starting",
      (_replay(_speech(4)) or 99) - 8 <= 10)
check("speech: loud speech is heard fastest",
      (_replay(_speech(7)) or 99) <= (_replay(_speech(2.5)) or 99))
check("silence: true digital silence never opens a turn, however long",
      _replay([0.0002] * 60) is None)
check("noise: ONE loud bang is not a sentence",
      _replay([0.002] * 9 + [0.09] + [0.002] * 9) is None)

# The bar has to come from the room, not from a constant — that is the whole
# fix. A loud room must raise it and a silent one must not.
_quiet = live_mod.gate_for([0.0004] * 20)
_loud = live_mod.gate_for([0.012] * 20)
check("gate: a noisy room raises the bar above a quiet one",
      _loud > _quiet)
check("gate: a silent room still keeps an absolute floor",
      _quiet >= live_mod.VOICE_RMS)
check("gate: a very loud room cannot lock the user out entirely",
      live_mod.gate_for([0.5] * 20) <= live_mod.NOISE_CEILING)
check("gate: too few samples falls back to the absolute floor",
      live_mod.gate_for([0.02, 0.02]) == live_mod.VOICE_RMS)
check("gate: the absolute floor sits under real speech",
      0.001 < live_mod.VOICE_RMS < 0.008)
check("speech: a turn cannot open before enough audio exists to judge",
      live_mod.VOICE_CHUNKS >= 6
      and live_mod.turn_opens([0.9] * (live_mod.VOICE_CHUNKS - 1)) is False)
check("speech: voiced chunks are counted in a window, not as a run "
      "(real speech dips between words)",
      live_mod.VOICE_WINDOW > live_mod.VOICE_CHUNKS)
check("speech: pre-roll covers the whole decision window, so no word is lost",
      live_mod.PREROLL_CHUNKS >= live_mod.VOICE_WINDOW)

def _silent_hold_closes_nothing():
    """The whole point: hold, say nothing, let go -> no turn, no answer."""
    s = live_mod.LiveSession(client=None, model="m", system_instruction="s")
    s._open_mic_now = lambda hold_id=None: None
    s._close_mic_now = lambda hold_id=None: None
    s.begin_turn()
    seen = []
    for v in ROOM[:20]:            # two seconds of holding a silent mic
        seen.append(v)
        if live_mod.turn_opens(seen):
            s._speaking = True
    return s.end_turn() is False and not s._speaking
check("silence: hold, say nothing, let go -> Neo stays quiet",
      _silent_hold_closes_nothing())


# ---- Neo has to know what time it is ----
# Asked the time, Neo answered four hours out. The turn-based path had a
# per-turn [[now: ...]] tag; the LIVE path had no timestamp at all, so the model
# fell back to UTC. Assembling the same knowledge separately per path is how
# that gap opened, so it's assembled once in context.py and both paths use it.
import context as _ctx
import datetime as _dt
try:
    from zoneinfo import ZoneInfo as _ZI
    _t1 = _dt.datetime(2026, 8, 25, 13, 38, tzinfo=_ZI("America/New_York"))
    _has_zone = True
except Exception:
    _t1, _has_zone = _dt.datetime(2026, 8, 25, 13, 38), False

if _has_zone:
    _f = _ctx.time_facts(_t1)
    check("time: reports the real local hour, not UTC", _f["time"] == "1:38 pm")
    check("time: names the zone and the offset",
          _f["timezone"] == "EDT" and _f["offset"] == "UTC-04:00")
    check("time: the offset is the four hours Neo used to be wrong by",
          "-04:00" in _f["offset"])
    check("time: knows the weekday and the date",
          _f["day"] == "Tuesday" and _f["date"] == "August 25th, 2026")
    check("time: knows what tomorrow is",
          _f["tomorrow"] == "Wednesday, August 26th")
    check("time: knows roughly where in the day we are",
          _f["part_of_day"] == "afternoon" and _f["weekend"] is False)
    check("time: spoken form is one natural sentence",
          _ctx.spoken_time(_t1).startswith("It's 1:38 pm on Tuesday"))
    _sat = _dt.datetime(2026, 8, 29, 9, 0, tzinfo=_ZI("America/New_York"))
    check("time: knows a Saturday is the weekend",
          _ctx.time_facts(_sat)["weekend"] is True)
    _night = _dt.datetime(2026, 8, 25, 2, 0, tzinfo=_ZI("America/New_York"))
    check("time: 2am is the middle of the night, not morning",
          _ctx.time_facts(_night)["part_of_day"] == "the middle of the night")

check("time: ordinals read like a person says them",
      _ctx._ordinal(1) == "1st" and _ctx._ordinal(2) == "2nd"
      and _ctx._ordinal(3) == "3rd" and _ctx._ordinal(11) == "11th"
      and _ctx._ordinal(21) == "21st" and _ctx._ordinal(13) == "13th")
check("time: local_now always carries a timezone",
      _ctx.local_now().tzinfo is not None)
check("time: an unusable NEO_TZ falls back instead of raising",
      _ctx.local_now("Not/AZone").tzinfo is not None)

# ---- the standing brief reaches BOTH paths ----
_brief = _ctx.brief(_t1 if _has_zone else None)
check("brief: carries a real timestamp", "2026" in _brief)
check("brief: says the stamp is a snapshot, so a long session re-checks",
      "snapshot" in _brief.lower() or "not from this instant" in _brief)
check("brief: tells the model it has no clock of its own",
      "clock" in _brief.lower() and "get_time" in _brief)
check("brief: names the machine it's running on",
      any(w in _brief for w in ("macOS", "Darwin", "Linux")))
for _rule in ("calculate", "search_web", "look"):
    check(f"brief: points at {_rule} instead of guessing", _rule in _brief)
check("brief: forbids claiming an action that didn't happen",
      "NEVER claim" in _brief)
check("brief: tells it to ask rather than answer a misheard question",
      "didn't clearly hear" in _brief)
check("brief: sets the spoken register (no markdown, no lists)",
      "no markdown" in _brief and "no lists" in _brief)

check("brief: the pipeline path builds it", "context.brief()" in
      _insp.getsource(neo.Brain.__init__))

# Named against the whole family, not one method. The prompt assembly moved
# from _start_live into _start_live_inner and this check went on passing a
# `"context.brief()" in getsource(_start_live)` test against a method that no
# longer contained it — i.e. it stopped checking anything and said nothing.
check("brief: the live path builds it too",
      any("context.brief()" in _insp.getsource(getattr(neo.Neo, _m))
          for _m in dir(neo.Neo) if _m.startswith("_start_live")))
check("time: the per-turn tag now comes from the shared source",
      "context.spoken_time" in _insp.getsource(neo._now_tag))

# ---- get_time is a real tool the model can reach ----
check("time: get_time is in the toolbox",
      "get_time" in {t.__name__ for t in agent.TOOLS})
check("time: its docstring tells the model it has no clock",
      "no clock" in " ".join((agent.get_time.__doc__ or "").lower().split()))
check("time: it answers with a real, spoken time",
      "It's" in agent.get_time() and ":" in agent.get_time())
check("time: it's instant, so it gets no spoken filler",
      not live_mod.should_fill(["get_time"]))


# =========================================================================== #
# deck.py — the synced presentation.
#
# The thing that must not break is the SYNC. Everything else degrades: a bad
# visual becomes bullets, a failed art call becomes plain slides, a closed
# window becomes nothing. But a deck that stops advancing, or that jumps
# backwards, is the feature failing in the most visible possible way.
# =========================================================================== #
import json
import context as context_mod
import deck as deck_mod

check("deck: normalise strips everything that isn't a word",
      deck_mod.normalize("A-fib, the SECOND leading cause!") ==
      ["a", "fib", "the", "second", "leading", "cause"])

# Cues are what identify a slide. Filler words identify nothing, and a cue made
# of them fires on every slide at once.
_cue = deck_mod.cue_words("And then the blood pools in the left atrium.")
check("deck: a cue drops filler and keeps the distinctive words",
      "and" not in _cue and "the" not in _cue and "blood" in _cue
      and "atrium" in _cue)
check("deck: a cue is bounded, so a long sentence can still be matched",
      len(deck_mod.cue_words("one two three four five six seven eight nine ten "
                             "eleven twelve thirteen")) <= deck_mod.CUE_WORDS)

check("deck: the cue fires when Neo says those words",
      deck_mod.cue_hit(deck_mod.normalize(
          "so the blood pools in the left atrium and clots"), _cue))
check("deck: it does not fire on unrelated speech",
      not deck_mod.cue_hit(deck_mod.normalize(
          "the weather tomorrow looks fine and clear"), _cue))
# The model rearranges its own clauses constantly. An order-sensitive match
# misses those, and a slide that never advances is worse than one a beat early.
check("deck: word order doesn't matter",
      deck_mod.cue_hit(deck_mod.normalize(
          "in the atrium, left side, blood tends to pool"), _cue))
check("deck: a cue that has scrolled out of the recent window doesn't re-fire",
      not deck_mod.cue_hit(
          deck_mod.normalize("blood pools left atrium " + "filler " * 40), _cue))

_SLIDES = [
    {"narration": "Atrial fibrillation is a chaotic rhythm in the upper chambers.",
     "visual": {"kind": "figure", "shape": "heart", "image": "",
                "labels": [{"text": "here", "say": "upper chambers",
                             "x": 50, "y": 50}]}},
    {"narration": "Because the atrium never fully empties, blood pools and clots.",
     "visual": None},
    {"narration": "That clot travels to the brain, and that is the stroke risk.",
     "visual": None},
]

def _tracker_follows_speech():
    t = deck_mod.Tracker(_SLIDES, min_dwell=0)
    seen = []
    seen += t.feed("Atrial fibrillation is a chaotic rhythm in the upper chambers.")
    seen += t.feed("Because the atrium never fully empties, blood pools and clots.")
    seen += t.feed("That clot travels to the brain, and that's the stroke risk.")
    slides = [e for e in seen if e[0] == "slide"]
    # Slide 0 too. This used to assert [1, 2] — enshrining a bug where the
    # first slide's figure was never revealed on any deck ever built.
    return slides == [("slide", 0), ("slide", 1), ("slide", 2)], seen

_ok, _seen = _tracker_follows_speech()
check("deck: slides advance on the words that belong to them", _ok)
check("deck: a callout fires when its own phrase is spoken",
      ("callout", 0, 0) in _seen)

def _tracker_never_goes_backwards():
    t = deck_mod.Tracker(_SLIDES, min_dwell=0)
    t.feed("Atrial fibrillation is a chaotic rhythm in the upper chambers.")
    t.feed("Because the atrium never fully empties, blood pools and clots.")
    at = t.index
    # He asks a follow-up and Neo repeats slide one's words.
    t.feed("Atrial fibrillation, that chaotic rhythm in the upper chambers again.")
    return t.index >= at
check("deck: repeating an earlier phrase never rewinds the deck",
      _tracker_never_goes_backwards())

def _tracker_never_stalls():
    """The safety valve. If the model paraphrases so heavily that no cue ever
    matches, the deck must still move — a frozen slide one is the worst
    possible failure and the one a user would call broken."""
    t = deck_mod.Tracker(_SLIDES, min_dwell=0)
    for _ in range(12):
        t.feed("completely different wording nothing like the script at all")
    return t.index == len(_SLIDES) - 1
check("deck: heavy paraphrasing still walks the deck to the end",
      _tracker_never_stalls())

def _tracker_is_not_trigger_happy():
    t = deck_mod.Tracker(_SLIDES, min_dwell=0)
    t.feed("Atrial fibrillation is a chaotic rhythm in the upper chambers.")
    return t.index == 0
check("deck: it does not skip ahead while slide one is still being said",
      _tracker_is_not_trigger_happy())

# ---- parsing: the model's answer is never trusted ----
check("deck: a fenced JSON answer still parses",
      deck_mod.parse_plan('```json\n{"title":"T","slides":'
                          '[{"heading":"H","narration":"Some words here."}]}\n```')
      ["title"] == "T")
check("deck: prose either side of the JSON is ignored",
      deck_mod.parse_plan('Sure! {"title":"T","slides":[{"heading":"H",'
                          '"narration":"Words."}]} Hope that helps!') is not None)
check("deck: a trailing comma doesn't cost the deck",
      deck_mod.parse_plan('{"title":"T","slides":[{"heading":"H",'
                          '"narration":"Words.",}],}') is not None)
check("deck: garbage in gives None, not an exception",
      deck_mod.parse_plan("I'm not able to help with that.") is None)
check("deck: a slide with no narration is dropped, not rendered empty",
      len(deck_mod.parse_plan('{"title":"T","slides":[{"heading":"A",'
                              '"narration":""},{"heading":"B",'
                              '"narration":"Real words."}]}')["slides"]) == 1)
check("deck: the deck length is capped whatever the model returns",
      len(deck_mod.parse_plan(json.dumps({"title": "T", "slides": [
          {"heading": "H", "narration": "Words here."}] * 40}))["slides"])
      <= deck_mod.MAX_SLIDES)

# ---- visuals: every malformed shape degrades, none of them crash ----
check("deck: an unknown visual kind is refused",
      deck_mod._clean_visual({"kind": "hologram"}) is None)
check("deck: a one-node flow is refused (there is nothing to show)",
      deck_mod._clean_visual({"kind": "flow",
                              "nodes": [{"id": "a", "label": "x"}]}) is None)
check("deck: an edge pointing at a node that doesn't exist is dropped",
      deck_mod._clean_visual({"kind": "flow", "nodes": [
          {"id": "a", "label": "A", "x": 20, "y": 30, "tone": "blue"},
          {"id": "b", "label": "B", "x": 70, "y": 60, "tone": "rose"}],
          "edges": [{"from": "a", "to": "ghost"}]})["edges"] == [])
check("deck: off-canvas coordinates are pulled back on canvas",
      0 < deck_mod._clean_visual({"kind": "flow", "nodes": [
          {"id": "a", "label": "A", "x": -500, "y": 9000},
          {"id": "b", "label": "B"}]})["nodes"][0]["x"] < 100)
check("deck: a made-up colour falls back instead of breaking the CSS",
      deck_mod._clean_visual({"kind": "crosssection", "style": "rings", "layers": [
          {"label": "A", "tone": "chartreuse"}, {"label": "B", "tone": "blue"}]}
      )["layers"][0]["tone"] in deck_mod.TONES)

def _bad_visuals_leave_a_usable_deck():
    plan = deck_mod.parse_plan(json.dumps({"title": "T", "slides": [
        {"narration": "First thing. Second thing."},
        {"narration": "Another. And more."}]}))
    plan = deck_mod.merge_visuals(plan, '{"visuals":[{"kind":"nonsense"},null]}')
    plan = deck_mod.fallback_visuals(plan)
    # Fallback produces {"kind":"line","text":"..."} — text is the key
    return all(s["visual"].get("text") for s in plan["slides"])
check("deck: a failed visuals call still leaves readable slides",
      _bad_visuals_leave_a_usable_deck())

def _unknown_visual_degrades_gracefully():
    """An unknown kind from the model must not crash the renderer."""
    plan = deck_mod.parse_plan(json.dumps({"title": "T", "slides": [
        {"narration": "First thing here."}]}))
    plan = deck_mod.merge_visuals(plan, json.dumps({"visuals": [
        {"kind": "nope", "labels": [{"text": "look", "say": "first thing",
                                     "x": 20, "y": 30}]}]}))
    plan = deck_mod.fallback_visuals(plan)
    # Should not raise; slide should have a line fallback
    return plan["slides"][0]["visual"]["kind"] == "line"
check("deck: an unknown visual kind degrades to a plain line rather than crashing",
      _unknown_visual_degrades_gracefully())

# ---- rendering ----
def _render_every_kind():
    kinds = [
        {"kind": "flow", "nodes": [{"id": "a", "label": "A", "x": 20, "y": 30,
                                    "tone": "blue", "sub": "n"},
                                   {"id": "b", "label": "B", "x": 70, "y": 60,
                                    "tone": "rose", "sub": ""}],
         "edges": [{"from": "a", "to": "b", "label": "flows", "flow": True}]},
        {"kind": "crosssection", "style": "rings",
         "layers": [{"label": "Outer", "sub": "n", "tone": "green"},
                    {"label": "Inner", "sub": "", "tone": "blue"}]},
        {"kind": "process", "steps": [{"label": "One", "sub": "n"},
                                      {"label": "Two", "sub": ""}]},
        {"kind": "versus",
         "left":  {"title": "X", "points": ["p"], "tone": "blue"},
         "right": {"title": "Y", "points": ["q"], "tone": "rose"}},
        {"kind": "number", "value": "1 in 8", "label": "L", "sub": "n"},
        {"kind": "line", "text": "one sentence key line"},
    ]
    d = {"title": "T",
         "slides": [{"narration": "Words here.", "visual": v}
                    for v in kinds]}
    out = deck_mod.render_html(d)
    return all(len(out) > 3000 for _ in [0]) and "svg" in out and "EventSource" in out
check("deck: every visual kind renders into one self-contained page",
      _render_every_kind())

def _render_escapes_hostile_text():
    """Slide text comes from a model that just read a web page, and a web page
    will happily hand it a script tag."""
    d = {"title": "<script>alert(1)</script>",
         "slides": [{"narration": "w",
                     "visual": {"kind": "line",
                                "text": "</div><script>bad()"}}]}
    out = deck_mod.render_html(d)
    return "<script>alert" not in out and "<script>bad()" not in out
check("deck: hostile text from a web page can't inject into the page",
      _render_escapes_hostile_text())

check("deck: an empty deck still renders rather than raising",
      len(deck_mod.render_html({"title": "T", "slides": []})) > 500)

# ---- the script Neo is told to say ----
def _script_is_just_the_words():
    plan = deck_mod.parse_plan(json.dumps({"title": "T", "slides": [
        {"narration": "First bit."},
        {"narration": "Second bit."}]}))
    script = deck_mod.narration_script(plan)
    # No headings, no numbering — anything structural in here gets read aloud.
    return ("First bit." in script and "Second bit." in script
            and "1." not in script)
check("deck: the script Neo speaks contains no structure to read aloud",
      _script_is_just_the_words())

# ---- prompts ----
check("deck: the plan prompt asks for spoken register, not written",
      "OUT LOUD" in deck_mod.plan_prompt("x") and
      "markdown" in deck_mod.plan_prompt("x"))
check("deck: the plan prompt tells the model why slide openings matter",
      "distinctive" in deck_mod.plan_prompt("x").lower())
check("deck: the visuals prompt carries the finished narration, not the topic",
      "unusual-phrase-xyz" in deck_mod.visuals_prompt(
          {"title": "t", "slides": [{"narration": "unusual-phrase-xyz"}]}))
check("deck: the visuals prompt insists callout cues be copied verbatim",
      "word for word" in deck_mod.visuals_prompt({"title": "t", "slides": []}).lower())

# ---- the tool: presentations are OUT of the toolbox on purpose ----
# A full day went into generated decks and they never got good: drawn diagrams
# from free models were unusable, icon composition was worse, and a layout
# engine of my own produced figures that collided with their own labels. It was
# also the most wrapper-shaped thing in the product — a worse version of
# something a person can already make, that nobody asked a voice assistant for.
# The code stays (the renderer is still the best way to put a verified picture
# on screen); the model is simply no longer offered it.
check("presentations are no longer a tool the model can reach for",
      not any(getattr(f, "__name__", "") == "present" for f in agent.TOOLS))
check("the deck code is still importable, so the good half survives",
      callable(getattr(agent, "present", None)))
check("what replaced it points at the real screen",
      any(getattr(f, "__name__", "") == "show_me_on_screen" for f in agent.TOOLS)
      and any(getattr(f, "__name__", "") == "stop_showing" for f in agent.TOOLS))
check("the brief sends 'where is X' to the screen, not to a slide deck",
      "show_me_on_screen" in context_mod.brief())
check("deck: closing one is instant, so it gets no filler",
      not live_mod.should_fill(["close_presentation"]))
check("the standing brief still tells Neo to SHOW rather than only say — it is "
      "just a ring on the real screen now, not a slide",
      "ring" in context_mod.brief().lower()
      and "show" in context_mod.brief().lower())


# ---- a closed turn that never gets an answer must not be silent ----
# The log that started this: activity_end went out at :55, the same transcript
# came back three times, and Neo said nothing at all until the idle timer closed
# the socket thirty seconds later. Indistinguishable from a crash.
check("live: a turn still waiting inside the window is not overdue",
      not live_mod.answer_overdue(100.0, 105.0, 12.0))
check("live: a turn with no answer past the window is overdue",
      live_mod.answer_overdue(100.0, 113.0, 12.0))
check("live: not waiting on anything is never overdue",
      not live_mod.answer_overdue(0.0, 99999.0, 12.0))
check("live: the answer window closes well before the idle hang-up",
      live_mod.ANSWER_TIMEOUT_S < live_mod.IDLE_TIMEOUT_S)



# ---- the deck's own plumbing: server, event stream, live art swap ----
# Pure functions can't catch these. The bugs that actually broke this feature
# were an SSE response with no delimiter (the browser buffers it and the deck
# never moves) and a mid-deck reload landing on the title card. Both are
# reachable over a socket with no browser and no model.
def _deck_server_streams_and_swaps():
    import urllib.request
    _d = {"title": "T", "slides": [
        {"narration": f"Slide {i} words.",
         "visual": {"kind": "line", "text": f"step {i}"}}
        for i in range(3)]}
    p = deck_mod.Presentation(_d, log=lambda *a: None)
    port = p.serve()
    try:
        base = f"http://127.0.0.1:{port}"
        page = urllib.request.urlopen(base + "/", timeout=4).read().decode()
        if "EventSource" not in page or "const START = -1" not in page:
            return False

        stream = urllib.request.urlopen(base + "/events", timeout=6)
        # No Content-Length AND no chunking means the only valid delimiter is
        # connection close. Anything else and the browser may never see a byte.
        if stream.headers.get("Content-Length") is not None:
            return False

        p.push({"t": "slide", "i": 1})
        line = b""
        for _ in range(40):
            line = stream.readline()
            if line.startswith(b"data:"):
                break
        if json.loads(line[5:].decode()) != {"t": "slide", "i": 1}:
            return False
        if p.shown != 1:
            return False

        # The art call lands mid-deck: the page must reload onto slide 1, not
        # flash back to the title card in the middle of a sentence.
        swapped = json.loads(json.dumps(_d))
        swapped["slides"][1]["visual"] = {"kind": "number", "value": "NEW",
                                          "label": "l", "sub": ""}
        p.replace(swapped)
        stream.close()
        page2 = urllib.request.urlopen(base + "/", timeout=4).read().decode()
        return "const START = 1" in page2 and "NEW" in page2
    finally:
        p.close()

check("deck: the event stream is framed so a browser can actually read it",
      _deck_server_streams_and_swaps())

def _deck_survives_a_closed_window():
    """He closes the deck mid-answer. Neo must keep talking."""
    _d = {"title": "T", "slides": [
        {"narration": "Words.", "visual": {"kind": "line", "text": "p"}}]}
    p = deck_mod.Presentation(_d, log=lambda *a: None)
    p.close()
    p.feed("more words that nobody is watching")   # must not raise
    deck_mod._set(p)
    deck_mod.feed("and neither must this")
    deck_mod.close()
    return deck_mod.current() is None
check("deck: closing the window can't take the answer down with it",
      _deck_survives_a_closed_window())

check("deck: with nothing on screen, feeding transcript is a no-op",
      deck_mod.feed("anything at all") is None)


# ---- hot reload must never fire mid-conversation ----
# Twice now, editing a file while the user was holding fn killed the process
# between "(holding)" and the model's first word. Indistinguishable from the
# freeze this whole rebuild was about. The new code can wait for a gap.
def _reload_waits_while_busy():
    import threading as _th
    fired = []
    calls = {"n": 0}
    def fingerprint_moves():
        calls["n"] += 1
        return float(calls["n"])          # always "changed"
    saved = neo.source_fingerprint
    neo.source_fingerprint = fingerprint_moves
    try:
        busy = {"v": True}
        t = _th.Thread(target=neo._reload_watcher, daemon=True,
                       kwargs={"interval": 0.01, "settle": 0,
                               "restartable": lambda: True,
                               "on_change": lambda: fired.append(1),
                               "busy": lambda: busy["v"]})
        t.start()
        _time.sleep(0.15)
        held = not fired                  # must NOT have restarted while busy
        busy["v"] = False                 # conversation over
        t.join(timeout=1.0)
        return held and bool(fired)       # and it DOES restart once free
    finally:
        neo.source_fingerprint = saved
check("reload: a source change never restarts Neo mid-conversation",
      _reload_waits_while_busy())


# ---- latency: a tool call is three to twenty seconds of dead air ----
# Measured on the real key: "how many grams is a cup of grated carrots" cost 29
# seconds — search_web twice, calculate twice — for a fact the model knew cold.
# The docstrings and the brief are what the model reads to decide, so they are
# the fix, and they are testable.
check("speed: search_web's docstring tells the model when NOT to search",
      "do not search" in agent.search_web.__doc__.lower()
      and "conversion" in agent.search_web.__doc__.lower())
check("speed: and never to search the same thing twice",
      "twice" in agent.search_web.__doc__.lower())
check("speed: a grounded lookup can never sit there forever",
      0 < agent.GROUNDED_TIMEOUT_S <= 10)
# Collapse the wrapping first. This check was passing on "say it was thin" —
# a phrase in a DIFFERENT rule that happened to contain the substring — while
# the sentence it actually means, "If you already know the answer, SAY IT",
# is line-wrapped and was never being seen. Deleting the unrelated rule broke
# a check about something else entirely, which is how you find out a test was
# matching the wrong text.
_brief_flat = " ".join(context_mod.brief().lower().split())
check("speed: the brief says answering from memory is correct, not lazy",
      "say it" in _brief_flat and "silence" in _brief_flat)
check("speed: calculate is scoped to real arithmetic, not recalling a quantity",
      "not for recalling" in context_mod.brief().lower())


# ---- fillers must not sound like a recording ----
# One fixed string per tool meant the third "One sec, looking that up." landed
# as a machine reading a script.
def _fillers_vary():
    seen = {live_mod.filler_for(["search_web"]) for _ in range(6)}
    return len(seen) >= 3
check("filler: the same tool doesn't say the same words every time",
      _fillers_vary())
# A filler covers the gap before the real answer, so one that runs long
# collides with it. The exception is `present`, where the wait is genuinely
# several seconds and a bare "One sec." reads as a crash — that line is allowed
# to say what is happening. Anything else stays short.
check("filler: every line is short enough not to collide with the answer",
      all(len(w) <= (130 if name == "present" else 40)
          for name, opts in live_mod._FILLERS.items()
          for w in (opts if isinstance(opts, list) else [opts])))
check("filler: no tools still means no filler",
      live_mod.filler_for([]) == "")

# ---- "hello" must never cost a web search ----
# Measured: the user said "Hello." and Neo ran search_web AND calculate, then died
# waiting. The live instruction is what the model reads before deciding.
def _live_instruction_gates_tools():
    import neo as _n
    src = open(_n.__file__).read()
    return ("BEFORE ANY TOOL" in src and "hello" in src.lower()
            and "NO tool call" in src)
check("speed: the live brief forbids tools for greetings and small talk",
      _live_instruction_gates_tools())


# ---- the OTHER stale-answer bug: a deck narrated into the wrong conversation
# A presentation takes the better part of a minute to build, and neo.py hands
# the finished script to `self.live` — which is REBOUND every time a new socket
# opens. The guard only asked "is some session running", so a deck whose own
# conversation had died in the meantime was narrated into whatever conversation
# happened to be open when it finished. From the user's seat that is Neo suddenly
# answering something from several minutes ago. Runs the real method.
def _deck_narration_guard():
    import neo as _n
    import time as _t

    class _Sess:
        def __init__(self):
            self.said = []
        def is_running(self):
            return True
        def speak(self, script, log=None):
            self.said.append(script)
            return True

    def _try(live, wanted, asked_at):
        stub = type("S", (), {})()
        stub.live, stub._deck_session, stub._deck_asked_at = live, wanted, asked_at
        _n.Neo._narrate_deck(stub, "SCRIPT")
        return live.said if live is not None else []

    now = _t.time()
    same = _Sess()
    ok_same = _try(same, same, now) == ["SCRIPT"]

    old, new_sess = _Sess(), _Sess()
    _try(new_sess, old, now)                      # deck belonged to `old`
    ok_moved = new_sess.said == []

    stale = _Sess()
    _try(stale, stale, now - _n.DECK_STALE_SEC - 5)
    ok_stale = stale.said == []
    return ok_same, ok_moved, ok_stale

try:
    _same, _moved, _stale = _deck_narration_guard()
    check("deck: a finished deck narrates into the session that asked for it",
          _same)
    check("deck: a deck is NOT narrated into a different conversation", _moved)
    check("deck: a deck that took too long is dropped, not spoken late", _stale)
except (Exception, SystemExit) as _e:
    print(f"SKIP - deck narration guard (needs macOS deps): {_e}")


# ---- the stale-question bug: Neo answering something from hours ago ----
# convo.json really did end with FOUR unanswered user turns. Every new live
# session seeded them, and the model answered the oldest one it could see —
# which is why "Hello." ran search_web and calculate (it was answering a carrot
# question from hours earlier) and why the first word took 29 seconds.
import convo as convo_mod
check("history: a question Neo never answered is not replayed into a new session",
      convo_mod.for_replay([
          {"role": "user", "text": "old q"}, {"role": "model", "text": "old a"},
          {"role": "user", "text": "never answered"}])
      == [{"role": "user", "text": "old q"}, {"role": "model", "text": "old a"}])
check("history: several unanswered questions in a row all go",
      convo_mod.for_replay([
          {"role": "user", "text": "q"}, {"role": "model", "text": "a"},
          {"role": "user", "text": "x"}, {"role": "user", "text": "y"},
          {"role": "user", "text": "z"}])[-1]["role"] == "model")
check("history: a complete conversation is passed through untouched",
      len(convo_mod.for_replay([
          {"role": "user", "text": "q"}, {"role": "model", "text": "a"}])) == 2)
check("history: replay still has to START with a user turn (Gemini requires it)",
      convo_mod.for_replay([
          {"role": "model", "text": "leading"}, {"role": "user", "text": "q"},
          {"role": "model", "text": "a"}])[0]["role"] == "user")
check("history: nothing answerable leaves nothing to replay",
      convo_mod.for_replay([{"role": "user", "text": "only q"}]) == [])
check("history: an empty conversation is fine",
      convo_mod.for_replay([]) == [])

def _live_uses_for_replay():
    import neo as _n
    return "for_replay" in open(_n.__file__).read()
check("history: the live session actually uses it",
      _live_uses_for_replay())


# ---- present() called twelve times in thirty seconds ----
# Real log, 23:44: twelve `present` calls in half a minute. Each one tore the
# window down and rebuilt, so the screen strobed and every art thread found it
# was no longer current and binned its figures. the user watched plain text flash
# in and out and never saw a single diagram.
def _present_is_single_flight():
    import agent as _a, threading as _th, time as _t
    calls = []
    class _FakeDeck:
        def present(self, topic, client, log=None, **kw):
            calls.append(topic)
            _t.sleep(0.4)                     # a build takes real time
            return {"title": "T", "slides": 3, "script": "one\n\ntwo"}
        def close(self, *a): pass
    import sys as _s
    saved_deck = _s.modules.get("deck")
    saved_client = _a._client
    _s.modules["deck"] = _FakeDeck()
    _a._client = object()
    _a._present_lock = None
    try:
        out = []
        # daemon=True because join() below gives up after 8s and a straggler
        # otherwise outlives the finally block, calling into the REAL deck
        # module once it is restored — that is where the stray "[deck] ready
        # after 155s" lines at the end of this suite come from. (It is NOT the
        # cause of the intermittent SIGABRT here; that survives this change.)
        threads = [_th.Thread(target=lambda: out.append(_a.present("same topic")),
                              daemon=True)
                   for _ in range(6)]
        for t in threads: t.start()
        for t in threads: t.join(timeout=8)
        return len(calls) == 1 and len(out) == 6
    finally:
        # THE FAKE STAYS UNTIL EVERY THREAD IS ACTUALLY DONE.
        #
        # join(timeout=8) gives up; the old code then restored the REAL deck
        # while stragglers were still inside present(). Those threads went on
        # to build an actual presentation — which spawns Cocoa windows from a
        # non-main thread and drives PortAudio — minutes after this test
        # finished. That is where the stray "[deck] ready after 155s" lines
        # come from, and it is why this suite intermittently died with SIGSEGV
        # at the very end, popping a "Python quit unexpectedly" dialog on
        # the user's screen every time.
        #
        # A straggler must never reach the real module. If one is still alive,
        # the fake stays installed for the rest of the run: this is the last
        # test that touches deck, so nothing else needs the real one.
        for t in threads:
            t.join(timeout=5)
        if any(t.is_alive() for t in threads):
            print("      (a present() thread is still running; keeping the "
                  "stub so it cannot reach the real deck)")
        else:
            if saved_deck is not None: _s.modules["deck"] = saved_deck
            else: _s.modules.pop("deck", None)
        _a._client = saved_client
        _a._present_lock = None
check("deck: six simultaneous present() calls build exactly one presentation",
      _present_is_single_flight())

check("deck: the filler warns him it takes a moment, so he doesn't ask again",
      any("sec" in w or "moment" in w
          for w in live_mod._FILLERS["present"]))
check("deck: escape actually reaches the server that owns the window",
      "/quit" in deck_mod.render_html({"title": "T", "slides": []}))
check("deck: figures are waited for before anything is shown",
      deck_mod.ART_WAIT_S >= 5)


# ---- present() must not block the live tool dispatch ----
# The whole failure, from the 23:52 log: present() took ~11s inline (a plan
# call, then a 9s wait for artwork). _handle_tools awaits the tool before
# answering the model, so Gemini saw no function response, assumed the call was
# lost, and fired present FOURTEEN times in twelve seconds. Each rebuild binned
# the previous deck's figures, so the user got plain text and a strobing window.
# get_time never did this because it returns instantly.
def _present_returns_instantly_and_narrates_when_ready():
    """present() must not block, and the narration must arrive AFTER the deck.

    A tool that blocks inside a live dispatch kills the websocket — nineteen
    seconds of no traffic and the server sent 1006 abnormal closure, so the
    deck appeared and Neo never spoke a word. And the user's own requirement: the
    presentation shows up when everything is DONE, with Neo actually ready.
    """
    import providers as _pv
    saved = _pv.resolve
    saved_open = deck_mod.Presentation.open_window
    saved_imgs = deck_mod.collect_images
    _pv.resolve = lambda kind, client, log=None: ("gemini", "fake-model")
    opened = []
    deck_mod.Presentation.open_window = lambda self: opened.append(1)
    deck_mod.collect_images = lambda plan, log=print, fetch=None: {}
    import json as _js, time as _tm
    plan = _js.dumps({"title": "T", "slides": [
        {"narration": "First step words here."},
        {"narration": "Second step words here."}]})
    art = _js.dumps({"visuals": [{"kind": "number", "value": "9",
                                  "label": "L", "sub": ""}]})

    class _R:
        def __init__(s, t): s.text = t

    class _M:
        def generate_content(s, contents="", **kw):
            _tm.sleep(0.7)                  # BOTH calls are slow
            return _R(art if "kind" in contents else plan)

    script = []
    try:
        t0 = _tm.time()
        out = deck_mod.present("topic", type("C", (), {"models": _M()})(),
                               log=lambda m: None, on_ready=script.append)
        inline = _tm.time() - t0
        if out is None or inline > 0.4:
            return False               # blocked: this is what killed the socket
        if opened or script:
            return False               # showed something before it was ready
        _tm.sleep(5.0)
        p = deck_mod.current()
        if p is None or not opened or not script:
            return False
        kinds = [(s.get("visual") or {}).get("kind") for s in p.deck["slides"]]
        # Fully dressed BEFORE it was shown, sitting on the title card, and the
        # script handed over only once all of that was true.
        return all(k != "line" for k in kinds) and p.shown == -1 and bool(script[0])
    finally:
        _pv.resolve = saved
        deck_mod.Presentation.open_window = saved_open
        deck_mod.collect_images = saved_imgs
        deck_mod.close()


check("deck: present() returns at once and narrates only when the deck is up",
      _present_returns_instantly_and_narrates_when_ready())

check("deck: words spoken while the deck builds are not thrown away",
      hasattr(deck_mod, "_prebuffer"))


# ---- the wedge: PortAudio on the asyncio event loop ----
# The log that found this ends at "(let go — thinking)" and never writes another
# line. The statement right after that log call was _close_mic_now(), which does
# real device I/O — stop() and close() each block, and block outright while the
# open worker still holds the device. Running that inside a coroutine froze the
# WHOLE event loop: receive task, tool dispatch and the no-answer watchdog all
# stopped with it. Silent, and identical to "the model is being slow".
def _mic_close_never_blocks_the_caller():
    import threading as _th, time as _t
    s = live_mod.LiveSession.__new__(live_mod.LiveSession)
    s.log = lambda m: None
    class _Hangs:
        def stop(self): _t.sleep(4)
        def close(self): _t.sleep(4)
    s._mic_stream = _Hangs()
    t0 = _t.time()
    s._close_mic_soon()
    return (_t.time() - t0) < 0.5
check("live: closing the mic never blocks the caller (it did, on the event loop)",
      _mic_close_never_blocks_the_caller())

def _a_stalled_event_loop_announces_itself():
    import threading as _th, time as _t
    s = live_mod.LiveSession.__new__(live_mod.LiveSession)
    s._stop = _th.Event()
    s._beat = _t.time() - 30          # loop hasn't ticked in 30s
    s._awaiting_since = 0.0
    said = []
    s.log = said.append
    s.on_state = lambda x: None
    s.on_end = lambda x: None
    t = _th.Thread(target=s._pulse_watch, kwargs={"stall": 2.0}, daemon=True)
    t.start(); t.join(timeout=6)
    return bool(said) and s._stop.is_set()

def _a_clean_hangup_is_not_reported_as_a_stall():
    """The other half, and the one the log was full of. `_idle_loop` returning
    on an ordinary hang-up leaves the heartbeat frozen by design, and the
    watcher ran on regardless — so eight seconds after every quiet close it
    announced "the session thread stalled for 9s", directly under "closed".
    A warning that fires on the normal path is not a warning."""
    import threading as _th, time as _t
    s = live_mod.LiveSession.__new__(live_mod.LiveSession)
    s._stop = _th.Event()
    s._beat = _t.time() - 30
    s._awaiting_since = 0.0
    s._ended = True                     # on_end already fired: this is over
    said = []
    s.log = said.append
    s.on_state = lambda x: None
    s.on_end = lambda x: None
    t = _th.Thread(target=s._pulse_watch, kwargs={"stall": 2.0}, daemon=True)
    t.start(); t.join(timeout=6)
    return said == [] and not t.is_alive()


check("live: a clean hang-up is never reported as a stall",
      _a_clean_hangup_is_not_reported_as_a_stall())
check("live: the idle hang-up stops the watcher instead of leaving it running",
      "_stop.set()" in _insp.getsource(live_mod.LiveSession._idle_loop))
check("live: a frozen session thread says so instead of going quiet",
      _a_stalled_event_loop_announces_itself())


# ---- why Neo answered a question from five hours earlier ----
# Traced end to end in the logs. At 18:06 Neo answered "What's the time right
# now?" OUT LOUD — and the reply was never written to disk, because on_end only
# fired from stop() and two watchdogs, never from a normal idle hang-up. The
# question survived; the answer didn't. At 23:43 the user said "Hello." and Neo
# replied "You asked about the time, and it's 11:43pm".
check("history: an orphan in the MIDDLE is dropped, not just a trailing one",
      convo_mod.for_replay([
          {"role": "model", "text": "lead"},
          {"role": "user", "text": "orphan from hours ago"},
          {"role": "user", "text": "q"}, {"role": "model", "text": "a"}])
      == [{"role": "user", "text": "q"}, {"role": "model", "text": "a"}])
check("history: what survives replay is strictly alternating",
      all(t["role"] == ("user" if i % 2 == 0 else "model")
          for i, t in enumerate(convo_mod.for_replay([
              {"role": "user", "text": "x"},
              {"role": "user", "text": "q1"}, {"role": "model", "text": "a1"},
              {"role": "model", "text": "stray"},
              {"role": "user", "text": "q2"}, {"role": "model", "text": "a2"}]))))
check("history: a stray model turn with no question is dropped too",
      convo_mod.for_replay([{"role": "model", "text": "stray"}]) == [])

def _end_fires_once_from_every_exit():
    """on_end must fire on a NORMAL close, and never twice."""
    import threading as _th
    s = live_mod.LiveSession.__new__(live_mod.LiveSession)
    s._ended = False
    s._end_lock = _th.Lock()
    seen = []
    s.on_end = seen.append
    s._fire_end("closed")
    s._fire_end("stalled")      # a second exit path must not double-commit
    return seen == ["closed"]
check("live: the session reports its end exactly once, however it ends",
      _end_fires_once_from_every_exit())

check("live: a normal hang-up flushes the reply (on_end reachable from _run)",
      "_fire_end(\"closed\")" in open(live_mod.__file__).read())

check("history: the fallback chat is seeded with complete pairs too",
      "for_replay(self._turns)" in open(neo.__file__).read())


# ---- the flashing orange microphone ----
# The mic queue holds 64 chunks = 6.4s of audio, so a turn's _END_OF_TURN
# marker is drained long AFTER the key came up. Press again inside that window
# and the old turn's cleanup closed the NEW hold's stream: the orange dot
# blinks out mid-sentence and that hold records nothing. Its signature is in
# the log three times as back-to-back "(let go, but nothing was said)".
def _cleanup_never_closes_a_newer_holds_mic():
    s = live_mod.LiveSession.__new__(live_mod.LiveSession)
    s.log = lambda m: None
    s._hold_id = 7
    s._mic_hold_id = 7                 # hold 7 owns the open stream
    closed = []
    class _S:
        def stop(self): closed.append("stop")
        def close(self): closed.append("close")
    s._mic_stream = _S()
    s._close_mic_now(hold_id=6)        # a stale worker from hold 6
    return closed == [] and s._mic_stream is not None
check("mic: a stale worker cannot close a newer hold's stream",
      _cleanup_never_closes_a_newer_holds_mic())

def _its_own_hold_still_closes():
    s = live_mod.LiveSession.__new__(live_mod.LiveSession)
    s.log = lambda m: None
    s._hold_id = 7
    s._mic_hold_id = 7
    closed = []
    class _S:
        def stop(self): closed.append("stop")
        def close(self): closed.append("close")
    s._mic_stream = _S()
    s._close_mic_now(hold_id=7)
    return closed == ["stop", "close"] and s._mic_stream is None
check("mic: the hold that owns the stream still releases it",
      _its_own_hold_still_closes())

check("mic: an unscoped close still works (teardown, end_turn)",
      True)

# ---- present() failures must say WHY ----
# 3789 lines of neo.log contained not one [deck] entry, because build()'s
# exception was swallowed and only its class name reached the model.
check("deck: a failed plan call logs the actual error, not just its type",
      "plan call failed on" in open(deck_mod.__file__).read())

def _unparseable_plan_says_what_came_back():
    """Run it, don't grep for a phrase. The old version searched deck.py for
    the literal string "returned nothing usable", which says nothing about
    whether the log line is ever reached and broke the moment the message was
    built from a variable."""
    import providers as _pv
    saved, msgs = _pv.resolve, []

    class _M:
        def generate_content(self, model=None, contents="", **kw):
            return type("R", (), {"text": "I'd rather not, sorry."})()

    _pv.resolve = lambda kind, client, log=None: ("gemini", "m")
    try:
        got = deck_mod.build("a topic", type("C", (), {"models": _M()})(),
                             log=msgs.append)
    finally:
        _pv.resolve = saved
    joined = " ".join(msgs)
    return got is None and "I'd rather not" in joined


check("deck: an unparseable answer is logged with what came back",
      _unparseable_plan_says_what_came_back())


def _a_refused_plan_tries_the_other_model():
    """A refusal from the first model must not end the presentation. the user
    asked three times for a walkthrough of his dad's condition and got "I'm
    having trouble loading that visual" every time — one model declining, and
    no second attempt."""
    import providers as _pv
    saved, seen = _pv.resolve, []

    class _M:
        def generate_content(self, model=None, contents="", **kw):
            seen.append(model)
            if model == "first":
                return type("R", (), {"text": "I can't help with that."})()
            return type("R", (), {"text": _json.dumps(
                {"title": "T", "slides": [{"narration": "Step one words."}]})})()

    _pv.resolve = lambda kind, client, log=None: (
        "gemini", "first" if kind == "chat" else "second")
    try:
        got = deck_mod.build("a topic", type("C", (), {"models": _M()})(),
                             log=lambda m: None)
    finally:
        _pv.resolve = saved
    return bool(got) and seen == ["first", "second"]


check("deck: a model that refuses the plan is not the end of it",
      _a_refused_plan_tries_the_other_model())

# ---- the instant ack ----
# The live filler used to be sent THROUGH the model, so the first sound needed
# a full round trip. These are pre-synthesized at boot and play locally.
check("ack: there are spoken lines for building a presentation",
      len(banter.ACKS.get("present", [])) >= 3)
# The two buckets the user actually hears most — a presentation and a lookup —
# must each have a cloud-voice line ready. Requiring ALL of them is what put
# 53 requests through a free tier and got 50 of them refused.
check("ack: the buckets he hears most have a cloud-voice line ready",
      all(any(p in banter.all_phrases() for p in banter.ACKS[b])
          for b in ("present", "lookup")))
check("ack: the live path plays them locally instead of asking the model",
      "self.on_tools(names)" in open(live_mod.__file__).read())
check("ack: present maps to the presentation lines",
      neo.Neo._ACK_FOR_TOOL.get("present") == "present")


# ---- long prompts: a 90-second hold must not lose the "answer me" marker ----
# The mic queue is 64 chunks and a chunk is 100ms, so anything past ~6.4s of
# speech overflows it. end_turn MUST still get _END_OF_TURN in, or the key-up
# never closes the turn and Neo listens forever.
def _long_hold_keeps_the_end_marker():
    import queue as _q
    s = live_mod.LiveSession.__new__(live_mod.LiveSession)
    s._mic_q = _q.Queue(maxsize=64)
    s._holding, s._speaking = True, True
    s.log = lambda m: None
    s._mic_stream, s._hold_id, s._mic_hold_id = None, 1, 1
    s._last_activity = _time.time()
    for _ in range(900):                 # 90 seconds of audio
        try: s._mic_q.put_nowait(b"\x00" * 3200)
        except _q.Full: pass
    s.end_turn()
    found = False
    while not s._mic_q.empty():
        if s._mic_q.get_nowait() is live_mod._END_OF_TURN:
            found = True
    return found
check("long: a 90-second hold still delivers the end-of-turn marker",
      _long_hold_keeps_the_end_marker())

check("long: a long press is never force-released while the tap is alive",
      all(neo.reconcile_fn_state(True, True, s_, 180, 0, 3, False) is None
          and neo.reconcile_fn_state(True, False, s_, 180, 3, 3, False) != "release"
          for s_ in (5, 10, 20, 30, 60, 120, 170)))
check("long: past the ceiling it still gives up cleanly",
      neo.reconcile_fn_state(True, True, 190, 180, 0, 3, False) == "maxhold")
check("long: a key-up genuinely lost to a dead tap is still recovered",
      neo.reconcile_fn_state(True, False, 10, 180, 3, 3, True) == "release")

def _a_very_long_question_survives_every_pure_path():
    q = ("So basically what happened was " + "and then this happened, " * 400).strip()
    if len(convo_mod.for_replay([{"role": "user", "text": q},
                                 {"role": "model", "text": "Got it."}])) != 2:
        return False
    if len(deck_mod.cue_words(q)) > deck_mod.CUE_WORDS:
        return False
    t = deck_mod.Tracker([{"narration": q, "visual": None},
                          {"narration": "Second step, different words.", "visual": None}], min_dwell=0)
    return isinstance(t.feed(q), list)
check("long: a 1600-word question survives history, cues and the tracker",
      _a_very_long_question_survives_every_pure_path())

# ---- the acks must not sound like a recording ----
# Variety lives in ACKS, which pick() draws from. all_phrases() is a much
# smaller list — only what gets spent on cloud TTS quota — so counting IT for
# variety was measuring the wrong thing the moment those two diverged.
check("ack: there are plenty of phrases to choose from",
      sum(len(v) for v in banter.ACKS.values()) >= 45)
check("ack: every bucket has real variety",
      all(len(banter.ACKS[b]) >= 6 for b in
          ("quick", "medium", "present", "lookup", "hands", "long")))
def _never_says_the_same_thing_twice_running():
    for bucket in ("present", "lookup", "quick", "medium", "hands"):
        picks = [banter.pick(bucket) for _ in range(40)]
        if any(a == b for a, b in zip(picks, picks[1:])):
            return False
    return True
check("ack: the same line never comes out twice in a row",
      _never_says_the_same_thing_twice_running())
check("ack: present and hands route to their own lines, not the generic ones",
      banter.pick("present") in banter.ACKS["present"]
      and banter.pick("hands") in banter.ACKS["hands"])


# Boot no longer waits on synthesis AT ALL — the lines live on disk and are
# made in the background, so the core/rest split has nothing left to do. What
# still matters is that boot cannot be blocked by it.
check("ack: boot never waits on synthesising a voice line",
      "daemon=True" in _insp.getsource(neo.Speech._recache))
check("ack: every bucket has a line that can be made in the live voice",
      all(any(p in banter.all_phrases() for p in banter.ACKS[b])
          for b in banter.ACKS))
check("ack: a line already on disk is never paid for twice",
      "_ack_from_disk" in _insp.getsource(neo.Speech._synth_into))
check("ack: a refused line backs off instead of hammering the quota",
      "backoff" in _insp.getsource(neo.Speech._synth_into))


# ---- two voices at once ----
# agent.py did `import neo` to reach its logger. neo.py is run as a SCRIPT, so
# its module name is __main__ — `import neo` loaded the whole file a second
# time as a separate module, re-running every module-level statement and
# building a second, half-initialised Neo (with the DEFAULT voice, not the
# configured one) inside the first. That is the "old Neo's voice" talking over
# the new one.
def _no_module_imports_neo_back():
    """A real import statement, not a mention in a comment or docstring."""
    import ast as _ast
    for f in ("agent.py", "deck.py", "live.py", "convo.py", "banter.py",
              "context.py"):
        try:
            tree = _ast.parse(open(f).read())
        except Exception:
            continue
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                if any(a.name == "neo" for a in node.names):
                    return False
            if isinstance(node, _ast.ImportFrom) and node.module == "neo":
                return False
    return True
check("voice: no module that neo.py imports may import neo back",
      _no_module_imports_neo_back())
# Asserted through the SIGNATURE rather than a literal line of source. The
# string form broke the moment bind() grew a parameter (set_voice_mode, in
# September) even though the rule it protects — agent never imports neo, it
# gets what it needs handed to it — was never in question.
check("voice: agent gets its logger injected instead",
      "logger" in _insp.signature(agent.bind).parameters)
check("voice: ...and anything else it needs is injected the same way",
      all(p.default is not _insp.Parameter.empty
          for p in _insp.signature(agent.bind).parameters.values()))
_ack_src = _insp.getsource(neo.Neo._live_ack)
check("voice: the instant ack refuses to play over the live model",
      "_playback" in _ack_src and "playing" in _ack_src)

# ---- the filler was a SECOND VOICE, and there were two of them ----
# The lines were synthesised with Kokoro while the answer came out of the live
# socket in Gemini's voice, so one exchange had two different people in it.
# And _live_ack fires per tool-call BATCH, not per question, so one question
# produced "Let me check." and then "One sec.".
check("voice: the filler is synthesised in the live session's own voice",
      neo.GEMINI_TTS_VOICE == live_mod.VOICE)
# This check used to assert the OPPOSITE, and the assertion was the bug. The
# cache was being built before the Gemini client existed, so every line failed
# — and "stay silent rather than use the wrong voice" turned that into Neo
# making no sound at all from the moment a tool started. He asked a question
# and got nothing back until the watchdog reset the session.
# A filler in the local voice is cosmetic. Silence while working is
# indistinguishable from broken, which is what the filler exists to prevent.
check("voice: a missing filler falls back to a voice, never to silence",
      "self._speak(text)" in _insp.getsource(neo.Neo._ack))
check("voice: the filler cache is built after the client exists, not before",
      "_recache" in _insp.getsource(neo.Neo._boot)
      if hasattr(neo.Neo, "_boot") else True)
check("voice: filler audio is keyed by voice, so two voices can't be confused",
      "(voice, text)" in _insp.getsource(neo.Speech.ack_audio))
check("voice: filler lines survive a restart instead of being re-synthesised",
      "_ack_from_disk" in _insp.getsource(neo.Speech._recache))
check("voice: boot no longer waits on synthesising them",
      "daemon=True" in _insp.getsource(neo.Speech._recache))


def _only_one_filler_per_question():
    """Two tool batches in one turn must produce ONE spoken line, and a second
    question gets its own."""
    import types as _pyt
    n = neo.Neo.__new__(neo.Neo)
    said = []
    n._ack = lambda kind: said.append(kind)
    n.live = None
    n.state = _pyt.SimpleNamespace(get=lambda: "idle", set=lambda s: None)
    n.ACK_DELAY_S = 0.02
    n._ack_turn, n._acked_turn = 1, None
    n._live_ack(["search_web"])
    n._live_ack(["read_webpage"])      # same question, second batch
    _time.sleep(0.25)
    one = said == ["lookup"]
    n._ack_turn = 2                    # he asks something else
    n._live_ack(["search_web"])
    _time.sleep(0.25)
    return one and said == ["lookup", "lookup"]


check("filler: one question gets one spoken line, however many tools it uses",
      _only_one_filler_per_question())


def _a_fast_tool_is_never_covered():
    """The line only lands if the work is still going. A tool that answers
    before the delay is up must produce silence, not 'Checking.' talking over
    the answer."""
    import types as _pyt
    n = neo.Neo.__new__(neo.Neo)
    said = []
    n._ack = lambda kind: said.append(kind)
    n.live = None
    speaking = {"v": "idle"}
    n.state = _pyt.SimpleNamespace(get=lambda: speaking["v"], set=lambda s: None)
    n.ACK_DELAY_S = 0.15
    n._ack_turn, n._acked_turn = 1, None
    n._live_ack(["search_web"])
    speaking["v"] = "speaking"         # the model started answering first
    _time.sleep(0.4)
    return said == []


check("filler: a tool that answers quickly is not covered at all",
      _a_fast_tool_is_never_covered())
check("filler: the delay is long enough to be worth it, short enough to help",
      0.8 <= neo.Neo.ACK_DELAY_S <= 3.0)


# ---- the figures have to actually teach ----
# The renderer was fine; the PROMPT capped every label at four words, so the
# model produced "Amino acid" where a textbook figure says "20 kinds, joined by
# peptide bonds". A blob with three arrows teaches nothing.
check("deck: the visuals prompt no longer caps labels at four words",
      "A label is 1-4 words" not in deck_mod.visuals_prompt(
          {"title": "t", "slides": []}))
check("deck: it demands teaching density instead",
      "TEACH." in deck_mod.visuals_prompt({"title": "t", "slides": []}))
check("deck: and pushes for a real photograph or plate",
      "Wikimedia Commons" in deck_mod.visuals_prompt({"title": "t", "slides": []}))

# ---- the comparison grid: the densest shape, and the one that was missing ----
_G = {"kind": "grid",
      "cols": [{"title": "Carbohydrates", "tone": "amber"},
               {"title": "Proteins", "tone": "blue"}],
      "rows": [{"label": "Monomer",
                "cells": ["Monosaccharide (glucose)", "Amino acid, 20 kinds"]},
               {"label": "Bond", "cells": ["Glycosidic linkage", "Peptide bond"]}]}
check("deck: a comparison grid survives sanitising",
      deck_mod._clean_visual(_G)["rows"][0]["cells"][0]
      == "Monosaccharide (glucose)")
check("deck: a grid cell keeps a real clause, not four words",
      len(deck_mod._clean_visual(_G)["rows"][0]["cells"][1]) > 15)
check("deck: a one-column grid is refused (nothing to compare)",
      deck_mod._clean_visual({"kind": "grid",
                              "cols": [{"title": "Only"}],
                              "rows": [{"label": "a", "cells": ["x"]},
                                       {"label": "b", "cells": ["y"]}]}) is None)
check("deck: a short row is padded rather than misaligned",
      deck_mod._clean_visual({"kind": "grid",
          "cols": [{"title": "A"}, {"title": "B"}, {"title": "C"}],
          "rows": [{"label": "r1", "cells": ["only one"]},
                   {"label": "r2", "cells": ["a", "b", "c"]}]}
      )["rows"][0]["cells"] == ["only one", "", ""])
check("deck: a grid renders into the page",
      "Monosaccharide" in deck_mod.render_html(
          {"title": "T", "slides": [{"narration": "n", "visual": _G}]}))
check("deck: hostile text in a grid cell is escaped",
      "<script>x" not in deck_mod.render_html({"title": "T", "slides": [
          {"narration": "n", "visual": {"kind": "grid",
           "cols": [{"title": "A"}, {"title": "B"}],
           "rows": [{"label": "</div><script>x", "cells": ["<script>x", "b"]},
                    {"label": "r", "cells": ["c", "d"]}]}}]}))
check("deck: grid is in the known kinds",
      "grid" in deck_mod.VISUAL_KINDS)


# ---- structures: the shapes boxes and tables cannot draw ----
# the user asked for atoms and Lewis dot structures. No amount of prompt tuning
# gets those out of a box-and-arrow vocabulary — they needed real renderers.
_WATER = {"kind": "molecule", "caption": "Water",
          "atoms": [{"el": "O", "x": 50, "y": 38, "lone": 2},
                    {"el": "H", "x": 31, "y": 62}, {"el": "H", "x": 69, "y": 62}],
          "bonds": [{"from": 0, "to": 1}, {"from": 0, "to": 2, "order": 1}]}
check("chem: a molecule survives sanitising with its lone pairs",
      deck_mod._clean_visual(_WATER)["atoms"][0]["lone"] == 2)
check("chem: a bond pointing at an atom that doesn't exist is dropped",
      len(deck_mod._clean_visual({"kind": "molecule",
          "atoms": [{"el": "H", "x": 30, "y": 50}, {"el": "H", "x": 70, "y": 50}],
          "bonds": [{"from": 0, "to": 99}, {"from": 0, "to": 1}]})["bonds"]) == 1)
check("chem: a one-atom 'molecule' is refused",
      deck_mod._clean_visual({"kind": "molecule",
                              "atoms": [{"el": "H", "x": 50, "y": 50}]}) is None)
check("chem: a nonsense bond order falls back to single",
      deck_mod._clean_visual({"kind": "molecule",
          "atoms": [{"el": "C", "x": 30, "y": 50}, {"el": "O", "x": 70, "y": 50}],
          "bonds": [{"from": 0, "to": 1, "order": 47}]})["bonds"][0]["order"] == 1)
check("chem: double and triple bonds are kept",
      [b["order"] for b in deck_mod._clean_visual({"kind": "molecule",
          "atoms": [{"el": "C", "x": 20, "y": 50}, {"el": "O", "x": 50, "y": 50},
                    {"el": "N", "x": 80, "y": 50}],
          "bonds": [{"from": 0, "to": 1, "order": 2},
                    {"from": 1, "to": 2, "order": 3}]})["bonds"]] == [2, 3])
check("chem: a molecule renders, with its bonds and letters",
      all(s in deck_mod.render_html({"title": "T", "slides": [
              {"narration": "n", "visual": _WATER}]})
          for s in ("class=\"bond\"", "class=\"el\"", "class=\"lp\"")))

_ATOM = {"kind": "atom", "symbol": "Na", "name": "Sodium",
         "protons": 11, "neutrons": 12, "shells": [2, 8, 1]}
check("chem: a Bohr atom survives sanitising",
      deck_mod._clean_visual(_ATOM)["shells"] == [2, 8, 1])
check("chem: an atom with no shells is refused",
      deck_mod._clean_visual({"kind": "atom", "symbol": "X", "shells": []}) is None)
check("chem: a junk shell count is dropped, not rendered",
      deck_mod._clean_visual({"kind": "atom", "symbol": "C",
                              "shells": [2, "banana", 4]})["shells"] == [2, 4])
check("chem: the atom draws every electron it claims",
      deck_mod.render_html({"title": "T", "slides": [
          {"narration": "n", "visual": _ATOM}]}).count('class="e"') == 11)
check("chem: element colouring is real, not one grey for everything",
      len({deck_mod.element_color(e) for e in ("C", "O", "N", "H", "S")}) == 5)
check("chem: an unknown element still gets a colour rather than crashing",
      deck_mod.element_color("Xx").startswith("#"))
check("chem: both shapes are documented for the model",
      "STRUCTURAL FORMULA" in deck_mod.visuals_prompt({"title": "t", "slides": []})
      and "Bohr shell diagram" in deck_mod.visuals_prompt({"title": "t", "slides": []}))
check("chem: molecule and atom are known kinds",
      "molecule" in deck_mod.VISUAL_KINDS and "atom" in deck_mod.VISUAL_KINDS)


# ---- animated scenes: things that MOVE on the words that describe them ----
# The whole differentiator. A static picture cannot carry a change, and
# "the disc bulges backwards and compresses the nerve roots" IS a change.
_SCENE = {"kind": "scene", "stage": "body", "caption": "L4/L5",
  "parts": [{"id": "disc", "shape": "disc", "x": 44, "y": 58, "w": 11, "h": 5,
             "tone": "amber", "label": "Disc"},
            {"id": "nerves", "shape": "tube", "x": 62, "y": 58, "w": 20, "h": 14,
             "tone": "blue", "strands": 9, "label": "Cauda equina"}],
  "beats": [{"say": "soft centre pushes backwards",
             "set": {"disc": {"dx": 1.9, "scale": 1.35, "tone": "rose"}}},
            {"say": "presses on the nerve roots",
             "set": {"nerves": {"squash": 0.4, "flash": True}},
             "note": "conduction fails"}]}
check("scene: a scene survives sanitising with its parts and beats",
      len(deck_mod._clean_visual(_SCENE)["parts"]) == 2
      and len(deck_mod._clean_visual(_SCENE)["beats"]) == 2)
check("scene: a beat naming a part that doesn't exist is emptied, not crashed",
      deck_mod._clean_visual({"kind": "scene",
          "parts": [{"id": "a", "shape": "disc", "x": 50, "y": 50, "w": 10, "h": 5}],
          "beats": [{"say": "x", "set": {"ghost": {"dy": 5}}}]}
      )["beats"][0]["set"] == {})
check("scene: an absurd squash is clamped to something still visible",
      deck_mod._clean_visual({"kind": "scene",
          "parts": [{"id": "a", "shape": "tube", "x": 50, "y": 50, "w": 10, "h": 5}],
          "beats": [{"say": "x", "set": {"a": {"squash": 0.0001}}}]}
      )["beats"][0]["set"]["a"]["squash"] >= 0.22)
check("scene: an unknown shape falls back rather than rendering nothing",
      deck_mod._clean_visual({"kind": "scene", "parts": [
          {"id": "a", "shape": "hologram", "x": 50, "y": 50, "w": 10, "h": 5}]}
      )["parts"][0]["shape"] == "disc")
check("scene: a scene with no parts is refused",
      deck_mod._clean_visual({"kind": "scene", "parts": []}) is None)

def _beats_fire_on_the_right_words():
    """The sync, end to end: beats ride the SAME cue path as figure labels, so
    a scene needs no special handling in the Tracker at all."""
    steps = [{"narration": "Stacked between each vertebra sits a disc. The soft "
                           "centre pushes backwards through a tear, and presses "
                           "on the nerve roots behind it.",
              "visual": {"kind": "scene",
                "parts": [{"id": "d", "shape": "disc", "x": 50, "y": 50,
                           "w": 10, "h": 5}],
                "beats": [{"say": "stacked between each vertebra"},
                          {"say": "soft centre pushes backwards"},
                          {"say": "presses on the nerve roots"}]}}]
    t = deck_mod.Tracker(steps, min_dwell=0)
    words = steps[0]["narration"].split()
    fired = []
    for i in range(0, len(words), 3):        # transcript fragments, as they land
        for ev in t.feed(" ".join(words[i:i + 3])):
            if ev[0] == "callout":
                fired.append(ev[2])
    return fired == [0, 1, 2]
check("scene: every beat fires, in order, on the words that describe it",
      _beats_fire_on_the_right_words())

check("scene: beats reach the page as data, and the driver is there to apply them",
      all(s in deck_mod.render_html({"title": "T", "slides": [
              {"narration": "n", "visual": _SCENE}]})
          for s in ("data-beats", "function beat(", "data-part=")))
check("scene: hostile text in a part label can't inject",
      "<script>x" not in deck_mod.render_html({"title": "T", "slides": [
          {"narration": "n", "visual": {"kind": "scene", "parts": [
              {"id": "a", "shape": "disc", "x": 50, "y": 50, "w": 10, "h": 5,
               "label": "</text><script>x"}]}}]}))
_vp = deck_mod.visuals_prompt({"title": "t", "slides": []})
check("scene: it's a known kind and still documented for the model",
      "scene" in deck_mod.VISUAL_KINDS and '"kind":"scene"' in _vp)
# scene builds pictures out of seven fixed primitives, so its ceiling is a
# yellow ellipse beside nine parallel lines labelled "cauda equina" — which is
# exactly what a real deck produced. It is no longer sold as the shape to
# prefer.
check("scene: the prompt no longer sells it as the shape to prefer",
      "most powerful" not in _vp and "LAST RESORT" in _vp.upper())

# THIS CHECK USED TO ASSERT THE BUG. It required "THIS IS THE DEFAULT" to
# appear in the svg section — i.e. it enforced telling the model to hand-draw
# raw SVG for almost every slide. That is the one thing this file's own header
# says was tried and was unusable ("overlapping text, off-canvas paths, no
# consistency between slides"), and it is what a real deck produced: one
# figure, and the user's verdict on it was "total shit".
#
# The composed shapes come out right every time because deck.py draws them and
# only their CONTENT comes from the model. Raw SVG is now the last resort it
# always should have been, and the test asserts that instead.
check("svg: the prompt does NOT make hand-drawn SVG the default",
      "THIS IS THE DEFAULT" not in _vp)
check("svg: it is offered as a last resort, after the composed shapes",
      _vp.upper().count("LAST RESORT") >= 2)
check("svg: the reason is written down, so it is not re-promoted by accident",
      "unusable" in _vp.lower() or "free-handing" in _vp.lower())
check("svg: and says how to make it look drawn rather than clip art",
      all(w in _vp for w in ("linearGradient", "BUILD FROM CURVES",
                             "COMPOSE THE FRAME", "ONE ACCENT")))


# ---- model-authored SVG: the only route to an ACCURATE figure ----
# A fixed vocabulary of six shapes cannot draw a vertebra. An ellipse beside
# some parallel lines is not a spine, and no prompt makes it one. So the model
# draws, and the sanitiser makes that safe: this markup came from something
# that just read a web page.
_ATTACKS = [
    "<svg onload=alert(1)><script>alert(1)</script><path d='M0,0'/></svg>",
    "<g><foreignObject><iframe src=x></iframe></foreignObject>"
    "<circle cx='5' cy='5' r='3'/></g>",
    "<a href='javascript:alert(1)'><text>hi</text></a>",
    "<image href='http://evil.example/x.png'/><rect width='10' height='10'/>",
    "<circle onclick='steal()' cx='1' cy='1' r='1' style='x:url(http://e)'/>",
    "<use xlink:href='http://evil.example/#x'/><path d='M1,1 L2,2'/>",
    "<style>* { background: url(http://evil.example) }</style><path d='M0,0'/>",
]
def _sanitiser_holds():
    banned = ("script", "onload", "onclick", "onerror", "javascript:", "iframe",
              "foreignobject", "href", "style=", "<img", "<image")
    for a in _ATTACKS:
        out = deck_mod.sanitize_svg(a).lower()
        if any(b in out for b in banned):
            return False
    return True
check("svg: every injection attempt is stripped, not escaped",
      _sanitiser_holds())
check("svg: legitimate drawing survives the sanitiser intact",
      "<path" in deck_mod.sanitize_svg(
          "<g data-part='x'><path d='M10,10 L20,20' stroke='#8fb6ff' "
          "stroke-width='2'/></g>")
      and "data-part" in deck_mod.sanitize_svg(
          "<g data-part='x'><path d='M10,10'/></g>"))
check("svg: an unclosed tag can't break the rest of the page",
      deck_mod.sanitize_svg("<g><circle cx='1' cy='1' r='1'>").count("</g>") == 1)
check("svg: the outer <svg> is dropped — we own the viewBox",
      "<svg" not in deck_mod.sanitize_svg("<svg viewBox='0 0 9 9'><path d='M0,0'/></svg>"))
check("svg: an empty or destroyed drawing is refused, so the step falls back",
      deck_mod._clean_visual({"kind": "svg", "svg": "<script>x</script>"}) is None)
check("svg: a beat naming a part the drawing never tagged is dropped",
      deck_mod._clean_visual({"kind": "svg",
          "svg": "<g data-part='real'><path d='M1,1 L9,9' stroke='#fff'/></g>",
          "beats": [{"say": "x", "set": {"ghost": {"dy": 4},
                                         "real": {"dy": 4}}}]}
      )["beats"][0]["set"].keys() == {"real"})
check("svg: it renders inside our frame with the beat driver attached",
      all(s in deck_mod.render_html({"title": "T", "slides": [{"narration": "n",
              "visual": deck_mod._clean_visual({"kind": "svg",
                  "svg": "<g data-part='a'><path d='M1,1 L9,9' stroke='#fff' "
                         "stroke-width='2'/></g>",
                  "beats": [{"say": "go", "set": {"a": {"dy": 3}}}]})}]})
          for s in ("viewBox=\"0 0 1000 620\"", "data-beats", "function beat(")))
check("svg: its beats are cued off the narration like everything else",
      len(deck_mod.plan_cues([{"narration": "The disc bulges backwards now.",
          "visual": {"kind": "svg", "svg": "<path d='M0,0'/>",
                     "beats": [{"say": "disc bulges backwards"}]}}]
      )[0]["callouts"]) == 1)
check("svg: the prompt hands over a strict frame, not a blank page",
      all(s in deck_mod.visuals_prompt({"title": "t", "slides": []})
          for s in ("0 0 1000 620", "#8fb6ff", "data-part", "textbook plate")))
check("svg: it's a known kind",
      "svg" in deck_mod.VISUAL_KINDS)


# ---- real pictures beat drawn ones for anything that has an appearance ----
# A language model writing SVG paths cannot produce a medical illustration. It
# can produce a clean schematic. So for anatomy the right move is to FETCH a
# published plate, and drawing is for mechanisms and for anything that moves.
check("images: the search targets illustrations, not snapshots",
      "illustration" in deck_mod.search_image.__doc__.lower()
      or "diagram OR illustration" in open(deck_mod.__file__).read())
def _ranker_prefers_a_teaching_plate():
    cands = [
      {"title": "File:Blausen 0484 HerniatedDisc.png",
       "imageinfo": [{"mime": "image/png", "url": "u", "extmetadata": {}}], "index": 7},
      {"title": "File:My holiday snap.jpg",
       "imageinfo": [{"mime": "image/jpeg", "url": "u", "extmetadata": {}}], "index": 0},
      {"title": "File:Herniated disc numlabels-en.svg",
       "imageinfo": [{"mime": "image/svg+xml", "url": "u", "extmetadata": {}}], "index": 1},
      {"title": "File:Lumbar spine autopsy specimen.jpg",
       "imageinfo": [{"mime": "image/jpeg", "url": "u", "extmetadata": {}}], "index": 2}]
    return deck_mod.pick_image(cands)["title"].startswith("File:Blausen")
check("images: a Blausen plate outranks a snapshot, a pre-labelled diagram "
      "and an autopsy photo",
      _ranker_prefers_a_teaching_plate())
check("images: an already-annotated file is pushed down, not chosen",
      deck_mod.rank_image("Heart numlabels-en.svg", "image/svg+xml", 0)
      > deck_mod.rank_image("Blausen heart anatomy.png", "image/png", 5))
check("images: a format a browser can't show is refused outright",
      deck_mod.rank_image("x.tiff", "image/tiff", 0) is None)
# The advice about WHEN to attach a photograph now travels with the `figure`
# spec rather than sitting in a global section, because code chooses the shape
# and a step that is not a figure has no use for it.
_figprompt = deck_mod.visuals_prompt({"title": "t", "slides": []},
                                     kinds=["figure"])
check("images: a figure prompt says when to fetch a real picture",
      "FETCH A REAL PICTURE" in _figprompt)
check("images: and when NOT to, because a wrong organ teaches something false",
      "LEAVE `image` OUT" in _figprompt)
check("images: a step that is not a figure is not told about image search",
      "FETCH A REAL PICTURE" not in deck_mod.visuals_prompt(
          {"title": "t", "slides": []}, kinds=["process"]))
check("images: there's a self-test, because this can't be verified offline",
      callable(getattr(deck_mod, "image_selftest", None)))


# ---- the duplicate-picture bug ----
# The self-test on the real machine returned FOUR successes at 435KB each, by
# the same illustrator — a herniated disc, a nephron and a phospholipid bilayer
# all resolving to one generic file. The cause was a query that appended
# "(diagram OR illustration OR anatomy OR medical OR scheme)": that clause did
# the matching instead of the subject. Bias belongs in the ranker, which sees
# results, not in the query, which decides what they are.
check("images: the query is the subject alone, with no relevance-hijacking OR",
      "diagram OR illustration OR anatomy" not in open(deck_mod.__file__).read()
      .split("gsrsearch")[1][:200])
check("images: two identical files are recognised as identical",
      deck_mod.image_fingerprint(b"same bytes")
      == deck_mod.image_fingerprint(b"same bytes")
      and deck_mod.image_fingerprint(b"a") != deck_mod.image_fingerprint(b"b"))
def _one_picture_is_never_shown_twice():
    plan = {"slides": [{"visual": {"kind": "figure", "image": t, "labels": []}}
                       for t in ("a", "b", "c")]}
    same = (b"IDENTICAL" * 40, "Lynch")
    got = deck_mod.collect_images(plan, log=lambda m: None,
                                  fetch=lambda t, l=None: same)
    # one step keeps it; the others fall back to a drawn silhouette
    return len(got) == 1 and plan["slides"][1]["visual"]["image"] == ""
check("images: the same plate is never used on two steps",
      _one_picture_is_never_shown_twice())
def _different_pictures_all_survive():
    plan = {"slides": [{"visual": {"kind": "figure", "image": t, "labels": []}}
                       for t in ("a", "b", "c")]}
    got = deck_mod.collect_images(plan, log=lambda m: None,
                                  fetch=lambda t, l=None: (t.encode() * 50, "c"))
    return len(got) == 3
check("images: genuinely different pictures are all kept",
      _different_pictures_all_survive())


# ---- the filler lines spoke in the WRONG voice ----
# _recache caches 2 lines per bucket up front and fills the other ~41 on a
# background thread that takes ~15s. If the voice resolves or changes while
# that runs, its audio was synthesised in the OLD voice and got merged in —
# so the user heard a stranger read the filler and his own Neo read the answer.
check("voice: the background cache fill is generation-guarded",
      "_voice_gen" in open(neo.__file__).read()
      and "discarding" in open(neo.__file__).read())
check("voice: switching voice invalidates any fill still in flight",
      open(neo.__file__).read().count("_voice_gen") >= 4)

# ---- reading and listening at the same time ----
# The no-figure card used to print up to 150 characters of the NARRATION, large,
# while Neo read the same sentence aloud. the user's words: "when Neo is talking I
# don't want to see text on the screen, I can read, or I can listen, but I can't
# do both." So the fallback is a LABEL now — the subject of the step, a few
# words, the way a title card works.
_LONG = ("They are built from repeating sugar units and are crucial for "
         "immediate energy needs, providing fuel for cellular respiration "
         "across every tissue in the body")
check("line: the fallback is a label, not a sentence of the script",
      len(deck_mod._key_line(_LONG).split()) <= deck_mod.KEYWORD_LIMIT)
check("line: it never reprints a clause of what Neo is saying",
      deck_mod._key_line(_LONG).lower() not in _LONG.lower())
check("line: it keeps the words that carry the subject",
      "deoxyribose" in deck_mod._key_line(
          "DNA contains the sugar deoxyribose and the base thymine.").lower())
check("line: filler alone never becomes a card",
      deck_mod._key_line("And so it was.") == ""
      or len(deck_mod._key_line("And so it was.").split()) <= deck_mod.KEYWORD_LIMIT)
check("line: an empty step renders nothing rather than an empty frame",
      deck_mod._line({"text": ""}) == "")
check("line: a short line still gets the big type",
      len(deck_mod._key_line("Short and punchy.")) < 30)


# ---- why every presentation was plain text ----
# The log said it outright once the logging existed:
#   [02:10:26] [deck] Four Biological Macromolecules — 6 slides, building art
#   [02:10:28] [deck] visuals failed (ServerError) — using plain steps
# The heavy model refused every request, dress() gave up after ONE try, and
# the whole feature quietly degraded to text. It now tries the chat model too,
# and logs the real message rather than a bare class name.
def _visuals_survive_one_bad_model():
    import providers as _pv, json as _j
    saved = _pv.resolve
    _pv.resolve = lambda kind, client, log=None: (
        "gemini", "heavy-model" if kind == "heavy" else "chat-model")
    art = _j.dumps({"visuals": [{"kind": "number", "value": "9",
                                 "label": "L", "sub": ""}]})
    class _R:
        def __init__(s, t): s.text = t
    class _M:
        def generate_content(s, model=None, **kw):
            if model == "heavy-model":
                raise RuntimeError("503 ServerError: model overloaded")
            return _R(art)
    class _C:
        models = _M()
    try:
        plan = {"title": "T", "slides": [{"narration": "n", "visual": None}]}
        dressed, images = deck_mod.dress(plan, _C(), log=lambda m: None)
        return dressed["slides"][0]["visual"]["kind"] == "number"
    finally:
        _pv.resolve = saved
check("deck: one failing model no longer costs the whole presentation",
      _visuals_survive_one_bad_model())

def _dress_always_returns_a_pair():
    """present() does `dressed, images = dress(...)`. An early return of a bare
    dict would raise there — silently, on a background thread."""
    import providers as _pv, inspect as _i
    saved = _pv.resolve
    _pv.resolve = lambda kind, client, log=None: ("gemini", None)
    try:
        out = deck_mod.dress({"title": "T", "slides": [
            {"narration": "n", "visual": None}]}, None, log=lambda m: None)
        return isinstance(out, tuple) and len(out) == 2
    finally:
        _pv.resolve = saved
check("deck: dress() always returns (deck, images), even with no model at all",
      _dress_always_returns_a_pair())

# Written against the intent, not a literal slice width. This used to grep for
# the exact string "str(e)[:200]" and started failing the moment someone
# trimmed the slice to 180 — a passing/failing signal about how long a log line
# is, not about whether the reason survives.
_vb = _insp.getsource(deck_mod.visuals_batch)
check("deck: a failed visuals call logs the actual error, not just its class",
      "type(e).__name__" in _vb and "str(e)" in _vb)
check("deck: the answer gets an explicit output ceiling so it can't truncate",
      "max_output_tokens" in open(deck_mod.__file__).read())

# =========================================================================== #
# The 4 September session: four things the user hit in one conversation.
# =========================================================================== #

# ---- two voices at once ----
# 17:21:30 the filler started ("On it. This one runs in the background..."),
# 17:21:32 the live model started answering straight over the top of it. The
# guard in _live_ack could only ask whether the model was ALREADY speaking; it
# wasn't, it began two seconds later. This is the other half of that guard.
def _cut_ack_behaviour():
    n = neo.Neo.__new__(neo.Neo)
    calls = {"stop": 0}

    class _FakeSd:
        def stop(self):
            calls["stop"] += 1

    saved, neo.sd = neo.sd, _FakeSd()
    try:
        n._ack_playing = False
        n._cut_ack()                       # nothing playing: must not touch audio
        idle_ok = calls["stop"] == 0
        n._ack_playing = True
        n._cut_ack()                       # filler playing: cut it mid-word
        cut_ok = calls["stop"] == 1 and n._ack_playing is False
        n._cut_ack()                       # and it does not re-stop
        idempotent = calls["stop"] == 1
    finally:
        neo.sd = saved
    return idle_ok and cut_ok and idempotent
check("voice: the live answer cuts a filler that is still playing",
      _cut_ack_behaviour())

check("voice: the live session can tell Neo when its own voice starts",
      "on_speech_start" in open("live.py").read()
      and "on_speech_start=self._cut_ack" in open("neo.py").read())


# ---- "get these off my screen" has to actually mean it ----
# The walkthrough is fire-and-forget on its own thread and waits for a click
# between steps. stop_showing closed the window; the thread then drew the next
# step a moment later. Neo had already said "done, they're gone", so the user had
# to ask twice — and did, in exactly those words.
def _close_supersedes_the_walkthrough():
    import pointer
    before = pointer.current_generation()
    pointer.close("test")                  # no window open; must still cancel
    return pointer.current_generation() > before
check("screen: stopping cancels the walkthrough, not just the window",
      _close_supersedes_the_walkthrough())

def _guide_guards_every_draw():
    import pointer, inspect
    src = inspect.getsource(pointer.guide)
    # both the top of the loop AND immediately before the draw, because
    # locate() and verify_step() are seconds of model round trips in between
    return src.count("_superseded()") >= 3
check("screen: the walkthrough re-checks for a stop before it draws",
      _guide_guards_every_draw())


# ---- never claim a ring that was refused ----
# verify_step said: wanted 'thumbs up icon', the ring is on '95K like icon' —
# so it correctly refused to draw. guide() returned "I couldn't find that on
# your screen", _run() threw that away, and the only thing the user heard was the
# model narrating the optimistic string show_me_on_screen returns instantly:
# "I've put a ring around it for you."
def _failed_walkthrough_says_so():
    import inspect, agent as _a
    src = inspect.getsource(_a.show_me_on_screen)
    return 'spoke["n"] == 0' in src and "outcome" in src
check("screen: a walkthrough that showed nothing says why, out loud",
      _failed_walkthrough_says_so())


# ---- a skill that does not exist cannot be improved ----
# "go to Claude and rebuild the skill and make it actually work" fired
# improve_skill with a name that was not a skill at all. The brief says
# "modify skills/<name>.py", Claude found no such file and CREATED one, then
# reported success. The real highlighting code was never touched.
def _improve_skill_refuses_a_name_that_is_not_a_skill():
    import agent as _a
    import skills as _sk
    _sk.load_all()
    started = {"n": 0}

    class _C:
        def start(self, *a):
            started["n"] += 1
            return "started"

    saved, _a._claude = _a._claude, _C()
    try:
        out = _a.improve_skill("screen highlight", "fix it")
        refused = started["n"] == 0 and "no skill called" in out.lower()
        _a.improve_skill("timer", "put it in the top right")
        real_one_still_works = started["n"] == 1
    finally:
        _a._claude = saved
    return refused and real_one_still_works
check("skills: Claude is never sent to rebuild a skill that doesn't exist",
      _improve_skill_refuses_a_name_that_is_not_a_skill())


def _skill_cannot_shadow_a_builtin():
    import skills as _sk
    return (_sk._shadows_builtin("highlight_on_screen") is True
            and _sk._shadows_builtin("gym_streak") is False)
check("skills: one named after a built-in tool is refused, not loaded beside it",
      _skill_cannot_shadow_a_builtin())


# =========================================================================== #
# 6 September: the tap that was enabled and deaf.
# =========================================================================== #
# The Mac slept at 17:26 and woke at 17:30. Neo's last log line was 16:40 and
# fn did nothing for the next hour and a half — while the main run loop sat
# perfectly healthy waiting for events that were never coming. CGEventTapIsEnabled
# said True the whole time, so the old watchdog had nothing to catch.
#
# The detector compares two clocks: when the SYSTEM last saw a modifier change,
# and when our tap last delivered one.
def _deaf_detector():
    """Counts, not clocks.

    The first version compared "seconds since the SYSTEM last saw a modifier"
    against "seconds since WE last got one", and a single event a session tap
    legitimately never sees — secure input, the lock screen, a switched user —
    read as total deafness. It rebuilt the tap 134 times in four days, several
    exactly 60s apart, which is the cooldown floor. Neo's own log watcher found
    that on its first run.
    """
    D = neo.tap_looks_deaf
    # Genuinely deaf: forty modifier events went past a tap that got none.
    real = D(ours_age=5400, missed=40) is True
    # A couple of missed events is a lock screen, not a broken tap.
    lockscreen = D(ours_age=5400, missed=2) is False
    # He walked away: nobody typed, so nothing was missed.
    away = D(ours_age=5400, missed=0) is False
    # Busy right now — we are being delivered events.
    fine = D(ours_age=1, missed=50) is False
    # Quartz declining to answer is not evidence.
    unknown = D(ours_age=5400, missed=None) is False
    return real and lockscreen and away and fine and unknown
check("tap: an enabled-but-deaf tap is caught by comparing it to the system",
      _deaf_detector())

check("tap: a quiet room is never mistaken for a broken tap",
      neo.tap_looks_deaf(ours_age=99999, missed=0) is False)
check("tap: the bar is high enough that a lock screen can't trip it",
      neo.TAP_MISSED_EVENTS >= 8 and neo.TAP_DEAF_S >= 60)


def _health_check_is_wired():
    # _FnGuard.check_ is a PyObjC selector, not a Python function — inspect
    # cannot read it, so read the file.
    import inspect
    src = open("neo.py").read()
    guard = src[src.index("def check_(self, timer):"):]
    guard = guard[:guard.index("\n    def ", 10) if "\n    def " in guard[10:]
                  else len(guard)]
    cb = inspect.getsource(neo.make_event_tap)
    return (
        # the check has to run BEFORE the hold logic, which returns early
        # whenever the key is up — and a deaf tap means the key always is
        guard.index("watch_tap_health") < guard.index('holder.get("down")')
        # every delivery, including the "you were disabled" notice, is proof
        and "note_tap_event()" in cb
        # a rebuild that fails must not kill a working Neo
        and "if not fatal:" in cb)
check("tap: the health check runs before the hold logic that returns early",
      _health_check_is_wired())


def _rebuild_is_rate_limited():
    import inspect
    src = inspect.getsource(neo.watch_tap_health)
    return "TAP_REBUILD_COOLDOWN_S" in src and neo.TAP_REBUILD_COOLDOWN_S >= 30
check("tap: rebuilding is rate-limited so it can never become a loop",
      _rebuild_is_rate_limited())


def _fresh_tap_is_not_judged():
    """A tap that has never delivered an event must look identical to one that
    has stopped delivering them ONLY if we forget to start its clock."""
    import inspect
    return "note_tap_event()" in inspect.getsource(neo.make_event_tap)
check("tap: a freshly built tap starts its own health clock",
      _fresh_tap_is_not_judged())


# =========================================================================== #
# 9 Sept: Neo turned itself off and nobody was watching
# =========================================================================== #
# A Claude job edited agent.py. The reload watcher exited cleanly to come back
# on the new code — and nothing brought it back, because make_app.sh --start
# had quietly fallen through to `open Neo.app` when the LaunchAgent refused to
# load. NEO_MANAGED was still 1, so Neo believed it was supervised. It was
# dead for eighteen minutes.
def _never_exits_into_nothing():
    import threading as _th
    fired = []
    calls = {"n": 0}

    def moving():
        calls["n"] += 1
        return float(calls["n"])          # always "changed"

    saved = neo.source_fingerprint
    neo.source_fingerprint = moving
    try:
        t = _th.Thread(target=neo._reload_watcher, daemon=True,
                       kwargs={"interval": 0.01, "settle": 0,
                               "restartable": lambda: False,   # nobody watching
                               "on_change": lambda: fired.append(1)})
        t.start()
        _time.sleep(0.2)
        return fired == []                # it must NOT have exited
    finally:
        neo.source_fingerprint = saved
check("restart: Neo refuses to exit for a code change when nothing will "
      "restart it", _never_exits_into_nothing())

check("restart: supervision is asked of launchd, not assumed from an env var",
      "launchctl" in _insp.getsource(neo.supervised)
      and "pid = " in _insp.getsource(neo.supervised))

def _managed_env_is_not_proof():
    """Check the CODE, not the prose — the docstring explains why NEO_MANAGED
    is insufficient, so a plain substring test matches its own explanation."""
    src = _insp.getsource(neo.supervised)
    code = "\n".join(l for l in src.splitlines()
                      if not l.strip().startswith("#"))
    code = code.split('"""')[0] + code.split('"""')[-1]   # drop the docstring
    return "NEO_MANAGED" not in code
check("restart: NEO_MANAGED alone is not treated as proof",
      _managed_env_is_not_proof())

_mk = open("make_app.sh").read()
# Somebody's first hold on a fresh install getting silence is how they conclude
# the product is broken. The rate limit and the short-hold floor both exist for
# good reasons, and neither should apply to the very first lost turn.
_src_drop = open("neo.py", encoding="utf-8").read()
check("first turn: a lost first hold always gets an answer, rate limit or not",
      "_dropped_one_yet" in _src_drop
      and "if not getattr(self, \"_dropped_one_yet\", False):" in _src_drop)
check("first turn: and the short-hold floor does not silence it either",
      "held >= 1.2 or first_ever" in _src_drop)

check("restart: --start says plainly when it could not supervise Neo",
      "NOTHING WILL RESTART IT" in _mk and "bootout" in _mk.split("--start")[1])


print("\nAll tests passed.")
import sys as _sys, os as _os
_sys.stdout.flush()
_sys.stderr.flush()
_os._exit(0)


# ---- the island (overlay.py): one capsule, states by motion, dot by colour ----
import overlay as _ov
_st = _ov.State()
_st.set_step("Searching the web"); _st.set_level(0.7)
check("island: the state carries a step line and a level", _st.get_step() == "Searching the web" and _st.get_level() == 0.7)
_st.set_step("x" * 100)
check("island: a step line is capped so the capsule can't run off the screen", len(_st.get_step()) <= 40)
_ovs = _ov._HTML
check("island: it is a capsule that is hidden when idle", "#cap.on{opacity:1" in _ovs and "opacity:0;transform:translateY(-16px)" in _ovs)
check("island: the dot carries the three colours (blue, amber, green)",
      "#2997FF" in _ovs and ".thinking #dot{background:#F4B23E" in _ovs and ".speaking #dot{background:#30D158" in _ovs)
check("island: the bars stay monochrome", "#bars i{" in _ovs and "background:#111" in _ovs.split("#bars i{")[1][:80])
check("island: bars follow the level while listening AND speaking", "state==='listening'||state==='speaking'" in _ovs)
check("island: thinking says so in a word", "thinking:'Thinking'" in _ovs)
check("island: sits under the menu bar, centred", "visibleFrame()" in open("overlay.py").read() and "(sf.size.width - PANEL_W) / 2" in open("overlay.py").read())
check("island: light or dark by what is behind it", "_ground_is_dark" in open("overlay.py").read())
check("island: the mic level reaches it while the key is held", "on_level=self.state.set_level" in open("neo.py").read()
      and "self.on_level(min(1.0, chunk_rms(chunk)" in open("live.py").read())


# ---- the intelligence upgrade (11 Sept, night) ----
import agent as _ag2
check("brain: think_hard is a tool the fast voice can hand reasoning to",
      _ag2.think_hard in _ag2.TOOLS and "allowed to think" in _ag2.think_hard.__doc__)
check("brain: the rules name the two speeds", "TWO SPEEDS" in open("context.py").read() and "think_hard" in open("context.py").read())
_nsrc2 = open("neo.py").read()
check("brain: a typed turn gets a real thinking budget", "budget = THINK_TYPED" in _nsrc2 and "THINK_TYPED = 2048" in open("commands.py").read())
check("brain: the session learner runs on the smart model, not the lite one",
      'lambda pr: getattr(self.brain._gen(pr), "text", "")' in _nsrc2)
check("acks: known-slow tools get their line within half a second",
      'min(self.ACK_DELAY_S, 0.5)' in _nsrc2)

# ---- what the LIVE run found (11 Sept): three real bugs no string test caught ----
check("calc: plain arithmetic is exact and local (no quota needed)",
      agent._local_maths("17% of 2350") == "399.5." and agent._local_maths("2^10") == "1,024."
      and agent._local_maths("how many feet in a mile") is None)
check("calc: the grounded path has a model fallback, never 'I couldn't'",
      "fallback=lambda: _maths_by_model(problem)" in open("agent.py").read())
check("parsers: reminders and calendar resolve the chat model, never a hard-coded one",
      all('providers.resolve("chat", client, log)' in open(f).read() and '"gemini-2.5-flash")' not in open(f).read()
          for f in ("remind.py", "agenda.py")))
check("parsers: one retry on a busy model", all("for attempt in range(2)" in open(f).read() for f in ("remind.py", "agenda.py")))
check("vision: look_at_screen retries once before giving up", "for attempt in range(2)" in open("hands.py").read().split("def describe_screen")[1])
check("workflows: morning brief, prep, copy screen text, message, volume are tools",
      all(t in agent.TOOLS for t in (agent.morning_brief, agent.prep_for_meeting, agent.copy_screen_text, agent.message_someone, agent.set_volume)))
check("ladder: the hands rung is back, second from the top",
      "YOUR HANDS ON THEIR SCREEN" in __import__("memory").PERSONALITY and "IF IT'S ON THEIR SCREEN, ACT ON THEIR SCREEN" in open("context.py").read())
import workflows as _wf, datetime as _dt2
_b = _wf.format_brief({"now": _dt2.datetime(2026, 9, 14, 7, 30), "events": None, "todos": None, "weather": None, "mail": None})
check("workflows: the brief says what it can't see", "can't see the calendar" in _b)
check("workflows: volume words map to levels", _wf.set_volume.__doc__ and "mute" in _wf.set_volume.__doc__)
check("quota: a daily 429 on the chat model moves to the next model/key and retries the SAME turn",
      "def _reopen_chat" in open("neo.py").read() and 'self._reopen_chat(why="quota")' in open("neo.py").read()
      and "Catch me again tomorrow" in open("neo.py").read().split("used up every model")[1][:80])
