"""The design tokens every surface in the app is built from.

One colour dict, one type scale, one set of minimum sizes — read by the hub's
page (`hub_ui/style.css` mirrors the values), by `shared/webbubble.py`, which
turns them into the CSS custom properties every bubble inherits, and by any
feature that needs a colour by name.

This used to be a canvas-drawn widget kit as well, because the bubbles were
`tk.Canvas` windows. They are transparent web views now — see
`shared/webbubble.py` for why — so the drawing code went with them and what is
left is the vocabulary.

Values that are rgba() in the handoff are pre-blended against the surface they
sit on, so each one is a plain colour usable in either place.
"""

from __future__ import annotations

# ── Design tokens ─────────────────────────────────────────────────────────────
# Names mirror the handoff.
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
