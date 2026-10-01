# Ultimate SSH

A tabbed SSH terminal for people with too many hosts. Session tree built from
your SSH config, split panes, broadcast a command to a whole tier at once, and
a graphical remote file browser.

Built for Linux desktops, in the spirit of MobaXterm — but leaning on the parts
Linux already does well instead of reimplementing them.

```
sudo dnf install vte291-gtk4     # or let install.sh do it
./install.sh
ultimate-ssh
```

## The design

The division of labour is the whole point:

- **VTE** does terminal emulation — the widget behind GNOME Terminal.
- **`ssh(1)`** does transport, auth, agent forwarding, ProxyJump. There is no
  SSH library here, no second credential store, no second auth path. The file
  browser shells out to the same `ssh`.
- **Ultimate SSH** does the tree, the tabs, the fan-out and the file grid.

That is why it is a few thousand lines rather than a few hundred thousand.

## Your connections live in `~/.ultimate-ssh/config`

On first run your `~/.ssh/config` is **copied** into `~/.ultimate-ssh/config`.
From then on `ssh` is invoked with `-F` on that copy, and Ultimate SSH reads
and writes only it. **Your system `~/.ssh/config` is never modified.** Re-import
at any time from the menu.

Because `-F` replaces the system config entirely, the copy is self-contained:
defaults you want everywhere belong in its `Host *` block.

### Where `Host *` must live

ssh uses the **first** value it finds for each setting, so a `Host *` block
placed *above* your hosts silently overrides every per-host `User`, `Port` and
`IdentityFile` below it — you can edit a username, save it, and watch ssh
ignore it. `ssh -G <host>` shows what it really resolves.

Ultimate SSH warns at startup when your file has this problem, warns again in
the edit dialog for the affected fields, and **Menu → Fix defaults order**
moves the block to the end, which is what `ssh_config(5)` recommends. The block
is moved, not rewritten, and hosts without their own value still inherit the
default. The starter template already puts it last.

### Editing

Right-click anything in the sidebar. On a host: Connect, Copy `ssh <alias>`,
Edit, Duplicate, Move to category, Delete. On a category: Open all, New
connection here, Rename, Delete. `Ctrl+Shift+M` opens the full manager —
categories on the left, connections on the right, add/rename/delete on both.

Right-click edits save immediately. **The manager batches** — nothing reaches
disk until you press Save — so it warns you if you close it with unsaved work,
and its title shows "unsaved changes" while any is pending.

In the manager, a single click *selects* a connection so the buttons underneath
act on it; **double-click** (or Enter) opens it for editing. In the edit dialog
**Enter saves**, and closing it with unsaved edits asks first rather than
discarding them. Every label names
the file actually being written, which is never `~/.ssh/config`.

### How the config writer is kept safe

`sshconfig.py` is the only code that touches the file, contains no GTK, and is
covered by 65 headless tests.

- **Untouched blocks are emitted byte-for-byte.** The file is parsed into
  blocks that keep their original lines; a block is re-rendered only if it was
  edited. Editing one host produces a three-line diff, not a reformat.
- **A stanza owns only its indented lines**, so a section banner is never
  absorbed into the host above it and deleted along with it.
- **Every save writes a timestamped backup** to `~/.ultimate-ssh/backups/`.
- **Writes are atomic** (temp file + `os.replace`) and preserve permissions,
  so an interrupted save cannot leave a truncated config.
- **Outside edits are never clobbered**: the file's `(mtime_ns, size)` is
  recorded at load and the save is refused if it changed.
- Options the form doesn't model are preserved, not dropped.

## Groups

Categories come from banner comments in the config:

```
# --- Web tier ---
Host web-01
    HostName 10.10.1.10
```

Each group gets a stable hashed colour shown in the sidebar and on its tabs, so
a production tab never looks like a dev tab. Hosts above the first banner are
grouped by the `Generated from AWS account … (region)` header if there is one.

## Broadcast

`Ctrl+Shift+B` cycles **off → group → ALL**.

- **group** — mirror keystrokes to open panes whose category matches the pane
  you are typing in. Type once, hit every box in `Web tier`.
- **ALL** — every open pane.

