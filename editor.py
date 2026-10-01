#!/usr/bin/env python3
"""Editing UI for the connection list: host dialog and manager window.

All the dangerous work lives in sshconfig.py. This module only collects input
and reports errors -- it never formats or writes the file itself.
"""

import os

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib, Gio, Gdk, Pango  # noqa: E402

import sshconfig
from sshconfig import SshConfig, ConfigError, KNOWN_KEYS

BACKUP_DIR = sshconfig.BACKUP_DIR


def display_path(path):
    """~-shortened path for labels, so they name the file actually written."""
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home) else path


def error_dialog(parent, message, detail=""):
    dlg = Gtk.AlertDialog(message=message, detail=detail,
                          buttons=["OK"], cancel_button=0, default_button=0)
    dlg.show(parent)


def confirm(parent, message, detail, ok_label, on_ok):
    dlg = Gtk.AlertDialog(message=message, detail=detail,
                          buttons=["Cancel", ok_label],
                          cancel_button=0, default_button=1)

    def answered(d, res):
        try:
            if d.choose_finish(res) == 1:
                on_ok()
        except GLib.Error:
            pass

    dlg.choose(parent, None, answered)


def confirm_or_cancel(parent, message, detail, ok_label, on_ok, on_cancel):
    """confirm(), but the caller also hears about Cancel.

    Needed where declining has to undo something the click already did, such
    as a toggle button that must snap back.
    """
    dlg = Gtk.AlertDialog(message=message, detail=detail,
                          buttons=["Cancel", ok_label],
                          cancel_button=0, default_button=1)

    def answered(d, res):
        try:
            chosen = d.choose_finish(res)
        except GLib.Error:
            chosen = 0
        (on_ok if chosen == 1 else on_cancel)()

    dlg.choose(parent, None, answered)


def save_config(parent, config):
    """Persist, with a backup. Returns the backup path, or None on failure."""
    try:
        return config.save(backup_dir=BACKUP_DIR) or ""
    except sshconfig.ConflictError as exc:
        error_dialog(parent, "Config changed on disk", str(exc))
    except ConfigError as exc:
        error_dialog(parent, "Could not save", str(exc))
    except OSError as exc:
        error_dialog(parent, "Could not save", str(exc))
    return None


class _Modal(Gtk.Window):
    """Gtk.Dialog is deprecated in GTK4; this is the small replacement."""

    def __init__(self, parent, title, width=460):
        super().__init__(title=title, modal=True, transient_for=parent)
        self.set_default_size(width, -1)
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.box.set_margin_top(16)
        self.box.set_margin_bottom(16)
        self.box.set_margin_start(16)
        self.box.set_margin_end(16)
        self.set_child(self.box)

    def add_buttons(self, ok_label, on_ok, destructive=False):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                      halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _b: self.close())
        row.append(cancel)

        ok = Gtk.Button(label=ok_label)
        ok.add_css_class("destructive-action" if destructive
                         else "suggested-action")
        ok.connect("clicked", lambda _b: on_ok())
        row.append(ok)
        self.box.append(row)
        self.ok_button = ok
        # Without a default widget, Enter in a field does nothing at all and
        # the edit looks accepted while going nowhere.
        self.set_default_widget(ok)


def prompt_text(parent, title, label, initial, on_ok, secret=False):
    """One-line text prompt (rename a category, name a new one, ...)."""
    win = _Modal(parent, title)
    win.box.append(Gtk.Label(label=label, xalign=0.0))
    entry = Gtk.Entry(text=initial or "")
    if secret:
        entry.set_visibility(False)
        entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
    win.box.append(entry)

    def accept():
        # a passphrase is taken as typed; trimming it would silently break it
        value = entry.get_text() if secret else entry.get_text().strip()
        if not value:
            return
        win.close()
        on_ok(value)

    entry.connect("activate", lambda _e: accept())
    win.add_buttons("OK", accept)
    win.present()
    entry.grab_focus()


