"""test_onboard.py — the first five minutes, checked without a window.

The rule the whole flow rests on: every check is REAL. A setup that says "done"
before it is done teaches the person to distrust everything Neo says after.

Run: python3 test_onboard.py
"""
import os
import os as _os
import sys
import tempfile

sys.path.insert(0, ".")
import onboard
import perms

FAILED = []


def skip(name, why=""):
    print(f"SKIP - {name}" + (f" ({why})" if why else ""))


def check(name, cond):
    print(("PASS - " if cond else "FAIL - ") + name)
    if not cond:
        FAILED.append(name)


# ---- the key: found in a paste, saved into .env, junk refused ----
check("key: a Gemini key is found inside whatever was copied",
      onboard.key_in("here you go AIzaSyD-abcdefghijklmnopqrstuvwxyz012345 ok")
      == "AIzaSyD-abcdefghijklmnopqrstuvwxyz012345")
check("key: the newer AQ. shape is recognised too",
      onboard.key_in("AQ." + "x" * 44) is not None)
check("key: an Anthropic key is not mistaken for a Gemini one",
      onboard.key_in("sk-ant-api03-" + "x" * 40) is None)
check("key: ordinary clipboard text finds nothing",
      onboard.key_in("meet at 3, bring the notes") is None)

fd, env = tempfile.mkstemp(suffix=".env")
os.write(fd, b"OTHER=1\nGEMINI_API_KEY=paste_your_key_here\n")
os.close(fd)
ok = onboard.save_key("AIzaSyD-abcdefghijklmnopqrstuvwxyz012345", env)
body = open(env).read()
check("key: saving replaces the placeholder instead of adding a second line",
      ok and body.count("GEMINI_API_KEY=") == 1
      and "AIzaSyD-abc" in body and "OTHER=1" in body)
check("key: junk is refused and the file is untouched",
      onboard.save_key("not a key", env) is False and "AIzaSyD-abc" in open(env).read())
check("key: 'already set' reads the real file",
      onboard.key_already_set(env) is True)
os.unlink(env)

# ---- permissions: real reads, never guesses ----
st = perms.status()
check("perms: every permission answers True, False or None — never a guess",
      all(v in (True, False, None) for v in st.values()))
check("perms: the required set is the three Neo cannot work without",
      set(perms.REQUIRED) == {"accessibility", "input", "microphone"})
check("perms: 'missing' lists only required ones that are not granted",
      perms.missing({"accessibility": True, "input": False, "microphone": True,
                     "screen": False, "calendar": False}) == ["input"])
check("perms: each one deep-links to its exact System Settings pane",
      all(p.startswith("Privacy_") for _, _, _, p in perms.PERMISSIONS))
check("perms: Accessibility is read with no new dependency",
      "ctypes" in open("perms.py").read() and "AXIsProcessTrusted" in open("perms.py").read())

# ---- the tour never mentions presentations ----
check("tour: presentation mode is not in the tour",
      not any("present" in (a + b + c).lower() for a, b, c in onboard.TOUR))
check("tour: nine things, each with a phrase to say",
      len(onboard.TOUR) == 9 and all(c[1].startswith("“") for c in onboard.TOUR))
check("tour: every card is a feature that exists (a tool or route backs it)",
      all(any(w in (a + b + c).lower() for w in ws) for (a, b, c), ws in zip(onboard.TOUR, [
          ("screen",), ("ring", "highlight"), ("opens apps", "clicks"),
          ("calendar", "reminders"), ("drafts", "inbox"), ("panel",),
          ("markets", "sport"), ("remember", "abilities"), ("whisper", "hush")])))
# The welcome scene leads with the GESTURE, not with three CSS mock-ups of
# windows. Those read as grey placeholder boxes — the worst possible first
# impression for a product whose claim is that it does real things — and the
# tagline said nothing ("the all-in-one assistant for your Mac" describes every
# assistant ever shipped). What is on screen now is the one thing that is
# actually true and actually different: hold the key, say it, let go.
check("welcome: leads with the gesture, not a generic tagline",
      'class="mark arrive">Neo<' in onboard._HTML
      and 'class="hero arrive">Hold' in onboard._HTML
      and "Say it. Let go." in onboard._HTML
      and "The all-in-one assistant for your Mac." not in onboard._HTML)
