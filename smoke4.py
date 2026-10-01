#!/usr/bin/env python3
"""GUI smoke test for settings, the auth fallback, and the file browser.

Everything runs against tests/fixture-config.txt and a temporary app home;
neither ~/.ssh/config nor ~/.ultimate-ssh is touched.
"""
import os
import re
import sys
import shutil
import tempfile

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Vte", "3.91")
from gi.repository import Gtk, GLib, Gdk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sshconfig

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "tests", "fixture-config.txt")
SCRATCH = os.environ.get("SMOKE_OUT", tempfile.gettempdir())

# Redirect the app home BEFORE importing the app module, which reads these.
_HOME = tempfile.mkdtemp(prefix="ultimate-ssh-test-")
sshconfig.APP_HOME = _HOME
sshconfig.HOSTS_FILE = os.path.join(_HOME, "config")
sshconfig.BACKUP_DIR = os.path.join(_HOME, "backups")
sshconfig.SESSION_FILE = os.path.join(_HOME, "session.json")
shutil.copy2(FIXTURE, sshconfig.HOSTS_FILE)
os.chmod(sshconfig.HOSTS_FILE, 0o600)

import ultimate_ssh as app  # noqa: E402
import editor  # noqa: E402

app.SSH_CONFIG = sshconfig.HOSTS_FILE
app.SESSION_FILE = sshconfig.SESSION_FILE
app.SETTINGS_FILE = os.path.join(_HOME, "settings.json")
editor.BACKUP_DIR = sshconfig.BACKUP_DIR

FAILURES = []
REAL_SSH_CONFIG = open(os.path.expanduser("~/.ssh/config"),
                       errors="replace").read()


def snapshot(path):
    """Contents of a real user path, or None if it does not exist.

    The app home may legitimately exist because the app is installed and in
    use; what matters is that the test does not touch it.
    """
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


REAL_APP_CONFIG = os.path.expanduser("~/.ultimate-ssh/config")
REAL_APP_SNAPSHOT = snapshot(REAL_APP_CONFIG)


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"   {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def labels_of(popover):
    out, child = [], popover.get_child().get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Button):
            out.append(child.get_label())
        child = child.get_next_sibling()
    return out