def prompt_choice(parent, title, label, options, on_ok, extra_note=None):
    win = _Modal(parent, title)
    win.box.append(Gtk.Label(label=label, xalign=0.0, wrap=True))
    dropdown = Gtk.DropDown.new_from_strings(options)
    win.box.append(dropdown)
    if extra_note:
        note = Gtk.Label(label=extra_note, xalign=0.0, wrap=True)
        note.add_css_class("dim-label")
        win.box.append(note)

    def accept():
        idx = dropdown.get_selected()
        win.close()
        if 0 <= idx < len(options):
            on_ok(options[idx])

    win.add_buttons("OK", accept)
    win.present()


class CredentialsDialog(_Modal):
    """Retry a failed login as a different user, optionally with a password.

    The password is never stored, never written to disk and never placed on a
    command line -- it is handed back to the caller, typed once into ssh's own
    prompt, and dropped.
    """

    def __init__(self, parent, alias, current_user, on_retry, on_remember=None):
        super().__init__(parent, f"Sign in to {alias}", width=460)
        self.on_retry = on_retry
        self.on_remember = on_remember

        intro = Gtk.Label(xalign=0.0, wrap=True)
        intro.set_markup(
            "Authentication failed. The usual cause is the wrong "
            "<b>username</b> — a <tt>Host *</tt> default can force one onto "
            "every host.")
        self.box.append(intro)

        grid = Gtk.Grid(row_spacing=8, column_spacing=12)
        lab = Gtk.Label(label="Username", xalign=1.0)
        lab.add_css_class("dim-label")
        grid.attach(lab, 0, 0, 1, 1)
        self.user = Gtk.Entry(text=current_user or "", hexpand=True)
        self.user.set_placeholder_text("root, ubuntu, ec2-user …")
        grid.attach(self.user, 1, 0, 1, 1)

        lab2 = Gtk.Label(label="Password", xalign=1.0)
        lab2.add_css_class("dim-label")
        grid.attach(lab2, 0, 1, 1, 1)
        self.password = Gtk.PasswordEntry(show_peek_icon=True, hexpand=True)
        grid.attach(self.password, 1, 1, 1, 1)
        self.box.append(grid)

        hint = Gtk.Label(xalign=0.0, wrap=True)
        hint.add_css_class("dim-label")
        hint.set_text("Leave the password blank to type it at ssh's own "
                      "prompt. Nothing you enter here is saved.")
        self.box.append(hint)

        self.force = Gtk.CheckButton(
            label="Skip key authentication and use the password")
        self.force.set_active(True)
        self.box.append(self.force)

        self.remember = Gtk.CheckButton(
            label=f"Save this username to “{alias}” in my connection list")
        self.box.append(self.remember)

        self.add_buttons("Connect", self._go)
        self.user.set_activates_default(True)

    def _go(self):
        user = self.user.get_text().strip()
        password = self.password.get_text()
        force = self.force.get_active()
        remember = self.remember.get_active()
        self.close()
        if remember and user and self.on_remember:
            self.on_remember(user)
        self.on_retry(user or None, password or None, force)


