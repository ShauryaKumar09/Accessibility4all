"""The Voice Control compact bar — a floating pill that sits over any app.

The window itself, the shared look, and the `Scheduler` that replaced Tk's
`after()` all live in `shared/webbubble.py` — read its header for why every
bubble is a transparent web view rather than a `tk.Canvas`. What's here is only
what makes this bubble the *voice* one: a mic circle you can hold, a waveform
driven by the real mic level, and one line of live state.

Colours and proportions come from
`design_handoff_a11y4all_redesign/design/feature_windows_full_panels.dc.html`
("Compact bar — floats over any app"), drawn about a quarter smaller than the
mock so it stays out of the way of real work.
"""

from __future__ import annotations

import json
import queue
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import webbubble as wb  # noqa: E402
from shared.ui_kit import C         # noqa: E402

Scheduler = wb.Scheduler             # the feature imports it from here

# ── geometry ─────────────────────────────────────────────────────────────────
# The design's compact bar is 460x80. Everything below scales together EXCEPT
# the two accessibility floors the redesign fixes: the mic circle stays a 48px
# target and the line of text stays 17px.
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

BODY = """
<div class="pill" id="bar">
  <div id="mic"><div class="cap"></div><div class="base"></div></div>
  <div id="wave"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i></div>
  <div class="label" id="text"></div>
</div>
<span id="measure"></span>
"""

CSS = """
#bar {
  gap: %(GAP)dpx; width: %(BAR_W)dpx; height: %(BAR_H)dpx;
  padding: %(PAD_V)dpx %(PAD_R)dpx %(PAD_V)dpx %(PAD_L)dpx;
  transition: width .24s var(--ease), border-color .22s var(--ease);
  will-change: width;
}
#bar.listening { border-color: rgba(111,155,255,.45); }

/* the mic circle — also the press-and-hold fallback for the ` key */
#mic {
  flex: none; width: %(MIC_D)dpx; height: %(MIC_D)dpx; border-radius: 50%%;
  display: flex; flex-direction: column; align-items: center;
  justify-content: center; gap: 3px; cursor: pointer;
  background: #1c202a; border: 2px solid var(--off);
  transition: background .22s var(--ease), border-color .22s var(--ease),
              transform .18s var(--ease);
}
#mic:active { transform: scale(.94); }
#mic .cap  { width: %(CAP_W)dpx; height: %(CAP_H)dpx; border-radius: %(CAP_W)dpx;
             background: var(--fg-bubble); }
#mic .base { width: %(BASE_W)dpx; height: %(BASE_H)dpx; border-radius: 2px;
             background: var(--fg-bubble); }
#mic .cap, #mic .base { transition: background .22s var(--ease); }
#bar.listening #mic { background: var(--accent-fill); border-color: var(--accent); }
#bar.listening #mic .cap, #bar.listening #mic .base { background: var(--accent-on); }

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
  background: var(--off); opacity: .5;
  transform: scaleY(.16); transform-origin: center;
  transition: transform .3s var(--ease), background .25s var(--ease),
              opacity .25s var(--ease);
  will-change: transform;
}
#bar.listening #wave i {
  background: %(WAVE)s; opacity: 1;
  animation: fw-bar var(--dur) ease-in-out var(--delay) infinite both;
}
@keyframes fw-bar {
  0%%, 100%% { transform: scaleY(.2); }
  50%%       { transform: scaleY(1); }
}

#text { flex: 1; min-width: 0; font-size: %(TEXT_PX)dpx; }
#measure {
  position: absolute; visibility: hidden; white-space: nowrap;
  font-size: %(TEXT_PX)dpx; left: -9999px; top: -9999px;
}
"""

JS = """
const bar = document.getElementById('bar');
const wave = document.getElementById('wave');
const textEl = document.getElementById('text');
const measure = document.getElementById('measure');

// the fixed part of the pill: paddings + mic + gaps + waveform + border
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

// Release is watched on the window, not on the circle, so dragging off the mic
// while still holding the button ends the recording.
let held = false;
document.getElementById('mic').addEventListener('mousedown',
  () => { held = true; window.pywebview.api.mic_down(); });
window.addEventListener('mouseup',
  () => { if (held) { held = false; window.pywebview.api.mic_up(); } });
"""

_TOKENS = dict(
    BAR_W=BAR_W, BAR_H=BAR_H, BAR_MAX_W=BAR_MAX_W, MIC_D=MIC_D, PAD_V=PAD_V,
    PAD_L=PAD_L, PAD_R=PAD_R, GAP=GAP, TEXT_PX=TEXT_PX, WAVE_GAP=WAVE_GAP,
    WAVE_BAR_W=WAVE_BAR_W, WAVE_H=WAVE_H, CAP_W=CAP_W, CAP_H=CAP_H,
    BASE_W=BASE_W, BASE_H=BASE_H, WAVE=C["WAVE"],
    CHROME=PAD_L + MIC_D + GAP + WAVE_W + GAP + PAD_R + 2,  # +2: the pill's border
)


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

    Everything else is safe to call from any thread — see `webbubble.Bubble`.
    """

    def __init__(self, events: queue.Queue, sched: "wb.Scheduler | None" = None,
                 on_closed=None):
        self._last: tuple[str, str, str] | None = None
        self._last_level = -1.0
        self.bubble = wb.Bubble(
            "Voice Control", BODY, BAR_W, BAR_H,
            css=CSS % _TOKENS, js=JS % _TOKENS, api=_JsApi(events),
            sched=sched, on_closed=on_closed)

    # ── lifecycle ──
    def run(self, on_started=None):
        self.bubble.run(on_started)

    def close(self):
        self.bubble.close()

    # ── state in ──
    def set_state(self, state: str, text: str, color: str):
        payload = (state, text, color)
        if payload == self._last:
            return
        self._last = payload
        self.bubble.call(f"setState({json.dumps(state)}, {json.dumps(text)}, "
                         f"{json.dumps(color)})")

    def set_level(self, level: float):
        level = max(0.0, min(1.0, level))
        if abs(level - self._last_level) < 0.01:
            return
        self._last_level = level
        self.bubble.call(f"setLevel({level:.3f})")

    def rect(self) -> dict:
        """Where the pill actually is, for `feature_bus` presence."""
        return self.bubble.rect()
