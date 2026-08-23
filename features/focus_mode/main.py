"""Focus Mode — blocks distracting sites system-wide for a timed session.

Runs as its own process when toggled ON in the hub. Blocks system-wide (not
just Chrome) by redirecting configured domains to 127.0.0.1 in the OS hosts
file — works for every browser and app, not just one. Requires admin/root to
edit the hosts file. The block is removed automatically when the timer ends
or the feature is toggled off, so it never outlives this process. See
features/README.md.
"""

from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import tkinter as tk
from tkinter import font as tkfont, messagebox

FEATURE_DIR = Path(__file__).resolve().parent
ROOT = FEATURE_DIR.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import console, feature_bus, platform as plat  # noqa: E402

console.configure_stdio()

SETTINGS_FILE = FEATURE_DIR / "settings.json"
BACKUP_FILE = FEATURE_DIR / "hosts_backup.txt"
HOSTS_PATH = (Path(r"C:\Windows\System32\drivers\etc\hosts") if plat.IS_WINDOWS
              else Path("/etc/hosts"))
MARK_BEGIN = "# BEGIN Accessibility4all Focus Mode"
MARK_END = "# END Accessibility4all Focus Mode"

BG = "#1a1a2e"
CARD = "#23233f"
FG = "#e0e0ff"
MUTED = "#8a8ab0"
ACCENT = "#748ffc"
OK = "#69db7c"
WARN = "#ffd166"
REC = "#ff6b6b"

WIN_W, WIN_H = 360, 340


def log(msg: str):
    console.safe_print(f"[focus_mode] {msg}", flush=True)


def default_settings() -> dict:
    return {"blocklist": ["youtube.com", "twitter.com", "reddit.com"], "duration_minutes": 25}


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


def is_admin() -> bool:
    try:
        if plat.IS_WINDOWS:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        return os.geteuid() == 0
    except Exception:
        return False


def _strip_block(text: str) -> str:
    out, skipping = [], False
    for line in text.splitlines():
        if line.strip() == MARK_BEGIN:
            skipping = True
            continue
        if line.strip() == MARK_END:
            skipping = False
            continue
        if not skipping:
            out.append(line)
    return "\n".join(out).rstrip("\n") + "\n"


def _flush_dns():
    try:
        if plat.IS_WINDOWS:
            subprocess.run(["ipconfig", "/flushdns"], check=False, capture_output=True)
        else:
            subprocess.run(["dscacheutil", "-flushcache"], check=False, capture_output=True)
    except Exception:
        pass


def apply_block(domains: list[str]):
    if not is_admin():
        raise PermissionError("Run the hub as Administrator to block sites.")
    original = HOSTS_PATH.read_text(encoding="utf-8", errors="ignore")
    if not BACKUP_FILE.exists():
        BACKUP_FILE.write_text(original, encoding="utf-8")
    base = _strip_block(original)
    lines = [MARK_BEGIN]
    for d in domains:
        d = d.strip().lower()
        if not d:
            continue
        lines.append(f"127.0.0.1 {d}")
        lines.append(f"127.0.0.1 www.{d}")
    lines.append(MARK_END)
    HOSTS_PATH.write_text(base + "\n".join(lines) + "\n", encoding="utf-8")
    _flush_dns()


def remove_block():
    if not HOSTS_PATH.exists():
        return
    current = HOSTS_PATH.read_text(encoding="utf-8", errors="ignore")
    if MARK_BEGIN not in current:
        return
    if not is_admin():
        raise PermissionError("Run the hub as Administrator to unblock sites.")
    HOSTS_PATH.write_text(_strip_block(current), encoding="utf-8")
    _flush_dns()


class FocusModeApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self._active = False
        self._end_time = 0.0
        self._tick_id = None

        self.title("Focus Mode")
        self.resizable(False, False)
        self.geometry(f"{WIN_W}x{WIN_H}")
        self.configure(bg=BG)
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self._shutdown)

        self._build_ui()
        save_settings(self.settings)
        self._cleanup_stale_block()
        feature_bus.update_presence("focus_mode", os.getpid())
        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT, self._shutdown)

    def _cleanup_stale_block(self):
        """Clear any block left over from a previous crash, since no timer owns it now."""
        try:
            if HOSTS_PATH.exists() and MARK_BEGIN in HOSTS_PATH.read_text(
                    encoding="utf-8", errors="ignore") and is_admin():
                remove_block()
                log("cleared stale block from a previous session")
        except Exception as e:
            log(f"stale-block cleanup skipped: {e}")

    def _build_ui(self):
        frame = tk.Frame(self, bg=BG)
        frame.pack(fill="both", expand=True, padx=16, pady=14)

        tk.Label(frame, text="Focus Mode", font=tkfont.Font(family="Helvetica", size=16, weight="bold"),
                 fg=FG, bg=BG).pack(anchor="w")
        tk.Label(frame, text="Blocks sites system-wide via the hosts file",
                 font=tkfont.Font(family="Helvetica", size=10), fg=MUTED, bg=BG).pack(
            anchor="w", pady=(0, 10))

        if not is_admin():
            tk.Label(frame, text="Not running as Administrator — blocking will fail. "
                                  "Restart the hub as Administrator.",
                     font=tkfont.Font(family="Helvetica", size=9), fg=WARN, bg=BG,
                     wraplength=WIN_W - 32, justify="left").pack(anchor="w", pady=(0, 10))

        tk.Label(frame, text="Blocked sites (one per line)", fg=FG, bg=BG,
                 font=tkfont.Font(family="Helvetica", size=9)).pack(anchor="w")
        self._blocklist_text = tk.Text(frame, height=5, width=36, bg=CARD, fg=FG,
                                        insertbackground=FG, relief="flat")
        self._blocklist_text.pack(fill="x", pady=(2, 8))
        self._blocklist_text.insert("1.0", "\n".join(self.settings["blocklist"]))

        duration_row = tk.Frame(frame, bg=BG)
        duration_row.pack(fill="x", pady=(0, 10))
        tk.Label(duration_row, text="Duration (minutes)", fg=FG, bg=BG,
                 font=tkfont.Font(family="Helvetica", size=9)).pack(side="left")
        self._duration_var = tk.IntVar(value=int(self.settings["duration_minutes"]))
        tk.Spinbox(duration_row, from_=1, to=480, textvariable=self._duration_var,
                   width=6, bg=CARD, fg=FG, relief="flat",
                   buttonbackground=CARD).pack(side="right")

        self._toggle_btn = tk.Button(frame, text="Start focus session", command=self._toggle,
                                      font=tkfont.Font(family="Helvetica", size=10, weight="bold"),
                                      bg=CARD, fg=FG, relief="flat", padx=14, pady=8,
                                      cursor="hand2")
        self._toggle_btn.pack(fill="x")

        self.status_var = tk.StringVar(value="Idle")
        tk.Label(frame, textvariable=self.status_var,
                 font=tkfont.Font(family="Helvetica", size=11, weight="bold"),
                 fg=MUTED, bg=BG, wraplength=WIN_W - 32, justify="left").pack(
            anchor="w", pady=(12, 0))

    def _domains(self) -> list[str]:
        raw = self._blocklist_text.get("1.0", "end").strip()
        return [d.strip() for d in raw.splitlines() if d.strip()]

    def _toggle(self):
        if self._active:
            self._stop_session()
        else:
            self._start_session()

    def _start_session(self):
        domains = self._domains()
        if not domains:
            self.status_var.set("Add at least one site to block")
            return
        minutes = max(1, int(self._duration_var.get()))
        try:
            apply_block(domains)
        except Exception as e:
            log(f"apply_block failed: {e}")
            messagebox.showerror("Focus Mode", str(e))
            return
        self.settings["blocklist"] = domains
        self.settings["duration_minutes"] = minutes
        save_settings(self.settings)
        self._active = True
        self._end_time = time.time() + minutes * 60
        self._toggle_btn.configure(text="Stop focus session")
        self._tick()
        log(f"blocking {len(domains)} site(s) for {minutes} minute(s)")

    def _stop_session(self):
        try:
            remove_block()
        except Exception as e:
            log(f"remove_block failed: {e}")
            messagebox.showerror("Focus Mode", str(e))
        self._active = False
        if self._tick_id:
            self.after_cancel(self._tick_id)
            self._tick_id = None
        self._toggle_btn.configure(text="Start focus session")
        self.status_var.set("Stopped")

    def _tick(self):
        if not self._active:
            return
        remaining = self._end_time - time.time()
        if remaining <= 0:
            self._stop_session()
            self.status_var.set("Session complete")
            return
        mins, secs = divmod(int(remaining), 60)
        self.status_var.set(f"Blocking — {mins}m {secs}s remaining")
        self._tick_id = self.after(1000, self._tick)

    def _shutdown(self, *_args):
        log("shutting down")
        if self._active:
            try:
                remove_block()
            except Exception as e:
                log(f"could not clear block on shutdown: {e}")
        feature_bus.remove_presence("focus_mode")
        try:
            self.destroy()
        except Exception:
            pass
        sys.exit(0)


def main():
    log("feature started")
    app = FocusModeApp()
    app.mainloop()
    feature_bus.remove_presence("focus_mode")
    log("exited")


if __name__ == "__main__":
    main()
