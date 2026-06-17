"""Cross-platform helpers for macOS and Windows."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

import pyautogui

IS_MAC = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"

_dpi_configured = False


def enable_dpi_awareness():
    """Use physical pixel coords on Windows so OCR and clicks align."""
    global _dpi_configured
    if _dpi_configured or not IS_WINDOWS:
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
    _dpi_configured = True

MAC_TESSERACT_PATHS = (
    "/opt/homebrew/bin/tesseract",
    "/usr/local/bin/tesseract",
)
WIN_TESSERACT_PATHS = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
)


def mod_key() -> str:
    """Primary modifier for browser shortcuts (command on Mac, ctrl on Windows)."""
    return "command" if IS_MAC else "ctrl"


def alt_key() -> str:
    """Secondary modifier (option on Mac, alt on Windows)."""
    return "option" if IS_MAC else "alt"


def normalize_hotkey_keys(keys: list[str]) -> list[str]:
    """Map Groq/action keys to what pyautogui expects on this OS."""
    out = []
    for k in keys:
        kl = k.lower()
        if kl in ("command", "cmd"):
            out.append(mod_key())
        elif kl == "option":
            out.append(alt_key())
        else:
            out.append(k)
    return out


def hotkey_action(*keys: str) -> dict:
    """Build a hotkey action; use 'mod' for the platform primary modifier."""
    resolved = [mod_key() if k == "mod" else k for k in keys]
    return {"action": "hotkey", "keys": resolved}


def paste_text(text: str):
    """Paste text via clipboard + mod+V (reliable on Mac and Windows)."""
    import pyperclip

    saved = None
    try:
        saved = pyperclip.paste()
    except Exception:
        pass
    pyperclip.copy(text)
    time.sleep(0.05)
    pyautogui.hotkey(mod_key(), "v")
    time.sleep(0.15)
    if saved is not None:
        try:
            pyperclip.copy(saved)
        except Exception:
            pass


def get_chrome_window_bounds(log_fn=None) -> tuple[int, int, int, int] | None:
    """Return (left, top, width, height) of the front Chrome window, or None."""
    def _log(msg: str, level: str = "INFO"):
        if log_fn:
            log_fn("CHROME", msg, level)

    try:
        if IS_MAC:
            result = subprocess.run(
                ["osascript", "-e",
                 'tell application "Google Chrome" to get bounds of front window'],
                check=True, capture_output=True, text=True, timeout=5,
            )
            parts = [int(p.strip()) for p in result.stdout.strip().split(",")]
            if len(parts) != 4:
                return None
            left, top, right, bottom = parts
            width, height = right - left, bottom - top
        elif IS_WINDOWS:
            import pygetwindow as gw

            wins = [w for w in gw.getAllWindows()
                    if w.title and "chrome" in w.title.lower() and w.visible]
            if not wins:
                return None
            w = wins[0]
            left, top, width, height = w.left, w.top, w.width, w.height
        else:
            return None
        if width < 50 or height < 50:
            return None
        _log(f"Chrome window {width}x{height} at ({left}, {top})")
        return left, top, width, height
    except Exception as e:
        _log(f"could not get Chrome bounds: {e}", "WARN")
        return None


def activate_chrome(log_fn=None) -> bool:
    """Bring Google Chrome to the foreground (best-effort)."""
    def _log(msg: str, level: str = "INFO"):
        if log_fn:
            log_fn("CHROME", msg, level)

    try:
        if IS_MAC:
            subprocess.run(
                ["osascript", "-e", 'tell application "Google Chrome" to activate'],
                check=True, capture_output=True, timeout=5,
            )
        elif IS_WINDOWS:
            import pygetwindow as gw

            wins = [w for w in gw.getAllWindows()
                    if w.title and "chrome" in w.title.lower() and w.visible]
            if not wins:
                subprocess.Popen(["chrome"], shell=True)
                time.sleep(1.0)
                wins = [w for w in gw.getAllWindows()
                        if w.title and "chrome" in w.title.lower() and w.visible]
            if wins:
                wins[0].activate()
            else:
                raise RuntimeError("no Chrome window found")
        else:
            _log(f"Chrome focus not implemented on {sys.platform}", "WARN")
            return False
        time.sleep(0.25)
        _log("Google Chrome activated (foreground)")
        return True
    except Exception as e:
        _log(f"could not activate Chrome: {e}", "WARN")
        return False


def configure_tesseract():
    """Point pytesseract at the binary if it is not already on PATH."""
    import pytesseract

    if shutil.which("tesseract"):
        return
    candidates = MAC_TESSERACT_PATHS if IS_MAC else WIN_TESSERACT_PATHS
    for path in candidates:
        if Path(path).is_file():
            pytesseract.pytesseract.tesseract_cmd = path
            return
    hint = "brew install tesseract" if IS_MAC else "install from https://github.com/UB-Mannheim/tesseract/wiki"
    raise RuntimeError(f"Tesseract OCR not found. {hint}")


def chrome_system_prompt(screen_w: int, screen_h: int,
                         center_x: int, center_y: int) -> str:
    """Groq system prompt with the correct modifier key for this OS."""
    m = mod_key()
    a = alt_key()
    os_name = "macOS" if IS_MAC else "Windows"
    return f"""You are an accessibility assistant that controls {os_name} for disabled users, mostly inside Google Chrome.
