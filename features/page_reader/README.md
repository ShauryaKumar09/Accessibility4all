# Page Reader

Reads on-screen text aloud using OCR + text-to-speech. Works on **macOS** and **Windows**.

## Setup

1. Install shared deps from the project root:
   ```bash
   pip install -r requirements.txt
   ```

2. Install **Tesseract OCR**:

   **macOS**
   ```bash
   brew install tesseract
   tesseract --version
   ```

   **Windows**
   - Download: https://github.com/UB-Mannheim/tesseract/wiki
   - Install to `C:\Program Files\Tesseract-OCR` (added to PATH automatically by the installer)
   - Verify: `tesseract --version`

3. Optional: add `GROQ_API_KEY` to the project `.env` for **Voice-guided sections**.

## Permissions

**macOS** — System Settings → Privacy & Security:
- **Accessibility** — global hotkeys and hover-to-read
- **Screen Recording** — OCR screenshots

**Windows** — allow microphone/screen access if prompted. Global hotkeys may require running the terminal as administrator if they do not register.

## Usage

Toggle **Page Reader** ON in the hub (`python hub.py`).

| Action | How |
|--------|-----|
| Read Chrome page | Default hotkey **F9** (focuses Chrome, reads only the browser window) |
| Stop speaking | Default hotkey **F10** |
| Read a section by voice | Voice Control ON + “Voice-guided sections” → e.g. “read the billing information” |
| Read last section | “read that again” (via Voice Control) |
| Hover-to-read | Turn on "Read when I rest the pointer" in the hub, then pause the mouse over text (~0.5s) |
| Pause / resume | The round button on the bubble |

While reading, a small bubble sits at the bottom of the screen showing the line
being read and how far through it is. That is its whole UI — one button.

## Settings

All settings live in the **hub**: open Accessibility4all and press **Settings**
on the Page Reader row. They are saved to `settings.json` in this folder and the
running feature picks up changes within a second.

- "Let me pick sections by voice" (voice-guided sections)
- "Read when I rest the pointer" (hover-to-read)
- "Skip the clutter" (Groq summary — important content only)
- Start reading / Stop reading shortcut keys (Ctrl/Command, Alt/Option, Shift,
  and function keys)
- Speech rate (`tts_rate` in `settings.json`)

Press **Show me how to use it** in the same sheet for a three-step walkthrough.
