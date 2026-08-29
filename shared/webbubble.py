"""Floating desktop bubbles, drawn as transparent web views.

Every running feature shows one small always-on-top window with live state and
at most one big action — the design's "the desktop only gets a bubble". This
module is the shared machinery for those windows.

Why not `tk.Canvas` (which `shared/ui_kit.py` still draws the hub-side widgets
with): a bubble has to *float*. It is a capsule or a rounded card with a drop
shadow sitting over the user's real work, so everything outside its shape must
be see-through. tkinter on macOS cannot do that — `wm attributes -transparent`
is accepted and then paints the whole window solid black (checked on Tk 9.0.3),
which leaves every pill inside a visible rectangle. pywebview's Cocoa backend
renders a genuinely transparent frameless window, so the bubbles are HTML: the
shape is `border-radius`, the motion is CSS transitions and keyframes running on
the compositor, and Python only pushes state in.

What a feature gets from here:

- `Bubble` — one transparent, frameless, always-on-top window. Give it a body,
  a bit of CSS and JS, and a size; drive it with `call()`, `show()`, `hide()`
  and `move()`. `run()` blocks the main thread the way a Tk main loop did.
- `Scheduler` — `after()` / `after_cancel()` without a Tk main loop, so the
  poll loops and timers features already had keep working unchanged.
- `BASE_CSS` — the shared look: tokens generated from `shared/ui_kit.py`'s `C`
  dict (so bubbles, hub and any remaining canvas widget stay one product), plus
  the handful of pieces every bubble is built from — `.pill`, `.card`, `.dot`,
  `.label`, `.chip`, `.circle`, `.track`, `.btn`.

Sizes here are logical points, which is what the design's px values mean.
"""

from __future__ import annotations

import heapq
import json
import sys
import threading
import time
from pathlib import Path

import webview

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import feature_bus  # noqa: E402
from shared.ui_kit import C    # noqa: E402

# Room left around a bubble inside its window for the drop shadow. The window
# is transparent, so this is invisible — but it is real estate that swallows
# clicks, which is why bubbles are sized to their content and no larger.
SHADOW_PAD_X = 40
SHADOW_PAD_Y = 30
BOTTOM_GAP = 72          # a bottom-centred bubble's distance from the edge
STACK_GAP = 12           # between two bubbles that are up at the same time

# Bottom-up order for bubbles that share the bottom-centre spot. A feature that
# is off leaves no gap, because the stack is built from who is actually present
# (feature_bus), not from this list alone.
# (tone_reader is absent on purpose: its card appears next to your selection,
# not in the bottom-centre pile.)
STACK_ORDER = ["voice_control", "page_reader", "focus_mode",
               "dyslexia_font", "colorblind_filter"]

# Shared timing. One easing curve and one duration family keeps every bubble
# moving the same way.
EASE = "cubic-bezier(.22,.61,.36,1)"
FADE_MS = 200            # how long a bubble takes to fade itself out

