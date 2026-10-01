#!/usr/bin/env python3
"""Round-trip-safe reader/writer for ~/.ssh/config.

The hard requirement here is that editing one host must not disturb anything
else in the file. A 652-line config with generator banners, section comments
and hand-merged inventory is not something to reformat wholesale.

So the file is parsed into a list of blocks, each of which keeps its original
lines. A block is re-rendered from parsed fields *only* if it was edited;
everything else is emitted byte-for-byte as it came in.

No GTK in here on purpose -- this is the part that can destroy data, so it is
plain Python and testable without a display.
"""

import os
import re
import time
import shutil
import subprocess

# Ultimate SSH keeps its own copy of your connections and never edits ~/.ssh/config.
# ssh is pointed at this file with -F, so the copy is authoritative for the app
# while your system config stays exactly as it was.
APP_HOME = os.path.expanduser("~/.ultimate-ssh")
HOSTS_FILE = os.path.join(APP_HOME, "config")
BACKUP_DIR = os.path.join(APP_HOME, "backups")
SESSION_FILE = os.path.join(APP_HOME, "session.json")
SYSTEM_SSH_CONFIG = os.path.expanduser("~/.ssh/config")

TEMPLATE = """\
# Ultimate SSH connections.
# ssh is invoked with -F on this file, so it is self-contained.
#
# Group hosts with banner comments:  # --- Web tier ---

# --- Servers ---

# Host web-01
#     HostName 10.0.0.10
#     User deploy


# Defaults for every host.
# ssh uses the FIRST value it finds for each setting, so this block must stay
# at the END -- above the hosts it would override every one of them.
Host *
    ServerAliveInterval 60
    StrictHostKeyChecking accept-new
"""

SECTION_RE = re.compile(r"^#\s*-{2,}\s*(.+?)\s*-{2,}\s*$")
AWS_BANNER_RE = re.compile(r"Generated from AWS account\s+(\S+)\s+\((\S+)\)")
HOST_RE = re.compile(r"^Host\s+(.+?)\s*$", re.IGNORECASE)

INDENT = "    "
UNGROUPED = "Ungrouped"

# Fields the editor exposes as first-class form entries. Anything else a host
# carries is preserved verbatim in the "extra options" bucket.
KNOWN_KEYS = ["HostName", "User", "Port", "IdentityFile", "ProxyJump"]


class ConfigError(Exception):
    pass


def ensure_app_home():
    """Create ~/.ultimate-ssh and seed its config. Returns what happened.

    'imported'  -- copied from ~/.ssh/config on first run
    'created'   -- no system config existed, wrote a starter template
    'existing'  -- already set up, left alone
    """
    os.makedirs(APP_HOME, mode=0o700, exist_ok=True)
    os.makedirs(BACKUP_DIR, mode=0o700, exist_ok=True)
    if os.path.exists(HOSTS_FILE):
        return "existing"
    if os.path.exists(SYSTEM_SSH_CONFIG):
        shutil.copy2(SYSTEM_SSH_CONFIG, HOSTS_FILE)
        os.chmod(HOSTS_FILE, 0o600)
        return "imported"
    with open(HOSTS_FILE, "w") as fh:
        fh.write(TEMPLATE)
    os.chmod(HOSTS_FILE, 0o600)
    return "created"


def import_system_config():
    """Re-copy ~/.ssh/config over the app's copy, backing the copy up first.

    Returns the backup path. Raises if there is nothing to import.
    """
    if not os.path.exists(SYSTEM_SSH_CONFIG):
        raise ConfigError(f"{SYSTEM_SSH_CONFIG} does not exist")
    os.makedirs(BACKUP_DIR, mode=0o700, exist_ok=True)
    backup = None
    if os.path.exists(HOSTS_FILE):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = os.path.join(BACKUP_DIR, f"config.{stamp}.pre-import")
        shutil.copy2(HOSTS_FILE, backup)
    tmp = HOSTS_FILE + ".ultimate-ssh-tmp"
    shutil.copy2(SYSTEM_SSH_CONFIG, tmp)
    os.chmod(tmp, 0o600)
    os.replace(tmp, HOSTS_FILE)
    return backup


class ConflictError(ConfigError):
    """The file changed on disk since we loaded it."""


# --------------------------------------------------------------------------
# blocks
# --------------------------------------------------------------------------

