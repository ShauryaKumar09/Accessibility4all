"""Click Highlighter — flashes a ring at the cursor on every click, and can
switch Windows to its large high-contrast pointer.

Helps anyone who loses track of where a click landed (motor difficulties,
low vision, or just a fast-moving cursor on a big screen). Runs as its own
process; a pynput mouse listener drives a small always-on-top ring bubble
that pulses at the click point, sized bigger than the shipped default so
it's easy to spot without ever obscuring more than a moment.

See features/README.md.
"""

from __future__ import annotations

import signal
import sys
import threading
from pathlib import Path

FEATURE_DIR = Path(__file__).resolve().parent
ROOT = FEATURE_DIR.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pynput import mouse                                              # noqa: E402

from shared import console, settings_store as store                   # noqa: E402
from shared import webbubble as wb                                     # noqa: E402

console.configure_stdio()

FEATURE_ID = "click_highlight"

RING_SIZE = 64
FADE_MS = 260
SHOW_MS = 420          # how long the ring stays up after a click

BODY = """
<div class="ring" id="ring"></div>
"""
CSS = """
html, body { background: transparent; }
#stage { animation: none; }
.ring {
  width: %(SIZE)dpx; height: %(SIZE)dpx; border-radius: 50%%;
  border: 4px solid var(--accent);
  background: var(--accent-fill);
  box-shadow: 0 0 0 2px rgba(0,0,0,.25);
  transform: scale(.4); opacity: 0;
  transition: transform .18s cubic-bezier(.22,.61,.36,1),
              opacity .18s cubic-bezier(.22,.61,.36,1);
}
.ring.pulse { transform: scale(1); opacity: 1; }
""" % {"SIZE": RING_SIZE}
JS = """
function pulse() {
  const ring = document.getElementById('ring');
  ring.classList.remove('pulse');
  void ring.offsetWidth;
  ring.classList.add('pulse');
}
"""

def log(msg: str):
    console.safe_print(f"[click_highlight] {msg}", flush=True)


def load_settings() -> dict:
    return store.load(FEATURE_ID)


class ClickHighlightApp:
    def __init__(self):
        self.settings = load_settings()
        self._watcher = store.Watcher(FEATURE_ID)
        self._sched = wb.Scheduler(on_error=lambda e: log(f"timer failed: {e}"))
        self.bubble = wb.Bubble("Click Highlighter", BODY, RING_SIZE, RING_SIZE,
                                css=CSS, js=JS, hidden=True, sched=self._sched,
                                on_closed=self._sched.stop)
        self._listener: mouse.Listener | None = None
        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT, self._shutdown)

    def run(self):
        self.bubble.run(self._on_started)

    def _on_started(self):
        self._listener = mouse.Listener(on_click=self._on_click)
        self._listener.start()
        self._sched.after(700, self._watch_settings)

    def _on_click(self, x, y, button, pressed):
        if not pressed:
            return
        # pynput reports physical pixels on Windows; webview windows are
        # placed in logical points, same scaling colorblind_filter's OCR
        # click math already accounts for elsewhere in this app.
        sw, sh = wb.screen_size()
        try:
            import pyautogui
            px, py = pyautogui.size()
            x = x * sw / px
            y = y * sh / py
        except Exception:
            pass
        half = self.bubble.w // 2
        self.bubble.move(int(x - half), int(y - half))
        self.bubble.show(for_ms=SHOW_MS)
        self.bubble.call("pulse()")

    def _watch_settings(self):
        if self._watcher.changed():
            self.settings = load_settings()
        self._sched.after(700, self._watch_settings)

    def _shutdown(self, *_args):
        log("shutting down")
        if self._listener:
            self._listener.stop()
        self._sched.stop()
        self.bubble.close()
        sys.exit(0)


def main():
    log("feature started")
    ClickHighlightApp().run()


if __name__ == "__main__":
    main()
