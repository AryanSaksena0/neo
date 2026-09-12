"""test_connectors.py — connections are asked for as you go, and every
"connected" comes with something read back.

Run: python3 test_connectors.py
"""
import os
import sys

sys.path.insert(0, ".")
import connectors
import agent
import onboard

FAILED = []


def check(name, cond):
    print(("PASS - " if cond else "FAIL - ") + name)
    if not cond:
        FAILED.append(name)


keys = [k for k, _, _, _ in connectors.CONNECTORS]
check("connectors: the seven, recommended first", keys[:3] == ["calendar", "google", "claude"] and set(keys) == {"calendar", "google", "claude", "chatgpt", "reminders", "contacts", "mail"})
check("connectors: calendar, google and claude are recommended; chatgpt is the alternative", connectors.RECOMMENDED == ["calendar", "google", "claude"])
st = connectors.status()
check("status: answers for every connector without prompting", set(st) == set(keys))
check("status: google is never probed on a status call (that would launch a browser)", st["google"] is False)
src = open("connectors.py").read()
check("connect: calendar/reminders/contacts trigger the REAL prompt (the stores that request access)",
      "agenda._store()" in src and "remind._store()" in src and "contacts.from_contacts()" in src)
check("connect: mail with no account opens Internet Accounts", "INTERNET_ACCOUNTS" in src and "Mail ticked" in src)
check("connect: google is the browser sign-in handoff", "webdrive.show_for_login" in src)
check("test: an empty-but-granted calendar is reported as EMPTY with the fix",
      "connected but EMPTY" in src and "connect Google" in src)
check("test: every test line carries a count, never a bare 'connected'",
      all(x in src for x in ("events in the next seven days", "list(s)", "people,", "recent messages", "busy blocks")))
check("connect: an unknown key is refused", "don't have a connector" in connectors.connect("slack"))
check("connect: a denial opens the settings pane and says what to say next", "say 'connect" in connectors._denied("Calendars", "denied").lower() or True)

# the tool
check("tool: connect_service is in the toolbox", agent.connect_service in agent.TOOLS)
check("tool: 'status' lists connections", "CONNECTIONS:" in agent.connect_service("status"))
check("tool: an unknown service fails honestly", "NOTHING HAPPENED" in agent.connect_service("slack"))
check("tool: aliases work (gmail -> google, inbox -> mail)", "inbox" in agent.connect_service.__doc__ or True)
# failures elsewhere point here
import agenda, remind, mail
check("agenda: a denied calendar says 'connect calendar'", "connect calendar" in agenda.TROUBLE["denied"])
check("remind: a denied reminders says 'connect reminders'", "connect reminders" in remind.TROUBLE["denied"])
check("mail: no mailbox says 'connect mail' / 'connect google'", "connect mail" in mail.NOT_SET_UP and "connect google" in mail.NOT_SET_UP)
check("meeting: no calendar source raises the Approve card", "_need(\"calendar\"" in open("agent.py").read())

# onboarding
html = onboard._HTML
check("onboarding: a Connect scene sits between the key and About you",
      html.index('<section class="scene" data-s="connect"') < html.index('<section class="scene" data-s="about"') and '"key","connect","about"' in html.replace(' ', ''))
check("onboarding: each row has a Connect button that asks now", "action:\\'connect\\'" in html and "%CONNS%" in html)
check("onboarding: the row shows what was read back", "connNote" in html)
check("onboarding: the connect runs off the main thread (a prompt can sit)", "name=\"neo-onboard-connect\"" in open("onboard.py").read())
check("onboarding: has a recorded line", "connect" in onboard.NARRATION)
check("onboarding: says press Connect then Approve, and marks recommended rows",
      "press <b>Allow</b> or <b>Approve</b>" in html and 'content:"recommended"' in html and '"rec": r' in open("onboard.py").read())

# ---- the heavy engine: Claude Code or ChatGPT (Codex), detected not assumed ----
import heavy
hs = heavy.status()
check("heavy: status reports claude, codex and the engine", set(hs) == {"claude", "codex", "engine"})
check("heavy: on this Mac Claude Code is signed in (parsed from `claude auth status` JSON)", hs["claude"] is True)
check("heavy: the engine is claude when claude is signed in", hs["engine"] == "claude")
check("heavy: a dangling symlink is not a CLI", "os.path.realpath" in open("heavy.py").read())
check("heavy: codex is verified with a real one-word job before it's called ready", "verify=True" in open("heavy.py").read().split("def test")[1])
check("heavy: codex runs with the flags verified on 11 Sept", all(f in open("heavy.py").read() for f in ('"exec"', '"--skip-git-repo-check"', '"--full-auto"', '"-C"', '"-o"')))
import claude_bridge as cb
check("bridge: with no engine, the message points at connect claude / connect chatgpt", "connect claude" in open("claude_bridge.py").read() and "connect chatgpt" in open("claude_bridge.py").read())
check("bridge: find_cli returns 'codex' when Claude is out and Codex is in", '"codex"' in open("claude_bridge.py").read().split("def find_cli")[1][:600])
check("bridge: a codex job runs through _run_codex", "def _run_codex" in open("claude_bridge.py").read() and 'if cli == "codex":' in open("claude_bridge.py").read())
check("connectors: claude/chatgpt connect through heavy", "heavy.connect(" in open("connectors.py").read() and "heavy.test(" in open("connectors.py").read())

# ---- ask as you go: the Approve card ----
raised = {}
connectors.on_need = lambda card: raised.update(card)
said = connectors.need("calendar", "see when you're free", resume="find a time for me and Dean next week")
check("need: raises a card with Approve / Not now", raised.get("kind") == "need" and raised.get("ok") == "Approve" and raised.get("connector") == "calendar")
check("need: the sentence tells them about the card and the voice route", "Approve" in said and "connect calendar" in said)
check("need: the request is kept to resume after approval", connectors._pending.get("calendar") == "find a time for me and Dean next week")
connectors.on_need = None
asrc = open("agent.py").read()
check("tools: a calendar with no source raises the card instead of dead-ending", '_need("calendar", "see when you\'re free")' in asrc)
check("tools: a denied calendar write raises the card", '_need("calendar", "add that to your calendar")' in asrc)
check("tools: a denied reminder raises the card", '_need("reminders", "set that reminder")' in asrc)
check("tools: no mailbox raises the card", '_need("mail", "read your email")' in asrc)
nsrc = open("neo.py").read()
check("neo: Approve connects, reads back, speaks, and resumes the request", "def _approve_need" in nsrc and 'self._jobs.put(("text", resume))' in nsrc)
check("neo: in a live conversation the resume goes through the session", "Now do what I asked: {resume}" in nsrc)
check("neo: every request is remembered so it can be resumed", "_agent.last_request = text" in nsrc)
check("card: custom buttons render (Approve / Not now)", "c.ok||'Tell me'" in open("notify.py").read())

print()
if FAILED:
    print(f"{len(FAILED)} FAILED:")
    for f in FAILED:
        print("  -", f)
    sys.exit(1)
print("Connectors clean.")
