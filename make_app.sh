#!/bin/bash
# make_app.sh — turn Neo into a real Mac app that starts itself. No terminal.
#
# Builds ~/Applications/Neo.app and (unless you pass --no-login) a LaunchAgent
# that starts it at login and restarts it if it ever dies.
#
#   ./make_app.sh              install (run this once, then forget it exists)
#   ./make_app.sh --stop       stop Neo and stop it coming back
#   ./make_app.sh --start      start it again
#   ./make_app.sh --uninstall  remove the LaunchAgent (leaves the app)
#
# INSTALL ONCE, RUNS FOREVER. After this, Neo starts at login, comes back if it
# crashes, comes back if you kill it, and restarts itself into the new version
# whenever the code on disk changes. You should never need to start it by hand
# again — if you find yourself doing that, something here is broken.
#
# Re-run any time — it overwrites cleanly.
#
# WHY THE LAUNCHAGENT RUNS THE APP AND NOT PYTHON
# macOS attaches Microphone / Input Monitoring / Accessibility permission to the
# BINARY that asks. Point a LaunchAgent at .venv/bin/python and the permissions
# land on that python, which is a different identity from Neo.app — so you end
# up granting everything twice and wondering why the app build still can't hear
# you. Running the bundle's own executable keeps one identity: Neo.app. That is
# also why the old install_autostart.sh is retired; it did exactly this wrong.

set -e

NEO_DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/Applications/Neo.app"
LABEL="app.neo.assistant"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
MODE="${1:-}"
THROTTLE=10

# Neo drives its OWN copy of Chrome, with its own profile in .browser/. That
# Chrome is a separate process tree and pkill on neo.py never touched it, so
# "stopped" left a browser running that kept the profile alive — and rewrote
# .browser/ seconds after the folder was deleted, which is what made a
# reinstall fail with "destination path already exists and is not an empty
# directory". Matched on the profile path so only NEO's Chrome dies; the
# person's own browser and their open tabs are untouched.
stop_neo_chrome() {
  local prof="$NEO_DIR/.browser"
  local pids
  pids="$(pgrep -f -- "--user-data-dir=$prof" 2>/dev/null || true)"
  [ -n "$pids" ] && kill $pids 2>/dev/null || true
  sleep 1
  pids="$(pgrep -f -- "--user-data-dir=$prof" 2>/dev/null || true)"
  [ -n "$pids" ] && kill -9 $pids 2>/dev/null || true
  return 0
}

if [ "$MODE" = "--stop" ]; then
  launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || \
    launchctl unload "$PLIST" 2>/dev/null || true
  pkill -f "neo.py" 2>/dev/null || true
  stop_neo_chrome
  echo "Neo stopped, and it won't come back until:  ./make_app.sh --start"
  exit 0
fi

if [ "$MODE" = "--start" ]; then
  # Clear any half-loaded job first. A stale registration is why bootstrap
  # failed on 9 Sept, and the old fallback then ran `open Neo.app` — which
  # starts Neo with NOBODY WATCHING IT. Neo still believed it was managed, so
  # the next code change made it exit to "restart" and it simply never came
  # back. Eighteen minutes dead before anyone noticed the silence.
  launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$UID" "$PLIST" 2>/dev/null || \
    launchctl load -w "$PLIST" 2>/dev/null || true
  sleep 1
  if launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1; then
    echo "Neo is running again, supervised by launchd."
  else
    # Start it anyway so he has a working Neo — but say plainly that the
    # self-restart is off, because that is the part that bites later.
    open "$APP"
    echo "WARNING: the LaunchAgent would not load."
    echo "Neo is running, but NOTHING WILL RESTART IT — it will not pick up"
    echo "code changes, and if it exits it stays exited. Fix with:"
    echo "    launchctl bootout gui/$UID/$LABEL; ./make_app.sh"
  fi
  exit 0
fi

if [ "$MODE" = "--uninstall" ]; then
  launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || \
    launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Login start removed. The app is still at $APP."
  echo "Quit the running copy with:  pkill -f neo.py"
  exit 0
fi

if [ ! -x "$NEO_DIR/.venv/bin/python" ]; then
  echo "No venv at $NEO_DIR/.venv — run ./setup.sh first."
  exit 1
fi

