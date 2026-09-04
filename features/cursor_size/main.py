"""Cursor Size — lets you scale Windows' mouse pointer up or down.

Helps anyone who loses track of a small cursor (low vision, a big or
high-DPI screen). Drives the same "Pointer size" control as Settings >
Accessibility > Mouse pointer and touch, so it changes the cursor
everywhere, not just in one app. This process just shows a small
confirmation pill when the size changes; the actual registry write happens
in hub.py (see hub.py's `_apply_cursor_size` for why).

See features/README.md.
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

from shared import console, feature_bus, platform as plat, settings_store as store  # noqa: E402
from shared import webbubble as wb                                     # noqa: E402
from shared.ui_kit import C                                            # noqa: E402

console.configure_stdio()

FEATURE_ID = "cursor_size"

BUBBLE_W, BUBBLE_H = 182, 40
COIN = 34               # what it folds down to: a status light, tappable
SHOW_MS = 2200
WATCH_MS = 700

BODY = """
<div class="pill" id="pill">
  <span class="dot" id="dot"></span>
  <span class="label" id="label"></span>
  <span class="glyph" id="glyph">↖</span>
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
    console.safe_print(f"[cursor_size] {msg}", flush=True)


def _read_registry_state():
    """Read back the two registry values hub.py's _apply_cursor_size writes,
    for correlation with the hub's own apply_result log line."""
    if not plat.IS_WINDOWS:
        return None, None
    import winreg
    cursor_size = None
    base_size = None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Accessibility",
                            0, winreg.KEY_READ) as key:
            cursor_size, _ = winreg.QueryValueEx(key, "CursorSize")
    except OSError:
        pass
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Cursors",
                            0, winreg.KEY_READ) as key:
            base_size, _ = winreg.QueryValueEx(key, "CursorBaseSize")
    except OSError:
        pass
    return cursor_size, base_size


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


class CursorSizeApp:
    def __init__(self):
        self.settings = load_settings()
        self._watcher = store.Watcher(FEATURE_ID)
        self._sched = wb.Scheduler(on_error=lambda e: log(f"timer failed: {e}"))
        self.bubble = wb.Bubble("Cursor Size", BODY, BUBBLE_W, BUBBLE_H,
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
        verified_cursor_size, verified_base_size = _read_registry_state()
        console.log_event("cursor_size_subprocess", "on_started",
                          settings_size=self.settings.get("size"),
                          verified_cursor_size=verified_cursor_size,
                          verified_base_size=verified_base_size)
        self._draw()
        save_settings(self.settings)
        self.bubble.show()
        self._announce()
        self._sched.after(900, self._arrange_loop)
        self._sched.after(WATCH_MS, self._watch_settings)

    def _label(self) -> str:
        if not plat.IS_WINDOWS:
            return "Windows only"
        return f"Cursor size: {self.settings.get('size', 1)}"

    def _draw(self):
        self.bubble.set_text("label", self._label())
        self.bubble.set_style("dot", "background", C["ON"])

    def _watch_settings(self):
        if self._watcher.changed():
            self.settings = load_settings()
            log(f"settings changed in the hub — {self._label()}")
            verified_cursor_size, verified_base_size = _read_registry_state()
            console.log_event("cursor_size_subprocess", "watch_settings_apply",
                              settings_size=self.settings.get("size"),
                              verified_cursor_size=verified_cursor_size,
                              verified_base_size=verified_base_size)
            self._draw()
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
    CursorSizeApp().run()


if __name__ == "__main__":
    main()
