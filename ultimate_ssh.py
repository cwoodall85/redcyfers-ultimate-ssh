#!/usr/bin/env python3
"""Ultimate SSH - a session-tree terminal for people with too many hosts.

Tier 2: broadcast input to many hosts, split panes, an SFTP-ish remote file
explorer, reconnect, and session restore.

Design rule, unchanged from Tier 1: ssh(1) does all the connecting. There is no
SSH library here, no credential store, no second auth path. Your ~/.ssh/config
is the only source of truth. The explorer shells out to the *same* ssh with a
shared ControlMaster socket, so it rides the connection your terminal already
opened instead of authenticating again.
"""

import os
import re
import sys
import json
import stat
import shlex
import socket
import colorsys
import hashlib
import subprocess

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Vte", "3.91")
from gi.repository import Gtk, Vte, GLib, Gio, Gdk, Pango, GObject  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import editor  # noqa: E402
import chatbridge  # noqa: E402
import sshconfig  # noqa: E402
from sshconfig import SshConfig  # noqa: E402

# Ultimate SSH's own copy of your connections, seeded from ~/.ssh/config on first
# run. ssh gets -F on this file, so the app never reads or writes the system
# config again after the import.
SSH_CONFIG = sshconfig.HOSTS_FILE
SESSION_FILE = sshconfig.SESSION_FILE
APP_ID = "dev.ultimatessh.UltimateSsh"

RUNTIME_DIR = os.path.join(
    os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "ultimate-ssh")

# Opening a whole group means N simultaneous logins; ask first past this many.
BULK_CONFIRM_AT = 8

# Copy-on-select writes CLIPBOARD once the selection has settled for this
# long. Every selection-changed during a drag would otherwise be a fresh
# clipboard offer, and each new offer cancels the previous one -- if Klipper
# was mid-read of the old offer it records an EMPTY item, and with "prevent
# empty clipboard" on it then re-offers that empty item as the clipboard.
COPY_SETTLE_MS = 60

# ULTIMATE_SSH_DEBUG=clipboard logs every selection change, button event,
# clipboard write and clipboard ownership change to ~/.ultimate-ssh/debug.log.
DEBUG = os.environ.get("ULTIMATE_SSH_DEBUG", "")
DEBUG_LOG = os.path.join(sshconfig.APP_HOME, "debug.log")


def debug(topic, message):
    if topic not in DEBUG and "all" not in DEBUG:
        return
    import time
    now = time.time()
    stamp = time.strftime("%H:%M:%S", time.localtime(now))
    try:
        with open(DEBUG_LOG, "a") as fh:
            fh.write(f"{stamp}.{int(now * 1000) % 1000:03d} {topic}: {message}\n")
    except OSError:
        pass


PALETTE = [
    "#2e3436", "#cc0000", "#4e9a06", "#c4a000",
    "#3465a4", "#75507b", "#06989a", "#d3d7cf",
    "#555753", "#ef2929", "#8ae234", "#fce94f",
    "#729fcf", "#ad7fa8", "#34e2e2", "#eeeeec",
]
FG = "#d0d0d0"
BG = "#101216"
FONT_MIN, FONT_MAX = 6, 48
LIGHT_FG = "#1c1c1c"
LIGHT_BG = "#fbfbfb"

SETTINGS_FILE = os.path.join(sshconfig.APP_HOME, "settings.json")
DEFAULT_SETTINGS = {"theme": "system", "font": "monospace 11",
                    "scrollback": 100000, "colors": True,
                    "persistence": "plain", "density": "compact",
                    "statusbar": True, "statusbar_interval": 5,
                    "copy_on_select": True, "opacity": 0.92}

# How see-through the window may get: below half, text over a busy desktop
# stops being readable.
OPACITY_MIN = 0.5


def opacity_of(settings):
    try:
        v = float(settings.get("opacity", 1.0))
    except (TypeError, ValueError):
        v = 1.0
    return min(1.0, max(OPACITY_MIN, v))


# The window paints nothing under the terminals, so a terminal background
# with alpha shows the desktop through it (the compositor does the rest, as
# for Konsole's profile opacity). Everything that is not a terminal keeps a
# fill at the same opacity, so the sidebar and bars stay readable. The fill
# is the terminal's own background: GTK themes don't agree on colour names
# (Breeze has no @window_bg_color), and an unknown one drops the rule.
# VTE paints its own background opaque whatever alpha it is given, so a
# see-through terminal stops clearing its background and gets this fill
# from GTK instead.
TRANSLUCENT_CSS = """
window.ussh-translucent,
window.ussh-translucent notebook,
window.ussh-translucent notebook > stack,
window.ussh-translucent .ussh-panel list,
window.ussh-translucent .ussh-panel listview,
window.ussh-translucent .ussh-panel scrolledwindow,
window.ussh-translucent .ussh-panel viewport,
window.ussh-translucent .ussh-panel .view {
  background-color: transparent;
}
window.ussh-translucent .ussh-panel,
window.ussh-translucent notebook > header,
window.ussh-translucent vte-terminal.ussh-see-through {
  background-color: rgba(%(rgb)s, %(alpha).3f);
}
"""


def load_settings():
    values = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE) as fh:
            stored = json.load(fh)
        if isinstance(stored, dict):
            values.update({k: v for k, v in stored.items()
                           if k in DEFAULT_SETTINGS})
    except (OSError, ValueError):
        pass
    return values


def save_settings(values):
    try:
        os.makedirs(sshconfig.APP_HOME, mode=0o700, exist_ok=True)
        with open(SETTINGS_FILE, "w") as fh:
            json.dump(values, fh, indent=2)
    except OSError:
        pass

BROADCAST_OFF, BROADCAST_GROUP, BROADCAST_ALL = range(3)
BROADCAST_LABELS = ["Broadcast: off", "Broadcast: group", "Broadcast: ALL"]


def rgba(spec):
    c = Gdk.RGBA()
    c.parse(spec)
    return c


