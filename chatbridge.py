"""The bridge to Ultimate Chat: post what is on a terminal to a channel.

Ultimate Mail owns the chat client -- the token in the keyring, the URL,
the wire -- and exposes it as ``ultimate-mail chat …``. This module shells
out to that command and nothing else, the same way the file browser shells
out to ``ssh``: one credential store, one client, no second copy of either.
If Ultimate Mail is not installed, ``available()`` is False and the menu
items do not appear.

What crosses over is a Markdown message: an optional note, then the
selected terminal text in a fenced block, tagged with the host it came
from so the chat can offer a shell back on it (Ultimate Mail asks
``ultimate-ssh --list-hosts`` for that).

No GTK in here. The dialog that gathers the note lives in ultimate_ssh.py.
"""

import json
import shutil
import logging
import subprocess

log = logging.getLogger("ultimate-ssh.chat")

COMMAND = "ultimate-mail"
AGENT_CHANNEL = "viktor"        # the one that answers
MAX_CHARS = 60_000              # the server caps a body at 64 KB


class BridgeError(Exception):
    pass


def available():
    return shutil.which(COMMAND) is not None


def _run(args, stdin=None, timeout=30):
    try:
        proc = subprocess.run([COMMAND, "chat"] + args, input=stdin,
                              capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise BridgeError("ultimate-mail is not installed") from e
    except subprocess.TimeoutExpired as e:
        raise BridgeError("the chat server did not answer in time") from e
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise BridgeError(err[-1] if err else f"exit {proc.returncode}")
    return proc.stdout


def channels():
    """``[{"id", "name", "kind"}, ...]`` from the server, or BridgeError."""
    out = _run(["channels", "--json"])
    try:
        rows = json.loads(out or "[]")
    except ValueError as e:
        raise BridgeError("unreadable channel list") from e
    return [{"id": c["id"], "name": c.get("name") or c["id"],
             "kind": c.get("kind") or "feed"} for c in rows
            if isinstance(c, dict) and c.get("id") and not c.get("archived")]


def compose(selection, note="", host=None, command=None):
    """The Markdown body: note, then the selection as a code block.

    Trailing whitespace on each line is dropped -- terminal selections carry
    padding to the pane's width -- and a fence longer than any run of
    backticks in the text is chosen, so the block cannot be closed early.
    """
    lines = [ln.rstrip() for ln in (selection or "").replace("\r", "").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    text = "\n".join(lines)
    if len(text) > MAX_CHARS:
        text = text[-MAX_CHARS:]
        text = "… (earlier lines dropped)\n" + text
    longest = 0
    run = 0
    for ch in text:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    fence = "`" * max(3, longest + 1)
    parts = []
    if note.strip():
        parts.append(note.strip())
    where = []
    if host:
        where.append(f"on `{host}`")
    if command:
        where.append(f"after `{command}`")
    if where:
        parts.append("From the terminal " + " ".join(where) + ":")
    parts.append(f"{fence}\n{text}\n{fence}")
    return "\n\n".join(parts)


def post(channel, body, host=None, title=None, thread_id=None):
    """Post ``body`` as Markdown. Returns the server's message dict."""
    args = ["post", channel, "--file", "-", "--kind", "markdown",
            "--tag", "ssh", "--json"]
    if host:
        args += ["--attr", f"host={host}"]
    if title:
        args += ["--title", title]
    if thread_id:
        args += ["--thread", str(thread_id)]
    out = _run(args, stdin=body)
    try:
        return json.loads(out)
    except ValueError:
        return {"raw": out.strip()}