BASE_CSS = """
:root {
  --bubble: %(BUBBLE)s;
  --card: %(CARD)s;
  --border: %(BORDER_CTRL)s;
  --chip-bg: %(WARM_BG)s;
  --chip-border: %(WARM_BORDER)s;
  --chip-fg: %(WARM_TEXT)s;
  --fg: %(FG)s;
  --fg-bubble: %(FG_BUBBLE)s;
  --fg-muted: %(FG_MUTED)s;
  --accent: %(ACCENT)s;
  --accent-fill: %(ACCENT_FILL)s;
  --accent-on: %(ACCENT_ON)s;
  --on: %(ON)s;
  --off: %(OFF_TRACK)s;
  --track: %(TRACK_BG)s;
  --stop: %(STOP_BORDER)s;
  --inset: %(INSET)s;
  --ease: %(EASE)s;
}
* { box-sizing: border-box; }
html, body {
  margin: 0; height: 100%%; background: transparent; overflow: hidden;
  -webkit-user-select: none; user-select: none; cursor: default;
  font-family: -apple-system, 'Helvetica Neue', Helvetica, Arial, sans-serif;
  color: var(--fg-bubble);
}
body { display: flex; align-items: center; justify-content: center; }

/* Every bubble enters the same way: up, in, and settled. It never enters
   half-drawn, because the window is only shown once the page has painted. */
#stage {
  animation: bb-in .26s var(--ease) both;
  transition: opacity %(FADE_MS)dms var(--ease),
              transform %(FADE_MS)dms var(--ease);
}
#stage.leaving { opacity: 0; transform: translateY(6px) scale(.97); }
@keyframes bb-in {
  from { opacity: 0; transform: translateY(10px) scale(.96); }
  to   { opacity: 1; transform: none; }
}

/* ── the pieces bubbles are built from ── */
.pill {
  display: flex; align-items: center;
  border-radius: 999px;
  background: var(--bubble);
  border: 1px solid var(--border);
  box-shadow: 0 14px 34px rgba(0,0,0,.55), 0 2px 5px rgba(0,0,0,.4);
}
.card {
  display: flex; flex-direction: column;
  border-radius: 16px;
  background: var(--bubble);
  border: 1px solid var(--border);
  box-shadow: 0 18px 44px rgba(0,0,0,.6), 0 2px 6px rgba(0,0,0,.45);
}
.dot {
  flex: none; width: 12px; height: 12px; border-radius: 50%%;
  background: var(--dot, var(--on));
  transition: background .25s var(--ease);
}
.label {
  font-size: 17px; line-height: 1.2; color: var(--fg-bubble);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  transition: color .22s var(--ease);
}
.chip {
  align-self: flex-start; font-size: 17px; font-weight: 600;
  padding: 6px 14px; border-radius: 999px;
  background: var(--chip-bg); border: 1px solid var(--chip-border);
  color: var(--chip-fg);
}
.circle {
  flex: none; width: 48px; height: 48px; border-radius: 50%%;
  display: flex; align-items: center; justify-content: center;
  cursor: pointer;
  background: var(--accent-fill); border: 2px solid var(--accent);
  transition: background .22s var(--ease), border-color .22s var(--ease),
              transform .18s var(--ease);
}
.circle:active { transform: scale(.94); }
.track {
  height: 5px; border-radius: 3px; background: var(--track); overflow: hidden;
}
.track > i {
  display: block; height: 5px; border-radius: 3px; background: var(--accent);
  width: 0; transition: width .3s var(--ease);
}
.btn {
  height: 52px; display: flex; align-items: center; justify-content: center;
  border-radius: 12px; border: 2px solid var(--border);
  background: var(--inset); font-size: 18px; color: var(--fg);
  cursor: pointer;
  transition: border-color .18s var(--ease), background .18s var(--ease),
              transform .18s var(--ease);
}
.btn:hover { border-color: var(--accent); }
.btn:active { transform: scale(.98); }
"""


def page(body: str, css: str = "", js: str = "") -> str:
    """Wrap a bubble's markup in the shared stylesheet, stage and helpers."""
    tokens = dict(C, EASE=EASE, FADE_MS=FADE_MS)
    return (
        "<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\"><style>"
        + (BASE_CSS % tokens) + css
        + "</style></head><body><div id=\"stage\">" + body + "</div>"
        + "<script>" + STAGE_JS + js + "</script>"
        + "</body></html>"
    )


def _dpi_scale() -> float:
    """How many physical pixels Windows paints per logical point.

    1.0 at 100% display scaling, 1.25 at 125%, and so on.
    """
    if sys.platform != "win32":
        return 1.0                # Cocoa already reports points
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hdc = user32.GetDC(0)
        try:
            LOGPIXELSX = 88
            dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, LOGPIXELSX)
        finally:
            user32.ReleaseDC(0, hdc)
        return (dpi / 96.0) if dpi else 1.0
    except Exception:
        return 1.0


def screen_size() -> tuple[int, int]:
    """The main display in logical points — what window coordinates are in.

    `webview.screens` reports PHYSICAL pixels, while window positions and
    sizes are logical points. On a display scaled above 100% the two differ,
    and placing a bubble from the physical number pushes it past the edge —
    at 125% a bottom-right card landed a third of the way off the screen
    with its text cut off. Divide the physical size back down to points.
    """
    try:
        s = webview.screens[0]
        scale = _dpi_scale()
        return int(s.width / scale), int(s.height / scale)
    except Exception:
        return 1440, 900