def group_color(name):
    """Stable pastel per group, so prod boxes never look like dev boxes."""
    h = int(hashlib.md5(name.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    r, g, b = colorsys.hls_to_rgb(h, 0.68, 0.62)
    return "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))


def human_size(n):
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0


# Host parsing, grouping and editing all live in sshconfig.py -- it is the
# only thing that touches ~/.ssh/config, and it is tested headlessly.


# --------------------------------------------------------------------------
# remote access -- everything goes through ssh(1) over a shared control socket
# --------------------------------------------------------------------------

# Remote session wrappers. Each falls back to a plain login shell rather than
# dropping you back to the sidebar if the tool isn't installed on that host.
# NOT str.format: these snippets contain shell brace groups (`|| { ...; }`),
# which format() would try to read as replacement fields.
SESSION_TOKEN = "@@SESSION@@"
PERSISTENCE_COMMANDS = {
    "plain": None,
    "tmux": 'tmux new-session -A -s @@SESSION@@ || { echo "tmux is not '
            'installed on this host - opening a plain shell"; '
            'exec "$SHELL" -l; }',
    "screen": 'screen -DR @@SESSION@@ || { echo "screen is not installed on '
              'this host - opening a plain shell"; exec "$SHELL" -l; }',
}
PERSISTENCE_ORDER = ["plain", "tmux", "screen"]
PERSISTENCE_LABELS = {"plain": "Session: plain",
                      "tmux": "Session: tmux",
                      "screen": "Session: screen"}
DEFAULT_SESSION_NAME = "ultimate"

# Claude Code, wrapped in tmux so a dropped connection doesn't kill it.
CLAUDE_COMMAND = (
    'tmux new-session -A -s @@SESSION@@ claude || claude '
    '|| { echo "claude is not installed on this host - opening a plain shell"; '
    'exec "$SHELL" -l; }'
)


def session_command(template, name):
    """Substitute the session name without letting shell braces confuse us."""
    return template.replace(SESSION_TOKEN, name)

MUX_FAILURE_MARKERS = (
    "mux_client",
    "control socket",
    "multiplexing",
    "read from master failed",
)


def sweep_control_sockets():
    """Delete control sockets nobody is listening on. Returns how many.

    A master killed uncleanly leaves its socket behind, and the next client to
    find it fails with a broken pipe instead of just connecting. Connecting to
    the socket is the reliable test: refused means the master is gone.
    """
    removed = 0
    try:
        names = os.listdir(RUNTIME_DIR)
    except OSError:
        return 0
    for name in names:
        path = os.path.join(RUNTIME_DIR, name)
        try:
            if not stat.S_ISSOCK(os.stat(path).st_mode):
                continue
        except OSError:
            continue
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(2)
        try:
            probe.connect(path)
        except OSError:
            try:
                os.unlink(path)
                removed += 1
            except OSError:
                pass
        finally:
            probe.close()
    return removed


def ssh_argv(alias, remote_command=None, user=None, force_password=False,
             batch=False, identity=None, allow_master=True, no_mux=False,
             persistence=None, session_name=DEFAULT_SESSION_NAME,
             force_tty=False):
    """ssh argv, optionally multiplexed so terminal and explorer share a login.

    %C is a short hash of (host, port, user), which keeps the socket path well
    under the ~108 byte sockaddr_un limit that bites long hostnames. It also
    means overriding the user gets its own socket rather than reusing a
    session authenticated as somebody else.

    allow_master is the important one. Only long-lived interactive sessions may
    *create* a master; one-shot commands join an existing one or open their own
    connection. Otherwise a directory listing becomes the master that your
    shell depends on, and when that short command's master expires the shell
    dies with "read from master failed".
    """
    if remote_command is None and persistence and persistence != "plain":
        template = PERSISTENCE_COMMANDS.get(persistence)
        if template:
            remote_command = session_command(template, session_name)
            force_tty = True    # tmux and screen need a terminal

    os.makedirs(RUNTIME_DIR, mode=0o700, exist_ok=True)
    argv = ["/usr/bin/ssh", "-F", SSH_CONFIG]
    if force_tty:
        argv.append("-t")
    if no_mux:
        argv += ["-S", "none"]          # last-resort fallback: no sharing
    else:
        argv += [
            "-o", "ControlMaster=" + ("auto" if allow_master else "no"),
            "-o", f"ControlPath={RUNTIME_DIR}/c-%C",
            "-o", "ControlPersist=60",
        ]
    if user:
        argv += ["-o", f"User={user}"]
    if identity:
        argv += ["-o", f"IdentityFile={identity}", "-o", "IdentitiesOnly=yes"]
    if force_password:
        # Stop ssh offering the key that just got refused; go straight to the
        # password prompt instead of burning attempts on pubkey auth.
        argv += ["-o", "PubkeyAuthentication=no",
                 "-o", "PreferredAuthentications=password,keyboard-interactive"]
    if batch:
        # Non-interactive callers (the explorer) must fail fast rather than
        # block forever on a password prompt with no terminal to type into.
        argv += ["-o", "BatchMode=yes"]
    argv.append(alias)
    if remote_command:
        argv += ["--", remote_command]
    return argv


def scp_argv(extra):
    # ControlMaster=no: transfers ride an existing master but never become one
    argv = [
        "/usr/bin/scp",
        "-F", SSH_CONFIG,
        "-o", "ControlMaster=no",
        "-o", f"ControlPath={RUNTIME_DIR}/c-%C",
        "-o", "ControlPersist=60",
    ]
    return argv + extra


class RemoteError(Exception):
    pass


def resolve_ssh_settings(alias):
    """What ssh will actually use for `alias`, asked of ssh itself.

    Reading the file cannot answer this: precedence between wildcard and
    specific blocks, and any Include, are ssh's own business. `-G` parses the
    config and resolves it without connecting to anything, so it is cheap
    enough to run before each spawn.
    """
    try:
        proc = subprocess.run(["/usr/bin/ssh", "-F", SSH_CONFIG, "-G", alias],
                              capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return {}
    if proc.returncode != 0:
        return {}

    resolved = {}
    for line in proc.stdout.splitlines():
        key, _, value = line.partition(" ")
        if not value:
            continue
        key = key.lower()
        if key == "identityfile":
            resolved.setdefault("identityfile", []).append(value)
        else:
            resolved.setdefault(key, value)
    return resolved


def run_argv_async(argv, on_done):
    """Run a command without blocking the UI thread. on_done(stdout, error).

    Deliberately the bytes API, not communicate_utf8_async: that one hands back
    a C string, so it truncates at the first NUL -- and NUL is exactly what we
    use to delimit filenames in directory listings.
    """
    try:
        proc = Gio.Subprocess.new(
            argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE)
    except GLib.Error as exc:
        on_done(None, str(exc))
        return

    def finished(p, res):
        try:
            _ok, out, err = p.communicate_finish(res)
        except GLib.Error as exc:
            on_done(None, str(exc))
            return
        decode = lambda b: (b.get_data() or b"").decode("utf-8", "replace") if b else ""
        if p.get_exit_status() != 0:
            on_done(None, decode(err).strip() or f"exited {p.get_exit_status()}")
        else:
            on_done(decode(out), None)

    proc.communicate_async(None, None, finished)


def run_ssh_async(alias, remote_command, on_done):
    """Everything the explorer does goes through here.

    BatchMode: with no tty, a password prompt would hang the listing forever.
    If a terminal tab already holds this host open, the shared control socket
    means no authentication happens here at all.
    """
    run_argv_async(
        ssh_argv(alias, remote_command, batch=True, allow_master=False),
        on_done)


# --------------------------------------------------------------------------
# host stats -- what the status bar along the bottom is made of
# --------------------------------------------------------------------------

# One round trip per poll, and it rides the control socket the terminal
# already holds open, so a reading costs no login and no agent has to be
# installed on the far end. Sections are tagged because a login shell is free
# to print its own noise between the commands.
#
# Every read is guarded and the last command is a bare echo, so a host missing
# any one of these files still returns the rest with exit status 0 -- a partial
# reading is worth more than an error.
STATS_COMMAND = "; ".join([
    "LC_ALL=C",
    "echo @@CPU", "grep -m1 '^cpu ' /proc/stat 2>/dev/null",
    "echo @@NCPU", "grep -c '^processor' /proc/cpuinfo 2>/dev/null",
    "echo @@LOAD", "cat /proc/loadavg 2>/dev/null",
    "echo @@MEM",
    "grep -E '^(MemTotal|MemAvailable|MemFree|Buffers|Cached|SwapTotal|"
    "SwapFree):' /proc/meminfo 2>/dev/null",
    "echo @@UP", "cat /proc/uptime 2>/dev/null",
    "echo @@NET", "cat /proc/net/dev 2>/dev/null",
    "echo @@DF", "df -kP 2>/dev/null",
    "echo @@END",
])

# How often the bar may refresh, in seconds. The floor is not politeness --
# below it the CPU delta is computed from too few jiffies to mean anything.
STATS_INTERVAL_MIN, STATS_INTERVAL_MAX = 2, 300


def short_error(text):
    """The one line of ssh's complaint that fits in a status bar."""
    for line in (text or "").splitlines():
        line = line.strip()
        low = line.lower()
        if not line or low.startswith(("warning:", "pseudo-terminal")):
            continue
        return line[:90]
    return "no reading"


# Filesystems that are not a disk anybody can fill up.
PSEUDO_FS = {"tmpfs", "devtmpfs", "ramfs", "squashfs", "overlay", "udev",
             "none", "shm", "efivarfs", "proc", "sysfs", "devpts", "mqueue",
             "hugetlbfs", "tracefs", "cgroup", "cgroup2"}
PSEUDO_MOUNTS = ("/dev/", "/proc/", "/sys/", "/run/", "/snap/",
                 "/var/lib/docker/")


def is_real_disk(device, mount):
    if device in PSEUDO_FS or device.startswith("fuse"):
        return False
    if mount in ("/dev", "/proc", "/sys", "/run"):
        return False
    if mount.startswith("/run/media/"):
        return True      # removable media is under /run but is a real disk
    return not mount.startswith(PSEUDO_MOUNTS)


def parse_stats(text):
    """STATS_COMMAND output -> numbers. {} if this host told us nothing.

    Deliberately forgiving: each section is parsed on its own, so a host with
    no /proc/cpuinfo still reports its memory and its disks. The one hard
    requirement is the @@END marker -- without it the output was truncated
    mid-reading and the numbers cannot be trusted.
    """
    sections, current = {}, None
    for line in (text or "").splitlines():
        marker = line.strip()
        if marker.startswith("@@"):
            current = marker[2:]
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    if "END" not in sections:
        return {}

    def first(name):
        lines = sections.get(name) or [""]
        return lines[0].strip()

    out = {}

    cpu = first("CPU").split()
    if len(cpu) >= 5 and cpu[0] == "cpu":
        try:
            nums = [int(v) for v in cpu[1:]]
        except ValueError:
            nums = []
        if len(nums) >= 5:
            # idle + iowait are the two columns that are not work being done
            out["cpu"] = (sum(nums), nums[3] + nums[4])

    try:
        out["ncpu"] = max(1, int(first("NCPU")))
    except ValueError:
        pass

    load = first("LOAD").split()
    if len(load) >= 3:
        try:
            out["load"] = tuple(float(v) for v in load[:3])
        except ValueError:
            pass

    mem = {}
    for line in sections.get("MEM", []):
        key, _, value = line.partition(":")
        digits = value.split()
        if digits and digits[0].isdigit():
            mem[key.strip()] = int(digits[0]) * 1024      # meminfo is in kB
    total = mem.get("MemTotal")
    if total:
        available = mem.get("MemAvailable")
        if available is None:
            # Kernels before 3.14 have no MemAvailable; fall back to what
            # `free` used to call "free + buffers + cached".
            available = (mem.get("MemFree", 0) + mem.get("Buffers", 0)
                         + mem.get("Cached", 0))
        out["mem"] = (total, max(0, total - min(total, available)))
    swap_total = mem.get("SwapTotal")
    if swap_total:
        out["swap"] = (swap_total,
                       max(0, swap_total - mem.get("SwapFree", 0)))

    # The fraction is kept: it is the clock the network rates are divided
    # by. Timing them at this end instead would fold ssh's own round-trip
    # jitter -- which on a slow link is most of a poll -- into every rate.
    uptime = first("UP").split()
    if uptime:
        try:
            out["clock"] = float(uptime[0])
        except ValueError:
            pass
        else:
            out["uptime"] = int(out["clock"])

    net = {}
    for line in sections.get("NET", []):
        name, _, rest = line.partition(":")
        name, fields = name.strip(), rest.split()
        # Receive bytes is the first column and transmit bytes the ninth.
        # The two header lines carry no colon, so they fall out here.
        if not name or len(fields) < 9:
            continue
        try:
            net[name] = (int(fields[0]), int(fields[8]))
        except ValueError:
            continue
    if net:
        out["net"] = net

    disks = []
    for line in sections.get("DF", []):
        fields = line.split()
        # df's own header fails the digit test, which is how it gets skipped
        if (len(fields) < 6 or not fields[1].isdigit()
                or not fields[2].isdigit()):
            continue
        size, used = int(fields[1]) * 1024, int(fields[2]) * 1024
        if size <= 0:
            continue
        mount = " ".join(fields[5:])     # -P guarantees the mount point last
        # Take df's own Capacity column when it is there. used/size disagrees
        # with it by the reserved-for-root blocks -- typically 5% -- and a bar
        # that argues with `df -h` on the same host is worse than useless.
        capacity = fields[4].rstrip("%")
        pct = (float(capacity) if capacity.replace(".", "", 1).isdigit()
               else 100.0 * used / size)
        disks.append({
            "device": fields[0], "mount": mount, "size": size, "used": used,
            "pct": pct, "real": is_real_disk(fields[0], mount),
        })
    if disks:
        out["disks"] = disks
    return out


def cpu_percent(previous, current):
    """Percent busy between two /proc/stat samples.

    One sample can only give the average since boot, which on a box that has
    been up for months never moves. The delta between polls is the only
    honest number, so the first poll of a host shows nothing.
    """
    if not previous or not current:
        return None
    total = current[0] - previous[0]
    idle = current[1] - previous[1]
    if total <= 0:
        return None      # counters went backwards: the host rebooted
    return max(0.0, min(100.0, 100.0 * (total - idle) / total))


# Interfaces whose bytes are somebody else's bytes counted a second time.
# Each of these rides on a physical interface that already reported the same
# traffic -- bridge members, container veths, VM taps, tunnels -- so adding
# them to the total would claim twice what actually crossed the wire. They are
# still listed in the tooltip; they just do not count towards the two numbers.
STACKED_IFACES = ("br", "veth", "docker", "virbr", "vnet", "vmnet", "tap",
                  "tun", "wg", "ppp", "bond", "dummy", "sit", "gre", "ifb")


def is_real_iface(name):
    return name != "lo" and not name.startswith(STACKED_IFACES)


def net_rates(previous, current, seconds):
    """Bytes/sec per interface between two /proc/net/dev samples.

    The same bargain as the CPU percent: one sample is only a total since
    boot, so nothing can be shown until there is a second one to subtract.
    """
    if not previous or not current or not seconds or seconds <= 0:
        return None
    rates = {}
    for name, (rx, tx) in current.items():
        before = previous.get(name)
        if before is None:
            continue                     # interface appeared between polls
        if rx < before[0] or tx < before[1]:
            continue                     # reboot, or a 32-bit counter wrapped
        rates[name] = ((rx - before[0]) / seconds, (tx - before[1]) / seconds)
    return rates or None


def net_total(rates):
    """The pair the bar shows: bytes/sec down and up across the real wires."""
    if not rates:
        return None
    # If every interface is a stacked one there is nothing to double count
    # against -- inside a container, eth0 may well be a veth end -- so rather
    # than show nothing, count them all.
    real = [v for name, v in rates.items() if is_real_iface(name)]
    real = real or list(rates.values())
    return sum(v[0] for v in real), sum(v[1] for v in real)


def human_rate(per_second):
    return f"{human_size(per_second)}/s"


def format_uptime(seconds):
    if seconds is None or seconds < 0:
        return ""
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    if days:
        return f"{days}d {hours}h"
    minutes = rest // 60
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def pick_disk(disks, preferred="/"):
    """The one filesystem the bar has room for: / if there is one."""
    real = [d for d in disks or [] if d["real"]]
    for disk in real:
        if disk["mount"] == preferred:
            return disk
    return max(real, key=lambda d: d["size"], default=None)


DISK_ALERT_AT = 90        # a second filesystem this full gets its own chip
METER_WIDTH = 8


def meter(pct):
    filled = int(round(max(0.0, min(100.0, pct)) / 100.0 * METER_WIDTH))
    return "█" * filled + "░" * (METER_WIDTH - filled)


def meter_tone(pct):
    if pct >= 90:
        return "#e01b24"
    if pct >= 75:
        return "#e5a50a"
    return None


def _gauge(label, pct, detail=""):
    # The unused cells are drawn faint rather than in the foreground colour:
    # at full strength the shade glyph reads as a texture and the eye cannot
    # find where the bar actually ends.
    bar = meter(pct)
    filled = bar.rstrip("░")
    body = (f"{filled}<span alpha='30%'>{bar[len(filled):]}</span>"
            f" {pct:3.0f}%")
    tone = meter_tone(pct)
    if tone:
        body = f"<span color='{tone}'><b>{body}</b></span>"
    return f"{label} {body}" + (f" {detail}" if detail else "")


def stats_markup(name, data, cpu_pct=None, net=None, stale=False):
    """The whole bar as one Pango string. Pure, so a test can read it."""
    esc = GLib.markup_escape_text
    parts = [f"<b>{esc(name)}</b>"]

    if cpu_pct is None:
        parts.append("CPU <span alpha='55%'>░░░░░░░░   —</span>")
    else:
        parts.append(_gauge("CPU", cpu_pct))

    if data.get("mem"):
        total, used = data["mem"]
        parts.append(_gauge("MEM", 100.0 * used / total,
                            f"{human_size(used)}/{human_size(total)}"))

    disk = pick_disk(data.get("disks"))
    if disk:
        parts.append(_gauge(esc(disk["mount"]), disk["pct"],
                            f"{human_size(disk['used'])}/"
                            f"{human_size(disk['size'])}"))

    if data.get("net"):
        total = net_total(net)
        if total is None:
            parts.append("net <span alpha='55%'>↓ —  ↑ —</span>")
        else:
            down, up = total
            parts.append(f"net ↓ {human_rate(down)}  ↑ {human_rate(up)}")

    if data.get("swap") and data["swap"][1] > 0:
        total, used = data["swap"]
        parts.append(f"swap {human_size(used)}/{human_size(total)}")

    if data.get("load"):
        cores = f" ({data['ncpu']} cpu)" if data.get("ncpu") else ""
        one, five, fifteen = data["load"]
        parts.append(f"load {one:.2f} {five:.2f} {fifteen:.2f}{cores}")

    if data.get("uptime") is not None:
        parts.append(f"up {format_uptime(data['uptime'])}")

    # Anything else close to full is the whole point of having the bar, so it
    # gets said out loud instead of hiding in the tooltip.
    for other in sorted((d for d in data.get("disks") or []
                         if d["real"] and d is not disk
                         and d["pct"] >= DISK_ALERT_AT),
                        key=lambda d: -d["pct"])[:2]:
        parts.append(f"<span color='#e01b24'><b>⚠ {esc(other['mount'])} "
                     f"{other['pct']:.0f}%</b></span>")

    line = "  ·  ".join(parts)
    return f"{line} <span alpha='55%'>(stale)</span>" if stale else line


def stats_tooltip(name, data, net=None):
    esc = GLib.markup_escape_text
    rows = [f"<b>{esc(name)}</b>"]
    if data.get("mem"):
        total, used = data["mem"]
        rows.append(f"memory   {human_size(used)} used of {human_size(total)}")
    if data.get("swap"):
        total, used = data["swap"]
        rows.append(f"swap     {human_size(used)} used of {human_size(total)}")
    rates = net or {}
    ifaces = sorted(data.get("net") or {},
                    key=lambda n: (-sum(rates.get(n, (0, 0))), n))
    # A container host has dozens of veths. Show the wires, plus whatever
    # stacked interface is actually moving something.
    ifaces = [i for i in ifaces
              if is_real_iface(i) or sum(rates.get(i, (0, 0))) > 0][:8]
    if ifaces:
        rows.append("")
        name_w = max(len(i) for i in ifaces)
        for iface in ifaces:
            rate = rates.get(iface)
            moved = (f"↓ {human_rate(rate[0]):>9}  ↑ {human_rate(rate[1]):>9}"
                     if rate else "↓ —  ↑ —")
            note = "" if is_real_iface(iface) else "  (not counted)"
            rows.append(esc(f"{iface:<{name_w}}  {moved}{note}"))

    disks = [d for d in data.get("disks") or [] if d["real"]]
    if disks:
        usage = {id(d): f"{human_size(d['used'])}/{human_size(d['size'])}"
                 for d in disks}
        mount_w = max(len(d["mount"]) for d in disks)
        usage_w = max(len(v) for v in usage.values())
        rows.append("")
        for disk in sorted(disks, key=lambda d: d["mount"]):
            rows.append(esc(f"{disk['mount']:<{mount_w}}  "
                            f"{usage[id(disk)]:>{usage_w}}  "
                            f"{disk['pct']:3.0f}%  {disk['device']}"))
    return "\n".join(rows)


class Entry:
    def __init__(self, kind, size, mtime, mode, name):
        self.kind = kind        # find's %y: d, f, l, ...
        self.size = size
        self.mtime = mtime
        self.mode = mode
        self.name = name

    @property
    def is_dir(self):
        return self.kind == "d"


def remote_path_expr(path):
    """Shell-quote a remote path while keeping a leading ~ meaningful.

    shlex.quote("~") returns "'~'", which the remote shell will NOT expand --
    quoting it naively makes every home-relative path fail to resolve.
    """
    if path in ("", "~"):
        return '"$HOME"'
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def list_remote_dir(alias, path, on_done):
    """List a remote directory. on_done(resolved_path, [Entry], error).

    Null-delimited find(1) output rather than parsing `ls -l`: filenames with
    spaces, quotes or newlines in them stop being a parsing problem.
    """
    quoted = remote_path_expr(path)
    cmd = (
        f"cd -- {quoted} && pwd && "
        f"find . -maxdepth 1 -mindepth 1 -printf '%y\\t%s\\t%T@\\t%M\\t%f\\0' "
        f"2>/dev/null"
    )

    def done(out, err):
        if err is not None:
            on_done(None, None, err)
            return
        head, _, rest = out.partition("\n")
        resolved = head.strip() or path
        entries = []
        for record in rest.split("\0"):
            if not record:
                continue
            parts = record.split("\t", 4)
            if len(parts) != 5:
                continue
            kind, size, mtime, mode, name = parts
            try:
                entries.append(Entry(kind, int(size), float(mtime), mode, name))
            except ValueError:
                continue
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        on_done(resolved, entries, None)

    run_ssh_async(alias, cmd, done)


# --------------------------------------------------------------------------
# explorer
# --------------------------------------------------------------------------

ICON_BY_EXT = {
    ".png": "image-x-generic", ".jpg": "image-x-generic",
    ".jpeg": "image-x-generic", ".gif": "image-x-generic",
    ".svg": "image-x-generic", ".webp": "image-x-generic",
    ".tar": "package-x-generic", ".gz": "package-x-generic",
    ".zip": "package-x-generic", ".xz": "package-x-generic",
    ".rpm": "package-x-generic", ".deb": "package-x-generic",
    ".conf": "text-x-generic-template", ".cfg": "text-x-generic-template",
    ".ini": "text-x-generic-template", ".yaml": "text-x-generic-template",
    ".yml": "text-x-generic-template", ".json": "text-x-generic-template",
    ".sh": "text-x-script", ".bash": "text-x-script", ".py": "text-x-script",
    ".pl": "text-x-script", ".rb": "text-x-script",
    ".log": "text-x-generic", ".txt": "text-x-generic",
    ".md": "text-x-generic", ".sql": "text-x-generic",
}


# Makes a remote shell announce its working directory the way a local one does.
# bash and zsh both handled; defined on one line because it is typed into a
# live shell rather than sourced from a file.
OSC7_SNIPPET = (
    r"""__ussh7(){ printf '\033]7;file://%s%s\033\\' "${HOSTNAME:-$(uname -n)}" "$PWD"; }; """
    r"""if [ -n "$ZSH_VERSION" ]; then autoload -Uz add-zsh-hook 2>/dev/null; """
    r"""add-zsh-hook precmd __ussh7 2>/dev/null || precmd_functions+=(__ussh7); """
    r"""else PROMPT_COMMAND="__ussh7${PROMPT_COMMAND:+;$PROMPT_COMMAND}"; fi; __ussh7"""
)


def _icon_label(icon_name, text, pixel_size=16):
    """An icon beside a label, for buttons that deserve more than one or other."""
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                  halign=Gtk.Align.CENTER)
    icon = Gtk.Image.new_from_icon_name(icon_name)
    icon.set_pixel_size(pixel_size)
    box.append(icon)
    box.append(Gtk.Label(label=text))
    return box


def entry_icon(entry):
    if entry.is_dir:
        return "folder"
    if entry.kind == "l":
        return "emblem-symbolic-link"
    if "x" in entry.mode[1:4]:
        return "application-x-executable"
    return ICON_BY_EXT.get(os.path.splitext(entry.name)[1].lower(),
                           "text-x-generic")


class ExplorerPane(Gtk.Box):
    """A graphical remote file browser docked beside a terminal.

    Icon grid with history navigation, drag-and-drop upload, and a right-click
    menu for rename / delete / chmod / download. Every operation is one ssh or
    scp over the control socket the terminal already opened.
    """

    def __init__(self, tab, alias, on_cd_terminal):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.tab = tab
        self.alias = alias
        self.on_cd_terminal = on_cd_terminal
        self.path = "~"
        self.entries = []
        self.history = []
        self.forward = []
        self.view_mode = "grid"
        self._popover = None
        self.follow_handlers = []
        self._nav_seq = 0
        self.set_size_request(360, -1)

        self.append(self._build_toolbar())

        self.grid = Gtk.FlowBox(
            valign=Gtk.Align.START, homogeneous=True,
            selection_mode=Gtk.SelectionMode.SINGLE,
            max_children_per_line=12, min_children_per_line=2,
            row_spacing=2, column_spacing=2)
        self.grid.connect("child-activated", self._on_activated)
        self.grid.set_margin_start(6)
        self.grid.set_margin_end(6)
        self.grid.set_margin_top(4)

        right_click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        right_click.connect("pressed", self._on_right_click)
        self.grid.add_controller(right_click)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.grid)

        # Drop files anywhere in the browser to upload them here.
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        scroller.add_controller(drop)
        self.append(scroller)

        self.status = Gtk.Label(xalign=0.0, wrap=True)
        self.status.add_css_class("dim-label")
        self.status.set_margin_start(10)
        self.status.set_margin_end(10)
        self.status.set_margin_bottom(6)
        self.status.set_margin_top(4)
        self.append(self.status)

        self.navigate("~", record=False)

    def _build_toolbar(self):
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        bar.set_margin_top(6)
        bar.set_margin_bottom(4)
        bar.set_margin_start(6)
        bar.set_margin_end(6)

        self.back_btn = Gtk.Button(icon_name="go-previous-symbolic",
                                   tooltip_text="Back")
        self.back_btn.connect("clicked", lambda _b: self.go_back())
        bar.append(self.back_btn)

        self.forward_btn = Gtk.Button(icon_name="go-next-symbolic",
                                      tooltip_text="Forward")
        self.forward_btn.connect("clicked", lambda _b: self.go_forward())
        bar.append(self.forward_btn)

        up = Gtk.Button(icon_name="go-up-symbolic",
                        tooltip_text="Parent directory")
        up.connect("clicked", lambda _b: self.navigate(self.path + "/.."))
        bar.append(up)

        home = Gtk.Button(icon_name="go-home-symbolic", tooltip_text="Home")
        home.connect("clicked", lambda _b: self.navigate("~"))
        bar.append(home)

        self.path_entry = Gtk.Entry(hexpand=True)
        self.path_entry.set_text(self.path)
        self.path_entry.connect(
            "activate", lambda e: self.navigate(e.get_text().strip()))
        bar.append(self.path_entry)

        self.view_btn = Gtk.Button(icon_name="view-list-symbolic",
                                   tooltip_text="Switch to list view")
        self.view_btn.connect("clicked", lambda _b: self.toggle_view())
        bar.append(self.view_btn)

        refresh = Gtk.Button(icon_name="view-refresh-symbolic",
                             tooltip_text="Refresh")
        refresh.connect("clicked", lambda _b: self.refresh())
        bar.append(refresh)

        upload = Gtk.Button(icon_name="document-send-symbolic",
                            tooltip_text="Upload a file here")
        upload.connect("clicked", lambda _b: self.upload())
        bar.append(upload)

        cd = Gtk.Button(icon_name="utilities-terminal-symbolic",
                        tooltip_text="Send `cd <path>` to this tab's terminal")
        cd.connect("clicked", lambda _b: self.on_cd_terminal(self.path))
        bar.append(cd)

        self.follow_btn = Gtk.ToggleButton(icon_name="insert-link-symbolic")
        self.follow_btn.set_tooltip_text(
            "Follow the terminal: browse whatever directory the shell is in")
        self.follow_btn.connect("toggled", self._on_follow_toggled)
        bar.append(self.follow_btn)
        return bar

    # -- following the terminal's directory ---------------------------------

    @property
    def following(self):
        return self.follow_btn.get_active()

    def _on_follow_toggled(self, button):
        if button.get_active():
            self._start_following()
        else:
            self._stop_following()
            self.status.set_text("no longer following the terminal")

    def _start_following(self):
        """Watch every pane in this tab for OSC 7 directory reports.

        VTE tracks the sequence itself, so this costs nothing until the shell
        actually reports -- no polling, and no typing `pwd` into your session.
        """
        self._stop_following()
        for pane in self.tab.panes():
            handler = pane.term.connect("notify::current-directory-uri",
                                        self._on_terminal_cwd_changed)
            self.follow_handlers.append((pane.term, handler))

        path = self.terminal_path()
        if path:
            self.status.set_text(f"following the terminal · {path}")
            self.navigate(path, record=False)
        else:
            self._offer_cwd_reporting()

    def _stop_following(self):
        for term, handler in self.follow_handlers:
            try:
                term.disconnect(handler)
            except (TypeError, RuntimeError):
                pass
        self.follow_handlers = []

    def refresh_following(self):
        """Re-attach after the pane tree changed (a split added a terminal)."""
        if self.following:
            self._start_following()

    def terminal_path(self):
        """Where the shell says it is, or None if it does not report.

        The focused pane wins, so following does the obvious thing in a split.
        """
        panes = self.tab.panes()
        active = self.tab.window.active_pane
        if active is not None and active in panes:
            panes = [active] + [p for p in panes if p is not active]
        for pane in panes:
            uri = pane.term.get_current_directory_uri()
            if not uri:
                continue
            try:
                path, _host = GLib.filename_from_uri(uri)
            except GLib.Error:
                continue
            if path:
                return path
        return None

    def _on_terminal_cwd_changed(self, _term, _pspec):
        if not self.following:
            return
        path = self.terminal_path()
        if path and path != self.path:
            self.status.set_text(f"following the terminal · {path}")
            self.navigate(path, record=False)

    def _offer_cwd_reporting(self):
        """The remote shell isn't reporting. Offer to make it, for this session.

        ssh does not forward VTE_VERSION, so the vte.sh snippet that makes a
        local shell emit OSC 7 never fires on the far side. This installs an
        equivalent hook in the running shell only -- nothing is written to the
        remote host, and it is gone when the session ends.
        """
        def enable():
            pane = self.tab.window.active_pane
            panes = self.tab.panes()
            if pane is None or pane not in panes:
                pane = panes[0] if panes else None
            if pane is None:
                return
            # leading space so it stays out of history under ignorespace
            pane.term.feed_child((" " + OSC7_SNIPPET + "\n").encode())
            self.status.set_text(
                "asked the shell to report its directory — "
                "it will follow from the next prompt")

        def cancel():
            self.follow_btn.set_active(False)

        editor.confirm_or_cancel(
            self.tab.window,
            "This shell doesn't report its directory",
            "Ultimate SSH can follow the terminal only if the shell announces "
            "where it is (OSC 7). ssh does not forward the setting that makes "
            "that happen.\n\nEnable it for this session? A one-line hook is "
            "typed into the running shell. Nothing is written to the remote "
            "host, and it disappears when you log out.",
            "Enable for this session", enable, cancel)

    # -- navigation --------------------------------------------------------

    def navigate(self, path, record=True):
        if record and self.path:
            self.history.append(self.path)
            self.forward.clear()
        self.status.set_text(f"listing {path} …")
        # Listings are async and can finish out of order: clicking into a
        # folder while a slower listing is still running would otherwise let
        # the stale result land last and replace the directory you asked for.
        self._nav_seq += 1
        seq = self._nav_seq
        list_remote_dir(
            self.alias, path,
            lambda resolved, entries, error:
                self._on_listed(resolved, entries, error, seq=seq))

    def refresh(self):
        self.navigate(self.path, record=False)

    def go_back(self):
        if self.history:
            target = self.history.pop()
            self.forward.append(self.path)
            self.navigate(target, record=False)

    def go_forward(self):
        if self.forward:
            target = self.forward.pop()
            self.history.append(self.path)
            self.navigate(target, record=False)

    def toggle_view(self):
        self.view_mode = "list" if self.view_mode == "grid" else "grid"
        self.view_btn.set_icon_name(
            "view-grid-symbolic" if self.view_mode == "list"
            else "view-list-symbolic")
        self.view_btn.set_tooltip_text(
            "Switch to icon view" if self.view_mode == "list"
            else "Switch to list view")
        self._render()

    def _on_listed(self, resolved, entries, error, seq=None):
        if seq is not None and seq != self._nav_seq:
            return          # superseded by a newer navigation
        if error is not None:
            self.status.set_text(f"⚠ {error}")
            return
        self.path = resolved
        self.entries = entries
        self.path_entry.set_text(resolved)
        self._render()
        dirs = sum(1 for e in entries if e.is_dir)
        self.status.set_text(
            f"{len(entries)} items · {dirs} folders · {len(entries) - dirs} files"
            "   ·   drop files here to upload")
        self.back_btn.set_sensitive(bool(self.history))
        self.forward_btn.set_sensitive(bool(self.forward))

    # -- rendering ----------------------------------------------------------

    def _render(self):
        while (child := self.grid.get_first_child()) is not None:
            self.grid.remove(child)
        if self.view_mode == "grid":
            self.grid.set_max_children_per_line(12)
            self.grid.set_min_children_per_line(2)
            for entry in self.entries:
                self.grid.append(self._tile(entry))
        else:
            self.grid.set_max_children_per_line(1)
            self.grid.set_min_children_per_line(1)
            for entry in self.entries:
                self.grid.append(self._row(entry))

    def _tile(self, entry):
        child = Gtk.FlowBoxChild()
        child.entry = entry
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_size_request(96, -1)
        box.set_margin_top(8)
        box.set_margin_bottom(8)

        icon = Gtk.Image.new_from_icon_name(entry_icon(entry))
        icon.set_pixel_size(48)
        box.append(icon)

        name = Gtk.Label(label=entry.name)
        name.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        name.set_max_width_chars(13)
        name.set_justify(Gtk.Justification.CENTER)
        box.append(name)

        if not entry.is_dir:
            size = Gtk.Label(label=human_size(entry.size))
            size.add_css_class("dim-label")
            box.append(size)

        child.set_child(box)
        child.set_tooltip_text(f"{entry.name}\n{entry.mode}  {human_size(entry.size)}")
        return child

    def _row(self, entry):
        child = Gtk.FlowBoxChild()
        child.entry = entry
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_top(4)
        box.set_margin_bottom(4)
        box.set_margin_start(6)
        box.set_margin_end(6)

        icon = Gtk.Image.new_from_icon_name(entry_icon(entry))
        icon.set_pixel_size(16)
        box.append(icon)

        name = Gtk.Label(label=entry.name, xalign=0.0, hexpand=True)
        name.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        box.append(name)

        mode = Gtk.Label(label=entry.mode)
        mode.add_css_class("dim-label")
        box.append(mode)

        if not entry.is_dir:
            size = Gtk.Label(label=human_size(entry.size))
            size.add_css_class("dim-label")
            box.append(size)

        child.set_child(box)
        return child

    def _on_activated(self, _flowbox, child):
        entry = child.entry
        if entry.is_dir:
            self.navigate(os.path.join(self.path, entry.name))
        else:
            self.download(entry)

    def _selected(self):
        children = self.grid.get_selected_children()
        return children[0].entry if children else None

    # -- context menu -------------------------------------------------------

    def _on_right_click(self, _gesture, _n, x, y):
        child = self.grid.get_child_at_pos(int(x), int(y))
        if child is None:
            return
        self.grid.select_child(child)
        entry = child.entry

        items = [
            ("Open" if entry.is_dir else "Download",
             lambda: self._on_activated(self.grid, child)),
            ("Copy path", lambda: self.copy_path(entry)),
            (None, None),
            ("Rename…", lambda: self.rename(entry)),
            ("Permissions…", lambda: self.chmod(entry)),
            ("Delete…", lambda: self.delete(entry)),
            (None, None),
            ("Open in terminal here",
             lambda: self.on_cd_terminal(
                 os.path.join(self.path, entry.name) if entry.is_dir
                 else self.path)),
        ]

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        popover = Gtk.Popover()
        popover.set_has_arrow(False)
        popover.set_child(box)
        for label, handler in items:
            if label is None:
                box.append(Gtk.Separator())
                continue
            btn = Gtk.Button(label=label)
            btn.add_css_class("flat")
            inner = btn.get_child()
            if isinstance(inner, Gtk.Label):
                inner.set_xalign(0.0)
            btn.connect("clicked",
                        lambda _b, h=handler: (popover.popdown(), h()))
            box.append(btn)

        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_parent(self.grid)
        popover.set_pointing_to(rect)
        popover.connect("closed", lambda p: p.unparent())
        self._popover = popover
        popover.popup()

    # -- file operations ----------------------------------------------------

    def _remote(self, entry):
        return os.path.join(self.path, entry.name)

    def _run(self, command, busy, done_message):
        """Run a remote command, then refresh."""
        self.status.set_text(busy)

        def done(_out, err):
            if err is not None:
                self.status.set_text(f"⚠ {err}")
            else:
                self.status.set_text(done_message)
                self.refresh()

        run_ssh_async(self.alias, command, done)

    def copy_path(self, entry):
        text = self._remote(entry)
        provider = Gdk.ContentProvider.new_for_bytes(
            "text/plain;charset=utf-8", GLib.Bytes.new(text.encode()))
        self.tab.window.get_clipboard().set_content(provider)
        self.status.set_text(f"copied {text}")

    def rename(self, entry):
        def go(new_name):
            if "/" in new_name:
                self.status.set_text("⚠ a name cannot contain /")
                return
            src = shlex.quote(self._remote(entry))
            dst = shlex.quote(os.path.join(self.path, new_name))
            self._run(f"mv -n -- {src} {dst}", f"renaming {entry.name} …",
                      f"✓ renamed to {new_name}")

        editor.prompt_text(self.tab.window, "Rename",
                           f"New name for “{entry.name}”", entry.name, go)

    def chmod(self, entry):
        current = entry.mode

        def go(mode):
            if not re.fullmatch(r"[0-7]{3,4}", mode.strip()):
                self.status.set_text("⚠ use an octal mode such as 644 or 0755")
                return
            target = shlex.quote(self._remote(entry))
            self._run(f"chmod {mode.strip()} -- {target}",
                      f"chmod {mode} {entry.name} …", f"✓ mode set to {mode}")

        editor.prompt_text(
            self.tab.window, "Permissions",
            f"Octal mode for “{entry.name}”  (currently {current})",
            "755" if entry.is_dir else "644", go)

    def delete(self, entry):
        target = shlex.quote(self._remote(entry))
        # rmdir, not rm -rf: a non-empty directory should refuse rather than
        # silently destroy a tree from a stray double-click.
        command = f"rmdir -- {target}" if entry.is_dir else f"rm -- {target}"
        detail = (f"{self._remote(entry)} will be removed on {self.alias}."
                  + (" Only empty folders can be removed here."
                     if entry.is_dir else ""))

        editor.confirm(
            self.tab.window, f"Delete “{entry.name}”?", detail, "Delete",
            lambda: self._run(command, f"deleting {entry.name} …",
                              f"✓ deleted {entry.name}"))

    def download(self, entry=None):
        entry = entry or self._selected()
        if entry is None:
            self.status.set_text("select a file first")
            return
        if entry.is_dir:
            self.status.set_text("pick a file — folder download isn't supported")
            return

        dest_dir = os.path.expanduser("~/Downloads")
        os.makedirs(dest_dir, exist_ok=True)
        self.status.set_text(f"downloading {entry.name} …")
        argv = scp_argv([f"{self.alias}:{shlex.quote(self._remote(entry))}",
                         dest_dir + "/"])

        def done(_out, err):
            if err is not None:
                self.status.set_text(f"⚠ {err}")
            else:
                self.status.set_text(f"✓ ~/Downloads/{entry.name}")

        run_argv_async(argv, done)

    def upload(self):
        dialog = Gtk.FileDialog(title="Upload to " + self.alias)

        def chosen(dlg, res):
            try:
                gfile = dlg.open_finish(res)
            except GLib.Error:
                return  # cancelled
            path = gfile.get_path()
            if path:
                self.upload_paths([path])
            else:
                self.status.set_text("⚠ that file isn't on the local filesystem")

        dialog.open(self.tab.window, None, chosen)

    def _on_drop(self, _target, value, _x, _y):
        paths = [f.get_path() for f in value.get_files() if f.get_path()]
        if not paths:
            self.status.set_text("⚠ those files aren't on the local filesystem")
            return False
        self.upload_paths(paths)
        return True

    def upload_paths(self, paths):
        remaining = list(paths)
        self.status.set_text(
            f"uploading {len(remaining)} item(s) to {self.path} …")

        def step(_out=None, err=None):
            if err is not None:
                self.status.set_text(f"⚠ {err}")
                self.refresh()
                return
            if not remaining:
                self.status.set_text(f"✓ uploaded {len(paths)} item(s)")
                self.refresh()
                return
            local = remaining.pop(0)
            flags = ["-r"] if os.path.isdir(local) else []
            argv = scp_argv(flags + [local,
                                     f"{self.alias}:{shlex.quote(self.path)}/"])
            run_argv_async(argv, step)

        step()


# --------------------------------------------------------------------------
# terminals
# --------------------------------------------------------------------------

AUTH_FAILURE_MARKERS = (
    "permission denied",
    "authentication failed",
    "too many authentication failures",
    "no supported authentication methods",
)

PASSWORD_PROMPT_RE = re.compile(
    r"(password|passphrase)[^\n]*:\s*$", re.IGNORECASE)


class PaneChooser(Gtk.Box):
    """Shown in a freshly split pane: pick what this pane should connect to."""

    def __init__(self, window, same_alias, on_pick):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.window = window
        self.on_pick = on_pick
        self.set_margin_top(10)
        self.set_margin_bottom(10)
        self.set_margin_start(10)
        self.set_margin_end(10)

        title = Gtk.Label(xalign=0.0)
        title.set_markup("<b>Connect this pane to…</b>")
        self.append(title)

        if same_alias:
            same = Gtk.Button(label=f"Same host — {same_alias}")
            same.add_css_class("suggested-action")
            same.connect("clicked", lambda _b: self.on_pick("same", None))
            self.append(same)
            self.default_button = same
        else:
            self.default_button = None

        local = Gtk.Button(label="Local shell")
        local.connect("clicked", lambda _b: self.on_pick("local", None))
        self.append(local)
        if self.default_button is None:
            self.default_button = local

        self.search = Gtk.SearchEntry(placeholder_text="or search a host…")
        self.search.connect("search-changed", lambda _e: self._filter())
        self.search.connect("activate", lambda _e: self._activate_first())
        self.append(self.search)

        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.listbox.connect(
            "row-activated", lambda _l, row: self.on_pick("host", row.host))
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.listbox)
        self.append(scroller)

        for group, members in window.sidebar.groups.items():
            for host in members:
                self.listbox.append(self._row(host, group))
        self.listbox.set_filter_func(self._matches)

    def _row(self, host, group):
        row = Gtk.ListBoxRow()
        row.host = host
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        dot = Gtk.Label()
        dot.set_markup(f'<span color="{group_color(group)}">●</span>')
        box.append(dot)
        name = Gtk.Label(label=host.alias, xalign=0.0, hexpand=True)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        box.append(name)
        addr = Gtk.Label(label=host.hostname)
        addr.add_css_class("dim-label")
        addr.set_ellipsize(Pango.EllipsizeMode.END)
        addr.set_max_width_chars(16)
        box.append(addr)
        row.set_child(box)
        return row

    def _matches(self, row):
        query = self.search.get_text().strip().lower()
        return not query or query in row.host.haystack

    def _filter(self):
        self.listbox.invalidate_filter()

    def _activate_first(self):
        row = self.listbox.get_first_child()
        while row is not None:
            if isinstance(row, Gtk.ListBoxRow) and self._matches(row):
                self.on_pick("host", row.host)
                return
            row = row.get_next_sibling()


