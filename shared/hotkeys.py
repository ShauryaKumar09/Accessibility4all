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