class SettingsDialog(_Modal):
    """Appearance and terminal preferences."""

    THEMES = [("Follow the system", "system"),
              ("Dark", "dark"),
              ("Light", "light")]
    DENSITIES = [("Compact — one line per host", "compact"),
                 ("Comfortable — hostname on its own line", "comfortable")]

    def __init__(self, parent, settings, on_apply):
        super().__init__(parent, "Settings", width=460)
        self.settings = settings
        self.on_apply = on_apply

        grid = Gtk.Grid(row_spacing=10, column_spacing=12)

        lab = Gtk.Label(label="Theme", xalign=1.0)
        lab.add_css_class("dim-label")
        grid.attach(lab, 0, 0, 1, 1)
        self.theme = Gtk.DropDown.new_from_strings([t[0] for t in self.THEMES])
        keys = [t[1] for t in self.THEMES]
        if settings.get("theme", "system") in keys:
            self.theme.set_selected(keys.index(settings.get("theme", "system")))
        self.theme.set_hexpand(True)
        grid.attach(self.theme, 1, 0, 1, 1)

        lab2 = Gtk.Label(label="Terminal font", xalign=1.0)
        lab2.add_css_class("dim-label")
        grid.attach(lab2, 0, 1, 1, 1)
        # a real picker: family and size together, previewed in the button
        self.font = Gtk.FontDialogButton.new(Gtk.FontDialog())
        self.font.set_font_desc(
            Pango.FontDescription(settings.get("font", "monospace 11")))
        self.font.set_use_font(True)
        self.font.set_hexpand(True)
        grid.attach(self.font, 1, 1, 1, 1)

        lab4 = Gtk.Label(label="Sidebar spacing", xalign=1.0)
        lab4.add_css_class("dim-label")
        grid.attach(lab4, 0, 2, 1, 1)
        self.density = Gtk.DropDown.new_from_strings(
            [d[0] for d in self.DENSITIES])
        keys = [d[1] for d in self.DENSITIES]
        current = settings.get("density", "compact")
        if current in keys:
            self.density.set_selected(keys.index(current))
        self.density.set_hexpand(True)
        grid.attach(self.density, 1, 2, 1, 1)

        lab3 = Gtk.Label(label="Scrollback lines", xalign=1.0)
        lab3.add_css_class("dim-label")
        grid.attach(lab3, 0, 3, 1, 1)
        self.scrollback = Gtk.Entry(
            text=str(settings.get("scrollback", 100000)), hexpand=True)
        grid.attach(self.scrollback, 1, 3, 1, 1)

        lab5 = Gtk.Label(label="Status bar", xalign=1.0)
        lab5.add_css_class("dim-label")
        grid.attach(lab5, 0, 4, 1, 1)
        self.statusbar = Gtk.CheckButton(
            label="Show CPU, memory and disk for the focused host")
        self.statusbar.set_active(bool(settings.get("statusbar", True)))
        grid.attach(self.statusbar, 1, 4, 1, 1)

        lab6 = Gtk.Label(label="Refresh every", xalign=1.0)
        lab6.add_css_class("dim-label")
        grid.attach(lab6, 0, 5, 1, 1)
        pace = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.statusbar_interval = Gtk.Entry(
            text=str(settings.get("statusbar_interval", 5)), width_chars=5)
        pace.append(self.statusbar_interval)
        seconds = Gtk.Label(label="seconds", xalign=0.0)
        seconds.add_css_class("dim-label")
        pace.append(seconds)
        grid.attach(pace, 1, 5, 1, 1)

        lab7 = Gtk.Label(label="Opacity", xalign=1.0)
        lab7.add_css_class("dim-label")
        grid.attach(lab7, 0, 6, 1, 1)
        self.opacity = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 50, 100, 1)
        self.opacity.set_value(round(100 * float(settings.get("opacity", 1.0))))
        self.opacity.set_digits(0)
        self.opacity.set_hexpand(True)
        self.opacity.set_format_value_func(lambda _s, v: f"{v:.0f}%")
        grid.attach(self.opacity, 1, 6, 1, 1)
        self.box.append(grid)

        note = Gtk.Label(xalign=0.0, wrap=True)
        note.add_css_class("dim-label")
        note.set_text("Theme and font apply to open terminals immediately; "
                      "scrollback applies to new ones. A monospaced font is "
                      "strongly recommended. Ctrl+Shift+ +/- resizes on the "
                      "fly, Ctrl+Shift+0 resets.\n\n"
                      "The status bar reads /proc and df on the host in the "
                      "focused pane only, over the SSH connection that pane "
                      "already holds open. Ctrl+Shift+S hides it.\n\n"
                      "Below 100% opacity the desktop shows through the "
                      "terminals.")
        self.box.append(note)

        self.add_buttons("Apply", self._apply)

    def _apply(self):
        try:
            scrollback = max(1000, int(self.scrollback.get_text().strip()))
        except ValueError:
            scrollback = 100000
        desc = self.font.get_font_desc()
        try:
            # Matches STATS_INTERVAL_MIN/MAX in the app: under two seconds the
            # CPU delta is measured over too few jiffies to mean anything.
            pace = min(300, max(2, int(
                self.statusbar_interval.get_text().strip())))
        except ValueError:
            pace = 5
        values = {
            "theme": self.THEMES[self.theme.get_selected()][1],
            "font": desc.to_string() if desc else "monospace 11",
            "scrollback": scrollback,
            "density": self.DENSITIES[self.density.get_selected()][1],
            "statusbar": self.statusbar.get_active(),
            "statusbar_interval": pace,
            "opacity": self.opacity.get_value() / 100,
        }
        self.close()
        self.on_apply(values)


