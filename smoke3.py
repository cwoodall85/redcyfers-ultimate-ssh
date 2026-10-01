#!/usr/bin/env python3
"""GUI smoke test for the editing layer.

Runs entirely against a COPY of tests/fixture-config.txt, a synthetic config
with invented hosts. The fixture is checked byte-for-byte at the end to prove
it was never written to, and ~/.ssh/config is never opened at all.
"""
import os
import sys
import shutil
import tempfile

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Vte", "3.91")
from gi.repository import Gtk, GLib, Gdk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ultimate_ssh as app
import editor
import sshconfig

_HOME = tempfile.mkdtemp(prefix="ultimate-ssh-test-")
sshconfig.APP_HOME = _HOME
sshconfig.SESSION_FILE = os.path.join(_HOME, "session.json")
app.SESSION_FILE = sshconfig.SESSION_FILE

SCRATCH = os.environ.get("SMOKE_OUT", tempfile.gettempdir())
SHOT = f"{SCRATCH}/editing.png"
FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "tests", "fixture-config.txt")

FAILURES = []
ORIGINAL = open(FIXTURE, errors="replace").read()


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"   {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def menu_labels(popover):
    labels, child = [], popover.get_child().get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Button):
            labels.append(child.get_label())
        child = child.get_next_sibling()
    return labels