check("welcome: no fake screenshots on the first screen",
      'id="showcase"' not in onboard._HTML and "CARDS.slice(0,3)" not in onboard._HTML)
check("welcome: and the tour still has the real commands to show",
      all(f'{k}:' in onboard._HTML for k in ("point", "hl", "remind", "see", "do", "mail", "answer")))
check("welcome: a missing element can never take the whole script down again",
      "function fill(id, html)" in onboard._HTML and "if (el) el.innerHTML" in onboard._HTML)
check("welcome: no intro sequence, no orb",
      "runIntro" not in onboard._HTML and 'class="orb' not in onboard._HTML)
check("cards: every spoken line is a real command",
      all(x in onboard._HTML for x in ("Where do I turn off read receipts?", "Remind me to call mum at six.",
                                       "Draft a reply to that.", "Lease or buy?")))
# The sidecar is "<voice>\n<text>", so this catches BOTH kinds of drift:
# a copy edit that leaves the audio saying the old script, and a voice change
# that leaves half the scenes as a different person. Both have happened.
import os as _os
import live as _live
_recorded, _stale, _voices = {}, [], set()
for _k, _v in onboard.NARRATION.items():
    _t = _os.path.join("onboard_audio", f"{_k}.txt")
    if not _os.path.exists(_os.path.join("onboard_audio", f"{_k}.wav")):
        continue
    _voice, _, _said = (open(_t).read() if _os.path.exists(_t) else "").partition("\n")
    _recorded[_k] = _voice
    _voices.add(_voice)
    if _said != _v:
        _stale.append(_k)
check("narration: no scene plays audio that says something else"
      + (f"  <-- {_stale}" if _stale else ""), not _stale)
check("narration: every recorded line is the same voice"
      + (f"  <-- {sorted(_voices)}" if len(_voices) > 1 else ""), len(_voices) <= 1)
check("narration: and it is the voice the live session speaks in"
      + (f"  <-- recorded {sorted(_voices)}, live is {_live.VOICE}"
         if _voices and _live.VOICE not in _voices else ""),
      not _voices or _live.VOICE in _voices)

# ---- once, then never uninvited ----
check("marker: a machine that has been through it is not asked again",
      "NEO_SKIP_ONBOARD" in open("neo.py").read()
      and "onboard.onboarded()" in open("neo.py").read())
check("marker: the marker is per machine, not committed",
      ".neo.onboarded" in open(".gitignore").read())

# ---- narration is short, and there is a line for every scene ----
check("voice: every scene has a line for Neo to say",
      set(onboard.NARRATION) >= {"welcome", "perms", "key", "first", "tour", "done"})
check("voice: no line is a paragraph",
      all(len(v) < 240 for v in onboard.NARRATION.values()))

# ---- 'first words' advances only on a real turn ----
check("first words: Neo tells the onboarding when it actually hears something",
      "ob.heard(text)" in open("neo.py").read())

# ---- the installer ----
_inst = open("install.sh").read()
check("install: refuses anything that isn't an Apple Silicon Mac",
      "arm64" in _inst and "Darwin" in _inst)
check("install: is re-runnable (pulls if already there)",
      "pull --ff-only" in _inst)
# A non-empty ~/neo is the NORMAL failed state, not a rare one: Neo's own
# Chrome recreates .browser/ after everything else is deleted. `git clone`
# refuses a non-empty directory, so the one-liner died with a git error.
check("install: a non-empty ~/neo does not kill the installer",
      "is not an empty directory" in _inst or "keeping what's already there" in _inst)
check("install: and it lays the code in WITHOUT destroying .env or .browser",
      "tar cf -" in _inst and "rm -rf \"$DIR\"" not in _inst)
_mk = open("make_app.sh").read()
check("stop: takes Neo's own Chrome with it, matched on the profile path",
      "stop_neo_chrome" in _mk and "--user-data-dir=$prof" in _mk)

