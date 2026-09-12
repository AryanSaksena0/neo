# What Neo can do

Everything Neo has, and what to say to get it. Written to be skimmed when you
have forgotten something.

**The whole interface:** hold `fn`, say the thing, let go. Right-Option works
identically if `fn` ever misbehaves.

---

## Before you add anything: Neo already works

There is no account, no signup, and **no key needed to install and run**.
Be clear-eyed about what that gets you, though — it is a working, honest Neo,
not a full one.

| Works with no key | Why |
|---|---|
| Hearing you | faster-whisper, on your Mac |
| Neo's voice | Kokoro, on your Mac |
| "What time is it?" · "What's the weather?" | the machine's clock; Open-Meteo needs no key |
| Opening apps and websites | macOS |
| Whisper, bedtime, hush, stealth, "say that again" | pure Python |
| Remembering and forgetting things | a local file |
| Handing work to Claude Code | your own CLI subscription |

**Everything else needs the key** — conversation, looking at your screen, the
pointer, the highlighter, reminders, the calendar, mail, markets, scores,
documents, presentations. Ask for one of those without a key and Neo says so in
one sentence and offers the route. It does not pretend.

**Say "add my key"** and Neo opens Google's key page, you press *Create API
key*, and it takes the key from your clipboard and restarts itself into the
full version. You never have to see the key, open a terminal, or edit a file.

---

## Two kinds of command

Worth knowing, because it explains why some phrases work exactly and others are
flexible.

**Fixed phrases** are matched in code (`commands.py`). They work the same way
every time, cost nothing, and never depend on the model's judgement. These are
marked **[fixed]** below.

**Everything else** is decided by the model from what you said. You don't need
the exact words — "what's Apple at", "how's Apple doing" and "check Apple stock"
all land in the same place. Phrasings below are examples, not magic words.

---

## Voice modes

Four states, one switch. Say any of these at any time — including **while Neo is
talking**, which is the point of the first one.

| Say | What happens |
|---|---|
| press `fn` | **Stops Neo instantly, mid-word.** Always has. |
| "hush" · "shush" · "mute" · "be quiet" · "stop talking" · "that's enough" · "never mind" | **Silence, and nothing else.** No reply, no acknowledgement — the rest of that answer is discarded as it arrives. |
| "whisper mode" · "talk quietly" · "keep it down" · "lower your voice" · "too loud" | **Whisper mode.** Neo answers about 13 dB down and slightly slower. The microphone is unchanged. |
| "bedtime mode" · "night mode" · "good night" · "everyone's asleep" | **Bedtime mode.** See below — this one changes the *microphone*. |
| "normal mode" · "talk normal" · "wake up" · "speak up" · "louder" | **Back to normal.** Leaves whisper *and* bedtime. |

Mode changes are asked for the normal way — hold `fn` and say it. Only **hush**
works mid-sentence, deliberately: the microphone that listens while Neo speaks
hears Neo too, so it is trusted to stop him and nothing else.

### Bedtime mode is not a louder whisper

Whisper mode only changes how loudly Neo answers. **Bedtime also opens the
microphone right up** so you can whisper *to* Neo across a dark room.

- microphone amplified **8×** before anything judges it
- the "that was basically silence" gate drops from `0.003` to `0.0004`
- Neo answers quieter than whisper mode — if the point is not waking anyone, the
  answer has to be quieter than the question
- the bar for "that was real speech" drops too — amplifying alone isn't enough,
  because two of those three tests are ratios against the room, so gain moves
  the audio and the bar together
- shushing still works, because the hush listener gets the same sensitivity

**It only works because you asked for it.** Amplifying 8× with the gate near zero
is exactly wrong in a normal room — every fridge hum becomes a turn. At night,
with one person in the room, there is nothing else to hear.

**It expires after about four hours.** Neo restarts itself whenever the code
changes — several times an hour, invisibly — so a mode that vanished on restart
would work for ninety seconds and then quietly stop. It survives those restarts.
But waking up to a machine still amplifying the mic 8× in a room now full of
people is the one failure this mode must not have, so it doesn't last the night.
Whisper mode expires the same way.

### Why voice-hush is off by default

To hear "shush" while Neo talks, the microphone has to be open while Neo talks —
and macOS shows its **orange microphone dot** whenever any app holds the mic. That
light is a system privacy indicator with no way to suppress it, by design. So
voice-hush meant an orange dot during every single answer.

A light that says "something is listening" should mean it and should be rare, so
this is off unless you ask for it. `NEO_HUSH=1` enables it. The `fn` key stops Neo
instantly and holds no microphone at all, so the default loses nothing.

---

## Your screen

| Say | What happens |
|---|---|
| "where do I click to…" · "walk me through…" | A ring appears on the real control and moves to the next as you click. |
| "highlight the line about…" · "show me where it says…" | A highlighter goes over the actual words on screen. |
| "what's on my screen?" · "what does this say?" | Neo screenshots and describes it. |
| "what's the difference between X and Y" · "how does X work" | An **answer panel** appears bottom-right with the shape of the answer. |
| "make me a presentation on…" | Full-screen animated explainer, narrated live. Say the word "presentation" and you get this specifically. |
| "stop showing me" · "get that off my screen" | Clears everything — rings, highlights, panels. |

