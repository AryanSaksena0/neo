"""
claude_bridge.py — Neo hands real work to Claude Code, on your Max plan.

"Ask Claude to add dark mode to myapp" -> Neo runs the Claude Code CLI
headlessly in that project's folder. Claude edits files, runs what it needs,
finishes; Neo raises a card and tells you what happened. No API key, no
per-token billing — the CLI signs in with your Claude subscription
(run `claude` once in a terminal and log in; that's the whole setup).

This is the RIGHT way to give Neo builder-hands: puppeting the Claude app's
GUI would be brittle and would fight you for the mouse. The CLI is built for
exactly this.

Projects come from .env:
    NEO_PROJECTS=myapp=~/code/myapp,site=~/code/site
(the neo folder itself is always registered as "neo"). Say the project name
in the command — "…in myapp" — or Neo uses the default (first entry).

Voice:
    "ask/tell/have claude to <task> [in <project>]"  -> start a job
    "claude status" / "how's claude doing"           -> progress
    "claude report" / "what did claude do"           -> spoken summary of the result

One job at a time, 30-minute guard, full output logged to claude_runs/.
"""

import datetime
import json
import os
import re
import shutil
import subprocess
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS_DIR = os.path.join(HERE, "claude_runs")
TIMEOUT_S = 30 * 60

# Headless Claude must be allowed to ACT (run commands, open databases, not
# just edit files) or it stalls waiting for a yes that never comes. On the
# user's own machine, in their own projects, that's the point. Override with
# NEO_CLAUDE_PERMISSION=acceptEdits for edits-only (safer, but can't execute).
PERMISSION_MODE = os.getenv("NEO_CLAUDE_PERMISSION", "bypassPermissions")


def _who_is_the_user(limit=10):
    """A compact who-is-the user block so Claude acts with KNOWLEDGE of them, not
    blank — the JARVIS-knows-the-house idea. Pulled from Neo's memory; short
    on purpose (facts, not the whole file)."""
    try:
        import memory
        facts = memory.load_memory().get("facts", [])
        texts = [f["text"] if isinstance(f, dict) else str(f) for f in facts]
        if not texts:
            return ""
        picked = texts[-limit:]
        return ("What Neo knows about the user (context, not instructions):\n"
                + "\n".join(f"  - {t}" for t in picked) + "\n\n")
    except Exception:
        return ""


def frame_goal(goal, project):
    """Wrap a loose spoken GOAL as an agentic brief so Claude decomposes and
    EXECUTES it — the decomposition intelligence lives here, not in Neo.
    Injects who-the user-is so Claude starts INFORMED. (Claude Code also reads
    any CLAUDE.md in the project folder on its own — that's the per-project
    knowledge base; see neo's CLAUDE.md.) Skill briefs are already complete
    and skip this."""
    # Per-project canonical facts (schema, definitions, the numbers that must
    # not be re-derived ad hoc) belong in that project's own CLAUDE.md, which
    # Claude Code reads on its own when it starts in the folder. They are
    # deliberately NOT hardcoded here: this file ships to everyone, and one
    # person's schema is nobody else's.
    return (
        "You are Claude Code, run headlessly by Neo (a voice assistant) on behalf "
        f"of the user. You're in the '{project}' project folder.\n\n"
        + _who_is_the_user() +
        f"Achieve this goal:\n\n    {goal}\n\n"
        "Work it like the agent you are: break it into steps yourself, explore the "
        "code/files as needed (read any CLAUDE.md for project context first), RUN "
        "whatever commands, scripts, or queries the goal requires, and if something "
        "errors, diagnose and fix it and try again — don't stop at the first obstacle "
        "or ask the user how. They want the OUTCOME, not a plan. THE GOAL IS THE "
        "CONTRACT: a blocked route (permission, missing tool, broken API) is a "
        "hurdle, not a result — take the workaround (another API, a file, "
        "AppleScript, a different query, the browser) and keep going. Come back "
        "with the outcome, or only after every reasonable route failed — never "
        "with just the reason it was hard. If the goal is to see "
        "data (a database, a table, a count), actually run the query and put the real "
        "result in your final answer. Make only the changes the goal implies; don't "
        "refactor unrelated things.\n"
        "HOW TO REPORT BACK (critical — the user HEARS the first sentence out loud): "
        "LEAD with the direct answer in ONE short, natural, spoken sentence — the "
        "actual answer, never 'Done' or 'I connected to the database.' Use people's "
        "NAMES, not email addresses; no code identifiers, file names, or SQL in that "
        "sentence. Then at most one more short caveat sentence if truly needed. Do "
        "NOT narrate your process (pip installs, which column, how you queried) "
        "unless they asked how — they want the answer, not the receipt. No markdown.")