class HostDialog(_Modal):
    """Create or edit one Host stanza.

    Options the form doesn't model are kept in the "other options" box rather
    than silently dropped -- a config can contain anything ssh understands.
    """

    def __init__(self, parent, config, view=None, group=None, on_done=None):
        super().__init__(parent,
                         f"Edit {view.alias}" if view else "New SSH connection",
                         width=520)
        self.config = config
        self.view = view
        self.on_done = on_done

        grid = Gtk.Grid(row_spacing=8, column_spacing=12)
        self.entries = {}

        def row(n, label, value, placeholder="", trailing=None):
            lab = Gtk.Label(label=label, xalign=1.0)
            lab.add_css_class("dim-label")
            grid.attach(lab, 0, n, 1, 1)
            entry = Gtk.Entry(text=value or "", hexpand=True)
            entry.set_activates_default(True)
            if placeholder:
                entry.set_placeholder_text(placeholder)
            if trailing is None:
                grid.attach(entry, 1, n, 1, 1)
            else:
                pair = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                               spacing=6)
                pair.append(entry)
                pair.append(trailing)
                grid.attach(pair, 1, n, 1, 1)
            return entry

        # Re-checks as the key path is typed, so a bad key is caught here
        # rather than as a baffling ssh error minutes later.
        self.key_note = Gtk.Label(xalign=0.0, wrap=True)
        self.key_note.set_margin_start(4)
        self.key_fix = Gtk.Button()
        self.key_fix.add_css_class("flat")
        self.key_fix.connect("clicked", lambda _b: self._fix_key())
        self.key_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                               spacing=8)
        self.key_row.append(self.key_note)
        self.key_row.append(self.key_fix)
        self.key_row.set_visible(False)
        self._key_report = None

        block = view.block if view else None
        self.entries["Alias"] = row(
            0, "Name", view.alias if view else "", "web-01")
        n = 0
        for key in KNOWN_KEYS:
            n += 1
            hint = {"HostName": "10.0.0.5 or host.example.com",
                    "Port": "22", "User": "",
                    "IdentityFile": "~/.ssh/id_ed25519",
                    "ProxyJump": ""}.get(key, "")
            trailing = None
            if key == "IdentityFile":
                trailing = Gtk.Button(label="Browse…")
                trailing.set_tooltip_text(
                    "Pick a private key file (not the .pub)")
                trailing.connect("clicked", self._browse_identity)
            self.entries[key] = row(n, key, block.get(key) if block else "",
                                    hint, trailing)
            if key == "IdentityFile":
                # immediately beneath the field it is about, wherever
                # KNOWN_KEYS happens to place that
                n += 1
                grid.attach(self.key_row, 1, n, 1, 1)
                self.entries[key].connect("changed",
                                          lambda _e: self._check_key())

        cat_label = Gtk.Label(label="Category", xalign=1.0)
        cat_label.add_css_class("dim-label")
        grid.attach(cat_label, 0, n + 1, 1, 1)

        self.groups = config.group_names()
        current = group or (view.group if view else None)
        if current and current not in self.groups:
            self.groups.append(current)
        self.dropdown = Gtk.DropDown.new_from_strings(self.groups)
        if current in self.groups:
            self.dropdown.set_selected(self.groups.index(current))
        grid.attach(self.dropdown, 1, n + 1, 1, 1)
        self.box.append(grid)

        self.box.append(Gtk.Label(label="Other ssh_config options",
                                  xalign=0.0))
        self.extra = Gtk.TextView()
        self.extra.set_monospace(True)
        self.extra.get_buffer().set_text(block.extra_options() if block else "")
        scroller = Gtk.ScrolledWindow()
        scroller.set_size_request(-1, 90)
        scroller.set_child(self.extra)
        scroller.add_css_class("frame")
        self.box.append(scroller)

        shadowed = sorted(config.shadowed_keys() & set(KNOWN_KEYS))
        if shadowed:
            warn = Gtk.Label(xalign=0.0, wrap=True)
            warn.set_markup(
                "<span color='#ff8080'>⚠ A <tt>Host *</tt> block above these "
                "hosts already sets <b>" + ", ".join(shadowed) + "</b>. ssh "
                "uses the first value it finds, so changes to those fields "
                "here will have no effect.</span>\n"
                "<span alpha='70%'>Menu → “Fix defaults order” moves that "
                "block to the end of the file, which is where ssh_config(5) "
                "says it belongs.</span>")
            self.box.append(warn)

        if view and config.is_generated_group(view.group):
            warn = Gtk.Label(xalign=0.0, wrap=True)
            warn.set_markup(
                "<span color='#c4a000'>⚠ This host came from the AWS "
                "generator at the top of the file. Re-running "
                "<tt>describe-instances</tt> will overwrite these edits.</span>")
            self.box.append(warn)

        self.add_buttons("Save", self._save)
        self._initial = self._snapshot()
        self.connect("close-request", self._on_close_request)
        self.entries["Alias"].grab_focus()
        self._check_key()       # "changed" only fires on later edits

    # -- identity file checking -------------------------------------------

    def _check_key(self):
        """Re-inspect the key field and show what ssh would trip over."""
        report = sshconfig.inspect_identity_file(
            self.entries["IdentityFile"].get_text())
        self._key_report = report
        if report is None or report.ok:
            self.key_row.set_visible(False)
            return

        colour = "#c4a000" if report.codes <= {"network"} else "#ff8080"
        self.key_note.set_markup(
            f"<span color='{colour}'>⚠ This key "
            f"{GLib.markup_escape_text(report.summary())}.</span>")

        if report.can_convert:
            self.key_fix.set_label("Convert to OpenSSH…")
            self.key_fix.set_visible(True)
        elif report.can_chmod:
            self.key_fix.set_label("Fix permissions")
            self.key_fix.set_visible(True)
        else:
            self.key_fix.set_visible(False)
        self.key_row.set_visible(True)

    def _fix_key(self):
        report = self._key_report
        if report is None:
            return

        if report.can_chmod and not report.can_convert:
            try:
                sshconfig.fix_identity_permissions(report.path)
            except OSError as exc:
                error_dialog(self, "Could not change the permissions",
                             str(exc))
            self._check_key()
            return

        if not report.can_convert:
            return
        if sshconfig.putty_key_is_encrypted(report.path):
            prompt_text(self, "Passphrase needed",
                        "This .ppk is passphrase-protected. Enter its "
                        "passphrase to convert it:", "",
                        lambda pw: self._convert_key(report.path, pw),
                        secret=True)
        else:
            self._convert_key(report.path, None)

    def _convert_key(self, path, passphrase):
        try:
            dest = sshconfig.convert_putty_key(path, passphrase=passphrase)
        except ConfigError as exc:
            error_dialog(self, "Could not convert the key", str(exc))
            return
        # the .ppk is left alone: it is still what PuTTY and WinSCP expect
        self.entries["IdentityFile"].set_text(display_path(dest))
        self._check_key()

    def _browse_identity(self, _button):
        """File picker for IdentityFile, so the path needn't be typed.

        Opens on whatever the field already names, else ~/.ssh, and stores the
        result ~-shortened -- that is what ssh_config(5) reads and it keeps the
        file portable between machines.
        """
        entry = self.entries["IdentityFile"]
        dialog = Gtk.FileDialog(title="Select an SSH private key")

        current = os.path.expanduser(entry.get_text().strip())
        ssh_dir = os.path.expanduser("~/.ssh")
        if current and os.path.isfile(current):
            dialog.set_initial_file(Gio.File.new_for_path(current))
        elif os.path.isdir(ssh_dir):
            dialog.set_initial_folder(Gio.File.new_for_path(ssh_dir))

        def chosen(dlg, res):
            try:
                gfile = dlg.open_finish(res)
            except GLib.Error:
                return                          # cancelled
            path = gfile.get_path()
            if not path:
                error_dialog(self, "That key isn't on this machine",
                             "ssh needs a local path it can read, so a remote "
                             "or virtual location won't work.")
                return
            entry.set_text(display_path(path))

        dialog.open(self, None, chosen)

    def _snapshot(self):
        buf = self.extra.get_buffer()
        return (
            tuple(self.entries[k].get_text() for k in ["Alias"] + KNOWN_KEYS),
            buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False),
            self.dropdown.get_selected(),
        )

    def _on_close_request(self, _win):
        """Closing used to bin whatever had been typed, without a word."""
        if getattr(self, "_saved", False) or self._snapshot() == self._initial:
            return False

        dialog = Gtk.AlertDialog(
            message="Save your changes?",
            detail="This connection has edits that have not been saved.",
            buttons=["Cancel", "Discard them", "Save"],
            cancel_button=0, default_button=2)

        def answered(dlg, res):
            try:
                choice = dlg.choose_finish(res)
            except GLib.Error:
                choice = 0
            if choice == 2:
                self._save()
            elif choice == 1:
                self._saved = True      # let the next close through
                self.destroy()

        dialog.choose(self, None, answered)
        return True

    def _save(self):
        buf = self.extra.get_buffer()
        extra = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        alias = self.entries["Alias"].get_text().strip()
        options = [(k, self.entries[k].get_text()) for k in KNOWN_KEYS]
        idx = self.dropdown.get_selected()
        group = self.groups[idx] if 0 <= idx < len(self.groups) else None

        try:
            if self.view is None:
                self.config.add_host(alias, options, group)
                # add_host doesn't take extras; apply them to the new block
                view = self.config.find(alias)
                if view is not None and extra.strip():
                    view.block.set_extra_options(extra)
                    for k, v in options:
                        view.block.set(k, v)
            else:
                self.config.update_host(self.view, alias=alias, options=options,
                                        extra=extra, group=group)
        except ConfigError as exc:
            error_dialog(self, "Invalid connection", str(exc))
            return

        self._saved = True
        self.close()
        if self.on_done:
            self.on_done()


