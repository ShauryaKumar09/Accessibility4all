"""Dyslexia Font - Windows-focused font helper.

Runs as its own process when toggled ON in the hub. Website font changes are
handled by the bundled Chrome extension. Windows app font substitution is
opt-in, backed up, and reversible.
"""

from __future__ import annotations

import ctypes
import json
import os
import random
import signal
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import font as tkfont, messagebox, ttk

FEATURE_DIR = Path(__file__).resolve().parent
ROOT = FEATURE_DIR.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import console, platform as plat  # noqa: E402

console.configure_stdio()

SETTINGS_FILE = FEATURE_DIR / "settings.json"
BACKUP_FILE = FEATURE_DIR / "windows_font_backup.json"
EXTENSION_DIR = FEATURE_DIR / "chrome_extension"

FONT_CHOICES = (
    "OpenDyslexic",
    "Atkinson Hyperlegible",
    "Comic Sans MS",
    "Arial",
)

SUBSTITUTION_TARGETS = (
    "Arial",
    "Calibri",
    "Segoe UI",
    "Tahoma",
    "Times New Roman",
    "Verdana",
)

FONT_SUBSTITUTES_PATH = r"Software\Microsoft\Windows NT\CurrentVersion\FontSubstitutes"
FONTS_PATH = r"Software\Microsoft\Windows NT\CurrentVersion\Fonts"


def log(msg: str):
    console.safe_print(f"[dyslexia_font] {msg}", flush=True)


def default_settings() -> dict:
    return {
        "website_enabled": True,
        "font_family": "OpenDyslexic",
        "letter_spacing": 0.03,
        "line_height": 1.55,
        "font_weight": "inherit",
        "windows_targets": list(SUBSTITUTION_TARGETS),
    }


def load_settings() -> dict:
    settings = default_settings()
    if SETTINGS_FILE.exists():
        try:
            loaded = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            settings.update(loaded)
        except Exception as e:
            log(f"bad settings.json: {e}; using defaults")
    return settings


def save_settings(settings: dict):
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")


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


def open_extension_folder():
    try:
        if plat.IS_WINDOWS:
            os.startfile(EXTENSION_DIR)  # type: ignore[attr-defined]
        else:
            webbrowser.open(EXTENSION_DIR.as_uri())
    except Exception as e:
        log(f"could not open extension folder: {e}")
        messagebox.showerror("Dyslexia Font", f"Could not open extension folder:\n{e}")


def open_chrome_extensions():
    try:
        if plat.IS_WINDOWS:
            subprocess.Popen(["cmd", "/c", "start", "", "chrome", "chrome://extensions"])
        else:
            webbrowser.open("chrome://extensions")
    except Exception as e:
        log(f"could not open Chrome extensions: {e}")
        messagebox.showerror("Dyslexia Font", f"Could not open Chrome extensions:\n{e}")


# ── Screening test (short, non-diagnostic self-check) ──────────────────────────
# NOT a diagnostic tool. It cannot identify dyslexia. It only notices a few
# patterns sometimes associated with reading differences, so the disclaimer is
# shown before the quiz starts and again with the result.
SCREENING_DISCLAIMER = (
    "This is NOT a diagnostic tool and cannot identify dyslexia. It only notices "
    "a few patterns that are sometimes associated with reading differences. If "
    "you have concerns about reading difficulty, please consult a qualified "
    "specialist such as an educational psychologist or learning-disabilities specialist."
)

SCREENING_QUESTIONS = [
    {"prompt": "Which one matches the target letter?\n\nTarget: b",
     "options": ["d", "b", "p", "q"], "correct": 1},
    {"prompt": "Which one matches the target letter?\n\nTarget: p",
     "options": ["q", "d", "p", "b"], "correct": 2},
    {"prompt": "Which word is spelled the same forwards and as shown?\n\nTarget: was",
     "options": ["saw", "was", "sae", "aws"], "correct": 1},
    {"prompt": "Which word does NOT rhyme with the others?",
     "options": ["cat", "hat", "dog", "bat"], "correct": 2},
    {"prompt": "Which word does NOT rhyme with the others?",
     "options": ["light", "night", "sight", "bench"], "correct": 3},
    {"prompt": "Put in order: which comes first alphabetically?",
     "options": ["dog", "cat", "ant", "elk"], "correct": 2},
]

SCREENING_PASSAGE = (
    "The quick brown fox jumps over the lazy dog. Reading every day helps build "
    "stronger word recognition and comprehension over time."
)
SCREENING_TYPICAL_WPM = 200  # rough, non-clinical adult reference


