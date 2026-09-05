#!/usr/bin/env python3
"""Fox Switcher — double-tap Shift retypes what you just typed in the next keyboard layout.

Mode A (typed run): buffered keycodes are erased and replayed after advancing the
layout, so the compositor does the character mapping. No translation tables.
Mode B (selection): selected text is mapped back to keycodes via the live keymap,
then replayed the same way.

Run `fox_switcher.py --selftest` for the behavioural checks.
"""
import os
import selectors
import subprocess
import sys
import time

# ---------------------------------------------------------------- tuning ----
DOUBLE_TAP_S = 0.30     # max gap between the two Shift taps
# Injected key events are dropped if they arrive too fast, and the loss grows
# with the length of the run: measured against Ghostty, 2ms lost most of a
# 38-char sentence, 5ms was clean at 38 chars but lost a 77-char one, 8ms and
# 10ms were clean at both. Backspaces are rate-limited the same way — pacing
# only the replay leaves un-erased text behind. Lower this and long sentences
# start coming out truncated.
KEY_DELAY_S = 0.010     # between injected keystrokes
SETTLE_S = 0.010        # after a layout switch, before replaying
IDLE_CLEAR_S = 60.0     # forget the buffer after this much silence
BUFFER_CAP = 200        # max remembered keystrokes
RESCAN_S = 1.0          # device hotplug poll
SUBPROC_S = 2.0         # give up on any helper process that blocks this long
VIRTUAL_NAME = "Fox Switcher virtual keyboard"
TERMINAL_CLASSES = {
    "com.mitchellh.ghostty", "ghostty", "Alacritty", "alacritty",
    "foot", "footclient", "kitty", "org.wezfurlong.wezterm", "xterm",
}

# evdev keycodes. Kept literal so the pure logic needs no evdev import.
KEY_BACKSPACE, KEY_TAB, KEY_ENTER, KEY_ESC = 14, 15, 28, 1
KEY_LEFTSHIFT, KEY_RIGHTSHIFT = 42, 54
KEY_KPENTER, KEY_CAPSLOCK = 96, 58
SHIFTS = {KEY_LEFTSHIFT, KEY_RIGHTSHIFT}
# ctrl, alt, super (left and right)
NONSHIFT_MODS = {29, 97, 56, 100, 125, 126}
NAV = {103, 105, 106, 108, 102, 107, 104, 109, 110, 111}  # arrows, home/end, pgup/dn, ins, del
# Keypad keys type a digit with NumLock on and navigate with it off, and we
# don't track the LED. Caps Lock is the Compose key under `compose:caps`, where
# one sequence spans several keycodes but produces a single character. Either
# way the buffer would stop matching the screen, so both just reset it: a
# conversion that declines to fire beats one that eats the wrong characters.
KEYPAD = set(range(71, 84)) | {55, 98, 117, 121}
BOUNDARY = {KEY_ENTER, KEY_KPENTER, KEY_TAB, KEY_ESC, KEY_CAPSLOCK} | NAV | KEYPAD
PRINTABLE = (
    set(range(2, 14))       # 1..0 - =
    | set(range(16, 28))    # q..]
    | set(range(30, 41))    # a..'
    | {41, 43}              # ` \
    | set(range(44, 54))    # z../
    | {57}                  # space
    | {86}                  # 102nd key (<>) on ISO keyboards
)