A red banner names the target count and hosts while it is armed. Input mirrors
from the focused pane only, and dead sessions are skipped. The `+` on a group
row opens every host in it, asking first past 8 hosts.

## Remote file browser

A **local terminal** opens from the big button on the welcome screen, the one
in the header, or `Ctrl+Shift+T`.

`Ctrl+Shift+O` docks a graphical browser beside the terminal: icon grid or list
view, back/forward/up/home, drag-and-drop upload, and a right-click menu with
rename, permissions, delete, copy path and download.

Listings come from null-delimited `find(1)` output rather than parsed `ls -l`,
so filenames with spaces, quotes or newlines are not a parsing problem.
Requires GNU `find` on the remote.

### Following the terminal

The link button (🔗) makes the browser follow whatever directory the shell is
in — `cd` in the terminal and the file list moves with you. The terminal button
next to it does the reverse, sending `cd <path>` to the shell.

Following uses **OSC 7**, the escape sequence a shell emits to announce its
working directory. VTE parses it out of the stream, so there is no polling and
nothing is typed into your session.

Most remote shells don't emit it: the snippet that makes a local shell report
only fires when `VTE_VERSION` is set, and ssh does not forward that. So the
first time you press the button on a host that stays silent, Ultimate SSH
offers to install a `PROMPT_COMMAND` hook (bash) or `precmd` hook (zsh) **in
the running shell only**. Nothing is written to the remote host and it is gone
when you log out. Decline and the button simply switches back off.

In a split, the focused pane wins, and a terminal created by a later split is
picked up automatically.

### Connection multiplexing

Every `ssh` and `scp` shares one control socket per host:

```
-o ControlMaster=auto -o ControlPath=$XDG_RUNTIME_DIR/ultimate-ssh/c-%C -o ControlPersist=60
```

So opening the browser on a host you already have a terminal on costs no second
login, and listings return in milliseconds. `%C` hashes (host, port, user),
which keeps the socket path under the ~108-byte `sockaddr_un` limit. Browser
commands run with `BatchMode=yes` so they fail fast instead of hanging on a
password prompt with no terminal to type into.

**Only interactive sessions may create a master.** One-shot commands — every
directory listing, `scp`, `chmod` — run with `ControlMaster=no`: they join an
existing master or open their own connection, but never become the master
themselves. Otherwise a directory listing becomes the master your shell depends
on, and when that short command's socket expires the shell dies with
`mux_client_request_session: read from master failed: broken pipe`.

Sockets left behind by a master that was killed are swept at startup and before
each connect — connecting to the socket is the test, and a refusal means the
master is gone. If a session still dies from a broken master, Ultimate SSH
clears the socket and reconnects once **without** sharing rather than leaving
you stuck. Menu → **Reset SSH connection sharing** does it on demand.

## Host status bar

Along the bottom, for the host in the **focused pane**:

```
db-01  ·  CPU ██░░░░░░  24%  ·  MEM ███░░░░░  38% 5.9G/15.6G  ·  /  ████░░░░
43% 42.1G/98.3G  ·  net ↓ 4.7M/s  ↑ 812K/s  ·  load 0.42 0.30 0.28 (4 cpu)
·  up 12d 4h  ·  ⚠ /var 93%
```

Each reading is one command — `/proc/stat`, `/proc/meminfo`, `/proc/loadavg`,
`/proc/uptime`, `/proc/net/dev` and `df -kP` — sent over the control socket
that pane already holds open, so it costs no login and needs nothing installed
on the host. The
disk percentage is `df`'s own Capacity column, so the bar never argues with
`df -h` run in the terminal above it.

Only the focused pane is polled. Thirty idle tabs each running `df` every few
seconds is how a monitoring feature turns into the load it was meant to watch.
Readings are kept per host, so switching tabs shows the last numbers at once
rather than an empty bar.

CPU and the network rates are deltas between two polls, so both are blank for
the first few seconds on a host: a single sample can only give the total since
boot, which on a box that is up for months says nothing about now. Amber past
75%, red past 90%. `/` is the filesystem shown; any **other** filesystem over
90% gets its own red chip on the right, and the tooltip lists all of them with
sizes and devices.

