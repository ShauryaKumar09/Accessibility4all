# Handoff: Accessibility4all UI redesign

## Overview
Accessibility4all is a Python/tkinter accessibility hub (`hub.py`) that launches four
feature processes, each of which opens its own always-on-top tkinter window
(`features/<name>/main.py`). This handoff redesigns:

1. **The hub window** — one uniform list of four features, large targets, plain language.
2. **The four feature windows** — reduced to small, inconspicuous desktop *bubbles*
   that show live state only.
3. **A new home for settings and instructions** — moved out of the bubbles and into
   the hub (per-feature settings sheet + a step-through walkthrough popup).

Goals, in the user's words: easy to read, easy to click, few buttons, no tiny controls,
no instructions cluttering the always-on-top windows.

## About the design files
The files in `design/` are **design references written as HTML** (Design Components —
each `.dc.html` opens directly in a browser; `support.js` is their runtime and is NOT
part of the product). They are prototypes of look and behavior, **not code to port**.

The target codebase is **Python 3 + tkinter** (see `hub.py`, `features/*/main.py`,
`shared/`). Implement these designs there, using tkinter Canvas-drawn widgets in the
style already established in `hub.py` (`_rounded_rect_points`, `PillButton`,
`FeatureCard` all draw rounded shapes on a `tk.Canvas` — follow that pattern rather
than introducing ttk themes or a new UI toolkit). Where a design calls for something
tkinter can't do natively (blurred translucency, CSS animation), approximate with a
solid dark fill and a `self.after()` frame loop.

## Fidelity
**High fidelity.** Colors, type sizes, control sizes, spacing and copy in this document
are final and should be matched. Every hex value, px size and string below appears in
the design files.

---

## Screens / views

### 1. Hub window — `design/hub_redesign.dc.html`
Replaces the current `hub.py` window (currently 580×620, `PALETTE_NORMAL`).

**Purpose:** turn features on and off. Nothing else.

**Layout** (window 800px wide, height fits content, min 640px — must grow with text size,
never scroll the list):
- Title bar: 40px, 1px bottom border `#1f232b`.
- Header block: padding `36px 44px 24px`, column gap 8px.
  - `Accessibility4all` — 34px, weight 600, letter-spacing -0.5px, `#f4f6fa`.
  - `Turn on the help you need.` — 19px, `#b6becd`.
- Feature list: padding `0 44px 8px`, vertical `gap: 14px`, four rows. **Do not scroll.**
- Footer: padding `18px 44px 22px`, 1px top border `#1f232b`, space-between.
  - Left: `N of 4 features on` — 16px, `#7d859a`.
  - Right: label `Text size` (16px `#7d859a`) + two 52×46px buttons `A−` (18px) and
    `A+` (22px), radius 12px, 2px border `#2e333e`, bg `#191c23`, hover border `#6f9bff`.

**Feature row** (the only interactive element; the whole row is the hit target):
- padding `24px 26px`, radius 16px, bg `#191c23`, 2px border, `gap: 24px`.
- Border `#4ad991` when on, `#262a33` when off. Hover bg `#1d212a`.
- Name — 24px, weight 600, letter-spacing -0.2px, `#f4f6fa`.
- Description — 18px, line-height 1.4, `#b6becd`, `text-wrap: pretty`.
- Right side: word `On`/`Off` (18px, weight 600, `#4ad991` / `#7d859a`, min-width 36px,
  right-aligned) then the switch.
- Switch: 76×42px, radius 21px, 4px padding, 2px border; track `#4ad991` (on) /
  `#3a4051` (off), border `#4ad991` / `#4a5165`; knob 30×30px white, offset 0 → 30px.
- Row order and copy (rewritten from the `feature.json` descriptions — plain language):
  1. **Voice Control** — "Hold the ` key and tell Chrome what to do."
  2. **Page Reader** — "Reads what's on your screen out loud."
  3. **Tone & Social Cues** — "Explains the tone behind a message you highlight."
  4. **Dyslexia Font** — "Swaps websites to an easier-to-read font."