class RawBlock:
    """Anything that is not a Host stanza: banners, comments, blank lines."""

    kind = "raw"

    def __init__(self, lines):
        self.lines = list(lines)

    def render(self):
        return list(self.lines)

    @property
    def is_blank(self):
        return all(not ln.strip() for ln in self.lines)

    @property
    def section_title(self):
        for ln in self.lines:
            m = SECTION_RE.match(ln.strip())
            if m and m.group(1).strip("-# "):
                return m.group(1).strip()
        return None


class HostBlock:
    """A `Host` line plus its indented option/comment lines."""

    kind = "host"

    def __init__(self, lines):
        self.lines = list(lines)
        self.dirty = False
        self.aliases = []
        self.options = []    # ordered [key, value]
        self.comments = []   # indented comment lines, preserved
        self._parse()

    def _parse(self):
        m = HOST_RE.match(self.lines[0].strip())
        self.aliases = m.group(1).split() if m else []
        for ln in self.lines[1:]:
            stripped = ln.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                self.comments.append(stripped)
                continue
            key, _, val = stripped.partition(" ")
            self.options.append([key.strip(), val.strip()])

    # -- accessors ----------------------------------------------------

    @property
    def alias(self):
        return self.aliases[0] if self.aliases else ""

    @property
    def is_pattern(self):
        """`Host *` is a defaults block, not a machine."""
        return any(c in a for a in self.aliases for c in "*?!")

    def get(self, key):
        for k, v in self.options:
            if k.lower() == key.lower():
                return v
        return ""

    def set(self, key, value):
        value = (value or "").strip()
        for pair in self.options:
            if pair[0].lower() == key.lower():
                if value:
                    pair[1] = value
                else:
                    self.options.remove(pair)
                self.dirty = True
                return
        if value:
            self.options.append([key, value])
            self.dirty = True

    def extra_options(self):
        """Option lines the form doesn't cover, as raw text."""
        known = {k.lower() for k in KNOWN_KEYS}
        return "\n".join(f"{k} {v}" for k, v in self.options
                         if k.lower() not in known)

    def set_extra_options(self, text):
        known = {k.lower() for k in KNOWN_KEYS}
        kept = [p for p in self.options if p[0].lower() in known]
        for ln in (text or "").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            key, _, val = ln.partition(" ")
            if key.strip():
                kept.append([key.strip(), val.strip()])
        self.options = kept
        self.dirty = True

    def rename(self, alias):
        alias = alias.strip()
        if not alias:
            raise ConfigError("alias cannot be empty")
        if len(alias.split()) != 1:
            raise ConfigError("alias cannot contain whitespace")
        if self.aliases[:1] != [alias]:
            self.aliases = [alias] + self.aliases[1:]
            self.dirty = True

    def render(self):
        if not self.dirty:
            return list(self.lines)
        out = ["Host " + " ".join(self.aliases)]
        out += [f"{INDENT}{k} {v}" for k, v in self.options]
        out += [f"{INDENT}{c}" for c in self.comments]
        return out


# --------------------------------------------------------------------------
# the file
# --------------------------------------------------------------------------

class HostView:
    """What the UI sees: a host plus the group it currently sits in."""

    def __init__(self, block, group):
        self.block = block
        self.group = group

    @property
    def alias(self):
        return self.block.alias

    @property
    def hostname(self):
        return self.block.get("HostName") or self.block.alias

    @property
    def note(self):
        return self.block.comments[0].lstrip("# ").strip() \
            if self.block.comments else ""

    @property
    def haystack(self):
        return f"{self.alias} {self.hostname} {self.group} {self.note}".lower()