# ------------------------------------------------------------ pure logic ----
class Switcher:
    """All the behaviour, none of the I/O. `env` is the single seam."""

    def __init__(self, env):
        self.env = env
        self.buf = []            # [(keycode, shift_held)]
        self.mods = set()
        self.last_shift = None   # (keycode, timestamp)
        self.key_since_shift = False
        self.armed = None        # shift keycode whose release will fire
        self.sel_gesture = False
        self.dragging = None     # None | False (pressed) | True (pressed+moved)
        self.last_activity = 0.0

    # -- input events -------------------------------------------------------
    def on_key(self, code, down, t):
        self.last_activity = t

        if code in SHIFTS:
            if down:
                self.mods.add(code)
                self._shift_press(code, t)
            else:
                self.mods.discard(code)
                if self.armed == code:
                    self.armed = None
                    self.fire()
            return

        if code in NONSHIFT_MODS:
            (self.mods.add if down else self.mods.discard)(code)
            return

        if not down:
            return

        self.key_since_shift = True
        self.armed = None

        if self.mods & NONSHIFT_MODS:      # a shortcut, not text
            self.clear()
        elif code in NAV and self.mods & SHIFTS:
            self.clear()                   # keyboard selection
            self.sel_gesture = True
        elif code in BOUNDARY:
            self.clear()
        elif code == KEY_BACKSPACE:
            if self.buf:
                self.buf.pop()
        elif code in PRINTABLE:
            self.buf.append((code, bool(self.mods & SHIFTS)))
            del self.buf[:-BUFFER_CAP]

    # ponytail: no Hyprland socket2 subscription for focus changes. Every way
    # of changing focus goes through a click or a mod-combo, both of which
    # already clear the buffer. Subscribe if a case turns up that doesn't.
    def on_button(self, down, t):
        self.last_activity = t
        if down:
            self.clear()
            self.dragging = False
        else:
            if self.dragging:
                self.sel_gesture = True
            self.dragging = None

    def on_motion(self, t):
        self.last_activity = t
        if self.dragging is False:
            self.dragging = True

    def tick(self, t):
        if self.buf and t - self.last_activity > IDLE_CLEAR_S:
            self.clear()

    # -- internals ----------------------------------------------------------
    def clear(self):
        self.buf.clear()
        self.sel_gesture = False

    def _shift_press(self, code, t):
        prev = self.last_shift
        if prev and prev[0] == code and not self.key_since_shift and t - prev[1] <= DOUBLE_TAP_S:
            # Fire on the *release* of this tap: Shift is physically down right
            # now, and every replayed key would come out shifted.
            self.armed = code
            self.last_shift = None
        else:
            self.last_shift = (code, t)
        self.key_since_shift = False

    def fire(self):
        if self.buf:
            self._mode_a()
        elif self.sel_gesture:
            self._mode_b()

    def _mode_a(self):
        self.env.send_backspace(len(self.buf))
        self.env.switch_next()
        self.env.send_keys(list(self.buf))
        # buffer kept as-is: another double-tap advances to the next layout

    def _mode_b(self):
        text = self.env.primary()
        if not text:
            return
        src = self._best_group(text)
        seq = []
        for ch in text:
            k = self.env.char_to_key(ch, src)
            if k is None:
                self.env.notify(f"Fox Switcher: cannot type {ch!r} in this layout")
                return
            seq.append(k)
        tgt = (src + 1) % self.env.n_groups
        if self.env.focused_class() in TERMINAL_CLASSES:
            # A terminal selection is a viewport overlay, not editable text:
            # typing would append garbage at the prompt instead of replacing it.
            out = "".join(self.env.key_to_char(c, tgt, sh) or "" for c, sh in seq)
            self.env.clip(out)
            self.env.notify("Fox Switcher: converted text copied to clipboard")
        else:
            self.env.switch_to(tgt)
            self.env.send_keys(seq)
        self.sel_gesture = False

    def _best_group(self, text):
        active = self.env.active_group()
        order = [active] + [g for g in range(self.env.n_groups) if g != active]
        best, best_n = active, -1
        for g in order:
            n = sum(1 for ch in text if self.env.char_to_key(ch, g))
            if n > best_n:                 # strict: ties keep the active group
                best, best_n = g, n
        return best