class Smoke(Gtk.Application):
    def __init__(self, path):
        super().__init__(application_id="dev.ultimatessh.Smoke3")
        self.path = path

    def do_activate(self):
        self.win = win = app.UltimateSshWindow(self)
        win.present()
        GLib.timeout_add(600, self.run_checks)

    def run_checks(self):
        win = self.win
        sidebar = win.sidebar

        print("\n== sidebar reads through the new config model ==")
        check("groups loaded", len(sidebar.groups) == 6, str(len(sidebar.groups)))
        total = sum(len(v) for v in sidebar.groups.values())
        check("hosts loaded", total == 16, str(total))
        check("config object attached",
              isinstance(sidebar.config, sshconfig.SshConfig))

        print("\n== right-click menus ==")
        host_row = None
        group_row = None
        row = sidebar.listbox.get_first_child()
        while row is not None and (host_row is None or group_row is None):
            if getattr(row, "host", None) is not None and host_row is None:
                host_row = row
            elif getattr(row, "host", None) is None and group_row is None:
                group_row = row
            row = row.get_next_sibling()

        sidebar.listbox.get_row_at_y = lambda _y: group_row
        sidebar._on_right_click(None, 1, 10, 10)
        labels = menu_labels(sidebar._popover)
        check("group menu appears", sidebar._popover.get_visible())
        check("group menu has rename", any("Rename category" in l for l in labels))
        check("group menu has delete", any("Delete category" in l for l in labels))
        check("group menu has new connection",
              any("New connection here" in l for l in labels))
        check("group menu offers open-all",
              any(l.startswith("Open all") for l in labels), str(labels[:1]))
        sidebar._popover.popdown()

        sidebar.listbox.get_row_at_y = lambda _y: host_row
        sidebar._on_right_click(None, 1, 10, 10)
        labels = menu_labels(sidebar._popover)
        check("host menu appears", sidebar._popover.get_visible())
        for want in ("Connect", "Edit connection…", "Duplicate",
                     "Move to category…", "Delete…"):
            check(f"host menu has {want!r}", want in labels)
        check("host menu has copy-command",
              any(l.startswith("Copy") for l in labels))
        sidebar._popover.popdown()

        print("\n== edit a host through the dialog ==")
        view = sidebar.config.find("lab-web2")
        dlg = editor.HostDialog(self.win, sidebar.config, view=view,
                                on_done=self.win._commit)
        check("dialog prefilled from config",
              dlg.entries["HostName"].get_text() == "10.10.1.11",
              dlg.entries["HostName"].get_text())
        dlg.entries["Port"].set_text("2222")
        dlg.entries["User"].set_text("deploy")
        dlg._save()

        saved = open(self.path).read()
        check("edit reached the file", "Port 2222" in saved)
        check("sidebar reloaded with the edit",
              sidebar.config.find("lab-web2").block.get("Port") == "2222")
        check("backup directory created", os.path.isdir(editor.BACKUP_DIR))
        backups = os.listdir(editor.BACKUP_DIR)
        check("a backup was written", len(backups) >= 1, str(backups[-1:]))
        check("backup holds the pre-edit content",
              open(os.path.join(editor.BACKUP_DIR, sorted(backups)[-1])).read()
              == ORIGINAL)
        check("rest of the file intact", saved.count("Host ") ==
              ORIGINAL.count("Host "), f"{saved.count('Host ')}")

        print("\n== Enter saves, closing asks ==")
        view2 = sidebar.config.find("lab-dev01")
        d2 = editor.HostDialog(self.win, sidebar.config, view=view2,
                               on_done=lambda: self.win._commit(sidebar.config))
        check("dialog has a default button", d2.get_default_widget() is not None)
        check("fields activate it", d2.entries["User"].get_activates_default())
        check("an untouched dialog closes freely",
              d2._on_close_request(d2) is False)
        d2.entries["User"].set_text("enter-user")
        check("a dialog with edits refuses to close silently",
              d2._on_close_request(d2) is True)
        check("nothing written by that attempted close",
              "enter-user" not in open(self.path).read())
        # Enter == activating the default button
        d2.get_default_widget().emit("clicked")
        check("Enter saves the edit",
              "enter-user" in open(self.path).read())
        check("the username actually changed",
              sidebar.config.find("lab-dev01").block.get("User") == "enter-user",
              sidebar.config.find("lab-dev01").block.get("User"))

        print("\n== add a host through the dialog ==")
        dlg = editor.HostDialog(self.win, sidebar.config, view=None,
                                group="Services", on_done=self.win._commit)
        dlg.entries["Alias"].set_text("lab-svc9")
        dlg.entries["HostName"].set_text("10.10.3.99")
        dlg.extra.get_buffer().set_text("Compression yes")
        dlg._save()

        check("new host in the file", "Host lab-svc9" in open(self.path).read())
        added = sidebar.config.find("lab-svc9")
        check("landed in the chosen category",
              added is not None and added.group == "Services",
              added.group if added else "missing")
        check("extra options preserved",
              added is not None and added.block.get("Compression") == "yes")

        print("\n== duplicate / rename / delete ==")
        alias = sidebar.config.duplicate_host(sidebar.config.find("lab-svc9"))
        self.win._commit()
        check("duplicate created", alias == "lab-svc9-copy"
              and sidebar.config.find(alias) is not None, alias)

        sidebar.config.rename_group("Services", "Service tier")
        self.win._commit()
        check("category renamed on disk",
              "# --- Service tier ---" in open(self.path).read())
        # 3 from the fixture + lab-svc9 + its duplicate
        check("members followed", len(sidebar.groups.get("Service tier", [])) == 5,
              str(len(sidebar.groups.get("Service tier", []))))

        sidebar.config.delete_host(sidebar.config.find("lab-svc9-copy"))
        self.win._commit()
        check("delete reached the file",
              "lab-svc9-copy" not in open(self.path).read())

        print("\n== manager window ==")
        refreshed = {"count": 0}
        mgr = editor.ManagerWindow(
            self.win, self.path,
            on_saved=lambda: (refreshed.update(count=refreshed["count"] + 1),
                              self.win._on_manager_saved())[0])
        mgr.present()
        check("manager lists categories",
              self.count_rows(mgr.group_list) == 6,
              str(self.count_rows(mgr.group_list)))
        check("save disabled until dirty", not mgr.save_btn.get_sensitive())
        mgr.selected_group = "Dev boxes"
        mgr.refresh()
        check("host pane follows the category",
              self.count_rows(mgr.host_list) == 3,
              str(self.count_rows(mgr.host_list)))
        mgr.config.add_group("Kubernetes")
        mgr.mark_dirty()
        check("save enabled once dirty", mgr.save_btn.get_sensitive())
        check("nothing written before Save",
              "Kubernetes" not in open(self.path).read())
        mgr.save()
        check("Save wrote the category", "# --- Kubernetes ---" in open(self.path).read())
        check("dirty cleared after save", not mgr.dirty)
        check("save reports success", mgr.save() is not False)
        check("saving notifies the app", refreshed["count"] >= 1,
              str(refreshed["count"]))
        check("the sidebar picked the change up without a restart",
              "Kubernetes" in sidebar.groups, str(list(sidebar.groups))[:60])

        print("\n== click behaviour in the manager ==")
        mgr.selected_group = "Dev boxes"
        mgr.refresh()
        first = mgr.host_list.get_first_child()
        mgr.host_list.get_row_at_y = lambda _y: first

        opened = {"n": 0}
        real_edit = mgr.edit_host
        mgr.edit_host = lambda: opened.update(n=opened["n"] + 1)

        mgr._on_host_click(None, 1, 0, 10)
        check("single click selects the row",
              mgr.host_list.get_selected_row() is first)
        check("single click does NOT open the editor", opened["n"] == 0,
              str(opened["n"]))

        mgr._on_host_click(None, 2, 0, 10)
        check("double click opens the editor", opened["n"] == 1, str(opened["n"]))

        check("Enter opens the selected connection",
              mgr._on_host_key(None, Gdk.KEY_Return, 0, 0) is True
              and opened["n"] == 2)
        check("other keys are left alone",
              mgr._on_host_key(None, Gdk.KEY_a, 0, 0) is False)

        check("the selection is what the buttons act on",
              mgr._selected_host() is not None
              and mgr._selected_host().alias == first.view.alias)
        mgr.edit_host = real_edit

        print("\n== unsaved work is not thrown away ==")
        # the invariant is that it names the file being edited -- never the
        # system config, which this app no longer touches
        check("button names the file it actually writes",
              editor.display_path(self.path) in mgr.save_btn.get_label()
              and "~/.ssh/config" not in mgr.save_btn.get_label(),
              mgr.save_btn.get_label())
        check("a clean window closes freely",
              mgr._on_close_request(mgr) is False)
        mgr.config.add_group("Unsaved-Category")
        mgr.mark_dirty()
        check("a dirty window refuses to close silently",
              mgr._on_close_request(mgr) is True)
        check("and says so in its title",
              "unsaved" in mgr.get_title().lower(), mgr.get_title())
        check("nothing was written by the attempted close",
              "Unsaved-Category" not in open(self.path).read())
        mgr.reload()
        check("discarding restores the saved state", not mgr.dirty)
        check("title clears too", "unsaved" not in mgr.get_title().lower())

        mgr.selected_group = "Dev boxes"
        mgr.refresh()
        mgr.host_list.select_row(mgr.host_list.get_first_child())
        mgr.duplicate_host()
        check("manager duplicate marks dirty", mgr.dirty)
        mgr.save()
        check("manager duplicate persisted",
              "lab-dev00-copy" in open(self.path).read())

        GLib.spawn_command_line_sync(f"spectacle -a -b -n -o {SHOT}")
        mgr.close()

        print("\n== the fixture was never touched ==")
        check("fixture byte-identical",
              open(FIXTURE, errors="replace").read() == ORIGINAL)

        print("\n" + ("ALL CHECKS PASSED" if not FAILURES
                      else f"FAILURES: {FAILURES}"))
        self.quit()
        return False

    @staticmethod
    def count_rows(listbox):
        n, child = 0, listbox.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.ListBoxRow):
                n += 1
            child = child.get_next_sibling()
        return n


if __name__ == "__main__":
    tmp = tempfile.mktemp(suffix=".sshconfig")
    shutil.copy2(FIXTURE, tmp)
    os.chmod(tmp, 0o600)
    app.SSH_CONFIG = tmp
    editor.BACKUP_DIR = tempfile.mkdtemp()
    try:
        rc = Smoke(tmp).run([])
    finally:
        os.path.exists(tmp) and os.unlink(tmp)
    sys.exit(1 if FAILURES else 0)
