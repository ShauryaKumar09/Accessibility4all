"""The Voice Control compact bar — a floating pill that sits over any app.

Why this one bubble is a web view and not a tkinter canvas
----------------------------------------------------------
The rest of the bubbles are drawn on a `tk.Canvas` (see `shared/ui_kit.py`).
That works when the window can hide its square corners against a dark desktop,
but the design calls for a *floating pill*: rounded ends, a real drop shadow,
and nothing boxy behind it. tkinter on macOS cannot do that — `wm attributes
-transparent` is accepted and then paints the whole window solid black (checked
on Tk 9.0.3), so the pill always sits inside a visible rectangle.

pywebview's Cocoa backend renders a genuinely transparent frameless window, so
the bar is HTML: the shape is a `border-radius: 999px` div, the waveform is CSS
keyframes running on the compositor at the display's refresh rate, and every
state change is a CSS transition rather than a Python frame loop. Python only
pushes state in (`set_state`, `set_level`); it never draws.

The colours and proportions come from
`design_handoff_a11y4all_redesign/design/feature_windows_full_panels.dc.html`
("Compact bar — floats over any app"), drawn about a quarter smaller than the
mock so it stays out of the way of real work (see the geometry block below),
and the CSS custom properties mirror `shared/ui_kit.py`'s `C` dict so the bar
matches the hub and the other bubbles.
"""

from __future__ import annotations

import heapq
import json
import queue
import sys
import threading
import time
from pathlib import Path

import webview

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.ui_kit import C  # noqa: E402

# ── geometry ─────────────────────────────────────────────────────────────────
# The design's compact bar is 460x80. It floats over the user's real work, so
# it is drawn here at roughly three-quarters of that: everything below is scaled
# down together EXCEPT the two accessibility floors, which the redesign fixes —
# the mic circle stays a 48px target and the line of text stays 17px.
MIC_D = 48                 # minimum touch target; never shrink this
TEXT_PX = 17               # minimum body copy; never shrink this either
PAD_V = 8                  # padding above/below the mic circle
PAD_L = 8
PAD_R = 18
GAP = 12                   # mic → waveform → text
WAVE_BARS, WAVE_BAR_W, WAVE_GAP, WAVE_H = 9, 4, 4, 24
WAVE_W = WAVE_BARS * WAVE_BAR_W + (WAVE_BARS - 1) * WAVE_GAP
CAP_W, CAP_H = 9, 15       # the mic glyph: capsule + base bar
BASE_W, BASE_H = 14, 3

BAR_H = MIC_D + PAD_V * 2                       # 64
BAR_W = 348                                     # the resting pill
BAR_MAX_W = 500            # long result lines may grow the pill this far
WIN_W = BAR_MAX_W + 60     # fixed window box: room for the widest pill + shadow
WIN_H = BAR_H + 30
BOTTOM_GAP = 72            # distance from the bottom of the screen

