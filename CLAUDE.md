# Accessibility4all

A modular accessibility assistant. The app is a **hub** that lets users toggle
individual assistive **features** on and off depending on their disability
(e.g. voice control today; eye tracking, switch access, etc. later). Each
feature is an isolated plugin so multiple people can build features in parallel.

This file is read by Claude Code and is the shared reference for all
collaborators. Keep it up to date when the architecture changes.

---

## Quick start

```bash
# 1. Create / activate the virtual env (Python 3.12)
python3 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Add your Groq API key (used by the voice feature)
echo "GROQ_API_KEY=your_key_here" > .env

# 4. Run the hub (the main entry point)
python hub.py
```

The hub window lists every feature as a large row with an **Off/On** switch.
Toggle one ON and it launches as its own process; toggle OFF and it's stopped.
**Settings** on a row opens that feature's settings and its walkthrough.

> **Run from a terminal while developing** — each feature's logs print to the
> hub's terminal, which is how you debug.

### macOS permissions (required)

The voice feature controls the mouse/keyboard and reads the screen. Grant these
in **System Settings → Privacy & Security**, for whichever app runs Python
(Terminal, iTerm, or your IDE):

- **Accessibility** — needed for clicks and keystrokes (`pyautogui`).
- **Screen Recording** — needed for screenshots/OCR.
- **Microphone** — needed for voice capture.
- **Automation → Google Chrome** — needed for the self-check/undo feature to
  read the active tab's URL (`plat.get_chrome_state`). Granted on first use via a
  one-time prompt; without it the self-check simply skips.

Page Reader also needs **Accessibility** (global hotkeys, click-to-read) and
**Screen Recording** (OCR).

On **Windows**, allow microphone access if prompted; install Tesseract and
Chrome for full functionality.

---

## Architecture

```
hub.py                  ← MAIN ENTRY POINT: toggles + every feature's settings
hub_state.json          ← auto-created; toggles, text size, walkthroughs seen
requirements.txt        ← shared deps for the hub + all features
.env                    ← secrets (GROQ_API_KEY); git-ignored, never committed
shared/
├── ui_kit.py           ← the design tokens (colours, type scale, min sizes)
├── webbubble.py        ← the floating bubble kit every feature's window uses
├── settings_store.py   ← per-feature settings.json + a change Watcher
├── hotkeys.py          ← hotkey strings, and capture from a tkinter key event
├── windows_fonts.py    ← Windows font substitution (hub + dyslexia_font)
├── feature_bus.py      ← file-based IPC (commands, presence)
├── screen_ocr.py       ├── platform.py  ├── groq_vision.py  ├── console.py
features/
├── README.md           ← the feature-developer contract (READ THIS to add one)
├── _template/          ← copy-me starter (folders starting with _ are hidden)
└── voice_control/      ← the first real feature (voice → Chrome control)
    ├── feature.json    ← manifest (name, description, entry, version, author)
    ├── settings.json   ← this feature's settings; the HUB writes them
    ├── bubble.py       ← the floating compact bar (a transparent web view)
    └── main.py         ← runnable entry point
```

**How it works:** the hub auto-discovers every folder under `features/` that has
a `feature.json` (or at least a `main.py`). Each feature runs as its **own OS
process** (`subprocess.Popen([python, entry], cwd=feature_dir)`):

- **Toggle ON** → launch the process. **Toggle OFF** → terminate it (force-kill
  after 3s).
- **Isolation:** a feature crash can't take down the hub or other features. If a
  feature exits on its own, the hub flips its toggle to OFF and shows
  `crashed (exit N)` (liveness polled every 1s).
- **Persistence:** enabled toggles are saved to `hub_state.json` and auto-started
  next launch.
