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

FEATURE_DIR = Path(__file__).resolve().parent
ROOT = FEATURE_DIR.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import console, settings_store as store, webbubble as wb  # noqa: E402
from shared import windows_fonts as winfonts                          # noqa: E402

console.configure_stdio()

FEATURE_ID = "dyslexia_font"
SETTINGS_FILE = FEATURE_DIR / "settings.json"

# Kept so anything importing this module still finds them.
FONT_CHOICES = winfonts.FONT_CHOICES
SUBSTITUTION_TARGETS = winfonts.SUBSTITUTION_TARGETS
apply_windows_substitution = winfonts.apply_windows_substitution
restore_windows_substitution = winfonts.restore_windows_substitution
is_font_installed = winfonts.is_font_installed

# The design's confirmation bubble: a 52px pill, a 12px green dot, four words.
BUBBLE_W, BUBBLE_H = 196, 52
SHOW_MS = 3000          # how long the confirmation stays up
WATCH_MS = 700          # how often we notice the hub editing settings.json

BODY = """
<div class="pill" id="pill">
  <span class="dot"></span>
  <span class="label">Easier font on</span>
</div>
"""
CSS = """
#pill { height: 52px; gap: 12px; padding: 0 20px; }
"""


def log(msg: str):
    console.safe_print(f"[dyslexia_font] {msg}", flush=True)


def default_settings() -> dict:
    return dict(store.DEFAULTS[FEATURE_ID])


def load_settings() -> dict:
    return store.load(FEATURE_ID)


def save_settings(settings: dict):
    store.save(FEATURE_ID, settings)


class DyslexiaFontApp:
    """The bubble: a 12px green dot and the words `Easier font on`.

    It has no runtime controls at all — the font choice, spacing and preview
    belong in the hub. The process stays alive so the hub's toggle keeps
    meaning "on", and the pill reappears for a moment whenever a setting
    changes, then gets out of the way again.
    """

    def __init__(self):
        self.settings = load_settings()
        self._watcher = store.Watcher(FEATURE_ID)
        self._sched = wb.Scheduler(on_error=lambda e: log(f"timer failed: {e}"))
        self.bubble = wb.Bubble("Dyslexia Font", BODY, BUBBLE_W, BUBBLE_H,
                                css=CSS, sched=self._sched,
                                on_closed=self._sched.stop)
        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT, self._shutdown)

    def run(self):
        self.bubble.run(self._on_started)

    def _on_started(self):
        save_settings(self.settings)
        self.bubble.place_stacked("dyslexia_font")
        self.bubble.show(for_ms=SHOW_MS)
        self._sched.after(WATCH_MS, self._watch_settings)

    def _watch_settings(self):
        if self._watcher.changed():
            self.settings = load_settings()
            log(f"settings changed in the hub — font is "
                f"{self.settings.get('font_family')}")
            self.bubble.place_stacked("dyslexia_font")
        self.bubble.show(for_ms=SHOW_MS)
        self._sched.after(WATCH_MS, self._watch_settings)

    def _shutdown(self, *_args):
        log("shutting down")
        self._sched.stop()
        self.bubble.close()
        sys.exit(0)


def main():
    log("feature started")
    DyslexiaFontApp().run()


if __name__ == "__main__":
    main()