# ------------------------------------------------------------------ I/O ----
class Env:
    """Everything that touches the outside world."""

    def __init__(self):
        from evdev import UInput, ecodes
        from xkbcommon import xkb

        opt = lambda name: (self._hypr("getoption", f"input:{name}", "-j") or {}).get("str") or ""
        self.keymap = xkb.Context().keymap_new_from_names(
            rules=opt("kb_rules") or None,
            model=opt("kb_model") or None,
            layout=opt("kb_layout") or "us",
            variant=opt("kb_variant") or None,
            options=opt("kb_options") or None,
        )
        self.state = self.keymap.state_new()
        self.n_groups = self.keymap.num_layouts()
        self.names = [self.keymap.layout_get_name(g) for g in range(self.n_groups)]
        self.shift_mask = 1 << self.keymap.mod_get_index("Shift")

        # char -> (keycode, shift) per group, built from the live keymap
        self.rev = []
        for g in range(self.n_groups):
            m = {}
            for code in range(1, 128):
                for shift in (False, True):
                    ch = self.key_to_char(code, g, shift)
                    if ch and ch.isprintable() and ch not in m:
                        m[ch] = (code, shift)
            self.rev.append(m)

        self.ui = UInput({ecodes.EV_KEY: list(range(1, 128))}, name=VIRTUAL_NAME)
        self.ecodes = ecodes

    # -- keymap oracle ------------------------------------------------------
    def key_to_char(self, code, group, shift):
        self.state.update_mask(self.shift_mask if shift else 0, 0, 0, 0, 0, group)
        return self.state.key_get_string(code + 8)  # evdev -> xkb keycode

    def char_to_key(self, ch, group):
        return self.rev[group].get(ch)

    # -- helper processes ---------------------------------------------------
    @staticmethod
    def _run(cmd, **kw):
        """Run a helper without ever wedging the daemon.

        Everything here is single-threaded: one blocked helper and no
        keystroke gets handled again. Discarding stdio by default also
        matters for wl-copy, which forks a child to serve the selection —
        inheriting our stdout would leave that child holding the pipe.
        """
        kw.setdefault("stdout", subprocess.DEVNULL)
        kw.setdefault("stderr", subprocess.DEVNULL)
        try:
            return subprocess.run(cmd, timeout=SUBPROC_S, **kw)
        except (subprocess.TimeoutExpired, OSError):
            return None

    # -- hyprland -----------------------------------------------------------
    @staticmethod
    def _hypr(*args):
        import json
        r = Env._run(["hyprctl", *args], stdout=subprocess.PIPE, text=True)
        if r is None or r.returncode != 0:
            return None
        try:
            return json.loads(r.stdout)
        except ValueError:
            return r.stdout.strip()

    def active_group(self):
        d = self._hypr("devices", "-j") or {}
        kbs = d.get("keyboards") or []
        kb = next((k for k in kbs if k.get("main")), kbs[0] if kbs else None)
        if kb and kb.get("active_keymap") in self.names:
            return self.names.index(kb["active_keymap"])
        return 0

    def switch_next(self):
        self._hypr("switchxkblayout", "all", "next")
        time.sleep(SETTLE_S)

    def switch_to(self, g):
        self._hypr("switchxkblayout", "all", str(g))
        time.sleep(SETTLE_S)

    def focused_class(self):
        return (self._hypr("activewindow", "-j") or {}).get("class", "")

    # -- injection ----------------------------------------------------------
    def _tap(self, code, shift):
        ev = self.ecodes.EV_KEY
        if shift:
            self.ui.write(ev, KEY_LEFTSHIFT, 1)
        self.ui.write(ev, code, 1)
        self.ui.write(ev, code, 0)
        if shift:
            self.ui.write(ev, KEY_LEFTSHIFT, 0)
        self.ui.syn()
        time.sleep(KEY_DELAY_S)

    def send_backspace(self, n):
        for _ in range(n):
            self._tap(KEY_BACKSPACE, False)

    def send_keys(self, keys):
        for code, shift in keys:
            self._tap(code, shift)

    # -- selection / clipboard / notify -------------------------------------
    @staticmethod
    def primary():
        r = Env._run(["wl-paste", "--primary", "--no-newline"],
                     stdout=subprocess.PIPE, text=True)
        return r.stdout if r and r.returncode == 0 else ""

    @staticmethod
    def clip(text):
        Env._run(["wl-copy"], input=text, text=True)

    @staticmethod
    def notify(msg):
        Env._run(["notify-send", "-t", "2000", msg])


# ----------------------------------------------------------------- daemon ----
def _open_devices(sel, open_paths):
    """Attach new input devices, drop vanished ones. Returns nothing; mutates.

    ponytail: 1s poll instead of a udev netlink subscription. Nobody types
    within a second of plugging a keyboard in; subscribe to udev if that
    ever stops being true.
    """
    from evdev import InputDevice, ecodes, list_devices

    live = set(list_devices())
    for path in list(open_paths):
        if path not in live:
            dev = open_paths.pop(path)
            try:
                sel.unregister(dev)
                dev.close()
            except (KeyError, OSError):
                pass

    for path in sorted(live - set(open_paths)):
        try:
            dev = InputDevice(path)
        except OSError:
            continue
        keys = set(dev.capabilities().get(ecodes.EV_KEY, []))
        wanted = (KEY_LEFTSHIFT in keys and 30 in keys) or ecodes.BTN_LEFT in keys
        if not wanted or dev.name == VIRTUAL_NAME:
            dev.close()
            continue
        sel.register(dev, selectors.EVENT_READ)
        open_paths[path] = dev


