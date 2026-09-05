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

Sizes here are CSS pixels, which is what the design's px values mean. On
Windows the window is then made `devicePixelRatio` times bigger in real pixels
(`Bubble._fit_to_scale`) so the page gets the room the design assumed, and
every placement number goes through `Bubble._px` for the same reason.

Windows draws these differently from macOS, and the difference is not
cosmetic. Cocoa composites real per-pixel alpha, so there the window is
transparent, the bubble floats inside it with its CSS drop shadow, and the
padding around it (`SHADOW_PAD_*`) is invisible. WebView2 does not: its
surface was measured painting opaque no matter what it is asked for, so on
Windows the window IS the bubble — no padding, the bubble stretched edge to
edge, its rounded corners cut out of the window by DWM, and DWM's own shadow
in place of the CSS one. `page()` and `Bubble._apply_shape` carry the
detail, including the several things that look like they should work and put
the box straight back.
"""

from __future__ import annotations

import heapq
import json
import os
import sys
import threading
import time
from pathlib import Path

if sys.platform == "win32":
    # THE fix for the opaque box behind every bubble on Windows.
    #
    # pywebview asks for transparency by setting the WebView2 control's
    # `DefaultBackgroundColor` to `Color.Transparent`. That property is known
    # to be unreliable — it reads back as transparent (`A=0`) while the
    # surface still composites opaque, which is exactly what was measured
    # here: raw `GetPixel` on the window's own DC returned Chromium's default
    # page background (#121212), and the bubble sat on a rectangle of it.
    # Microsoft documents an environment variable as the way to set this
    # before the WebView2 environment is created, precisely because the API
    # path misbehaves (it is also their documented fix for the white flash
    # the API leaves before it takes effect).
    #
    # Format is AARRGGBB, and 00 alpha means transparent — so 00FFFFFF is a
    # fully transparent backdrop. This must be set before ANY window is
    # created (hence module import time), and only if the user hasn't set
    # their own value.
    os.environ.setdefault("WEBVIEW2_DEFAULT_BACKGROUND_COLOR", "00FFFFFF")

    # Windows' default timer resolution is ~15.6ms, so a 16ms sleep actually
    # takes about 31 — which turns a resize animation into six visible steps.
    # Asking for 1ms makes the scheduler's frames land when they are meant to.
    try:
        import ctypes

        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass


import webview

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import feature_bus  # noqa: E402
from shared.ui_kit import C    # noqa: E402

# Room left around a bubble inside its window for the drop shadow.
#
# macOS gets it: the window is genuinely transparent there, so the padding is
# invisible and the shadow can spill into it.
#
# Windows gets none, because the window is NOT transparent there — see
# `page()`. Any padding would be opaque background, which is the box. The
# bubble is the window instead, its rounded corners cut out of the window
# itself (`Bubble._apply_shape`), and the CSS drop shadow with it — DWM
# draws its own shadow around the window instead.
_WIN = sys.platform == "win32"
SHADOW_PAD_X = 0 if _WIN else 34
SHADOW_PAD_Y = 0 if _WIN else 40
BOTTOM_GAP = 10          # a bottom-centred bubble's gap above the taskbar
STACK_GAP = 12           # between two bubbles that are up at the same time

# Bottom-up order for bubbles that share the bottom-centre spot. A feature that
# is off leaves no gap, because the stack is built from who is actually present
# (feature_bus), not from this list alone.
# (tone_reader is absent on purpose: its card appears next to your selection,
# not in the bottom-centre pile.)
STACK_ORDER = ["voice_control", "page_reader", "focus_mode",
               "colorblind_filter"]

# Shared timing. One easing curve and one duration family keeps every bubble
# moving the same way.
# What the window is filled with before the page paints. The bubble's own
# colour, so the moment between the window appearing and the page rendering
# looks like the bubble rather than a flash of something else.
BACKDROP_KEY = C["BUBBLE"]

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
  /* `transparent` here is only correct on macOS, where Cocoa composites real
     per-pixel alpha. On Windows it is a trap: the page ends up painting
     Chromium's own default page background (measured: #121212) because
     WebView2's `DefaultBackgroundColor` is ignored by the runtime, and an
     opaque #121212 rectangle is exactly the "box" around the bubble. The
     Windows build therefore paints the colour key itself, in CSS, where
     Chromium cannot ignore it — see `page()`, which swaps this per platform. */
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
  /* `will-change` only — deliberately NOT `translateZ(0)` /
     `backface-visibility`. Those force WebView2 to allocate an OPAQUE backing
     layer for the stage, which paints a black rectangle over the window's
     keyed-out background and puts the "white box" back as a black one. The
     opacity/transform transitions below are already compositor-driven. */
  will-change: opacity, transform;
}
/* Beat the still-attached `bb-in` animation, which otherwise holds the
   entrance's final transform and leaves the exit with nothing to animate. */
#stage.leaving {
  opacity: 0;
  transform: translateY(6px) scale(.97);
  animation: none;
}
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
  box-shadow: 0 8px 20px rgba(0,0,0,.45), 0 1px 3px rgba(0,0,0,.35);
}
.card {
  display: flex; flex-direction: column;
  border-radius: 12px;
  background: var(--bubble);
  border: 1px solid var(--border);
  box-shadow: 0 10px 26px rgba(0,0,0,.5), 0 1px 4px rgba(0,0,0,.4);
}
.dot {
  flex: none; width: 9px; height: 9px; border-radius: 50%%;
  background: var(--dot, var(--on));
  transition: background .25s var(--ease);
}
.label {
  font-size: 14px; line-height: 1.2; color: var(--fg-bubble);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  transition: color .22s var(--ease);
}
.chip {
  align-self: flex-start; font-size: 13px; font-weight: 600;
  padding: 4px 10px; border-radius: 999px;
  background: var(--chip-bg); border: 1px solid var(--chip-border);
  color: var(--chip-fg);
}
.circle {
  flex: none; width: 34px; height: 34px; border-radius: 50%%;
  display: flex; align-items: center; justify-content: center;
  cursor: pointer;
  background: var(--accent-fill); border: 2px solid var(--accent);
  transition: background .22s var(--ease), border-color .22s var(--ease),
              transform .18s var(--ease);
}
.circle:active { transform: scale(.94); }
.track {
  height: 4px; border-radius: 2px; background: var(--track); overflow: hidden;
}
.track > i {
  display: block; height: 4px; border-radius: 2px; background: var(--accent);
  width: 0; transition: width .3s var(--ease);
}
.btn {
  height: 38px; display: flex; align-items: center; justify-content: center;
  border-radius: 10px; border: 1px solid var(--border);
  background: var(--inset); font-size: 14px; color: var(--fg);
  cursor: pointer;
  transition: border-color .18s var(--ease), background .18s var(--ease),
              transform .18s var(--ease);
}
.btn:hover { border-color: var(--accent); }
.btn:active { transform: scale(.98); }
"""


