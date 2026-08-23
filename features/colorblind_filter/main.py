"""Color Blind Filter — toggles Windows' built-in system-wide color filter.

Runs as its own process when toggled ON in the hub. Applies system-wide (not
just Chrome) by driving Windows' own Ease of Access > Color Filters feature:
set HKCU\\Software\\Microsoft\\ColorFiltering, then nudge atbroker.exe to make
it take effect immediately (same trick as pressing Win+Ctrl+C). See
features/README.md.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, ttk

FEATURE_DIR = Path(__file__).resolve().parent
ROOT = FEATURE_DIR.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import console, feature_bus, platform as plat  # noqa: E402

console.configure_stdio()

SETTINGS_FILE = FEATURE_DIR / "settings.json"
COLOR_FILTERING_PATH = r"Software\Microsoft\ColorFiltering"

FILTER_TYPES = {
    "Deuteranopia": 3,
    "Protanopia": 4,
    "Tritanopia": 5,
    "Grayscale": 0,
    "Invert": 1,
    "Grayscale Inverted": 2,
}


def log(msg: str):
    console.safe_print(f"[colorblind_filter] {msg}", flush=True)


def default_settings() -> dict:
    return {"enabled": False, "filter_name": "Deuteranopia"}


def load_settings() -> dict:
    s = default_settings()
    if SETTINGS_FILE.exists():
        try:
            s.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except Exception as e:
            log(f"bad settings.json: {e} — using defaults")
    return s


def save_settings(settings: dict):
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def _read_registry_dword(root, path: str, name: str) -> int | None:
    import winreg

    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ) as key:
            value, vtype = winreg.QueryValueEx(key, name)
            if vtype == winreg.REG_DWORD:
                return int(value)
    except FileNotFoundError:
        return None
    except OSError:
        return None
    return None


def _set_registry_dword(root, path: str, name: str, value: int):
    import winreg

    with winreg.CreateKeyEx(root, path, 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, value)


def is_filter_active() -> bool:
    if not plat.IS_WINDOWS:
        return False
    import winreg

    return bool(_read_registry_dword(winreg.HKEY_CURRENT_USER, COLOR_FILTERING_PATH, "Active"))


def _toggle_live():
    """Same effect as the Win+Ctrl+C shortcut — applies the current registry state."""
    subprocess.run(["atbroker.exe", "/colorfiltershortcut"], check=False)
    time.sleep(0.2)


def set_filter(enabled: bool, filter_type: int):
    if not plat.IS_WINDOWS:
        raise RuntimeError("Color filters are only available on Windows.")
    import winreg

    current_active = is_filter_active()
    current_type = _read_registry_dword(winreg.HKEY_CURRENT_USER, COLOR_FILTERING_PATH, "FilterType")

    if enabled:
        if current_active and current_type != filter_type:
            _toggle_live()          # off first so a type change is picked up cleanly
            current_active = False
        _set_registry_dword(winreg.HKEY_CURRENT_USER, COLOR_FILTERING_PATH, "FilterType", filter_type)
        if not current_active:
            _toggle_live()
    else:
        if current_active:
            _toggle_live()


class ColorblindFilterApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self.title("Color Blind Filter")
        self.resizable(False, False)
        self.geometry("340x220")
        self.configure(bg="#1a1a2e")
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self._shutdown)

        self._build_ui()
        save_settings(self.settings)
        feature_bus.update_presence("colorblind_filter", os.getpid())
        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT, self._shutdown)

    def _build_ui(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        frame = tk.Frame(self, bg="#1a1a2e")
        frame.pack(fill="both", expand=True, padx=16, pady=14)

        tk.Label(frame, text="Color Blind Filter", font=("Helvetica", 16, "bold"),
                 fg="#e0e0ff", bg="#1a1a2e").pack(anchor="w")
        tk.Label(frame, text="System-wide Windows color filter", font=("Helvetica", 10),
                 fg="#8a8ab0", bg="#1a1a2e").pack(anchor="w", pady=(0, 12))

        self._enabled_var = tk.BooleanVar(value=bool(self.settings["enabled"]))
        tk.Checkbutton(frame, text="Enable filter", variable=self._enabled_var,
                       command=self._apply, fg="#e0e0ff", bg="#1a1a2e",
                       activebackground="#1a1a2e", activeforeground="#e0e0ff",
                       selectcolor="#23233f").pack(anchor="w")

        tk.Label(frame, text="Filter type", fg="#e0e0ff", bg="#1a1a2e").pack(anchor="w", pady=(10, 2))
        self._filter_var = tk.StringVar(value=self.settings["filter_name"])
        combo = ttk.Combobox(frame, textvariable=self._filter_var,
                              values=list(FILTER_TYPES), state="readonly")
        combo.pack(fill="x")
        combo.bind("<<ComboboxSelected>>", lambda _e: self._apply())

        self.status_var = tk.StringVar()
        tk.Label(frame, textvariable=self.status_var, fg="#ffd166", bg="#1a1a2e",
                 wraplength=300, justify="left").pack(anchor="w", pady=(12, 0))

        if not plat.IS_WINDOWS:
            self.status_var.set("This feature is Windows-only.")
            combo.configure(state="disabled")

    def _apply(self):
        try:
            set_filter(self._enabled_var.get(), FILTER_TYPES[self._filter_var.get()])
            self.settings["enabled"] = self._enabled_var.get()
            self.settings["filter_name"] = self._filter_var.get()
            save_settings(self.settings)
            self.status_var.set("Applied." if self._enabled_var.get() else "Filter off.")
            log(f"filter set: enabled={self._enabled_var.get()} type={self._filter_var.get()}")
        except Exception as e:
            log(f"apply failed: {e}")
            self.status_var.set(str(e))
            messagebox.showerror("Color Blind Filter", str(e))

    def _shutdown(self, *_args):
        log("shutting down")
        feature_bus.remove_presence("colorblind_filter")
        try:
            self.destroy()
        except Exception:
            pass
        sys.exit(0)


def main():
    log("feature started")
    app = ColorblindFilterApp()
    app.mainloop()
    feature_bus.remove_presence("colorblind_filter")
    log("exited")


if __name__ == "__main__":
    main()
