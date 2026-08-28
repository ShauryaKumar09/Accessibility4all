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

import signal
import sys
from pathlib import Path

FEATURE_DIR = Path(__file__).resolve().parent
ROOT = FEATURE_DIR.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import console, platform as plat, settings_store as store  # noqa: E402
from shared import webbubble as wb                                     # noqa: E402
from shared.ui_kit import C                                            # noqa: E402

console.configure_stdio()

FEATURE_ID = "cursor_size"

BUBBLE_W, BUBBLE_H = 220, 52
SHOW_MS = 2200
WATCH_MS = 700

BODY = """
<div class="pill" id="pill">
  <span class="dot" id="dot"></span>
  <span class="label" id="label"></span>
</div>
"""
CSS = """
#pill { height: 52px; gap: 12px; padding: 0 20px; }
"""


def log(msg: str):
    console.safe_print(f"[cursor_size] {msg}", flush=True)


def load_settings() -> dict:
    return store.load(FEATURE_ID)


def save_settings(settings: dict):
    store.save(FEATURE_ID, settings)


class CursorSizeApp:
    def __init__(self):
        self.settings = load_settings()
        self._watcher = store.Watcher(FEATURE_ID)
        self._sched = wb.Scheduler(on_error=lambda e: log(f"timer failed: {e}"))
        self.bubble = wb.Bubble("Cursor Size", BODY, BUBBLE_W, BUBBLE_H,
                                css=CSS, sched=self._sched,
                                on_closed=self._sched.stop)
        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT, self._shutdown)

    def run(self):
        self.bubble.run(self._on_started)

    def _on_started(self):
        self._draw()
        save_settings(self.settings)
        self.bubble.place_stacked("cursor_size")
        self.bubble.show(for_ms=SHOW_MS)
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
            self._draw()
            self.bubble.place_stacked("cursor_size")
            self.bubble.show(for_ms=SHOW_MS)
        self._sched.after(WATCH_MS, self._watch_settings)

    def _shutdown(self, *_args):
        log("shutting down")
        self._sched.stop()
        self.bubble.close()
        sys.exit(0)


def main():
    log("feature started")
    CursorSizeApp().run()


if __name__ == "__main__":
    main()
