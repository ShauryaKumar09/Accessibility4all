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

import os
import signal
import sys
from pathlib import Path

FEATURE_DIR = Path(__file__).resolve().parent
ROOT = FEATURE_DIR.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import console, feature_bus, settings_store as store, webbubble as wb  # noqa: E402
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
BUBBLE_W, BUBBLE_H = 164, 40
COIN = 34               # what it folds down to: a status light, tappable
SHOW_MS = 3000          # how long the confirmation stays up
WATCH_MS = 700          # how often we notice the hub editing settings.json

BODY = """
<div class="pill" id="pill">
  <span class="dot"></span>
  <span class="label">Easier font on</span>
  <span class="glyph" id="glyph">Aa</span>
</div>
"""
CSS = """
#pill { height: 100%; gap: 9px; padding: 0 14px; cursor: pointer;
        overflow: hidden; }

/* Folded down: the glyph alone, filling the coin. The window is already the
   right size when this class lands — Python animates the window, CSS only
   says what is inside it. Tap it to see the full line again. */
#glyph { display: none; font-size: 15px; font-weight: 600; color: var(--fg-bubble); }
.small #pill { padding: 0; gap: 0; justify-content: center; }
.small #label, .small .label, .small #dot, .small .dot { display: none; }
.small #glyph { display: block; }
"""
JS = """
document.getElementById('pill').addEventListener('click',
  () => window.pywebview.api.tap());
"""


def log(msg: str):
    console.safe_print(f"[dyslexia_font] {msg}", flush=True)


def default_settings() -> dict:
    return dict(store.DEFAULTS[FEATURE_ID])


def load_settings() -> dict:
    return store.load(FEATURE_ID)


def save_settings(settings: dict):
    store.save(FEATURE_ID, settings)


class _BubbleApi:
    """What the bubble's page can call: a tap opens the full pill again."""

    def __init__(self, app):
        self._app = app

    def tap(self):
        self._app.tap()


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
                                css=CSS, js=JS, api=_BubbleApi(self), sched=self._sched,
                                on_closed=self._sched.stop)
        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT, self._shutdown)


    # ── the pill and the coin ──
    def _announce(self):
        """Open to the full pill, then fold down to the coin on its own."""
        self._cancel_fold()
        self.bubble.set_compact(False)
        self.bubble.call("document.body.classList.remove('small')")
        self.bubble.animate_size(BUBBLE_W, BUBBLE_H)
        self._reposition()
        self._fold_id = self._sched.after(SHOW_MS, self._fold)

    def _fold(self):
        """Settle into the coin: still there, out of the way."""
        self._cancel_fold()
        self.bubble.set_compact(True)
        self.bubble.call("document.body.classList.add('small')")
        self.bubble.animate_size(COIN, COIN)
        self._reposition()

    def _cancel_fold(self):
        if getattr(self, "_fold_id", None):
            self._sched.after_cancel(self._fold_id)
            self._fold_id = None

    def tap(self):
        """Tapping the bubble opens it again for a few seconds."""
        self._announce()

    def _reposition(self):
        """Take a slot in the bottom arrangement and say which one it is."""
        self.bubble.place_stacked(FEATURE_ID)
        feature_bus.update_presence(FEATURE_ID, os.getpid(), self.bubble.rect())

    def _arrange_loop(self):
        """The other bubbles come and go, so the slot is rechecked."""
        self._reposition()
        self._sched.after(900, self._arrange_loop)

    def run(self):
        self.bubble.run(self._on_started)

    def _on_started(self):
        save_settings(self.settings)
        self.bubble.show()
        self._announce()
        self._sched.after(900, self._arrange_loop)
        self._sched.after(WATCH_MS, self._watch_settings)

    def _watch_settings(self):
        if self._watcher.changed():
            self.settings = load_settings()
            log(f"settings changed in the hub — font is "
                f"{self.settings.get('font_family')}")
            self.bubble.show()
            self._announce()
        self._sched.after(WATCH_MS, self._watch_settings)

    def _shutdown(self, *_args):
        log("shutting down")
        feature_bus.remove_presence(FEATURE_ID)
        self._sched.stop()
        self.bubble.close()
        sys.exit(0)


def main():
    log("feature started")
    DyslexiaFontApp().run()


if __name__ == "__main__":
    main()