def page(body: str, css: str = "", js: str = "") -> str:
    """Wrap a bubble's markup in the shared stylesheet, stage and helpers."""
    tokens = dict(C, EASE=EASE, FADE_MS=FADE_MS, BACKDROP_KEY=BACKDROP_KEY)
    # How the window stops being a rectangle differs by platform.
    #
    # macOS composites real per-pixel alpha: a page with no background is a
    # window with no background, so the bubble floats with its drop shadow and
    # the padding around it is invisible.
    #
    # Windows cannot do that here. WebView2 draws through DirectComposition,
    # and its surface was measured painting opaque whatever it is asked for —
    # `DefaultBackgroundColor = Transparent`, the documented
    # `WEBVIEW2_DEFAULT_BACKGROUND_COLOR` variable, a magenta page keyed out
    # with `TransparencyKey`, the same key applied by hand with
    # `SetLayeredWindowAttributes`: every one of them left a rectangle behind
    # the bubble, and the keying attempts made it a magenta rectangle rather
    # than none. So on Windows the window IS the bubble: no padding, the
    # bubble's own surface stretched edge to edge, and the rounded corners cut
    # cut out of the window itself (see `Bubble._apply_shape`). There is nothing
    # left over to paint a box with.
    if sys.platform == "win32":
        body_css = (
            "html, body { background: transparent !important; }"
            "#stage { width: 100%; height: 100%; }"
            # The bubble fills the window, so its shadow would have nowhere
            # to fall and its border would be half-clipped by the window edge.
            # The radius matches DWM's rounding (`Bubble._apply_shape`): a
            # capsule's 999px corners would leave wedges of bare window either
            # side of the bubble, which is the box in miniature.
            "#stage > * { width: 100% !important; height: 100% !important;"
            " box-shadow: none !important; border: 0 !important;"
            " border-radius: 8px !important; }"
        )
    else:
        # macOS: the window is genuinely transparent and has room around the
        # bubble for its shadow, so the bubble is sized here rather than by the
        # window — `bbSize()` moves these two variables and the transition runs
        # on the compositor. `#stage > *` fills the stage so a feature's own
        # `height: 100%` means the same thing on both platforms.
        body_css = (
            "html, body { background: transparent !important; }"
            "#stage { width: var(--bw, auto); height: var(--bh, auto);"
            " transition: width .22s var(--ease), height .22s var(--ease); }"
            "#stage > * { width: 100%; height: 100%; }"
        )
    return (
        "<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\"><style>"
        + (BASE_CSS % tokens) + css + body_css
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


_REAL_WORK_AREA: tuple[int, int, int, int] | None = None


def screen_size() -> tuple[int, int]:
    """The main display in logical points — what window coordinates are in.

    `webview.screens` reports PHYSICAL pixels, while window positions and
    sizes are logical points. On a display scaled above 100% the two differ,
    and placing a bubble from the physical number pushes it past the edge —
    at 125% a bottom-right card landed a third of the way off the screen
    with its text cut off. Divide the physical size back down to points.
    """
    if sys.platform == "win32":
        try:
            import ctypes

            user32 = ctypes.windll.user32
            hdc = user32.GetDC(0)
            try:
                # True pixels, the space windows are actually placed in — the
                # virtualised GetSystemMetrics answer is 1536x864 on a
                # 1920x1080 display at 125%, which put every edge-anchored
                # bubble short of the edge. See `_real_desktop`.
                phys = (ctypes.windll.gdi32.GetDeviceCaps(hdc, 118),
                        ctypes.windll.gdi32.GetDeviceCaps(hdc, 117))
            finally:
                user32.ReleaseDC(0, hdc)
            if all(phys):
                return phys
        except Exception:
            pass
    try:
        s = webview.screens[0]
        scale = _dpi_scale()
        return int(s.width / scale), int(s.height / scale)
    except Exception:
        return 1440, 900


def _real_desktop() -> tuple[int, int, int, int] | None:
    """The primary display's work area in TRUE pixels, DPI scaling included.

    Everything Windows tells a DPI-unaware process is virtualised: on a
    1920x1080 display at 125% both `GetSystemMetrics` and `SPI_GETWORKAREA`
    answer 1536x864, while `SetWindowPos` places windows in the real 1920x1080
    space. Placing a bubble from the virtualised number therefore leaves it
    short of the edge it was anchored to — Focus Mode's collapsed circle sat
    365px above the taskbar, floating in the middle of the screen, instead of
    10px above it in the bottom-left corner.

    Asking a *different* process for the metrics is what gets the real ones
    without making this process DPI-aware, which would change how every window
    is sized (see `_fit_to_scale`). Returns (left, top, right, bottom) or None.
    """
    if sys.platform != "win32":
        return None
    global _REAL_WORK_AREA
    if _REAL_WORK_AREA is not None:
        return _REAL_WORK_AREA
    try:
        import ctypes
        from ctypes import wintypes

        # PROCESS_PER_MONITOR_DPI_AWARE in a throwaway query is not possible
        # once a process has a DPI context, so read the monitor directly:
        # MonitorFromWindow + GetMonitorInfo report virtualised values too,
        # but the physical mode of the display does not lie.
        user32 = ctypes.windll.user32
        hdc = user32.GetDC(0)
        try:
            # DESKTOPHORZRES/VERTRES are the true pixel dimensions even when
            # the process is DPI-unaware — that is exactly what they are for.
            DESKTOPVERTRES, DESKTOPHORZRES = 117, 118
            phys_w = ctypes.windll.gdi32.GetDeviceCaps(hdc, DESKTOPHORZRES)
            phys_h = ctypes.windll.gdi32.GetDeviceCaps(hdc, DESKTOPVERTRES)
        finally:
            user32.ReleaseDC(0, hdc)
        if not phys_w or not phys_h:
            return None
        virt_w = user32.GetSystemMetrics(0)
        virt_h = user32.GetSystemMetrics(1)
        if not virt_w or not virt_h:
            return None
        kx, ky = phys_w / virt_w, phys_h / virt_h
        rect = wintypes.RECT()
        if not user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
            return None
        _REAL_WORK_AREA = (int(rect.left * kx), int(rect.top * ky),
                           int(rect.right * kx), int(rect.bottom * ky))
        return _REAL_WORK_AREA
    except Exception:
        return None


def work_area() -> tuple[int, int]:
    """The usable bottom-right corner of the primary display, in real pixels.

    Bubbles sit just above the taskbar, and the taskbar is not part of the work
    area — so this is what "near the bottom of the screen" is measured from.

    The numbers must be in the same space `SetWindowPos` places windows in,
    which on Windows is real pixels rather than the virtualised ones a
    DPI-unaware process is told about — see `_real_desktop`.
    """
    real = _real_desktop()
    if real is not None:
        return real[2], real[3]
    sw, sh = screen_size()
    if sys.platform == "darwin":
        try:
            from AppKit import NSScreen

            screen = NSScreen.mainScreen()
            frame, visible = screen.frame(), screen.visibleFrame()
            # Cocoa measures from the bottom-left and window positions are
            # measured from the top, so the usable bottom edge is the screen
            # height less whatever the Dock takes — which is exactly the
            # visible frame's y origin.
            return int(visible.size.width), int(frame.size.height - visible.origin.y)
        except Exception:
            return sw, sh
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            SPI_GETWORKAREA, SM_CXSCREEN, SM_CYSCREEN = 0x0030, 0, 1
            rect = wintypes.RECT()
            user32 = ctypes.windll.user32
            if user32.SystemParametersInfoW(
                    SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
                logical_w = user32.GetSystemMetrics(SM_CXSCREEN)
                logical_h = user32.GetSystemMetrics(SM_CYSCREEN)
                if logical_w and logical_h:
                    kx, ky = sw / logical_w, sh / logical_h
                    return int(rect.right * kx), int(rect.bottom * ky)
        except Exception:
            pass
    return sw, sh


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
        # Real pixels per CSS pixel for this window — 1.0 until the page says
        # otherwise (see `_fit_to_scale`). Placement is in real pixels, so
        # every size used to position a bubble goes through `_px`.
        self.scale = 1.0
        self._place_default(move=False)
        self._visible = not hidden
        self._ready = threading.Event()
        self._on_closed = on_closed
        self._sched = sched
        self._hide_at: int | None = None
        self._transparency_wired = False
        self._hwnd: int | None = None
        # Whether the bubble is in its small resting form. Compact bubbles
        # share one row along the bottom; expanded ones get a row each.
        self._compact = True
        self._size_gen = 0
        self._animating = False
        self._last_w, self._last_h = self.w, self.h
        self.window = webview.create_window(
            title, html=page(body, css, js),
            width=self.w, height=self.h, x=self.x, y=self.y,
            frameless=True, transparent=True, on_top=True, resizable=False,
            easy_drag=False, background_color=BACKDROP_KEY, hidden=hidden,
            js_api=api,
        )
        self.window.events.closed += self._closed

    def _place_default(self, move: bool = True):
        """Bottom-centre: where a bubble sits until a feature places it."""
        sw, sh = work_area()
        x = max(0, (sw - self._px(self.w)) // 2)
        y = max(0, sh - self._px(self.h) - BOTTOM_GAP)
        self._default_pos = (x, y)
        if move:
            self.move(x, y)
        else:
            self.x, self.y = x, y

    # ── lifecycle ──
    def run(self, on_started=None):
        """Start the GUI loop. Blocks; `on_started` runs on a worker thread."""
        def _boot():
            self._ready.set()
            self._make_really_transparent()
            self._fit_to_scale()
            self._apply_shape()
            if sys.platform != "win32":
                # The stage is sized in CSS there; start it at the size the
                # bubble was created with.
                self.call(f"bbSize({self.content_w}, {self.content_h})")
            self._enforce_hidden()
            if on_started:
                on_started()
        webview.start(_boot, private_mode=True)

    def _make_really_transparent(self, attempt: int = 0):
        """Re-assert the transparent browser backdrop once the runtime is up.

        pywebview already asks for a transparent window, and on Windows that
        is what actually works: the WebView2 surface composites with real
        alpha, so a page with no background leaves the desktop showing.

        What does NOT work — measured, repeatedly — is helping it along by
        keying the window out. Painting the form (or the page) magenta and
        setting `TransparencyKey` puts the box back rather than removing it,
        because WebView2 draws through DirectComposition and a layered
        window's colour key never reaches those pixels: the bubble ends up on
        a magenta rectangle instead of a dark one. Do not reintroduce it.

        All that is left here is one belt-and-braces write. pywebview sets
        `DefaultBackgroundColor` before `EnsureCoreWebView2Async`, where the
        value can be discarded, and then sets it on the EdgeChrome *wrapper*
        instead of the control it holds, which does nothing at all. Setting it
        on the real control after `CoreWebView2` exists is the assignment that
        is guaranteed to stick.

        The waits are existence checks, not timing guesses: the form is built
        on another thread, and `CoreWebView2` is null until the runtime has
        started. Both are read on the UI thread — touching a WinForms
        control's properties from the scheduler thread wedged the window
        outright, leaving the page unloaded and every later `evaluate_js`
        blocked forever.
        """
        if sys.platform != "win32" or self._transparency_wired:
            return                # Cocoa composites transparency properly
        try:
            from System import Action
            from System.Drawing import Color, Size
            from webview.platforms.winforms import BrowserView

            form = BrowserView.instances.get(self.window.uid)
            if form is None:
                self._retry_transparency(attempt)
                return

            done = []

            def _apply():
                control = getattr(getattr(form, "browser", None), "webview", None)
                if control is None or control.CoreWebView2 is None:
                    return                       # not up yet; caller retries
                control.DefaultBackgroundColor = Color.Transparent
                # pywebview floors every window at 200x100 (`MinimumSize`),
                # so a 46px-tall pill was handed a 100px-tall window and the
                # leftover 54px were painted as background — a bubble sitting
                # in a slab. Clearing the floor and re-applying the size the
                # bubble actually asked for is what makes the window the same
                # shape as the bubble.
                self._hwnd = int(str(form.Handle))
                form.MinimumSize = Size(0, 0)
                form.ClientSize = Size(self.w, self.h)
                done.append(True)

            if form.InvokeRequired:
                form.Invoke(Action(_apply))      # must run on the UI thread
            else:
                _apply()

            if done:
                self._transparency_wired = True
            else:
                self._retry_transparency(attempt)
        except Exception as e:
            # Not fatal: the bubble still works, it just keeps its backdrop.
            print(f"[webbubble] transparent background unavailable: {e}", flush=True)

    def _retry_transparency(self, attempt: int):
        """Look again shortly — up to about ten seconds."""
        if attempt < 200 and self._sched:
            self._sched.after(
                50, lambda: self._make_really_transparent(attempt + 1))

    def _fit_to_scale(self):
        """Give the page as many CSS pixels as the bubble's geometry assumes.

        A bubble is written in CSS pixels, and WebView2 lays the page out with
        the display's scale factor applied — so on a 125% display a 262px-wide
        window has only 210 CSS pixels of room, and a line of text measured to
        fit ends up ellipsised. The window is therefore made `devicePixelRatio`
        times bigger in real pixels, which is also what makes a bubble the same
        apparent size as everything else on a scaled desktop.

        The ratio is read from the page. `GetDpiForMonitor` looks like the
        tidier source and is not usable: pywebview leaves the process
        DPI-unaware, so Windows answers 96 for every monitor however it is
        scaled.
        """
        if sys.platform != "win32":
            return          # Cocoa sizes windows in points; a Retina ratio of
                            # 2 here would double every bubble
        try:
            ratio = float(self.window.evaluate_js("window.devicePixelRatio") or 1)
        except Exception:
            return
        if ratio <= 1.01:
            return
        want = (int(round(self.w * ratio)), int(round(self.h * ratio)))
        try:
            self.window.resize(*want)
            self.scale = ratio
            if (self.x, self.y) == self._default_pos:
                # The constructor placed the bubble before the scale was
                # known, so the bottom-centre default was computed from CSS
                # pixels and sat too low. Re-place it now, unless the feature
                # has already put it somewhere of its own choosing.
                self._place_default()
        except Exception as e:
            print(f"[webbubble] could not fit window to scale: {e}", flush=True)

    # ── shape ──
    def _apply_shape(self):
        """Round the window's corners so the bubble is not a rectangle.

        DWM, not `SetWindowRgn`. A window region looks like the exact answer —
        it would give a real capsule instead of DWM's fixed 8px corners — and
        it does not work here: WebView2 draws through DirectComposition, which
        the region does not clip, so the browser surface keeps painting the
        square corners the region just cut away. What that looks like on
        screen is a rounded pill with a black rectangle hanging off it.

        DWM's rounding happens at composition, after WebView2 has drawn, which
        is why it is the one thing that survives. The cost is that a bubble is
        a rounded rectangle rather than a capsule on Windows; on macOS the
        window is genuinely transparent and CSS decides the shape.

        Windows 10 has no corner preference and returns a failure code, which
        is fine: the bubble is then square-cornered and nothing else changes.
        """
        if sys.platform != "win32" or not self._hwnd:
            return
        try:
            import ctypes

            DWMWA_WINDOW_CORNER_PREFERENCE = 33
            DWMWCP_ROUND = 2
            pref = ctypes.c_int(DWMWCP_ROUND)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                self._hwnd, DWMWA_WINDOW_CORNER_PREFERENCE,
                ctypes.byref(pref), ctypes.sizeof(pref))
        except Exception as e:
            print(f"[webbubble] could not round corners: {e}", flush=True)

    def _enforce_hidden(self):
        """Undo pywebview showing a transparent window we asked to stay hidden.

        pywebview's Windows backend calls Show()/Activate() for ANY
        transparent window as soon as its page starts loading, ignoring
        hidden=True (see edgechromium.py's on_navigation_start). Every
        bubble here is transparent, so each one flashed itself onto the
        screen at launch — the Tone card appeared with no text selected,
        and bubbles meant to stay out of the way showed up as a half-painted
        box before their CSS had applied.
        """
        if self._visible:
            return
        try:
            self.window.hide()
        except Exception:
            pass

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
            # Showing the window again can drop the shape, so re-cut it.
            # Only that: the rest of the window setup runs once, and re-running
            # it here resized the window back to its unscaled size every time
            # a bubble was re-shown.
            self._apply_shape()
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
        # Where the bubble is now is also what a size animation anchors to.
        self._last_w, self._last_h = self.w, self.h
        try:
            self.window.move(self.x, self.y)
        except Exception:
            pass

    def _px(self, css: int) -> int:
        """A CSS length in the real pixels the window is placed with."""
        return int(round(css * self.scale))

    def place_bottom_center(self, bottom_gap: int = BOTTOM_GAP):
        sw, sh = work_area()
        self.move(max(0, (sw - self._px(self.w)) // 2),
                  max(0, sh - self._px(self.h) - bottom_gap))

    def place_bottom_right(self, bottom_gap: int = BOTTOM_GAP,
                           side_gap: int = BOTTOM_GAP):
        """Park the bubble in the bottom-right corner and leave it there.

        For a card the user reads rather than glances at: a fixed corner is
        predictable and never lands on top of the thing they were reading,
        which is what following the pointer did.
        """
        sw, sh = work_area()
        self.move(max(0, sw - self._px(self.w) - side_gap),
                  max(0, sh - self._px(self.h) - bottom_gap))

    def set_compact(self, compact: bool):
        """Say whether the bubble is currently in its small resting form.

        Two collapsed bubbles sit beside each other rather than one above the
        other — there is no reason for two pills the size of a coin to take
        two rows — so the layout needs to know which shape each one is in.
        """
        self._compact = bool(compact)

    def place_bottom_left(self, bottom_gap: int = BOTTOM_GAP,
                          side_gap: int = 16):
        """Park the bubble in the bottom-left corner.

        For a bubble that is a status light rather than a conversation: it
        stays out of the bottom-centre pile the other features share, so it
        never pushes them around as it grows and shrinks.
        """
        _, sh = work_area()
        self.move(side_gap, max(0, sh - self._px(self.h) - bottom_gap))

    def place_stacked(self, feature_id: str):
        """Put this bubble in its place in the bottom-centre arrangement.

        The arrangement, from the taskbar up:

        * one bottom row holding every bubble that is in its small resting
          form, side by side and centred as a group — two coin-sized pills do
          not need two rows of screen;
        * a row each, above that, for every bubble that has expanded, in
          `STACK_ORDER`.

        It is worked out from scratch every time, from who is actually up
        (`feature_bus` presence, skipping hidden bubbles) rather than from
        where anyone currently is. "Sit above whatever is already down there"
        reads the other bubble's last position, so two bubbles each move above
        the other and the pile climbs the screen a step at a time — which is
        how the Page Reader bubble ended up floating in mid-air with nothing
        beneath it.
        """
        sw, sh = work_area()
        me = {"w": self._px(self.w), "h": self._px(self.h),
              "compact": self._compact}
        others = self._live_neighbours(feature_id)

        def rank(fid: str) -> tuple[int, str]:
            # Anything not in the list sorts after everything that is, stably,
            # so a new feature needs no change here to behave.
            return (STACK_ORDER.index(fid) if fid in STACK_ORDER
                    else len(STACK_ORDER), fid)

        row = [(fid, win) for fid, win in others.items() if win.get("compact")]
        if me["compact"]:
            row.append((feature_id, me))
        row.sort(key=lambda item: rank(item[0]))
        row_h = max((win["h"] for _, win in row), default=0)
        bottom = sh - BOTTOM_GAP

        if me["compact"]:
            # Centre the whole row as a group, then take my slot in it.
            total = sum(win["w"] for _, win in row) + STACK_GAP * (len(row) - 1)
            x = (sw - total) // 2
            for fid, win in row:
                if fid == feature_id:
                    break
                x += win["w"] + STACK_GAP
            # Bubbles in the row are bottom-aligned, so a taller one does not
            # make the shorter ones look like they are floating.
            self.move(max(0, x), max(0, bottom - me["h"]))
            return

        # Expanded: a row of my own, above the compact row and above every
        # expanded bubble that sorts before me.
        top = bottom - (row_h + STACK_GAP if row else 0)
        for fid, win in sorted(others.items(), key=lambda item: rank(item[0])):
            if win.get("compact") or rank(fid) >= rank(feature_id):
                continue
            top -= win["h"] + STACK_GAP
        self.move(max(0, (sw - me["w"]) // 2), max(0, top - me["h"]))

    def _live_neighbours(self, feature_id: str) -> dict:
        """The other features' bubbles that are actually on screen."""
        out = {}
        for other, info in feature_bus.load_presence().items():
            if other == feature_id or not info:
                continue
            win = info.get("window") or {}
            if not win.get("visible", True) or "h" not in win or "w" not in win:
                continue
            if "started_at" not in info:
                # Written by a build before presence carried a timestamp, or
                # left by a process that died without cleaning up. The PID
                # check can be fooled by PID reuse, so an entry we cannot date
                # is not trustworthy enough to arrange around.
                continue
            if not feature_bus.is_feature_running(other):
                continue
            out[other] = win
        return out

    def keep_stacked(self, feature_id: str, every_ms: int = 900):
        """Re-place this bubble whenever the pile below it changes.

        Bubbles come and go while the hub is running, and a bubble that was
        placed above another one stays there long after that other one is
        gone — which is how a lone Page Reader ended up floating in the middle
        of the screen. Cheap to run: reading `presence.json` is a few
        kilobytes, and the window is only moved when the answer changes.
        """
        if not self._sched:
            return
        # A size animation is moving this window every ~12ms from its own
        # thread. Re-placing it from here mid-flight reads a width the
        # animation is halfway through changing and slams the window to a
        # position computed for a size it no longer has — the bubble visibly
        # jumps. The animation ends by placing itself, so skipping the tick
        # loses nothing.
        if not self._animating:
            self.place_stacked(feature_id)
        self._sched.after(every_ms, lambda: self.keep_stacked(feature_id, every_ms))

    # ── growing and shrinking ──
    def animate_size(self, width: int, height: int, ms: int = 200,
                     on_done=None, anchor: str = "bottom-center"):
        """Grow or shrink the bubble, keeping the edge it is anchored to.

        The two platforms get there differently, because their windows are
        different things (see `page()`).

        **macOS** — the window is transparent and has room around the bubble,
        so the bubble is resized in CSS and the transition runs on the
        compositor. The window is only ever made big enough to hold it: it
        jumps to whichever size is larger straight away, and is trimmed to the
        target once the transition has finished.

        **Windows** — the window IS the bubble, so it has to be resized, one
        frame at a time, on a thread of its own. Two things make that smooth
        rather than a slideshow: `SetWindowPos` posted asynchronously (a
        blocking one costs 30-110ms on a WebView2 window), and frames driven
        by the clock so a late one catches up instead of stretching the
        animation.
        """
        target = (int(width), int(height))
        if (self.content_w, self.content_h) == target:
            if on_done:
                on_done()
            return
        anchor_left = anchor == "bottom-left"
        self._size_gen += 1
        gen = self._size_gen

        if sys.platform != "win32" or not self._hwnd:
            self._animate_in_page(target, ms, gen, anchor_left, on_done)
            return

        start = (self.content_w, self.content_h)
        bottom = self.y + self._px(self.h)
        centre = self.x + self._px(self.w) // 2
        began = time.monotonic()
        self._animating = True

        def run():
            while True:
                if gen != self._size_gen:
                    return                   # a newer animation took over (it owns the flag)
                t = min(1.0, (time.monotonic() - began) * 1000 / ms)
                # ease-out: quick off the mark, settling into the final size
                e = 1 - (1 - t) ** 3
                w = round(start[0] + (target[0] - start[0]) * e)
                h = round(start[1] + (target[1] - start[1]) * e)
                self._set_content_size(w, h)
                pw, ph = self._px(self.w), self._px(self.h)
                x = self.x if anchor_left else max(0, centre - pw // 2)
                self._set_bounds(x, max(0, bottom - ph), pw, ph)
                if t >= 1.0:
                    break
                time.sleep(0.012)
            self._apply_shape()
            self._animating = False
            if on_done:
                on_done()

        # Its own thread, not the feature's scheduler: the scheduler also runs
        # poll loops and hotkey work, and a frame that waits behind those is a
        # frame the eye sees as a step.
        threading.Thread(target=run, daemon=True).start()

    def _animate_in_page(self, target: tuple[int, int], ms: int, gen: int,
                         anchor_left: bool, on_done):
        """The macOS path: CSS moves the bubble, the window just contains it."""
        self._animating = True
        hold = (max(self.content_w, target[0]), max(self.content_h, target[1]))
        self.resize_content(*hold)
        (self._anchor_bottom_left if anchor_left
         else self._anchor_bottom_center)()
        self.call(f"bbSize({target[0]}, {target[1]})")

        def settle():
            if gen != self._size_gen:
                return
            self._animating = False
            self._set_content_size(*target)
            self.resize_content(*target)
            (self._anchor_bottom_left if anchor_left
             else self._anchor_bottom_center)()
            if on_done:
                on_done()

        if self._sched:
            self._sched.after(ms + 40, settle)
        else:
            settle()

    def _set_content_size(self, w: int, h: int):
        """Book-keeping for a size change that the caller applies itself."""
        self.content_w, self.content_h = int(w), int(h)
        self.w = self.content_w + SHADOW_PAD_X
        self.h = self.content_h + SHADOW_PAD_Y

    def _set_bounds(self, x: int, y: int, pw: int, ph: int):
        """Move and resize the window in one call, without pywebview.

        `SetWindowPos` does both at once and does not have to be marshalled
        onto the UI thread, which is what keeps a resize animation smooth.
        """
        self.x, self.y = int(x), int(y)
        self._last_w, self._last_h = self.w, self.h
        try:
            import ctypes

            # ASYNCWINDOWPOS is the difference between a smooth animation
            # and a slideshow: a plain SetWindowPos on a WebView2 window
            # blocks until the browser has resized itself, measured at 30-110ms
            # a frame. Posting the request instead costs under a millisecond
            # and lets the browser catch up on its own thread.
            SWP_NOZORDER, SWP_NOACTIVATE, SWP_ASYNCWINDOWPOS = 0x0004, 0x0010, 0x4000
            ctypes.windll.user32.SetWindowPos(
                self._hwnd, 0, int(x), int(y), int(pw), int(ph),
                SWP_NOZORDER | SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS)
        except Exception:
            # Fall back to the slow path rather than leaving the window behind.
            try:
                self.window.resize(pw, ph)
                self.window.move(int(x), int(y))
            except Exception:
                pass

    def _anchor_bottom_left(self):
        """Keep the bubble's bottom-left corner where it already was."""
        bottom = self.y + self._px(self._last_h if self._last_h else self.h)
        self._last_w, self._last_h = self.w, self.h
        self.move(self.x, max(0, bottom - self._px(self.h)))

    def _anchor_bottom_center(self):
        """Keep the bubble's bottom edge and centre where they already were."""
        bottom = self.y + self._px(self._last_h if self._last_h else self.h)
        centre = self.x + self._px(self._last_w if self._last_w else self.w) // 2
        self._last_w, self._last_h = self.w, self.h
        self.move(max(0, centre - self._px(self.w) // 2),
                  max(0, bottom - self._px(self.h)))

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
            # In real pixels, like every other size handed to the window —
            # see `_fit_to_scale`.
            self.window.resize(self._px(self.w), self._px(self.h))
            self._apply_shape()          # the corners move with the size
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
        return {"x": self.x + self._px(self.w - self.content_w) // 2,
                "y": self.y + self._px(self.h - self.content_h) // 2,
                "w": self._px(self.content_w), "h": self._px(self.content_h),
                # A hidden bubble holds no place in the stack — a feature that
                # is on but showing nothing used to push the others up the
                # screen, leaving one visible bubble floating in mid-air.
                "visible": self._visible,
                "compact": self._compact}

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
  /* Drop the exit state and let the layout settle on it BEFORE restarting the
     entrance. Removing the class and restarting the keyframes in one frame
     left the browser interpolating from the exit transform into the entrance
     transform, so a re-shown bubble jumped instead of rising. */
  stage.style.transition = 'none';
  stage.classList.remove('leaving');
  void stage.offsetWidth;
  stage.style.transition = '';
  stage.style.animation = 'none';
  void stage.offsetWidth;          /* restart the entrance keyframes */
  stage.style.animation = '';
}
function leave() { stage.classList.add('leaving'); }

/* Resize the bubble itself, with a transition. Used where the window is
   transparent and bigger than the bubble (macOS): the shape can grow in CSS,
   on the compositor, and the window only has to be big enough to contain it.
   On Windows the window IS the bubble, so it is resized instead. */
function bbSize(w, h) {
  stage.style.setProperty('--bw', w + 'px');
  stage.style.setProperty('--bh', h + 'px');
}

"""