def file_stamp(path):
    """(mtime_ns, size) -- the identity we use to spot outside edits.

    Not getmtime(): a float loses sub-millisecond resolution, so an edit made
    moments after loading can look identical. Nanoseconds plus size does not.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


class SshConfig:
    def __init__(self, path, blocks, default_group, stamp, trailing_newline=True):
        self.path = path
        self.blocks = blocks
        self.default_group = default_group
        self.stamp = stamp
        self.trailing_newline = trailing_newline

    # -- load / render / save ------------------------------------------

    @classmethod
    def load(cls, path):
        if not os.path.exists(path):
            return cls(path, [], UNGROUPED, None)

        with open(path, "r", errors="replace") as fh:
            text = fh.read()
        lines = text.split("\n")
        trailing_newline = text.endswith("\n")
        if trailing_newline and lines and lines[-1] == "":
            lines.pop()

        default_group = UNGROUPED
        if lines:
            m = AWS_BANNER_RE.search(lines[0])
            if m:
                default_group = f"AWS {m.group(2)}"

        blocks = []
        pending_raw = []
        i = 0
        while i < len(lines):
            line = lines[i]
            if HOST_RE.match(line.strip()) and not line.startswith((" ", "\t")):
                if pending_raw:
                    blocks.append(RawBlock(pending_raw))
                    pending_raw = []
                host_lines = [line]
                i += 1
                # A stanza owns only its INDENTED continuation lines. Stopping
                # at column 0 is what keeps a section banner from being
                # swallowed into the host above it -- and thus deleted with it.
                while i < len(lines) and lines[i].startswith((" ", "\t")) \
                        and lines[i].strip():
                    host_lines.append(lines[i])
                    i += 1
                blocks.append(HostBlock(host_lines))
                continue
            pending_raw.append(line)
            i += 1
        if pending_raw:
            blocks.append(RawBlock(pending_raw))

        return cls(path, blocks, default_group,
                   file_stamp(path), trailing_newline)

    def render(self):
        out = []
        for block in self.blocks:
            out.extend(block.render())
        text = "\n".join(out)
        if self.trailing_newline and not text.endswith("\n"):
            text += "\n"
        return text

    def save(self, backup_dir=None):
        """Atomic write with a backup. Returns the backup path (or None).

        Refuses to write if the file changed underneath us -- silently
        clobbering an edit made in $EDITOR would be unforgivable.
        """
        if self.stamp is not None and file_stamp(self.path) != self.stamp:
            raise ConflictError(
                f"{self.path} changed on disk since it was loaded. "
                "Reload before saving so your edits don't clobber it.")

        self.validate()

        backup = None
        if backup_dir and os.path.exists(self.path):
            os.makedirs(backup_dir, mode=0o700, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            backup = os.path.join(backup_dir, f"config.{stamp}")
            shutil.copy2(self.path, backup)

        mode = 0o600
        if os.path.exists(self.path):
            mode = os.stat(self.path).st_mode & 0o777

        tmp = self.path + ".ultimate-ssh-tmp"
        with open(tmp, "w") as fh:
            fh.write(self.render())
        os.chmod(tmp, mode)
        os.replace(tmp, self.path)      # atomic: no truncated config, ever
        self.stamp = file_stamp(self.path)
        return backup

    def validate(self):
        seen = set()
        for block in self.blocks:
            if block.kind != "host" or block.is_pattern:
                continue
            for alias in block.aliases:
                if alias in seen:
                    raise ConfigError(f"duplicate host alias: {alias}")
                seen.add(alias)

    # -- reading --------------------------------------------------------

    def hosts(self):
        views = []
        group = self.default_group
        for block in self.blocks:
            if block.kind == "raw":
                title = block.section_title
                if title:
                    group = title
            elif not block.is_pattern:
                views.append(HostView(block, group))
        return views

    def groups(self):
        """Ordered {group: [HostView]} -- includes declared-but-empty groups."""
        result = {self.default_group: []}
        group = self.default_group
        for block in self.blocks:
            if block.kind == "raw":
                title = block.section_title
                if title:
                    group = title
                    result.setdefault(group, [])
            elif not block.is_pattern:
                result.setdefault(group, []).append(HostView(block, group))
        if not result[self.default_group]:
            del result[self.default_group]
        return result

    def group_names(self):
        return list(self.groups().keys())

    def find(self, alias):
        for view in self.hosts():
            if view.alias == alias:
                return view
        return None

    # -- block placement -------------------------------------------------

    def _section_index(self, group):
        """Index of the RawBlock carrying `group`'s banner, or None."""
        for i, block in enumerate(self.blocks):
            if block.kind == "raw" and block.section_title == group:
                return i
        return None

    def _group_end_index(self, group):
        """Where a new host in `group` should be inserted."""
        start = self._section_index(group)
        if start is None:
            # the implicit leading group: ends at the first banner
            for i, block in enumerate(self.blocks):
                if block.kind == "raw" and block.section_title:
                    return i
            return len(self.blocks)
        for i in range(start + 1, len(self.blocks)):
            block = self.blocks[i]
            if block.kind == "raw" and block.section_title:
                return i
        return len(self.blocks)

    def _insert_host_block(self, block, group):
        idx = self._group_end_index(group)
        # step back over trailing blank lines so the host lands next to its
        # neighbours rather than after the gap before the next banner
        while idx > 0 and self.blocks[idx - 1].kind == "raw" \
                and self.blocks[idx - 1].is_blank:
            idx -= 1
        self.blocks.insert(idx, block)
        self.blocks.insert(idx + 1, RawBlock([""]))

    def _remove_host_block(self, block):
        idx = self.blocks.index(block)
        self.blocks.pop(idx)
        if idx < len(self.blocks):
            nxt = self.blocks[idx]
            if nxt.kind == "raw" and nxt.is_blank:
                nxt.lines.pop()
                if not nxt.lines:
                    self.blocks.pop(idx)

    # -- editing ---------------------------------------------------------

    def add_host(self, alias, options, group, comments=None):
        alias = alias.strip()
        if not alias:
            raise ConfigError("alias cannot be empty")
        if len(alias.split()) != 1:
            raise ConfigError("alias cannot contain whitespace")
        if any(c in alias for c in "*?!"):
            raise ConfigError("alias cannot contain wildcards")
        if self.find(alias) is not None:
            raise ConfigError(f"host {alias} already exists")
        if group not in self.groups() and group != self.default_group:
            self.add_group(group)

        block = HostBlock([f"Host {alias}"])
        block.dirty = True
        for key, value in options:
            if (value or "").strip():
                block.options.append([key, value.strip()])
        block.comments = list(comments or [])
        self._insert_host_block(block, group)
        return block

    def update_host(self, view, alias=None, options=None, extra=None, group=None):
        block = view.block
        if options:
            for key, value in options:
                block.set(key, value)
        if extra is not None:
            block.set_extra_options(extra)
        if alias and alias != block.alias:
            if self.find(alias) is not None:
                raise ConfigError(f"host {alias} already exists")
            block.rename(alias)
        if group is not None and group != view.group:
            self.move_host(view, group)
        return block

    def delete_host(self, view):
        self._remove_host_block(view.block)

    def duplicate_host(self, view):
        """Copy a stanza wholesale, including options the form doesn't model."""
        base = view.alias + "-copy"
        alias, n = base, 2
        while self.find(alias) is not None:
            alias, n = f"{base}{n}", n + 1
        block = HostBlock([f"Host {alias}"])
        block.dirty = True
        block.options = [[k, v] for k, v in view.block.options]
        block.comments = list(view.block.comments)
        self._insert_host_block(block, view.group)
        return alias

    def move_host(self, view, group):
        if group == view.group:
            return
        if group not in self.groups() and group != self.default_group:
            self.add_group(group)
        self._remove_host_block(view.block)
        self._insert_host_block(view.block, group)
        view.group = group

    # -- groups ----------------------------------------------------------

    def add_group(self, name):
        name = name.strip()
        if not name:
            raise ConfigError("category name cannot be empty")
        if name in self.groups():
            raise ConfigError(f"category “{name}” already exists")
        if self.blocks and not (self.blocks[-1].kind == "raw"
                                and self.blocks[-1].is_blank):
            self.blocks.append(RawBlock([""]))
        self.blocks.append(RawBlock([f"# --- {name} ---"]))
        self.blocks.append(RawBlock([""]))

    def rename_group(self, old, new):
        new = new.strip()
        if not new:
            raise ConfigError("category name cannot be empty")
        if new != old and new in self.groups():
            raise ConfigError(f"category “{new}” already exists")
        idx = self._section_index(old)
        if idx is None:
            raise ConfigError(f"category “{old}” has no banner to rename")
        block = self.blocks[idx]
        for i, ln in enumerate(block.lines):
            m = SECTION_RE.match(ln.strip())
            if m and m.group(1).strip() == old:
                block.lines[i] = f"# --- {new} ---"
                return
        raise ConfigError(f"could not find the banner for “{old}”")

    def delete_group(self, name, move_to=None):
        """Remove a category. Its hosts move to `move_to`, or are deleted."""
        members = list(self.groups().get(name, []))
        if members:
            if move_to is None:
                for view in members:
                    self.delete_host(view)
            else:
                if move_to == name:
                    raise ConfigError("cannot move hosts into the category "
                                      "being deleted")
                for view in members:
                    self.move_host(view, move_to)

        idx = self._section_index(name)
        if idx is None:
            raise ConfigError(f"category “{name}” has no banner to remove")
        block = self.blocks[idx]
        block.lines = [ln for ln in block.lines
                       if not (SECTION_RE.match(ln.strip())
                               and SECTION_RE.match(ln.strip()).group(1).strip()
                               == name)]
        if not block.lines:
            self.blocks.pop(idx)

    # -- misc -------------------------------------------------------------

    def shadowed_keys(self):
        """Keys a wildcard block silently overrides for the hosts below it.

        ssh uses the FIRST value it finds for each parameter, so a `Host *`
        block placed before the host stanzas wins over every one of them. A
        per-host User edited under such a block has no effect whatsoever.
        """
        keys = set()
        for i, block in enumerate(self.blocks):
            if block.kind != "host" or not block.is_pattern:
                continue
            later_specific = any(b.kind == "host" and not b.is_pattern
                                 for b in self.blocks[i + 1:])
            if later_specific:
                keys.update(k for k, _ in block.options)
        return keys

    def move_defaults_to_end(self):
        """Relocate wildcard blocks below the host stanzas. Returns how many.

        This is the layout ssh_config(5) asks for: specific declarations near
        the beginning, general defaults at the end. Blocks are moved, not
        rewritten, so their contents stay byte-for-byte identical.
        """
        wildcards = [b for b in self.blocks
                     if b.kind == "host" and b.is_pattern]
        if not wildcards:
            return 0
        if not self.shadowed_keys():
            return 0        # already harmless; leave the file alone

        for block in wildcards:
            self._remove_host_block(block)

        if self.blocks and not (self.blocks[-1].kind == "raw"
                                and self.blocks[-1].is_blank):
            self.blocks.append(RawBlock([""]))
        # deliberately not a "# --- x ---" banner: that would read as a category
        self.blocks.append(RawBlock([
            "# Defaults for every host.",
            "# ssh uses the FIRST value it finds for each setting, so these",
            "# must stay at the end or they override every host above them.",
        ]))
        for block in wildcards:
            self.blocks.append(block)
            self.blocks.append(RawBlock([""]))
        return len(wildcards)

    def is_generated_group(self, group):
        """AWS-derived hosts get rewritten by the generator; warn before edit."""
        return group == self.default_group and group.startswith("AWS ")