# Where the claude CLI usually lives when it's not on PATH for GUI apps.
_CLI_CANDIDATES = (
    "claude",
    os.path.expanduser("~/.claude/local/claude"),
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
    os.path.expanduser("~/.local/bin/claude"),
)


def find_cli():
    """The Claude CLI, or the string "codex" when Claude isn't signed in but
    OpenAI's Codex is (heavy.py). None when neither is — the heavy rung is
    then honestly absent."""
    try:
        import heavy
        eng = heavy.engine()
    except Exception:
        eng = None
    if eng == "codex":
        return "codex"
    for c in _CLI_CANDIDATES:
        p = shutil.which(c) or (c if os.path.isfile(c) and os.access(c, os.X_OK) else None)
        if p:
            return p
    return None


# Folders scanned for git repos so "in <project>" just works without config.
_SCAN_DIRS = ("~/Desktop", "~/code", "~/Projects", "~/Documents/GitHub", "~/dev")


def _discover(scan_dirs=_SCAN_DIRS, cap=12):
    """Auto-register git repos sitting directly inside common project folders."""
    found = {}
    for base in scan_dirs:
        base = os.path.expanduser(base)
        if not os.path.isdir(base):
            continue
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            continue
        for name in entries:
            path = os.path.join(base, name)
            if os.path.isdir(os.path.join(path, ".git")):
                found.setdefault(name.lower(), path)
                if len(found) >= cap:
                    return found
    return found


def projects():
    """{"name": abs_path}: neo always; auto-discovered git repos; NEO_PROJECTS
    entries override everything (explicit beats guessed)."""
    out = {"neo": HERE}
    out.update(_discover())
    out["neo"] = HERE                     # never let a scan shadow the home repo
    raw = os.getenv("NEO_PROJECTS", "")
    for pair in raw.split(","):
        if "=" in pair:
            name, path = pair.split("=", 1)
            name, path = name.strip().lower(), os.path.expanduser(path.strip())
            if name and os.path.isdir(path):
                out[name] = path
    return out


def aliases(env=None):
    """{"what speech hears": "the real project name"}, from NEO_PROJECT_ALIASES.

    Speech-to-text mangles project names that aren't dictionary words, and it
    mangles them the same way every time. Rather than hardcode one person's
    product here, this maps the manglings you actually hear to the folder you
    named:

        NEO_PROJECT_ALIASES=my app=myapp,side project=sideproj

    Longest heard-phrase first, so "my app pro" can't be eaten by "my app".
    Pure given `env`.
    """
    env = os.environ if env is None else env
    out = {}
    for pair in (env.get("NEO_PROJECT_ALIASES") or "").split(","):
        if "=" not in pair:
            continue
        heard, real = pair.split("=", 1)
        heard, real = heard.strip().lower(), real.strip().lower()
        if heard and real:
            out[heard] = real
    return dict(sorted(out.items(), key=lambda kv: -len(kv[0])))


# --------------------------------------------------------------------------- #
# intent parsing (pure)
# --------------------------------------------------------------------------- #
def _norm(text):
    return re.sub(r"\s+", " ", text.strip())


def parse_task(text, known=None):
    """
    "ask claude to add dark mode in myapp" -> ("add dark mode", "myapp")
    Project defaults to None (caller picks). Returns None if not a claude task.
    """
    low = _norm(text.lower())
    m = re.search(r"\b(?:ask|tell|have|get)\s+claude\s+to\s+(.+)$", low)
    if not m:
        m = re.search(r"\bhave\s+claude\s+(.+)$", low)
    if not m:
        return None
    task = m.group(1).strip().rstrip(".")
    project = None
    names = sorted((known if known is not None else projects()).keys(), key=len, reverse=True)
    for name in names:
        tail = re.search(r"\b(?:in|on|for)\s+(?:the\s+)?" + re.escape(name) + r"\s*$", task)
        if tail:
            project = name
            task = task[:tail.start()].strip().rstrip(",")
            break
    return (task, project) if task else None


def detect_project(text, known=None):
    """Which registered project does this goal mention? Handles Whisper's endless
    manglings of an unusual project name. NEO_PROJECT_ALIASES maps what speech
    recognition actually hears to the folder you named:
        NEO_PROJECT_ALIASES=my app=myapp,side project=sideproj
    A base name also matches its real folder ('myapp' -> 'myapp-web'). None if
    unmatched."""
    low = text.lower()
    for heard, real in aliases().items():
        low = low.replace(heard, real)
    low = " " + re.sub(r"[^a-z0-9 ]", " ", low) + " "
    projs = known if known is not None else projects()
    names = sorted(projs.keys(), key=len, reverse=True)
    for name in names:                        # exact segment match
        if f" {name} " in low:
            return name
    for name in names:                        # base of a hyphenated/underscored name
        base = re.split(r"[-_ ]", name)[0]
        if len(base) >= 4 and f" {base}" in low:
            return name
    return None