class Smoke(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="dev.ultimatessh.Smoke4")

    def do_activate(self):
        self.win = win = app.UltimateSshWindow(self)
        win.present()
        GLib.timeout_add(500, self.checks)

    def checks(self):
        win = self.win

        print("\n== app home ==")
        check("app home is not ~/.ssh", "/.ssh/" not in sshconfig.HOSTS_FILE)
        check("connections read from the app's own copy",
              sum(len(v) for v in win.sidebar.groups.values()) == 16)

        print("\n== local terminal buttons ==")

        def find_button(root, text):
            child = root.get_first_child() if root else None
            while child is not None:
                if isinstance(child, Gtk.Button):
                    inner = child.get_child()
                    if isinstance(inner, Gtk.Label) and text in (inner.get_label() or ""):
                        return child
                    if isinstance(inner, Gtk.Box):
                        sub = inner.get_first_child()
                        while sub is not None:
                            if isinstance(sub, Gtk.Label) and text in (sub.get_label() or ""):
                                return child
                            sub = sub.get_next_sibling()
                found = find_button(child, text)
                if found is not None:
                    return found
                child = child.get_next_sibling()
            return None

        welcome_btn = find_button(win.welcome, "Open local terminal")
        check("welcome screen has a big local-terminal button",
              welcome_btn is not None)
        check("it is prominent", welcome_btn is not None
              and welcome_btn.has_css_class("suggested-action"))
        header_btn = find_button(win.get_titlebar(), "Local terminal")
        check("header has one too, beside Colors", header_btn is not None)

        before = win.notebook.get_n_pages()
        welcome_btn.emit("clicked")
        check("clicking it opens a terminal tab",
              isinstance(win.current_tab(), app.TerminalTab))
        check("and replaces the welcome page",
              win.notebook.get_n_pages() == before)
        header_btn.emit("clicked")
        check("header button opens another",
              win.notebook.get_n_pages() == before + 1)

        print("\n== ssh argv ==")
        argv = app.ssh_argv("lab-web1")
        check("-F points at the app config",
              argv[1] == "-F" and argv[2] == sshconfig.HOSTS_FILE, argv[2])
        argv_u = app.ssh_argv("lab-web1", user="root")
        check("username override", "User=root" in argv_u, " ".join(argv_u[-4:]))
        argv_p = app.ssh_argv("lab-web1", user="root", force_password=True)
        check("force password disables pubkey",
              "PubkeyAuthentication=no" in argv_p)
        check("force password prefers password auth",
              any("PreferredAuthentications=password" in a for a in argv_p))
        argv_b = app.ssh_argv("lab-web1", "ls", batch=True)
        check("explorer uses BatchMode so it cannot hang",
              "BatchMode=yes" in argv_b)
        check("remote command passed after --",
              argv_b[-2] == "--" and argv_b[-1] == "ls")

        print("\n== connection sharing ==")
        argv_term = app.ssh_argv("lab-web1")
        argv_once = app.ssh_argv("lab-web1", "ls", allow_master=False)
        check("interactive sessions may create a master",
              "ControlMaster=auto" in argv_term)
        check("one-shot commands never create a master",
              "ControlMaster=no" in argv_once,
              " ".join(argv_once[:8]))
        check("the file browser is a one-shot caller",
              "ControlMaster=no" in app.ssh_argv("h", "find .", batch=True,
                                                 allow_master=False))
        check("scp never creates a master",
              "ControlMaster=no" in app.scp_argv(["a", "b:c"]))
        argv_nomux = app.ssh_argv("lab-web1", no_mux=True)
        check("fallback disables sharing entirely",
              "-S" in argv_nomux and "none" in argv_nomux)
        check("fallback carries no ControlPath",
              not any("ControlPath" in a for a in argv_nomux))

        import socket as _socket
        os.makedirs(app.RUNTIME_DIR, mode=0o700, exist_ok=True)
        dead = os.path.join(app.RUNTIME_DIR, "c-testdead")
        live = os.path.join(app.RUNTIME_DIR, "c-testlive")
        for p_ in (dead, live):
            if os.path.exists(p_):
                os.unlink(p_)
        s_dead = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        s_dead.bind(dead)          # bound but never listening -> connect refused
        s_dead.close()
        s_live = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        s_live.bind(live)
        s_live.listen(1)
        removed = app.sweep_control_sockets()
        check("stale socket swept", not os.path.exists(dead), f"removed {removed}")
        check("live master left alone", os.path.exists(live))
        s_live.close()
        os.path.exists(live) and os.unlink(live)

        marker_pane = win.current_tab().panes()[0] if win.current_tab() else None
        check("mux failure markers cover the real message",
              any(m in "mux_client_request_session: read from master failed: "
                       "broken pipe" for m in app.MUX_FAILURE_MARKERS))

        print("\n== tmux / screen / Claude sessions ==")
        plain = app.ssh_argv("lab-web1")
        check("plain mode runs no remote command", "--" not in plain)
        tmux = app.ssh_argv("lab-web1", persistence="tmux")
        check("tmux allocates a tty", "-t" in tmux)
        check("tmux attaches or creates",
              "tmux new-session -A -s ultimate" in tmux[-1], tmux[-1][:40])
        check("tmux falls back to a shell if missing",
              "not installed" in tmux[-1] and "exec \"$SHELL\"" in tmux[-1])
        screen = app.ssh_argv("lab-web1", persistence="screen")
        check("screen reattaches", "screen -DR ultimate" in screen[-1])
        check("screen allocates a tty", "-t" in screen)
        check("plain persistence is a no-op",
              app.ssh_argv("lab-web1", persistence="plain") == plain)

        claude = app.session_command(app.CLAUDE_COMMAND, "claude")
        check("Claude Code runs inside tmux",
              "tmux new-session -A -s claude claude" in claude)
        check("Claude falls back to running bare", "|| claude" in claude)
        check("session name substituted, braces intact",
              app.SESSION_TOKEN not in claude and "|| { echo" in claude)

        import subprocess as _sp
        for label, cmd in (("tmux", tmux[-1]), ("screen", screen[-1]),
                           ("claude", claude)):
            rc = _sp.run(["bash", "-n"], input=cmd, text=True,
                         capture_output=True).returncode
            check(f"{label} command is valid shell", rc == 0)

        check("persistence defaults to plain",
              app.DEFAULT_SETTINGS["persistence"] == "plain")
        win.apply_settings({"persistence": "tmux"})
        check("header button follows the mode",
              win.persist_btn.get_label() == "Session: tmux",
              win.persist_btn.get_label())
        win.cycle_persistence()
        check("cycling advances to screen",
              win.settings["persistence"] == "screen", win.settings["persistence"])
        win.cycle_persistence()
        check("cycling wraps back to plain",
              win.settings["persistence"] == "plain")

        print("\n== settings ==")
        check("defaults loaded", win.settings["theme"] == "system")
        win.apply_settings({"theme": "light", "font": "monospace 13",
                            "scrollback": 5000})
        check("settings written to disk", os.path.exists(app.SETTINGS_FILE))
        check("light theme selected", win.dark_terminal() is False)
        check("reload keeps the choice",
              app.load_settings()["theme"] == "light",
              app.load_settings()["theme"])
        win.new_local_shell()
        pane = win.current_tab().panes()[0]
        bg = pane.term.get_color_background_for_draw()
        check("light theme gives the terminal a light background",
              bg.red > 0.8 and bg.green > 0.8, f"{bg.red:.2f},{bg.green:.2f}")
        check("font applied to new terminals",
              pane.term.get_font().to_string().endswith("13"),
              pane.term.get_font().to_string())
        win.apply_settings({"theme": "dark", "font": "monospace 11",
                            "scrollback": 100000})
        bg = pane.term.get_color_background_for_draw()
        check("switching to dark repaints existing terminals",
              bg.red < 0.2 and bg.green < 0.2, f"{bg.red:.2f},{bg.green:.2f}")

        print("\n== split layouts ==")
        win.new_local_shell()
        qtab = win.current_tab()
        check("starts as one pane", len(qtab.panes()) == 1)
        win.set_active_pane(qtab.panes()[0])
        win.quad_layout()
        check("quad makes four panes", len(qtab.panes()) == 4,
              str(len(qtab.panes())))

        def paneds(widget, acc):
            if isinstance(widget, Gtk.Paned):
                acc.append(widget.get_orientation())
                for c in (widget.get_start_child(), widget.get_end_child()):
                    paneds(c, acc)
            return acc

        pending = [p for p in qtab.panes() if p.chooser is not None]
        check("the three new panes ask which host to use",
              len(pending) == 3, str(len(pending)))
        check("the original pane keeps its session",
              sum(1 for p in qtab.panes() if p.chooser is None) == 1)
        chooser = pending[0].chooser
        check("picker offers a local shell and a host search",
              chooser.search is not None and chooser.listbox is not None)
        rows = 0
        r = chooser.listbox.get_first_child()
        while r is not None:
            rows += 1; r = r.get_next_sibling()
        check("every host is listed in the picker", rows == 16, str(rows))
        chooser.search.set_text("lab-web2")
        visible = [x for x in [chooser.listbox.get_row_at_index(i)
                               for i in range(rows)] if x and chooser._matches(x)]
        check("the picker filters", len(visible) == 1, str(len(visible)))
        target = visible[0].host
        pending[0]._picked("host", target)
        check("picking a host connects that pane to it",
              pending[0].alias == "lab-web2" and pending[0].chooser is None,
              str(pending[0].alias))
        check("and it uses that host's ssh command",
              "lab-web2" in pending[0].argv)
        pending[1]._picked("local", None)
        check("picking local gives a shell",
              pending[1].alias is None and pending[1].chooser is None)
        pending[2]._picked("same", None)
        check("“same host” keeps the original target",
              pending[2].chooser is None)

        print("  -- pane identity --")
        win.new_local_shell()
        solo = win.current_tab()
        solo.refresh_identity()
        check("a lone pane shows no header strip",
              not solo.panes()[0].header.get_visible())
        win.close_current_tab()
        win.notebook.set_current_page(win.notebook.page_num(qtab))
        qtab.refresh_identity()
        check("split panes each get a header strip",
              all(p.header.get_visible() for p in qtab.panes()))
        check("the strip names the pane's own host",
              pending[0].header_label.get_text() == "lab-web2",
              pending[0].header_label.get_text())
        check("a local pane says so",
              pending[1].header_label.get_text() == "local",
              pending[1].header_label.get_text())

        win.set_active_pane(pending[0])
        check("the tab follows the focused pane",
              qtab.label_text.get_text().startswith("lab-web2"),
              qtab.label_text.get_text())
        check("and counts the others", "+3" in qtab.label_text.get_text(),
              qtab.label_text.get_text())
        check("the window title follows too",
              win.get_title().startswith("lab-web2"), win.get_title())
        win.set_active_pane(pending[1])
        check("moving focus renames it again",
              qtab.label_text.get_text().startswith("local"),
              qtab.label_text.get_text())
        check("only the focused strip is emphasised",
              "<b>" in pending[1].header_label.get_label()
              and "alpha" in pending[0].header_label.get_label())

        orients = paneds(qtab.hpaned.get_start_child(), [])
        check("it is a real 2x2, not four in a row",
              orients.count(Gtk.Orientation.HORIZONTAL) == 1
              and orients.count(Gtk.Orientation.VERTICAL) == 2, str(orients))
        check("quad refuses to run on an already-split tab",
              (win.quad_layout(), len(qtab.panes()))[1] == 4)
        check("a pane awaiting a choice is not a broadcast target",
              all(not p.alive for p in qtab.panes() if p.chooser is not None))

        win.set_active_pane(qtab.panes()[0])
        win.close_pane()
        check("closing a pane collapses back to three",
              len(qtab.panes()) == 3, str(len(qtab.panes())))
        check("the tab count drops with it",
              "+2" in qtab.label_text.get_text(), qtab.label_text.get_text())
        win.equalize_panes()
        check("even split runs without error", True)

        menu = win._build_layout_menu()
        labels, child = [], menu.get_child().get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Button):
                labels.append(child.get_label())
            child = child.get_next_sibling()
        for want in ("Split right", "Split down", "Quad", "Even split",
                     "Close pane"):
            check(f"layout menu offers {want!r}",
                  any(l.startswith(want) for l in labels), str(labels[:2]))
        win.close_current_tab()

        print("\n== sidebar spacing ==")
        def first_host_row(sb):
            row = sb.listbox.get_first_child()
            while row is not None:
                if getattr(row, "host", None) is not None:
                    return row
                row = row.get_next_sibling()
            return None

        win.apply_settings({"density": "compact"})
        check("compact is the default", app.DEFAULT_SETTINGS["density"] == "compact")
        row = first_host_row(win.sidebar)
        check("compact puts host and address on one line",
              row.get_child().get_orientation() == Gtk.Orientation.HORIZONTAL)
        check("and removes the vertical padding",
              row.get_child().get_margin_top() == 0)
        compact_h = row.get_child().measure(Gtk.Orientation.VERTICAL, -1)[0]

        win.apply_settings({"density": "comfortable"})
        row = first_host_row(win.sidebar)
        check("comfortable stacks them again",
              row.get_child().get_orientation() == Gtk.Orientation.VERTICAL)
        roomy_h = row.get_child().measure(Gtk.Orientation.VERTICAL, -1)[0]
        check("so compact rows really are shorter",
              compact_h < roomy_h, f"{compact_h}px vs {roomy_h}px")

        win.apply_settings({"density": "compact"})
        check("the choice persists", app.load_settings()["density"] == "compact")
        check("rows carry a tooltip for truncated names",
              first_host_row(win.sidebar).get_tooltip_text() is not None,
              str(first_host_row(win.sidebar).get_tooltip_text()))

        print("\n== font size ==")
        win.apply_settings({"font": "monospace 11"})
        pane_f = win.current_tab().panes()[0]
        win.zoom_font(2)
        check("zoom in raises the point size",
              win.settings["font"] == "monospace 13", win.settings["font"])
        check("open terminals resize immediately",
              pane_f.term.get_font().to_string() == "monospace 13",
              pane_f.term.get_font().to_string())
        win.zoom_font(-4)
        check("zoom out lowers it",
              win.settings["font"] == "monospace 9", win.settings["font"])
        for _ in range(40):
            win.zoom_font(-1)
        check("it cannot shrink past the floor",
              win.settings["font"] == f"monospace {app.FONT_MIN}",
              win.settings["font"])
        for _ in range(80):
            win.zoom_font(1)
        check("nor grow past the ceiling",
              win.settings["font"] == f"monospace {app.FONT_MAX}",
              win.settings["font"])
        win.reset_font()
        check("reset restores the default",
              win.settings["font"] == app.DEFAULT_SETTINGS["font"],
              win.settings["font"])
        check("the size survives a reload",
              app.load_settings()["font"] == app.DEFAULT_SETTINGS["font"])

        win.apply_settings({"font": "monospace 11"})
        handled = pane_f._on_scroll(
            type("E", (), {"get_current_event_state":
                           staticmethod(lambda: Gdk.ModifierType.CONTROL_MASK)})(),
            0, -1)
        check("Ctrl+scroll up zooms in",
              handled and win.settings["font"] == "monospace 12",
              win.settings["font"])
        plain = pane_f._on_scroll(
            type("E", (), {"get_current_event_state":
                           staticmethod(lambda: 0)})(), 0, -1)
        check("plain scroll is left to the terminal", plain is False)

        print("\n== settings dialog ==")
        dlg = editor.SettingsDialog(win, win.settings, win.apply_settings)
        check("dialog offers three themes", len(dlg.THEMES) == 3)
        check("dialog reflects current theme",
              dlg.THEMES[dlg.theme.get_selected()][1] == "dark")
        check("font is chosen with a real picker, not typed",
              isinstance(dlg.font, Gtk.FontDialogButton))
        check("picker shows the current font",
              dlg.font.get_font_desc().to_string() == win.settings["font"],
              dlg.font.get_font_desc().to_string())
        dlg.close()

        print("\n== auth fallback ==")
        # feed() is processed on the main loop, so the checks that read the
        # screen back run from timeouts rather than immediately
        pane = win.current_tab().panes()[0]

        def phase_auth_feed():
            # let the shell finish its startup banner first, or it scrolls the
            # message away and the test measures the wrong thing
            pane.term.reset(True, True)
            pane.term.feed(b"\r\nPermission denied (publickey,password).\r\n")
            GLib.timeout_add(300, lambda: (phase_auth_text(), False)[1])
            return False

        def phase_auth_text():
            check("auth failure recognised", pane._looks_like_auth_failure())
            pane.term.reset(True, True)
            pane.term.feed(b"\r\nbash: command not found\r\n")
            GLib.timeout_add(300, lambda: (phase_auth_clean(), False)[1])
            return False

        def phase_auth_clean():
            check("unrelated output is not misread",
                  not pane._looks_like_auth_failure())

        GLib.timeout_add(1500, phase_auth_feed)

        captured = {}
        creds = editor.CredentialsDialog(
            win, "lab-web1", "deploy",
            on_retry=lambda u, p, f: captured.update(user=u, password=p, force=f))
        check("username prefilled from the host",
              creds.user.get_text() == "deploy", creds.user.get_text())
        check("skip-keys defaults on", creds.force.get_active())
        creds.user.set_text("root")
        creds.password.set_text("hunter2")
        creds._go()
        check("dialog returns the new username", captured.get("user") == "root")
        check("dialog returns the password", captured.get("password") == "hunter2")
        check("dialog returns force-password", captured.get("force") is True)

        pane._arm_password("s3cret")
        check("password armed", pane._pending_password == "s3cret")
        check("prompt matcher accepts ssh's wording",
              bool(app.PASSWORD_PROMPT_RE.search("admin@host's password: ")))
        check("prompt matcher ignores ordinary text",
              not app.PASSWORD_PROMPT_RE.search("changed password policy"))
        pane._disarm_password()
        check("disarm clears it", pane._pending_password is None)

        print("\n== dead pane: R to reconnect, Q to close ==")
        dead_tab = win.current_tab()
        dead_pane = dead_tab.panes()[0]
        check("no key grab while the session is alive",
              dead_pane._dead_keys is None)
        dead_pane.alive = False
        dead_pane._arm_dead_keys()
        check("keys armed once it dies", dead_pane._dead_keys is not None)

        respawned = {}
        dead_pane.reconnect = lambda **kw: respawned.update(kw or {"hit": True})
        handled = dead_pane._on_dead_key(None, Gdk.KEY_r, 0, 0)
        check("R reconnects", handled and bool(respawned))
        respawned.clear()
        handled_upper = dead_pane._on_dead_key(None, Gdk.KEY_R, 0, 0)
        check("capital R works too", handled_upper and bool(respawned))

        # a live pane must never have its keystrokes stolen
        dead_pane.alive = True
        check("R passes through to a live shell",
              not dead_pane._on_dead_key(None, Gdk.KEY_r, 0, 0))
        dead_pane.alive = False
        check("Ctrl+R is left to the shell",
              not dead_pane._on_dead_key(None, Gdk.KEY_r, 0,
                                         Gdk.ModifierType.CONTROL_MASK))
        check("unrelated keys pass through",
              not dead_pane._on_dead_key(None, Gdk.KEY_x, 0, 0))

        closed = {}
        dead_tab.close_pane = lambda p: closed.update(pane=p)
        check("Q closes the pane",
              dead_pane._on_dead_key(None, Gdk.KEY_q, 0, 0)
              and closed.get("pane") is dead_pane)
        dead_pane._disarm_dead_keys()
        check("disarms cleanly", dead_pane._dead_keys is None)

        print("\n== file browser ==")
        fixture_dir = os.path.join(SCRATCH, "browser-fixture")
        shutil.rmtree(fixture_dir, ignore_errors=True)
        os.makedirs(os.path.join(fixture_dir, "assets"), exist_ok=True)
        for name in ("index.php", "logo.png", "deploy.sh", "notes.txt"):
            with open(os.path.join(fixture_dir, name), "w") as fh:
                fh.write("x" * 32)
        os.chmod(os.path.join(fixture_dir, "deploy.sh"), 0o755)

        real = app.ssh_argv
        app.ssh_argv = lambda alias, cmd=None, **kw: (
            ["/bin/bash", "-c", cmd] if cmd else real(alias, **kw))

        tab = win.current_tab()
        browser = app.ExplorerPane(tab, "fixture-host", lambda p: None)
        # must be in the widget tree: a popover on an unrealized parent
        # segfaults rather than failing politely
        tab.hpaned.set_end_child(browser)
        browser.navigate(fixture_dir, record=False)

        def after_listing():
            app.ssh_argv = real
            names = [c.entry.name for c in self.children(browser.grid)]
            check("grid populated", len(names) == 5, str(sorted(names)))
            check("folders sort first", names[0] == "assets", names[0])
            icons = {c.entry.name: app.entry_icon(c.entry)
                     for c in self.children(browser.grid)}
            check("folder icon", icons.get("assets") == "folder")
            check("image icon by extension",
                  icons.get("logo.png") == "image-x-generic")
            check("executable icon from mode bits",
                  icons.get("deploy.sh") == "application-x-executable",
                  icons.get("deploy.sh"))
            check("plain text icon", icons.get("notes.txt") == "text-x-generic")

            check("starts in grid view", browser.view_mode == "grid")
            browser.toggle_view()
            check("toggles to list view", browser.view_mode == "list")
            check("list view is one item per row",
                  browser.grid.get_max_children_per_line() == 1)
            browser.toggle_view()

            # unattached widget: no allocation to hit-test, so stand in for
            # the position lookup exactly as the sidebar test does
            child = self.children(browser.grid)[0]
            browser.grid.get_child_at_pos = lambda _x, _y: child
            browser._on_right_click(None, 1, 5, 5)
            menu = labels_of(browser._popover)
            for want in ("Rename…", "Permissions…", "Delete…", "Copy path"):
                check(f"context menu has {want!r}", want in menu)
            browser._popover.popdown()

            check("drop target installed for uploads",
                  browser._on_drop is not None)
            check("history starts empty", browser.history == [])
            check("a superseded listing is ignored",
                  browser._on_listed("/should/not/appear", [], None, seq=0)
                  is None and browser.path != "/should/not/appear",
                  browser.path)

            print("\n== follow the terminal ==")
            check("follow button is a toggle",
                  isinstance(browser.follow_btn, Gtk.ToggleButton))
            check("not following by default", not browser.following)
            check("no directory reported yet",
                  browser.terminal_path() is None,
                  str(browser.terminal_path()))

            # VTE parses OSC 7 out of the stream, so feeding the sequence a
            # shell would emit proves the whole path without a remote host
            target = os.path.join(fixture_dir, "assets")
            pane_t = tab.panes()[0].term
            pane_t.feed(("\033]7;file://localhost" + target + "\033\\").encode())
            GLib.timeout_add(400, lambda: (phase_follow(target), False)[1])

        def phase_follow(target):
            check("terminal directory picked up from OSC 7",
                  browser.terminal_path() == target, str(browser.terminal_path()))
            app.ssh_argv = lambda alias, cmd=None, **kw: (
                ["/bin/bash", "-c", cmd] if cmd else real(alias, **kw))
            browser.follow_btn.set_active(True)
            check("following is on", browser.following)
            GLib.timeout_add(900, lambda: (phase_followed(target), False)[1])

        def phase_followed(target):
            check("browser jumped to the terminal's directory",
                  browser.path == target, browser.path)
            names = [c.entry.name for c in self.children(browser.grid)]
            check("and listed that directory", names == [], str(names))

            # a cd in the shell moves the browser without any button press
            parent = os.path.dirname(target)
            tab.panes()[0].term.feed(
                ("\033]7;file://localhost" + parent + "\033\\").encode())
            GLib.timeout_add(900, lambda: (phase_followed_cd(parent), False)[1])

        def phase_followed_cd(parent):
            app.ssh_argv = real
            check("browser follows a directory change automatically",
                  browser.path == parent, browser.path)
            browser.follow_btn.set_active(False)
            check("toggling off stops following", not browser.following)
            check("watchers disconnected", browser.follow_handlers == [])

            print("\n== terminal colours toggle ==")
            win.colors_btn.set_active(True)
            captured = {}
            pane_c = tab.panes()[0]
            original = pane_c.term.set_colors
            pane_c.term.set_colors = (
                lambda fg, bg, pal: (captured.update(palette=pal),
                                     original(fg, bg, pal))[1])
            pane_c.apply_appearance()
            colourful = {c.to_string() for c in captured["palette"]}
            check("colours on gives a real palette", len(colourful) > 8,
                  f"{len(colourful)} distinct")

            win.colors_btn.set_active(False)
            pane_c.apply_appearance()
            flat = {c.to_string() for c in captured["palette"]}
            check("colours off collapses the palette", len(flat) == 1,
                  f"{len(flat)} distinct")
            check("button label follows state",
                  win.colors_btn.get_label() == "Colors: off",
                  win.colors_btn.get_label())
            check("choice persisted", app.load_settings()["colors"] is False)
            pane_c.term.set_colors = original
            win.colors_btn.set_active(True)
            check("toggling back restores colours",
                  app.load_settings()["colors"] is True)

            GLib.spawn_command_line_sync(
                f"spectacle -a -b -n -o {SCRATCH}/browser.png")
            self.finish()

        GLib.timeout_add(2600, lambda: (after_listing(), False)[1])
        return False

    @staticmethod
    def children(flowbox):
        out, child = [], flowbox.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.FlowBoxChild):
                out.append(child)
            child = child.get_next_sibling()
        return out

    def finish(self):
        print("\n== nothing outside the sandbox was touched ==")
        check("~/.ssh/config unchanged",
              open(os.path.expanduser("~/.ssh/config"),
                   errors="replace").read() == REAL_SSH_CONFIG)
        check("real connection list untouched",
              snapshot(REAL_APP_CONFIG) == REAL_APP_SNAPSHOT)
        print("\n" + ("ALL CHECKS PASSED" if not FAILURES
                      else f"FAILURES: {FAILURES}"))
        self.quit()


if __name__ == "__main__":
    try:
        Smoke().run([])
    finally:
        shutil.rmtree(_HOME, ignore_errors=True)
    sys.exit(1 if FAILURES else 0)
