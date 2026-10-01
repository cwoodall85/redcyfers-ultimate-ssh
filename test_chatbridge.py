#!/usr/bin/env python3
"""The bridge to Ultimate Chat, without a network or a window.

Checks: the message body is well-formed Markdown whatever the selection
contains, the subprocess is driven with the right arguments, and every
failure comes back as a BridgeError with the server's last line.
"""
import os
import sys
import json
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chatbridge  # noqa: E402

FAILURES = 0


def check(name, cond, detail=""):
    global FAILURES
    if cond:
        print(f"  ok   {name}")
    else:
        FAILURES += 1
        print(f"  FAIL {name}  {detail}")


def main():
    print("compose")
    body = chatbridge.compose("line one   \nline two\n\n\n", note="Why?",
                              host="web-01")
    check("note first", body.startswith("Why?"))
    check("trailing padding dropped", "line one   " not in body)
    check("trailing blank lines dropped", body.endswith("line two\n```"))
    check("host named", "on `web-01`" in body)
    tricky = chatbridge.compose("a ``` b ```` c")
    check("fence longer than any backtick run", tricky.startswith("`````\n"))
    huge = chatbridge.compose("x" * 100_000)
    check("oversize is truncated from the front",
          len(huge) < 70_000 and "earlier lines dropped" in huge)
    check("no note, no host: just the block",
          chatbridge.compose("z").strip() == "```\nz\n```")

    print("subprocess")
    calls = []

    class Proc:
        def __init__(self, rc, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err

    def fake_run(argv, input=None, capture_output=True, text=True, timeout=30):
        calls.append((argv, input))
        if argv[2] == "channels":
            return Proc(0, json.dumps([{"id": "notes", "kind": "notes"},
                                       {"id": "viktor", "kind": "agent",
                                        "name": "Viktor"},
                                       {"id": "old", "archived": True}]))
        if argv[2] == "post":
            if argv[3] == "nope":
                return Proc(1, "", "chat: forbidden\n")
            return Proc(0, json.dumps({"id": 42, "channel": argv[3]}))
        return Proc(1, "", "boom")
    subprocess.run, real = fake_run, subprocess.run
    try:
        rows = chatbridge.channels()
        check("channels parsed, archived dropped",
              [c["id"] for c in rows] == ["notes", "viktor"])
        check("channels called with --json",
              calls[-1][0][:4] == ["ultimate-mail", "chat", "channels",
                                   "--json"])
        m = chatbridge.post("viktor", "body", host="web-01", title="T")
        argv, stdin = calls[-1]
        check("post goes to stdin as markdown",
              stdin == "body" and "--file" in argv and "-" in argv
              and "markdown" in argv)
        check("host attr and tag attached",
              "host=web-01" in argv and "ssh" in argv)
        check("post returns the server's message", m["id"] == 42)
        try:
            chatbridge.post("nope", "x")
            check("a refused post raises", False)
        except chatbridge.BridgeError as e:
            check("a refused post raises with the last line",
                  str(e) == "chat: forbidden")
    finally:
        subprocess.run = real

    print("\n" + ("ALL CHECKS PASSED" if not FAILURES
                  else f"FAILURES: {FAILURES}"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
