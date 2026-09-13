"""
test_routing.py — fuzz Neo's command routing without booting Neo.

neo.py can't import off-Mac (PyObjC), so CHAIN below REPLICATES the intent
order in Neo._handle. If you change the order there, change it here — the
Claude Code test run cross-checks the two and flags drift.

Run:  python test_routing.py
"""

# Suites live in tests/ but RUN from the repo root, so that the modules
# under test import and open("neo.py") still resolves. This makes the
# import work either way, so a suite can also be run directly.
import os as _bootstrap_os, sys as _bootstrap_sys
_bootstrap_sys.path.insert(0, _bootstrap_os.path.dirname(
    _bootstrap_os.path.dirname(_bootstrap_os.path.abspath(__file__))))

import commands
import hands
import claude_bridge
import chrome
import skills

# must mirror Neo._handle top-to-bottom
_KNOWN = {"neo": ".", "the project": "."}


def _browser_search(t):
    return ("browser" in t.lower()) and bool(hands.parse_search(t))


CHAIN = [
    # Ending a conversation is checked before everything else. There is no
    # matching "start" intent — a key press does that.
    ("live_off",      commands.wants_live_off),
    ("repeat",        commands.wants_repeat),
    ("speed",         commands.wants_speed),
    ("voice",         lambda t: commands.parse_voice_switch(t) is not None),
    ("brain",         commands.wants_brain),
    ("close",         commands.wants_close),
    ("chrome",        chrome.wants_chrome),
    ("teach",         lambda t: bool(skills.parse_teach(t))),
    ("skill_list",    skills.wants_skill_list),
    ("repair",        lambda t: skills.parse_repair(t) is not None),
    ("tidy_memory",   commands.wants_tidy_memory),
    ("claude_task",   lambda t: bool(claude_bridge.parse_task(t, _KNOWN))),
    ("claude_cancel", claude_bridge.wants_cancel),
    ("claude_status", claude_bridge.wants_status),
    ("claude_report", claude_bridge.wants_report),
    ("computer_task", commands.is_computer_task),
    ("forget",        commands.wants_forget),
    ("recap",         commands.wants_recap),
    ("visual",        commands.wants_visual),
    ("screen",        hands.wants_screen),
    ("music",         commands.wants_music),
    ("type",          lambda t: hands.parse_type(t) is not None),
    ("browser_search", _browser_search),
    ("open",          lambda t: hands.parse_open(t) is not None),
    ("radar",         commands.wants_radar),
    ("lookup",        commands.wants_lookup),
    ("skill",         lambda t: skills.find(t) is not None),
    # anything else -> the agent chat
]


def route(text):
    for name, fn in CHAIN:
        try:
            if fn(text):
                return name
        except Exception as e:
            return f"CRASH:{name}:{e}"
    return "chat"


