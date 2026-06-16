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

The hub window lists every feature with an **OFF/ON** toggle. Toggle one ON and
it launches as its own process; toggle OFF and it's stopped.

> **Run from a terminal while developing** — each feature's logs print to the
> hub's terminal, which is how you debug.

### macOS permissions (required)

The voice feature controls the mouse/keyboard and reads the screen. Grant these
in **System Settings → Privacy & Security**, for whichever app runs Python
(Terminal, iTerm, or your IDE):

- **Accessibility** — needed for clicks and keystrokes (`pyautogui`).
- **Screen Recording** — needed for screenshots/OCR.
- **Microphone** — needed for voice capture.

If clicks/hotkeys "do nothing," Accessibility permission is almost always the
cause.

---

## Architecture

```
hub.py                  ← MAIN ENTRY POINT: the toggle launcher (tkinter)
hub_state.json          ← auto-created; remembers which toggles were on
requirements.txt        ← shared deps for the hub + all features
.env                    ← secrets (GROQ_API_KEY); git-ignored, never committed
features/
├── README.md           ← the feature-developer contract (READ THIS to add one)
├── _template/          ← copy-me starter (folders starting with _ are hidden)
└── voice_control/      ← the first real feature (voice → Chrome control)
    ├── feature.json    ← manifest (name, description, entry, version, author)
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
  (that's why `_template/` never appears). Use the **Rescan features** button to
  pick up folders added while the hub is running — no hub code changes needed.

This process-per-feature model is deliberate: it lets separate developers own
separate folders without merge conflicts, and it's required because GUI features
(like voice control) each need their own tkinter main loop.

---

## Adding a feature (for collaborators)

1. Copy `features/_template/` to `features/your_feature/`.
2. Edit `feature.json` (`name`, `description`, `entry`, `version`, `author`).
3. Replace `main.py` with your code. It **must be runnable on its own**
   (`python features/your_feature/main.py`) with cwd = your folder.
4. List any new deps in `requirements.txt`.
5. Run `python hub.py`, click **Rescan features**, toggle your feature on.

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
     bar", "search for X", "go to youtube.com" → focus the field, **paste** the
     text (Cmd+V, reliable on macOS), optionally press Enter. Spoken URLs like
     "youtube dot com" are normalised to "youtube.com".
   - **Shortcut fast-path** (`match_shortcut` / `CHROME_SHORTCUTS`): new/close/
     reopen tab, reload, find, zoom, scroll, bookmarks, etc. → fire the hotkey,
     no screenshot/AI.
   - **Click by title** (`match_click_target`): for "click on the video titled X"
     / "click on X", the spoken title is fuzzy-matched directly against the
     on-screen OCR text (deterministic) and that element is clicked. More reliable
     than asking the model to count items ("third video"). For **video** clicks it
     aims at the **thumbnail** (above the title, via `_thumbnail_point`) so it
     opens the video, never the channel name; "click a video" with no title clicks
     the first plausible video's thumbnail.
   - **Vision fallback**: anything else / no confident title match →
     screenshot → `pytesseract` OCR → send the **numbered** element list to Groq
     → it returns the element **index** → we look up the verified coordinate.
   Each vision step takes a **fresh** screenshot, and the loop **waits** after a
   page-changing step (`_changes_page`) so later clicks see the new screen.
6. **Execute** — `pyautogui` performs the hotkey / click / scroll / type, or a
   `sequence` of those.

Every run logs each step to the terminal with timings, and appends a JSON line to
`features/voice_control/trials.jsonl` (`commands`, per-step `method` =
`"shortcut"`/`"vision"`).

> **Why OCR text instead of sending the raw image to a vision model?** OCR gives
> the *exact* pixel box of each on-screen word, so clicks land precisely. Vision
> LLMs are unreliable at predicting precise click coordinates. The screenshot is
> still "sent to Groq" — just as a numbered element list rather than pixels.

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

### tkinter thread-safety (critical)

tkinter is **not** thread-safe; calling a widget from a non-main thread segfaults
on macOS. The audio callback runs on PortAudio's thread and must only append
frames + store a float `level`. Worker threads update the UI only via
`_set_status` / `_set_trial_info`, which wrap everything in `self.after(0, ...)`.

### Audio backend

Uses **`sounddevice`**, not PyAudio — PyAudio/PortAudio segfaulted on macOS
(Core Audio error -50). Do not switch the audio I/O back to PyAudio.

---

## Conventions

- One shared `.venv` and one `requirements.txt` for the whole project.
- Secrets go in `.env` (git-ignored). `load_dotenv()` finds it from any feature
  folder because it searches parent directories.
- Features don't share in-memory state (separate processes) — coordinate via
  files or another explicit channel if needed.
