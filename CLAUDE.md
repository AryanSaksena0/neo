# Neo — project knowledge for Claude Code

You are working inside **Neo**, a hands-free voice assistant for macOS. When
Neo hands you a job in this folder, this file is your standing context. Read it
before touching anything, so you act with knowledge rather than from scratch.

If `CLAUDE.local.md` exists next to this file, it is the owner's private half
of the same context — read that too.

## What Neo is

**Hold `fn`, talk, let go.** Releasing the key means "I'm done, answer me".
That is the entire interface. Do not turn it into a toggle, a wake word, or a
silence timer.

**Live is the default.** There is no "conversation mode" to invoke and none
should be added back — an assistant you have to switch into conversational mode
is not one.

The microphone is open only while the key is held (and, when `NEO_HUSH=1`,
while Neo is speaking so it can hear "shush"). Nowhere else. This is why there
is no permanent orange mic dot, and it is a promise the product makes in
writing. Do not open the mic anywhere else.

## Architecture

| file | what it is |
|---|---|
| `neo.py` | the app: fn key (Quartz tap), the routing chain (`_handle` → `_handle_text`), watchdogs, reload-on-edit |
| `live.py` | the Gemini speech-to-speech socket |
| `agent.py` | every model-callable tool |
| `providers.py` | job → model resolution. **The only place a model id lives** |
| `commands.py` | fixed-phrase intent matchers and the speech cleaner |
| `context.py` | the system prompt's ground rules |
| `memory.py` · `person.py` · `learn.py` | long-term memory, the profile, and mining closed sessions for facts |
| `situation.py` · `desk.py` | what you're looking at; clipboard, files, documents |
| `agenda.py` · `remind.py` · `mail.py` | calendar writes, Reminders, mail (drafts only) |
| `highlight.py` · `pointer.py` · `spotlight.py` | reading the screen, highlighting, the on-screen walkthrough |
| `overlay.py` · `hud.py` · `notify.py` · `panel.py` · `visuals.py` | the island, the working card, notices, the answer panel |
| `webdrive.py` · `chrome.py` | Neo's own headless Chrome; the user's Chrome |
| `connectors.py` · `access.py` · `perms.py` | services, macOS permission prompts (main thread!), permission state |
| `claude_bridge.py` · `heavy.py` | handing real work to Claude Code or Codex |
| `skills.py` · `skills/` | self-written, hot-loaded skills |
| `onboard.py` | the first five minutes |

## Rules that are not negotiable

1. **Never claim what didn't happen.** A tool that cannot do the thing says so
   in its first sentence and offers the route it does have. Confident-wrong is
   the worst possible output. See `context.py` GROUND_RULES and every tool's
   return value.
2. **Every capability has a fallback rung, and the last rung is honesty.**
   Contacts → memory → inbox → Google directory → ask. Mac calendar → Google
   Calendar in Neo's browser → "I can't see your week" (and propose no time).
   Mail.app → IMAP → Gmail in the browser. Voice → whisper → typed stealth.
   Gemini voice → local Kokoro.
3. **Nothing personal in shipped code.** No names, schools, companies, friends,
   emails, schemas, or bundle ids with a name in them. `test_ship.py` enforces
   this and has caught real leaks; do not weaken it. Per-project facts belong
   in that project's own CLAUDE.md, never hardcoded into Neo.
4. **Never spend the user's money without being told to.** Any provider that
   bills is invisible unless `NEO_ALLOW_PAID` names it. The gate is in
   `providers.have()`, deliberately in one place. A comment is not a gate.
5. **No em dashes in anything written for the user** (drafts, Gmail).
   Enforced in `mail.format_body`.
6. **Mail is draft-only, permanently.** Neo writes; the person sends.
7. **Tests assert code, not prose.** A test that greps a comment proves
   nothing. `./go.sh` discovers and runs every `test_*.py` and refuses to
   commit on a failure.
8. **A suite isolates itself.** Set `NEO_NO_AUDIO=1` inside the test file, not
   just in the runner. Suites that relied on `go.sh` for isolation wrote to the
   real `voice.json` and opened a real browser sign-in when run by hand.
9. **Ask before inventing.** If a behaviour is unspecified, ask. Don't guess
   and ship.

## Things that have bitten before

- **Permission prompts must be requested on the main thread.** EventKit and
  Contacts deliver their prompt through the main run loop; asking from a worker
  thread silently never prompts. That is what `access.py` is for.
- **macOS attaches permission to the binary that asks.** Granting Microphone to
  a terminal's python is a different identity from `Neo.app`. This is why
  `make_app.sh` points the LaunchAgent at the bundle executable, and why the
  bundle's launcher script must stay byte-stable across rebuilds.
- **The free Gemini tier is a small daily per-model, per-project quota.** Not a
  rate limit — a day's allowance. Anything that makes several model calls per
  request (the deck, vision) can spend it in one sitting. Check
  `providers.CANDIDATES` comments before reordering anything; the ordering is
  measured, not guessed.
- **A model id in the source ages out.** Resolve jobs through `providers`, and
  let a failing model be demoted at runtime.

## Working here

```bash
cd ~/neo && ./go.sh                 # restart, run every suite, commit
cd ~/neo && .venv/bin/python neo.py --doctor
tail -40 ~/neo/neo.log
```

Run `./go.sh` after every change. It will not commit on a failing suite, and
that is the point.