- **Discovery rules:** folders named with a leading `_` or `.` are ignored
  (that's why `_template/` never appears). Known features are listed in a fixed
  order (`FEATURE_ORDER`) and anything new is appended — no hub code changes
  needed, but restart the hub to pick up a folder added while it was running
  (the Rescan button was removed in the redesign).

This process-per-feature model is deliberate: it lets separate developers own
separate folders without merge conflicts, and it's required because GUI features
(like voice control) each need their own GUI main loop.

### Where the UI lives

Everything the user sees is HTML/CSS/JS in a pywebview window. The split is
**one hub window (a full app shell) and one small floating bubble per running
feature (live state only)** — the design's "settings live in the hub, the
desktop only gets a bubble".

- **Hub window** (`hub.py` + `hub_ui/index.html` + `app.js` + `style.css`) —
  a pywebview window, not tkinter. `hub.py`'s `Api` class is the only bridge
  between Python and the page: feature discovery, subprocess start/stop/poll,
  and reading/writing `features/<id>/settings.json` all live there and get
  called from JS via `window.pywebview.api.*`. Left sidebar lists every
  feature (label navigates to its page, its own switch toggles the process —
  separate hit targets). Each feature page has a title row (name, hotkey
  chips, on/off switches, each with an info-circle that opens a dimmed
  popup — never a second OS window), a bespoke settings panel for anything
  that isn't a boolean (Dyslexia's font picker/steppers, Color Blind's
  filter-type buttons, Focus Mode's block-list/duration), and a two-column
  body: numbered clickable instructions on the left, a placeholder panel on
  the right reserved for a future preview. `hub_ui/style.css`'s CSS custom
  properties mirror `shared/ui_kit.py`'s `C` dict 1:1 so the hub and the
  bubbles read as one product. This is why `pywebview` and
  (on macOS) extra `pyobjc-framework-Cocoa`/`pyobjc-framework-WebKit` are in
  requirements.txt.
- **Feature bubbles** (`shared/webbubble.py`) — each running feature is its
  own OS process showing one small transparent, frameless, always-on-top
  window with live state and at most one big action: no settings, no shortcut
  rows, no instructions. `Bubble` is the window (give it a body, CSS, JS and a
  size; drive it with `call` / `set_text` / `set_style` / `show` / `hide` /
  `move`), `Scheduler` is the `after()` / `after_cancel()` that replaced Tk's
  main loop, and `BASE_CSS` is the shared look — tokens generated from
  `shared/ui_kit.py`'s `C` dict plus the pieces every bubble is built from:
  `.pill`, `.card`, `.dot`, `.label`, `.chip`, `.circle`, `.track`, `.btn`.
  Build a new bubble out of those rather than inventing a second visual
  language. Minimum target size is 48px and body copy never below 17px.
- **Why the bubbles are web views and not `tk.Canvas`** — a bubble has to
  *float*: a capsule or rounded card with a drop shadow over the user's real
  work, so everything outside its shape must be see-through. tkinter on macOS
  cannot do that — `wm attributes -transparent` is accepted and then paints
  the whole window solid black (checked on Tk 9.0.3), which left every pill
  inside a visible rectangle. As HTML the shape is `border-radius`, motion is
  CSS transitions and keyframes running on the compositor (no Python frame
  loops), and a state change is one `evaluate_js` of about a millisecond.
  `shared/ui_kit.py` is now just the token vocabulary — its canvas widget kit
  went when the last canvas bubble did.
- Settings themselves still live in `features/<id>/settings.json` via
  `shared/settings_store.py`; the running feature notices a hub-made edit
  through `settings_store.Watcher` (polled ~700ms) and re-applies without a
  restart — unchanged by the pywebview move, since that file format was
  always UI-agnostic.

One habit survives from the canvas era: colours that are `rgba()` in the
handoff are pre-blended against the surface behind them in `C`, so a token is
a plain colour usable from either the hub's stylesheet or a bubble's.

---

## Adding a feature (for collaborators)

1. Copy `features/_template/` to `features/your_feature/`.
2. Edit `feature.json` (`name`, `description`, `entry`, `version`, `author`).
3. Replace `main.py` with your code. It **must be runnable on its own**
   (`python features/your_feature/main.py`) with cwd = your folder.
4. List any new deps in `requirements.txt`.
5. Run `python hub.py` and toggle your feature on. Add an entry to
   `FEATURE_DATA` in `hub.py` (name, description, `options`/`shortcuts` with
   info-popup text, and 3-4 numbered `instructions`) so your feature reads
   like the rest; without it the hub falls back to your `feature.json`
   description and shows no settings/instructions.

Full contract (signals, logging, shared state, gotchas) is in
**`features/README.md`**. Read it before building.

---

## The `voice_control` feature

Hold-to-talk voice commands that control Google Chrome.

**Pipeline** (`features/voice_control/main.py`, `_process`):

1. **Record** — push-to-talk via `sounddevice` (hold the button, release to send).
2. **Transcribe** — Google STT through `SpeechRecognition` (15s timeout).
3. **Focus Chrome** — `osascript` brings Chrome to the foreground.
4. **Split into sub-commands** — `split_commands()` breaks one utterance into
   stacked steps on "then" / "and then" / "and &lt;verb&gt;" / ", &lt;verb&gt;", so
   *"open a new tab, go to youtube.com, and then click the first video"* runs as
   three commands. "search for cats and dogs" stays one command.
5. **Per sub-command, resolve + execute** (`_run_subcommand`):
   - **Typing / search / navigation** (`_match_typing`): "type X into the search
     bar", "go to youtube.com" → focus the field, **paste** the text (Cmd+V,
     reliable on macOS), optionally press Enter. Spoken URLs like "youtube dot
     com" are normalised to "youtube.com".
   - **Context-aware search** (`_match_typing` → `site_search` → `find_search_box`):
     a bare "search X" / "search up X" / "look up X" searches the **current
     page's own search box** so the user stays on the site — e.g. on Amazon,
     "search up clothing" types into Amazon's bar, not the address bar. We take a
     screenshot, locate the page's search field from OCR placeholder text
     (`find_search_box`: a short "Search"/"Search <site>" line up top), click it,
     then paste + Enter. When **no** on-page box is found (e.g. a blank tab) it
     falls back to a Google search in the address bar. Saying "google X" /
     "search the web for X" / "search X in the address bar" **forces** the
     omnibox Google search.
   - **Raw input control** (`_match_input_control`, checked FIRST in
     `_run_subcommand`): literal manual control for when smart targeting can't
     find the right thing — "press enter" / "press escape" / "press the down
     arrow" (presses that key), a **bare** "click" / "double click" / "right
     click" (clicks wherever the cursor already is, `click_here`), and "move the
     pointer up/down/left/right [a little | a lot | by N]" (`move`, relative
     nudge). Deterministic, never screenshots or calls the model. Checked before
     the click-intent skip so "press …"/"click" aren't sent down the element-
     targeting path; "click on X" / "press the blue button" still target normally
     because the trailing word isn't a bare click / known key.
   - **Shortcut fast-path** (`match_shortcut` / `CHROME_SHORTCUTS`): new/close/
     reopen tab, reload, find, zoom, scroll, bookmarks, etc. → fire the hotkey,
     no screenshot/AI.
   - **Click by title** (`match_click_target`): for "click on the video titled X"
     / "click on X", the spoken title is fuzzy-matched directly against the
     on-screen OCR text (deterministic) and that element is clicked. More reliable
     than asking the model to count items ("third video"). Priority: title match →
     ordinal → results-tab/category → generic "a video". Title matching is tried
     FIRST and (for videos) only against plausible video-title lines, so saying
     "click on the video …" can't be hijacked into a "Videos" tab. Confidence
     needs the exact phrase or most spoken words, so a single shared word won't
     pick the wrong video. For **video** clicks it aims at the **thumbnail** above
     the title (`_thumbnail_point`, height-clamped so it never overshoots into the
     tabs or another row), so it opens the video, never the channel name. Click
     commands ("click/tap/select …") also skip the shortcut table so a title word
     like "reload" can't fire a hotkey.
   - **Vision fallback for clicks** (`ask_groq_vision`): when a click can't be
     matched locally, the **actual screenshot** is sent to a Groq **vision** model
     (`llama-4-scout`) with a numbered red box drawn over each OCR element. The
     model SEES the page and replies one of two ways: `{"index": N}` → we click
     box N's **verified OCR coordinate** (precise; thumbnail offset still applies
     for videos); or `{"point": [x%, y%]}` → for purely-**visual** targets that
     OCR can't see (images, icons, avatars, coloured tiles like a Netflix profile
     square — "click the green profile"), the model points at the centre as
     percentages and we click there. Text stays pixel-precise via OCR; visual
     targets are reachable at all via the point path.
   - **Text fallback** (`ask_groq`): non-click commands (scroll/type into a field)
     send the numbered element **text** list to the text model for an index.
   Each vision step takes a **fresh** screenshot, and the loop **waits** after a
   page-changing step (`_changes_page`) so later clicks see the new screen.
