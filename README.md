# Accessibility4all

A modular accessibility hub for **macOS and Windows**. Toggle assistive features on and off depending on what you need.

## Quick start

```bash
python3 -m venv .venv
# macOS/Linux:  source .venv/bin/activate
# Windows:      .venv\Scripts\activate

pip install -r requirements.txt
echo "GROQ_API_KEY=your_key_here" > .env
python hub.py
```

## Features

| Feature | Description |
|---------|-------------|
| **Voice Control** | Hold-to-talk commands for Google Chrome (shortcuts + OCR + AI) |
| **Page Reader** | Read on-screen text aloud (hotkeys, voice, click-to-read) |

Restart the hub after adding a new feature folder so it is discovered.

## Platform notes

### macOS
- Install Tesseract: `brew install tesseract`
- Grant **Accessibility**, **Screen Recording**, and **Microphone** to Terminal/your IDE for voice control and page reader
- Voice Control uses the `` ` `` (backtick) key for push-to-talk when Accessibility is granted

### Windows
- Install [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki)
- Google Chrome must be installed for Voice Control
- Voice Control uses `` ` `` via pynput, or the on-screen mic dot

## Troubleshooting

**A feature's toggle flips straight back to off.** The hub footer now says why —
either a missing key (`... needs GROQ_API_KEY`) or the last error the feature
printed. The same line is in the terminal, prefixed with the feature id.

**Nothing happens when a feature starts, and the hub seems frozen.** After a
crash, macOS puts up a "reopen windows?" alert that blocks the app before it
draws anything. Silence it for the Python interpreter:

```bash
defaults write org.python.python ApplePersistenceIgnoreState -bool YES
```

**Features die instantly with `exit code -5` / `Trace/BPT trap: 5`.** pynput
reads the keyboard layout from HIToolbox on its listener thread, which recent
macOS builds refuse outside the main dispatch queue. `shared/pynput_darwin.py`
works around it and every feature calls it at import; if you add a feature that
uses pynput, call `pynput_darwin.install()` from the main thread before starting
any listener.

See [CLAUDE.md](CLAUDE.md) for architecture details and [features/README.md](features/README.md) for adding features.