**The answer panel** picks one of eight layouts: process, comparison, numbers,
timeline, breakdown, structure, table, definition. It shows up on its own when a
picture helps, clears when you ask the next question, and never eats a click.

---

## Documents and files

| Say | What happens |
|---|---|
| "summarise the PDF I've got open" | Reads what's in front of you. |
| "find my physics packet" | Spotlight by filename *or* by what's inside. |
| "open my draft strategy doc" | Opens it and verifies it actually opened. |
| "what did I just copy?" | Reads the clipboard. |

Scanned PDFs work — pages are rendered and looked at, so tables survive.

---

## Calendar, reminders and mail

| Say | What happens |
|---|---|
| "what have I got tomorrow?" | Reads the calendar. |
| "put lunch with Priya on Thursday at one" | Adds it, then reads it back out of the calendar to confirm. |
| "remind me to call mum at six" · "…tomorrow morning" · "…in twenty minutes" · "…on Friday" | A real reminder in the Mac's Reminders app (so it fires on your phone too), read back before it's confirmed. |
| "remind me to call mum" · "remind me later to…" | The one case Neo asks first: *"When do you want reminding?"* Answer, and it's set. |
| "any new email?" · "did anyone email about X?" | Reads the inbox. |
| "draft a reply to…" | Writes it into Drafts. |

**Mail is draft-only, permanently.** Neo writes; you send.

---

## Stealth mode

| Do | What happens |
|---|---|
| **Double-press `fn`** | Stealth on. A small card confirms it and fades. No voice in, no voice out, nothing conspicuous. |
| Press `fn` (in stealth) | A chat box drops into the top-right. Type, return, read the answer; the thread stays. `Esc` closes it. |
| Hold `fn` and whisper (in stealth) | Still works: the answer comes back as text. |
| Double-press `fn` again, or type "normal mode" | Voice back. |

Same brain and every tool ("remind me…", "what's on my screen", "check my Gmail") — only the ears and the voice are swapped for a box. Fillers are dropped, the step line shows as a quiet working line, Neo's browser never pops a sign-in window, and the working tab stays down. Persists across restarts; expires after four hours.

---

## Google Docs, Slides, Sheets, Gmail and Calendar

| Say | What happens |
|---|---|
| "make a doc for my thesis notes" · "start a slide deck about…" · "new spreadsheet called…" | A new Doc / Slides / Sheet, named, opens in your browser in your own Google account. A doc body gets typed in. |
| "write an email to Dean in Gmail" | Gmail's compose window opens with To, Subject and the body filled in — formatted, no em dashes. You press Send. |
| "find a time for me, Dean and Priya next week" | Neo finds a slot where *you* are free, then opens Google Calendar's editor with the guests added; Google's own Find a time shows theirs. |

No Google API, nothing to verify: these are Google's own URLs, opened in the browser you're already signed into. Nothing is sent, saved or invited without you pressing the button.

## Who is who

Names become addresses without anyone typing them. The Mac's Contacts app usually only has phone numbers, so Neo goes where the emails are: memory ("remember that Priya's email is…"), whoever has written to your inbox, and **Google Contacts plus your school or work directory** (`contacts.google.com`) — silently through Neo's own browser when it's signed in, otherwise by opening the search in your Chrome for a few seconds and reading the page. A found address is remembered, so the next time is instant.

---

## Its own browser, in the background

| Say | What happens |
|---|---|
| "check my Gmail" · "open the shared doc and read it to me" · "what's on my Drive" | Neo drives its **own** copy of Chrome, headless, with its own profile. Your windows are untouched; keep working. |
| "click Sent" · "put my email in the field" | Acts on the page it has open, by the visible label. |
| *(a sign-in page appears)* | Neo brings its window forward once and asks you to sign in. Cookies persist; it never sees a password. Say "done" and it goes back to the background. |

No Google API, no verification, nothing to install: it is the Chrome already on the Mac, run separately.

---

## What it knows about you (and keeps current)

Built from the machine, refreshed daily, never asked for: your name and timezone; how you want answers (learned when you say "shorter", "just show it"); how you write (greeting, sign-off, register, from sent mail and, with Full Disk Access, Messages); your active hours and recurring week; every browser profile tagged work / personal / school / family (say "that profile is my mum's" to correct one); what you're working on; what's installed and which permissions Neo has; the people who come up. Every conversation is mined once, when it closes, for durable facts. `profile.json`, local only.

---

## Live numbers

| Say | What happens |
|---|---|
| "what's Apple at?" · "how's the market?" | Stocks, indexes, crypto. |
| "what's the score?" · "did the Lakers win?" | Live and upcoming games. |
| "what are the odds on…" | Polymarket and Kalshi. Settled markets are filtered out. |
| "what's the weather?" | Current conditions and today's high/low. |
| "what time is it?" | Real local time, in your timezone. |