**Removed from the current hub, deliberately:** the high-contrast pill, "Rescan
features", per-feature version/author metadata, the status glyph + word
(Running/Stopped/Crashed/Failed), the left accent bar, per-feature emoji icons, and the
scroll area. `On`/`Off` plus the green border carries state (still redundant with color).
Keep `discover_features()` and the crash/exit handling in `_poll` — surface a crash by
flipping the row back to Off and showing a short line in the footer instead of a status
glyph.

### 2. Sub-UIs as desktop bubbles — `design/sub_ui_bubbles.dc.html`
Replaces the four feature windows entirely. Each is a small, borderless, always-on-top
window with **live state only — no settings, no shortcut rows, no instruction text.**
All sit bottom-center of the screen (Tone is the exception: it appears near the
selection). Shared bubble styling: bg `rgba(18,20,25,0.92)`, 1px border `#2e333e`,
radius 999px, shadow `0 12px 34px rgba(0,0,0,0.55)`, text `#dfe4ec` at 17px.

**Voice Control bubble** (replaces the 300×360 window)
- Resting: 60px tall, `padding: 0 10px` — just a 40px circle (bg `#262a33`) with the mic
  mark: an 8×14px capsule (radius 4) above a 14×3px bar, `#9aa3b4`.
- Listening: `padding: 0 22px 0 10px`, border `#6f9bff`, circle bg `#2b3a63`, mark
  `#eaf0ff`, a pulsing ring (2px `#6f9bff`, scale 1 → 1.6, opacity 0.5 → 0, 1.6s),
  then a 9-bar waveform (4px wide bars, 26px tall, `#8ab4ff`, each bar scaleY 0.2 → 1,
  0.6–1.0s, staggered 0.05s) and the text `Listening…`.
- Done: circle bg `#1f3a2c`, mark `#8ff0bb`, text = what happened (e.g. `Opened a new tab`),
  then it collapses back to resting on its own.
- Keep the existing `` ` `` push-to-talk listener and the click-and-hold fallback on the dot.
  The chat transcript, audio meter, "Always on" checkbox and the "hold ` to talk" hint all
  move out (transcript → drop it; Always on → hub settings sheet; hint → walkthrough).

**Page Reader bubble** (replaces the 300×248 window)
- 64px tall pill, `padding: 0 12px`, content `gap: 16px`.
- One 48×48px circular button, bg `#2b3a63`, 2px border `#6f9bff`: two 6×18px bars
  (radius 2, `#eaf0ff`, gap 5px) when reading; a play triangle (15px left border
  `#eaf0ff`, 9px transparent top/bottom) when paused.
- Right of it, a 260px column: the line being read (17px, single line, ellipsis) and a
  5px progress bar — track `#2b3038`, fill `#6f9bff`, radius 3px.
- The three option checkboxes and both hotkey rows move to the hub settings sheet.

**Tone & Social Cues bubble** (replaces the 320×260 window *and* the 520×600 panel)
- 400px wide card near the selection, radius 16px, padding `20px 22px`, gap 12px,
  bg `rgba(18,20,25,0.95)`, 1px border `#2e333e`, shadow `0 16px 40px rgba(0,0,0,0.6)`.
- Tone chip: 17px weight 600, padding `6px 14px`, radius 999px, bg `#3a2f1c`,
  1px border `#7a6134`, text `#ffd68a`.
- Answer: 19px, line-height 1.45, `text-wrap: pretty` — two sentences maximum, written
  plainly (no cue-type names, no quote/interpretation/confidence structure).
- One dismiss button `Got it`: 52px tall, radius 12px, 2px border `#2e333e`,
  bg `#1e222a`, 18px text, hover border `#6f9bff`.
- The panel-font-size spinbox, the Shift+Click checkbox and the hotkey row move to the hub.

