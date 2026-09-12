# Privacy

Last updated: 12 September 2026.

The short version: **Neo runs on your Mac and sends nothing to us, because
there is no us to send it to.** There is no Neo server, no Neo account, and no
telemetry. What Neo knows about you is in files in the Neo folder, on your
disk, and you can read or delete any of them with a text editor.

The one thing that does leave your machine is the question you asked, on its
way to the AI model that answers it. That is described in full below.

## Who this is

Neo is free, open software maintained by an individual. There is no company, no
service, and no infrastructure behind it. Questions, and anything you think is
wrong here, go to the issue tracker in the repository this came from.

## What stays on your Mac

All of it. These are real files in the Neo folder — open them:

| File | What's in it |
|---|---|
| `memory.json` | Facts you asked Neo to remember, and facts it inferred |
| `person.json` / `profile.json` | Your name, timezone, how you like answers, your routine |
| `convo.json` | The current conversation |
| `neo.log` | What Neo did, for debugging |
| `neo_metrics.jsonl` | Timings — how long turns took |
| `.browser/` | Cookies for Neo's own copy of Chrome, including any Google session |
| `.env` | Your API keys |

Nothing in that list is uploaded, synced, or backed up anywhere by Neo. Deleting
a file deletes the thing. `.gitignore` keeps every one of them out of the
repository, so they cannot be committed by accident.

## What leaves your Mac, and where it goes

To answer you at all, Neo sends data to the AI provider whose key you supplied
— by default **Google's Gemini API**, under
[Google's API terms](https://ai.google.dev/gemini-api/terms). Specifically:

| Sent | When |
|---|---|
| **Your voice audio** | Only while you hold the key. The mic closes when you let go. |
| **What you said, as text** | Every turn |
| **Recent conversation** | Every turn, so it follows the thread |
| **Facts from memory** | Every turn, so it knows who you are |
| **A screenshot or screen text** | Only when you ask it to look at, point at, or highlight something |
| **Document text** | Only when you ask about a document |
| **Clipboard contents** | Only when you ask what you copied |
| **Calendar / mail contents** | Only when you ask about them |

Each turn also carries a short note about *what is available* — which app is
in front, what kind of thing is on the clipboard — never the contents. A
clipboard holding something that looks like a password or an API key is not
mentioned at all.

If you connect Claude Code, work you hand off goes to Anthropic under their
terms. If you add keys for other providers, requests routed to them go to them.
Neo never sends anything to a provider you have not given a key for, and
providers that charge money are invisible to it unless you switch them on
explicitly with `NEO_ALLOW_PAID`.

**Free API tiers are usually not private.** Google, and most providers, may
train on what you send them on a free tier. Your conversations, and any screen
or document text you asked about, can become training data for their models.
That is their policy and not something Neo can change, and it is the real price
of the free key — the thing that makes Neo cost nothing to run is also the
thing that makes it least private. Read your provider's terms. If that matters
to you, use a paid key, where the terms are usually different, and check them.

## What Neo does not do

- No analytics, no crash reporting, no usage pings, no "anonymous statistics".
- No account, no sign-up, no email address collected.
- No selling, sharing, licensing, or aggregating of your data, in any form,
  de-identified or otherwise. There is no mechanism for it in the code.
- No listening when you are not holding the key. There is no wake word and no
  always-on microphone, which is why there is no permanent orange mic dot.
- No sending mail. Neo writes drafts; you press send.

If a future version ever collects anything, it will be opt-in, it will be
announced in Neo before it takes effect, and this page will say so on the day
it ships. It will not be buried here in advance as a right reserved.

## macOS permissions

Neo asks for Accessibility, Microphone, Screen Recording, Calendars, Reminders
and Contacts, each at the moment it first needs it, and each optional —
declining one switches that feature off and leaves the rest working. Every
prompt is macOS's own, and you can revoke any of them in System Settings >
Privacy & Security at any time.

Full Disk Access is never required. Granting it lets Neo learn your writing
style from Messages; not granting it costs you that one feature.

## Your data, your disk

- **See it** — say "what do you know about me", or open `memory.json`.
- **Delete some of it** — say "forget about X".
- **Delete all of it** — quit Neo and delete `memory.json`, `person.json`,
  `profile.json`, `convo.json`, `neo.log` and `.browser/`. Neo starts blank.
- **Take it with you** — they are plain JSON. Copy the folder.

There is no "request a copy" process because there is nothing held anywhere
for you to request.

## Data already sent to a provider

Once a request has gone to Google or Anthropic, it is subject to their
retention and deletion policies, not this one. Neo cannot reach into a provider
and delete it for you. Use their own controls:
[Google](https://support.google.com/gemini) ·
[Anthropic](https://privacy.anthropic.com).

## Age

Neo is not intended for children under 13, and nothing here is collected from
anyone at any age.

## Changes

This page changes when Neo's behaviour changes, in the same commit. The date at
the top moves. The repository history shows exactly what changed and when.