# --------------------------------------------------------------------------- #
# The bundle
# --------------------------------------------------------------------------- #
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/MacOS/Neo" <<EOF
#!/bin/bash
# Launcher. Keeps the working directory at the repo so .env, memory.json,
# models.json and skills/ all resolve, and appends to the same log the
# terminal run uses.
cd "$NEO_DIR"
# NEO_MANAGED tells Neo something is watching it, which is what licenses it to
# exit on purpose (bad permissions, new code on disk) and trust the relaunch.
export NEO_MANAGED=1
exec "$NEO_DIR/.venv/bin/python" "$NEO_DIR/neo.py" >> "$NEO_DIR/neo.log" 2>&1
EOF
chmod +x "$APP/Contents/MacOS/Neo"

cat > "$APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>            <string>Neo</string>
  <key>CFBundleDisplayName</key>     <string>Neo</string>
  <key>CFBundleIdentifier</key>      <string>app.neo.assistant</string>
  <key>CFBundleVersion</key>         <string>3.0</string>
  <key>CFBundleShortVersionString</key> <string>3.0</string>
  <key>CFBundleExecutable</key>      <string>Neo</string>
  <key>CFBundlePackageType</key>     <string>APPL</string>
  <key>LSMinimumSystemVersion</key>  <string>13.0</string>
  <!-- No Dock icon, no menu bar. Matches setActivationPolicy_(1) in neo.py. -->
  <key>LSUIElement</key>             <true/>
  <!-- These strings are what macOS shows in the permission prompts. If a usage
       string is missing for a permission the app asks for, macOS kills the
       process instead of prompting — silently, which reads as a crash. -->
  <key>NSMicrophoneUsageDescription</key>
    <string>Neo listens while you're talking to it, and closes the mic when you stop.</string>
  <key>NSAppleEventsUsageDescription</key>
    <string>Neo opens apps and types for you when you ask it to.</string>
  <key>NSCalendarsUsageDescription</key>
    <string>Neo reads your calendar to answer questions about your day.</string>
  <key>NSCalendarsFullAccessUsageDescription</key>
    <string>Neo reads your calendar to answer questions about your day, and adds events when you ask.</string>
  <key>NSContactsUsageDescription</key>
    <string>Neo looks up people you name so it can address email to them.</string>
  <key>NSRemindersUsageDescription</key>
    <string>Neo sets reminders in Reminders when you ask it to.</string>
  <key>NSRemindersFullAccessUsageDescription</key>
    <string>Neo sets reminders in Reminders when you ask it to, and reads them back to check.</string>
  <key>NSSystemAdministrationUsageDescription</key>
    <string>Neo needs to watch the fn key so you can talk to it from anywhere.</string>
</dict>
</plist>
EOF

# A generic icon so it isn't a blank page in Login Items and the app switcher.
ICON_SRC="/System/Library/CoreServices/CoreTypes.bundle/Contents/Resources/GenericApplicationIcon.icns"
if [ -f "$ICON_SRC" ]; then
  cp "$ICON_SRC" "$APP/Contents/Resources/Neo.icns" 2>/dev/null || true
  /usr/libexec/PlistBuddy -c "Add :CFBundleIconFile string Neo" \
    "$APP/Contents/Info.plist" 2>/dev/null || true
fi

# Tell Launch Services the bundle changed, or it keeps serving a stale copy.
touch "$APP"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f "$APP" 2>/dev/null || true

echo "Built $APP"

