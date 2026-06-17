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

Click **Rescan features** in the hub after adding a new feature folder.

## Platform notes

### macOS
- Install Tesseract: `brew install tesseract`
- Grant **Accessibility**, **Screen Recording**, and **Microphone** to Terminal/your IDE for voice control and page reader
- Voice Control uses the `` ` `` (backtick) key for push-to-talk when Accessibility is granted

### Windows
- Install [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki)
- Google Chrome must be installed for Voice Control
- Voice Control uses `` ` `` via pynput, or the on-screen mic dot

See [CLAUDE.md](CLAUDE.md) for architecture details and [features/README.md](features/README.md) for adding features.
