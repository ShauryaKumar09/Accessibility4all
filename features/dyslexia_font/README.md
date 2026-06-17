# Dyslexia Font

Applies dyslexia-friendly font settings to websites in Google Chrome and offers
an opt-in Windows font substitution helper for some desktop apps.

This feature is Windows-focused. Website font changes are handled by a Chrome
extension, because browser extension APIs are more reliable than Python screen
automation for changing page text.

## Setup

1. Install shared project dependencies from the repo root:
   ```bash
   pip install -r requirements.txt
   ```

2. Run the hub:
   ```bash
   python hub.py
   ```

3. Toggle **Dyslexia Font** ON.

4. In the Dyslexia Font window, click **Open extension folder**.

5. In Chrome, open `chrome://extensions`, turn on **Developer mode**, click
   **Load unpacked**, and select:
   ```text
   features/dyslexia_font/chrome_extension
   ```

6. Click the puzzle-piece Extensions button in Chrome and pin
   **Accessibility4all Dyslexia Font** if you want it visible in the toolbar.

## Website Settings

After the extension is loaded, open its **Details** page in Chrome and choose
**Extension options**. You can change:

- enabled/disabled
- font stack
- letter spacing
- line height
- font weight

The default font stack tries `OpenDyslexic`, then `Atkinson Hyperlegible`, then
`Comic Sans MS`, then Arial. Install your preferred font in Windows for best
results.

## Windows App Font Substitution

The Windows section in the feature window can check whether a selected font is
installed. It also includes an experimental opt-in substitution helper.

Important limits:

- It writes only after you confirm.
- It stores a backup in this feature folder.
- Use **Restore Windows fonts** to remove substitutions made by this feature.
- Some apps ignore Windows font substitution because they bundle fonts, render
  custom UI, or run inside webviews.
- You may need to sign out or restart Windows before changes are visible.

The website extension is the recommended path. Use Windows substitution only if
you understand the restore path.
