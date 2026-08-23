"""Dyslexia Font — a quiet desktop confirmation, nothing else.

Every control this feature used to show (font choice, letter spacing, line
height, the preview and the Windows substitution buttons) now lives in the
hub's settings sheet. The running process draws one small pill that says the
easier font is on, then hides itself; it stays alive so the hub's toggle keeps
meaning "on", and re-shows the pill briefly whenever the settings change.

Website font changes are still handled by the bundled Chrome extension. Windows
app font substitution is opt-in, backed up, and reversible — see
shared/windows_fonts.py, which the hub calls.
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

from shared import console, settings_store as store, ui_kit as ui  # noqa: E402
from shared import windows_fonts as winfonts                       # noqa: E402
from shared.ui_kit import C                                        # noqa: E402

console.configure_stdio()

FEATURE_ID = "dyslexia_font"
SETTINGS_FILE = FEATURE_DIR / "settings.json"

# Kept so anything importing this module still finds them.
FONT_CHOICES = winfonts.FONT_CHOICES
SUBSTITUTION_TARGETS = winfonts.SUBSTITUTION_TARGETS
apply_windows_substitution = winfonts.apply_windows_substitution
restore_windows_substitution = winfonts.restore_windows_substitution
is_font_installed = winfonts.is_font_installed

BUBBLE_H = 52
DOT = 12
SHOW_MS = 3000          # how long the confirmation stays up
WATCH_MS = 700          # how often we notice the hub editing settings.json


def log(msg: str):
    console.safe_print(f"[dyslexia_font] {msg}", flush=True)


def default_settings() -> dict:
    return dict(store.DEFAULTS[FEATURE_ID])


def load_settings() -> dict:
    return store.load(FEATURE_ID)


def save_settings(settings: dict):
    store.save(FEATURE_ID, settings)


class DyslexiaFontApp(tk.Tk):
    """The bubble: a 12px green dot and the words `Easier font on`."""

    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self._watcher = store.Watcher(FEATURE_ID)
        self._hide_after: str | None = None

        self.fonts = ui.FontSet(1.0)
        self.title("Dyslexia Font")
        self.resizable(False, False)
        text_w = self.fonts["ui"].measure("Easier font on")
        self._width = 20 * 2 + DOT + 12 + text_w
        self._transparent = ui.make_bubble(self, self._width, BUBBLE_H)
        self.canvas = ui.bubble_canvas(self, self._width, BUBBLE_H,
                                       self._transparent)
        self.canvas.pack(fill="both", expand=True)
        self._draw()

        save_settings(self.settings)
        self.show()
        self.after(WATCH_MS, self._watch_settings)
        self.protocol("WM_DELETE_WINDOW", self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT, self._shutdown)

    def _draw(self):
        self.canvas.delete("all")
        ui.pill(self.canvas, 1, 1, self._width - 1, BUBBLE_H - 1,
                fill=C["BUBBLE"], outline=C["BORDER_CTRL"], width=1)
        cy = BUBBLE_H / 2
        self.canvas.create_oval(20, cy - DOT / 2, 20 + DOT, cy + DOT / 2,
                                fill=C["ON"], outline="")
        self.canvas.create_text(20 + DOT + 12, cy, anchor="w",
                                text="Easier font on", font=self.fonts["ui"],
                                fill=C["FG_BUBBLE"])

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
            log(f"settings changed in the hub — font is "
                f"{self.settings.get('font_family')}")
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
    DyslexiaFontApp().mainloop()


if __name__ == "__main__":
    main()