class Scheduler:
    """`after()` / `after_cancel()` without a tkinter main loop.

    Features used to lean on Tk's event loop to serialise every timer and every
    worker-thread hand-off onto one thread. The web view has its own loop we
    can't post into, so this replaces it: one daemon thread runs due callbacks
    in order, which keeps those single-threaded assumptions (poll loops, hide
    timers, countdown ticks) intact.
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


class Bubble:
    """One floating window.

    `run()` blocks the main thread until the bubble closes; everything else is
    safe to call from any thread, because a state change is one `evaluate_js`
    (about a millisecond) which pywebview marshals onto its own UI thread.
    """

    def __init__(self, title: str, body: str, width: int, height: int,
                 css: str = "", js: str = "", api=None, hidden: bool = False,
                 on_closed=None, sched: "Scheduler | None" = None):
        self.w = width + SHADOW_PAD_X
        self.h = height + SHADOW_PAD_Y
        self.content_w, self.content_h = width, height
        sw, sh = screen_size()
        self.x = max(0, (sw - self.w) // 2)
        self.y = max(0, sh - self.h - BOTTOM_GAP)
        self._visible = not hidden
        self._ready = threading.Event()
        self._on_closed = on_closed
        self._sched = sched
        self._hide_at: int | None = None
        self.window = webview.create_window(
            title, html=page(body, css, js),
            width=self.w, height=self.h, x=self.x, y=self.y,
            frameless=True, transparent=True, on_top=True, resizable=False,
            easy_drag=False, background_color="#000000", hidden=hidden,
            js_api=api,
        )
        self.window.events.closed += self._closed

    # ── lifecycle ──
    def run(self, on_started=None):
        """Start the GUI loop. Blocks; `on_started` runs on a worker thread."""
        def _boot():
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

    # ── showing and hiding ──
    def show(self, for_ms: int | None = None):
        """Bring the bubble back, replaying its entrance.

        `for_ms` hides it again after that long — the pattern the confirmation
        bubbles use, where a pill says one thing and then gets out of the way.
        """
        self._cancel_hide()
        self.call("enter()")
        if not self._visible:
            self._visible = True
            try:
                self.window.show()
            except Exception:
                pass
        self._raise_above_everything()
        if for_ms and self._sched:
            self._hide_at = self._sched.after(for_ms, self.hide)

    def _raise_above_everything(self):
        """Put this bubble back on top of the other always-on-top windows.

        `on_top=True` at creation is not enough. Windows orders topmost
        windows by which was most recently made topmost, so a bubble created
        early loses to anything topmost'd after it — the hub itself, or
        another feature's bubble — and ends up buried, which is exactly what
        users saw.

        The flip to False and back is deliberate: WinForms' `TopMost` setter
        short-circuits when the value has not changed, so re-setting True on
        a window that already believes it is topmost does nothing at all.
        Changing it twice forces the real z-order call. pywebview implements
        `on_top` on both the Windows and macOS backends, so this needs no
        platform branch.
        """
        try:
            self.window.on_top = False
            self.window.on_top = True
        except Exception:
            pass          # the window is going away; z-order no longer matters

    def hide(self):
        """Fade out, then take the window away once the fade has played."""
        self._cancel_hide()
        if not self._visible:
            return
        self.call("leave()")
        if self._sched:
            self._sched.after(FADE_MS + 20, self._hide_now)
        else:
            self._hide_now()

    def _hide_now(self):
        self._visible = False
        try:
            self.window.hide()
        except Exception:
            pass

    def _cancel_hide(self):
        if self._hide_at is not None and self._sched:
            self._sched.after_cancel(self._hide_at)
        self._hide_at = None

    @property
    def visible(self) -> bool:
        return self._visible

    # ── placing ──
    def move(self, x: int, y: int):
        self.x, self.y = int(x), int(y)
        try:
            self.window.move(self.x, self.y)
        except Exception:
            pass

    def place_bottom_center(self, bottom_gap: int = BOTTOM_GAP):
        sw, sh = screen_size()
        self.move(max(0, (sw - self.w) // 2), max(0, sh - self.h - bottom_gap))

    def place_bottom_right(self, bottom_gap: int = BOTTOM_GAP,
                           side_gap: int = BOTTOM_GAP):
        """Park the bubble in the bottom-right corner and leave it there.

        For a card the user reads rather than glances at: a fixed corner is
        predictable and never lands on top of the thing they were reading,
        which is what following the pointer did.
        """
        sw, sh = screen_size()
        self.move(max(0, sw - self.w - side_gap), max(0, sh - self.h - bottom_gap))

    def place_stacked(self, feature_id: str):
        """Bottom-centre, but above any bubble that is already down there.

        Several features can be on at once, and every one of them wants the
        same spot. Presence (`feature_bus`) says which bubbles are actually up
        and where their drawn shapes are, so each new one sits on top of the
        pile rather than on top of another bubble.
        """
        sw, sh = screen_size()
        top = sh - BOTTOM_GAP - self.content_h          # the bottom slot
        presence = feature_bus.load_presence()
        for other in STACK_ORDER:
            if other == feature_id:
                break
            info = presence.get(other)
            if not info or not feature_bus.is_feature_running(other):
                continue
            if "started_at" not in info:
                # Written by a build before presence carried a timestamp, or
                # left behind by a process that died without cleaning up. The
                # PID check above can be fooled by PID reuse, so an entry we
                # cannot date is not trustworthy enough to stack against.
                continue
            win = info.get("window") or {}
            if "y" in win and "h" in win:
                top = min(top, win["y"] - STACK_GAP - self.content_h)
        x = max(0, (sw - self.w) // 2)
        y = max(0, top - (self.h - self.content_h) // 2)
        self.move(x, y)

    def place_near(self, x: int, y: int, below: int = 18):
        """Put the bubble under a point (a text selection, the pointer), kept
        fully on screen."""
        sw, sh = screen_size()
        self.move(min(max(0, x - self.w // 2), sw - self.w),
                  min(max(0, y + below), sh - self.h))

    def resize_content(self, width: int, height: int):
        """Fit the window to a bubble whose content just changed size.

        Only the card-shaped bubbles need this — a pill is a fixed size, and
        resizing a window every frame is what made the old canvas bubbles look
        laggy. Call it while the bubble is hidden, then `show()`.
        """
        self.content_w, self.content_h = int(width), int(height)
        self.w = self.content_w + SHADOW_PAD_X
        self.h = self.content_h + SHADOW_PAD_Y
        try:
            self.window.resize(self.w, self.h)
        except Exception:
            pass

    def measure(self, js: str, default=0):
        """Read a number back out of the page (an element's height, usually)."""
        if not self._ready.is_set():
            return default
        try:
            value = self.window.evaluate_js(js)
        except Exception:
            return default
        return default if value is None else value

    def rect(self) -> dict:
        """Where the drawn bubble is, for `feature_bus` presence."""
        return {"x": self.x + (self.w - self.content_w) // 2,
                "y": self.y + (self.h - self.content_h) // 2,
                "w": self.content_w, "h": self.content_h}

    # ── talking to the page ──
    def call(self, js: str):
        if not self._ready.is_set():
            return
        try:
            self.window.evaluate_js(js)
        except Exception:
            pass          # the window is going away; state updates don't matter

    def set_text(self, element_id: str, text: str):
        self.call(f"document.getElementById({json.dumps(element_id)})"
                  f".textContent = {json.dumps(text)}")

    def set_style(self, element_id: str, prop: str, value: str):
        self.call(f"document.getElementById({json.dumps(element_id)})"
                  f".style.setProperty({json.dumps(prop)}, {json.dumps(value)})")

    def set_class(self, element_id: str, name: str, on: bool):
        self.call(f"document.getElementById({json.dumps(element_id)})"
                  f".classList.toggle({json.dumps(name)}, {str(bool(on)).lower()})")


# Every bubble page gets these two, so `show()` / `hide()` can replay the
# entrance and play an exit without each feature writing the same three lines.
STAGE_JS = """
const stage = document.getElementById('stage');
function enter() {
  stage.classList.remove('leaving');
  stage.style.animation = 'none';
  void stage.offsetWidth;          /* restart the entrance keyframes */
  stage.style.animation = '';
}
function leave() { stage.classList.add('leaving'); }
"""