---

## Handing off real work

| Say | What happens |
|---|---|
| "ask Claude to fix the signup bug in my app" | Claude Code runs headlessly in the real project folder. |
| "how's Claude doing?" | Progress on the current or last job. |
| "that's broken" · "file a bug about that" | Files a bug about **Neo itself** with Claude Code. |

Jobs take minutes and run in the background. You get told when they finish.

---

## Teaching Neo new things

| Say | What happens |
|---|---|
| "teach yourself to track my reading streak" | Claude writes a skill, tests it, and it hot-loads without a restart. |
| "what skills do you have?" **[fixed]** | Lists them. |
| "the timer should sit in the top right" | Rebuilds an existing skill to your spec. |

**Skills right now:** `calendar_peek`, `headlines`, `timer`, `todo`.

A skill can't be named after something Neo already has built in — that gets
refused rather than loaded alongside it.

---

## Memory

| Say | What happens |
|---|---|
| "remember that Priya replied" | Kept permanently, across restarts. |
| "forget about X" **[fixed]** | Drops it. |
| "clean up your memory" **[fixed]** | Consolidation pass over everything stored. |
| "open your brain" **[fixed]** | The 3D memory window. |
| "close your brain" **[fixed]** | Shuts it. |

---

## Driving the Mac

| Say | What happens |
|---|---|
| "open Safari" · "open youtube.com" | Apps and URLs. |
| "click the submit button" | Clicks by name, not coordinates. |
| "type my email into that field" | Finds the field by its label and types. |
| "play something by Drake" · "pause" · "skip" | Spotify or Apple Music. |
| "scroll down" · "press return" | Direct control. |

Anything scriptable on macOS is reachable via AppleScript, so Neo can drive apps
that have no specific support here.

---

## Conversation

| Say | What happens |
|---|---|
| "say that again" **[fixed]** | Replays the last answer. |
| "that's all" **[fixed]** | Hangs up the live session. |
| "anything on my radar?" **[fixed]** | Pending background findings, on demand. |
| "how fast are you?" **[fixed]** | Real latency report from logged turns. |
| "focus mode" / "I'm working" **[fixed]** | Neo stops touching your screen. |
| "switch your voice to Fable" **[fixed]** | Changes voice — george, emma, heart, bella, fable… |

---

## Running in the background

The **sentinel** watches your calendar, due dates and to-dos, and surfaces things
as quiet cards rather than interrupting. Ask "anything on my radar?" any time.

**Neo also watches itself.** It reads its own log every 90 seconds for known
failure signatures — a key listener gone deaf, two holds that captured nothing,
the speaker refusing to open, a skill that stopped loading — and raises a silent
card. Ask it directly any time:

| Say | What happens |
|---|---|
| "what's been going wrong?" · "are you okay?" · "have you had any errors?" | Neo reports its own faults from the log. |

It reports; it never repairs. Fixing something is still `"ask Claude to..."`, and
still your call. Every signature came from a fault that actually happened — on
its first run it found 134 unnecessary key-listener rebuilds caused by a bug in
the rebuild logic itself.

---

## When something goes wrong

```bash
cd ~/Desktop/neo
tail -40 neo.log          # what just happened
.venv/bin/python neo.py --doctor   # permissions, mic, speakers, key listener
./go.sh                   # restart, run every test suite, commit
```

`--doctor` prints two speaker lines — the real system output and what the audio
layer thinks it is. **Those disagreeing is a bug**, and Neo now corrects it
automatically before every sound it makes, so your headphones get the voice
instead of the laptop.

### Switches

| Variable | Does |
|---|---|
| `NEO_HUSH=0` | Turns off voice hush (the `fn` key still stops Neo instantly). |
| `NEO_NO_PANEL=1` | No answer panel. |
| `NEO_NO_HUD=1` | No status HUD. |
| `NEO_NO_OVERLAY=1` | No on-screen anything. |
| `NEO_PTT=both` | Right-Option works as a second push-to-talk key. |
| `NEO_BEDTIME_MIC_GAIN` | How hard bedtime listens. Default `8.0`. |
| `NEO_BEDTIME_MIC_GATE` | Bedtime's silence threshold. Default `0.0004`. |
| `NEO_WHISPER_GAIN` | Whisper loudness. Default `0.5`. |
| `NEO_ALLOW_PAID=1` | Lets Neo use providers that bill. Off by default, enforced in code. |

---

## Things Neo will not do

Not limitations — deliberate.

- **Send mail.** It drafts; you send.
- **Listen when you haven't asked.** The mic opens while you hold `fn`, and
  while Neo is speaking so it can hear "shush". Nowhere else.
- **Use your headphone microphone.** Output follows your headphones; input stays
  on the Mac, because routing the mic through a Bluetooth headset flips it into
  hands-free mode and wrecks the audio both ways.
- **Claim it did something it didn't.** A failed action returns an explicit
  "this did not happen" that the model is told not to paper over.
- **Spend money.** Any provider that bills is invisible unless `NEO_ALLOW_PAID=1`.
