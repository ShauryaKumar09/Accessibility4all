"""Shared canvas-drawn UI kit for the Accessibility4all redesign.

Every surface in the app (hub rows, settings sheets, the walkthrough, the
desktop bubbles) is drawn on a `tk.Canvas` with rounded shapes rather than with
ttk widgets, so the same tokens produce the same look in every process. tkinter
has no blur or CSS animation, so translucent design values are pre-blended to
solid fills and animation is a `after()` frame loop.

Nothing here touches tkinter from a background thread — callers must marshal via
`after(0, ...)` exactly as before.
"""

from __future__ import annotations

import sys
import tkinter as tk
from tkinter import font as tkfont

_IS_WINDOWS = sys.platform.startswith("win")


# ── Design tokens ─────────────────────────────────────────────────────────────
# Names mirror the handoff. Values that are rgba() in the design are blended
# against the surface they sit on, because tkinter fills are opaque.
C = dict(
    BG="#14161b",              # window background
    CARD="#191c23",            # card / row background
    CARD_HOVER="#1d212a",
    INSET="#1e222a",           # secondary button fill
    CHIP="#21252e",

    BORDER="#262a33",          # default border
    BORDER_CTRL="#2e333e",     # control border
    BORDER_CHIP="#343a46",
    DIVIDER="#1f232b",

    FG="#f4f6fa",              # primary text
    FG_BUBBLE="#dfe4ec",       # text inside bubbles
    FG_SECOND="#b6becd",       # secondary text
    FG_MUTED="#7d859a",
    FG_CAPTION="#6f7789",
    FG_DISABLED="#565e6e",

    ACCENT="#6f9bff",          # accent border / focus ring
    ACCENT_FILL="#2b3a63",
    ACCENT_ON="#eaf0ff",       # text on accent fill
    WAVE="#8ab4ff",

    ON="#4ad991",
    OFF_TRACK="#3a4051",
    OFF_BORDER="#4a5165",
    KNOB="#ffffff",

    STOP_BORDER="#ff8f8f",
    STOP_FILL="#3a2226",
    STOP_TEXT="#ffdede",

    WARM_BG="#3a2f1c",
    WARM_BORDER="#7a6134",
    WARM_TEXT="#ffd68a",

    # rgba(18,20,25,0.92) — bubble surface, flattened
    BUBBLE="#121419",
    # rgba(6,7,10,0.72) over #14161b — modal scrim, flattened
    SCRIM="#0a0b0f",

    DOT_IDLE="#262a33",
    DOT_IDLE_FG="#9aa3b4",
    DOT_DONE="#1f3a2c",
    DOT_DONE_FG="#8ff0bb",
    TRACK_BG="#2b3038",        # progress track
)

FAMILY = "Helvetica"

# Type scale from the handoff. Nothing below 15, no body copy below 17.
SIZES = dict(
    display=34, h1=30, h2=28, h3=24, h4=22, body_lg=20, body=19,
    body_sm=18, ui=17, ui_sm=16, caption=15,
)

FONT_SCALE_MIN, FONT_SCALE_MAX, FONT_SCALE_STEP = 0.85, 1.6, 0.15

# Minimum touch target (px) and the standard switch geometry.
MIN_TARGET = 48
SWITCH_W, SWITCH_H = 76, 42
SWITCH_KNOB = 30


class FontSet:
    """Named fonts that all rescale together when the user presses A− / A+."""

    def __init__(self, scale: float = 1.0):
        self.scale = scale
        self._fonts: dict[str, tkfont.Font] = {}
        for name, size in SIZES.items():
            self._fonts[name] = tkfont.Font(
                family=FAMILY, size=round(size * scale))
            self._fonts[name + "_b"] = tkfont.Font(
                family=FAMILY, size=round(size * scale), weight="bold")

    def __getitem__(self, name: str) -> tkfont.Font:
        return self._fonts[name]

    def px(self, name: str) -> int:
        """The design px value of a role at the current scale."""
        base = name[:-2] if name.endswith("_b") else name
        return round(SIZES[base] * self.scale)

    def set_scale(self, scale: float):
        self.scale = scale
        for name, font in self._fonts.items():
            base = name[:-2] if name.endswith("_b") else name
            font.configure(size=round(SIZES[base] * scale))


def s(value: float, scale: float) -> int:
    """Scale a spacing/size token by the current text scale."""
    return max(1, round(value * scale))


