"""
heavy.py — the heavy engine: Claude Code, or ChatGPT (Codex CLI).

hand_to_claude is Neo's rung for real computer/code/data work. It needs an
agent CLI signed in with the person's own subscription — nothing bills
through Neo. Two are supported:

  claude   Claude Code   `claude -p …`         sign in: `claude auth login`
  codex    ChatGPT       `codex exec …`        sign in: `codex login`
                         (OpenAI's Codex CLI; a ChatGPT Plus/Pro account)

Both are DETECTED, never assumed: status() runs the CLI's own auth check.
Neither installed and signed in means the heavy rung is honestly absent and
Neo says so. Claude is recommended (it is what Neo's own skills are built
and tested with); Codex is a real alternative for people who have ChatGPT
and not Claude.

Verified on 11 Sept 2026: `claude auth status`, `codex login status`,
`codex exec --skip-git-repo-check -C <dir> -o <file> <prompt>`.
"""

import os
import shutil
import subprocess

CLAUDE_INSTALL = "npm install -g @anthropic-ai/claude-code"
CODEX_INSTALL = "brew install --cask codex"
CLAUDE_PLANS = "https://claude.ai/upgrade"
CODEX_PLANS = "https://chatgpt.com/pricing"

_CLAUDE_CANDIDATES = ("claude", os.path.expanduser("~/.claude/local/claude"),
                      "/opt/homebrew/bin/claude", "/usr/local/bin/claude",
                      os.path.expanduser("~/.local/bin/claude"))
_CODEX_CANDIDATES = ("codex", "/opt/homebrew/bin/codex", "/usr/local/bin/codex")


def _find(cands):
    for c in cands:
        p = shutil.which(c) or (c if os.path.isfile(c) and os.access(c, os.X_OK) else None)
        if p and os.path.exists(os.path.realpath(p)):     # a dangling symlink is not a CLI
            return p
    return None


def claude_cli():
    return _find(_CLAUDE_CANDIDATES)


def codex_cli():
    return _find(_CODEX_CANDIDATES)


def _run(args, timeout=20):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "CI": "1"})
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return -1, str(e)


def claude_signed_in():
    """True/False, or None when the CLI isn't there."""
    cli = claude_cli()
    if not cli:
        return None
    rc, out = _run([cli, "auth", "status"])
    # Claude Code 2.x prints JSON: {"loggedIn": true, "authMethod": "claude.ai", ...}
    try:
        import json, re
        m = re.search(r"\{.*\}", out, re.S)
        if m:
            return bool(json.loads(m.group(0)).get("loggedIn"))
    except Exception:
        pass
    low = out.lower()
    return rc == 0 and "logged in" in low and "not logged" not in low


def codex_signed_in(verify=False):
    """`codex login status` says "Logged in" even when the token has expired
    (seen 11 Sept: status fine, exec failed to refresh). verify=True runs a
    one-word job so the answer is real; it costs ~10 seconds."""
    cli = codex_cli()
    if not cli:
        return None
    rc, out = _run([cli, "login", "status"])
    low = out.lower()
    if not (rc == 0 and "logged in" in low and "not logged" not in low):
        return False
    if not verify:
        return True
    ok, out = run_codex("Reply with exactly the single word: ready", cwd="/tmp", timeout_s=90)
    return ok and "ready" in out.lower()


def status():
    """{'claude': True|False|None, 'codex': True|False|None, 'engine': 'claude'|'codex'|None}"""
    c, x = claude_signed_in(), codex_signed_in()
    engine = "claude" if c else ("codex" if x else None)
    return {"claude": c, "codex": x, "engine": engine}


def engine():
    """The engine to use right now, or None. NEO_HEAVY=codex forces Codex
    when both are signed in."""
    st = status()
    pref = (os.getenv("NEO_HEAVY") or "").lower()
    if pref == "codex" and st["codex"]:
        return "codex"
    return st["engine"]


# --------------------------------------------------------------------------- #
# connecting, out loud
# --------------------------------------------------------------------------- #
def _terminal(command):
    """Run a command in the person's Terminal, in front of them — sign-ins are
    interactive (a browser opens, they approve)."""
    script = f'tell application "Terminal"\n  activate\n  do script "{command}"\nend tell'
    subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)


def connect(which, log=print):
    """Install if missing, then sign in. Returns a spoken sentence."""
    which = (which or "").lower()
    if which in ("claude", "claude code", "anthropic"):
        cli = claude_cli()
        if not cli:
            if not shutil.which("npm"):
                return ("Claude Code needs Node. I've opened a Terminal with the install; "
                        "it takes a minute, then say 'connect claude' again.")
            _terminal(f"brew install node 2>/dev/null; {CLAUDE_INSTALL} && claude auth login")
            return ("Installing Claude Code in a Terminal window, then it will ask you to sign in "
                    "with your Claude account in the browser. Approve it there, come back, and "
                    "say 'connect claude' so I can check.")
        if claude_signed_in():
            return "Claude Code is connected."
        _terminal(f"{cli} auth login")
        return ("A Terminal window is asking you to sign in to Claude in your browser. Approve "
                "it there, then say 'connect claude' and I'll check.")
    if which in ("codex", "chatgpt", "openai", "gpt"):
        cli = codex_cli()
        if not cli:
            _terminal(f"{CODEX_INSTALL} && codex login")
            return ("Installing OpenAI's Codex in a Terminal window, then it will ask you to sign "
                    "in with your ChatGPT account. Approve it, come back, and say 'connect chatgpt'.")
        if codex_signed_in():
            return "ChatGPT is connected through Codex."
        _terminal(f"{cli} login")
        return ("A Terminal window is asking you to sign in with your ChatGPT account. Approve it "
                "in the browser, then say 'connect chatgpt' and I'll check.")
    return f"I don't know a heavy engine called {which!r}."


def test(which):
    which = (which or "").lower()
    if which.startswith("claude") or which == "anthropic":
        s = claude_signed_in()
        if s is None:
            return "Claude Code isn't installed."
        return "Claude Code: signed in and ready for heavy jobs." if s else \
               "Claude Code is installed but not signed in."
    s = codex_signed_in(verify=True)
    if s is None:
        return "Codex (ChatGPT) isn't installed."
    return "ChatGPT: Codex ran a test job and answered — ready for heavy work." if s else \
           "Codex is installed but the ChatGPT sign-in has expired or a test job failed — say 'connect chatgpt'."


# --------------------------------------------------------------------------- #
# running a job on Codex (Claude runs through claude_bridge as before)
# --------------------------------------------------------------------------- #
def run_codex(task, cwd, timeout_s=1800, on_line=None):
    """(ok, output). `codex exec` in the project folder, full-auto, the final
    message written to a file so we get the answer and not the transcript."""
    import tempfile
    cli = codex_cli()
    if not cli:
        return False, "Codex isn't installed."
    fd, last = tempfile.mkstemp(prefix="neo-codex-", suffix=".txt")
    os.close(fd)
    args = [cli, "exec", "--skip-git-repo-check", "--full-auto", "-C", cwd, "-o", last, task]
    try:
        p = subprocess.Popen(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, env={**os.environ, "CI": "1"})
        lines = []
        for line in p.stdout:
            lines.append(line)
            if on_line:
                try:
                    on_line(line.rstrip())
                except Exception:
                    pass
        p.wait(timeout=timeout_s)
        rc = p.returncode
    except Exception as e:
        return False, f"Couldn't run Codex: {e}"
    try:
        final = open(last).read().strip()
    except OSError:
        final = ""
    out = final or "".join(lines[-40:])
    return rc == 0, out
