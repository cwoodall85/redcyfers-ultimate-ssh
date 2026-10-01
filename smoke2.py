"""Tier 2 smoke test.

Broadcast, splits, reconnect and session persistence are driven for real.
The explorer's transport is stubbed to bash -c so the command construction and
the null-delimited parser get exercised end to end without logging in anywhere.
"""
import os
import sys
import shutil
import tempfile
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Vte", "3.91")
from gi.repository import Gtk, GLib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Sandbox the app home before importing the app: otherwise the test writes a
# session file into the real ~/.ultimate-ssh.
import sshconfig  # noqa: E402
_HOME = tempfile.mkdtemp(prefix="ultimate-ssh-test-")
sshconfig.APP_HOME = _HOME
sshconfig.HOSTS_FILE = os.path.join(_HOME, "config")
sshconfig.BACKUP_DIR = os.path.join(_HOME, "backups")
sshconfig.SESSION_FILE = os.path.join(_HOME, "session.json")

import ultimate_ssh as app

app.SESSION_FILE = sshconfig.SESSION_FILE
app.SSH_CONFIG = sshconfig.HOSTS_FILE

SCRATCH = os.environ.get("SMOKE_OUT", tempfile.gettempdir())
SHOT = f"{SCRATCH}/tier2.png"
FIXTURE = f"{SCRATCH}/remote-fixture"

FAILURES = []


class FakeHost:
    """Stands in for a HostView: an alias that cannot possibly resolve, so the
    failure path is exercised without touching any real machine."""

    def __init__(self, alias, group):
        self.alias = alias
        self.group = group
        self.hostname = alias + ".invalid"
        self.note = ""


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"   {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def build_fixture():
    shutil.rmtree(FIXTURE, ignore_errors=True)
    os.makedirs(f"{FIXTURE}/subdir", exist_ok=True)
    with open(f"{FIXTURE}/plain.txt", "w") as fh:
        fh.write("x" * 4096)
    # the names that break every `ls -l` parser ever written
    with open(f"{FIXTURE}/name with spaces.log", "w") as fh:
        fh.write("y" * 100)
    with open(f"{FIXTURE}/tricky'quote\".conf", "w") as fh:
        fh.write("z")


# A reading from an imaginary host, so the parser is checked against numbers
# whose answers are known rather than against whatever this machine happens to
# be doing at the time.
STATS_FIXTURE = """@@CPU
cpu  1000 20 300 8000 100 0 40 0 0 0
@@NCPU
4
@@LOAD
0.42 0.30 0.28 1/512 4242
@@MEM
MemTotal:       16000000 kB
MemFree:         1000000 kB
MemAvailable:    6000000 kB
Buffers:          200000 kB
Cached:          4000000 kB
SwapTotal:       2000000 kB
SwapFree:        1500000 kB
@@UP
90061.42 350000.00
@@NET
Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo:   12345     100    0    0    0     0          0         0    12345     100    0    0    0     0       0          0
  eth0: 1000000    5000    0    0    0     0          0         0   500000    2500    0    0    0     0       0          0
docker0:  777777      42    0    0    0     0          0         0    88888      21    0    0    0     0       0          0
@@DF
Filesystem     1024-blocks      Used Available Capacity Mounted on
/dev/sda1        103080000  41232000  56600000      43% /
tmpfs              8000000       100   7999900       1% /dev/shm
/dev/sdb1         52000000  49400000   2600000      95% /var/log
@@END
"""


def term_text(term):
    got = term.get_text_range_format(app.Vte.Format.TEXT, 0, 0,
                                     term.get_cursor_position()[1], 400)
    body = got[0] if isinstance(got, tuple) else got
    return body or ""


