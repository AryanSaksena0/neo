# Neo

**Hold `fn`. Say the thing. Let go.**

A voice assistant that lives on the key under your thumb. No window, no wake
word, no app to switch to. It sees what's on your screen, points at things, reads
your documents, remembers you, and does things on your Mac instead of describing
them. The microphone is only open while the key is down.

It runs on macOS, on Apple Silicon, and costs nothing to operate.

---

## Install

One line, in Terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/aryansaksena2010-web/neo/main/install.sh | bash
```

That installs Homebrew if you don't have it, gets Neo into `~/neo`, sets up
Python and the speech models, builds `Neo.app`, and opens it. Neo then walks you
through the rest in its own voice — the four macOS permissions it needs, and one
free key — in about three minutes.

**No key, no account, no signup needed to start.** Neo installs and runs.
It hears you and answers out loud, both with models on your own Mac. Without a
key it will tell you the time and the weather, open your apps and websites,
switch to whisper or stealth, remember and forget things, and hand real work to
Claude Code if you have it. Ask it anything else and it says so plainly rather
than pretending.

Almost everything good, though, needs a key: conversation, your screen, the
pointer, the highlighter, reminders, the calendar, mail, markets, explanations.
That's one free Gemini key. Say **"add my key"** — Neo opens the page, you press
*Create API key*, and copying it is the whole step. Neo takes it from your
clipboard and becomes the full version on the spot, no restart. You never have
to see the key, and nothing bills, ever.

### Make two or three keys, not one

This is the difference between Neo being good and Neo being annoying, and it
takes thirty extra seconds.

Google's free allowance is counted **per project, not per account**. A second
project on the same Google account is a second full day's allowance. Neo stacks
every key you give it and moves to the next one the moment today's runs dry, so
three keys is three times the day. Copy a second and third key at the key screen
— or say "add my key" again any time — and Neo files each into its own slot.

With one key you will hit a wall mid-afternoon. With three you mostly won't.

Everything else is optional and documented in [.env.example](.env.example) — a
second free key for a bigger daily allowance, other providers as fallbacks, and
the switches in [FEATURES.md](FEATURES.md).

## What it's like

> "What does this error mean?" — it's already looking at your screen.
>
> "Where do I turn off read receipts?" — a ring lands on the actual control.
>
> "Show me the line about the deadline." — a highlighter goes over the words.
>
> "Open Safari, go to the form, put my email in." — it does it; it doesn't
> describe how.
>
> "Put lunch on Thursday at one." · "Remind me to call mum at six." — added,
> then read back out of the calendar or Reminders to confirm. If a time is
> missing it asks once, and never otherwise.
>
> "Did anyone email about the invoice?" — reads the inbox; replies go to Drafts.
>
> "Lease or buy?" — a small panel appears beside the answer.
>
> "What's Apple at?" · "What's the score?" — live numbers, no subscription.
>
> "Remember I take the 8:15." — kept, across restarts.
>
> "Teach yourself to track my reading." — it writes the skill, tests it, and
> keeps it.
>
> "Whisper." · "Hush." — quieter, or silent, instantly.

The full list is in [FEATURES.md](FEATURES.md).

## Things Neo will not do

Deliberate, not limitations.

- **Send mail.** It drafts; you send.
- **Listen when you haven't asked.** The mic opens while you hold the key.
  Nowhere else. That's why there's no permanent orange dot.
- **Spend money.** Any provider that bills is invisible unless you switch it on,
  and that gate is enforced in code.
- **Claim it did something it didn't.** A failed action says so.

## If something's off

```bash
~/neo/go.sh                          # restart and run every self-test
python3 ~/neo/neo.py --doctor        # permissions, mic, speakers, key listener
tail -40 ~/neo/neo.log               # what just happened
python3 ~/neo/onboard.py             # run the setup again
```

Neo also watches its own log and raises a quiet card when it notices a fault.
Ask it "what's been going wrong?" any time.

## Layout

| | |
|---|---|
| `neo.py` | the app, and the intent-routing chain |
| `live.py` | the speech-to-speech session |
| `agent.py` | every tool Neo can call |
| `onboard.py` | the first five minutes |
| `providers.py` | which model does which job — the only place a model id lives |
| `highlight.py` · `pointer.py` | reading the screen, highlighting and pointing |
| `panel.py` · `visuals.py` | the answer panel |
| `skills/` | self-written skills |

`./go.sh` finds every `test_*.py`, runs it, and refuses to commit on a
failure. Suites are discovered rather than listed, so a new one cannot be
written and then silently never run.

## Your data

Neo has no server, no account and no telemetry. Memory, your profile, the
conversation and the logs are plain files in the Neo folder; delete one and the
thing is gone. The only thing that leaves your Mac is the question you asked,
on its way to the model that answers it — and on a free API tier that provider
may train on it. That trade is the price of the free key, and it is spelled out
in full in [PRIVACY.md](PRIVACY.md).

## Licence

[AGPL-3.0](LICENSE). Free to run, read, change and share; if you run a modified
Neo as a service, your users get your source too. Plain-English summary in
[NOTICE.md](NOTICE.md), and [TERMS.md](TERMS.md) for the rest.
