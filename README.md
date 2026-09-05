# Fox Switcher

Double-tap **Shift** and the text you just typed is retyped in the next keyboard
layout, in place. A [Caramba Switcher](https://caramba-switcher.com/) / Punto
Switcher equivalent for **Hyprland on Wayland**, where no global key-capture API
exists and `xneur` (X11-only) can't run.

```
you type:   ghbdtn rfr ltkf
double-tap Shift
you get:    привет как дела
```

Two mechanics:

- **Typed run** — nothing selected: what you typed since the last boundary is
  erased and retyped.
- **Selection** — text selected: that selection is retyped.

## Why it works with any layout

It never translates characters. It remembers the *keycodes* you pressed, advances
the layout, and replays the same keycodes — the compositor does the mapping. So
`us,ka,ru` cycles en → ka → ru → en with no layout-specific code, and punctuation
that sits in different places across layouts comes out right:

| key | en | Shift+en | ru | Shift+ru |
|-----|----|----|----|----|
| `/` | `/` | `?` | `.` | `,` |
| `7` | `7` | `&` | `7` | `?` |
| `,` | `,` | `<` | `б` | `Б` |

A hand-written ЙЦУКЕН↔QWERTY table gets all three wrong.

Replay is injected through `/dev/uinput` as real kernel-level key events, so it
behaves identically in Ghostty, Claude Code and other TUIs, XWayland apps,
browsers and games.

## Install

```bash
git clone https://github.com/EugeneTuaev/fox-switcher
cd fox-switcher
./install.sh
```

The installer checks your system, installs any missing packages, adds you to the
`input` group if needed, installs the binary and the user service, runs the
self-test, and starts it. To remove everything again:

```bash
./install.sh --uninstall
```

## Log out and back in

**If the installer added you to the `input` group, you must start a new login
session before Fox Switcher can work.** Linux applies group membership only at
login, so your current session cannot read the keyboard no matter what is
installed. Either option works:

- **Log out:** `hyprctl dispatch exit`, then log back in at your display manager
- **Or just reboot:** `systemctl reboot` — simplest, and always works

It starts by itself afterwards. Confirm with:

```bash
id -nG | grep -qw input && echo "input group: ok"
python3 -c "import evdev; print(len(evdev.list_devices()), 'devices readable')"
systemctl --user status fox-switcher
```

The device count must be greater than 0. If it prints `0`, the session still
does not have the group — reboot.

The installer already tells you this at the end, so you only need this section
if you skipped it.

## Requirements

Handled by the installer, listed here for reference:

- Hyprland (uses `hyprctl switchxkblayout`)
- `python-evdev`, `python-xkbcommon`, `wl-clipboard`
- Membership of the `input` group
- `/dev/uinput` writable — on most systems logind already grants this via ACL

Manual install, if you prefer:

```bash
sudo pacman -S python-evdev python-xkbcommon wl-clipboard
sudo usermod -aG input "$USER"
install -Dm755 fox_switcher.py ~/.local/bin/fox-switcher
install -Dm644 fox-switcher.service ~/.config/systemd/user/fox-switcher.service
systemctl --user daemon-reload
systemctl --user enable --now fox-switcher
```

## Tuning

Constants at the top of `fox_switcher.py`: double-tap window, inter-key delay,
idle timeout, buffer cap, and the window classes treated as terminals.

## What resets the remembered run

Enter, Tab, Escape, arrows/Home/End/PgUp/PgDn, any Ctrl/Alt/Super shortcut, a
mouse click, and 60 seconds of idleness. Backspace drops one keystroke rather
than the whole run.

## Privacy

The daemon reads your keyboard, so be deliberate about it. Wayland offers no way
to detect a password field, and none is attempted. The buffer is held in memory
only, capped at 200 keystrokes, cleared by every boundary above, and never
written to disk or logged. Stop the service when that isn't good enough.

## Tests

```bash
python3 fox_switcher.py --selftest
```

Asserts only, no framework. Drives the pure logic through a fake environment and
round-trips the keymap oracle against your real installed layouts.

## Known limits

- A terminal selection is a viewport overlay, not editable text, so selection
  mode can't replace it. With a terminal focused it copies the converted text to
  the clipboard and notifies instead.
- Hotplug is picked up by a 1-second device rescan.
- Focus changes are caught via the click or shortcut that caused them, not by
  subscribing to compositor events.