check("install: ends by opening Neo, which does the rest",
      "make_app.sh" in _inst)


# ---- the voice is Neo's own, recorded, or nothing ----
import os as _os
_unrecorded = [k for k in onboard.NARRATION
               if not _os.path.exists(_os.path.join("onboard_audio", f"{k}.wav"))]
if _unrecorded:
    # A missing line is SILENCE, which is a deliberate, safe degradation — and
    # far better than the alternative that produced it (half the flow in a
    # different voice). It is still not shippable polish, so it is loud here.
    skip(f"voice: {len(_unrecorded)} scene(s) still unrecorded ({', '.join(_unrecorded)})",
         "Gemini TTS free tier is 10/day/project; run onboard_record.py when it resets")
else:
    check("voice: every scene has a recorded line in Neo's own voice", True)
_ob_src = open("onboard.py").read()
check("voice: a missing recording means silence, never a stand-in voice",
      '["say"' not in _ob_src                        # the macOS say command
      and "no recorded line" in _ob_src)
check("voice: played OUTSIDE the process (afplay), so nothing we do can starve it",
      '["afplay", path]' in _ob_src and "sounddevice" not in _ob_src)
check("voice: one line at a time — a new scene terminates the old process first",
      "self._hush()" in _ob_src.split("def _narrate")[1].split("def _hush")[0]
      and "v.terminate()" in _ob_src)
check("perms: the status poll runs off the main thread",
      "target=self._poll_perms" in _ob_src)


class _Voice:
    """Stands in for the afplay process."""
    def __init__(self): self.dead = False
    def poll(self): return 1 if self.dead else None
    def terminate(self): self.dead = True


_fake = onboard.Onboarding.__new__(onboard.Onboarding)
_fake._voice = _Voice()
_v = _fake._voice
onboard.Onboarding._hush(_fake)
check("voice: _hush terminates the playing line and forgets it",
      _v.dead and _fake._voice is None)
onboard.Onboarding._hush(_fake)      # nothing playing: must not raise
check("voice: _hush with nothing playing is a no-op", _fake._voice is None)

# ---- no personal references ----
_tour = " ".join(a + b + c for a, b, c in onboard.TOUR).lower()
check("copy: the tour names nobody and nothing personal",
      not any(w in _tour for w in ("sam", "priya", "gym", "roth", "ira", "the project", "the user")))

# ---- terms and privacy exist and are one click away ----
check("legal: the policies exist", _os.path.exists("PRIVACY.md") and _os.path.exists("TERMS.md"))
check("legal: the welcome links to both", "PRIVACY.md" in onboard._HTML and "TERMS.md" in onboard._HTML)
# The policy used to reserve rights the code has no mechanism for: server
# retention, and selling de-identified data, both marked [INTENDED]. Consenting
# a user in advance to something that does not exist is the opposite of honest,
# so the rule is now the strict one: the policy describes what ships TODAY.
_priv = open("PRIVACY.md").read()
_terms = open("TERMS.md").read()
check("legal: the policy promises nothing that isn't built",
      "INTENDED" not in _priv and "[N]" not in _priv and "[link]" not in _priv
      and "[email address]" not in _priv)
check("legal: no data-sale clause, because there is no such mechanism",
      "sell" not in _priv.lower().split("## what neo does not do")[0])
check("legal: the policy names the real destination of a request",
      "gemini" in _priv.lower() and "ai.google.dev/gemini-api/terms" in _priv)
check("legal: free tiers being used for training is stated, not buried",
      "train" in _priv.lower())
check("legal: terms carry a warranty disclaimer and a liability limit",
      'as is' in _terms.lower() and "liability" in _terms.lower())
check("legal: a licence file exists and the terms point at it",
      _os.path.exists("LICENSE") and "LICENSE" in _terms)


if FAILED:
    print(f"\n{len(FAILED)} FAILED:")
    for n in FAILED:
        print("  - " + n)
    sys.exit(1)
print("\nOnboarding clean.")