def main():
    from evdev import ecodes

    try:
        env = Env()
    except Exception as e:                                   # noqa: BLE001
        sys.exit(f"fox-switcher: cannot start: {e}")

    sw = Switcher(env)
    sel = selectors.DefaultSelector()
    open_paths = {}
    _open_devices(sel, open_paths)
    if not open_paths:
        sys.exit("fox-switcher: no readable input devices. Are you in the 'input' "
                 "group, and did you start a fresh login session since joining it?")

    print(f"fox-switcher: watching {len(open_paths)} devices, "
          f"{env.n_groups} layouts: {', '.join(env.names)}", flush=True)

    next_scan = time.monotonic() + RESCAN_S
    while True:
        for key, _ in sel.select(timeout=RESCAN_S):
            dev = key.fileobj
            try:
                for e in dev.read():
                    if e.type == ecodes.EV_KEY:
                        if e.code == ecodes.BTN_LEFT:
                            sw.on_button(e.value == 1, e.timestamp())
                        elif e.value in (0, 1):   # ignore autorepeat
                            sw.on_key(e.code, e.value == 1, e.timestamp())
                    elif e.type == ecodes.EV_REL:
                        sw.on_motion(e.timestamp())
            except OSError:
                try:
                    sel.unregister(dev)
                except KeyError:
                    pass
                open_paths.pop(dev.path, None)

        now = time.monotonic()
        sw.tick(time.time())
        if now >= next_scan:
            _open_devices(sel, open_paths)
            next_scan = now + RESCAN_S


# --------------------------------------------------------------- selftest ----
class FakeEnv:
    """Two fake layouts: group 0 types 'abc', group 1 types 'xyz'. Same keycodes."""
    n_groups = 2
    TABLE = {0: {10: "a", 11: "b", 12: "c"}, 1: {10: "x", 11: "y", 12: "z"}}

    def __init__(self, active=0, primary="", cls="firefox"):
        self.calls = []
        self._active = active
        self._primary = primary
        self._cls = cls

    def send_backspace(self, n): self.calls.append(("bs", n))
    def send_keys(self, keys): self.calls.append(("keys", list(keys)))
    def switch_next(self): self.calls.append(("next",))
    def switch_to(self, g): self.calls.append(("to", g))
    def clip(self, t): self.calls.append(("clip", t))
    def notify(self, m): self.calls.append(("notify", m))
    def primary(self): return self._primary
    def focused_class(self): return self._cls
    def active_group(self): return self._active

    def key_to_char(self, code, group, shift):
        ch = self.TABLE[group].get(code)
        return ch.upper() if ch and shift else ch

    def char_to_key(self, ch, group):
        for code, c in self.TABLE[group].items():
            if c == ch:
                return (code, False)
            if c.upper() == ch:
                return (code, True)
        return None