# -- identity files ------------------------------------------------------
#
# ssh reports every one of these the same way -- "Permission denied
# (publickey)" -- sometimes preceded by a terse "invalid format" that reads
# like a corrupt file rather than the wrong key format entirely. That sends
# people hunting for an authorization problem they do not have, so the file
# is worth looking at before ssh ever sees it.

NETWORK_FS = {"nfs", "nfs4", "cifs", "smb3", "smbfs", "fuse.sshfs", "9p",
              "vboxsf", "virtiofs", "afs", "ncpfs", "fuse.vmhgfs-fuse"}

PUBLIC_KEY_PREFIXES = ("ssh-rsa ", "ssh-ed25519 ", "ssh-dss ", "ecdsa-sha2-",
                       "sk-ssh-ed25519", "sk-ecdsa-sha2-")


def filesystem_type(path):
    """fstype of the mount `path` sits on, or "" if it cannot be told."""
    try:
        real = os.path.realpath(path)
        best, fstype = "", ""
        with open("/proc/self/mounts") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                target = parts[1].replace("\\040", " ")
                if real == target or real.startswith(target.rstrip("/") + "/"):
                    if len(target) >= len(best):
                        best, fstype = target, parts[2]
        return fstype
    except OSError:
        return ""


class KeyReport:
    """What is wrong with one IdentityFile, and what can be done about it."""

    def __init__(self, path, issues, fstype=""):
        self.path = path
        self.issues = issues        # [(code, message)], worst first
        self.fstype = fstype

    @property
    def ok(self):
        return not self.issues

    @property
    def codes(self):
        return {code for code, _ in self.issues}

    @property
    def can_convert(self):
        return "putty" in self.codes and bool(shutil.which("puttygen"))

    @property
    def can_chmod(self):
        return "perms" in self.codes

    def summary(self):
        return "; ".join(message for _, message in self.issues)