class ScreeningDialog(tk.Toplevel):
    """Short non-diagnostic quiz (letter reversal, rhyme, reading speed)."""

    WIN_W, WIN_H = 440, 380

    def __init__(self, parent):
        super().__init__(parent)
        self.title("Dyslexia Screening")
        self.resizable(False, False)
        self.geometry(f"{self.WIN_W}x{self.WIN_H}")
        self.configure(bg="#1a1a2e")
        self.attributes("-topmost", True)

        self._questions = random.sample(SCREENING_QUESTIONS, len(SCREENING_QUESTIONS))
        self._idx = 0
        self._flags = 0
        self._reading_start = 0.0

        self._frame = tk.Frame(self, bg="#1a1a2e")
        self._frame.pack(fill="both", expand=True)
        self._show_intro()

    def _clear(self):
        for child in self._frame.winfo_children():
            child.destroy()

    def _title(self, text: str):
        tk.Label(self._frame, text=text,
                 font=tkfont.Font(family="Helvetica", size=15, weight="bold"),
                 fg="#e0e0ff", bg="#1a1a2e", wraplength=self.WIN_W - 40, justify="left").pack(
            anchor="w", padx=20, pady=(20, 8))

    def _disclaimer_label(self, parent):
        tk.Label(parent, text=SCREENING_DISCLAIMER, font=tkfont.Font(family="Helvetica", size=9),
                 fg="#ffd166", bg="#1a1a2e", wraplength=self.WIN_W - 40, justify="left").pack(
            anchor="w", padx=20, pady=(0, 12))

    def _show_intro(self):
        self._clear()
        self._title("Dyslexia Screening")
        self._disclaimer_label(self._frame)
        tk.Label(self._frame,
                 text=f"{len(self._questions)} quick questions, then one short "
                      "timed reading task. Answer as best you can.",
                 font=tkfont.Font(family="Helvetica", size=10),
                 fg="#8a8ab0", bg="#1a1a2e", wraplength=self.WIN_W - 40, justify="left").pack(
            anchor="w", padx=20, pady=(0, 20))
        tk.Button(self._frame, text="Start", command=self._show_question,
                  font=tkfont.Font(family="Helvetica", size=10, weight="bold"),
                  bg="#23233f", fg="#e0e0ff", relief="flat", padx=16, pady=8,
                  cursor="hand2").pack(anchor="w", padx=20)

    def _show_question(self):
        if self._idx >= len(self._questions):
            self._show_reading_task()
            return
        self._clear()
        q = self._questions[self._idx]
        tk.Label(self._frame, text=f"Question {self._idx + 1} of {len(self._questions)}",
                 font=tkfont.Font(family="Helvetica", size=9),
                 fg="#8a8ab0", bg="#1a1a2e").pack(anchor="w", padx=20, pady=(16, 4))
        tk.Label(self._frame, text=q["prompt"],
                 font=tkfont.Font(family="Helvetica", size=13, weight="bold"),
                 fg="#e0e0ff", bg="#1a1a2e", wraplength=self.WIN_W - 40, justify="left").pack(
            anchor="w", padx=20, pady=(4, 16))

        options = list(enumerate(q["options"]))
        random.shuffle(options)
        for orig_idx, text in options:
            tk.Button(self._frame, text=text, anchor="w",
                      command=lambda oi=orig_idx: self._answer(q, oi),
                      font=tkfont.Font(family="Helvetica", size=11),
                      bg="#23233f", fg="#e0e0ff", relief="flat", padx=14, pady=8,
                      cursor="hand2").pack(fill="x", padx=20, pady=4)

    def _answer(self, q: dict, chosen_idx: int):
        if chosen_idx != q["correct"]:
            self._flags += 1
        self._idx += 1
        self._show_question()

    def _show_reading_task(self):
        self._clear()
        self._title("Reading speed")
        tk.Label(self._frame, text="Read the passage below aloud or silently, "
                                    "then click Done as soon as you finish.",
                 font=tkfont.Font(family="Helvetica", size=9),
                 fg="#8a8ab0", bg="#1a1a2e", wraplength=self.WIN_W - 40, justify="left").pack(
            anchor="w", padx=20, pady=(0, 10))
        tk.Label(self._frame, text=SCREENING_PASSAGE,
                 font=tkfont.Font(family="Helvetica", size=12),
                 fg="#e0e0ff", bg="#23233f", wraplength=self.WIN_W - 60, justify="left",
                 padx=14, pady=14).pack(fill="x", padx=20)
        self._reading_start = time.perf_counter()
        tk.Button(self._frame, text="Done reading", command=self._finish_reading,
                  font=tkfont.Font(family="Helvetica", size=10, weight="bold"),
                  bg="#23233f", fg="#e0e0ff", relief="flat", padx=16, pady=8,
                  cursor="hand2").pack(anchor="w", padx=20, pady=16)

    def _finish_reading(self):
        elapsed = max(0.5, time.perf_counter() - self._reading_start)
        word_count = len(SCREENING_PASSAGE.split())
        wpm = word_count / (elapsed / 60)
        if wpm < SCREENING_TYPICAL_WPM * 0.5:
            self._flags += 1
        self._show_result(wpm)

    def _show_result(self, wpm: float):
        self._clear()
        if self._flags <= 1:
            bucket, color, msg = "low", "#69db7c", "Few patterns showed up in your answers."
        elif self._flags <= 3:
            bucket, color, msg = "medium", "#ffd166", "Some patterns showed up in your answers."
        else:
            bucket, color, msg = "high", "#ff6b6b", "Several patterns showed up in your answers."

        self._title("Result")
        tk.Label(self._frame, text=f"{msg} (reading speed: {wpm:.0f} words/min)",
                 font=tkfont.Font(family="Helvetica", size=12, weight="bold"),
                 fg=color, bg="#1a1a2e", wraplength=self.WIN_W - 40, justify="left").pack(
            anchor="w", padx=20, pady=(4, 12))
        self._disclaimer_label(self._frame)
        tk.Button(self._frame, text="Try again", command=self._restart,
                  font=tkfont.Font(family="Helvetica", size=10),
                  bg="#23233f", fg="#e0e0ff", relief="flat", padx=14, pady=6,
                  cursor="hand2").pack(anchor="w", padx=20)
        log(f"screening result: bucket={bucket} flags={self._flags} wpm={wpm:.0f}")

    def _restart(self):
        self._questions = random.sample(SCREENING_QUESTIONS, len(SCREENING_QUESTIONS))
        self._idx = 0
        self._flags = 0
        self._show_intro()


class DyslexiaFontApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.settings = load_settings()

        self.title("Dyslexia Font")
        self.resizable(False, False)
        self.geometry("430x470")
        self.configure(bg="#1a1a2e")
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self._shutdown)

        self._build_ui()
        save_settings(self.settings)
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

        tk.Label(
            frame,
            text="Dyslexia Font",
            font=("Helvetica", 16, "bold"),
            fg="#e0e0ff",
            bg="#1a1a2e",
        ).pack(anchor="w")

        tk.Label(
            frame,
            text="Windows website and app font helper",
            font=("Helvetica", 10),
            fg="#8a8ab0",
            bg="#1a1a2e",
        ).pack(anchor="w", pady=(0, 12))

        self.website_var = tk.BooleanVar(value=bool(self.settings["website_enabled"]))
        tk.Checkbutton(
            frame,
            text="Website font mode",
            variable=self.website_var,
            command=self._save_from_ui,
            fg="#e0e0ff",
            bg="#1a1a2e",
            activebackground="#1a1a2e",
            activeforeground="#e0e0ff",
            selectcolor="#23233f",
        ).pack(anchor="w")

        tk.Label(frame, text="Font", fg="#e0e0ff", bg="#1a1a2e").pack(anchor="w", pady=(10, 2))
        self.font_var = tk.StringVar(value=self.settings["font_family"])
        font_menu = ttk.Combobox(frame, textvariable=self.font_var, values=FONT_CHOICES, state="readonly")
        font_menu.pack(fill="x")
        font_menu.bind("<<ComboboxSelected>>", lambda _e: self._on_font_change())

        controls = tk.Frame(frame, bg="#1a1a2e")
        controls.pack(fill="x", pady=(10, 0))

        tk.Label(controls, text="Letter spacing", fg="#e0e0ff", bg="#1a1a2e").grid(row=0, column=0, sticky="w")
        self.spacing_var = tk.DoubleVar(value=float(self.settings["letter_spacing"]))
        tk.Scale(
            controls,
            from_=0.0,
            to=0.12,
            resolution=0.01,
            orient="horizontal",
            variable=self.spacing_var,
            command=lambda _v: self._save_from_ui(),
            bg="#1a1a2e",
            fg="#e0e0ff",
            highlightthickness=0,
            length=170,
        ).grid(row=1, column=0, sticky="we", padx=(0, 14))

        tk.Label(controls, text="Line height", fg="#e0e0ff", bg="#1a1a2e").grid(row=0, column=1, sticky="w")
        self.line_var = tk.DoubleVar(value=float(self.settings["line_height"]))
        tk.Scale(
            controls,
            from_=1.0,
            to=2.2,
            resolution=0.05,
            orient="horizontal",
            variable=self.line_var,
            command=lambda _v: self._save_from_ui(),
            bg="#1a1a2e",
            fg="#e0e0ff",
            highlightthickness=0,
            length=170,
        ).grid(row=1, column=1, sticky="we")

        self.weight_var = tk.StringVar(value=self.settings["font_weight"])
        self.bold_var = tk.BooleanVar(value=self.settings["font_weight"] == "700")
        tk.Checkbutton(
            frame,
            text="Use bold website text",
            variable=self.bold_var,
            command=self._toggle_bold,
            fg="#e0e0ff",
            bg="#1a1a2e",
            activebackground="#1a1a2e",
            activeforeground="#e0e0ff",
            selectcolor="#23233f",
        ).pack(anchor="w", pady=(4, 10))

        buttons = tk.Frame(frame, bg="#1a1a2e")
        buttons.pack(fill="x", pady=(4, 4))
        tk.Button(buttons, text="Open extension folder", command=open_extension_folder).pack(side="left")
        tk.Button(buttons, text="Open Chrome extensions", command=open_chrome_extensions).pack(side="left", padx=8)

        screening_row = tk.Frame(frame, bg="#1a1a2e")
        screening_row.pack(fill="x", pady=(0, 12))
        tk.Button(screening_row, text="Take screening test",
                  command=lambda: ScreeningDialog(self)).pack(side="left")

        tk.Frame(frame, height=1, bg="#33335a").pack(fill="x", pady=10)

        self.font_status = tk.StringVar()
        tk.Label(
            frame,
            textvariable=self.font_status,
            fg="#ffd166",
            bg="#1a1a2e",
            wraplength=390,
            justify="left",
        ).pack(anchor="w")

        windows_buttons = tk.Frame(frame, bg="#1a1a2e")
        windows_buttons.pack(fill="x", pady=(10, 0))
        self.apply_button = tk.Button(
            windows_buttons,
            text="Apply Windows substitution",
            command=self._apply_windows_fonts,
        )
        self.apply_button.pack(side="left")
        tk.Button(
            windows_buttons,
            text="Restore Windows fonts",
            command=self._restore_windows_fonts,
        ).pack(side="left", padx=8)

        self._on_font_change()

    def _toggle_bold(self):
        self.weight_var.set("700" if self.bold_var.get() else "inherit")
        self._save_from_ui()

    def _save_from_ui(self):
        self.settings.update(
            {
                "website_enabled": bool(self.website_var.get()),
                "font_family": self.font_var.get(),
                "letter_spacing": round(float(self.spacing_var.get()), 2),
                "line_height": round(float(self.line_var.get()), 2),
                "font_weight": self.weight_var.get(),
            }
        )
        save_settings(self.settings)

    def _on_font_change(self):
        self._save_from_ui()
        font_name = self.font_var.get()
        if not plat.IS_WINDOWS:
            self.font_status.set("This feature is intended for Windows.")
            self.apply_button.configure(state="disabled")
            return
        if is_font_installed(font_name):
            self.font_status.set(f"{font_name} appears to be installed.")
            self.apply_button.configure(state="normal")
        else:
            self.font_status.set(
                f"{font_name} was not found in Windows fonts. Install it first, "
                "or choose a font that is already installed."
            )
            self.apply_button.configure(state="disabled")

    def _apply_windows_fonts(self):
        font_name = self.font_var.get()
        message = (
            f"This will set current-user Windows font substitutions to {font_name} "
            "for common UI fonts. A backup will be saved so this feature can restore them. "
            "Some apps may require sign-out or restart. Continue?"
        )
        if not messagebox.askyesno("Apply Windows font substitution", message):
            return
        try:
            apply_windows_substitution(font_name, list(SUBSTITUTION_TARGETS))
            messagebox.showinfo(
                "Dyslexia Font",
                "Windows font substitution was applied. Sign out or restart if apps do not update.",
            )
            log(f"applied Windows font substitution to {font_name}")
        except Exception as e:
            log(f"apply failed: {e}")
            messagebox.showerror("Dyslexia Font", str(e))

    def _restore_windows_fonts(self):
        if not messagebox.askyesno(
            "Restore Windows fonts",
            "Restore the font substitution values backed up by Dyslexia Font?",
        ):
            return
        try:
            restore_windows_substitution()
            messagebox.showinfo("Dyslexia Font", "Windows font substitutions were restored.")
            log("restored Windows font substitutions")
        except Exception as e:
            log(f"restore failed: {e}")
            messagebox.showerror("Dyslexia Font", str(e))

    def _shutdown(self, *_args):
        log("shutting down")
        try:
            self.destroy()
        except Exception:
            pass
        sys.exit(0)


def main():
    log("feature started")
    app = DyslexiaFontApp()
    app.mainloop()


if __name__ == "__main__":
    main()