class ManagerWindow(Gtk.Window):
    """Full-list editor: categories on the left, their hosts on the right.

    Edits accumulate in memory; nothing reaches disk until Save.
    """

    def __init__(self, parent, path, on_saved):
        super().__init__(title="Manage SSH connections", transient_for=parent)
        self.set_default_size(900, 620)
        self.path = path
        self.on_saved = on_saved
        self.dirty = False
        self.selected_group = None

        self.config = SshConfig.load(path)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_child(outer)

        panes = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, vexpand=True)
        panes.set_position(280)
        outer.append(panes)

        panes.set_start_child(self._build_groups())
        panes.set_end_child(self._build_hosts())

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.set_margin_top(8)
        footer.set_margin_bottom(10)
        footer.set_margin_start(10)
        footer.set_margin_end(10)

        self.status = Gtk.Label(xalign=0.0, hexpand=True, wrap=True)
        self.status.add_css_class("dim-label")
        footer.append(self.status)

        revert = Gtk.Button(label="Discard changes")
        revert.connect("clicked", lambda _b: self.reload())
        footer.append(revert)

        self.save_btn = Gtk.Button(
            label=f"Save to {display_path(path)}")
        self.save_btn.add_css_class("suggested-action")
        self.save_btn.connect("clicked", lambda _b: self.save())
        footer.append(self.save_btn)
        outer.append(footer)

        self.connect("close-request", self._on_close_request)
        self.refresh()

    def _update_title(self):
        self.set_title("Manage SSH connections"
                       + (" — unsaved changes" if self.dirty else ""))

    def _on_close_request(self, _win):
        """Closing with pending edits used to discard them silently."""
        if not self.dirty:
            return False

        dialog = Gtk.AlertDialog(
            message="Save your changes?",
            detail=("Your edits to the connection list have not been written "
                    f"to {display_path(self.path)} yet. Closing now discards "
                    "them."),
            buttons=["Cancel", "Discard them", "Save"],
            cancel_button=0, default_button=2)

        def answered(dlg, res):
            try:
                choice = dlg.choose_finish(res)
            except GLib.Error:
                choice = 0
            if choice == 2:
                if self.save():
                    self.destroy()
            elif choice == 1:
                self.dirty = False
                self.destroy()

        dialog.choose(self, None, answered)
        return True     # hold the window open until the question is answered

    # -- layout ----------------------------------------------------------

    def _toolbar(self, specs):
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        bar.set_margin_start(8)
        bar.set_margin_end(8)
        bar.set_margin_bottom(8)
        for label, tooltip, handler in specs:
            btn = Gtk.Button(label=label)
            btn.set_tooltip_text(tooltip)
            btn.connect("clicked", lambda _b, h=handler: h())
            bar.append(btn)
        return bar

    def _build_groups(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        header = Gtk.Label(xalign=0.0)
        header.set_markup("<b>Categories</b>")
        header.set_margin_top(10)
        header.set_margin_start(10)
        header.set_margin_bottom(6)
        box.append(header)

        self.group_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.group_list.connect("row-selected", self._on_group_selected)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(self.group_list)
        box.append(scroller)

        box.append(self._toolbar([
            ("Add", "Create a new category", self.add_group),
            ("Rename", "Rename the selected category", self.rename_group),
            ("Delete", "Delete the selected category", self.delete_group),
        ]))
        return box

    def _build_hosts(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.hosts_header = Gtk.Label(xalign=0.0)
        self.hosts_header.set_markup("<b>Connections</b>")
        self.hosts_header.set_margin_top(10)
        self.hosts_header.set_margin_start(10)
        self.hosts_header.set_margin_bottom(6)
        box.append(self.hosts_header)

        self.host_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        # Single click selects so the buttons below can act on it; only a
        # double click opens the editor. Activating a row on one click made
        # every attempt to select something pop a dialog instead.
        clicks = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY)
        clicks.connect("pressed", self._on_host_click)
        self.host_list.add_controller(clicks)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_host_key)
        self.host_list.add_controller(keys)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(self.host_list)
        box.append(scroller)

        box.append(self._toolbar([
            ("Add", "Add a connection to this category", self.add_host),
            ("Edit", "Edit the selected connection", self.edit_host),
            ("Duplicate", "Copy the selected connection", self.duplicate_host),
            ("Move…", "Move it to another category", self.move_host),
            ("Delete", "Delete the selected connection", self.delete_host),
        ]))
        return box

    # -- state -----------------------------------------------------------

    def mark_dirty(self):
        self.dirty = True
        self.refresh()
        self._update_title()

    def reload(self):
        self.config = SshConfig.load(self.path)
        self.dirty = False
        self.refresh()
        self._update_title()

    def refresh(self):
        groups = self.config.groups()
        if self.selected_group not in groups:
            self.selected_group = next(iter(groups), None)

        while (row := self.group_list.get_first_child()) is not None:
            self.group_list.remove(row)
        target_row = None
        for name, members in groups.items():
            row = Gtk.ListBoxRow()
            row.group = name
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            line.set_margin_top(6)
            line.set_margin_bottom(6)
            line.set_margin_start(10)
            line.set_margin_end(10)
            label = Gtk.Label(label=name, xalign=0.0, hexpand=True)
            label.set_ellipsize(3)
            line.append(label)
            tally = Gtk.Label(label=str(len(members)))
            tally.add_css_class("dim-label")
            line.append(tally)
            row.set_child(line)
            self.group_list.append(row)
            if name == self.selected_group:
                target_row = row
        if target_row is not None:
            self.group_list.select_row(target_row)

        self._refresh_hosts()

        total = sum(len(v) for v in groups.values())
        state = "unsaved changes" if self.dirty else "no changes"
        self.status.set_text(
            f"{total} connections · {len(groups)} categories · {state}")
        self.save_btn.set_sensitive(self.dirty)

    def _refresh_hosts(self):
        while (row := self.host_list.get_first_child()) is not None:
            self.host_list.remove(row)

        members = self.config.groups().get(self.selected_group, [])
        self.hosts_header.set_markup(
            f"<b>Connections</b> · "
            f"{GLib.markup_escape_text(self.selected_group or '—')}"
            "   <span alpha='55%'>double-click to edit</span>")
        for view in members:
            row = Gtk.ListBoxRow()
            row.view = view
            line = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            line.set_margin_top(5)
            line.set_margin_bottom(5)
            line.set_margin_start(10)
            line.set_margin_end(10)
            alias = Gtk.Label(label=view.alias, xalign=0.0)
            line.append(alias)
            bits = [view.hostname]
            if view.block.get("User"):
                bits.append("user " + view.block.get("User"))
            if view.block.get("Port"):
                bits.append("port " + view.block.get("Port"))
            sub = Gtk.Label(label="  ·  ".join(bits), xalign=0.0)
            sub.add_css_class("dim-label")
            sub.set_ellipsize(3)
            line.append(sub)
            row.set_child(line)
            self.host_list.append(row)

    def _on_host_click(self, _gesture, n_press, _x, y):
        row = self.host_list.get_row_at_y(int(y))
        if row is None:
            return
        self.host_list.select_row(row)
        if n_press == 2:
            self.edit_host()

    def _on_host_key(self, _controller, keyval, _code, _state):
        """Enter opens the selected connection, the way a file manager would."""
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if self.host_list.get_selected_row() is not None:
                self.edit_host()
                return True
        return False

    def _on_group_selected(self, _lb, row):
        if row is not None:
            self.selected_group = row.group
            self._refresh_hosts()

    def _selected_host(self):
        row = self.host_list.get_selected_row()
        return row.view if row is not None else None

    # -- category actions -------------------------------------------------

    def add_group(self):
        def create(name):
            try:
                self.config.add_group(name)
            except ConfigError as exc:
                error_dialog(self, "Could not add category", str(exc))
                return
            self.selected_group = name
            self.mark_dirty()

        prompt_text(self, "New category", "Category name", "", create)

    def rename_group(self):
        if not self.selected_group:
            return
        old = self.selected_group

        def rename(new):
            try:
                self.config.rename_group(old, new)
            except ConfigError as exc:
                error_dialog(self, "Could not rename", str(exc))
                return
            self.selected_group = new
            self.mark_dirty()

        prompt_text(self, "Rename category", f"New name for “{old}”", old, rename)

    def delete_group(self):
        if not self.selected_group:
            return
        name = self.selected_group
        members = self.config.groups().get(name, [])

        def do_delete(move_to=None):
            try:
                self.config.delete_group(name, move_to=move_to)
            except ConfigError as exc:
                error_dialog(self, "Could not delete", str(exc))
                return
            self.selected_group = None
            self.mark_dirty()

        if not members:
            confirm(self, f"Delete category “{name}”?",
                    "It has no connections in it.", "Delete",
                    lambda: do_delete(None))
            return

        others = [g for g in self.config.group_names() if g != name]
        options = [f"Move them to: {g}" for g in others] + \
                  [f"Delete all {len(members)} connections"]

        def chosen(choice):
            if choice.startswith("Move them to: "):
                do_delete(choice[len("Move them to: "):])
            else:
                confirm(self, f"Delete {len(members)} connections?",
                        f"“{name}” and everything in it will be removed from "
                        f"{display_path(self.path)}. A backup is written first.",
                        "Delete them", lambda: do_delete(None))

        prompt_choice(self, f"Delete “{name}”",
                      f"“{name}” holds {len(members)} connections. "
                      "What should happen to them?", options, chosen)

    # -- host actions ------------------------------------------------------

    def add_host(self):
        HostDialog(self, self.config, view=None, group=self.selected_group,
                   on_done=self.mark_dirty).present()

    def edit_host(self):
        view = self._selected_host()
        if view is None:
            return
        HostDialog(self, self.config, view=view,
                   on_done=self.mark_dirty).present()

    def duplicate_host(self):
        view = self._selected_host()
        if view is None:
            return
        try:
            self.config.duplicate_host(view)
        except ConfigError as exc:
            error_dialog(self, "Could not duplicate", str(exc))
            return
        self.mark_dirty()

    def move_host(self):
        view = self._selected_host()
        if view is None:
            return
        others = [g for g in self.config.group_names() if g != view.group]
        if not others:
            return

        def chosen(group):
            self.config.move_host(view, group)
            self.mark_dirty()

        prompt_choice(self, "Move connection",
                      f"Move “{view.alias}” to which category?", others, chosen)

    def delete_host(self):
        view = self._selected_host()
        if view is None:
            return

        def do_delete():
            self.config.delete_host(view)
            self.mark_dirty()

        confirm(self, f"Delete “{view.alias}”?",
                f"{view.hostname} will be removed from "
                f"{display_path(self.path)}. "
                "A backup is written before saving.", "Delete", do_delete)

    # -- persistence --------------------------------------------------------

    def save(self):
        """Returns True if the write succeeded, so callers can act on failure."""
        backup = save_config(self, self.config)
        if backup is None:
            return False
        self.dirty = False
        self.refresh()
        self._update_title()
        where = f" · backup: {os.path.basename(backup)}" if backup else ""
        self.status.set_text(f"Saved to {display_path(self.path)}{where}")
        if self.on_saved:
            self.on_saved()
        return True
