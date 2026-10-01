#!/usr/bin/env bash
#
# Ultimate SSH installer.
#
# Installs into your home directory by default -- no root needed for the app
# itself. sudo is used only if system packages are actually missing.
#
#   ./install.sh              install (and install dependencies if needed)
#   ./install.sh --no-deps    install, never touch system packages
#   ./install.sh --uninstall  remove everything this script installed
#   ./install.sh --prefix DIR install somewhere else (default ~/.local)
#
set -euo pipefail

APP_NAME="Ultimate SSH"
APP_ID="ultimate-ssh"
PREFIX="${HOME}/.local"
DO_DEPS=1
DO_UNINSTALL=0
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

APP_FILES=(ultimate_ssh.py sshconfig.py editor.py chatbridge.py)
EXTRA_FILES=(README.md LICENSE)

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m warn\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --no-deps)   DO_DEPS=0 ;;
        --uninstall) DO_UNINSTALL=1 ;;
        --prefix)    shift; PREFIX="${1:?--prefix needs a directory}" ;;
        -h|--help)   sed -n '3,12p' "$0"; exit 0 ;;
        *)           die "unknown option: $1  (try --help)" ;;
    esac
    shift
done

LIBDIR="${PREFIX}/share/${APP_ID}"
BINDIR="${PREFIX}/bin"
DESKTOP_DIR="${PREFIX}/share/applications"
DESKTOP_FILE="${DESKTOP_DIR}/${APP_ID}.desktop"
LAUNCHER="${BINDIR}/${APP_ID}"

# ---------------------------------------------------------------- uninstall
if [ "$DO_UNINSTALL" -eq 1 ]; then
    say "Removing ${APP_NAME}"
    rm -rf  "$LIBDIR"
    rm -f   "$LAUNCHER" "$DESKTOP_FILE"
    command -v update-desktop-database >/dev/null 2>&1 &&
        update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
    say "Removed the application."
    echo
    echo "Your connections were left alone, in ~/.${APP_ID}/"
    echo "Delete them yourself with:  rm -rf ~/.${APP_ID}"
    exit 0
fi

# -------------------------------------------------------------- dependencies
have_deps() {
    python3 - <<'PY' >/dev/null 2>&1
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Vte", "3.91")
from gi.repository import Gtk, Vte
PY
}

install_deps() {
    local pm=""
    for candidate in dnf apt-get pacman zypper apk; do
        if command -v "$candidate" >/dev/null 2>&1; then pm="$candidate"; break; fi
    done
    [ -n "$pm" ] || die "no supported package manager found; install GTK4, VTE 3.91 and PyGObject by hand, then re-run with --no-deps"

    local sudo=""
    [ "$(id -u)" -ne 0 ] && sudo="sudo"

    say "Installing dependencies with ${pm} (this needs your password)"
    case "$pm" in
        dnf)     $sudo dnf install -y python3-gobject gtk4 vte291-gtk4 ;;
        apt-get) $sudo apt-get update &&
                 $sudo apt-get install -y python3-gi gir1.2-gtk-4.0 gir1.2-vte-3.91 ;;
        pacman)  $sudo pacman -S --needed --noconfirm python-gobject gtk4 vte4 ;;
        zypper)  $sudo zypper install -y python3-gobject typelib-1_0-Gtk-4_0 typelib-1_0-Vte-3_91 ;;
        apk)     $sudo apk add py3-gobject3 gtk4.0 vte3 ;;
    esac
}

command -v python3 >/dev/null 2>&1 || die "python3 is required"
command -v ssh     >/dev/null 2>&1 || die "the openssh client is required"

if have_deps; then
    say "GTK4, VTE 3.91 and PyGObject are already present"
elif [ "$DO_DEPS" -eq 1 ]; then
    install_deps
    have_deps || die "dependencies still missing after install -- check the output above"
else
    die "missing GTK4 / VTE 3.91 / PyGObject and --no-deps was given"
fi

# ------------------------------------------------------------------ install
say "Installing ${APP_NAME} into ${LIBDIR}"
mkdir -p "$LIBDIR" "$BINDIR" "$DESKTOP_DIR"

for f in "${APP_FILES[@]}"; do
    [ -f "${SOURCE_DIR}/${f}" ] || die "missing source file: ${f}"
    install -m 0644 "${SOURCE_DIR}/${f}" "${LIBDIR}/${f}"
done
for f in "${EXTRA_FILES[@]}"; do
    [ -f "${SOURCE_DIR}/${f}" ] && install -m 0644 "${SOURCE_DIR}/${f}" "${LIBDIR}/${f}"
done
chmod 0755 "${LIBDIR}/ultimate_ssh.py"

cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
exec python3 "${LIBDIR}/ultimate_ssh.py" "\$@"
EOF
chmod 0755 "$LAUNCHER"

cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=${APP_NAME}
GenericName=SSH session manager
Comment=Tabbed SSH terminal with a session tree, broadcast and a remote file browser
Exec=${LAUNCHER}
Icon=utilities-terminal
Terminal=false
Categories=Network;RemoteAccess;System;TerminalEmulator;
Keywords=ssh;terminal;remote;sftp;server;
StartupNotify=true
EOF

command -v update-desktop-database >/dev/null 2>&1 &&
    update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true

# ------------------------------------------------------------------- report
say "Installed."
echo
echo "  launch:       ${APP_ID}"
echo "  app files:    ${LIBDIR}"
echo "  connections:  ~/.${APP_ID}/config"
echo
echo "On first run your ~/.ssh/config is COPIED into ~/.${APP_ID}/config."
echo "Ultimate SSH reads and writes only that copy -- it never edits ~/.ssh/config."
echo

case ":${PATH}:" in
    *":${BINDIR}:"*) ;;
    *) warn "${BINDIR} is not on your PATH."
       warn "Add this to ~/.bashrc:   export PATH=\"\$HOME/.local/bin:\$PATH\""
       warn "Until then, launch it with: ${LAUNCHER}" ;;
esac
