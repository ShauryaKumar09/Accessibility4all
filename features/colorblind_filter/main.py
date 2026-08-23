"""Color Blind Filter — toggles Windows' built-in system-wide color filter.

Runs as its own process when toggled ON in the hub. Every control (enable
switch, filter type) now lives in the hub's settings sheet, which calls
`set_filter()` directly since this is a single registry write, not continuous
state — see hub.py's `_apply_colorblind_filter`. This process draws one small
pill confirming the current filter, and re-applies it on startup in case the
setting changed while it wasn't running.

Applies system-wide (not just Chrome) by driving Windows' own Ease of
Access > Color Filters feature: set HKCU\\Software\\Microsoft\\ColorFiltering
directly (see `set_filter()` below for why a live-toggle shortcut isn't used).
See features/README.md.
"""

from __future__ import annotations

import signal
import sys
from pathlib import Path

import tkinter as tk

FEATURE_DIR = Path(__file__).resolve().parent
ROOT = FEATURE_DIR.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import console, platform as plat, settings_store as store, ui_kit as ui  # noqa: E402
from shared.ui_kit import C                                                          # noqa: E402

console.configure_stdio()

FEATURE_ID = "colorblind_filter"
COLOR_FILTERING_PATH = r"Software\Microsoft\ColorFiltering"

FILTER_TYPES = {
    "Deuteranopia": 3,
    "Protanopia": 4,
    "Tritanopia": 5,
    "Grayscale": 0,
    "Invert": 1,
    "Grayscale Inverted": 2,
}

BUBBLE_H = 52
DOT = 12
SHOW_MS = 3000          # how long the confirmation stays up
WATCH_MS = 700          # how often we notice the hub editing settings.json


def log(msg: str):
    console.safe_print(f"[colorblind_filter] {msg}", flush=True)


def default_settings() -> dict:
    return dict(store.DEFAULTS[FEATURE_ID])


def load_settings() -> dict:
    return store.load(FEATURE_ID)


def save_settings(settings: dict):
    store.save(FEATURE_ID, settings)


def _read_registry_dword(root, path: str, name: str) -> int | None:
    import winreg

    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ) as key:
            value, vtype = winreg.QueryValueEx(key, name)
            if vtype == winreg.REG_DWORD:
                return int(value)
    except FileNotFoundError:
        return None
    except OSError:
        return None
    return None


def _set_registry_dword(root, path: str, name: str, value: int):
    import winreg

    with winreg.CreateKeyEx(root, path, 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, value)


def is_filter_active() -> bool:
    if not plat.IS_WINDOWS:
        return False
    import winreg

    return bool(_read_registry_dword(winreg.HKEY_CURRENT_USER, COLOR_FILTERING_PATH, "Active"))


def set_filter(enabled: bool, filter_type: int):
    """Write the registry state directly — the only reliable mechanism.

    atbroker.exe /colorfiltershortcut (simulating the Win+Ctrl+C hotkey) was
    tried first, but it's a TOGGLE relative to the OS's own internal state,
    and it measurably fails silently in some environments (non-zero exit, no
    error text, Active left unchanged) — so a "successful" call is
    indistinguishable from one that just undid our own write. Direct registry
    write is idempotent and verifiable; the tradeoff is the OS may need a
    lock/unlock (Win+L) or sign-out to visually refresh instantly.
    """
    if not plat.IS_WINDOWS:
        raise RuntimeError("Color filters are only available on Windows.")
    import winreg

    _set_registry_dword(winreg.HKEY_CURRENT_USER, COLOR_FILTERING_PATH, "FilterType", filter_type)
    _set_registry_dword(winreg.HKEY_CURRENT_USER, COLOR_FILTERING_PATH, "Active", 1 if enabled else 0)
    if is_filter_active() != enabled:
        raise RuntimeError("Registry updated but Windows did not confirm the new state.")


class ColorblindFilterApp(tk.Tk):
    """The bubble: a dot (colored when on, muted when off) and the filter name."""

    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self._watcher = store.Watcher(FEATURE_ID)
        self._hide_after: str | None = None

        self.fonts = ui.FontSet(1.0)
        self.title("Color Blind Filter")
        self.resizable(False, False)
        self._width = 0
        self._transparent = False
        self.canvas: tk.Canvas | None = None

        self._apply_and_draw()
        save_settings(self.settings)
        self.show()
        self.after(WATCH_MS, self._watch_settings)
        self.protocol("WM_DELETE_WINDOW", self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT, self._shutdown)

    def _label(self) -> str:
        if not plat.IS_WINDOWS:
            return "Windows only"
        if not self.settings.get("enabled"):
            return "Filter off"
        return f"Filter: {self.settings.get('filter_name', 'Deuteranopia')}"

    def _rebuild_canvas(self):
        text_w = self.fonts["ui"].measure(self._label())
        self._width = 20 * 2 + DOT + 12 + text_w
        if self.canvas is not None:
            self.canvas.destroy()
        self._transparent = ui.make_bubble(self, self._width, BUBBLE_H)
        self.canvas = ui.bubble_canvas(self, self._width, BUBBLE_H, self._transparent)
        self.canvas.pack(fill="both", expand=True)

    def _draw(self):
        self.canvas.delete("all")
        ui.pill(self.canvas, 1, 1, self._width - 1, BUBBLE_H - 1,
                fill=C["BUBBLE"], outline=C["BORDER_CTRL"], width=1)
        cy = BUBBLE_H / 2
        dot_color = C["ON"] if (plat.IS_WINDOWS and self.settings.get("enabled")) else C["DOT_IDLE"]
        self.canvas.create_oval(20, cy - DOT / 2, 20 + DOT, cy + DOT / 2,
                                fill=dot_color, outline="")
        self.canvas.create_text(20 + DOT + 12, cy, anchor="w",
                                text=self._label(), font=self.fonts["ui"],
                                fill=C["FG_BUBBLE"])

    def _apply_and_draw(self):
        if plat.IS_WINDOWS:
            try:
                set_filter(bool(self.settings.get("enabled", False)),
                          FILTER_TYPES[self.settings.get("filter_name", "Deuteranopia")])
            except Exception as e:
                log(f"apply failed: {e}")
        self._rebuild_canvas()
        self._draw()

    def show(self):
        """Bottom-centre, briefly — then out of the way."""
        ui.place_bottom_center(self, self._width, BUBBLE_H)
        ui.raise_bubble(self)
        if self._hide_after:
            self.after_cancel(self._hide_after)
        self._hide_after = self.after(SHOW_MS, self.withdraw)

    def _watch_settings(self):
        if self._watcher.changed():
            self.settings = load_settings()
            log(f"settings changed in the hub — {self._label()}")
            self._apply_and_draw()
            self.show()
        self.after(WATCH_MS, self._watch_settings)

    def _shutdown(self, *_args):
        log("shutting down")
        try:
            self.destroy()
        except Exception:
            pass
        sys.exit(0)


def main():
    log("feature started")
    ColorblindFilterApp().mainloop()


if __name__ == "__main__":
    main()