6. **Execute** — `pyautogui` performs the hotkey / click / scroll / type, or a
   `sequence` of those.
7. **Self-check + undo** (`_verify_outcome` / `_restore_page`): when the command
   clearly named a site ("search paper **on Amazon**", "go to **youtube.com**"),
   after running we read Chrome's active-tab URL/title (`plat.get_chrome_state`,
   AppleScript on macOS) and confirm the named site is there. If a wrong command
   opened a stray tab or landed on the wrong site, we apologise in the transcript
   and **go back** — closing extra tabs, then re-navigating to the page we were on
   before (`self._undo_target`). Verification is **conservative**: it only fires
   when a site is named as a destination (so "search for amazon rainforest" — a
   topic, not a place — never triggers it). The user can also say **"that's
   wrong, go back"** / "undo that" / "you did that wrong" (`_is_undo_request`) to
   revert manually; plain "go back" stays the normal browser-back shortcut.

Every run logs each step to the terminal with timings, and appends a JSON line to
`features/voice_control/trials.jsonl` (`commands`, per-step `method` =
`"shortcut"`/`"vision"`).

> **Why click a verified OCR coordinate, not a model-guessed pixel?** Vision LLMs
> are unreliable at predicting *exact* click pixels. So even when we send the real
> image (click fallback), we overlay numbered boxes from OCR and have the model
> pick a box number — then click that box's measured coordinate. The model gets to
> see the page; the click stays pixel-precise.