**Dyslexia Font bubble** (replaces the 430×430 window)
- No controls at all: a 52px pill, `padding: 0 20px`, gap 12px — a 12px `#4ad991` dot and
  the text `Easier font on`. Show it briefly when the feature turns on, then hide.
- Font choice, letter spacing, line height, preview and the Windows substitution buttons
  all move to that feature's hub settings sheet (see `feature_windows_full_panels.dc.html`
  for the full-size treatment of those controls).

### 3. Walkthrough popup (in the hub) — in `design/sub_ui_bubbles.dc.html`
Modal over the hub, covering everything below the title bar: scrim
`rgba(6,7,10,0.72)`, 28px padding.
- Card: 560px wide, padding 32px, radius 20px, bg `#191c23`, 1px border `#2e333e`,
  shadow `0 24px 60px rgba(0,0,0,0.6)`, gap 20px.
- `Step 1 of 3` — 15px `#7d859a`.
- Title — 28px weight 600, letter-spacing -0.3px.
- Body — 20px, line-height 1.5, `#dfe4ec`.
- Progress dots — 10px circles, `#6f9bff` active / `#343a46` inactive, gap 8px.
- Buttons row, gap 12px, both 60px tall, radius 12px:
  - `Back` — padding `0 24px`, 2px border `#2e333e`, bg `#1e222a`, 18px;
    text `#565e6e` when on step 1, else `#f4f6fa`.
  - `Next` (last step: `Got it`) — flex 1, bg `#2b3a63`, 2px border `#6f9bff`,
    20px weight 600, `#eaf0ff`.
- Voice Control copy (verbatim):
  1. "Hold the ` key and just talk" / "Press and hold the key to the left of the 1. Say
     something like “open a new tab” or “scroll down”, then let go."
  2. "A small dot shows it heard you" / "While you talk, a bubble at the bottom of the
     screen widens so you can see it's listening. It shrinks back on its own."
  3. "If it can't find something, say it differently" / "Try naming what you see on
     screen — “click the blue Sign in button” works better than “click sign in”."
  Write the same 3-step shape for the other three features.

### 4. Per-feature settings sheet (in the hub) — in `design/sub_ui_bubbles.dc.html`
480px wide sheet, radius 16px, bg `#14161b`, 1px border `#262a33`.
- Header: padding `22px 24px 18px`, 1px bottom border `#1f232b` — feature name 22px
  weight 600, right-aligned word `Settings` 17px `#7d859a`.
- Body padding `22px 24px`, gap 12px:
  - Option rows: padding `18px 20px`, radius 14px, bg `#191c23`, 2px border
    (`#4ad991` on / `#262a33` off), name 19px weight 600, `On`/`Off` 17px weight 600,
    then the same 76×42px switch as the hub rows.
  - Shortcut row: padding `16px 20px`, radius 14px, bg `#191c23`, 1px border `#262a33` —
    label `Shortcut key` 18px, the key in a chip (20px weight 600, padding `8px 16px`,
    radius 10px, bg `#21252e`, 1px border `#343a46`), and a 52px-tall `Change` button
    (padding `0 20px`, radius 12px, 2px border `#2e333e`, 17px, hover border `#6f9bff`).
  - Footer button `Show me how to use it` — 60px tall, radius 12px, 2px border `#2e333e`,
    bg `#1e222a`, 18px — opens the walkthrough.
- Page Reader option copy (rewritten from the source checkboxes):
  - "Let me pick sections by voice" (was "Voice-guided sections (needs Voice Control)")
  - "Read when I rest the pointer" (was "Hover-to-read (pause over text)")
  - "Skip the clutter" (was "Groq summary (important content only)")
- Real defaults from source: Page Reader `read_screen: F9`, `stop: F10`
  (`shared/feature_bus.py` → `DEFAULT_PAGE_READER_SETTINGS`); Tone Reader
  `analyze_selection: shift+command+'` (`features/tone_reader/settings.json`).