_CSS = """
:root {
  --bar-bg: %(CARD)s;
  --bar-border: %(BORDER_CTRL)s;
  --mic-bg: #1c202a;
  --mic-border: %(OFF_TRACK)s;
  --mic-fg: %(FG_BUBBLE)s;
  --mic-bg-on: %(ACCENT_FILL)s;
  --mic-border-on: %(ACCENT)s;
  --mic-fg-on: %(ACCENT_ON)s;
  --wave-off: %(OFF_TRACK)s;
  --wave-on: %(WAVE)s;
  --text: %(FG_BUBBLE)s;
  --ease: cubic-bezier(.22,.61,.36,1);
}
* { box-sizing: border-box; }
html, body {
  margin: 0; height: 100%%; background: transparent; overflow: hidden;
  -webkit-user-select: none; user-select: none; cursor: default;
}
body {
  display: flex; align-items: center; justify-content: center;
  font-family: -apple-system, 'Helvetica Neue', Helvetica, Arial, sans-serif;
}

/* the pill itself */
#bar {
  display: flex; align-items: center; gap: %(GAP)dpx;
  width: %(BAR_W)dpx; height: %(BAR_H)dpx;
  padding: %(PAD_V)dpx %(PAD_R)dpx %(PAD_V)dpx %(PAD_L)dpx;
  border-radius: 999px;
  background: var(--bar-bg);
  border: 1px solid var(--bar-border);
  box-shadow: 0 14px 34px rgba(0,0,0,.55), 0 2px 5px rgba(0,0,0,.4);
  transition: width .24s var(--ease), border-color .22s var(--ease);
  will-change: width;
}
#bar.listening { border-color: rgba(111,155,255,.45); }

/* the mic circle — also the press-and-hold fallback for the ` key */
#mic {
  flex: none; width: %(MIC_D)dpx; height: %(MIC_D)dpx; border-radius: 50%%;
  display: flex; flex-direction: column; align-items: center;
  justify-content: center; gap: 3px; cursor: pointer;
  background: var(--mic-bg); border: 2px solid var(--mic-border);
  transition: background .22s var(--ease), border-color .22s var(--ease),
              transform .18s var(--ease);
}
#mic:active { transform: scale(.94); }
#mic .cap  { width: %(CAP_W)dpx; height: %(CAP_H)dpx; border-radius: %(CAP_W)dpx;
             background: var(--mic-fg); }
#mic .base { width: %(BASE_W)dpx; height: %(BASE_H)dpx; border-radius: 2px;
             background: var(--mic-fg); }
#mic .cap, #mic .base { transition: background .22s var(--ease); }
#bar.listening #mic { background: var(--mic-bg-on); border-color: var(--mic-border-on); }
#bar.listening #mic .cap, #bar.listening #mic .base { background: var(--mic-fg-on); }

/* the waveform: nine bars, each on its own cycle */
#wave {
  flex: none; display: flex; align-items: center; gap: %(WAVE_GAP)dpx;
  height: %(WAVE_H)dpx;
  transform: scaleY(var(--level, 1));
  transition: transform .09s linear;   /* follows the real mic level */
  will-change: transform;
}
#wave i {
  display: block; width: %(WAVE_BAR_W)dpx; height: %(WAVE_H)dpx;
  border-radius: %(WAVE_BAR_W)dpx;
  background: var(--wave-off); opacity: .5;
  transform: scaleY(.16); transform-origin: center;
  transition: transform .3s var(--ease), background .25s var(--ease),
              opacity .25s var(--ease);
  will-change: transform;
}
#bar.listening #wave i {
  background: var(--wave-on); opacity: 1;
  animation: fw-bar var(--dur) ease-in-out var(--delay) infinite both;
}
@keyframes fw-bar {
  0%%, 100%% { transform: scaleY(.2); }
  50%%       { transform: scaleY(1); }
}

/* the one line of live state */
#text {
  flex: 1; min-width: 0; font-size: %(TEXT_PX)dpx; line-height: 1.2;
  color: var(--text); white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis;
  transition: color .22s var(--ease);
}
#measure {
  position: absolute; visibility: hidden; white-space: nowrap;
  font-size: %(TEXT_PX)dpx; left: -9999px; top: -9999px;
}
"""

_JS = """
const bar = document.getElementById('bar');
const wave = document.getElementById('wave');
const textEl = document.getElementById('text');
const measure = document.getElementById('measure');

// the fixed part of the pill: paddings + mic + gaps + waveform
const CHROME = %(CHROME)d;
const BASE_W = %(BAR_W)d, MAX_W = %(BAR_MAX_W)d;

// Each bar gets its own period and start offset, so the waveform reads as
// speech rather than as one bar mirrored nine times (design: fw-bar).
wave.querySelectorAll('i').forEach((el, i) => {
  el.style.setProperty('--dur', (0.62 + (i %% 5) * 0.11).toFixed(2) + 's');
  el.style.setProperty('--delay', (i * 0.055).toFixed(3) + 's');
});

// The off-screen span must use the *exact* font the line is drawn with, or its
// measurement comes up a few px short and the text ellipsises inside a pill
// that looks like it had room.
measure.style.font = getComputedStyle(textEl).font;

function setState(state, text, color) {
  bar.classList.toggle('listening', state === 'listening');
  textEl.textContent = text;
  textEl.style.color = color;
  // grow the pill only when the line genuinely needs the room; the width is an
  // explicit px value so the CSS transition has something to animate between
  measure.textContent = text;
  const want = Math.ceil(CHROME + measure.offsetWidth) + 4;
  bar.style.width = Math.min(MAX_W, Math.max(BASE_W, want)) + 'px';
}

// 0..1 mic energy. The floor keeps a quiet room alive rather than flat, and
// full-scale speech reaches the design's full-height bars.
function setLevel(level) {
  wave.style.setProperty('--level', (0.5 + 0.5 * level).toFixed(3));
}
"""

_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>%(css)s</style></head>
<body>
  <div id="bar">
    <div id="mic"><div class="cap"></div><div class="base"></div></div>
    <div id="wave"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i></div>
    <div id="text"></div>
  </div>
  <span id="measure"></span>
  <script>%(js)s</script>