### Two things that are easy to get wrong (don't regress these)

- **Retina coordinate scaling.** `pyautogui.screenshot()` returns PHYSICAL pixels
  (e.g. 2704×1756) but `pyautogui.click()` uses LOGICAL coordinates (1352×878).
  OCR coords are scaled by `pyautogui.size() / screenshot.size` before use. Skip
  this and every click lands in the wrong place.
- **Clicks use element INDICES, not model-invented coordinates.** The model is
  shown a numbered element list and returns `{"action":"click","index":N}`; we
  resolve `N` to the verified coordinate (`_resolve_click_index`). This is the
  fix for "it clicks randomly" — the model can only point at elements it was
  actually shown. An out-of-range / `-1` index raises a clear "no match" error
  instead of clicking somewhere random.

### Threading (critical)

The audio callback runs on PortAudio's thread and must only append frames +
store a float `level` — never touch anything else. Worker threads narrate
through `_set_status` / `_set_trial_info`, which push a closure onto `_ui_q`;
`_drain_ui` runs it.

Everything that used to be `self.after(...)` on a Tk main loop now runs on
`webbubble.Scheduler` — one daemon thread executing timers in order, so the
poll loops (`_poll_keys`, `_poll_meter`, `_drain_ui`) and the idle-reset timer
keep their single-threaded guarantees. `App.after` / `App.after_cancel` are the
same call sites as before; only the loop underneath changed. This applies to
every feature, not just this one: page_reader's hotkey/hover callbacks and
tone_reader's listener threads all still hand work over with `after(0, ...)`.
Bubbles are safe to update from any thread — each setter is one `evaluate_js`
call (~1ms) that pywebview marshals onto its own UI thread.

### Audio backend

Uses **`sounddevice`**, not PyAudio — PyAudio/PortAudio segfaulted on macOS
(Core Audio error -50). Do not switch the audio I/O back to PyAudio.

---

## The `page_reader` feature

Reads on-screen text aloud (OCR + TTS). See `features/page_reader/README.md`.

- **Read screen** — default `F9` hotkey (user-configurable)
- **Stop** — default `F10`
- **Read by voice** — with Voice Control on, a bare “read” (or “read this”, “read
  the page”, “start reading”) reads the screen now. `match_read_command` strips
  spoken lead-ins/tails (“okay, yeah, read this please”) before matching, and
  such short commands skip voice_control's `_should_process_command` length
  filter — without that, “read” (4 chars) was thrown away as filler.
- **Voice-guided sections** — when Voice Control is also on, say e.g. “read the billing information” (uses Groq)
- **Click-to-read** — optional toggle; click any line to hear it

Two rules keep reading prompt rather than mysteriously late:

- **The bus listener starts at the END of `commands.jsonl`** (`feature_bus.current_offset()`)
  and drops entries older than `STALE_COMMAND_S`. Starting at offset 0 replayed
  every command ever written, so a read queued in an earlier session fired the
  moment the feature was next toggled on.
- **`Speaker` synthesizes on one thread and plays on another**, one chunk ahead,
  and `groq_vision.script_to_lines` keeps the FIRST chunk short. Speech is per
  chunk, so a single long chunk meant seconds of silence before anything was
  heard, plus a synth-sized gap between every chunk.

Features coordinate via `feature_bus/commands.jsonl` and `feature_bus/presence.json`
at the project root. Shared OCR lives in `shared/screen_ocr.py`; cross-platform
helpers (Chrome focus, paste, shortcuts) live in `shared/platform.py`.

---

## Conventions

- One shared `.venv` and one `requirements.txt` for the whole project.
- Secrets go in `.env` (git-ignored). `load_dotenv()` finds it from any feature
  folder because it searches parent directories.
- Features don't share in-memory state (separate processes) — coordinate via
  files or another explicit channel if needed.