# ── Drawing primitives ────────────────────────────────────────────────────────
def fade(color: str, toward: str, t: float) -> str:
    """Blend two #rrggbb colours. tkinter has no alpha, so a fading element is
    drawn by fading its colour toward whatever it sits on."""
    t = max(0.0, min(1.0, t))
    a = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(toward[i:i + 2], 16) for i in (1, 3, 5))
    return "#%02x%02x%02x" % tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def rounded_points(x1, y1, x2, y2, r):
    """Control points for a rounded rect drawn via create_polygon(smooth=True)."""
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    return [
        x1 + r, y1, x2 - r, y1, x2, y1,
        x2, y1 + r, x2, y2 - r, x2, y2,
        x2 - r, y2, x1 + r, y2, x1, y2,
        x1, y2 - r, x1, y1 + r, x1, y1,
    ]


def rounded_rect(canvas: tk.Canvas, x1, y1, x2, y2, r,
                 fill="", outline="", width=0, **kw):
    return canvas.create_polygon(
        rounded_points(x1, y1, x2, y2, r), smooth=True,
        fill=fill, outline=outline, width=width, **kw)


def pill(canvas: tk.Canvas, x1, y1, x2, y2, fill="", outline="", width=0,
         tags=(), **kw):
    """A fully rounded capsule: two end caps plus the middle bar.

    create_polygon(smooth=True) rounds corners by cutting them, which looks
    wrong at radius = height/2, so capsules are built from real ovals.
    """
    h = y2 - y1
    r = h / 2
    items = [
        canvas.create_oval(x1, y1, x1 + h, y2, fill=fill, outline=fill,
                           tags=tags, **kw),
        canvas.create_oval(x2 - h, y1, x2, y2, fill=fill, outline=fill,
                           tags=tags, **kw),
        canvas.create_rectangle(x1 + r, y1, x2 - r, y2, fill=fill, outline=fill,
                                tags=tags, **kw),
    ]
    if outline and width:
        items.append(canvas.create_arc(x1, y1, x1 + h, y2, start=90, extent=180,
                                       style="arc", outline=outline, width=width,
                                       tags=tags))
        items.append(canvas.create_arc(x2 - h, y1, x2, y2, start=270, extent=180,
                                       style="arc", outline=outline, width=width,
                                       tags=tags))
        items.append(canvas.create_line(x1 + r, y1 + width / 2, x2 - r, y1 + width / 2,
                                        fill=outline, width=width, tags=tags))
        items.append(canvas.create_line(x1 + r, y2 - width / 2, x2 - r, y2 - width / 2,
                                        fill=outline, width=width, tags=tags))
    return items


def draw_switch(canvas: tk.Canvas, x: int, y: int, on: bool,
                w: int = SWITCH_W, h: int = SWITCH_H, knob: int = SWITCH_KNOB,
                knob_offset: float | None = None, tags=()):
    """The 76x42 track + 30px knob used by hub rows and settings rows.

    `knob_offset` (0..1) lets an animation slide the knob; None snaps to state.
    """
    track = C["ON"] if on else C["OFF_TRACK"]
    border = C["ON"] if on else C["OFF_BORDER"]
    pill(canvas, x, y, x + w, y + h, fill=track, outline=border, width=2,
         tags=tags)
    pad = (h - knob) / 2
    travel = w - knob - pad * 2
    t = (1.0 if on else 0.0) if knob_offset is None else knob_offset
    kx = x + pad + travel * t
    canvas.create_oval(kx, y + pad, kx + knob, y + pad + knob,
                       fill=C["KNOB"], outline="", tags=tags)


def wrap_lines(font: tkfont.Font, text: str, width: int) -> list[str]:
    """Greedy word wrap measured with the real font metrics."""
    if not text:
        return []
    lines: list[str] = []
    for para in text.split("\n"):
        words = para.split()
        if not words:
            lines.append("")
            continue
        cur = words[0]
        for word in words[1:]:
            trial = cur + " " + word
            if font.measure(trial) <= width:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
    return lines


def ellipsize(font: tkfont.Font, text: str, width: int) -> str:
    if font.measure(text) <= width:
        return text
    ell = "…"
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if font.measure(text[:mid] + ell) <= width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + ell