### 5. Full-size feature panels (optional) — `design/feature_windows_full_panels.dc.html`
An earlier, larger treatment of the same four features as full windows. Use it as the
reference for the **controls that moved into the hub**, in particular Dyslexia Font:
- Font picker: 2×2 grid of tappable cards, min-height 84px, padding `16px 18px`,
  radius 14px; selected bg `#2b3a63` + 2px `#6f9bff`, unselected `#191c23` + `#2e333e`.
  Each card renders its own name **in that typeface** (22px weight 600) plus a note
  (16px `#b6becd`): OpenDyslexic "Weighted bottoms", Atkinson Hyperlegible "Clearer
  letter shapes", Comic Sans MS "Informal, wide spacing", Arial "Plain and familiar"
  (`FONT_CHOICES`, `features/dyslexia_font/main.py`).
- Replace both `tk.Scale` sliders with 60×60px −/+ steppers (radius 12px, 2px border
  `#2e333e`, bg `#1e222a`, hover border `#6f9bff`; the bars inside are 22×4px `#f4f6fa`).
  "Space between letters" steps 0.01 in 0–0.12 em (default 0.03); "Space between lines"
  steps 0.05 in 1.0–2.2× (default 1.55). Values shown at 16px under a 19px label.
- Live preview: padding 20px, radius 14px, bg `#191c23`, 1px border `#262a33`, 20px text
  in the chosen font with the chosen spacing/line-height. Copy: "The quick brown fox
  jumps over the lazy dog. Reading should feel calm, not cramped."
- Page Reader primary action in the sheet: 84px tall, radius 16px — `Read this screen`
  (bg `#2b3a63`, 2px `#6f9bff`, 24px weight 600 `#eaf0ff`, play triangle) toggling to
  `Stop reading` (bg `#3a2226`, 2px `#ff8f8f`, `#ffdede`, two 8×26px bars).

### 6. Current state, for comparison — `design/current_state_reference.dc.html`
Pixel-accurate recreation of today's five windows (Voice Control 300×360, Page Reader
300×248, Tone 320×260 + its 520×600 panel, Dyslexia Font 430×430) built from the source
constants. Use it to confirm what's being replaced. Nothing here should be carried forward.

---

## Interactions & behavior
- **Hub row**: whole row toggles the feature (click, Space, Return — keep the existing
  `takefocus=1` + key bindings). Toggling starts/stops the subprocess exactly as
  `Hub._toggle` does today.
- **Text size A− / A+**: 0.85–1.6 in 0.15 steps (keep `FONT_SCALE_*` from `hub.py`), and
  the window must **grow** with the scale — the current implementation keeps a fixed
  geometry and clips rows. Recompute the window height after `_full_rebuild()`.
- **Voice bubble**: `` ` `` held (or dot press-and-hold) → listening; release →
  transcribing/acting; on completion show the one-line result for ~2s, then collapse.
  Expansion 62px → 256px wide; animate width over ~180ms with `after()` if practical.
- **Page Reader bubble**: the single button pauses/resumes. Progress reflects position
  through the captured text.
- **Tone bubble**: appears on hotkey or Shift+Click, positioned near the selection;
  `Got it`, Escape, or clicking away dismisses it. It never opens a second window.
- **Dyslexia bubble**: appears on enable, auto-hides after ~3s.
- **Walkthrough**: opens from the settings sheet, and automatically the first time a
  feature is switched on (persist a `seen_walkthrough` flag per feature id, alongside
  `hub_state.json`). Next advances, Back is disabled on step 1, `Got it` closes.
- **Focus**: every interactive element keeps a visible 2px `#6f9bff` focus ring — the
  current `FOCUS_RING` behavior in `hub.py` is correct, keep it.

## State
- `enabled: set[str]` and the subprocess map — unchanged (`hub_state.json`).
- `font_scale: float` — unchanged, but now also drives window height.
- New per-feature `seen_walkthrough: bool`.
- Per-feature settings keep their existing files (`features/*/settings.json`) and keys;
  only the UI that edits them moves into the hub.