# --------------------------------------------------------------------------- #
# Start at login
# --------------------------------------------------------------------------- #
if [ "$MODE" != "--no-login" ]; then
  # Retire the old agents: the one that pointed at python directly, and the
  # pre-release label. Either left running would fight this one for the key.
  launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || \
    launchctl unload "$PLIST" 2>/dev/null || true
  for oldplist in "$HOME"/Library/LaunchAgents/*neo*.plist; do
    [ -e "$oldplist" ] || continue
    old="$(basename "$oldplist" .plist)"
    [ "$old" = "$LABEL" ] && continue
    launchctl bootout "gui/$UID/$old" 2>/dev/null || true
    rm -f "$oldplist"
  done
  pkill -f "neo\.py" 2>/dev/null || true

  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <!-- The BUNDLE executable, not python — see the note at the top. -->
  <key>ProgramArguments</key>
  <array><string>$APP/Contents/MacOS/Neo</string></array>
  <key>WorkingDirectory</key><string>$NEO_DIR</string>
  <key>RunAtLoad</key><true/>
  <!-- UNCONDITIONAL. Neo is meant to be installed once and then always be
       there: back after a crash, back after a kill, back after a permission
       fix, back after a code change. Anything less means "restart it yourself",
       which is the thing we're removing.
       The deliberate off switch is `./make_app.sh --stop`, which unloads this
       agent — not a clean exit code, because Neo uses clean exits to relaunch
       itself into new code. -->
  <key>KeepAlive</key><true/>
  <!-- Floor on relaunch frequency, so a genuinely broken build (bad venv,
       missing permission) retries calmly instead of spinning. -->
  <key>ThrottleInterval</key><integer>10</integer>
  <!-- A process wedged in interpreter teardown never finishes exiting, so
       launchd still counts it as running and KeepAlive never fires — which is
       exactly what "I pressed fn and nothing happened" looks like. Cap it. -->
  <key>ExitTimeOut</key><integer>5</integer>
  <key>ProcessType</key><string>Interactive</string>
  <!-- Without these a crash is INVISIBLE: the traceback goes to a closed fd and
       neo.log just stops mid-line, which is indistinguishable from a hang. Three
       segfaults were diagnosed by guesswork before this existed. -->
  <key>StandardOutPath</key><string>$NEO_DIR/neo.out</string>
  <key>StandardErrorPath</key><string>$NEO_DIR/neo.err</string>
</dict>
</plist>
EOF
  # Loading has to be deterministic, and the naive version isn't: `bootstrap`
  # fails with "Input/output error" (errno 5) when the label is STILL
  # registered, which is exactly what happens on a re-run — bootout is
  # asynchronous, so firing bootstrap immediately after it races and loses.
  # Wait for the label to actually disappear, then bootstrap, then VERIFY.
  for _ in $(seq 1 20); do
    launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1 || break
    sleep 0.25
  done

  if launchctl bootstrap "gui/$UID" "$PLIST" 2>/tmp/neo-launchctl.err; then
    :
  else
    # Older macOS, or a stubborn registration: fall back and try once more.
    launchctl load -w "$PLIST" 2>>/tmp/neo-launchctl.err || true
  fi

  sleep 1
  if launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1; then
    echo "Login start installed and running ($PLIST)"
  else
    echo
    echo "!! The LaunchAgent did NOT load. Neo will not start at login yet."
    echo "   launchctl said:"
    sed 's/^/     /' /tmp/neo-launchctl.err 2>/dev/null | head -5
    echo "   Try:  launchctl bootout gui/$UID/$LABEL ; ./make_app.sh"
    echo "   Meanwhile Neo still runs if you open the app directly."
    echo
  fi
fi

# Start it now regardless, so the permission prompts appear immediately.
open "$APP" 2>/dev/null || true

cat <<EOF

Neo is running, and it will keep running. That's the last install step.

ONE remaining thing, and only because macOS won't let me do it for you.
Permissions are per-app, so what you granted Terminal does NOT carry over.
Open System Settings -> Privacy & Security and add ~/Applications/Neo.app to:

    Microphone          or it hears nothing
    Input Monitoring    or the fn tap does nothing
    Accessibility       same, plus typing on your behalf
    Screen Recording    only for "what's on my screen"

AND ONE MORE, easy to miss and it makes fn look completely broken:
    System Settings -> Keyboard -> "Press globe key to" -> Do Nothing
Otherwise macOS eats the fn tap (emoji picker / input switch) before Neo sees
it. You'd press fn, something else would happen, and Neo would look dead.

Stuck? This tells you exactly what's blocking it:
    "$NEO_DIR/.venv/bin/python" "$NEO_DIR/neo.py" --doctor

You do NOT need to restart anything after granting. Neo retries every ${THROTTLE}s
until the permission lands, then just starts working. Watch it happen:

    tail -f "$NEO_DIR/neo.log"

From here on it looks after itself:
  - starts at login
  - comes back if it crashes, or if you kill it
  - restarts into the new version whenever the code on disk changes
  - only ever one copy running, so nothing fights over the mic

The only two commands you should ever need again:

    ./make_app.sh --stop     stop it, and stop it coming back
    ./make_app.sh --start    bring it back
EOF
