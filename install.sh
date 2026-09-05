#!/usr/bin/env bash
# Fox Switcher installer. Run ./install.sh, or ./install.sh --uninstall
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HOME/.local/bin/fox-switcher"
UNIT="$HOME/.config/systemd/user/fox-switcher.service"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m   %s\n' "$*"; }
warn() { printf '  \033[33mwarn\033[0m %s\n' "$*"; }
die()  { printf '  \033[31mfail\033[0m %s\n\n' "$*" >&2; exit 1; }

in_group_file() { getent group input | grep -qw "$USER"; }
in_group_now()  { id -nG | grep -qw input; }

if [ "${1:-}" = "--uninstall" ]; then
  say "Removing Fox Switcher"
  systemctl --user disable --now fox-switcher.service 2>/dev/null || true
  rm -f "$BIN" "$UNIT"
  systemctl --user daemon-reload 2>/dev/null || true
  ok "service stopped, files removed"
  echo
  echo "Your 'input' group membership was left alone. To drop it too:"
  echo "  sudo gpasswd -d $USER input"
  echo
  exit 0
fi

say "Checking your system"

[ -f "$SRC/fox_switcher.py" ] || die "run this from the fox-switcher checkout"

if [ "${XDG_SESSION_TYPE:-}" != "wayland" ]; then
  warn "session is '${XDG_SESSION_TYPE:-unknown}', not wayland"
fi
if command -v hyprctl >/dev/null && hyprctl version >/dev/null 2>&1; then
  ok "Hyprland $(hyprctl version 2>/dev/null | head -1 | awk '{print $2}')"
else
  warn "Hyprland not detected; layout switching will not work"
fi

missing=()
python3 -c 'import evdev'            2>/dev/null || missing+=(python-evdev)
python3 -c 'from xkbcommon import xkb' 2>/dev/null || missing+=(python-xkbcommon)
command -v wl-paste >/dev/null        2>&1        || missing+=(wl-clipboard)

if [ ${#missing[@]} -gt 0 ]; then
  if command -v pacman >/dev/null; then
    say "Installing missing packages: ${missing[*]}"
    sudo pacman -S --needed "${missing[@]}"
  else
    die "missing dependencies: ${missing[*]} (install them, then re-run)"
  fi
else
  ok "python-evdev, python-xkbcommon, wl-clipboard"
fi

if [ -w /dev/uinput ]; then
  ok "/dev/uinput is writable"
else
  warn "/dev/uinput is not writable; the daemon cannot type without it"
fi

relogin=0
if in_group_now; then
  ok "you are in the 'input' group"
elif in_group_file; then
  warn "'input' group is set but not active in this session"
  relogin=1
else
  say "Joining the 'input' group (needed to read the keyboard)"
  sudo usermod -aG input "$USER"
  relogin=1
fi

say "Installing"
install -Dm755 "$SRC/fox_switcher.py" "$BIN";        ok "$BIN"
install -Dm644 "$SRC/fox-switcher.service" "$UNIT";  ok "$UNIT"
systemctl --user daemon-reload

python3 "$BIN" --selftest >/dev/null && ok "self-test passed"

if [ "$relogin" -eq 1 ]; then
  systemctl --user enable fox-switcher.service >/dev/null 2>&1
  say "ONE MORE STEP - you must start a new login session"
  cat <<EOF
  Linux applies group membership only at login. Your current session started
  before you joined the 'input' group, so nothing in it can read the keyboard
  yet. Fox Switcher is installed and enabled, but it will not work until you
  start a fresh session.

  Pick either one:

    1. Log out and back in
         hyprctl dispatch exit
       then log in again at your display manager.

    2. Or just reboot - simplest, always works
         systemctl reboot

  Afterwards it starts by itself. Confirm with:

    id -nG | grep -qw input && echo "input group: ok"
    systemctl --user status fox-switcher

EOF
else
  systemctl --user enable --now fox-switcher.service >/dev/null 2>&1
  sleep 1
  if systemctl --user is-active --quiet fox-switcher.service; then
    ok "service running"
    say "Done - double-tap Shift to convert what you just typed"
  else
    warn "service failed to start; see: systemctl --user status fox-switcher"
  fi
  echo
fi