def selftest():
    # Not a logic check: injecting faster than this drops keystrokes, and the
    # loss grows with the length of the run, so long sentences come out
    # truncated. Measured against Ghostty; 5ms was already too fast at 77
    # chars. Raise it if a slower machine still truncates, never lower it.
    assert KEY_DELAY_S >= 0.008, f"KEY_DELAY_S={KEY_DELAY_S} drops keystrokes"

    def tap_shift(sw, t, code=KEY_LEFTSHIFT):
        sw.on_key(code, True, t)
        sw.on_key(code, False, t + 0.01)

    def double_tap(sw, t, code=KEY_LEFTSHIFT):
        tap_shift(sw, t, code)
        tap_shift(sw, t + 0.1, code)

    # --- trigger detection ---
    sw = Switcher(FakeEnv()); sw.on_key(10, True, 0); sw.on_key(10, False, 0)
    double_tap(sw, 1.0)
    assert sw.env.calls, "double-tap must fire"

    sw = Switcher(FakeEnv()); sw.on_key(10, True, 0); sw.on_key(10, False, 0)
    tap_shift(sw, 1.0)
    sw.on_key(11, True, 1.05); sw.on_key(11, False, 1.06)   # key in between
    tap_shift(sw, 1.1)
    assert not sw.env.calls, "intervening key must not fire"

    sw = Switcher(FakeEnv()); sw.on_key(10, True, 0); sw.on_key(10, False, 0)
    tap_shift(sw, 1.0); tap_shift(sw, 2.0)                  # too slow
    assert not sw.env.calls, "slow taps must not fire"

    sw = Switcher(FakeEnv()); sw.on_key(10, True, 0); sw.on_key(10, False, 0)
    tap_shift(sw, 1.0, KEY_LEFTSHIFT); tap_shift(sw, 1.1, KEY_RIGHTSHIFT)
    assert not sw.env.calls, "different Shift keys must not fire"

    # --- mode A ---
    sw = Switcher(FakeEnv())
    for c in (10, 11, 12):
        sw.on_key(c, True, 0); sw.on_key(c, False, 0)
    double_tap(sw, 1.0)
    assert sw.env.calls == [("bs", 3), ("next",),
                            ("keys", [(10, False), (11, False), (12, False)])], sw.env.calls

    # second trigger advances again, same keys
    sw.env.calls.clear()
    double_tap(sw, 2.0)
    assert sw.env.calls == [("bs", 3), ("next",),
                            ("keys", [(10, False), (11, False), (12, False)])], sw.env.calls

    # shift state survives
    sw = Switcher(FakeEnv())
    sw.on_key(KEY_LEFTSHIFT, True, 0); sw.on_key(10, True, 0); sw.on_key(10, False, 0)
    sw.on_key(KEY_LEFTSHIFT, False, 0.01)
    sw.on_key(11, True, 0.2); sw.on_key(11, False, 0.2)
    double_tap(sw, 1.0)
    assert sw.env.calls[2] == ("keys", [(10, True), (11, False)]), sw.env.calls

    # Shift must not stay latched after a conversion fires
    sw = Switcher(FakeEnv())
    sw.on_key(10, True, 0); sw.on_key(10, False, 0)
    double_tap(sw, 1.0)
    assert not sw.mods, f"modifiers must be clean after firing: {sw.mods}"
    sw.env.calls.clear()
    sw.on_key(11, True, 1.5); sw.on_key(11, False, 1.5)
    double_tap(sw, 2.0)
    assert sw.env.calls[2] == ("keys", [(10, False), (11, False)]), sw.env.calls

    # --- boundaries ---
    for boundary in (KEY_ENTER, KEY_TAB, KEY_ESC, 103):
        sw = Switcher(FakeEnv())
        sw.on_key(10, True, 0); sw.on_key(10, False, 0)
        sw.on_key(boundary, True, 0.1); sw.on_key(boundary, False, 0.1)
        double_tap(sw, 1.0)
        assert not sw.env.calls, f"keycode {boundary} must clear the buffer"

    sw = Switcher(FakeEnv())                                  # ctrl combo
    sw.on_key(10, True, 0); sw.on_key(10, False, 0)
    sw.on_key(29, True, 0.1); sw.on_key(11, True, 0.1); sw.on_key(11, False, 0.1)
    sw.on_key(29, False, 0.2)
    double_tap(sw, 1.0)
    assert not sw.env.calls, "Ctrl combo must clear the buffer"

    # Keys that put a character on screen without landing in the buffer would
    # make the backspace count too low and eat text that was already there.
    for code in (79, 71, 82, 55, 98, KEY_CAPSLOCK):           # KP1 KP7 KP0 KP* KP/ compose
        sw = Switcher(FakeEnv())
        for c in (10, 11, 12):
            sw.on_key(c, True, 0); sw.on_key(c, False, 0)
        sw.on_key(code, True, 0.1); sw.on_key(code, False, 0.1)
        double_tap(sw, 1.0)
        assert not sw.env.calls, f"keycode {code} must reset the buffer, not desync it"

    sw = Switcher(FakeEnv())                                  # 102nd key is normal text
    sw.on_key(86, True, 0); sw.on_key(86, False, 0)
    sw.on_key(10, True, 0.1); sw.on_key(10, False, 0.1)
    double_tap(sw, 1.0)
    assert sw.env.calls[0] == ("bs", 2), sw.env.calls

    sw = Switcher(FakeEnv())                                  # mouse click
    sw.on_key(10, True, 0); sw.on_key(10, False, 0)
    sw.on_button(True, 0.1); sw.on_button(False, 0.2)
    double_tap(sw, 1.0)
    assert not sw.env.calls, "click must clear the buffer"

    sw = Switcher(FakeEnv())                                  # backspace pops one
    for c in (10, 11, 12):
        sw.on_key(c, True, 0); sw.on_key(c, False, 0)
    sw.on_key(KEY_BACKSPACE, True, 0.1); sw.on_key(KEY_BACKSPACE, False, 0.1)
    double_tap(sw, 1.0)
    assert sw.env.calls[0] == ("bs", 2), sw.env.calls

    sw = Switcher(FakeEnv())                                  # idle expiry
    sw.on_key(10, True, 0); sw.on_key(10, False, 0)
    sw.tick(IDLE_CLEAR_S + 1)
    double_tap(sw, IDLE_CLEAR_S + 2)
    assert not sw.env.calls, "idle must clear the buffer"

    sw = Switcher(FakeEnv())                                  # cap
    for i in range(BUFFER_CAP + 20):
        sw.on_key(10, True, 0); sw.on_key(10, False, 0)
    assert len(sw.buf) == BUFFER_CAP, len(sw.buf)

    # --- mode B ---
    sw = Switcher(FakeEnv(primary="abc"))                     # no gesture -> nothing
    double_tap(sw, 1.0)
    assert not sw.env.calls, "no selection gesture must not fire mode B"

    sw = Switcher(FakeEnv(primary="abc"))                     # mouse drag
    sw.on_button(True, 0.1); sw.on_motion(0.15); sw.on_button(False, 0.2)
    double_tap(sw, 1.0)
    assert sw.env.calls == [("to", 1),
                            ("keys", [(10, False), (11, False), (12, False)])], sw.env.calls

    sw = Switcher(FakeEnv(primary="abc"))                     # click without drag
    sw.on_button(True, 0.1); sw.on_button(False, 0.2)
    double_tap(sw, 1.0)
    assert not sw.env.calls, "a click with no motion is not a selection"

    sw = Switcher(FakeEnv(primary="abc"))                     # shift+arrow selection
    sw.on_key(KEY_LEFTSHIFT, True, 0.1)
    sw.on_key(105, True, 0.15); sw.on_key(105, False, 0.16)
    sw.on_key(KEY_LEFTSHIFT, False, 0.2)
    double_tap(sw, 1.0)
    assert sw.env.calls[0] == ("to", 1), sw.env.calls

    # source group detected from the text, not from the active layout
    sw = Switcher(FakeEnv(primary="xyz", active=0))
    sw.on_button(True, 0.1); sw.on_motion(0.15); sw.on_button(False, 0.2)
    double_tap(sw, 1.0)
    assert sw.env.calls[0] == ("to", 0), sw.env.calls   # xyz is group 1 -> next is 0

    # terminal: copy instead of typing
    sw = Switcher(FakeEnv(primary="abc", cls="com.mitchellh.ghostty"))
    sw.on_button(True, 0.1); sw.on_motion(0.15); sw.on_button(False, 0.2)
    double_tap(sw, 1.0)
    assert sw.env.calls[0] == ("clip", "xyz"), sw.env.calls
    assert not any(c[0] in ("keys", "to") for c in sw.env.calls), sw.env.calls

    # unmappable character aborts
    sw = Switcher(FakeEnv(primary="ab☃"))
    sw.on_button(True, 0.1); sw.on_motion(0.15); sw.on_button(False, 0.2)
    double_tap(sw, 1.0)
    assert len(sw.env.calls) == 1 and sw.env.calls[0][0] == "notify", sw.env.calls

    # --- keymap oracle round-trip against the real installed layouts ---
    try:
        env = Env.__new__(Env)
        from xkbcommon import xkb
        env.keymap = xkb.Context().keymap_new_from_names(layout="us,ru")
        env.state = env.keymap.state_new()
        env.n_groups = env.keymap.num_layouts()
        env.shift_mask = 1 << env.keymap.mod_get_index("Shift")
        env.rev = []
        for g in range(env.n_groups):
            m = {}
            for code in range(1, 128):
                for shift in (False, True):
                    ch = env.key_to_char(code, g, shift)
                    if ch and ch.isprintable() and ch not in m:
                        m[ch] = (code, shift)
            env.rev.append(m)
        n = 0
        for g in range(env.n_groups):
            for ch, (code, shift) in env.rev[g].items():
                assert env.key_to_char(code, g, shift) == ch, (g, ch)
                n += 1
        print(f"  oracle round-trip: {n} chars across {env.n_groups} groups")
    except ImportError:
        print("  oracle round-trip: SKIPPED (xkbcommon not installed)")

    print("selftest: all checks passed")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main()