`net` is what crossed the wire, down and up. It adds up the physical
interfaces only — bridges, container veths, VM taps and tunnels all carry
bytes that the interface underneath them has already counted, so including
them would report double the traffic. The tooltip breaks the rate out per
interface, busiest first, and marks the ones left out of the total. Interval
comes from the far end's own `/proc/uptime`, so a slow link's round-trip
jitter does not inflate the rate.

Click the bar to refresh it now. `Ctrl+Shift+S` hides it, and Settings sets the
interval (2–300 seconds, default 5).

A pane that fell back to a private connection after a multiplexing failure says
`connection sharing is off here` instead of polling — without the shared socket
every reading would be a fresh login.

## tmux and screen

The **Session** button in the header cycles **plain → tmux → screen**
(`Ctrl+Shift+P`) and applies to new connections. In tmux or screen mode a
connection runs:

```
tmux new-session -A -s ultimate        # attach if it exists, create if not
screen -DR ultimate
```

so the work survives a dropped connection, a closed laptop, or the app being
restarted underneath you. Reconnecting drops you straight back into the same
session. If the tool isn't installed on that host you get a plain login shell
and a one-line note, not a failed connection.

Right-click a host for a one-off **Connect in tmux** / **Connect in screen**
without changing the default.

Anything long-running — `apt upgrade`, a migration, a build — belongs in one of
these. A plain SSH session dies with its client.

## Claude Code

Right-click a host → **Run Claude Code (in tmux)** opens a tab running `claude`
on that host inside a tmux session named `claude`. Because it is wrapped in
tmux, closing the tab or losing the link leaves it running; picking the same
entry again reattaches to the session already in progress. Hosts without tmux
fall back to running `claude` directly, and hosts without `claude` fall back to
a shell.

## When a session dies

The pane says what happened and offers two keys: **R** to reconnect in place,
**Q** to close the pane. They are live only while no child process is running,
so a working shell never has its keystrokes taken — `Ctrl+R` still reaches the
shell's history search.

## When a login fails

If ssh exits with an authentication failure, the pane shows a retry bar. The
credentials dialog lets you connect as a **different username** — by far the
most common cause, since a `Host *` block can force one username onto every
host — optionally skipping key auth and going straight to a password, and can
save the working username back to that host.

A password typed into that dialog is never stored, never written to disk, never
placed on a command line and never put in an environment variable. It is held
in memory and typed once into ssh's own prompt, exactly as you would type it.
Leave it blank to type it yourself.

## Keys

Everything is `Ctrl+Shift+*` on purpose — bare `Ctrl+L`, `Ctrl+W`, `Ctrl+C` and
`Ctrl+T` belong to the remote shell and pass straight through.

| Key | Action |
|---|---|
| `Ctrl+Shift+L` | focus host filter (`Enter` connects to the top match) |
| `Ctrl+Shift+B` | cycle broadcast mode |
| `Ctrl+Shift+O` | toggle the remote file browser |
| `Ctrl+Shift+D` / `E` | split right / down |
| `Ctrl+Shift+X` | close pane |
| `Ctrl+Shift+K` | reconnect a dead pane in place |
| `R` / `Q` | on a dead pane: reconnect / close it |
| `Ctrl+Shift+M` | manage connections |
| `Ctrl+Shift+G` | terminal colours on / off |
| `Ctrl+Shift+S` | host status bar on / off |
| `Ctrl+Shift+P` | cycle session mode: plain / tmux / screen |
| `Ctrl+Shift+T` | local shell tab |
| `Ctrl+Shift+W` | close tab |
| `Ctrl+Shift+C` / `V` | copy / paste |
| `Alt+1..9` | switch tab |
| `Ctrl+Shift+R` | reload the connection list |

## Splitting a tab

The grid button in the header opens the layout menu: **Split right**, **Split
down**, **Quad — 2×2**, **Even split** and **Close pane**. Quad turns a single
pane into a 2×2 grid in one click; it starts from an unsplit tab, so close
extra panes first if you already have some.

A new pane doesn't guess what you want: it shows **"Connect this pane to…"**
with the full host list, a search box, a **Local shell** button, and — when the
tab is already on a host — **Same host**, focused, so Enter reproduces the old
clone-the-session behaviour in one keystroke. That means a quad can watch four
different servers at once, or four views of one.

