# HANDOFF — read this first

Written 11 Sept 2026 for whoever (or whichever Claude session) picks Neo up next.
It is the state, the goal, and the rules. Keep it current: update it in the
same commit as any change it describes.

## The goal, in one line

Ship Neo to a few primary users. **Every install is that person's own
assistant** — their name, their memory, their calendar, their people — and
nothing about the first owner is anywhere in the code. Every advertised skill
works or fails honestly, with a fallback.

## Hard rules (the owner has said each of these more than once)

1. **Nothing personal in the shipped code.** No names, schools, companies,
   friends, emails, bundle ids with a name. `test_ship.py` enforces it —
   it greps every shipped file. Personal data files are gitignored
   (`memory.json`, `person.json`, `.browser/`, `stealth.json`, `leads.json`…).
   The owner's old business modules live in `_to_delete/personal/`, unshipped.
2. **Never claim what didn't happen.** A tool that can't do the thing says so
   in its first sentence and offers the route it does have. Confident-wrong
   is the worst output. (`context.py` GROUND_RULES, and each tool's return.)
3. **Every skill has a fallback rung.** Contacts → memory → inbox → Google
   directory → ask. Mac calendar → Google Calendar in Neo's browser → "I
   can't see your week" (no time proposed). Mail.app → IMAP → Gmail in Neo's
   browser. Voice → whisper → stealth (typed). Gemini voice → Kokoro.
4. **No em dashes in anything written for the user** (drafts, Gmail). Code
   enforces it in `mail.format_body`.
5. **Ask before inventing.** If a behaviour is unspecified, ask the owner;
   don't guess and ship.
6. Tests assert code, not prose in comments. `./go.sh` discovers and runs
   every `test_*.py`
   and refuses to commit on a failure. Run it after every change.
7. Fresh-terminal commands must start with `cd ~/Desktop/neo`.

## Architecture in ten lines

- `neo.py` app: fn key (Quartz tap), live Gemini socket (`live.py`), the
  held/typed chain (`_handle` → `_handle_text`), watchdogs, reload on edit.
- `agent.py` every model-callable tool (53). `providers.py` model routing.
- `memory.py` PERSONALITY template (`{NAME}` from `person.py`) + facts.
  `person.py` = the second brain (profile.json: response prefs, writing
  style, routine, browser profiles tagged work/school/personal, apps, people).
  `learn.py` mines each closed session for facts/preferences.
- `overlay.py` the island (top-centre capsule). `stealth.py` typed mode
  (double-press fn). `hud.py` working card, `notify.py` notice card,
  `panel.py`/`visuals.py` answer panel.
- `webdrive.py` Neo's own headless Chrome (own profile in `.browser/`),
  login handoff. `chrome.py` the user's Chrome + profiles.
- `contacts.py` + `directory.py` who-is-who. `gsuite.py` Docs/Slides/Sheets/
  Gmail/Calendar via Google's own URLs. `mail.py` drafts (never sends).
  `agenda.py` calendar write, `remind.py` Reminders, both read-back-verified.
- `onboard.py` first run: welcome → permissions → key → **about you** →
  first words → tour. Narration in `onboard_audio/*.wav` (Charon), recorded
  by `onboard_record.py` (Gemini TTS free tier is **10 requests/day**).
- `install.sh` one-liner points at github.com/aryansaksena2010-web/neo.

## Connectors (11 Sept, evening)

`connectors.py` — Calendar, Reminders, Contacts, Mail, Google. Each has
`status()` (read from macOS), `connect()` (fires the real permission prompt
or the browser sign-in, now) and `test()` (reads something back: "3
calendars — School, Holidays — 22 events next week"). "Granted but empty"
is reported as empty with the fix. Tool: `connect_service`; onboarding has
a Connect scene after the key; every tool that can't reach one of these
says "say 'connect calendar'". `find_meeting_time` proposes no time without
a calendar source (`gsuite.busy_week`: Mac → Google-in-Neo's-browser → None).

## Heavy engine + ask-as-you-go (11 Sept, night)

- `heavy.py`: Claude Code (`claude auth status` JSON) or ChatGPT via OpenAI's
  Codex CLI (`codex login status`, verified with a real one-word `codex exec`
  because status lies after a token expires). `claude_bridge.find_cli()`
  returns "codex" when Claude is out and Codex is in; `_run_codex` runs the
  job with `codex exec --skip-git-repo-check --full-auto -C <dir> -o <file>`.
  Both connectors ("connect claude" / "connect chatgpt") install if missing
  (npm / brew cask) and open a Terminal for the interactive sign-in.
- Connectors now: calendar, google, claude (recommended), chatgpt (the
  alternative), reminders, contacts, mail. Onboarding says "press Connect,
  then Allow/Approve" and badges the recommended ones.
- Ask as you go: a tool that hits a wall calls `agent._need(key, why)` →
  `connectors.need` raises a notice card "Neo needs your Calendar to see
  when you're free — Approve / Not now". Approve → `connect` (real prompt),
  `test` (read-back), Neo says it, and the request that hit the wall is
  re-run (`agent.last_request`; inside a live session it's sent as a user
  turn). Wired for: calendar (meeting + add), reminders, mail.

## Intelligence pass (11 Sept, late)

- Models: chat/heavy lead with **gemini-3.8-flash** (2.5-flash is retiring —
  404 on the second key; it stays as a backstop). Google Search grounding
  still only works on 2.5-flash on key 1; `search_web` falls back to web.py.
- **Two speeds**: the live voice answers instantly; `think_hard(question)`
  hands reasoning to the chat model with a 2048 thinking budget (ground rule
  "TWO SPEEDS"). Typed (stealth) turns think with THINK_TYPED=2048.
- Fillers: every slow tool gets one; known-slow kinds fire within 0.5 s.
- Calendar on screen: **OCR geometry first** (`screencal.read_screen_ocr`:
  hour labels → time axis, day headers → columns, chip text → exact times),
  vision only as fallback. Found the Thursday 9:45–10:30 gap vision missed.
  "school day" = 8:00–15:30. No gap → says so + nearest outside the window.
- Permission prompts go through `access.py` on the main thread; System
  Settings is never opened as a reflex (that was `perms.input_monitoring`
  creating an event tap from python3 during test runs — now IOHIDCheckAccess).
- Measured live latency (last 40 turns): 1 s to first word without a tool;
  most tool turns 1–3 s; browser/vision tail 8–15 s.

## Live run (11 Sept, 23:00) — `test_live_tools.py`

Runs the real tools against the real Mac and network (skips anything
with a side effect on the person). Costs a handful of model requests; run
it by hand, not in go.sh:

    cd ~/Desktop/neo && .venv/bin/python test_live_tools.py

It found three real bugs no string test caught: reminders and calendar
adds were hard-wired to the retiring 2.5-flash (every one failed "no
model"); calculate had no fallback; vision gave up on one 503. All fixed.
Then it found the quota wall: **gemini-3.8-flash is 20 requests/day/key**.
So: `providers.generate_text(client, job, contents)` is the one call every
tool makes now — on a daily 429 it demotes the model and re-resolves, then
tries the same model on the next key. The brain's chat does the same
(`Brain._reopen_chat`) and re-sends the same turn with history intact.
"Catch me tomorrow" only when every model on every key is dry.

New workflows (tools): morning_brief, prep_for_meeting, copy_screen_text,
message_someone (Messages, typed not sent), set_volume. Ladder: the HANDS
rung (click_on_screen / type_into on their own screen) is back as rung 2.

## What works and has been seen working

- Voice, live conversation, island states, hush/whisper/bedtime, stealth
  (typed, in-flight additions, Esc anywhere), reminders (pure parts + API
  surface; first live use prompts for Reminders permission), drafts with
  formatting, the directory lookup in the user's Chrome (found a teacher's
  address live), the Calendar editor opening with guests.

## Known gaps / not verified live

- `busy_week` via Neo's browser (`gsuite.google_busy`) — parser tested on
  page text shaped like the real day view; never run against a signed-in
  browser. Until Neo's browser is signed into Google, `find_meeting_time`
  proposes **no time** and says so.
- Contacts framework first-use prompt on a fresh install.
- `create_google_doc` body typing waits 6 s then types via System Events —
  timing-based; verify on a slow machine.
- The Codex heavy path is built on verified flags but no full job has been
  run through it (the owner's ChatGPT token was expired at the time).
- The Approve card has not been clicked live yet.
- No clean install has been done on a second Mac. This is the biggest
  remaining risk and nothing below matters as much: `setup.sh` assumes
  Homebrew, Python 3.12, PyObjC and a Kokoro model download, none of which
  has been exercised on a machine that isn't the author's.
- The app bundle is unsigned. Built locally from a clone so nothing is
  quarantined and Gatekeeper stays quiet, and the bundle executable is a
  stable shell wrapper so TCC grants survive rebuilds — but a downloadable
  .dmg later needs an Apple Developer account and notarization.

## Decisions taken (owner said "build it")

- Heavy engine = the person's own subscription via CLI (Claude Code or
  Codex), never an API key. With neither connected, `hand_to_claude` answers
  "say connect claude or connect chatgpt" — the tool stays in the toolbox.
- Google sign-in for Neo's browser is a connector row in onboarding
  (recommended), not a forced step.

## How to run things

```bash
cd ~/Desktop/neo && ./go.sh                       # restart + all suites + commit
cd ~/Desktop/neo && .venv/bin/python -u onboard.py --demo
cd ~/Desktop/neo && .venv/bin/python person.py --refresh
cd ~/Desktop/neo && .venv/bin/python onboard_record.py   # re-record changed lines
```

## Files a fresh machine will not have (and that is correct)

Three gitignored files hold the personal half of this project. Neo works
without any of them; they exist so that personal data never enters a commit.

| file | what it is |
|---|---|
| `CLAUDE.local.md` | the owner's private project notes. Claude Code reads it alongside the tracked `CLAUDE.md` |
| `.personal-words` | the deny list `test_ship.py` scans for. Real names, so it is never committed; with no list, the test derives one from `git config` |
| `.memory-audit` | optional `keep:`/`gone:` phrases for the opt-in memory audit (`NEO_MEMORY_AUDIT=1 .venv/bin/python test_voice_feel.py`) |

## Local mode — 12 Sept 2026

**Neo now runs with no API key at all.** This was the highest-friction bug in
the product and it was invisible because it only ever hurt people who weren't
the author.

`neo.py` used to `sys.exit(1)` when `.env` had no `GEMINI_API_KEY`. Meanwhile
the README said "skip it and Neo still talks, in a local voice" and the
onboarding's own button said "Skip for now". Both were false: skipping killed
the app on the next launch, silently, *after* the person had installed
Homebrew, waited through a model download and granted four macOS permissions.
The escape hatch was a trap, at the exact moment someone decides whether this
thing is real.

It was never true that Neo needs a key to be useful: ears are faster-whisper
on device, the voice is Kokoro on device, every fixed phrase in `commands.py`
is pure Python, timers / reminders / calendar / Contacts / clipboard / files /
apps / music / volume are macOS, and markets, crypto, scores and the weather
are keyless public endpoints. What needs a key is *thinking*.

- `KEYLESS` in `neo.py` replaces the exit.
- `LocalBrain` duck-types `Brain` with `client = None`. `respond()` says what
  it can and cannot do in one sentence and offers the route out. Every other
  method the chain calls answers without a model.
- The live socket never opens keyless; `_load_engine` returns `kokoro`.
- The two chain spots that hand a client straight to a model (memory
  consolidation, `describe_screen`) say so honestly instead.
- `commands.wants_key_setup` hears "add my key" / "make yourself smarter" /
  "why can't you think". Checked HIGH in the chain, above everything that needs
  a model, because in local mode it is the most important sentence a person can
  say.
- `_add_key_flow` opens Google's key page in the **user's own** browser (their
  account, their click — no automation on the button, which keeps this clear of
  Google's ToS), watches the clipboard for `NEO_KEY_WAIT` seconds, saves via
  `onboard.save_key` and exits cleanly so the LaunchAgent relaunches into the
  full version. The key is never spoken, logged or shown.

`test_ship.py` pins all of it, including that the README and the code agree.
Every suite passes with a key and with `GEMINI_API_KEY=` empty.

**The rule this came from: an escape hatch has to escape somewhere.** Any
"skip", "later" or "not now" in this product must lead to a Neo that still
works.

> Note: an earlier version of this section, plus a "Shipping pass" section
> recording the 12 Sept personal-data and test fixes, were written to this file
> and later overwritten — Neo rewrites files in this folder while it runs. If
> you are editing HANDOFF.md, re-read it immediately before writing.

## Fresh-user reset — 12 Sept 2026, 01:40

The machine was returned to "never installed" so the owner can walk the
onboarding as a new user would.

**Removed:** the LaunchAgent (`app.neo.assistant`), `~/Applications/Neo.app`,
every running process, and all personal runtime state — `.env`,
`memory.json`, `profile.json`, `convo.json`, `.neo.onboarded`,
`stealth.json`, `voice.json`, `sentinel.json`, `usage.json`,
`models.json`, the logs, `neo_metrics.jsonl`, `.browser/`,
`.voicecache/`, `.imgcache/`, `claude_runs/`, `content_out/`,
`skills/*_data.json`.

**Backed up first, in full, to:**

    /Users/aryansaksena/neo-backup-20260912-013932

That includes the 70 learned facts, the profile, both Gemini keys, and the
signed-in browser profile. Restoring is a copy back into the repo. Kept in
place: `CLAUDE.local.md` and `.personal-words` (notes and dev tooling, not
runtime state).

Code and git history untouched — working tree clean at `cf977b0e`.

## Tomorrow: the one-paste install (owner's brief, 12 Sept)

Goal, in the owner's words: *"There should be a GitHub, and you paste 1 thing
into terminal, and you can get it, and once everything downloads, it goes to
the onboarding, and then the key input — either they create a key, or input
their own — and then the Claude Code / Codex account they want backing."*

**This answers an open question that was previously flagged as
"unspecified, ask before building":** the heavy engine is connected at
onboarding via the CLI login (Claude Code or Codex), not via an API key.

Smaller than it looks. What already exists:

- `install.sh` is the one-paste command, already pointing at
  github.com/aryansaksena2010-web/neo. It only needs the repo to exist.
- `connectors.py` already has `claude` and `chatgpt` rows with real
  `connect()` / `test()`, and the onboarding Connect scene renders whatever
  `connectors.status()` returns — so both already appear. Verify on screen.
- Scene order is already welcome -> perms -> key -> connect -> about -> first
  -> tour, which is the order asked for.
- The key scene already watches the clipboard continuously, so pasting your
  own key ALREADY works. It is simply not offered as a choice.

What actually needs doing:

1. **Key scene: make the two paths explicit.** One button creates a key
   (opens Google's page, Neo takes it from the clipboard); a second says
   "I already have one" and makes the paste target obvious. The plumbing is
   there; this is copy and a button.
2. **Push the repo to GitHub.** NOT DONE — publishing is the owner's call,
   and public-vs-private was never agreed. Ask first. AGPL was chosen with a
   public release in mind, so public is the likely intent, but do not assume.
3. **Walk the whole thing on this machine** and fix what the first run shows.
   Everything above is untested against a real first run.
4. Still untested anywhere: Homebrew installing from scratch, and the four TCC
   grants against a fresh `Neo.app` identity.

## Overnight run — 12 Sept 2026, 02:00–03:00

Owner's brief: fix the onboarding frustrations, then make Neo something you can
send a GitHub link to. Priorities he named: (1) knowledge — one wrong answer and
the user never comes back, (2) latency and voice, not robotic.

### Fixed

- **A key saved mid-session did nothing.** `KEYLESS` is decided once, at import,
  so a key added during onboarding left the process on `LocalBrain` and Kokoro —
  while the onboarding said *"Got it. That's my real voice from here on."* Neo
  promising a thing and not doing it, in the first two minutes of every install.
  `Neo.key_arrived()` now rebuilds the brain, the cloud voice, the filler cache,
  the ears and every binding in place. No restart. The spoken "add my key" flow
  uses it too and only falls back to the relaunch if it fails.
- **You could only ever have one key.** `save_key` overwrote `GEMINI_API_KEY`
  every time, while `providers.py` had rotation across `_2.._8` sitting unused.
  It now fills the next free slot, and re-adding a key is a no-op. The key scene
  says plainly that the allowance is per *project*, keeps watching after the
  first, and counts them back. **Rotation verified**: a dry key is retired, a
  burst 429 is not mistaken for a daily one, and the next key takes over.
- **The welcome screen.** Three CSS mock-ups of windows that read as grey
  placeholder boxes, under a tagline that described every assistant ever
  shipped. Replaced with the one thing that is actually true and actually
  different: hold the key, say it, let go. The JS that filled them is now
  guarded, because one missing element used to take the whole script with it.
- **The narration dragged** because it was literally instructed to:
  `STYLE = "Say this calmly and unhurried: "`. Now warm and normally paced.
  Copy rewritten shorter across every scene; "Give me three minutes" is gone.
- **Two voices in one install.** `live.py`, `neo.py` and `onboard_record.py`
  each decided the voice separately. One constant in `live.py` now; the
  recorder derives from it and re-records on a **voice** change, not just a text
  change. Default moved off Charon ("Informative", the flat one) to Sulafat.
- **The connect scene presented a macOS prompt and a browser sign-in
  identically.** Each row now says whether it is one tap or a sign-in, and the
  one-tap ones lead. Mail is marked "needs setup" because it genuinely is.

### Blocked, and this is the one thing not finished

**Four of the eight narration lines are unrecorded**, so those scenes are
silent. Gemini's TTS free tier is 10 requests/day/project and both keys are
spent. Silence is the deliberate degradation (a stand-in voice would be a
different person); it is still not polish. One command when the quota resets:

    cd ~/Desktop/neo && .venv/bin/python onboard_record.py

`test_onboard.py` now fails on stale text, fails on mixed voices, and skips
loudly on unrecorded scenes, so this cannot ship unnoticed again.

### Not done, and why

- **A downloadable, signed `.dmg`** needs an Apple Developer account ($99/yr).
  The install is still `curl | bash`, which is the single biggest remaining
  conversion loss for non-technical people.
- **The onboarding was never seen running.** Every change above is verified by
  structure, not by eye: HTML tag balance, every scene in `ORDER` present, every
  `getElementById` target existing, and the page's JavaScript parsing under
  `node --check`. It needs a real run.
- **Latency is unchanged** at ~4.2 s median to first sound.
- **Knowledge is still capped** by the free Gemini tier. The unexplored lever is
  `CEREBRAS_API_KEY`: `providers.py` already has `llama-3.3-70b` and
  `gpt-oss-120b` wired and marked free, and Cerebras exists to serve big models
  fast. Free signup, no code needed. Measured for comparison: OpenRouter's free
  `nemotron-3-super-120b` answers in **0.66 s to first token** (vs 4.2 s today),
  but its ~50/day account-wide pool makes it a fallback, not a brain.

Owner's `.env` has three `GEMINI_API_KEY` lines but **one is a placeholder** —
two real keys, not three.