# (utterance, expected route) — realistic spoken phrasings, incl. traps.
CORPUS = [
    # hanging up: generous, because being unable to end a live mic is the
    # worst bug this feature could have
    ("that's all", "live_off"),
    ("hang up", "live_off"),
    ("we're done", "live_off"),
    # voice switching (live bug class: must beat every other matcher)
    ("switch your voice to fable", "voice"),
    ("change the voice to emma", "voice"),
    ("use the george voice", "voice"),
    # personal data goes to the BRAIN (browser hands), never web search
    # (live bug 16:46: "my Google Calendar" hit wants_lookup via "google")
    # the calendar_peek SKILL answers these instantly now (EventKit search) —
    # BOTH phrasings from the live bugs: "google calendar" and "what day"
    ("can you check my google calendar and tell me the dates im flying to india", "skill"),
    ("what day do i fly out to india and what day do i come back", "skill"),
    ("whats on my schoology for tomorrow", "chat"),
    # the todo SKILL: capture / list / complete must survive every earlier matcher
    # ("remind me to …" is NOT the skill — the brain's set_reminder takes it)
    ("remind me to call jordan at five", "chat"),
    ("add email xavier to my to-do list", "skill"),
    ("put buy the domain on my list", "skill"),
    ("whats on my to-do list", "skill"),
    ("cross off buy the domain", "skill"),
    ("google neural networks for me", "lookup"),
    # screen time is a computer task / never a screenshot (live bug 02:20)
    ("go to my screen time and show me my usage like the hours", "computer_task"),
    # things Neo has to BUILD are computer tasks, not chat — missing this is how
    # "make me a screensaver" turned into a promise with no job (live bug Aug 4)
    ("i want you to make me a screen saver for my computer", "computer_task"),
    ("build me a widget that shows the live time", "computer_task"),
    ("check my screen time and tell me the hours for today", "computer_task"),
    # built-ins
    ("show me your brain", "brain"),
    ("neo open your memory", "brain"),
    # the live bug: close must never OPEN
    ("close the marketing dashboard", "close"),
    ("i said close the dashboard", "close"),
    ("close that window", "close"),
    ("what am i looking at on my screen right now", "screen"),
    # growth
    ("teach yourself to track my gym streak", "teach"),
    ("build a skill that can time my pomodoros", "teach"),
    ("can you teach yourself how to check surf reports", "teach"),
    ("what skills do you have", "skill_list"),
    ("fix that skill", "repair"),
    ("repair your headlines skill", "repair"),
    # self-measurement + memory hygiene
    ("how fast are you", "speed"),
    ("latency report", "speed"),
    ("clean up your memory", "tidy_memory"),
    ("consolidate your memories", "tidy_memory"),
    # claude
    ("ask claude to fix the overlay bug in neo", "claude_task"),
    ("have claude clean up the css in the project", "claude_task"),
    ("ask claude to draft the email onboarding flow", "claude_task"),
    ("ask claude to find leads page bugs in the project", "claude_task"),
    ("how's claude doing", "claude_status"),
    ("claude status", "claude_status"),
    ("what did claude do", "claude_report"),
    ("cancel the claude job", "claude_cancel"),
    ("stop that job", "claude_cancel"),
    # rich computer/code/data goals -> Claude, NOT the literal open-matcher
    ("open terminal, get into the the project code and open the signups database", "computer_task"),
    ("go into the the project repo and fix the bug where signups fail", "computer_task"),
    ("run the migration script on the the project database", "computer_task"),
    ("show me what's in the signups table", "computer_task"),
    # ...but a plain app open is still just an app open
    ("open spotify", "open"),
    ("open terminal", "open"),
    # repair turns
    ("say that again", "repeat"),
    ("what did you say", "repeat"),
    ("wait what was that", "repeat"),
    # the project engine
    ("forget that", "forget"),
    ("what do you know about me", "recap"),
    # visuals
    ("visualize the the project funnel", "visual"),
    ("draw me how ambassador payouts flow", "visual"),
    ("show me a diagram of the auth system", "visual"),
    # music control (Spotify / Apple Music via AppleScript)
    ("play feel no ways by drake on spotify", "music"),
    ("play some kendrick", "music"),          # "some <artist>" is a song request
    ("pause the music", "music"),
    ("pause spotify", "music"),
    ("skip this song", "music"),
    ("next song", "music"),
    ("go back a song", "music"),
    ("what song is this", "music"),
    ("what's playing right now", "music"),
    ("turn the music up", "music"),
    ("turn spotify down", "music"),
    ("resume the music", "music"),
    ("open spotify and play my music", "music"),
    # music TRAPS — idioms and the timer skill must NOT hit music
    ("stop the timer", "skill"),
    ("play it cool", "chat"),
    ("play devils advocate with me", "chat"),
    ("i want to play along", "chat"),
    ("open spotify", "open"),                 # a bare open is still just open
    # hands
    ("what's on my screen", "screen"),
    ("type hello world", "type"),
    ("search for cheap flights to austin in the browser", "browser_search"),
    ("open safari", "open"),
    ("go to nytimes.com", "open"),
    ("open blender", "open"),
    # chrome-with-profile — the new capability
    ("open schoology in my school profile", "chrome"),
    ("google the french revolution in my school account", "chrome"),
    ("pull up gmail on my work profile", "chrome"),
    # gmail/email now route to chrome (real logged-in session > dumb open)
    ("pull up gmail", "chrome"),
    ("check my email", "chrome"),
    ("any new emails in my school account", "chrome"),
    # sentinel / lookup / skills
    ("anything on my radar", "radar"),
    ("look up the latest on openai", "lookup"),
    ("google neural networks", "lookup"),
    ("what's in the news", "skill"),
    ("set a timer for 10 minutes", "skill"),
    ("give me a 5 minute countdown", "skill"),
    # the live hijack: "on my screen" phrasing must NOT trigger screen-vision
    ("set up a timer, leave it on my screen, a counting down timer for 10 minutes", "skill"),
    ("put a 10 minute timer on my screen", "skill"),
    ("stop the timer", "skill"),
    ("how much time is left on the timer", "skill"),
    ("i wanted the timer for 10 minutes, did i not say 10 minutes", "chat"),
    # traps — these MUST fall through to plain chat
    ("i want to learn how to code someday", "chat"),
    ("go to sleep neo", "chat"),
    ("i'm going to open a bank account tomorrow", "chat"),
    ("my teacher said to draft two essays tonight", "chat"),
    ("jordan is a nice guy", "chat"),
    ("should i learn spanish or french", "chat"),
    ("what's the point of college anyway", "chat"),
    ("is 79 a year the right price", "chat"),
    ("claude is a cool name for a model", "chat"),
    ("tell me a joke", "chat"),
    ("how was your day", "chat"),
    ("my memory is terrible lately", "chat"),
    ("i need to fix my sleep schedule", "chat"),

    # THE DESK, THE FEEDS, THE HIGHLIGHTER. All of these are answered by tools
    # the brain calls, so "chat" here means "reached the brain", not "chatted".
    # The point of each one is that it does NOT get grabbed by an older
    # matcher on its way past — a live price must not become a web search, and
    # a real email must not become lead outreach.
    ("what is apple trading at", "chat"),
    ("how is the market doing today", "chat"),
    ("what is bitcoin at", "chat"),
    ("whats the score in the nba", "chat"),
    ("any premier league games on tonight", "chat"),
    ("what are the odds on a fed rate cut", "chat"),
    ("read me this pdf", "chat"),
    ("summarise the document i have open", "chat"),
    ("what did i just copy", "chat"),
    ("find my physics packet", "chat"),
    ("put lunch with priya on thursday at one", "chat"),
    ("book a dentist appointment next tuesday at three", "chat"),
    ("add that to my calendar", "chat"),
    ("highlight the important part on my screen", "chat"),
    ("show me the line about the deadline", "chat"),
    # singular, named recipient -> the brain, which can actually write it.
    ("draft an email to my teacher", "chat"),
    ("write an email to priya about friday", "chat"),
    # ...but the outreach BATCH still belongs to the leads drafter.

    # SHOWING HIM A DOCUMENT. All reach the brain, which has open_document.
    # He asked to see a finished document and Neo took a screenshot, then
    # wrote AppleScript around the filename as he had said it out loud and
    # put a macOS error dialog on his screen.
    ("just show me the document", "chat"),
    ("show me the document you made", "chat"),
    ("open the 2026 ppr draft strategy file", "chat"),
    ("where is the document", "chat"),

    # FOCUS MODE. Reaches the brain, which calls focus_mode — these must not
    # be grabbed by an older matcher on the way past.
    ("i'm working so leave me alone", "chat"),
    ("focus mode", "chat"),
    ("focus mode off", "chat"),
    ("go ahead", "chat"),
]


def main():
    skills.load_all()
    bad = []
    for text, want in CORPUS:
        got = route(text)
        if got != want:
            bad.append((text, want, got))
    for text, want, got in bad:
        print(f"MISROUTE: {text!r}  expected={want}  got={got}")
    print(f"\n{len(CORPUS) - len(bad)}/{len(CORPUS)} utterances routed correctly.")
    if bad:
        raise SystemExit(1)
    print("Routing clean.")


if __name__ == "__main__":
    main()