- Bubble-local state: voice `idle | listening | done`, reader `reading | paused` + progress.
- Removed state: high-contrast palette, per-feature status kind
  (`running/stopped/crashed/failed`) as a displayed value, tone panel font size
  (replaced by three sizes: Small 18px / Medium 22px / Large 28px).

## Design tokens
Colors
- Window bg `#14161b`; card/row bg `#191c23`; row hover `#1d212a`; inset `#1e222a`;
  chip bg `#21252e`.
- Borders: `#262a33` (default), `#2e333e` (control), `#343a46` (chip), `#1f232b` (divider).
- Text: `#f4f6fa` primary, `#dfe4ec` bubble, `#b6becd` secondary, `#7d859a` muted,
  `#6f7789` caption, `#565e6e` disabled.
- Accent blue: `#6f9bff` border, `#2b3a63` fill, `#eaf0ff` on-fill, `#8ab4ff` waveform.
- On/green: `#4ad991`; off track `#3a4051`, off border `#4a5165`.
- Stop/red: `#ff8f8f` border, `#3a2226` fill, `#ffdede` text.
- Warm chip: bg `#3a2f1c`, border `#7a6134`, text `#ffd68a`.
- Bubble surface `rgba(18,20,25,0.92)`; scrim `rgba(6,7,10,0.72)`.

Type — Helvetica Neue / Helvetica (the codebase already uses Helvetica).
34 / 30 / 28 / 24 / 22 / 20 / 19 / 18 / 17 / 16 / 15px. **Nothing below 15px, and no
body copy below 17px.** Weights 400 and 600 only.

Spacing — 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 32, 36, 44px.
Radius — 10, 12, 14, 16, 20, 21 (switch), 999 (pill).
Shadows — `0 12px 34px rgba(0,0,0,0.55)` (bubble), `0 24px 60px rgba(0,0,0,0.55)` (sheet),
`0 30px 80px rgba(0,0,0,0.6)` (window).
Minimum target size — **48px**; primary actions 60–84px; the hub row is ~110px tall.

## Assets
No images or icon files. Every mark is drawn from rectangles and circles:
- Mic = a rounded capsule + a bar (+ a short stem in the large size).
- Play = a triangle; pause = two bars; plus/minus = 22×4px bars.
- Waveform = 9–13 rounded bars, animated vertically.
Two webfonts are referenced only by the Dyslexia Font picker/preview: **Atkinson
Hyperlegible** (Google Fonts) and **OpenDyslexic** (jsDelivr `@fontsource/opendyslexic`).
In the tkinter build, use whichever of these is installed locally and keep the existing
"This feature is intended for Windows." / "<font> appears to be installed." status logic.

## Files
- `design/hub_redesign.dc.html` — hub window (open in a browser; rows and A−/A+ are live).
- `design/sub_ui_bubbles.dc.html` — the four bubbles, the walkthrough, the settings sheet.
- `design/feature_windows_full_panels.dc.html` — full-size versions of the moved controls.
- `design/current_state_reference.dc.html` — today's UI, recreated for comparison.
- `design/support.js` — runtime for the `.dc.html` previews only; not part of the product.

Source files these designs replace: `hub.py` (`PALETTE_NORMAL`, `PALETTE_HIGH_CONTRAST`,
`FEATURE_ICONS`, `STATUS_STYLE`, `PillButton`, `FeatureCard`, `Hub._build_chrome`),
`features/voice_control/main.py` (`App._build_ui`, lines ~1327–1470),
`features/page_reader/main.py` (`PageReaderApp._build_ui`, ~402–466),
`features/tone_reader/main.py` (`ToneReaderApp._build_ui` ~261–323, `_ensure_panel`
~638–700), `features/dyslexia_font/main.py` (`DyslexiaFontApp._build_ui`, ~286–411).
