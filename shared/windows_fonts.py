"""Windows font-substitution helpers, shared by the hub and the Dyslexia Font feature.

These used to live in features/dyslexia_font/main.py. The redesign moves the
Apply / Restore controls into the hub's settings sheet, so both processes need
them; the registry work itself is unchanged.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import webbrowser
from pathlib import Path

from shared import console, platform as plat

ROOT = Path(__file__).resolve().parent.parent
FEATURE_DIR = ROOT / "features" / "dyslexia_font"
BACKUP_FILE = FEATURE_DIR / "windows_font_backup.json"
EXTENSION_DIR = FEATURE_DIR / "chrome_extension"

FONT_SUBSTITUTES_PATH = r"Software\Microsoft\Windows NT\CurrentVersion\FontSubstitutes"
FONTS_PATH = r"Software\Microsoft\Windows NT\CurrentVersion\Fonts"

FONT_CHOICES = (
    "OpenDyslexic",
    "Atkinson Hyperlegible",
    "Comic Sans MS",
    "Arial",
)

# Short plain-language note shown under each font name in the picker.
FONT_NOTES = {
    "OpenDyslexic": "Weighted bottoms",
    "Atkinson Hyperlegible": "Clearer letter shapes",
    "Comic Sans MS": "Informal, wide spacing",
    "Arial": "Plain and familiar",
}

SUBSTITUTION_TARGETS = (
    # Broadened well beyond the original 6 to catch more of what apps
    # actually declare — this only reaches apps that reference a font by
    # one of these exact names (Win32/UWP chrome mostly); apps that embed
    # or custom-load their own fonts (some modern apps, Electron, Chrome's
    # own UI) are outside what any registry substitution can touch — no
    # Windows API does a true "every app, no exceptions" override.
    "Arial",
    "Arial Black",
    "Calibri",
    "Calibri Light",
    "Cambria",
    "Candara",
    "Comic Sans MS",
    "Consolas",
    "Constantia",
    "Corbel",
    "Courier New",
    "Georgia",
    "Impact",
    "Lucida Console",
    "Lucida Sans Unicode",
    "Microsoft Sans Serif",
    "MS Sans Serif",
    "MS Shell Dlg",
    "MS Shell Dlg 2",
    "Palatino Linotype",
    "Segoe UI",
    "Segoe UI Light",
    "Segoe UI Semibold",
    "Segoe UI Symbol",
    "Sylfaen",
    "Tahoma",
    "Times New Roman",
    "Trebuchet MS",
    "Verdana",
)


def log(msg: str):
    console.safe_print(f"[dyslexia_font] {msg}", flush=True)


def _open_registry_key(root, path: str, access: int):
    import winreg

    return winreg.OpenKey(root, path, 0, access)


def _create_registry_key(root, path: str):
    import winreg

    return winreg.CreateKeyEx(root, path, 0, winreg.KEY_READ | winreg.KEY_WRITE)


def _read_registry_string(root, path: str, name: str) -> str | None:
    import winreg

    try:
        with _open_registry_key(root, path, winreg.KEY_READ) as key:
            value, value_type = winreg.QueryValueEx(key, name)
            if value_type in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
                return str(value)
    except FileNotFoundError:
        return None
    except OSError:
        return None
    return None


def _set_registry_string(root, path: str, name: str, value: str):
    import winreg

    with _create_registry_key(root, path) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)


def _delete_registry_value(root, path: str, name: str):
    import winreg

    try:
        with _open_registry_key(root, path, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
    except FileNotFoundError:
        return
    except OSError:
        return


def _enum_registry_values(root, path: str) -> list[tuple[str, str]]:
    import winreg

    values = []
    try:
        with _open_registry_key(root, path, winreg.KEY_READ) as key:
            i = 0
            while True:
                try:
                    name, value, _value_type = winreg.EnumValue(key, i)
                except OSError:
                    break
                values.append((str(name), str(value)))
                i += 1
    except FileNotFoundError:
        pass
    return values


def installed_windows_fonts() -> set[str]:
    if not plat.IS_WINDOWS:
        return set()

    import winreg

    found: set[str] = set()
    roots = (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)
    for root in roots:
        for name, _value in _enum_registry_values(root, FONTS_PATH):
            family = name
            for suffix in (
                " (TrueType)",
                " (OpenType)",
                " Regular (TrueType)",
                " Regular (OpenType)",
            ):
                family = family.replace(suffix, "")
            family = family.replace(" Bold", "").replace(" Italic", "").strip()
            if family:
                found.add(family.lower())
    return found


def is_font_installed(font_name: str) -> bool:
    if not plat.IS_WINDOWS:
        return False
    low = font_name.lower()
    fonts = installed_windows_fonts()
    return any(low == font or low in font for font in fonts)


def font_status_text(font_name: str) -> str:
    """The status line the design keeps verbatim from the old window."""
    if not plat.IS_WINDOWS:
        return "This feature is intended for Windows."
    if is_font_installed(font_name):
        return f"{font_name} appears to be installed."
    return (f"{font_name} was not found in Windows fonts. Install it first, "
            "or choose a font that is already installed.")


def broadcast_font_change():
    if not plat.IS_WINDOWS:
        return
    try:
        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x001A
        SMTO_ABORTIFHUNG = 0x0002
        result = ctypes.c_ulong()
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST,
            WM_SETTINGCHANGE,
            0,
            "Windows",
            SMTO_ABORTIFHUNG,
            3000,
            ctypes.byref(result),
        )
    except Exception as e:
        log(f"could not broadcast font change: {e}")


def apply_windows_substitution(font_name: str, targets: list[str]) -> None:
    if not plat.IS_WINDOWS:
        raise RuntimeError("Windows font substitution is only available on Windows.")
    if not is_font_installed(font_name):
        raise RuntimeError(f"{font_name} does not appear to be installed.")

    import winreg

    backup = {
        "font_name": font_name,
        "registry_root": "HKEY_CURRENT_USER",
        "path": FONT_SUBSTITUTES_PATH,
        "values": {},
    }
    for target in targets:
        backup["values"][target] = _read_registry_string(
            winreg.HKEY_CURRENT_USER, FONT_SUBSTITUTES_PATH, target
        )
    BACKUP_FILE.write_text(json.dumps(backup, indent=2), encoding="utf-8")

    for target in targets:
        _set_registry_string(winreg.HKEY_CURRENT_USER, FONT_SUBSTITUTES_PATH, target, font_name)
    broadcast_font_change()


def restore_windows_substitution() -> None:
    if not plat.IS_WINDOWS:
        raise RuntimeError("Windows font substitution is only available on Windows.")
    if not BACKUP_FILE.exists():
        raise RuntimeError("No Dyslexia Font backup file was found.")

    import winreg

    backup = json.loads(BACKUP_FILE.read_text(encoding="utf-8"))
    values = backup.get("values", {})
    for target, old_value in values.items():
        if old_value is None:
            _delete_registry_value(winreg.HKEY_CURRENT_USER, FONT_SUBSTITUTES_PATH, target)
        else:
            _set_registry_string(
                winreg.HKEY_CURRENT_USER, FONT_SUBSTITUTES_PATH, target, str(old_value)
            )
    broadcast_font_change()
    # Delete on success so BACKUP_FILE.exists() reliably means "a
    # substitution is currently applied and not yet restored" — callers
    # (e.g. dyslexia_font's startup crash-safety check) rely on that.
    try:
        BACKUP_FILE.unlink()
    except OSError:
        pass


def open_extension_folder():
    if plat.IS_WINDOWS:
        os.startfile(EXTENSION_DIR)  # type: ignore[attr-defined]
    else:
        webbrowser.open(EXTENSION_DIR.as_uri())


def open_chrome_extensions():
    if plat.IS_WINDOWS:
        subprocess.Popen(["cmd", "/c", "start", "", "chrome", "chrome://extensions"])
    else:
        webbrowser.open("chrome://extensions")