# ── Interactive canvases ──────────────────────────────────────────────────────
class TapCanvas(tk.Canvas):
    """A canvas that behaves like one big button.

    Click, Space and Return all activate it, hover and keyboard focus are
    tracked, and the focus ring is drawn as part of the shape so keyboard-only
    users can always see where they are.
    """

    def __init__(self, parent, width: int, height: int, command=None,
                 bg: str | None = None, takefocus: int = 1, **kw):
        self.command = command
        self.hovered = False
        self.focused = False
        super().__init__(parent, width=width, height=height,
                         bg=bg or parent["bg"], highlightthickness=0, bd=0,
                         takefocus=takefocus, cursor="hand2", **kw)
        self.bind("<Button-1>", self._on_click)
        self.bind("<Return>", self._activate)
        self.bind("<space>", self._activate)
        self.bind("<Enter>", lambda e: self._set(hover=True))
        self.bind("<Leave>", lambda e: self._set(hover=False))
        self.bind("<FocusIn>", lambda e: self._set(focus=True))
        self.bind("<FocusOut>", lambda e: self._set(focus=False))

    def _on_click(self, event=None):
        if self["takefocus"]:
            self.focus_set()
        return self._activate(event)

    def _activate(self, event=None):
        if self.command:
            self.command()
        return "break"

    def _set(self, hover: bool | None = None, focus: bool | None = None):
        if hover is not None:
            self.hovered = hover
        if focus is not None:
            self.focused = focus
        self.redraw()

    def redraw(self):
        """Subclasses draw here; called on every state change."""


class TextButton(TapCanvas):
    """A rounded text button: `Change`, `Back`, `Got it`, `A+`, and friends."""

    def __init__(self, parent, fonts: FontSet, text: str, command,
                 role: str = "ui_sm", height: int = 52, width: int | None = None,
                 h_pad: int = 20, radius: int = 12, primary: bool = False,
                 danger: bool = False, disabled: bool = False, bold: bool = False,
                 fill: str | None = None):
        self.fonts = fonts
        self.role = (role + "_b") if bold or primary else role
        self._text = text
        self.primary = primary
        self.danger = danger
        self.fill = fill
        self.disabled = disabled
        self.radius = radius
        w = width or max(MIN_TARGET, fonts[self.role].measure(text) + h_pad * 2)
        super().__init__(parent, w, height, command=command)
        self.redraw()

    def set_text(self, text: str):
        self._text = text
        self.configure(width=max(MIN_TARGET,
                                 self.fonts[self.role].measure(text) + 40))
        self.redraw()

    def set_disabled(self, disabled: bool):
        self.disabled = disabled
        self.configure(cursor="arrow" if disabled else "hand2")
        self.redraw()

    def _activate(self, event=None):
        if self.disabled:
            return "break"
        return super()._activate(event)

    def redraw(self):
        self.delete("all")
        w = int(self["width"])
        h = int(self["height"])
        if self.danger:
            fill, border, fg = C["STOP_FILL"], C["STOP_BORDER"], C["STOP_TEXT"]
        elif self.primary:
            fill, border, fg = C["ACCENT_FILL"], C["ACCENT"], C["ACCENT_ON"]
        else:
            fill, border, fg = (self.fill or C["INSET"]), C["BORDER_CTRL"], C["FG"]
        if self.disabled:
            fg = C["FG_DISABLED"]
        if self.focused:
            border = C["ACCENT"]
        elif self.hovered and not self.disabled:
            border = C["ACCENT"]
        rounded_rect(self, 1, 1, w - 1, h - 1, self.radius,
                     fill=fill, outline=border, width=2)
        self.create_text(w / 2, h / 2, text=self._text,
                         font=self.fonts[self.role], fill=fg)


# ── Bubble windows ────────────────────────────────────────────────────────────
def make_bubble(win: tk.Misc, width: int, height: int) -> bool:
    """Turn a Tk/Toplevel into a borderless, always-on-top desktop bubble.

    Returns True when the window really is transparent behind the drawn shape.
    Windows can key one colour out; macOS Tk accepts `-transparent` but then
    renders the whole window black, so there we use the documented fallback —
    a solid dark fill the same colour as the bubble, which hides the square
    corners against a dark desktop.
    """
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    transparent = False
    if _IS_WINDOWS:
        try:
            win.attributes("-transparentcolor", "#010203")
            win.configure(bg="#010203")
            transparent = True
        except tk.TclError:
            pass
    if not transparent:
        win.configure(bg=C["BUBBLE"])
    win.geometry(f"{width}x{height}")
    return transparent