Once a tab is split, each pane wears a small header naming the host it is
connected to, coloured by category, with the focused one in bold. The tab
itself renames to whichever pane you are in and counts the rest — `para-web1
+3` — so a quad of four different servers is never ambiguous. The window title
follows the same rule.

Dividers are draggable in both directions, and **Even split** resets them all
to 50/50. Panes connected to the same host share one connection, so four views
of one server cost one login.

| Key | Action |
|---|---|
| `Ctrl+Shift+D` / `E` | split right / down |
| `Ctrl+Shift+Q` | 2×2 quad |
| `Ctrl+Shift+X` | close pane |

## Settings

**Colors** in the header toggles terminal colouring — the highlighting `ls`,
`grep` and shell prompts apply to filenames, permissions and paths. Off
collapses the palette onto the foreground colour, so output is plain text while
bold and underline still come through. Nothing is asked of the remote shell,
and the choice is remembered.

Menu → Settings: theme (follow the system / dark / light), terminal font,
scrollback, and the host status bar with its refresh interval. The font is chosen with a real picker — family and size together,
previewed in the button. Theme and font apply to **open** terminals
immediately; scrollback applies to new ones. Stored in
`~/.ultimate-ssh/settings.json`.

**Sidebar spacing** has two modes. *Compact* (the default) keeps each host on
one line with its address dimmed to the right; *Comfortable* stacks the address
underneath, which doubles every row's height. Compact roughly halves the row
height, so about twice as many hosts fit on screen.

Text size can also be changed without opening Settings:

| Key | Action |
|---|---|
| `Ctrl+Shift` `+` | larger |
| `Ctrl+Shift` `-` | smaller |
| `Ctrl+Shift+0` | back to the default |
| `Ctrl` + scroll wheel | larger / smaller |

Sizes are clamped to 6–48 pt and remembered across restarts.

**Opacity** (Settings, 50–100 %, default 92 %) lets the desktop show through
the window, the way a Konsole profile's opacity does. Terminals and the welcome
page are see-through at that opacity; the sidebar, tab strip and status bar keep
a fill of the terminal background at the same opacity, so they stay readable.
100 % turns it off. It needs a compositing desktop (any Wayland session, or X11
with compositing on).

## Running a command

`ultimate-ssh --run COMMAND` opens a local tab running a shell command line,
like `konsole -e`, in the running window if there is one. `--title` names the
tab. The tab closes when the command succeeds; if it fails, it stays open with
the exit status, and R runs it again.

```
ultimate-ssh --title Claude --run "claude auth login --claudeai"
ultimate-ssh --host web-01            # a tab on a configured host
```

## Layout

| File | Role |
|---|---|
| `ultimate_ssh.py` | app, sidebar, terminals, broadcast, file browser |
| `sshconfig.py` | the only code that reads/writes the connection list — no GTK |
| `editor.py` | host, credentials, settings dialogs and the manager window |
| `install.sh` | installs to `~/.local`, `--uninstall` to remove |

## Tests

```sh
python3 test_sshconfig.py   # 65 checks, headless, no display needed
python3 smoke2.py           # 23 checks: broadcast, splits, listing parser
python3 smoke3.py           # 62 checks: menus, dialogs, manager, save safety
python3 smoke4.py           # 151 checks: settings, fonts, layout, auth, browser
```

345 checks. All run against `tests/fixture-config.txt`, a synthetic config with
invented hosts, and a temporary app home. Each asserts at exit that the fixture
is byte-identical and that nothing outside its sandbox was created.

## Known limits

- `Include` directives in the config are not followed.
- Remote listing needs GNU `find -printf`; a BSD or macOS host returns empty.
- The status bar reads `/proc`, so it stays blank on BSD, macOS and appliances.
- Downloads are files only — no recursive directory copy.
- No transfer progress bar; `scp` runs to completion, then the status updates.
- Split layouts are not saved by session restore (open hosts are).
- GTK prints harmless `GtkGizmo (slider)` warnings to stderr.

## Requirements

Linux with GTK4, VTE 3.91 (`vte291-gtk4`), PyGObject, Python 3.9+, and the
OpenSSH client. `install.sh` handles dnf, apt, pacman, zypper and apk.

## License

MIT — see [LICENSE](LICENSE).