The user gives you a voice command plus a list of visible text elements on the screen with their (x, y) positions. The screen is {screen_w} wide by {screen_h} tall; its center is about ({center_x}, {center_y}).

Your job is to decide what action to take and return ONLY a valid JSON object — no explanation, no markdown, no extra text.

The screen elements are given to you as a NUMBERED list, like `[7] "Sign in" at (412, 88)`.

Supported actions:
- {{"action": "hotkey", "keys": [<str>, ...]}}
- {{"action": "click", "index": <int>}}        ← click a listed element BY ITS NUMBER (preferred for clicks)
- {{"action": "scroll", "direction": "up"|"down"|"left"|"right", "amount": <int>}}
- {{"action": "type", "text": "<str>"}}

IMPORTANT — prefer keyboard shortcuts. The primary modifier on this system is "{m}" (NOT the other OS's modifier). For common browser actions ALWAYS use a hotkey instead of clicking:
- New tab: {{"action": "hotkey", "keys": ["{m}", "t"]}}
- Close tab: {{"action": "hotkey", "keys": ["{m}", "w"]}}
- Reopen closed tab: {{"action": "hotkey", "keys": ["{m}", "shift", "t"]}}
- New window: {{"action": "hotkey", "keys": ["{m}", "n"]}}
- Next/previous tab: ["{m}", "{a}", "right"] / ["{m}", "{a}", "left"] on Mac, or ["{m}", "tab"] / ["{m}", "shift", "tab"] on Windows
- Reload page: ["{m}", "r"]
- Go back / forward: browser back/forward shortcuts for this OS
- Focus the address bar: ["{m}", "l"]
- Find on page: ["{m}", "f"]
- Scroll to top / bottom: use the scroll action.

Rules:
- If the command is a common browser/system action, use a hotkey. Use "click" ONLY when the user clearly refers to a specific visible label/button that has no keyboard shortcut.
- To navigate to a website, return a hotkey to focus the address bar, OR a "type" action with the URL when the bar is already focused. Keep it to one action.
- When clicking, pick the element from the numbered list whose text best matches the user's intent and return its NUMBER as "index". NEVER invent coordinates — only choose from the numbered elements you were given.
- The elements are listed in reading order (roughly top-to-bottom, left-to-right). For ordinal/positional requests like "the first video", "the second link", "the top result", choose by that order among the relevant items.
- For "click on this/that <thing>", choose the element whose text names that thing.
- If NOTHING in the list matches the user's intent, do not guess a click. Prefer a hotkey, or return {{"action": "click", "index": -1}} to signal "no match".
- Return ONLY the raw JSON object.
"""


def chrome_shortcut_table() -> list[tuple[str, dict, str]]:
    """Spoken-intent regex → Chrome hotkey actions for the current platform."""
    if IS_MAC:
        next_tab = hotkey_action("mod", "option", "right")
        prev_tab = hotkey_action("mod", "option", "left")
        go_back = hotkey_action("mod", "[")
        go_forward = hotkey_action("mod", "]")
        home = hotkey_action("mod", "shift", "h")
        scroll_top = hotkey_action("mod", "up")
        scroll_bottom = hotkey_action("mod", "down")
        history = hotkey_action("mod", "y")
        downloads = hotkey_action("mod", "shift", "j")
        devtools = hotkey_action("mod", "option", "i")
        view_source = hotkey_action("mod", "option", "u")
        fullscreen = hotkey_action("mod", "ctrl", "f")
        settings = hotkey_action("mod", ",")
    else:
        next_tab = hotkey_action("mod", "tab")
        prev_tab = hotkey_action("mod", "shift", "tab")
        go_back = hotkey_action("alt", "left")
        go_forward = hotkey_action("alt", "right")
        home = hotkey_action("alt", "home")
        scroll_top = hotkey_action("home")
        scroll_bottom = hotkey_action("end")
        history = hotkey_action("mod", "h")
        downloads = hotkey_action("mod", "j")
        devtools = hotkey_action("mod", "shift", "i")
        view_source = hotkey_action("mod", "u")
        fullscreen = hotkey_action("f11")
        settings = hotkey_action("mod", "shift", "delete")  # clear data / settings area

    return [
        (r"(reopen|restore|undo clos\w*|bring back).*(tab)", hotkey_action("mod", "shift", "t"), "reopen closed tab"),
        (r"(new|open).*(incognito|private)", hotkey_action("mod", "shift", "n"), "new incognito window"),
        (r"(new|open|another).*(window)", hotkey_action("mod", "n"), "new window"),
        (r"(new|open|another|create).*(tab)", hotkey_action("mod", "t"), "new tab"),
        (r"(close|exit).*(window)", hotkey_action("mod", "shift", "w"), "close window"),
        (r"(close|exit).*(tab)", hotkey_action("mod", "w"), "close tab"),
        (r"(next|forward).*(tab)|(tab).*(right)", next_tab, "next tab"),
        (r"(previous|prev|last|back).*(tab)|(tab).*(left)", prev_tab, "previous tab"),
        (r"(quit|close).*(chrome|browser)", hotkey_action("mod", "q") if IS_MAC else hotkey_action("alt", "f4"), "quit Chrome"),
        (r"minimi[sz]e", hotkey_action("mod", "m") if IS_MAC else hotkey_action("mod", "shift", "m"), "minimize window"),
        (r"(hard|force|empty cache).*(reload|refresh)", hotkey_action("mod", "shift", "r"), "hard reload"),
        (r"(reload|refresh)", hotkey_action("mod", "r"), "reload page"),
        (r"(go )?forward", go_forward, "go forward"),
        (r"(go )?back", go_back, "go back"),
        (r"home ?page|go home", home, "home page"),
        (r"address bar|url bar|location bar|search bar|focus.*address|type.*(url|address)|go to (a |the )?(website|url|page)|open (a |the )?(website|url)|search( the web| google| online)?\b", hotkey_action("mod", "l"), "focus address bar"),
        (r"find next", hotkey_action("mod", "g"), "find next"),
        (r"find previous|find prev", hotkey_action("mod", "shift", "g"), "find previous"),
        (r"find|search.*(on|in).*(page)", hotkey_action("mod", "f"), "find on page"),
        (r"\bprint\b", hotkey_action("mod", "p"), "print"),
        (r"save.*(page|this)|^save", hotkey_action("mod", "s"), "save page"),
        (r"zoom in|make.*(big|larg)|increase.*(zoom|text|size)", hotkey_action("mod", "="), "zoom in"),
        (r"zoom out|make.*(small)|decrease.*(zoom|text|size)", hotkey_action("mod", "-"), "zoom out"),
        (r"reset zoom|actual size|default zoom|normal size", hotkey_action("mod", "0"), "reset zoom"),
        (r"full ?screen", fullscreen, "toggle full screen"),
        (r"(scroll|go|jump).*(top|beginning|start)|^top", scroll_top, "scroll to top"),
        (r"(scroll|go|jump).*(bottom|end)|^bottom", scroll_bottom, "scroll to bottom"),
        (r"page down|scroll down (?:one |a )?page", hotkey_action("space"), "page down"),
        (r"page up|scroll up (?:one |a )?page", hotkey_action("shift", "space"), "page up"),
        (r"pin.*tab", hotkey_action("mod", "shift", "p") if IS_MAC else hotkey_action("mod", "shift", "p"), "pin tab"),
        (r"mute.*tab", hotkey_action("mod", "shift", "m") if IS_MAC else hotkey_action("mod", "shift", "m"), "mute tab"),
        (r"(open|show).*(bookmark manager|bookmarks manager)", hotkey_action("mod", "option", "b") if IS_MAC else hotkey_action("mod", "shift", "o"), "bookmark manager"),
        (r"clear.*form|reset.*form", hotkey_action("mod", "shift", "delete") if IS_MAC else hotkey_action("mod", "delete"), "clear form"),
        (r"focus.*next.*(field|input|box)", hotkey_action("tab"), "next field"),
        (r"focus.*(previous|prev).*(field|input|box)", hotkey_action("shift", "tab"), "previous field"),
        (r"submit|press enter|hit enter", hotkey_action("enter"), "press enter"),
        (r"escape|cancel|close dialog", hotkey_action("escape"), "escape"),
        (r"open.*link.*new tab", hotkey_action("mod", "enter"), "open link in new tab"),
        (r"bookmark all", hotkey_action("mod", "shift", "d"), "bookmark all tabs"),
        (r"bookmark", hotkey_action("mod", "d"), "bookmark page"),
        (r"(show|hide|toggle).*(bookmark bar|bookmarks bar)", hotkey_action("mod", "shift", "b"), "toggle bookmarks bar"),
        (r"\bhistory\b", history, "open history"),
        (r"\bdownloads?\b", downloads, "open downloads"),
        (r"clear.*(browsing|history|data)", hotkey_action("mod", "shift", "backspace") if IS_MAC else hotkey_action("mod", "shift", "delete"), "clear browsing data"),
        (r"settings|preferences", settings, "open settings"),
        (r"dev(eloper)? tools|inspect", devtools, "open dev tools"),
        (r"view (page )?source", view_source, "view source"),
        (r"select all", hotkey_action("mod", "a"), "select all"),
        (r"\bcopy\b", hotkey_action("mod", "c"), "copy"),
        (r"\bpaste\b", hotkey_action("mod", "v"), "paste"),
        (r"\bcut\b", hotkey_action("mod", "x"), "cut"),
        (r"\bredo\b", hotkey_action("mod", "shift", "z"), "redo"),
        (r"\bundo\b", hotkey_action("mod", "z"), "undo"),
    ]


def permission_hints(feature: str) -> str:
    """Human-readable permission reminder for the current OS."""
    if IS_MAC:
        if feature == "voice_control":
            return ("macOS: grant Accessibility, Screen Recording, and Microphone "
                    "for Terminal/your IDE in System Settings → Privacy & Security.")
        if feature == "page_reader":
            return ("macOS: grant Accessibility (hotkeys/hover-to-read) and "
                    "Screen Recording (OCR) in System Settings → Privacy & Security.")
    if IS_WINDOWS:
        if feature == "page_reader":
            return "Windows: allow screen capture if prompted; global hotkeys may need the app run as administrator."
        if feature == "voice_control":
            return "Windows: allow microphone access; Chrome must be installed for browser control."
    return ""