</body></html>
"""


def _page() -> str:
    tokens = dict(
        C, BAR_W=BAR_W, BAR_H=BAR_H, BAR_MAX_W=BAR_MAX_W, MIC_D=MIC_D,
        PAD_V=PAD_V, PAD_L=PAD_L, PAD_R=PAD_R, GAP=GAP, TEXT_PX=TEXT_PX,
        WAVE_GAP=WAVE_GAP, WAVE_BAR_W=WAVE_BAR_W, WAVE_H=WAVE_H,
        CAP_W=CAP_W, CAP_H=CAP_H, BASE_W=BASE_W, BASE_H=BASE_H,
        CHROME=PAD_L + MIC_D + GAP + WAVE_W + GAP + PAD_R + 2,  # +2: the pill's border
    )
    return _HTML % {"css": _CSS % tokens, "js": _JS % tokens}


class Scheduler:
    """`after()` / `after_cancel()` without a tkinter main loop.

    The old bubble leaned on Tk's event loop to serialise every timer and every
    worker-thread hand-off onto one thread. The web view has its own loop that
    we don't get to post into, so this replaces it: one daemon thread runs due
    callbacks in order, which keeps the existing single-threaded assumptions in
    `main.py` (poll loops, the idle reset timer, the UI queue drain) intact.
    """

    def __init__(self, on_error=None):
        self._heap: list[tuple[float, int, object]] = []
        self._cancelled: set[int] = set()
        self._next_id = 0
        self._cv = threading.Condition()
        self._running = True
        self._on_error = on_error
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def after(self, ms: float, fn) -> int:
        with self._cv:
            self._next_id += 1
            tid = self._next_id
            heapq.heappush(self._heap, (time.monotonic() + ms / 1000.0, tid, fn))
            self._cv.notify()
            return tid

    def after_cancel(self, tid: int | None):
        if tid is None:
            return
        with self._cv:
            self._cancelled.add(tid)

    def stop(self):
        with self._cv:
            self._running = False
            self._cv.notify()

    def _run(self):
        while True:
            with self._cv:
                if not self._running:
                    return
                if not self._heap:
                    self._cv.wait(0.25)
                    continue
                due, tid, fn = self._heap[0]
                wait = due - time.monotonic()
                if wait > 0:
                    self._cv.wait(min(wait, 0.25))
                    continue
                heapq.heappop(self._heap)
                if tid in self._cancelled:
                    self._cancelled.discard(tid)
                    continue
            try:
                fn()
            except Exception as e:                      # never kill the loop
                if self._on_error:
                    self._on_error(e)


class _JsApi:
    """What the page can call. Press/release only — the bar has no settings."""

    def __init__(self, events: queue.Queue):
        self._events = events

    def mic_down(self):
        self._events.put("down")

    def mic_up(self):
        self._events.put("up")


class Bar:
    """The floating pill. `run()` blocks the main thread until the bar closes.

    Everything else on here is safe to call from any thread: state changes are
    handed to the web view as one `evaluate_js` call, which pywebview marshals
    onto its own UI thread.
    """

    def __init__(self, events: queue.Queue, screen_size: tuple[int, int],
                 on_closed=None):
        sw, sh = screen_size
        self.x = max(0, (sw - WIN_W) // 2)
        self.y = max(0, sh - WIN_H - BOTTOM_GAP)
        self._on_closed = on_closed
        self._ready = threading.Event()
        self._last: tuple[str, str, str] | None = None
        self._last_level = -1.0
        self.window = webview.create_window(
            "Voice Control", html=_page(),
            width=WIN_W, height=WIN_H, x=self.x, y=self.y,
            frameless=True, transparent=True, on_top=True, resizable=False,
            easy_drag=False, background_color="#000000",
            js_api=_JsApi(events),
        )
        self.window.events.closed += self._closed

    # ── lifecycle ──
    def run(self, on_started=None):
        """Start the GUI loop. Blocks; `on_started` runs on a worker thread."""
        def _boot():
            # the press-and-hold binding lives here rather than in the page so a
            # slow first paint can't drop it
            # release is watched on the window, not the circle, so dragging
            # off the mic while holding still ends the recording
            self.window.evaluate_js(
                "let held = false;"
                "document.getElementById('mic').addEventListener('mousedown',"
                " () => { held = true; window.pywebview.api.mic_down(); });"
                "window.addEventListener('mouseup',"
                " () => { if (held) { held = false; window.pywebview.api.mic_up(); } });"
            )
            self._ready.set()
            if on_started:
                on_started()
        webview.start(_boot, private_mode=True)

    def _closed(self):
        self._ready.clear()
        if self._on_closed:
            self._on_closed()

    def close(self):
        try:
            self.window.destroy()
        except Exception:
            pass

    # ── state in ──
    def set_state(self, state: str, text: str, color: str):
        payload = (state, text, color)
        if payload == self._last:
            return
        self._last = payload
        self._call(f"setState({json.dumps(state)}, {json.dumps(text)}, "
                   f"{json.dumps(color)})")

    def set_level(self, level: float):
        level = max(0.0, min(1.0, level))
        if abs(level - self._last_level) < 0.01:
            return
        self._last_level = level
        self._call(f"setLevel({level:.3f})")

    def rect(self) -> dict:
        """Where the pill actually is, for `feature_bus` presence."""
        return {"x": self.x + (WIN_W - BAR_W) // 2,
                "y": self.y + (WIN_H - BAR_H) // 2,
                "w": BAR_W, "h": BAR_H}

    def _call(self, js: str):
        if not self._ready.is_set():
            return
        try:
            self.window.evaluate_js(js)
        except Exception:
            pass          # the window is going away; state updates don't matter
