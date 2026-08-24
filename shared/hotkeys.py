"""Hotkey strings shared by the hub's capture dialog and the feature listeners.

Stored form is the one the features already use: lowercase modifiers in a fixed
order plus one key, e.g. `ctrl+shift+y`, `shift+command+'`, `F9`.
"""

from __future__ import annotations

# tkinter event.state bits (same on macOS, Windows and X11 for these four).
SHIFT, CONTROL, ALT, COMMAND = 0x0001, 0x0004, 0x0010, 0x0008

_MODIFIER_KEYSYMS = {
    "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
    "Meta_L", "Meta_R", "Command", "Super_L", "Super_R", "Caps_Lock",
    "Option_L", "Option_R",
}

_NAMED = {
    "space": "space", "Return": "enter", "Escape": "esc", "Tab": "tab",
    "BackSpace": "backspace", "Delete": "delete", "Home": "home", "End": "end",
    "Up": "up", "Down": "down", "Left": "left", "Right": "right",
    "Prior": "page_up", "Next": "page_down",
}


def format_from_tk_event(event) -> str | None:
    """Build a stored hotkey string from a tkinter <KeyPress>.

    Returns None while only modifiers are held, so the caller can keep waiting
    for the real key.
    """
    keysym = event.keysym
    if keysym in _MODIFIER_KEYSYMS:
        return None

    parts = []
    state = event.state
    if state & CONTROL:
        parts.append("ctrl")
    if state & ALT:
        parts.append("alt")
    if state & SHIFT:
        parts.append("shift")
    if state & COMMAND:
        parts.append("command")

    if keysym.startswith("F") and keysym[1:].isdigit():
        key = keysym.upper()
    elif keysym in _NAMED:
        key = _NAMED[keysym]
    elif len(keysym) == 1:
        key = keysym.lower()
    elif event.char and event.char.isprintable() and len(event.char) == 1:
        key = event.char.lower()
    else:
        key = keysym.lower()

    parts.append(key)
    return "+".join(parts)


def capture_hotkey(timeout: float = 8.0) -> str | None:
    """Block until a key combo is pressed, return the same stored string shape
    as `format_from_tk_event`. Used by the hub's webview frontend, which has
    no tkinter <KeyPress> event to read — everything else about the stored
    format is identical, so both capture paths stay interchangeable.
    """
    import threading
    from pynput import keyboard

    mod_keys = {
        keyboard.Key.ctrl_l: "ctrl", keyboard.Key.ctrl_r: "ctrl",
        keyboard.Key.alt_l: "alt", keyboard.Key.alt_r: "alt",
        keyboard.Key.shift_l: "shift", keyboard.Key.shift_r: "shift",
        keyboard.Key.cmd: "command", keyboard.Key.cmd_r: "command",
    }
    named = {
        keyboard.Key.space: "space", keyboard.Key.enter: "enter",
        keyboard.Key.esc: "esc", keyboard.Key.tab: "tab",
        keyboard.Key.backspace: "backspace", keyboard.Key.delete: "delete",
        keyboard.Key.home: "home", keyboard.Key.end: "end",
        keyboard.Key.up: "up", keyboard.Key.down: "down",
        keyboard.Key.left: "left", keyboard.Key.right: "right",
        keyboard.Key.page_up: "page_up", keyboard.Key.page_down: "page_down",
    }

    modifiers: set[str] = set()
    result: list[str | None] = [None]
    done = threading.Event()

    def on_press(key):
        if key in mod_keys:
            modifiers.add(mod_keys[key])
            return
        parts = [m for m in ("ctrl", "alt", "shift", "command") if m in modifiers]
        name = getattr(key, "name", None)
        if name and name.startswith("f") and name[1:].isdigit():
            part = name.upper()
        elif key in named:
            part = named[key]
        elif getattr(key, "char", None):
            part = key.char.lower()
        else:
            part = str(key).replace("Key.", "").lower()
        parts.append(part)
        result[0] = "+".join(parts)
        done.set()
        return False  # stop the listener

    listener = keyboard.Listener(on_press=on_press)
    listener.start()
    done.wait(timeout)
    listener.stop()
    return result[0]


def pretty(spec: str) -> str:
    """Human-facing label for a stored hotkey, e.g. `shift+command+'` -> ⇧⌘'."""
    if not spec:
        return "—"
    symbols = {"ctrl": "⌃", "control": "⌃", "alt": "⌥", "option": "⌥",
               "shift": "⇧", "cmd": "⌘", "command": "⌘"}
    out = []
    for part in spec.split("+"):
        low = part.strip().lower()
        if low in symbols:
            out.append(symbols[low])
        elif low == "space":
            out.append("Space")
        elif len(part) == 1:
            out.append(part.upper())
        else:
            out.append(part.replace("_", " ").title() if len(part) > 2 else part.upper())
    return "".join(out)