def bubble_canvas(parent, width: int, height: int, transparent: bool) -> tk.Canvas:
    if transparent:
        bg = parent["bg"]
    else:
        bg = C["BUBBLE"]
    return tk.Canvas(parent, width=width, height=height, bg=bg,
                     highlightthickness=0, bd=0)


def raise_bubble(win: tk.Misc):
    """Map and float a bubble. macOS ignores lift()/-topmost until the window
    is actually mapped, so features call this from an `after()` once running."""
    try:
        win.deiconify()
        win.attributes("-topmost", True)
        win.lift()
    except tk.TclError:
        pass


def place_bottom_center(win: tk.Misc, width: int, height: int, bottom_gap: int = 80):
    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    x = max(0, (sw - width) // 2)
    y = max(0, sh - height - bottom_gap)
    win.geometry(f"{width}x{height}+{x}+{y}")


class CircleButton(TapCanvas):
    """A round control whose glyph is drawn: play, pause, or a microphone.

    Used by the Page Reader bubble (play/pause) and the Voice Control bubble
    (the mic dot, which is also the press-and-hold fallback for the ` key).
    """

    def __init__(self, parent, diameter: int, kind: str, command=None,
                 fill: str | None = None, border: str | None = None,
                 glyph: str | None = None, border_width: int = 2,
                 takefocus: int = 1):
        self.kind = kind
        self.fill = fill or C["ACCENT_FILL"]
        self.border = border
        self.glyph = glyph or C["ACCENT_ON"]
        self.border_width = border_width
        super().__init__(parent, diameter, diameter, command=command,
                         takefocus=takefocus)
        self.redraw()

    def set_style(self, kind: str | None = None, fill: str | None = None,
                  border: str | None = None, glyph: str | None = None):
        if kind is not None:
            self.kind = kind
        if fill is not None:
            self.fill = fill
        if border is not None:
            self.border = border
        if glyph is not None:
            self.glyph = glyph
        self.redraw()

    def redraw(self):
        self.delete("all")
        d = int(self["width"])
        outline = self.border or ""
        if self.focused:
            outline = C["ACCENT"]
        width = self.border_width if outline else 0
        inset = width / 2
        self.create_oval(inset, inset, d - inset, d - inset, fill=self.fill,
                         outline=outline, width=width)
        cx = cy = d / 2
        if self.kind == "pause":
            bar_w, bar_h, gap = max(4, round(d * 0.125)), round(d * 0.375), max(3, round(d * 0.1))
            for sign in (-1, 1):
                x = cx + sign * (gap / 2 + (bar_w if sign > 0 else 0)) - (bar_w if sign < 0 else 0)
                self.create_rectangle(x, cy - bar_h / 2, x + bar_w, cy + bar_h / 2,
                                      fill=self.glyph, outline="")
        elif self.kind == "play":
            w, h = round(d * 0.31), round(d * 0.375)
            left = cx - w / 3
            self.create_polygon(left, cy - h, left, cy + h, left + w, cy,
                                fill=self.glyph, outline="")
        elif self.kind == "mic":
            cap_w, cap_h = max(6, round(d * 0.2)), max(10, round(d * 0.35))
            self.create_oval(cx - cap_w / 2, cy - cap_h / 2 - round(d * 0.05),
                             cx + cap_w / 2, cy + cap_h / 2 - round(d * 0.05),
                             fill=self.glyph, outline="")
            self.create_rectangle(cx - cap_w / 2, cy - round(d * 0.05),
                                  cx + cap_w / 2, cy + cap_h / 2 - round(d * 0.05),
                                  fill=self.glyph, outline="")
            bar_w, bar_h = max(10, round(d * 0.35)), max(3, round(d * 0.075))
            self.create_rectangle(cx - bar_w / 2, cy + cap_h / 2,
                                  cx + bar_w / 2, cy + cap_h / 2 + bar_h,
                                  fill=self.glyph, outline="")


def progress_bar(canvas: tk.Canvas, x1, y1, x2, height, fraction: float,
                 tags=()):
    """A 5px rounded track with an accent fill, as used in the reader bubble."""
    fraction = max(0.0, min(1.0, fraction))
    pill(canvas, x1, y1, x2, y1 + height, fill=C["TRACK_BG"], tags=tags)
    if fraction > 0:
        end = x1 + max(height, (x2 - x1) * fraction)
        pill(canvas, x1, y1, end, y1 + height, fill=C["ACCENT"], tags=tags)
