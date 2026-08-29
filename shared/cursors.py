"""Resize the mouse pointer, for real.

Writing `CursorBaseSize` and calling `SPI_SETCURSORS` is what Settings
appears to do, and it is what this used to do — but on its own it leaves the
pointer exactly the same size on screen. Windows only rescales the pointer
when the cursor images themselves are reloaded at a new size, which is what
`SetSystemCursor` does and no registry value can.

So both happen here: the registry values so the choice survives a reboot and
matches what Settings shows, and `SetSystemCursor` so it changes now.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

from shared import console
from shared import platform as plat

CURSORS_KEY = r"Control Panel\Cursors"
ACCESSIBILITY_KEY = r"SOFTWARE\Microsoft\Accessibility"

# Windows' own scale: 1 is the default pointer, 15 the largest, each step 16px.
MIN_SIZE, MAX_SIZE = 1, 15
BASE_PX = 32
STEP_PX = 16

# Registry name -> the OCR_* system cursor it provides.
_CURSOR_IDS = {
    "Arrow": 32512, "IBeam": 32513, "Wait": 32514, "Crosshair": 32515,
    "UpArrow": 32516, "SizeNWSE": 32642, "SizeNESW": 32643, "SizeWE": 32644,
    "SizeNS": 32645, "SizeAll": 32646, "No": 32648, "Hand": 32649,
    "AppStarting": 32650, "Help": 32651,
}

IMAGE_CURSOR = 2
LR_LOADFROMFILE = 0x0010
SPI_SETCURSORS = 0x0057


def pixels_for(size: int) -> int:
    """Pointer size in pixels for a 1-15 slider value."""
    return BASE_PX + STEP_PX * (clamp(size) - 1)


def clamp(size: int) -> int:
    return max(MIN_SIZE, min(MAX_SIZE, int(size)))


def _scheme_paths() -> dict[str, str]:
    """The cursor file each system cursor currently uses."""
    import winreg

    paths: dict[str, str] = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CURSORS_KEY, 0,
                            winreg.KEY_READ) as key:
            for name in _CURSOR_IDS:
                try:
                    value, _ = winreg.QueryValueEx(key, name)
                except OSError:
                    continue
                path = os.path.expandvars(str(value or ""))
                if path and os.path.exists(path):
                    paths[name] = path
    except OSError:
        pass
    return paths


def _write_registry(size: int):
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, ACCESSIBILITY_KEY, 0,
                            winreg.KEY_READ | winreg.KEY_WRITE) as key:
        winreg.SetValueEx(key, "CursorSize", 0, winreg.REG_DWORD, size)
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, CURSORS_KEY, 0,
                            winreg.KEY_READ | winreg.KEY_WRITE) as key:
        winreg.SetValueEx(key, "CursorBaseSize", 0, winreg.REG_SZ,
                          str(pixels_for(size)))


def read_size() -> int | None:
    """The slider value currently stored, or None if it was never set."""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, ACCESSIBILITY_KEY, 0,
                            winreg.KEY_READ) as key:
            return int(winreg.QueryValueEx(key, "CursorSize")[0])
    except OSError:
        return None


def reload_from_registry():
    """Put every pointer back to whatever the registry says it should be."""
    ctypes.windll.user32.SystemParametersInfoW(SPI_SETCURSORS, 0, None, 0)


def apply_size(size: int) -> int:
    """Resize every system pointer now. Returns how many actually changed."""
    if not plat.IS_WINDOWS:
        return 0
    size = clamp(size)
    _write_registry(size)

    # Size 1 is the default: reloading the scheme restores the stock pointers
    # more faithfully than re-rasterising them at 32px would.
    if size == MIN_SIZE:
        reload_from_registry()
        console.log_event("cursors", "apply_size", size=size, restored=True,
                          changed=len(_CURSOR_IDS))
        return len(_CURSOR_IDS)

    user32 = ctypes.windll.user32
    user32.LoadImageW.restype = wintypes.HANDLE
    user32.SetSystemCursor.argtypes = [wintypes.HANDLE, wintypes.DWORD]

    px = pixels_for(size)
    changed = 0
    for name, path in _scheme_paths().items():
        handle = user32.LoadImageW(None, path, IMAGE_CURSOR, px, px, LR_LOADFROMFILE)
        if not handle:
            continue
        # SetSystemCursor takes ownership of the handle; do not free it.
        if user32.SetSystemCursor(handle, _CURSOR_IDS[name]):
            changed += 1

    console.log_event("cursors", "apply_size", size=size, pixels=px,
                      changed=changed, of=len(_CURSOR_IDS))
    return changed