def _claudeish(low):
    """Whisper often hears 'claude' as 'cloud' (and 'clawed'/'clod'). For the
    Neo-command matchers below, treat those the same — 'cloud report' means
    'claude report', never Amazon."""
    return re.sub(r"\b(cloud|clawed|clod|clyde)\b", "claude", low)


def wants_status(text):
    low = _claudeish(_norm(text.lower()).replace("'", ""))
    return bool(re.search(r"\bclaude\b", low)) and any(p in low for p in (
        "status", "hows claude", "how is claude", "still working",
        "done yet", "progress"))


def wants_cancel(text):
    low = _claudeish(_norm(text.lower()).replace("'", ""))
    verbs = ("cancel", "stop", "kill", "abort")
    targets = ("claude", "the job", "that job", "the build", "the task")
    return any(v in low for v in verbs) and any(t in low for t in targets)


def wants_report(text):
    low = _claudeish(_norm(text.lower()).replace("'", ""))
    return bool(re.search(r"\bclaude\b", low)) and any(p in low for p in (
        "report", "what did claude", "whats claude done", "what has claude done",
        "results", "finish", "finished"))


# --------------------------------------------------------------------------- #
# the job
# --------------------------------------------------------------------------- #
class ClaudeBridge:
    """
    One background Claude Code job at a time.
    on_done(insight_dict) fires from the job thread when it finishes —
    neo.py routes it to the notification card + speech, same as the sentinel.
    """

    def __init__(self, on_done):
        self.on_done = on_done
        # Set by neo.py once the brain exists. Only used to shorten card
        # titles, so everything still works when it is None.
        self.client = None
        self.title_model = ""
        self.on_progress = None  # optional: called with a spoken check-in mid-job
        self.on_step = None      # optional: called with each live step label (HUD)
        self.on_start = None     # optional: (task, project) when a job ACTUALLY starts
                                 # -> the HUD task bar, no matter which path started it
        self._lock = threading.Lock()
        self._thread = None
        self._checkin = None
        self._proc = None
        self._cancelled = False
        self.current = None      # {"task","project","path","started"}
        self.last_artifacts = load_artifacts()   # survives a restart; see below
        self.last = None         # {"task","project","ok","summary","log","ended"}

    # ---- starting ------------------------------------------------------------
    def start(self, task, project=None, framed=True):
        """Start a job. `framed` wraps a loose spoken GOAL into an agentic
        brief (Claude decomposes + executes); pass framed=False for tasks that
        are ALREADY complete briefs (skill build/repair/improve)."""
        cli = find_cli()
        if not cli:
            return ("I don't have a heavy engine connected yet. Say 'connect claude' "
                    "for Claude Code, or 'connect chatgpt' for Codex, and I'll take it from there.")
        projs = projects()
        if project is None:
            project = next(iter(projs))
        if project not in projs:
            return (f"I don't know a project called {project}. "
                    "Add it to NEO underscore PROJECTS in the env file.")
        from commands import short_title
        # FIVE OR SIX WORDS. task_label only trimmed, so a long spoken request
        # became two lines of card cut mid-phrase — "Run draft simulations for
        # a six-person, point-per-reception (PPR)…" — which overflowed and
        # still did not say which job it was. Claude gets the full transcript
        # either way; this is only what the user reads.
        spoken = short_title(task, client=self.client, model=self.title_model)
        run_task = frame_goal(task, project) if framed else task
        with self._lock:
            if self.current is not None:
                return (f"Claude's already mid-job: {self.current['task']}. "
                        "One thing at a time — ask me for claude status.")
            self.current = {"task": spoken, "project": project, "path": projs[project],
                            "started": datetime.datetime.now(), "run_task": run_task}
        if self.on_start is not None:
            try:
                self.on_start(spoken, project)
            except Exception as e:
                print(f"[claude] on_start hook: {e}")
        self._thread = threading.Thread(target=self._run, args=(cli,), daemon=True,
                                        name="neo-claude")
        self._thread.start()
        return (f"On it — Claude's working on that in {project}. It'll figure out the "
                "steps and I'll tell you what it finds. Give it a bit.")

    def start_skill(self, want, client=None, model=""):
        """Learn a new ability, and DON'T stop until it demonstrably works.

        This occupies the job slot for the whole loop, because the loop runs
        Claude more than once: write, load, run it on the sentence that asked
        for it, judge the result, hand the evidence back, write again. See
        forge.py — the old path stopped at "Claude said it was done", which is
        how a skill that passes its own self_test and does nothing ended up
        live.
        """
        cli = find_cli()
        if not cli:
            return ("I don't have a heavy engine connected yet. Say 'connect claude' "
                    "or 'connect chatgpt' and I can build it.")
        projs = projects()
        project = "neo" if "neo" in projs else next(iter(projs), None)
        if not project:
            return "I don't have a project folder to build in."
        from commands import short_title
        with self._lock:
            if self.current is not None:
                return (f"Claude's already mid-job: {self.current['task']}. "
                        "One thing at a time.")
            self.current = {"task": short_title(f"learning to {want}",
                                                client=self.client,
                                                model=self.title_model),
                            "project": project, "path": projs[project],
                            "started": datetime.datetime.now(),
                            "run_task": want, "kind": "skill"}
        if self.on_start is not None:
            try:
                self.on_start(self.current["task"], project)
            except Exception as e:
                print(f"[claude] on_start hook: {e}")
        self._thread = threading.Thread(
            target=self._run_forge, args=(cli, want, client, model),
            daemon=True, name="neo-forge")
        self._thread.start()
        return ("Right — I'll build it, try it on exactly what you asked, and "
                "only keep it if it works. Give it a few minutes.")

    def _run_forge(self, cli, want, client, model):
        import forge
        with self._lock:
            job = dict(self.current)
        self._cancelled = False

        def run_claude(brief):
            ok, out, _rc = self._run_once(cli, job, brief)
            return ok, out

        def progress(msg):
            if self.on_progress is not None:
                try:
                    self.on_progress(msg)
                except Exception:
                    pass

        try:
            result = forge.forge(want, run_claude, client=client, model=model,
                                 log=print, on_progress=progress,
                                 should_stop=lambda: self._cancelled)
        except Exception as e:
            print(f"[forge] blew up: {type(e).__name__}: {e}")
            result = {"ok": False, "why": f"the build crashed ({type(e).__name__})",
                      "attempts": 0}

        spoken = forge.spoken_result(want, result)
        ended = datetime.datetime.now()
        log_path = self._save_log(job, json.dumps(result, indent=1, default=str))
        with self._lock:
            self.last = {"task": job["task"], "project": job["project"],
                         "ok": result.get("ok", False), "summary": spoken,
                         "log": log_path, "ended": ended.isoformat()}
            self.current = None
        if self._cancelled:
            return
        if result.get("ok"):
            try:
                import skills as _sk
                _sk.load_all()
            except Exception as e:
                print(f"[forge] couldn't reload skills: {e}")
        mins = max(1, int((ended - job["started"]).total_seconds() // 60))
        took = f" ({mins} min)" if mins > 1 else ""
        title = (f"Learned {result.get('name') or 'a new skill'}{took}"
                 if result.get("ok") else f"Couldn't learn that{took}")
        try:
            self.on_done({"key": f"skill:{ended.timestamp():.0f}", "kind": "claude",
                          "urgency": "medium" if result.get("ok") else "high",
                          "title": title, "detail": spoken, "task": job["task"],
                          "ok": result.get("ok", False), "summary": spoken})
        except Exception as e:
            print(f"[claude] on_done failed: {e}")

    def cancel(self):
        """Kill the running job on their order. Returns spoken confirmation."""
        with self._lock:
            cur, proc = self.current, self._proc
        if cur is None:
            return "Nothing's running to cancel."
        self._cancelled = True
        try:
            if proc is not None:
                proc.terminate()
        except Exception as e:
            print(f"[claude] cancel: {e}")
        return f"Killing it. {cur['task']} is cancelled."

    # ---- asking --------------------------------------------------------------
    def status(self):
        with self._lock:
            cur, last = self.current, self.last
        if cur:
            mins = int((datetime.datetime.now() - cur["started"]).total_seconds() // 60)
            when = "just started" if mins < 1 else f"{mins} minute{'s' if mins != 1 else ''} in"
            return f"Claude's still working on it — {cur['task']}, {when}."
        if last:
            return (f"Nothing running. Last job {'finished' if last['ok'] else 'failed'}: "
                    f"{last['task']}.")
        return "Claude's idle. Give me something — say, ask claude to fix the login bug."

    def report(self):
        with self._lock:
            cur, last = self.current, self.last
        if cur and not last:
            return self.status()
        if not last:
            return "No Claude runs yet."
        head = "Here's what Claude did" if last["ok"] else "That run failed"
        return f"{head}: {last['summary']}"

    # ---- the run -------------------------------------------------------------
    def _run_once(self, cli, job, run_task):
        """One Claude Code invocation. STREAMS Claude's steps live to on_step
        (so the HUD shows 'Reading db.py -> Running query -> ...'), and returns
        (ok, output, returncode). Falls back to plain capture if the streaming
        flags aren't supported. NEO_CLAUDE_STREAM=0 forces plain."""
        if cli == "codex":
            return self._run_codex(job, run_task)
        if os.getenv("NEO_CLAUDE_STREAM") == "0":
            return self._run_plain(cli, job, run_task)
        import json as _json
        import time as _t
        rc, result_text, raw = -1, "", []
        got_events = False
        try:
            self._proc = subprocess.Popen(
                [cli, "-p", run_task, "--output-format", "stream-json", "--verbose",
                 "--permission-mode", PERMISSION_MODE],
                cwd=job["path"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, env={**os.environ, "CI": "1"})
            t0 = _t.time()
            for line in self._proc.stdout:
                if self._cancelled or _t.time() - t0 > TIMEOUT_S:
                    self._proc.kill(); break
                line = line.strip()
                if not line:
                    continue
                raw.append(line)
                try:
                    ev = _json.loads(line)
                except ValueError:
                    continue
                got_events = True
                step = label_step(ev)
                if step and self.on_step:
                    try:
                        self.on_step(step)
                    except Exception:
                        pass
                if ev.get("type") == "result":
                    result_text = ev.get("result", "") or result_text
            self._proc.wait(timeout=60)
            rc = self._proc.returncode if self._proc.returncode is not None else -1
        except Exception as e:
            raw.append(f"[stream error] {e}")
        finally:
            self._proc = None
        # if the streaming flags weren't supported (no JSON events at all),
        # fall back to the plain, known-good path so a job never silently dies
        if not got_events:
            return self._run_plain(cli, job, run_task)
        out = result_text or "\n".join(raw)[-2000:]
        return (rc == 0 and not self._cancelled), out, rc

    def _run_codex(self, job, run_task):
        """The same job on OpenAI's Codex CLI (heavy.py) when Claude isn't
        signed in. Steps stream to the card as Codex prints them."""
        import heavy

        def line(l):
            if self.on_step and l and len(l) < 120 and not l.startswith(("[", "{")):
                try:
                    self.on_step(l[:70])
                except Exception:
                    pass
        try:
            ok, out = heavy.run_codex(run_task, job["path"], timeout_s=TIMEOUT_S, on_line=line)
        except Exception as e:
            return False, f"Couldn't run Codex: {e}", -1
        return (ok and not self._cancelled), out, (0 if ok else 1)

    def _run_plain(self, cli, job, run_task):
        """Non-streaming fallback: capture everything at the end."""
        rc, out = -1, ""
        try:
            self._proc = subprocess.Popen(
                [cli, "-p", run_task, "--permission-mode", PERMISSION_MODE],
                cwd=job["path"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env={**os.environ, "CI": "1"})
            stdout, stderr = self._proc.communicate(timeout=TIMEOUT_S)
            out = (stdout or "") + (("\n[stderr]\n" + stderr) if stderr else "")
            rc = self._proc.returncode
        except subprocess.TimeoutExpired:
            try:
                self._proc.kill()
            except Exception:
                pass
            out = "Timed out after 30 minutes."
        except Exception as e:
            out = f"Couldn't run Claude Code: {e}"
        finally:
            self._proc = None
        return (rc == 0 and not self._cancelled), out, rc

    def _checkin_fire(self, job):
        """Spoken 'still working' update if the job runs long (see banter.py)."""
        with self._lock:
            still = self.current is not None and self.current["started"] == job["started"]
        if still and self.on_progress is not None:
            try:
                import banter
                mins = int((datetime.datetime.now() - job["started"]).total_seconds() // 60)
                self.on_progress(banter.progress_line(job["task"], max(1, mins)))
            except Exception as e:
                print(f"[claude] check-in failed: {e}")

    def _run(self, cli):
        with self._lock:
            job = dict(self.current)
        # one honest check-in at 8 minutes so their never sitting clueless
        self._checkin = threading.Timer(8 * 60, self._checkin_fire, args=(job,))
        self._checkin.daemon = True
        self._checkin.start()
        self._cancelled = False
        run_task = job.get("run_task", job["task"])
        ok, out = False, ""
        # SELF-CORRECTION: up to 2 attempts — on a hard failure, retry once with
        # the error fed back so Claude takes a different approach.
        for attempt in range(2):
            ok, out, rc = self._run_once(cli, job, run_task)
            if not should_retry(rc, self._cancelled, attempt > 0, out):
                break
            if rc == 0:
                print(f"[claude] it declined rather than failed — "
                      f"pushing back: {(out or '')[:90]!r}")
            if self.on_progress is not None:
                try:
                    self.on_progress("First pass hit a wall — I'm having Claude "
                                     "try a different approach.")
                except Exception:
                    pass
            run_task = retry_task(run_task, out)

        if self._checkin is not None:
            self._checkin.cancel()
        log_path = self._save_log(job, out)
        summary = ("Cancelled on your order." if self._cancelled
                   else _summarize(out, ok))
        ended = datetime.datetime.now()
        with self._lock:
            self.last = {"task": job["task"], "project": job["project"], "ok": ok,
                         "summary": summary, "log": log_path, "ended": ended.isoformat()}
            self.current = None

        # How long it took belongs ON the card. A long build is minutes of
        # the user doing something else, and "Claude finished" with no duration
        # gives them nothing to calibrate the next estimate against. This was
        # computed and then dropped on the floor.
        mins = max(1, int((ended - job["started"]).total_seconds() // 60))
        took = f" ({mins} min)" if mins > 1 else ""
        title = (f"Claude finished{took}: {job['task']}" if ok   # task is already a
                 else f"Claude hit a wall{took}: {job['task']}")  # clean ≤70-char label
        if self._cancelled:
            return   # they ordered the kill; no card needed
        # `detail` is the FALLBACK spoken line; neo.py polishes the full
        # summary into plain spoken English (outcome + substance, one go)
        # before speaking, and only falls back to this if that fails.
        try:
            self.on_done({"key": f"claude:{ended.timestamp():.0f}", "kind": "claude",
                          "urgency": "medium" if ok else "high",
                          "title": title, "detail": spoken_result(ok, summary),
                          "task": job["task"], "ok": ok, "summary": summary})
        except Exception as e:
            print(f"[claude] on_done failed: {e}")

    def _save_log(self, job, out):
        try:
            os.makedirs(RUNS_DIR, exist_ok=True)
            stamp = job["started"].strftime("%Y%m%d-%H%M%S")
            path = os.path.join(RUNS_DIR, f"{stamp}-{job['project']}.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"task: {job['task']}\nproject: {job['project']} ({job['path']})\n"
                        f"started: {job['started'].isoformat()}\n\n{out}")
            return path
        except OSError:
            return ""


def _basename(p):
    return os.path.basename(str(p)) or str(p)


def _bash_label(cmd):
    """Raw shell on the HUD reads like garbage ('cat > /tmp/last5.mjs <<EOF
    import { neon }...'). Say what the command MEANS instead."""
    one = re.sub(r"\s+", " ", cmd)
    low = one.lower()
    m = re.match(r"cat\s*>\s*(\S+)\s*<<", one)          # heredoc file write
    if m:
        return "Writing " + _basename(m.group(1))
    if low.startswith(("grep", "rg ")) or " | grep" in low:
        return "Searching the code"
    if re.match(r"(node|python3?|bun|deno)\b", low):
        m = re.search(r"(?:node|python3?|bun|deno)\s+(?:run\s+)?([\w./-]+\.\w+)", one)
        return "Running " + (_basename(m.group(1)) if m else "a script")
    if low.startswith(("psql", "sqlite3")) or "select " in low:
        return "Querying the database"
    if low.startswith(("npm i", "npm install", "pip install", "brew install")):
        return "Installing packages"
    if low.startswith("git "):
        return "Checking the repo"
    if low.startswith(("ls", "find ", "tree", "pwd", "cat ", "head ", "tail ", "wc ")):
        return "Looking through the files"
    if low.startswith(("curl", "wget")):
        return "Fetching from the web"
    if low.startswith(("mkdir", "mv ", "cp ", "touch")):
        return "Organizing files"
    word = one.split(" ", 1)[0]
    return "Running " + (word[:36] if word else "a command")


def label_step(event):
    """Turn one Claude Code stream-json event into a short human step label
    ('Running: psql ...', 'Reading db.py', 'Editing signups.py'), or None if
    the event isn't a step worth showing. Pure — tested without a real run."""
    try:
        if event.get("type") != "assistant":
            return None
        for block in event.get("message", {}).get("content", []):
            if block.get("type") != "tool_use":
                continue
            name = block.get("name", "")
            inp = block.get("input", {}) or {}
            if name == "Bash":
                return _bash_label((inp.get("command", "") or "").strip())
            if name == "Read":
                return "Reading " + _basename(inp.get("file_path", "a file"))
            if name in ("Edit", "Write", "NotebookEdit"):
                return "Editing " + _basename(inp.get("file_path", "a file"))
            if name == "Grep":
                return "Searching for " + str(inp.get("pattern", ""))[:34]
            if name == "Glob":
                return "Finding files " + str(inp.get("pattern", ""))[:34]
            if name in ("WebFetch", "WebSearch"):
                # SAY WHAT IT IS LOOKING FOR. Every web step used to render as
                # the identical string "Looking online", so a job that searched
                # four times showed four identical rows and looked stuck rather
                # than busy. The query is the only thing that makes them
                # distinguishable, and it is the only interesting part anyway.
                what = (inp.get("query") or inp.get("prompt")
                        or inp.get("url") or "").strip()
                what = re.sub(r"^https?://(www\.)?", "", what).split("/")[0] \
                    if what.startswith("http") else what
                return f"Looking up {what[:38]}" if what else "Looking online"
            if name == "TodoWrite":
                return "Planning the steps"
            if name == "Task":
                return "Handing part of it to a helper"
            # An unrecognised tool is an INTERNAL name, and showing it leaks
            # plumbing onto their screen — "ToolSearch" appeared on a card
            # during a fantasy football job and means nothing to them.
            return "Working on it"
    except Exception:
        return None
    return None


# Claude Code signs in with OAuth and that token EXPIRES. When it has, every
# run fails in about five seconds with this on stdout — which reads, from the
# outside, exactly like a task Claude tried and could not do. The first real
# forge run burned three attempts on it and told the user it couldn't build the
# skill, which was true and completely unhelpful. Name the actual problem.
_AUTH_SIGNS = ("oauth access token has expired", "failed to authenticate",
               "please run /login", "invalid api key", "401")


def auth_problem(output):
    """Is this output a sign-in failure rather than a failed job?"""
    low = (output or "").lower()[:600]
    if "authenticate" in low or "oauth" in low or "/login" in low:
        return True
    return "401" in low and ("api error" in low or "unauthor" in low)


NEEDS_LOGIN = ("Claude Code is signed out — its login expired. Open a terminal "
               "and run claude, sign in, and ask me again.")


# Files a job says it wrote. Claude reports them in prose — "I wrote the full
# round-by-round plan to `2026_ppr_draft_strategy.md` in the neo folder" — and
# for a long time nothing did anything with that. the user asked for a document,
# got a document, and then had Neo read a summary of it aloud instead of
# opening it. When they said "just show me the document", Neo took a screenshot.
_FILE_RE = re.compile(
    r"[`'\"]?([\w./\-]+\.(?:md|txt|csv|json|pdf|html|py|ts|tsx|js|docx|xlsx|"
    r"pptx|png|jpg|svg))[`'\"]?")
# Files that are Neo's own plumbing, not something the user asked for.
_NOT_AN_ARTIFACT = {"claude.md", "readme.md", "neo.md", "package.json",
                    "requirements.txt", "tsconfig.json", ".gitignore"}


ARTIFACTS_PATH = os.path.join(HERE, "artifacts.json")


def load_artifacts():
    """The files the last finished job wrote, from disk.

    IN MEMORY IS NOT ENOUGH. Neo hot-reloads whenever a source file changes, so
    a job that finished at 02:39 had its file list wiped by a restart at 02:46 —
    and when the user asked to see the document seven minutes later, Neo said there
    wasn't one. The document was sitting right there.
    """
    try:
        with open(ARTIFACTS_PATH, encoding="utf-8") as f:
            paths = json.load(f)
    except Exception:
        return []
    return [p for p in paths if isinstance(p, str) and os.path.isfile(p)][:5]


def save_artifacts(paths):
    try:
        with open(ARTIFACTS_PATH, "w", encoding="utf-8") as f:
            json.dump(list(paths)[:5], f)
    except OSError as e:
        print(f"[claude] couldn't remember the files: {e}")


def artifacts(summary, project_path, limit=3):
    """Paths a job's report says it wrote, that actually exist on disk.

    Prose only — no parsing of tool events — because the report is the one
    place Claude reliably names what it produced. Anything that does not exist
    is dropped, so a file it merely talked about never gets opened.
    """
    if not summary or not project_path:
        return []
    out = []
    for m in _FILE_RE.finditer(str(summary)):
        name = m.group(1)
        if os.path.basename(name).lower() in _NOT_AN_ARTIFACT:
            continue
        for cand in (name if os.path.isabs(name)
                     else os.path.join(project_path, name),
                     os.path.join(project_path, os.path.basename(name))):
            if os.path.isfile(cand) and cand not in out:
                out.append(cand)
                break
        if len(out) >= limit:
            break
    return out


def handoff_line(paths):
    """What Neo SAYS when a job produced a file. One sentence, no summary.

    They asked for a document. Reading the document out loud is not delivering
    it — it is the opposite, and it was happening in the local voice while a
    live session was closed, so it also arrived as a different person.
    """
    if not paths:
        return ""
    name = os.path.basename(paths[0])
    stem = os.path.splitext(name)[0].replace("_", " ").replace("-", " ").strip()
    if len(paths) > 1:
        return f"Done. I've opened {stem}, and there are {len(paths) - 1} more files with it."
    return f"Done — I've opened {stem} on your screen."


# A job that REFUSES exits zero. Claude says "I can't do that without access to
# the project folder", the return code is 0, and everything downstream treats it
# as a finished job — so Neo told the user it was done when nothing had been done.
# They had to say "you don't need access to the project folder" themselves and ask
# again. That is the fallback being bad: not a crash, an unchallenged no.
_REFUSAL = re.compile(
    r"\b(i (?:can'?t|cannot|am unable to|was unable to|don'?t have (?:access|"
    r"permission))|unable to (?:complete|access|proceed)|i would need|"
    r"i'?m not able to|no access to)\b", re.I)
# Words that mean it actually did something, whatever else it said.
_DID_WORK = re.compile(
    r"\b(wrote|created|updated|edited|ran|installed|fixed|added|removed|"
    r"here'?s|here is|found|the answer|results?|committed|opened)\b", re.I)


def refused(output, limit=1200):
    """Did Claude decline rather than fail? Pure, so it is testable.

    Deliberately narrow. A long report that happens to contain "I can't find
    the config" is a job that did the work and hit one wall; a short answer
    that is nothing but a refusal is a job that never started.
    """
    text = (output or "").strip()
    if not text or len(text) > limit:
        return False
    if not _REFUSAL.search(text):
        return False
    return not _DID_WORK.search(text)


def should_retry(returncode, cancelled, already_retried, output=""):
    """Retry once on a hard failure OR an unchallenged refusal."""
    if cancelled or already_retried:
        return False
    return returncode != 0 or refused(output)


def retry_task(original_run_task, failed_output):
    """Augment the brief with the failure so Claude takes a different approach."""
    tail = (failed_output or "")[-900:]
    if refused(failed_output):
        # A refusal needs a different push than a crash. It did not hit a wall;
        # it decided there was one. the user had to say "you don't need access to
        # the project folder" themselves — Neo should be saying that.
        return (original_run_task +
                "\n\n--- A PREVIOUS ATTEMPT DECLINED RATHER THAN FAILED. It "
                "said:\n" + tail +
                "\n\nThat is not an answer. You have a working directory, a "
                "shell, the web and the whole filesystem — a missing permission "
                "or a missing folder is a HURDLE, not a result. Work out what "
                "you actually need, get it another way, and DO THE JOB. If you "
                "genuinely cannot, say precisely which single thing is missing "
                "and what you tried, in one sentence — never a general 'I don't "
                "have access'. ---")
    return (original_run_task +
            "\n\n--- IMPORTANT: a previous attempt FAILED. Tail of its output below. "
            "Diagnose what went wrong and take a DIFFERENT approach this time; "
            "don't repeat the same steps. ---\n" + tail)


def _summarize(out, ok):
    """Claude -p prints its final answer; keep the last meaningful chunk (full
    detail — used for the card + 'claude report', NOT read aloud in full)."""
    text = (out or "").strip()
    if not text:
        return "It finished without saying anything." if ok else "No output came back."
    tail = text[-1400:]
    sentences = re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", tail))
    keep = " ".join(sentences[-5:]).strip()
    return keep[:700] if keep else tail[:400]


def spoken_result(ok, summary):
    """Fallback spoken line when the Gemini polish isn't available: outcome
    first, then the gist. NEVER tells the user to ask for a report — they get
    everything in one go or not at all."""
    short = _spoken_short(summary)
    return short if ok else f"That didn't work. {short}"


def _spoken_short(summary):
    """The SHORT bit Neo says out loud — Claude leads with the answer, so take
    the FIRST 1-2 sentences (the answer), not a data dump. Strips emails so the
    voice never reads them aloud."""
    s = re.sub(r"\s+", " ", summary or "").strip()
    s = re.sub(r"\(?\s*[\w.+-]+@[\w.-]+\.\w+\s*\)?", "", s)   # no emails aloud
    # code-review artifacts sound like garbage: "(neo.py:872-1114)" refs and
    # markdown bullet markers ("- The routing ladder in...") get dropped
    s = re.sub(r"\(\s*[\w./ -]+\.\w{1,5}\s*:\s*\d+(?:\s*-\s*\d+)?\s*\)", "", s)
    s = re.sub(r"(?:^|(?<=\s))[-*•]\s+", "", s)
    s = re.sub(r"\(\s*\)", "", s).strip()
    if not s:
        return "Done."
    sents = re.split(r"(?<=[.!?])\s+", s)
    out = sents[0]
    if len(out) < 100 and len(sents) > 1:
        out += " " + sents[1]
    return out[:240].strip()


if __name__ == "__main__":
    print("claude cli:", find_cli() or "NOT FOUND")
    print("projects:", projects())