def inspect_identity_file(value):
    """Check an IdentityFile the way ssh will read it.

    None means there is nothing worth saying: a blank field (ssh falls back to
    its own defaults) or a value containing a % token only ssh can expand.
    """
    value = (value or "").strip().strip('"')
    if not value or "%" in value:
        return None

    path = os.path.expanduser(value)
    if not os.path.exists(path):
        return KeyReport(path, [("missing", "no file at that path")])
    if not os.path.isfile(path):
        return KeyReport(path, [("missing", "not a regular file")])

    try:
        with open(path, "rb") as fh:
            head = fh.read(120)
    except OSError as exc:
        return KeyReport(path, [("unreadable",
                                 f"cannot be read ({exc.strerror})")])

    issues = []
    text = head.decode("utf-8", "replace")
    if text.startswith("PuTTY-User-Key-File"):
        issues.append(("putty",
                       "is a PuTTY .ppk key, which OpenSSH cannot read"))
    elif text.startswith(PUBLIC_KEY_PREFIXES):
        issues.append(("public",
                       "is a public key -- ssh needs the private half"))
    elif "PRIVATE KEY" not in text:
        issues.append(("not_a_key", "does not look like a private key"))

    mode = os.stat(path).st_mode & 0o777
    # a public key is meant to be world-readable; its mode is not the problem
    if mode & 0o077 and "public" not in {c for c, _ in issues}:
        issues.append(("perms",
                       f"is readable by others (mode {mode:04o}); "
                       "ssh refuses keys like this"))

    fstype = filesystem_type(path)
    if fstype in NETWORK_FS:
        issues.append(("network",
                       f"is on a {fstype} filesystem, so connecting depends "
                       "on that mount being up"))
    return KeyReport(path, issues, fstype)