class Smoke(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="dev.ultimatessh.Smoke2")

    def do_activate(self):
        self.win = win = app.UltimateSshWindow(self)
        win.present()

        print("\n== path quoting ==")
        check("~ expands via $HOME", app.remote_path_expr("~") == '"$HOME"',
              app.remote_path_expr("~"))
        check("~/x keeps expansion", app.remote_path_expr("~/a b") == '"$HOME"/\'a b\'',
              app.remote_path_expr("~/a b"))
        check("absolute path quoted", app.remote_path_expr("/tmp/a b") == "'/tmp/a b'")

        print("\n== ssh argv ==")
        argv = app.ssh_argv("lab-web1")
        check("uses ControlMaster", "ControlMaster=auto" in argv)
        check("ControlPath uses %C hash",
              any("c-%C" in a for a in argv), " ".join(argv[-6:]))
        check("socket dir exists 0700",
              os.path.isdir(app.RUNTIME_DIR)
              and oct(os.stat(app.RUNTIME_DIR).st_mode)[-3:] == "700")

        # three local shells to broadcast between
        for _ in range(3):
            win.new_local_shell()
        GLib.timeout_add(1500, self.phase_broadcast)

    def phase_broadcast(self):
        win = self.win
        tabs = [win.notebook.get_nth_page(i) for i in range(win.notebook.get_n_pages())]
        tabs = [t for t in tabs if isinstance(t, app.TerminalTab)]
        panes = [t.panes()[0] for t in tabs]

        print("\n== broadcast ==")
        check("three shells open", len(panes) == 3, f"{len(panes)} panes")

        source = panes[0]
        win.set_active_pane(source)

        # OFF: nobody else should receive anything
        win.broadcast_mode = app.BROADCAST_OFF
        source.term.emit("commit", "echo OFFMODE\n", len("echo OFFMODE\n"))

        # ALL: emitting commit is exactly what a real keystroke does
        win.broadcast_mode = app.BROADCAST_ALL
        win._refresh_banner()
        check("banner visible when armed", win.banner.get_visible())
        payload = "echo FANOUT-$$\n"
        # A real keystroke does two things: VTE writes it to its own pty AND
        # emits commit. Emitting alone would leave the source shell untouched,
        # so feed it too or the test misreads its own artifact as a bug.
        source.term.emit("commit", payload, len(payload))
        source.term.feed_child(payload.encode())

        GLib.timeout_add(1500, self.phase_check_broadcast, panes)
        return False

    def phase_check_broadcast(self, panes):
        texts = [term_text(p.term) for p in panes]
        got = [("FANOUT-" in t and "echo FANOUT" in t) for t in texts]
        print("  followers that received the broadcast:", got[1:])
        check("followers ran the broadcast command", all(got[1:]))
        check("OFF mode did not leak to followers",
              not any("OFFMODE" in t for t in texts[1:]))
        # distinct $$ means these are genuinely separate shells, not an echo
        pids = set()
        for t in texts:
            for line in t.splitlines():
                if line.startswith("FANOUT-") and line[7:].strip().isdigit():
                    pids.add(line[7:].strip())
        check("each shell is a distinct process", len(pids) == 3, f"pids={sorted(pids)}")

        GLib.timeout_add(300, self.phase_splits)
        return False

    def phase_splits(self):
        win = self.win
        tab = win.current_tab()
        print("\n== splits ==")
        before = len(tab.panes())
        win.set_active_pane(tab.panes()[0])
        win.split(Gtk.Orientation.HORIZONTAL)
        check("split right adds a pane", len(tab.panes()) == before + 1,
              f"{before} -> {len(tab.panes())}")

        win.set_active_pane(tab.panes()[0])
        win.split(Gtk.Orientation.VERTICAL)
        check("split down adds a pane", len(tab.panes()) == before + 2,
              f"-> {len(tab.panes())}")

        deepest = tab.panes()[-1]
        win.set_active_pane(deepest)
        win.close_pane()
        check("close pane collapses the tree", len(tab.panes()) == before + 1,
              f"-> {len(tab.panes())}")

        GLib.timeout_add(500, self.phase_explorer)
        return False

    def phase_explorer(self):
        print("\n== explorer (transport stubbed to bash -c) ==")
        build_fixture()
        real = app.ssh_argv
        app.ssh_argv = lambda alias, cmd=None, **kw: (
            ["/bin/bash", "-c", cmd] if cmd else real(alias))

        def listed(resolved, entries, error):
            app.ssh_argv = real
            if error is not None:
                check("listing succeeded", False, error)
                self.finish()
                return
            names = [e.name for e in entries]
            check("listing succeeded", True, f"{len(entries)} entries")
            check("resolved path returned", resolved == FIXTURE, resolved)
            check("directories sort first", entries[0].name == "subdir", names[0])
            check("filename with spaces survives", "name with spaces.log" in names)
            check("filename with quotes survives", "tricky'quote\".conf" in names)
            sizes = {e.name: e.size for e in entries}
            check("size parsed", sizes.get("plain.txt") == 4096,
                  str(sizes.get("plain.txt")))
            kinds = {e.name: e.kind for e in entries}
            check("dir vs file distinguished",
                  kinds.get("subdir") == "d" and kinds.get("plain.txt") == "f")
            GLib.timeout_add(200, self.phase_stats)

        app.list_remote_dir("fixture-host", FIXTURE, listed)
        return False

    def phase_stats(self):
        print("\n== status bar: parsing ==")
        data = app.parse_stats(STATS_FIXTURE)
        check("cpu counters summed", data["cpu"] == (9460, 8100), str(data.get("cpu")))
        check("cpu count read", data["ncpu"] == 4, str(data.get("ncpu")))
        check("load read", data["load"] == (0.42, 0.30, 0.28), str(data.get("load")))
        check("memory used = total - available",
              data["mem"] == (16000000 * 1024, 10000000 * 1024), str(data.get("mem")))
        check("swap used", data["swap"] == (2000000 * 1024, 500000 * 1024),
              str(data.get("swap")))
        check("uptime in days and hours", app.format_uptime(data["uptime"]) == "1d 1h",
              app.format_uptime(data["uptime"]))
        check("clock keeps its fraction", data["clock"] == 90061.42,
              str(data.get("clock")))
        check("net counters read, headers skipped",
              data["net"] == {"lo": (12345, 12345), "eth0": (1000000, 500000),
                              "docker0": (777777, 88888)}, str(data.get("net")))
        check("df header skipped, rows kept", len(data["disks"]) == 3,
              str(len(data.get("disks", []))))
        check("tmpfs is not a disk",
              [d["real"] for d in data["disks"]] == [True, False, True])
        check("root chosen for the bar", app.pick_disk(data["disks"])["mount"] == "/")
        check("truncated output is refused", app.parse_stats(
            STATS_FIXTURE.replace("@@END", "")) == {})
        check("nfs mounts count as disks", app.is_real_disk("nas:/vol/home", "/home"))
        check("docker overlays do not",
              not app.is_real_disk("overlay", "/var/lib/docker/overlay2/x"))

        busy = app.cpu_percent((9460, 8100), (9560, 8150))
        check("cpu percent is the delta, not the boot average",
              abs(busy - 50.0) < 0.01, str(busy))
        check("one sample gives no percent", app.cpu_percent(None, (9460, 8100)) is None)
        check("a reboot gives no percent",
              app.cpu_percent((9460, 8100), (10, 5)) is None)

        rates = app.net_rates(data["net"],
                              {"lo": (12345, 12345),
                               "eth0": (1000000 + 4096, 500000 + 1024),
                               "docker0": (777777 + 8192, 88888 + 8192)}, 2.0)
        check("net rate is bytes over the elapsed seconds",
              rates["eth0"] == (2048.0, 512.0), str(rates.get("eth0")))
        check("loopback and docker are not on the wire",
              app.net_total(rates) == (2048.0, 512.0), str(app.net_total(rates)))
        check("an interface that only just appeared is skipped",
              app.net_rates({}, data["net"], 2.0) is None)
        check("a wrapped counter is skipped",
              app.net_rates(data["net"], {"eth0": (10, 5)}, 2.0) is None)
        check("one sample gives no rate",
              app.net_rates(None, data["net"], 2.0) is None)
        check("a zero interval gives no rate",
              app.net_rates(data["net"], data["net"], 0) is None)
        check("every interface stacked falls back to counting them all",
              app.net_total({"docker0": (8.0, 4.0)}) == (8.0, 4.0))

        markup = app.stats_markup("imaginary", data, busy, rates)
        check("host named in the bar", "<b>imaginary</b>" in markup)
        check("every gauge present",
              all(seg in markup for seg in ("CPU", "MEM", "50%", "43%")), markup)
        check("a filesystem near full is called out", "⚠ /var/log 95%" in markup, markup)
        check("network throughput is in the bar",
              "net ↓ 2.0K/s  ↑ 512B/s" in markup, markup)
        check("no second sample yet means no rate, not a zero",
              "net <span alpha='55%'>↓ —" in app.stats_markup("imaginary", data, busy))
        check("stale readings say so", "(stale)" in
              app.stats_markup("imaginary", data, busy, stale=True))
        check("tooltip lists every filesystem",
              "/var/log" in app.stats_tooltip("imaginary", data))
        tip = app.stats_tooltip("imaginary", data, rates)
        check("tooltip lists interfaces busiest first",
              tip.index("docker0") < tip.index("eth0"), tip)
        check("an idle interface nobody counts is left out",
              "lo " not in tip, tip)
        check("tooltip says which interfaces do not count",
              "docker0" in tip and "(not counted)" in tip, tip)

        print("\n== status bar: live against this machine ==")
        win = self.win
        pane = win.current_tab().panes()[0]
        win.set_active_pane(pane)
        check("the bar is on by default", win.statusbar.get_visible())
        win.statusbar.retarget(force=True)
        # two polls: the first can only sample, the second can subtract
        GLib.timeout_add(1200, lambda: (win.statusbar.retarget(force=True),
                                        False)[1])
        GLib.timeout_add(2600, self.phase_stats_live)
        return False

    def phase_stats_live(self):
        win = self.win
        text = win.statusbar.label.get_text()
        check("a real reading was rendered",
              "CPU" in text and "MEM" in text and "load" in text, text)
        check("the network readout is live too", "net ↓" in text, text)
        check("a percentage was measured", "%" in text, text)
        check("the local shell is named", text.startswith("local"), text[:40])
        check("the tooltip was filled in",
              "used of" in (win.statusbar.get_tooltip_markup() or ""))

        win.toggle_statusbar()
        check("Ctrl+Shift+S hides it", not win.statusbar.get_visible())
        check("hiding stops the polling", win.statusbar.timer is None)
        check("the choice is remembered",
              app.load_settings()["statusbar"] is False)
        win.toggle_statusbar()
        check("and brings it back", win.statusbar.get_visible()
              and win.statusbar.timer is not None)
        self.finish()
        return False

    def finish(self):
        print("\n== session persistence ==")
        self.win._save_session()
        check("session file written", os.path.exists(app.SESSION_FILE))
        check("local shells excluded from session",
              self.win._load_session() == [], str(self.win._load_session()))

        GLib.spawn_command_line_sync(f"spectacle -a -b -n -o {SHOT}")
        print("\n" + ("ALL CHECKS PASSED" if not FAILURES
                      else f"FAILURES: {FAILURES}"))
        self.quit()


sys.exit(Smoke().run([]))
