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
- **Accessibility** — global hotkeys and click-to-read
- **Screen Recording** — OCR screenshots

**Windows** — allow microphone/screen access if prompted. Global hotkeys may require running the terminal as administrator if they do not register.

## Usage

Toggle **Page Reader** ON in the hub (`python hub.py`).

| Action | How |
|--------|-----|
| Read entire screen | Default hotkey **F9** (configurable in the Page Reader panel) |
| Stop speaking | Default hotkey **F10** |
| Read a section by voice | Voice Control ON + “Voice-guided sections” → e.g. “read the billing information” |
| Read last section | “read that again” (via Voice Control) |
| Click-to-read | Enable in settings, then click any text |

No on-screen read/stop buttons — use hotkeys or voice.

## Settings

Saved to `settings.json` in this folder (created on first run):

- Voice-guided sections
- Click-to-read
- Custom hotkeys (supports Ctrl/Command, Alt/Option, Shift, and function keys)