class TerminalPane(Gtk.Box):
    """One VTE. A tab holds a tree of these once you start splitting."""

    def __init__(self, tab, title, argv, accent, group, alias=None,
                 connect_opts=None, pending=False):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.tab = tab
        self.window = tab.window
        self.title = title
        self.argv = argv
        self.accent = accent
        self.group = group
        self.alias = alias
        self.connect_opts = dict(connect_opts or {})
        self.alive = False
        self.mux_retried = False
        self.retry_bar = None
        self._dead_keys = None
        self._pending_password = None
        self._password_handler = None

        self.term = Vte.Terminal()
        self.term.set_vexpand(True)
        self.term.set_hexpand(True)
        self.term.set_mouse_autohide(True)
        self.apply_appearance()
        self.term.connect("child-exited", self._on_child_exited)
        self.term.connect("commit", self._on_commit)

        zoom = Gtk.EventControllerScroll(
            flags=Gtk.EventControllerScrollFlags.VERTICAL)
        zoom.connect("scroll", self._on_scroll)
        self.term.add_controller(zoom)

        focus = Gtk.EventControllerFocus()
        focus.connect("enter", lambda _c: self.window.set_active_pane(self))
        self.term.add_controller(focus)

        self._menu = None
        right_click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        right_click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        right_click.connect("pressed", self._on_term_right_click)
        self.term.add_controller(right_click)

        # Copy on select, done by us rather than left to the desktop.
        #
        # VTE only ever publishes a selection as PRIMARY. What made a selection
        # feel like it "auto-copied" was Klipper recording PRIMARY into its
        # history, whose top entry is what Ctrl+V pastes -- and on Wayland
        # that relay drops selections often enough that the paste is the
        # previous one. Writing CLIPBOARD ourselves takes Klipper out of the
        # path. Once per selection, on button release: a drag changes the
        # selection on every pointer move, and copying each step would fill
        # the clipboard history with fragments.
        self._copy_pending = False
        self._button_down = False
        self._copy_timer = 0
        self._last_copied = None
        self.term.connect("selection-changed", self._on_selection_changed)
        buttons = Gtk.EventControllerLegacy()
        buttons.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        buttons.connect("event", self._on_term_button_event)
        self.term.add_controller(buttons)
        self._watch_clipboards()

        self.header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.header.add_css_class("ussh-panel")
        self.header.set_margin_start(8)
        self.header.set_margin_end(8)
        self.header.set_margin_top(2)
        self.header.set_margin_bottom(2)
        self.header_dot = Gtk.Label()
        self.header.append(self.header_dot)
        self.header_label = Gtk.Label(xalign=0.0, hexpand=True)
        self.header_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.header.append(self.header_label)
        self.header.set_visible(False)   # pointless when the tab has one pane
        self.append(self.header)

        self.scroller = Gtk.ScrolledWindow(vexpand=True)
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_child(self.term)
        self.append(self.scroller)

        self.chooser = None
        if pending:
            self._show_chooser()
        else:
            self.spawn()

    # -- "what should this pane be?" --------------------------------------

    def _show_chooser(self):
        self.scroller.set_visible(False)
        self.chooser = PaneChooser(self.window, self.alias, self._picked)
        self.append(self.chooser)
        if self.chooser.default_button is not None:
            self.chooser.default_button.grab_focus()

    def _picked(self, kind, host):
        if self.chooser is not None:
            self.remove(self.chooser)
            self.chooser = None
        self.scroller.set_visible(True)

        if kind == "local":
            shell = os.environ.get("SHELL", "/bin/bash")
            self.title, self.alias, self.group = "local", None, "local"
            self.accent, self.connect_opts = "#8a8a8a", {}
            self.argv = [shell]
        elif kind == "host" and host is not None:
            self.title, self.alias, self.group = host.alias, host.alias, host.group
            self.accent = group_color(host.group)
            self.connect_opts = dict(self.tab.connect_opts)
            self.argv = ssh_argv(host.alias, **self.connect_opts)
        # "same" keeps the argv it was cloned with
        self.tab.refresh_identity()
        self.spawn()

    def _on_scroll(self, controller, _dx, dy):
        """Ctrl+scroll resizes; everything else is the terminal's own scroll."""
        if not (controller.get_current_event_state()
                & Gdk.ModifierType.CONTROL_MASK):
            return False
        self.window.zoom_font(-1 if dy > 0 else 1)
        return True

    # -- clipboard ---------------------------------------------------------

    _clipboards_watched = False

    def _watch_clipboards(self):
        """Debug only: log when CLIPBOARD or PRIMARY changes owner.

        `is_local` says whether this process still holds the selection; a
        change to not-local right after our copy is another client (Klipper,
        say) taking the clipboard over, and PRIMARY going not-local is what
        makes VTE drop its selection highlight.
        """
        if "clipboard" not in DEBUG and "all" not in DEBUG:
            return
        if TerminalPane._clipboards_watched:
            return
        TerminalPane._clipboards_watched = True
        for name, clip in (("CLIPBOARD", self.term.get_clipboard()),
                           ("PRIMARY", self.term.get_primary_clipboard())):
            clip.connect("changed", lambda c, n=name: debug(
                "clipboard", f"{n} changed local={c.is_local()} "
                f"formats={c.get_formats().to_string()}"))

    def post_to_chat(self, agent=False):
        """Send the selection to Ultimate Chat, with a note.

        ``agent`` preselects the channel that answers, so "Ask Viktor about
        this" is: highlight the error, right-click, type the question, send.
        The reply comes back in the chat, and the chat can open a shell
        back on this host.
        """
        if not self.term.get_has_selection():
            return
        text = self.term.get_text_selected(Vte.Format.TEXT) or ""
        if not text.strip():
            self.window.flash("nothing selected")
            return
        ChatPostDialog(self.window, text, host=self.alias,
                       agent=agent).present()

    def copy_selection(self):
        """Put the selected text on CLIPBOARD as a static snapshot.

        A plain string provider, not VTE's own: the text is serialised the
        instant a reader asks, so a reader never depends on the terminal's
        state at that later moment. Written once per distinct text while we
        still own the clipboard -- re-offering the same bytes only cancels
        whatever read was in flight.
        """
        if not self.term.get_has_selection():
            debug("clipboard", "copy: no selection")
            return False
        text = self.term.get_text_selected(Vte.Format.TEXT)
        if not text:
            debug("clipboard", "copy: selection has no text")
            return False
        clipboard = self.term.get_clipboard()
        if text == self._last_copied and clipboard.is_local():
            debug("clipboard", f"copy: already ours ({len(text)} chars)")
            return True
        clipboard.set_content(Gdk.ContentProvider.new_for_value(
            GObject.Value(str, text)))
        self._last_copied = text
        debug("clipboard", f"copy: wrote {len(text)} chars {text[:40]!r}")
        return True

    def paste_clipboard(self):
        clip = self.term.get_clipboard()
        debug("clipboard", f"paste: local={clip.is_local()} "
              f"formats={clip.get_formats().to_string()}")
        self.term.paste_clipboard()

    def select_all(self):
        self.term.select_all()

    def _on_selection_changed(self, _term):
        has = self.term.get_has_selection()
        debug("clipboard", f"selection-changed has={has} "
              f"button_down={self._button_down}")
        if not self.window.settings.get("copy_on_select", True):
            return
        if not has:
            return                      # a cleared selection keeps the clipboard
        if self._button_down:
            self._copy_pending = True   # still dragging; copy on release
        else:
            self._schedule_copy()       # select-all, or a click that landed late

    def _schedule_copy(self):
        """One clipboard write per gesture, after the selection settles."""
        if self._copy_timer:
            GLib.source_remove(self._copy_timer)
        self._copy_timer = GLib.timeout_add(COPY_SETTLE_MS, self._copy_settled)

    def _copy_settled(self):
        self._copy_timer = 0
        if self._button_down:
            self._copy_pending = True   # a new drag began; wait for it
        else:
            self.copy_selection()
        return GLib.SOURCE_REMOVE

    def _on_term_button_event(self, controller, event):
        if event is None:
            # PyGObject passes None for event kinds it has no wrapper for.
            event = controller.get_current_event()
            if event is None:
                return False
        kind = event.get_event_type()
        if kind == Gdk.EventType.BUTTON_PRESS:
            if event.get_button() == Gdk.BUTTON_PRIMARY:
                debug("clipboard", "button press")
                self._button_down = True
                self._copy_pending = False
                if self._copy_timer:
                    GLib.source_remove(self._copy_timer)
                    self._copy_timer = 0
        elif kind == Gdk.EventType.BUTTON_RELEASE:
            if event.get_button() == Gdk.BUTTON_PRIMARY:
                debug("clipboard", f"button release pending={self._copy_pending}")
                self._button_down = False
                if self._copy_pending:
                    self._copy_pending = False
                    self._schedule_copy()
        return False                    # never swallow; VTE still selects

    def _on_term_right_click(self, gesture, _n, x, y):
        """Right-click menu over the terminal.

        Claimed in the capture phase so the press never reaches VTE, which
        would otherwise drop the selection the menu is about to copy.
        """
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self.window.set_active_pane(self)
        self.term.grab_focus()

        items = [
            ("Copy", self.copy_selection, self.term.get_has_selection()),
            ("Paste", self.paste_clipboard, True),
            (None, None, False),
            ("Select all", self.select_all, True),
        ]
        if chatbridge.available():
            has = self.term.get_has_selection()
            items += [
                (None, None, False),
                ("Post selection to Ultimate Chat…",
                 lambda: self.post_to_chat(), has),
                (f"Ask {chatbridge.AGENT_CHANNEL.title()} about this…",
                 lambda: self.post_to_chat(agent=True), has),
            ]

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        popover = Gtk.Popover()
        popover.set_has_arrow(False)
        popover.set_child(box)
        for label, handler, enabled in items:
            if label is None:
                box.append(Gtk.Separator())
                continue
            btn = Gtk.Button(label=label)
            btn.add_css_class("flat")
            btn.set_sensitive(enabled)
            inner = btn.get_child()
            if isinstance(inner, Gtk.Label):
                inner.set_xalign(0.0)
            btn.connect("clicked",
                        lambda _b, h=handler: (popover.popdown(), h()))
            box.append(btn)

        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_parent(self.term)
        popover.set_pointing_to(rect)
        # focus goes back to the terminal, else typing lands nowhere
        popover.connect("closed",
                        lambda p: (p.unparent(), self.term.grab_focus()))
        self._menu = popover
        popover.popup()

    def refresh_header(self, show, focused):
        self.header.set_visible(show)
        if not show:
            return
        self.header_dot.set_markup(f'<span color="{self.accent}">●</span>')
        name = GLib.markup_escape_text(self.title or "—")
        self.header_label.set_markup(
            f"<b>{name}</b>" if focused else f"<span alpha='55%'>{name}</span>")

    def apply_appearance(self):
        settings = self.window.settings
        self.term.set_font(Pango.FontDescription(settings["font"]))
        self.term.set_scrollback_lines(settings["scrollback"])
        fg, bg = (FG, BG) if self.window.dark_terminal() else (LIGHT_FG, LIGHT_BG)
        background = rgba(bg)
        see_through = opacity_of(settings) < 1.0
        self.term.set_clear_background(not see_through)
        if see_through:
            self.term.add_css_class("ussh-see-through")
        else:
            self.term.remove_css_class("ussh-see-through")
        if settings.get("colors", True):
            palette = [rgba(c) for c in PALETTE]
        else:
            # Colours off: collapse the whole palette onto the foreground so
            # ls, grep and prompts render as plain text. Bold and underline
            # still come through, and nothing is asked of the remote shell.
            palette = [rgba(fg)] * 16
        self.term.set_colors(rgba(fg), background, palette)

    def _preflight(self):
        """Things ssh is about to do that the config does not look like it says.

        Both failures this catches surface as "Permission denied (publickey)",
        which describes neither of them, so they are worth saying plainly
        before the attempt rather than leaving them to be inferred after it.
        """
        if not self.alias:
            return []
        resolved = resolve_ssh_settings(self.alias)
        if not resolved:
            return []

        notes = []
        config = getattr(getattr(self.window, "sidebar", None), "config", None)
        view = config.find(self.alias) if config is not None else None
        if view is not None:
            for key in ("User", "Port", "HostName"):
                declared = (view.block.get(key) or "").strip()
                actual = resolved.get(key.lower(), "")
                if declared and actual and declared != actual:
                    notes.append(
                        f"this host sets {key} {declared}, but ssh will use "
                        f"{actual} — a Host * block above it wins. "
                        "Menu → “Fix defaults order” moves that block to the "
                        "end, where it belongs.")

        for path in resolved.get("identityfile", []):
            report = sshconfig.inspect_identity_file(path)
            # ssh lists its built-in default key paths whether they exist or
            # not, so absence is normal here and says nothing
            if report is None or report.ok or "missing" in report.codes:
                continue
            notes.append(f"key {path} {report.summary()}.")
        return notes

    def spawn(self):
        self._disarm_dead_keys()
        for note in self._preflight():
            self.term.feed(f"\x1b[33m⚠ {note}\x1b[0m\r\n".encode())
        self.term.spawn_async(
            Vte.PtyFlags.DEFAULT,
            os.path.expanduser("~"),
            self.argv,
            None,
            GLib.SpawnFlags.DEFAULT,
            None,
            None,
            -1,
            None,
            self._on_spawned,
        )

    def _on_spawned(self, _term, pid, error, *_):
        if error is not None:
            self.term.feed(f"\r\n\x1b[31mspawn failed: {error}\x1b[0m\r\n".encode())
            return
        self.alive = True
        self.term.grab_focus()

    def reconnect(self, argv=None, password=None):
        self._clear_retry_bar()
        if argv is not None:
            self.argv = argv
        self.term.reset(True, True)
        self.term.feed(f"\x1b[36m[reconnecting to {self.title}…]\x1b[0m\r\n".encode())
        if password:
            self._arm_password(password)
        self.spawn()

    # -- typing a password at ssh's own prompt ----------------------------

    def _arm_password(self, password):
        """Type the password into the pty when ssh asks for it.

        Deliberately not sshpass and not an askpass helper: the password never
        reaches argv (world-readable in ps), never reaches disk, and never
        reaches an environment variable. It lives in this process and is typed
        once, exactly as if you had typed it, then dropped.
        """
        self._pending_password = password
        self._password_handler = self.term.connect(
            "contents-changed", self._maybe_send_password)

    def _disarm_password(self):
        self._pending_password = None
        if self._password_handler is not None:
            self.term.disconnect(self._password_handler)
            self._password_handler = None

    def _tail_text(self, rows=3):
        col, row = self.term.get_cursor_position()
        got = self.term.get_text_range_format(
            Vte.Format.TEXT, max(0, row - rows), 0, row, max(col, 1))
        body = got[0] if isinstance(got, tuple) else got
        return body or ""

    def _maybe_send_password(self, _term):
        if not self._pending_password:
            return
        if not PASSWORD_PROMPT_RE.search(self._tail_text().rstrip("\n")):
            return
        password = self._pending_password
        self._disarm_password()      # one shot: never re-send on a retry prompt
        self.term.feed_child((password + "\n").encode())

    # -- failure handling --------------------------------------------------

    def _arm_dead_keys(self):
        """While the session is dead, R and Q act directly on the pane.

        Capture phase, so the keys are ours before VTE sees them -- and only
        ever while no child is running, so a live shell is never affected.
        """
        if self._dead_keys is not None:
            return
        controller = Gtk.EventControllerKey()
        controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        controller.connect("key-pressed", self._on_dead_key)
        self.term.add_controller(controller)
        self._dead_keys = controller

    def _disarm_dead_keys(self):
        if self._dead_keys is not None:
            self.term.remove_controller(self._dead_keys)
            self._dead_keys = None

    def _on_dead_key(self, _controller, keyval, _code, state):
        if self.alive or (state & Gdk.ModifierType.CONTROL_MASK):
            return False
        if keyval in (Gdk.KEY_r, Gdk.KEY_R):
            self.reconnect(argv=self.connect_argv() if self.alias else None)
            return True
        if keyval in (Gdk.KEY_q, Gdk.KEY_Q):
            self.tab.close_pane(self)
            return True
        return False

    def connect_argv(self, **overrides):
        """Rebuild this pane's ssh command with adjusted options."""
        opts = dict(self.connect_opts)
        opts.update(overrides)
        return ssh_argv(self.alias, **opts)

    def _looks_like_mux_failure(self):
        tail = self._tail_text(rows=40).lower()
        return any(marker in tail for marker in MUX_FAILURE_MARKERS)

    def _looks_like_auth_failure(self):
        # Generous window: a login banner or MOTD can push ssh's complaint
        # well up the screen before the process finally exits.
        tail = self._tail_text(rows=40).lower()
        return any(marker in tail for marker in AUTH_FAILURE_MARKERS)

    def _clear_retry_bar(self):
        if self.retry_bar is not None:
            self.remove(self.retry_bar)
            self.retry_bar = None

    def _show_retry_bar(self, message):
        self._clear_retry_bar()
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.set_margin_top(6)
        bar.set_margin_bottom(6)
        bar.set_margin_start(10)
        bar.set_margin_end(10)

        label = Gtk.Label(xalign=0.0, hexpand=True, wrap=True)
        label.set_markup(
            f"<span color='#ff8080'><b>{GLib.markup_escape_text(message)}</b></span>")
        bar.append(label)

        creds = Gtk.Button(label="Username / password…")
        creds.add_css_class("suggested-action")
        creds.connect("clicked", lambda _b: self.window.credentials_dialog(self))
        bar.append(creds)

        again = Gtk.Button(label="Retry")
        again.connect("clicked", lambda _b: self.reconnect())
        bar.append(again)

        close = Gtk.Button(icon_name="window-close-symbolic")
        close.add_css_class("flat")
        close.connect("clicked", lambda _b: self._clear_retry_bar())
        bar.append(close)

        self.append(bar)
        self.retry_bar = bar

    def _on_child_exited(self, _term, status):
        self.alive = False
        self._disarm_password()
        # VTE hands us the raw waitpid status, not an exit code: ssh failing
        # with 255 arrives here as 65280. Decode it or the message is noise.
        try:
            code = os.waitstatus_to_exitcode(status)
        except ValueError:
            code = status
        if code == 0:
            self.tab.close_pane(self)
            return
        why = f"killed by signal {-code}" if code < 0 else f"exited {code}"
        if code == 255:
            why += " (ssh: connection or auth failure)"
        self.term.feed(
            f"\r\n\x1b[33m[{self.title} {why}]\x1b[0m\r\n"
            f"\x1b[1m  press \x1b[32mR\x1b[0m\x1b[1m to reconnect"
            f"  ·  \x1b[31mQ\x1b[0m\x1b[1m to close this pane\x1b[0m\r\n"
            .encode()
        )
        self._arm_dead_keys()
        # A dead or stale control master is our problem, not the user's:
        # drop the shared socket and reconnect on a private connection.
        if self.alias and self._looks_like_mux_failure() and not self.mux_retried:
            self.mux_retried = True
            removed = sweep_control_sockets()
            self.connect_opts["no_mux"] = True
            self.term.feed(
                ("\r\n\x1b[36m[shared SSH connection was broken"
                 + (f"; cleaned {removed} stale socket(s)" if removed else "")
                 + " — reconnecting without connection sharing]\x1b[0m\r\n"
                 ).encode())
            GLib.timeout_add(300, lambda: (
                self.reconnect(argv=self.connect_argv()), False)[1])
            return

        if self.alias and code == 255 and self._looks_like_auth_failure():
            self._show_retry_bar(
                f"{self.title}: authentication failed. "
                "Wrong username is the usual cause.")

    def _on_commit(self, _term, text, _size):
        """Every keystroke the user types passes through here -- the hook the
        broadcast feature hangs on."""
        self.window.broadcast_from(self, text)


