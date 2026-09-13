# Neo

A voice assistant that lives on the key under your thumb.

**Hold `fn`. Say the thing. Let go.** That's the whole interface. There is no
window to open, no wake word, no app to switch to, and nothing to click. The
microphone is only open while the key is down.

Neo runs on macOS, costs nothing to operate, and does things on your machine
rather than describing them to you.

---

## What it can do

### Talk
A speech-to-speech socket, so it hears tone and timing and answers in about a
second. The connection stays open between holds, so it remembers the
conversation — and the conversation survives a restart. Press the key while
it's talking and it stops, because you interrupting is the point.

### See what you're looking at
Every question quietly carries what's in front of you: the app, the document
it has open, and what kind of thing is on your clipboard. So **"this" and
"that" work as words.**

> "What does this say?" — while a PDF is open
> "Why is this failing?" — right after copying an error

It carries what is *available*, never the contents. A clipboard holding a
password or an API key is not mentioned at all.

### Point at your screen
> "Where do I turn off read receipts?"

A ring appears on the actual control, on your actual screen, and moves to the
next one as you click — through a whole settings flow or a whole form, until
you get where you were going. The overlay is click-through, so it can never
eat the click it is pointing at.

### Highlight what you're reading
> "Show me the line about the deadline."

A highlighter goes over the actual words. macOS reads the screen locally and
supplies the coordinates; the model only chooses which sentence. After drawing,
Neo looks again and takes down any highlight that isn't on the right words.

### Read your documents
> "Summarise the PDF I've got open."
> "Find my physics packet and tell me what's in it."

Spotlight finds files by name *or* by what's inside them. PDFs are read by
rendering the pages and looking at them, so scans work and tables survive.

### Your calendar, your reminders, your mail
> "Put lunch with Priya on Thursday at one."
> "Remind me to call mum at six." · "Remind me to submit the form on Friday."

Nothing is confirmed that hasn't been read back out of the calendar or
Reminders afterwards. Reminders land in the Mac's own Reminders app, so they
fire on your phone too. If the time is genuinely missing ("remind me to call
mum", "…later"), Neo asks one question — *when?* — and otherwise just sets it.
**Mail is draft-only** — Neo writes it, you send it.

### Live numbers
> "What's Apple at?" · "How's the market?" · "What's the score?"
> "What are the odds on a rate cut?"

Stocks, indexes, crypto, live sports, and prediction markets. All real, all
current, all free.

### Delegate real work
> "Ask Claude to fix the signup bug in my app. I'm going out."

Claude Code runs headlessly in the actual project folder and tells you what
happened when you're back.

### Teach it new things
> "Teach yourself to track my reading streak."

Neo writes the code, tests it, and keeps it — permanently, without a restart.
There is no fixed feature list.

### The small stuff
Timers you can pause by voice, music, apps, the web, the weather, memory that
persists, and a background watcher for the calendar and to-dos.

---

## Setup

Neo needs **nothing** to install and run. It hears you and answers out loud
with models on your own Mac, tells you the time and the weather, opens your
apps and sites, and remembers things — with no key, no account and no signup.
Everything else, which is most of it, waits for the key.

*Thinking* needs one free **Gemini API key**. Say **"add my key"** and Neo opens
[aistudio.google.com/apikey](https://aistudio.google.com/apikey), you press
*Create API key*, and Neo takes it from there — it writes `GEMINI_API_KEY` into
`.env` itself and restarts into the full version.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./go.sh
```

macOS will ask for **Accessibility** (to see the key), **Microphone**,
**Screen Recording** (to look at the screen), and **Calendars** the first time
each is needed. Say yes to the ones you want; anything you decline just
switches that feature off.

### Free, and what free means here
Every job has a Gemini candidate, so Neo runs on that one key alone. The free
tier is roughly **20 requests a day per model** — but that quota is *per Google
project*, so a second project in the same account is a second full allowance.
Add `GEMINI_API_KEY_2` and `_3`; Neo rotates automatically when one runs dry.

Other providers (Groq, Cerebras, Mistral, OpenRouter…) are optional. Any
provider that bills is **invisible** unless you set `NEO_ALLOW_PAID=1`, and
that gate is enforced in code, not by a comment.

### If the key misbehaves
Right-option works exactly like `fn` and always has. If `fn` ever gets weird —
macOS reserves it for dictation and the emoji picker — use that instead. No
configuration.

---

## Handy things to say

| | |
|---|---|
| "That's all" | hangs up the live session |
| "Switch your voice to Fable" | changes voice |
| "Set a timer for twenty-five minutes" | on-screen countdown, pause by voice |
| "Remember that Priya replied" | kept across restarts |
| "What did I just copy?" | reads the clipboard |
| "What have I got tomorrow?" | reads the calendar |
| "Stop showing me" | clears anything on screen |

---

## Honest limitations

- **The first question after a boot takes about fifty seconds.** Every one
  after that is fast. It's a warm-up problem and it's being worked on.
- **Setup is the worst part of the product.** API keys, four permission
  prompts, a terminal. It should be one download and it isn't yet.
- **Two on-screen answers, not one.** Say the word "presentation" and you get
  the full-screen narrated explainer. Everything else that would land better
  with a picture gets the small answer panel instead — one component, bottom
  right, gone when you ask the next thing. See FEATURES.md.
- **Mail needs one thing from you.** Gmail app passwords don't work on
  supervised accounts. Add the account to the Mail app once and Neo is in with
  no password at all.
- **Occasionally it answers a noise.** A short tap in a loud room can open a
  turn. The thresholds are measured on one machine and re-measuring is the
  only honest way to change them.

---

## Layout

| file | what it is |
|---|---|
| `neo.py` | the app, and the intent-routing chain |
| `live.py` | the speech-to-speech session |
| `agent.py` | every tool the brain can call |
| `providers.py` | which model does which job — the only place a model id lives |
| `commands.py` | intent matchers and the speech cleaner |
| `situation.py` | what you're looking at, per turn |
| `desk.py` | clipboard, files, documents |
| `agenda.py` | writing to the calendar |
| `remind.py` | writing to Reminders |
| `mail.py` | reading mail, writing drafts |
| `feeds.py` | markets, crypto, scores, odds |
| `highlight.py` | reading the screen, highlighting text |
| `pointer.py` · `spotlight.py` | the on-screen walkthrough and its overlay |
| `memory.py` · `convo.py` | long-term memory, and the conversation |
| `skills/` | self-written skills |

Tests: `test_neo.py`, `test_routing.py`, `test_desk.py`, `test_highlight.py`,
`test_feeds.py`, `test_deck_hard.py`, `test_fuzz.py`. `./go.sh` restarts Neo
and runs them.