def fix_identity_permissions(path):
    """chmod 0600. ssh refuses a key others can read, and it is right to."""
    os.chmod(os.path.expanduser(path), 0o600)


def putty_key_is_encrypted(path):
    try:
        with open(os.path.expanduser(path), "r", errors="replace") as fh:
            for line in fh.read(400).splitlines():
                if line.startswith("Encryption:"):
                    return line.split(":", 1)[1].strip() != "none"
    except OSError:
        pass
    return False


def convert_putty_key(path, dest_dir=None, passphrase=None):
    """Convert a .ppk to an OpenSSH key and return the new path.

    puttygen cannot convert in place, and there is no point pretending the
    result is the same file -- it is a different format. It is written beside
    your other keys rather than into the app's own directory so that plain
    ssh, scp and ssh-agent find it too.
    """
    path = os.path.expanduser(path)
    if not shutil.which("puttygen"):
        raise ConfigError(
            "puttygen is not installed. It ships in the 'putty' package on "
            "most distributions (dnf install putty).")
    if putty_key_is_encrypted(path) and not passphrase:
        raise ConfigError("this .ppk is passphrase-protected; "
                          "the passphrase is needed to convert it")

    dest_dir = dest_dir or os.path.expanduser("~/.ssh")
    os.makedirs(dest_dir, mode=0o700, exist_ok=True)
    stem = os.path.splitext(os.path.basename(path))[0] or "converted-key"
    dest = os.path.join(dest_dir, stem)
    n = 2
    while os.path.exists(dest):        # never clobber an existing key
        dest = os.path.join(dest_dir, f"{stem}-{n}")
        n += 1

    argv = ["puttygen", path, "-O", "private-openssh-new", "-o", dest]
    pass_file = None
    try:
        if passphrase:
            # a temp file, not argv: command lines are world-readable in /proc
            import tempfile
            fd, pass_file = tempfile.mkstemp()
            with os.fdopen(fd, "w") as fh:
                fh.write(passphrase)
            os.chmod(pass_file, 0o600)
            argv += ["--old-passphrase", pass_file]

        old = os.umask(0o077)          # the key must not exist world-readable
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=30)
        finally:
            os.umask(old)
    except OSError as exc:
        raise ConfigError(f"could not run puttygen: {exc}")
    except subprocess.TimeoutExpired:
        raise ConfigError("puttygen did not finish; it may be waiting for a "
                          "passphrase")
    finally:
        if pass_file:
            os.unlink(pass_file)

    if proc.returncode != 0 or not os.path.exists(dest):
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise ConfigError(detail[-1] if detail else
                          "puttygen failed for an unstated reason")
    os.chmod(dest, 0o600)
    return dest
