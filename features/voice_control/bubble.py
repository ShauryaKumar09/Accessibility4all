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
MIC_D = 34                 # the hold-to-talk target
TEXT_PX = 14               # the live-state line
PAD_V = 6                  # padding above/below the mic circle
PAD_L = 6
PAD_R = 14
GAP = 9                    # mic → waveform → text
WAVE_BARS, WAVE_BAR_W, WAVE_GAP, WAVE_H = 7, 3, 3, 17
WAVE_W = WAVE_BARS * WAVE_BAR_W + (WAVE_BARS - 1) * WAVE_GAP
CAP_W, CAP_H = 7, 11       # the mic glyph: capsule + base bar
BASE_W, BASE_H = 11, 2

BAR_H = MIC_D + PAD_V * 2                       # 46
BAR_W = 300
MAX_LINES = 2                                   # a long line wraps, not widens
LINE_H = 17                                     # must match #text line-height


def _max_w() -> int:
    """How wide the bar may grow for a long line.

    A bar that grows to fit any confirmation on ONE line reached ~760px and
    read as a banner stretched across the desktop rather than a pill floating
    beside the work. So the width is capped tight and a long line wraps to a
    second row instead (`MAX_LINES`) — the bar gets taller, not wider.
    """
    sw, _ = wb.screen_size()
    return max(BAR_W, min(420, sw - 160))


BAR_MAX_W = _max_w()

# Idle, the bar is just the mic — a coin by the taskbar, sitting beside
# whatever else is resting down there. It grows into the full bar the moment
# it is listening or has something to say, the way a dictation pill does.
IDLE_W = 46
IDLE_H = 34

BODY = """
<div class="pill" id="bar">
  <div id="mic"><div class="cap"></div><div class="base"></div></div>
  <div id="wave"><i></i><i></i><i></i><i></i><i></i><i></i><i></i></div>
  <div class="label" id="text"></div>
  <div id="key"></div>
</div>
<span id="measure"></span>
"""

CSS = """
#bar {
  gap: %(GAP)dpx; width: 100%%; height: 100%%;
  padding: %(PAD_V)dpx %(PAD_R)dpx %(PAD_V)dpx %(PAD_L)dpx;
  transition: border-color .22s var(--ease);
  overflow: hidden;
}
/* Collapsed: the mic alone, filling the little window. Python animates the
   window (`Bubble.animate_size`); CSS only says what is inside it. */
#key { display: none; }
.small #bar { padding: 0; gap: 0; justify-content: center; }
.small #wave, .small #text, .small #key { display: none; }
.small #mic { width: 26px; height: 26px; border-width: 1px; }
.small #mic .cap  { width: 5px; height: 8px; }
.small #mic .base { width: 8px; height: 2px; }
#bar.listening { border-color: rgba(111,155,255,.45); }

/* the mic circle — also the press-and-hold fallback for the ` key */
#mic {
  flex: none; width: %(MIC_D)dpx; height: %(MIC_D)dpx; border-radius: 50%%;
  display: flex; flex-direction: column; align-items: center;
  justify-content: center; gap: 2px; cursor: pointer;
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

#text {
  flex: 1; min-width: 0; font-size: %(TEXT_PX)dpx; line-height: %(LINE_H)dpx;
  /* Wrap to at most two rows rather than stretching the bar across the
     desktop; anything longer than that ellipsises. */
  display: -webkit-box; -webkit-box-orient: vertical;
  -webkit-line-clamp: %(MAX_LINES)d; overflow: hidden;
  overflow-wrap: anywhere;
}
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
const BAR_H = %(BAR_H)d, LINE_H = %(LINE_H)d, MAX_LINES = %(MAX_LINES)d;

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
}

/* How wide the bar would like to be for this line, in CSS pixels. Python asks
   for it and resizes the window, because on Windows the window IS the bar —
   growing it in CSS alone would just clip the text. */
function wantedWidth(text) {
  measure.textContent = text;
  return Math.ceil(Math.min(MAX_W, Math.max(BASE_W, CHROME + measure.offsetWidth + 6)));
}

/* [width, height] for this line. Once the text is wider than the cap it wraps,
   so the bar answers with extra rows of height instead of more width. */
function wantedBox(text) {
  const w = wantedWidth(text);
  measure.textContent = text;
  const avail = w - CHROME;
  const lines = Math.min(MAX_LINES, Math.max(1, Math.ceil(measure.offsetWidth / Math.max(1, avail))));
  return [w, BAR_H + (lines - 1) * LINE_H];
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
    MAX_LINES=MAX_LINES, LINE_H=LINE_H,
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
        self._sched = sched
        self._expanded = False
        self.bubble = wb.Bubble(
            "Voice Control", BODY, IDLE_W, IDLE_H,
            css=CSS % _TOKENS, js=JS % _TOKENS, api=_JsApi(events),
            sched=sched, on_closed=on_closed)

    # ── lifecycle ──
    def run(self, on_started=None):
        self.bubble.run(on_started)

    def close(self):
        self.bubble.close()

    # ── state in ──
    def set_hotkey(self, key: str):
        """The one thing the collapsed oval says: the push-to-talk key."""
        self.bubble.set_text("key", key)

    def set_state(self, state: str, text: str, color: str):
        payload = (state, text, color)
        if payload == self._last:
            return
        self._last = payload
        self.bubble.call(f"setState({json.dumps(state)}, {json.dumps(text)}, "
                         f"{json.dumps(color)})")
        self._fit(state, text)

    def _fit(self, state: str, text: str):
        """Oval while idle, full bar while there is something to show.

        Idle is the resting state the user sees most of the time, so it is the
        small one — the bar earns its width only while listening, working, or
        showing what was heard.
        """
        want_bar = state != "idle"
        self.bubble.set_compact(not want_bar)
        if want_bar:
            box = self.bubble.measure(f"wantedBox({json.dumps(text)})", None)
            if isinstance(box, (list, tuple)) and len(box) == 2:
                width, height = int(box[0]), int(box[1])
            else:
                width, height = BAR_W, BAR_H
            self.bubble.call("document.body.classList.remove('small')")
            self.bubble.animate_size(width, height)
        else:
            # Unconditional, not "only if it was open": the first idle state
            # arrives before anything has expanded, and that is the one that
            # puts the bar into its resting oval.
            self.bubble.call("document.body.classList.add('small')")
            self.bubble.animate_size(IDLE_W, IDLE_H)
        self._expanded = want_bar

    def set_level(self, level: float):
        level = max(0.0, min(1.0, level))
        if abs(level - self._last_level) < 0.01:
            return
        self._last_level = level
        self.bubble.call(f"setLevel({level:.3f})")

    def place_stacked(self, feature_id: str):
        """Bottom-centre, above whatever is already down there — rechecked as
        other bubbles come and go."""
        self.bubble.keep_stacked(feature_id)

    def rect(self) -> dict:
        """Where the pill actually is, for `feature_bus` presence."""
        return self.bubble.rect()