class TerminalTab(Gtk.Box):
    """A notebook page: a tree of TerminalPanes, plus an optional explorer."""

    def __init__(self, window, host, argv, accent, is_local=False,
                 connect_opts=None, title=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.host = host
        self.title = title or (host.alias if host else "local")
        self.accent = accent
        self.is_local = is_local
        self.explorer = None

        self.hpaned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.hpaned.set_vexpand(True)
        self.append(self.hpaned)

        group = host.group if host else "local"
        self.connect_opts = dict(connect_opts or {})
        first = TerminalPane(self, self.title, argv, accent, group,
                             alias=host.alias if host else None,
                             connect_opts=self.connect_opts)
        self.hpaned.set_start_child(first)
        self.hpaned.set_resize_start_child(True)

    # -- pane tree -------------------------------------------------------

    def panes(self):
        found = []

        def walk(w):
            if isinstance(w, TerminalPane):
                found.append(w)
            elif isinstance(w, Gtk.Paned):
                for child in (w.get_start_child(), w.get_end_child()):
                    if child is not None:
                        walk(child)

        start = self.hpaned.get_start_child()
        if start is not None:
            walk(start)
        return found

    def _replace(self, old, new):
        """Swap a widget for another wherever it sits in the pane tree."""
        parent = old.get_parent()
        if parent is self.hpaned:
            self.hpaned.set_start_child(new)
        elif isinstance(parent, Gtk.Paned):
            if parent.get_start_child() is old:
                parent.set_start_child(new)
            else:
                parent.set_end_child(new)

    def split(self, pane, orientation, pending=True):
        """pending=True gives the new pane a picker instead of cloning blindly."""
        if pane is None or pane.get_parent() is None:
            return
        new = TerminalPane(self, pane.title, list(pane.argv), pane.accent,
                           pane.group, alias=pane.alias,
                           connect_opts=pane.connect_opts, pending=pending)
        paned = Gtk.Paned(orientation=orientation)
        # Both children resize with the window and neither may be squeezed to
        # nothing, so the divider stays draggable across its whole range.
        paned.set_resize_start_child(True)
        paned.set_resize_end_child(True)
        paned.set_shrink_start_child(False)
        paned.set_shrink_end_child(False)
        paned.set_wide_handle(True)   # a grabbable target, not a hairline

        # Remember the space the pane occupied: a fresh Paned has no
        # allocation yet, so without this the divider starts pinned at 0.
        extent = (pane.get_width() if orientation == Gtk.Orientation.HORIZONTAL
                  else pane.get_height())

        self._replace(pane, paned)
        paned.set_start_child(pane)   # reparents cleanly in GTK4
        paned.set_end_child(new)
        if extent > 40:
            paned.set_position(extent // 2)
        if not pending:
            new.term.grab_focus()
        self.refresh_identity()
        if self.explorer is not None:
            # the new terminal must be watched too, or following would ignore
            # a cd made in the pane you just created
            self.explorer.refresh_following()

    def equalize(self, widget=None):
        """Reset every divider in this tab to a 50/50 split."""
        widget = self.hpaned.get_start_child() if widget is None else widget
        if isinstance(widget, Gtk.Paned):
            extent = (widget.get_width()
                      if widget.get_orientation() == Gtk.Orientation.HORIZONTAL
                      else widget.get_height())
            if extent > 40:
                widget.set_position(extent // 2)
            for child in (widget.get_start_child(), widget.get_end_child()):
                self.equalize(child)

    def close_pane(self, pane):
        siblings = self.panes()
        if len(siblings) <= 1:
            self.window.close_tab(self)
            return

        parent = pane.get_parent()
        if not isinstance(parent, Gtk.Paned):
            return
        other = (parent.get_end_child() if parent.get_start_child() is pane
                 else parent.get_start_child())

        # Hand focus to a survivor *before* detaching: pulling the focused
        # widget out from under GtkPaned makes it complain about a nil child.
        survivor = self._first_pane_in(other)
        if survivor is not None:
            survivor.term.grab_focus()

        parent.set_start_child(None)
        parent.set_end_child(None)
        self._replace(parent, other)

        if survivor is not None:
            survivor.term.grab_focus()
        self.refresh_identity()

    @staticmethod
    def _first_pane_in(widget):
        if isinstance(widget, TerminalPane):
            return widget
        if isinstance(widget, Gtk.Paned):
            for child in (widget.get_start_child(), widget.get_end_child()):
                found = TerminalTab._first_pane_in(child)
                if found is not None:
                    return found
        return None

    # -- explorer --------------------------------------------------------

    def toggle_explorer(self):
        if self.is_local or self.host is None:
            return False
        if self.explorer is not None:
            self.hpaned.set_end_child(None)
            self.explorer = None
            return False
        self.explorer = ExplorerPane(self, self.host.alias, self.cd_terminal)
        self.hpaned.set_end_child(self.explorer)
        self.hpaned.set_resize_end_child(False)
        width = self.hpaned.get_width()
        if width > 400:
            self.hpaned.set_position(width - 340)
        return True

    def focused_pane(self):
        panes = self.panes()
        active = self.window.active_pane
        if active is not None and active in panes:
            return active
        return panes[0] if panes else None

    def refresh_identity(self):
        """Keep the pane strips and the tab label naming what's on screen."""
        panes = self.panes()
        focus = self.focused_pane()
        for pane in panes:
            pane.refresh_header(len(panes) > 1, pane is focus)
        if focus is None or not hasattr(self, "label_text"):
            return
        others = len(panes) - 1
        text = focus.title + (f"  +{others}" if others else "")
        self.label_text.set_text(text)
        self.label_text.set_width_chars(min(len(text), 22))
        self.label_dot.set_markup(f'<span color="{focus.accent}">●</span>')

    def cd_terminal(self, path):
        pane = self.window.active_pane
        if pane is None or pane.tab is not self:
            panes = self.panes()
            pane = panes[0] if panes else None
        if pane is not None:
            pane.term.feed_child(f"cd {shlex.quote(path)}\n".encode())
            pane.term.grab_focus()

    # -- tab label -------------------------------------------------------

    def make_label(self, on_close):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        dot = Gtk.Label()
        dot.set_markup(f'<span color="{self.accent}">●</span>')
        box.append(dot)
        self.label_dot = dot

        label = Gtk.Label(label=self.title)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        # width_chars is the floor; without it an ellipsizing label reports a
        # ~1-char minimum and the notebook squeezes every tab down to "...".
        label.set_width_chars(min(len(self.title), 22))
        label.set_max_width_chars(22)
        box.append(label)
        self.label_text = label

        close = Gtk.Button(icon_name="window-close-symbolic")
        close.add_css_class("flat")
        close.set_has_frame(False)
        close.connect("clicked", lambda _b: on_close(self))
        box.append(close)

        return box


# --------------------------------------------------------------------------
# sidebar
# --------------------------------------------------------------------------

class SessionSidebar(Gtk.Box):
    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.window = window
        self.on_connect = window.connect_to
        self.on_connect_group = window.connect_group
        self.query = ""
        self.expanded = set()
        self.groups = {}
        self.config = None
        self._popover = None
        self.compact = window.settings.get("density", "compact") == "compact"

        self.search = Gtk.SearchEntry(
            placeholder_text="Filter hosts  (Ctrl+Shift+L)")
        self.search.set_margin_top(6)
        self.search.set_margin_bottom(6)
        self.search.set_margin_start(6)
        self.search.set_margin_end(6)
        self.search.connect("search-changed", self._on_search)
        self.search.connect("activate", self._on_search_activate)
        self.append(self.search)

        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.BROWSE)
        self.listbox.set_filter_func(self._filter)
        self.listbox.connect("row-activated", self._on_row_activated)

        right_click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        right_click.connect("pressed", self._on_right_click)
        self.listbox.add_controller(right_click)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.listbox)
        self.append(scroller)

        self.status = Gtk.Label(xalign=0.0)
        self.status.add_css_class("dim-label")
        self.status.set_margin_start(10)
        self.status.set_margin_bottom(6)
        self.append(self.status)

        self.reload()

    def reload(self):
        while (row := self.listbox.get_first_child()) is not None:
            self.listbox.remove(row)

        self.config = SshConfig.load(SSH_CONFIG)
        self.groups = self.config.groups()
        hosts = [h for members in self.groups.values() for h in members]

        for group, members in self.groups.items():
            self.listbox.append(self._group_row(group, len(members)))
            for h in members:
                self.listbox.append(self._host_row(h))

        self.status.set_text(f"{len(hosts)} hosts · {len(self.groups)} groups")
        self.listbox.invalidate_filter()

    def find_host(self, alias):
        for members in self.groups.values():
            for h in members:
                if h.alias == alias:
                    return h
        return None

    def _group_row(self, group, count):
        row = Gtk.ListBoxRow()
        row.host = None
        row.group = group

        pad = 1 if self.compact else 4
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_margin_top(pad)
        box.set_margin_bottom(pad)
        box.set_margin_start(6)
        box.set_margin_end(6)

        row.arrow = Gtk.Label(xalign=0.0)
        row.arrow.set_markup("▸")
        box.append(row.arrow)

        dot = Gtk.Label()
        dot.set_markup(f'<span color="{group_color(group)}">●</span>')
        box.append(dot)

        name = Gtk.Label(xalign=0.0, hexpand=True)
        name.set_markup(f"<b>{GLib.markup_escape_text(group)}</b>")
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.set_tooltip_text(group)     # the sidebar truncates long names
        box.append(name)

        openall = Gtk.Button(icon_name="list-add-symbolic")
        openall.add_css_class("flat")
        openall.set_tooltip_text(f"Open all {count} hosts in tabs")
        openall.connect("clicked",
                        lambda _b, g=group: self.on_connect_group(g))
        box.append(openall)

        tally = Gtk.Label(label=str(count))
        tally.add_css_class("dim-label")
        box.append(tally)

        row.set_child(box)
        return row

    def _host_row(self, host):
        row = Gtk.ListBoxRow()
        row.host = host
        row.group = host.group

        # Compact keeps the hostname on the same line; comfortable stacks it,
        # which doubles every row's height and reads as double spacing.
        compact = self.compact
        box = Gtk.Box(
            orientation=(Gtk.Orientation.HORIZONTAL if compact
                         else Gtk.Orientation.VERTICAL),
            spacing=8 if compact else 0)
        box.set_margin_top(0 if compact else 3)
        box.set_margin_bottom(0 if compact else 3)
        box.set_margin_start(28)
        box.set_margin_end(6)

        alias = Gtk.Label(label=host.alias, xalign=0.0)
        alias.set_ellipsize(Pango.EllipsizeMode.END)
        if compact:
            alias.set_hexpand(True)
        box.append(alias)

        if host.hostname != host.alias:
            sub = Gtk.Label(label=host.hostname, xalign=0.0)
            sub.add_css_class("dim-label")
            sub.set_ellipsize(Pango.EllipsizeMode.END)
            if compact:
                sub.set_max_width_chars(18)
            box.append(sub)

        row.set_tooltip_text(f"{host.alias}  ·  {host.hostname}")
        row.set_child(box)
        return row

    def _filter(self, row):
        if self.query:
            # searching flattens the tree -- headers just get in the way
            return row.host is not None and self.query in row.host.haystack
        return row.host is None or row.group in self.expanded

    def _on_search(self, entry):
        self.query = entry.get_text().strip().lower()
        self.listbox.invalidate_filter()

    def _on_search_activate(self, _entry):
        for row in self._visible_rows():
            if row.host is not None:
                self.on_connect(row.host)
                return

    def _visible_rows(self):
        row = self.listbox.get_first_child()
        while row is not None:
            if isinstance(row, Gtk.ListBoxRow) and self._filter(row):
                yield row
            row = row.get_next_sibling()

    def _on_row_activated(self, _listbox, row):
        if row.host is not None:
            self.on_connect(row.host)
            return
        if row.group in self.expanded:
            self.expanded.discard(row.group)
            row.arrow.set_markup("▸")
        else:
            self.expanded.add(row.group)
            row.arrow.set_markup("▾")
        self.listbox.invalidate_filter()

    def focus_search(self):
        self.search.grab_focus()
        self.search.select_region(0, -1)

    # -- context menus ----------------------------------------------------

    def _on_right_click(self, _gesture, _n_press, x, y):
        row = self.listbox.get_row_at_y(int(y))
        if row is None:
            return
        self.listbox.select_row(row)
        w = self.window

        if row.host is not None:
            view = row.host
            items = [
                ("Connect", lambda: w.connect_to(view)),
                ("Connect in tmux", lambda: w.connect_with(view, "tmux")),
                ("Connect in screen", lambda: w.connect_with(view, "screen")),
                ("Run Claude Code (in tmux)", lambda: w.run_claude(view)),
                ("Copy “ssh " + view.alias + "”", lambda: w.copy_ssh_command(view)),
                (None, None),
                ("Edit connection…", lambda: w.edit_host(view)),
                ("Duplicate", lambda: w.duplicate_host(view)),
                ("Move to category…", lambda: w.move_host_dialog(view)),
                ("Delete…", lambda: w.delete_host(view)),
                (None, None),
                ("Manage all connections…", w.open_manager),
            ]
        else:
            group = row.group
            count = len(self.groups.get(group, []))
            items = [
                (f"Open all {count} hosts", lambda: w.connect_group(group)),
                (None, None),
                ("New connection here…", lambda: w.new_host_in(group)),
                ("Rename category…", lambda: w.rename_group(group)),
                ("Delete category…", lambda: w.delete_group(group)),
                (None, None),
                ("New category…", w.new_group),
                ("Manage all connections…", w.open_manager),
            ]

        self._popup_menu(items, x, y)

    def _popup_menu(self, items, x, y):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        popover = Gtk.Popover()
        popover.set_has_arrow(False)
        popover.set_child(box)

        for label, handler in items:
            if label is None:
                box.append(Gtk.Separator())
                continue
            btn = Gtk.Button(label=label)
            btn.add_css_class("flat")
            child = btn.get_child()
            if isinstance(child, Gtk.Label):
                child.set_xalign(0.0)
            btn.connect("clicked", lambda _b, h=handler: (popover.popdown(), h()))
            box.append(btn)

        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_parent(self.listbox)
        popover.set_pointing_to(rect)
        popover.connect("closed", lambda p: p.unparent())
        self._popover = popover      # keep it alive until it closes
        popover.popup()


# --------------------------------------------------------------------------
# window
# --------------------------------------------------------------------------

class HostStatusBar(Gtk.Box):
    """The bottom readout: CPU, memory, disk and network for the focused pane.

    Only the pane you are looking at is polled -- not every tab, and not every
    pane -- because each reading is a command on the far end, and thirty idle
    tabs quietly running df every few seconds is how a monitoring feature
    becomes the load it was meant to watch.

    Readings are kept per host, so switching tabs shows the last numbers
    immediately instead of an empty bar waiting on a round trip.
    """

    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.window = window
        self.readings = {}         # host key -> last reading
        self.inflight = set()
        self.timer = None
        self.ticks = 0
        self.key = None

        self.append(Gtk.Separator())
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_margin_start(10)
        row.set_margin_end(10)
        row.set_margin_top(3)
        row.set_margin_bottom(3)
        self.label = Gtk.Label(xalign=0.0, hexpand=True)
        self.label.set_ellipsize(Pango.EllipsizeMode.END)
        self.label.add_css_class("monospace")   # keeps the meters in a column
        row.append(self.label)
        self.append(row)

        click = Gtk.GestureClick()
        click.connect("pressed", lambda *_: self.retarget(force=True))
        self.add_controller(click)

        self.apply_settings()

    # -- wiring ------------------------------------------------------------

    def interval(self):
        try:
            value = int(self.window.settings.get("statusbar_interval", 5))
        except (TypeError, ValueError):
            value = 5
        return max(STATS_INTERVAL_MIN, min(STATS_INTERVAL_MAX, value))

    def apply_settings(self):
        """Called after every settings change; also does the initial start."""
        if self.timer is not None:
            GLib.source_remove(self.timer)
            self.timer = None
        on = bool(self.window.settings.get("statusbar", True))
        self.set_visible(on)
        if on:
            self.timer = GLib.timeout_add_seconds(self.interval(), self._tick)
            self.retarget(force=True)

    def _target(self):
        pane = self.window._pane_for_action()
        if pane is None:
            return None, None
        # Local shells share one key; every host gets its own.
        return pane, (pane.alias or "@local")

    def retarget(self, force=False):
        """The focused pane changed: show what is known, then go and look."""
        if not self.get_visible():
            return
        pane, key = self._target()
        self.key = key
        self._render()
        if pane is not None and (force or key not in self.readings):
            self._poll(pane, key)

    def _tick(self):
        if self.timer is None:
            return False
        self.ticks += 1
        pane, key = self._target()
        self.key = key
        if pane is not None:
            fails = (self.readings.get(key) or {}).get("fails", 0)
            # A host that keeps refusing gets asked every fifth tick instead of
            # being hammered at full rate for as long as the tab is open.
            if fails < 3 or self.ticks % 5 == 0:
                self._poll(pane, key)
        self._render()
        return True

    # -- polling -----------------------------------------------------------

    def _blocked(self, pane):
        """Why this pane cannot be read, or None if it can."""
        if getattr(pane, "chooser", None) is not None:
            return "waiting for a host to be picked"
        if not pane.alive:
            return "not connected"
        if pane.alias is not None and pane.connect_opts.get("no_mux"):
            # This pane fell back to a private connection, so there is no
            # shared socket to ride: every reading would be a fresh login.
            return "connection sharing is off here — no readings"
        return None

    def _poll(self, pane, key):
        if key in self.inflight or self._blocked(pane) is not None:
            return
        self.inflight.add(key)

        def done(out, error):
            self.inflight.discard(key)
            previous = self.readings.get(key, {})
            reading = {
                "stamp": GLib.get_monotonic_time(),
                "data": previous.get("data", {}),
                "sample": previous.get("sample"),
                "cpu": previous.get("cpu"),
                "net": previous.get("net"),
                "netsample": previous.get("netsample"),
                "clock": previous.get("clock"),
                "fails": previous.get("fails", 0),
                "error": previous.get("error"),
            }
            data = {} if error else parse_stats(out)
            if error:
                reading["fails"] += 1
                reading["error"] = short_error(error)
            elif not data:
                reading["fails"] += 1
                reading["error"] = "no /proc here — the readout needs Linux"
            else:
                reading.update(data=data, error=None, fails=0)
                reading["cpu"] = cpu_percent(previous.get("sample"),
                                             data.get("cpu"))
                reading["sample"] = data.get("cpu")
                # Prefer the far end's own clock; fall back to how long this
                # end waited, which is all a host with no /proc/uptime has.
                elapsed = None
                if data.get("clock") and previous.get("clock"):
                    elapsed = data["clock"] - previous["clock"]
                if (not elapsed or elapsed <= 0) and previous.get("stamp"):
                    elapsed = (reading["stamp"] - previous["stamp"]) / 1e6
                reading["net"] = net_rates(previous.get("netsample"),
                                           data.get("net"), elapsed)
                reading["netsample"] = data.get("net")
                reading["clock"] = data.get("clock")
            self.readings[key] = reading
            if self.key == key:
                self._render()

        if pane.alias is None:
            run_argv_async(["/bin/sh", "-c", STATS_COMMAND], done)
        else:
            # BatchMode, and never the master: same rules as the explorer.
            run_ssh_async(pane.alias, STATS_COMMAND, done)

    # -- drawing -----------------------------------------------------------

    def _dim(self, name, message):
        esc = GLib.markup_escape_text
        self.label.set_markup(f"<b>{esc(name)}</b>  "
                              f"<span alpha='55%'>{esc(message)}</span>")
        self.set_tooltip_markup(None)

    def _render(self):
        pane, key = self._target()
        if pane is None:
            self.label.set_markup("<span alpha='55%'>no session</span>")
            self.set_tooltip_markup(None)
            return

        name = pane.title or pane.alias or "local"
        blocked = self._blocked(pane)
        if blocked is not None:
            self._dim(name, blocked)
            return

        reading = self.readings.get(key)
        if reading is None:
            self._dim(name, "reading…")
            return
        if not reading.get("data"):
            self._dim(name, reading.get("error") or "reading…")
            return

        # Past three missed polls the numbers are history, not status.
        age = (GLib.get_monotonic_time() - reading["stamp"]) / 1e6
        stale = age > self.interval() * 3
        self.label.set_markup(stats_markup(name, reading["data"],
                                           reading.get("cpu"),
                                           reading.get("net"), stale))
        self.set_tooltip_markup(stats_tooltip(name, reading["data"],
                                              reading.get("net")))


class ChatPostDialog(Gtk.Window):
    """Where the selection goes, and what to say about it.

    The channel list is fetched on a thread; until it lands the dropdown
    shows what it can. Send runs on a thread too -- the server is across
    the internet -- and the window says what happened rather than closing
    on a failure the user then cannot read.
    """

    def __init__(self, parent, selection, host=None, agent=False):
        super().__init__(title="Post to Ultimate Chat", transient_for=parent,
                         modal=True, default_width=560, default_height=460)
        self.selection = selection
        self.host = host
        self._channels = []
        self._sending = False

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                      margin_top=14, margin_bottom=14, margin_start=16,
                      margin_end=16)
        self.set_child(box)

        row = Gtk.Box(spacing=8)
        row.append(Gtk.Label(label="Channel", xalign=0))
        self.channel = Gtk.DropDown.new_from_strings(
            [chatbridge.AGENT_CHANNEL if agent else "…"])
        self.channel.set_hexpand(True)
        row.append(self.channel)
        box.append(row)
        self._want = chatbridge.AGENT_CHANNEL if agent else None

        box.append(Gtk.Label(
            label=("Your question. The selection goes under it."
                   if agent else "A note, optional. The selection goes under it."),
            xalign=0))
        self.note = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.note.set_top_margin(6); self.note.set_bottom_margin(6)
        self.note.set_left_margin(8); self.note.set_right_margin(8)
        note_scroller = Gtk.ScrolledWindow(min_content_height=70)
        note_scroller.set_child(self.note)
        note_scroller.add_css_class("frame")
        box.append(note_scroller)

        lines = selection.count("\n") + 1
        box.append(Gtk.Label(
            label=f"Selection: {lines} line{'s' if lines != 1 else ''}, "
                  f"{len(selection):,} characters"
                  + (f", from {host}" if host else ""),
            xalign=0, css_classes=["dim-label"]))
        preview = Gtk.TextView(editable=False, monospace=True,
                               cursor_visible=False)
        preview.get_buffer().set_text(selection[:4000])
        pv_scroller = Gtk.ScrolledWindow(vexpand=True)
        pv_scroller.set_child(preview)
        pv_scroller.add_css_class("frame")
        box.append(pv_scroller)

        self.status = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
        box.append(self.status)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        buttons.append(cancel)
        self.send = Gtk.Button(label="Ask" if agent else "Post")
        self.send.add_css_class("suggested-action")
        self.send.connect("clicked", self._on_send)
        buttons.append(self.send)
        box.append(buttons)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", lambda _c, kv, _code, st: (
            self.close() or True) if kv == Gdk.KEY_Escape else False)
        self.add_controller(keys)

        GLib.Thread.new("chat-channels", self._load_channels)
        self.note.grab_focus()

    def _load_channels(self):
        try:
            rows = chatbridge.channels()
        except chatbridge.BridgeError as e:
            GLib.idle_add(self.status.set_text, f"Could not list channels: {e}")
            return
        GLib.idle_add(self._set_channels, rows)

    def _set_channels(self, rows):
        self._channels = rows
        names = [f"#{c['id']}" + ("  (answers)" if c["kind"] == "agent" else "")
                 for c in rows]
        model = Gtk.StringList.new(names or ["(no channels)"])
        self.channel.set_model(model)
        pick = 0
        for i, c in enumerate(rows):
            if c["id"] == (self._want or "notes"):
                pick = i
                break
        self.channel.set_selected(pick)
        return False

    def _on_send(self, _btn):
        if self._sending:
            return
        if not self._channels:
            self.status.set_text("No channel to post to yet.")
            return
        idx = self.channel.get_selected()
        channel = self._channels[idx]["id"] if idx < len(self._channels) \
            else self._channels[0]["id"]
        buf = self.note.get_buffer()
        note = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
        body = chatbridge.compose(self.selection, note=note, host=self.host)
        title = (note.strip().splitlines() or [""])[0][:120] or (
            f"From {self.host}" if self.host else "From the terminal")
        self._sending = True
        self.send.set_sensitive(False)
        self.status.set_text(f"Posting to #{channel}…")

        def work():
            try:
                m = chatbridge.post(channel, body, host=self.host, title=title)
            except chatbridge.BridgeError as e:
                GLib.idle_add(self._failed, str(e))
                return
            GLib.idle_add(self._done, channel, m)
        GLib.Thread.new("chat-post", work)

    def _failed(self, text):
        self._sending = False
        self.send.set_sensitive(True)
        self.status.set_text(f"Failed: {text}")
        return False

    def _done(self, channel, m):
        parent = self.get_transient_for()
        if parent is not None and hasattr(parent, "flash"):
            parent.flash(f"posted to #{channel}"
                         + (f" as #{m['id']}" if m.get("id") else ""))
        self.close()
        return False


class UltimateSshWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Ultimate SSH")
        self.set_default_size(1400, 860)

        self.active_pane = None
        self.broadcast_mode = BROADCAST_OFF
        self._in_broadcast = False

        self.settings = load_settings()
        gtk_settings = Gtk.Settings.get_default()
        # Remember how the desktop had it, so "Follow the system" can put it
        # back rather than guessing a value.
        self._system_prefers_dark = bool(
            gtk_settings.get_property("gtk-application-prefer-dark-theme"))
        # GTK 4.20+ also reports the desktop's colour scheme, and says when
        # it changes -- at login the desktop can publish it a moment after
        # we start, which would otherwise leave us light on a dark desktop.
        if gtk_settings.find_property("gtk-interface-color-scheme"):
            self._follow_scheme(gtk_settings)
            gtk_settings.connect("notify::gtk-interface-color-scheme",
                                 lambda st, _p: self._on_system_scheme(st))
        self.apply_theme()

        self.sidebar = SessionSidebar(self)
        self.sidebar.add_css_class("ussh-panel")

        self.notebook = Gtk.Notebook(scrollable=True)
        self.notebook.set_show_border(False)
        self.notebook.connect("switch-page", self._on_switch_page)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.banner = Gtk.Label(xalign=0.0)
        self.banner.add_css_class("ussh-panel")
        self.banner.set_margin_start(10)
        self.banner.set_margin_end(10)
        self.banner.set_margin_top(4)
        self.banner.set_margin_bottom(4)
        self.banner.set_visible(False)
        right.append(self.banner)

        self.toast = Gtk.Label(xalign=0.0)
        self.toast.add_css_class("dim-label")
        self.toast.add_css_class("ussh-panel")
        self.toast.set_margin_start(10)
        self.toast.set_margin_end(10)
        self.toast.set_visible(False)
        right.append(self.toast)

        self.notebook.set_vexpand(True)
        right.append(self.notebook)

        # Bottom of the window, under every tab: the host you are looking at.
        self.statusbar = HostStatusBar(self)
        self.statusbar.add_css_class("ussh-panel")
        right.append(self.statusbar)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_start_child(self.sidebar)
        paned.set_end_child(right)
        paned.set_position(260)
        paned.set_resize_start_child(False)
        self.set_child(paned)

        self._build_headerbar()
        self._install_shortcuts()
        self._show_welcome()
        GLib.timeout_add(800, self._warn_if_defaults_shadow)
        self.connect("close-request", self._on_close_request)

    def _build_headerbar(self):
        header = Gtk.HeaderBar()

        self.broadcast_btn = Gtk.Button(label=BROADCAST_LABELS[BROADCAST_OFF])
        self.broadcast_btn.set_tooltip_text(
            "Cycle broadcast: off → same group → ALL open tabs (Ctrl+Shift+B)")
        self.broadcast_btn.connect("clicked", lambda _b: self.cycle_broadcast())
        header.pack_start(self.broadcast_btn)

        self.colors_btn = Gtk.ToggleButton(label="Colors: on")
        self.colors_btn.set_tooltip_text(
            "Terminal colours for ls, grep, prompts and permissions "
            "(Ctrl+Shift+G)")
        self.colors_btn.set_active(self.settings.get("colors", True))
        self.colors_btn.connect("toggled", self._on_colors_toggled)
        header.pack_start(self.colors_btn)

        self.persist_btn = Gtk.Button(
            label=PERSISTENCE_LABELS[self.settings.get("persistence", "plain")])
        self.persist_btn.set_tooltip_text(
            "How new sessions are opened: plain shell, or wrapped in tmux or "
            "screen so a dropped connection doesn't kill your work "
            "(Ctrl+Shift+P)")
        self.persist_btn.connect("clicked", lambda _b: self.cycle_persistence())
        header.pack_start(self.persist_btn)

        local_btn = Gtk.Button()
        local_btn.set_child(_icon_label("utilities-terminal-symbolic",
                                        "Local terminal"))
        local_btn.set_tooltip_text(
            "Open a shell on this machine (Ctrl+Shift+T)")
        local_btn.connect("clicked", lambda _b: self.new_local_shell())
        header.pack_start(local_btn)

        explorer_btn = Gtk.Button(icon_name="folder-remote-symbolic")
        explorer_btn.set_tooltip_text("Toggle SSH explorer (Ctrl+Shift+O)")
        explorer_btn.connect("clicked", lambda _b: self.toggle_explorer())
        header.pack_end(explorer_btn)

        manage_btn = Gtk.Button(icon_name="document-edit-symbolic")
        manage_btn.set_tooltip_text(
            "Manage connections and categories (Ctrl+Shift+M)")
        manage_btn.connect("clicked", lambda _b: self.open_manager())
        header.pack_end(manage_btn)

        layout_btn = Gtk.MenuButton(icon_name="view-grid-symbolic")
        layout_btn.set_tooltip_text("Split the current tab into panes")
        layout_btn.set_popover(self._build_layout_menu())
        header.pack_end(layout_btn)

        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic")
        menu_btn.set_tooltip_text("Menu")
        menu_btn.set_popover(self._build_menu())
        header.pack_end(menu_btn)

        self.set_titlebar(header)

    def _build_layout_menu(self):
        return self._popover_menu([
            ("Split right    Ctrl+Shift+D",
             lambda: self.split(Gtk.Orientation.HORIZONTAL)),
            ("Split down     Ctrl+Shift+E",
             lambda: self.split(Gtk.Orientation.VERTICAL)),
            (None, None),
            ("Quad — 2×2     Ctrl+Shift+Q", self.quad_layout),
            ("Even split     ", self.equalize_panes),
            (None, None),
            ("Close pane     Ctrl+Shift+X", self.close_pane),
        ])

    def _popover_menu(self, items):
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        for label, handler in items:
            if label is None:
                box.append(Gtk.Separator())
                continue
            btn = Gtk.Button(label=label)
            btn.add_css_class("flat")
            child = btn.get_child()
            if isinstance(child, Gtk.Label):
                child.set_xalign(0.0)
            btn.connect("clicked",
                        lambda _b, h=handler: (popover.popdown(), h()))
            box.append(btn)
        popover.set_child(box)
        return popover

    def _build_menu(self):
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        items = [
            ("Settings…", self.open_settings),
            ("Manage connections…", self.open_manager),
            (None, None),
            ("Equalize split panes", self.equalize_panes),
            ("Host status bar on/off", self.toggle_statusbar),
            (None, None),
            ("Reset SSH connection sharing", self.reset_connections),
            (None, None),
            ("Fix defaults order (Host * last)…", self.fix_defaults_order),
            (None, None),
            ("Re-import from ~/.ssh/config…", self.reimport_system_config),
        ]
        for label, handler in items:
            if label is None:
                box.append(Gtk.Separator())
                continue
            btn = Gtk.Button(label=label)
            btn.add_css_class("flat")
            child = btn.get_child()
            if isinstance(child, Gtk.Label):
                child.set_xalign(0.0)
            btn.connect("clicked",
                        lambda _b, h=handler: (popover.popdown(), h()))
            box.append(btn)
        popover.set_child(box)
        return popover

    def _install_shortcuts(self):
        # Ctrl+Shift+* throughout: bare Ctrl+L/W/T/C belong to the remote
        # shell (clear, kill-word, transpose, SIGINT) and must pass through.
        controller = Gtk.ShortcutController()
        controller.set_scope(Gtk.ShortcutScope.GLOBAL)

        def bind(accel, fn):
            controller.add_shortcut(Gtk.Shortcut(
                trigger=Gtk.ShortcutTrigger.parse_string(accel),
                action=Gtk.CallbackAction.new(lambda *_: (fn(), True)[1]),
            ))

        bind("<Control><Shift>t", self.new_local_shell)
        bind("<Control><Shift>w", self.close_current_tab)
        bind("<Control><Shift>l", self.sidebar.focus_search)
        bind("<Control><Shift>r", self.sidebar.reload)
        bind("<Control><Shift>c", self.copy_selection)
        bind("<Control><Shift>v", self.paste_clipboard)
        bind("<Control><Shift>b", self.cycle_broadcast)
        bind("<Control><Shift>o", self.toggle_explorer)
        bind("<Control><Shift>d", lambda: self.split(Gtk.Orientation.HORIZONTAL))
        bind("<Control><Shift>e", lambda: self.split(Gtk.Orientation.VERTICAL))
        bind("<Control><Shift>x", self.close_pane)
        bind("<Control><Shift>k", self.reconnect_pane)
        bind("<Control><Shift>m", self.open_manager)
        bind("<Control><Shift>g", self.toggle_colors)
        bind("<Control><Shift>s", self.toggle_statusbar)
        bind("<Control><Shift>p", self.cycle_persistence)
        for accel in ("<Control><Shift>plus", "<Control><Shift>equal"):
            bind(accel, lambda: self.zoom_font(1))
        for accel in ("<Control><Shift>minus", "<Control><Shift>underscore"):
            bind(accel, lambda: self.zoom_font(-1))
        bind("<Control><Shift>0", self.reset_font)
        bind("<Control><Shift>q", self.quad_layout)

        for n in range(1, 10):
            bind(f"<Alt>{n}", lambda i=n - 1: self.notebook.set_current_page(i))

        self.add_controller(controller)

    # -- welcome ---------------------------------------------------------

    def _show_welcome(self):
        if self.notebook.get_n_pages() > 0:
            return
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        label = Gtk.Label()
        # The welcome page stands in for a terminal, so it is see-through
        # like one: no panel fill.
        label.set_markup(
            "<span size='large'>Pick a host on the left, or "
            "<b>Ctrl+Shift+L</b> to search.</span>\n\n"
            "<span alpha='60%'>Ctrl+Shift+T local shell · Ctrl+Shift+W close tab\n"
            "Ctrl+Shift+D / Ctrl+Shift+E split right / down · Ctrl+Shift+X close pane\n"
            "Ctrl+Shift+B broadcast · Ctrl+Shift+O explorer · Ctrl+Shift+K reconnect\n"
            "Ctrl+Shift+C/V copy·paste · Alt+1..9 switch tab</span>"
        )
        label.set_justify(Gtk.Justification.CENTER)
        box.append(label)

        local = Gtk.Button()
        local.set_child(_icon_label("utilities-terminal-symbolic",
                                    "Open local terminal", pixel_size=28))
        local.add_css_class("suggested-action")
        local.add_css_class("pill")
        local.set_halign(Gtk.Align.CENTER)
        local.set_size_request(300, 60)
        local.set_tooltip_text("Open a shell on this machine (Ctrl+Shift+T)")
        local.connect("clicked", lambda _b: self.new_local_shell())
        self.welcome_local_btn = local
        box.append(local)

        saved = self._load_session()
        if saved:
            btn = Gtk.Button(
                label=f"Restore last session "
                      f"({len(saved)} host{'' if len(saved) == 1 else 's'})")
            btn.set_halign(Gtk.Align.CENTER)
            btn.connect("clicked", lambda _b: self._restore_session(saved))
            box.append(btn)

        self.notebook.append_page(box, Gtk.Label(label="Ultimate SSH"))
        self.welcome = box

    def _drop_welcome(self):
        page = getattr(self, "welcome", None)
        if page is not None:
            idx = self.notebook.page_num(page)
            if idx >= 0:
                self.notebook.remove_page(idx)
            self.welcome = None

    # -- tabs ------------------------------------------------------------

    def _add_tab(self, tab):
        self._drop_welcome()
        label = tab.make_label(self.close_tab)
        idx = self.notebook.append_page(tab, label)
        self.notebook.set_tab_reorderable(tab, True)
        self.notebook.set_current_page(idx)
        panes = tab.panes()
        if panes:
            panes[0].term.grab_focus()

    def open_or_focus(self, host):
        """Bring up a tab on ``host``: the one already open, else a new
        one. What an external "open a shell here" means."""
        for i in range(self.notebook.get_n_pages()):
            page = self.notebook.get_nth_page(i)
            if isinstance(page, TerminalTab) and page.host is not None \
                    and page.host.alias == host.alias:
                self.notebook.set_current_page(i)
                self.present()
                return page
        tab = self.connect_to(host)
        self.present()
        return tab

    def connect_to(self, host, persistence=None, remote_command=None):
        sweep_control_sockets()   # never hand a session a dead master
        opts = {}
        if remote_command is not None:
            opts = {"remote_command": remote_command, "force_tty": True}
        else:
            mode = persistence or self.settings.get("persistence", "plain")
            if mode != "plain":
                opts = {"persistence": mode}
        tab = TerminalTab(self, host, ssh_argv(host.alias, **opts),
                          group_color(host.group), connect_opts=opts)
        self._add_tab(tab)
        return tab

    def connect_group(self, group):
        hosts = self.sidebar.groups.get(group, [])
        if not hosts:
            return
        if len(hosts) > BULK_CONFIRM_AT:
            dialog = Gtk.AlertDialog(
                message=f"Open {len(hosts)} sessions?",
                detail=(f"This logs in to every host in “{group}” at once. "
                        f"That is {len(hosts)} simultaneous SSH connections."),
                buttons=["Cancel", f"Open {len(hosts)} tabs"],
                cancel_button=0,
                default_button=1,
            )

            def answered(dlg, res):
                try:
                    if dlg.choose_finish(res) == 1:
                        self._open_hosts(hosts)
                except GLib.Error:
                    pass

            dialog.choose(self, None, answered)
        else:
            self._open_hosts(hosts)

    def _open_hosts(self, hosts):
        for h in hosts:
            self.connect_to(h)

    def connect_with(self, host, mode):
        self.connect_to(host, persistence=mode)

    def run_claude(self, host):
        """Open Claude Code on the host, inside tmux so it outlives a drop."""
        self.connect_to(host,
                        remote_command=session_command(CLAUDE_COMMAND, "claude"))

    def new_local_shell(self):
        shell = os.environ.get("SHELL", "/bin/bash")
        tab = TerminalTab(self, None, [shell], "#8a8a8a", is_local=True)
        self._add_tab(tab)

    def run_local(self, command, title=None):
        """A local tab running one shell command line (``--run``), the way
        ``konsole -e`` does. It closes when the command succeeds, like any
        pane; otherwise it stays with the exit status and R to run again."""
        tab = TerminalTab(self, None, ["/bin/sh", "-c", command], "#8a8a8a",
                          is_local=True, title=title or "local")
        self._add_tab(tab)

    def close_tab(self, tab):
        idx = self.notebook.page_num(tab)
        if idx >= 0:
            self.notebook.remove_page(idx)
        if self.active_pane is not None and self.active_pane.tab is tab:
            self.active_pane = None
        self._show_welcome()

    def current_tab(self):
        idx = self.notebook.get_current_page()
        if idx < 0:
            return None
        page = self.notebook.get_nth_page(idx)
        return page if isinstance(page, TerminalTab) else None

    def close_current_tab(self):
        tab = self.current_tab()
        if tab is not None:
            self.close_tab(tab)

    def _on_switch_page(self, _nb, page, _num):
        if isinstance(page, TerminalTab):
            page.refresh_identity()
            focus = page.focused_pane()
            self.set_title(
                f"{(focus.title if focus else page.title)} — Ultimate SSH")
            panes = page.panes()
            if panes and (self.active_pane is None
                          or self.active_pane.tab is not page):
                self.active_pane = panes[0]
        else:
            self.set_title("Ultimate SSH")
        self._refresh_banner()
        self.statusbar.retarget()

    # -- panes -----------------------------------------------------------

    def set_active_pane(self, pane):
        self.active_pane = pane
        if pane is not None and pane.tab is not None:
            pane.tab.refresh_identity()
        self._refresh_title()
        self._refresh_banner()
        self.statusbar.retarget()

    def _refresh_title(self):
        tab = self.current_tab()
        if tab is None:
            self.set_title("Ultimate SSH")
            return
        focus = tab.focused_pane()
        name = focus.title if focus is not None else tab.title
        self.set_title(f"{name} — Ultimate SSH")

    def _pane_for_action(self):
        pane = self.active_pane
        tab = self.current_tab()
        if tab is None:
            return None
        if pane is None or pane.tab is not tab:
            panes = tab.panes()
            pane = panes[0] if panes else None
        return pane

    def split(self, orientation):
        pane = self._pane_for_action()
        if pane is not None:
            pane.tab.split(pane, orientation)

    def quad_layout(self):
        """Turn the current tab into a 2x2 grid of panes."""
        tab = self.current_tab()
        if tab is None:
            return
        panes = tab.panes()
        if len(panes) != 1:
            self.flash("quad starts from a single pane — close the others "
                       "with Ctrl+Shift+X first")
            return
        first = panes[0]
        tab.split(first, Gtk.Orientation.HORIZONTAL)
        second = next((p for p in tab.panes() if p is not first), None)
        tab.split(first, Gtk.Orientation.VERTICAL)
        if second is not None:
            tab.split(second, Gtk.Orientation.VERTICAL)
        tab.equalize()
        self.flash("2×2 — each new pane asks which host to connect to; "
                   "“Same host” is one click")

    def close_pane(self):
        pane = self._pane_for_action()
        if pane is not None:
            pane.tab.close_pane(pane)

    def reconnect_pane(self):
        pane = self._pane_for_action()
        if pane is not None:
            pane.reconnect()

    def _current_terminal(self):
        pane = self._pane_for_action()
        return pane.term if pane is not None else None

    def copy_selection(self):
        pane = self._pane_for_action()
        if pane is not None:
            pane.copy_selection()

    def paste_clipboard(self):
        pane = self._pane_for_action()
        if pane is not None:
            pane.paste_clipboard()

    def toggle_explorer(self):
        tab = self.current_tab()
        if tab is None:
            return
        if tab.is_local:
            self.banner.set_markup(
                "<span color='#c4a000'>the explorer needs a remote host — "
                "this is a local shell</span>")
            self.banner.set_visible(True)
            GLib.timeout_add_seconds(3, lambda: (self._refresh_banner(), False)[1])
            return
        tab.toggle_explorer()

    # -- broadcast -------------------------------------------------------

    def cycle_broadcast(self):
        self.broadcast_mode = (self.broadcast_mode + 1) % 3
        self._refresh_banner()

    def _broadcast_targets(self, source):
        """Panes that should receive input mirrored from `source`."""
        targets = []
        for i in range(self.notebook.get_n_pages()):
            page = self.notebook.get_nth_page(i)
            if not isinstance(page, TerminalTab):
                continue
            for pane in page.panes():
                if pane is source or not pane.alive:
                    continue
                if self.broadcast_mode == BROADCAST_GROUP \
                        and pane.group != source.group:
                    continue
                targets.append(pane)
        return targets

    def broadcast_from(self, source, text):
        if self.broadcast_mode == BROADCAST_OFF or self._in_broadcast:
            return
        if self.active_pane is not None and source is not self.active_pane:
            return  # only mirror the pane you're actually typing in
        payload = text.encode("utf-8") if isinstance(text, str) else text
        self._in_broadcast = True
        try:
            for pane in self._broadcast_targets(source):
                pane.term.feed_child(payload)
        finally:
            self._in_broadcast = False

    def _refresh_banner(self):
        # Single source of truth: the button label is derived from the mode,
        # never set alongside it, so the two cannot drift apart.
        self.broadcast_btn.set_label(BROADCAST_LABELS[self.broadcast_mode])
        if self.broadcast_mode == BROADCAST_OFF:
            self.banner.set_visible(False)
            return
        source = self._pane_for_action()
        if source is None:
            self.banner.set_visible(False)
            return
        targets = self._broadcast_targets(source)
        scope = ("group “" + source.group + "”" if self.broadcast_mode
                 == BROADCAST_GROUP else "ALL open panes")
        names = ", ".join(sorted({p.title for p in targets})[:6])
        more = "" if len(targets) <= 6 else f" +{len(targets) - 6} more"
        self.banner.set_markup(
            f"<span background='#7a1010' color='#ffffff'><b> ⚠ BROADCAST </b></span>"
            f"  typing in <b>{GLib.markup_escape_text(source.title)}</b> also goes to "
            f"<b>{len(targets)}</b> pane(s) · {GLib.markup_escape_text(scope)}"
            + (f" · {GLib.markup_escape_text(names)}{more}" if targets else "")
        )
        self.banner.set_visible(True)

    # -- appearance --------------------------------------------------------

    def dark_terminal(self):
        theme = self.settings.get("theme", "system")
        if theme == "dark":
            return True
        if theme == "light":
            return False
        return self._system_prefers_dark

    def _follow_scheme(self, gtk_settings):
        scheme = gtk_settings.get_property("gtk-interface-color-scheme")
        dark = getattr(Gtk.InterfaceColorScheme, "DARK", None)
        light = getattr(Gtk.InterfaceColorScheme, "LIGHT", None)
        if scheme == dark:
            self._system_prefers_dark = True
        elif scheme == light:
            self._system_prefers_dark = False
        # DEFAULT / UNSUPPORTED: keep what prefer-dark said

    def _on_system_scheme(self, gtk_settings):
        before = self._system_prefers_dark
        self._follow_scheme(gtk_settings)
        if (self._system_prefers_dark == before
                or self.settings.get("theme", "system") != "system"):
            return
        self.apply_theme()
        notebook = getattr(self, "notebook", None)   # not built yet at startup
        for i in range(notebook.get_n_pages() if notebook else 0):
            page = notebook.get_nth_page(i)
            if isinstance(page, TerminalTab):
                for pane in page.panes():
                    pane.apply_appearance()

    def apply_theme(self):
        gtk_settings = Gtk.Settings.get_default()
        theme = self.settings.get("theme", "system")
        prefer_dark = (self._system_prefers_dark if theme == "system"
                       else theme == "dark")
        gtk_settings.set_property("gtk-application-prefer-dark-theme",
                                  prefer_dark)
        self.apply_opacity()

    def apply_opacity(self):
        alpha = opacity_of(self.settings)
        if getattr(self, "_opacity_css", None) is None:
            self._opacity_css = Gtk.CssProvider()
            Gtk.StyleContext.add_provider_for_display(
                self.get_display(), self._opacity_css,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        c = rgba(BG if self.dark_terminal() else LIGHT_BG)
        rgb = f"{round(c.red * 255)}, {round(c.green * 255)}, {round(c.blue * 255)}"
        css = TRANSLUCENT_CSS % {"alpha": alpha, "rgb": rgb}
        if hasattr(self._opacity_css, "load_from_string"):   # GTK 4.12+
            self._opacity_css.load_from_string(css)
        else:
            self._opacity_css.load_from_data(css, -1)
        if alpha < 1.0:
            self.add_css_class("ussh-translucent")
        else:
            self.remove_css_class("ussh-translucent")

    def _on_colors_toggled(self, button):
        on = button.get_active()
        button.set_label("Colors: on" if on else "Colors: off")
        if self.settings.get("colors", True) != on:
            self.apply_settings({"colors": on})

    def cycle_persistence(self):
        current = self.settings.get("persistence", "plain")
        nxt = PERSISTENCE_ORDER[
            (PERSISTENCE_ORDER.index(current) + 1) % len(PERSISTENCE_ORDER)
            if current in PERSISTENCE_ORDER else 0]
        self.apply_settings({"persistence": nxt})
        self.flash(
            "new connections open a plain shell" if nxt == "plain"
            else f"new connections attach or create a {nxt} session "
                 f"named “{DEFAULT_SESSION_NAME}” — they survive a dropped "
                 f"connection")

    def toggle_statusbar(self):
        self.settings["statusbar"] = not self.settings.get("statusbar", True)
        save_settings(self.settings)
        self.statusbar.apply_settings()
        self.flash("host status bar "
                   + ("on" if self.settings["statusbar"] else "off"))

    def toggle_colors(self):
        self.colors_btn.set_active(not self.colors_btn.get_active())

    def zoom_font(self, delta):
        """Resize every terminal, open ones included, and remember it."""
        desc = Pango.FontDescription(self.settings.get("font", "monospace 11"))
        size = desc.get_size() / Pango.SCALE
        if size <= 0:
            size = 11
        new_size = max(FONT_MIN, min(FONT_MAX, size + delta))
        if new_size == size:
            return
        desc.set_size(int(new_size * Pango.SCALE))
        self.apply_settings({"font": desc.to_string()})
        self.flash(f"terminal font: {desc.to_string()}")

    def reset_font(self):
        self.apply_settings({"font": DEFAULT_SETTINGS["font"]})
        self.flash(f"terminal font reset to {DEFAULT_SETTINGS['font']}")

    def open_settings(self):
        editor.SettingsDialog(self, self.settings, self.apply_settings).present()

    def apply_settings(self, values):
        self.settings.update(values)
        save_settings(self.settings)
        self.apply_theme()
        # keep the header toggle honest if colours were changed elsewhere;
        # the guard in _on_colors_toggled makes this non-recursive
        on = self.settings.get("colors", True)
        self.colors_btn.set_label("Colors: on" if on else "Colors: off")
        self.colors_btn.set_active(on)
        self.persist_btn.set_label(
            PERSISTENCE_LABELS.get(self.settings.get("persistence", "plain"),
                                   PERSISTENCE_LABELS["plain"]))
        compact = self.settings.get("density", "compact") == "compact"
        if compact != self.sidebar.compact:
            self.sidebar.compact = compact
            self.sidebar.reload()
        for i in range(self.notebook.get_n_pages()):
            page = self.notebook.get_nth_page(i)
            if isinstance(page, TerminalTab):
                for pane in page.panes():
                    pane.apply_appearance()
        self.statusbar.apply_settings()
        self.flash("settings applied")

    def reset_connections(self):
        """Close shared masters and clear stale sockets.

        The escape hatch for "mux_client ... broken pipe": next connect starts
        from a clean slate. Open sessions keep running.
        """
        aliases = set()
        for i in range(self.notebook.get_n_pages()):
            page = self.notebook.get_nth_page(i)
            if isinstance(page, TerminalTab) and page.host is not None:
                aliases.add(page.host.alias)
        for alias in aliases:
            run_argv_async(ssh_argv(alias)[:-1] + ["-O", "exit", alias],
                           lambda *_: None)
        removed = sweep_control_sockets()
        self.flash(f"cleared {removed} stale control socket(s); "
                   f"asked {len(aliases)} master(s) to exit")

    def equalize_panes(self):
        tab = self.current_tab()
        if tab is not None:
            tab.equalize()

    # -- authentication fallback -------------------------------------------

    def credentials_dialog(self, pane):
        view = self.sidebar.config.find(pane.alias) if pane.alias else None
        current_user = view.block.get("User") if view else ""

        def remember(user):
            if view is None:
                return
            try:
                self.sidebar.config.update_host(view, options=[("User", user)])
            except Exception as exc:
                editor.error_dialog(self, "Could not save username", str(exc))
                return
            self._commit()

        def retry(user, password, force_password):
            argv = ssh_argv(pane.alias, user=user, force_password=force_password)
            pane.reconnect(argv=argv, password=password)

        editor.CredentialsDialog(self, pane.alias, current_user,
                                 on_retry=retry, on_remember=remember).present()

    # -- connection list ---------------------------------------------------

    def _warn_if_defaults_shadow(self):
        """Say so at startup: a shadowed User is invisible until you hit it."""
        cfg = self.sidebar.config
        shadowed = sorted(cfg.shadowed_keys() & set(sshconfig.KNOWN_KEYS))
        if shadowed:
            self.flash(
                "⚠ a “Host *” block above your hosts overrides their "
                + ", ".join(shadowed)
                + " — Menu → “Fix defaults order” makes per-host settings work",
                seconds=20)
        return False

    def fix_defaults_order(self):
        """Move `Host *` below the host stanzas so per-host settings apply."""
        cfg = self.sidebar.config
        shadowed = sorted(cfg.shadowed_keys())
        if not shadowed:
            self.flash("defaults are already in the right place — "
                       "per-host settings win")
            return

        def go():
            moved = cfg.move_defaults_to_end()
            if not moved:
                self.flash("nothing to move")
                return
            if self._commit(cfg):
                self.flash("moved the defaults to the end — per-host "
                           "settings now take effect")

        editor.confirm(
            self, "Move the Host * defaults to the end?",
            "ssh uses the FIRST value it finds for each setting, and your "
            "`Host *` block sits above the hosts — so it overrides every "
            "per-host " + ", ".join(shadowed) + " in the file.\n\n"
            "Moving that block to the end makes per-host settings work, which "
            "is the layout ssh_config(5) recommends. Hosts without their own "
            "value still inherit the default. The block is moved, not "
            "rewritten, and a backup is taken first.",
            "Move it", go)

    def reimport_system_config(self):
        def go():
            try:
                backup = sshconfig.import_system_config()
            except Exception as exc:
                editor.error_dialog(self, "Could not import", str(exc))
                return
            self.sidebar.reload()
            self.flash("re-imported from ~/.ssh/config" +
                       (f" · previous copy backed up to {os.path.basename(backup)}"
                        if backup else ""))

        editor.confirm(
            self, "Re-import from ~/.ssh/config?",
            "This replaces the Ultimate SSH connection list with a fresh copy of your "
            "system SSH config. Your current list is backed up first, and "
            "~/.ssh/config itself is only read, never written.",
            "Re-import", go)

    # -- editing the connection list ---------------------------------------

    def _commit(self, config=None):
        """Persist sidebar edits. Any failure discards the in-memory change so
        what you see always matches what is on disk.

        Takes the config explicitly: a dialog must save the object it actually
        mutated, not whatever the sidebar happens to hold by the time OK is
        clicked, or a reload mid-edit would silently swallow the change.
        """
        backup = editor.save_config(self, config or self.sidebar.config)
        if backup is None:
            self.sidebar.reload()
            return False
        self.sidebar.reload()
        self.flash(f"saved ~/.ssh/config · backup {os.path.basename(backup)}"
                   if backup else "saved ~/.ssh/config")
        return True

    def flash(self, message, seconds=4):
        self.toast.set_text(message)
        self.toast.set_visible(True)
        self._toast_token = token = object()

        def clear():
            if getattr(self, "_toast_token", None) is token:
                self.toast.set_visible(False)
            return False

        GLib.timeout_add_seconds(seconds, clear)

    def open_manager(self):
        win = editor.ManagerWindow(self, SSH_CONFIG, self._on_manager_saved)
        win.present()

    def _on_manager_saved(self):
        self.sidebar.reload()
        self.flash("connection list updated")

    def edit_host(self, view):
        cfg = self.sidebar.config
        editor.HostDialog(self, cfg, view=view,
                          on_done=lambda: self._commit(cfg)).present()

    def new_host_in(self, group):
        cfg = self.sidebar.config
        editor.HostDialog(self, cfg, view=None, group=group,
                          on_done=lambda: self._commit(cfg)).present()

    def duplicate_host(self, view):
        alias = self.sidebar.config.duplicate_host(view)
        if self._commit():
            new_view = self.sidebar.config.find(alias)
            if new_view is not None:
                self.edit_host(new_view)

    def delete_host(self, view):
        def go():
            self.sidebar.config.delete_host(view)
            self._commit()

        editor.confirm(
            self, f"Delete “{view.alias}”?",
            f"{view.hostname} will be removed from ~/.ssh/config. "
            "A backup is written first.", "Delete", go)

    def move_host_dialog(self, view):
        others = [g for g in self.sidebar.config.group_names() if g != view.group]
        if not others:
            return

        def go(group):
            self.sidebar.config.move_host(view, group)
            self._commit()

        editor.prompt_choice(self, "Move connection",
                             f"Move “{view.alias}” to which category?",
                             others, go)

    def new_group(self):
        def go(name):
            try:
                self.sidebar.config.add_group(name)
            except Exception as exc:
                editor.error_dialog(self, "Could not add category", str(exc))
                return
            self._commit()

        editor.prompt_text(self, "New category", "Category name", "", go)

    def rename_group(self, group):
        def go(name):
            try:
                self.sidebar.config.rename_group(group, name)
            except Exception as exc:
                editor.error_dialog(self, "Could not rename", str(exc))
                return
            if group in self.sidebar.expanded:
                self.sidebar.expanded.discard(group)
                self.sidebar.expanded.add(name)
            self._commit()

        editor.prompt_text(self, "Rename category",
                           f"New name for “{group}”", group, go)

    def delete_group(self, group):
        members = self.sidebar.groups.get(group, [])

        def go(move_to):
            try:
                self.sidebar.config.delete_group(group, move_to=move_to)
            except Exception as exc:
                editor.error_dialog(self, "Could not delete", str(exc))
                return
            self._commit()

        if not members:
            editor.confirm(self, f"Delete category “{group}”?",
                           "It has no connections in it.", "Delete",
                           lambda: go(None))
            return

        others = [g for g in self.sidebar.config.group_names() if g != group]
        options = [f"Move them to: {g}" for g in others] + \
                  [f"Delete all {len(members)} connections"]

        def chosen(choice):
            if choice.startswith("Move them to: "):
                go(choice[len("Move them to: "):])
            else:
                editor.confirm(
                    self, f"Delete {len(members)} connections?",
                    f"“{group}” and every host in it will be removed from "
                    "~/.ssh/config. A backup is written first.",
                    "Delete them", lambda: go(None))

        editor.prompt_choice(
            self, f"Delete “{group}”",
            f"“{group}” holds {len(members)} connections. "
            "What should happen to them?", options, chosen)

    def copy_ssh_command(self, view):
        text = f"ssh {view.alias}"
        provider = Gdk.ContentProvider.new_for_bytes(
            "text/plain;charset=utf-8", GLib.Bytes.new(text.encode()))
        self.get_clipboard().set_content(provider)
        self.flash(f"copied “{text}”")

    # -- session persistence ---------------------------------------------

    def _open_aliases(self):
        aliases = []
        for i in range(self.notebook.get_n_pages()):
            page = self.notebook.get_nth_page(i)
            if isinstance(page, TerminalTab) and page.host is not None:
                aliases.append(page.host.alias)
        return aliases

    def _load_session(self):
        try:
            with open(SESSION_FILE) as fh:
                data = json.load(fh)
            return [a for a in data.get("hosts", []) if isinstance(a, str)]
        except (OSError, ValueError):
            return []

    def _save_session(self):
        try:
            os.makedirs(sshconfig.APP_HOME, mode=0o700, exist_ok=True)
            with open(SESSION_FILE, "w") as fh:
                json.dump({"hosts": self._open_aliases()}, fh)
        except OSError:
            pass

    def _restore_session(self, aliases):
        for alias in aliases:
            host = self.sidebar.find_host(alias)
            if host is not None:
                self.connect_to(host)

    def _on_close_request(self, _win):
        self._save_session()
        return False


class UltimateSsh(Gtk.Application):
    """Single-instance. ``ultimate-ssh --host ALIAS`` from a second
    process -- Ultimate Mail's "Shell on …" button, a script -- is handed
    to the running window, which opens or focuses the tab.
    ``--host ALIAS --command CMD`` opens a new tab on the host running CMD
    (the Claude bar's assistant uses it)."""

    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.first_run = None
        self.add_main_option("host", ord("H"), GLib.OptionFlags.NONE,
                             GLib.OptionArg.STRING_ARRAY,
                             "open a tab on this host alias (repeatable)",
                             "ALIAS")
        self.add_main_option("run", ord("e"), GLib.OptionFlags.NONE,
                             GLib.OptionArg.STRING_ARRAY,
                             "run this shell command line in a local tab "
                             "(repeatable)", "COMMAND")
        self.add_main_option("command", ord("c"), GLib.OptionFlags.NONE,
                             GLib.OptionArg.STRING,
                             "with --host: a new tab on that host running "
                             "this command", "COMMAND")
        self.add_main_option("title", ord("t"), GLib.OptionFlags.NONE,
                             GLib.OptionArg.STRING,
                             "tab title for --run", "TITLE")

    def do_command_line(self, command_line):
        options = command_line.get_options_dict().end().unpack()
        self.activate()
        win = self.props.active_window
        for alias in options.get("host") or []:
            host = win.sidebar.find_host(alias) if win is not None else None
            if host is None:
                command_line.printerr_literal(f"no host {alias!r} in "
                                              f"{SSH_CONFIG}\n")
                continue
            if options.get("command"):
                win.connect_to(host, remote_command=options["command"])
                win.present()
            else:
                win.open_or_focus(host)
        for command in options.get("run") or []:
            if win is not None:
                win.run_local(command, options.get("title"))
        return 0

    def do_activate(self):
        win = self.props.active_window
        if win is None:
            win = UltimateSshWindow(self)
            if self.first_run == "imported":
                count = len(win.sidebar.groups) and sum(
                    len(v) for v in win.sidebar.groups.values())
                win.flash(
                    f"imported {count} connections from ~/.ssh/config into "
                    f"{sshconfig.HOSTS_FILE} — your system config is untouched",
                    seconds=12)
            elif self.first_run == "created":
                win.flash(f"created a new connection list at "
                          f"{sshconfig.HOSTS_FILE}", seconds=12)
        win.present()


def list_hosts():
    """``alias<TAB>hostname<TAB>group`` per line, for Ultimate Mail. Reads
    the config directly; no window, no display."""
    try:
        config = SshConfig.load(SSH_CONFIG)
    except (OSError, sshconfig.ConfigError) as e:
        print(f"could not read {SSH_CONFIG}: {e}", file=sys.stderr)
        return 1
    for view in config.hosts():
        print(f"{view.alias}\t{view.hostname}\t{view.group}")
    return 0


if __name__ == "__main__":
    if "--list-hosts" in sys.argv[1:]:
        sys.exit(list_hosts())
    app = UltimateSsh()
    # Seed ~/.ultimate-ssh before any window reads it.
    app.first_run = sshconfig.ensure_app_home()
    sweep_control_sockets()   # clear masters left behind by a previous run
    sys.exit(app.run(sys.argv))
