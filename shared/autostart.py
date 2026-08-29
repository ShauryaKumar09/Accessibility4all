"""Launch the hub automatically when the user logs in.

Per-user only, on both platforms: a `Run` value under HKCU on Windows and a
LaunchAgent in the user's own Library on macOS. Neither needs admin rights,
which matters because Focus Mode's elevation prompt is already as much
friction as this app should ever ask for.

The hub restores whichever features were on last time by itself (see
`restore_enabled()` in hub.py), so all this has to do is start the hub.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from shared import console
from shared import platform as plat

ROOT = Path(__file__).resolve().parent.parent
HUB_ENTRY = ROOT / "hub.py"

RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "Accessibility4all"

MAC_LABEL = "com.accessibility4all.hub"
MAC_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{MAC_LABEL}.plist"


def _launch_interpreter() -> Path:
    """The interpreter to start the hub with.

    Prefer pythonw.exe: python.exe would flash a console window at every
    login, which is a poor first impression for an accessibility tool.
    """
    exe = Path(sys.executable)
    if plat.IS_WINDOWS:
        windowless = exe.with_name("pythonw.exe")
        if windowless.exists():
            return windowless
    return exe


def _windows_command() -> str:
    return f'"{_launch_interpreter()}" "{HUB_ENTRY}"'


# ── Windows ──
def _read_run_value() -> str | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0,
                            winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, RUN_VALUE_NAME)
            return str(value)
    except OSError:
        return None


def _enable_windows():
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0,
                            winreg.KEY_READ | winreg.KEY_WRITE) as key:
        winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, _windows_command())


def _disable_windows():
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0,
                            winreg.KEY_READ | winreg.KEY_WRITE) as key:
            winreg.DeleteValue(key, RUN_VALUE_NAME)
    except FileNotFoundError:
        pass          # already gone; that is the state we wanted


# ── macOS ──
def _enable_mac():
    import plistlib

    MAC_PLIST.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": MAC_LABEL,
        "ProgramArguments": [str(_launch_interpreter()), str(HUB_ENTRY)],
        "RunAtLoad": True,
    }
    with open(MAC_PLIST, "wb") as f:
        plistlib.dump(payload, f)
    # Load it now so the change takes effect without waiting for a re-login.
    subprocess.run(["launchctl", "load", "-w", str(MAC_PLIST)],
                   check=False, capture_output=True)


def _disable_mac():
    if MAC_PLIST.exists():
        subprocess.run(["launchctl", "unload", "-w", str(MAC_PLIST)],
                       check=False, capture_output=True)
        MAC_PLIST.unlink(missing_ok=True)


# ── the API the hub calls ──
def is_supported() -> bool:
    return bool(plat.IS_WINDOWS or plat.IS_MAC)


def is_enabled() -> bool:
    if plat.IS_WINDOWS:
        return _read_run_value() is not None
    if plat.IS_MAC:
        return MAC_PLIST.exists()
    return False


def set_enabled(enabled: bool) -> None:
    """Turn login-launch on or off, then confirm it actually stuck."""
    if not is_supported():
        raise RuntimeError("Starting at login is only supported on Windows and macOS.")
    if plat.IS_WINDOWS:
        _enable_windows() if enabled else _disable_windows()
    else:
        _enable_mac() if enabled else _disable_mac()

    verified = is_enabled()
    console.log_event("autostart", "apply_result", requested=enabled,
                      verified=verified, ok=(verified == enabled),
                      command=_windows_command() if plat.IS_WINDOWS else str(MAC_PLIST))
    if verified != enabled:
        raise RuntimeError("Could not change the start-at-login setting.")
