"""Focus Mode — blocks distracting sites system-wide for a timed session.

Runs as its own process when toggled ON in the hub. Blocks system-wide (not
just Chrome) by redirecting configured domains to 127.0.0.1 in the OS hosts
file — works for every browser and app, not just one. Requires admin/root to
edit the hosts file.

The block-list, duration, and "Blocking now" switch all live in the hub's
settings sheet (features/README.md / shared/settings_store.py). This process
watches settings.json (shared.settings_store.Watcher, same mechanism voice
control's always_on/dictation_mode use) and owns the countdown + auto-unblock
timer on its own Tk mainloop, since only it stays alive for the whole blocking
session regardless of whether the settings sheet is open. The block is
removed automatically when the timer ends, the switch is turned off, or this
process shuts down, so it never outlives an active session.
"""

from __future__ import annotations

import ctypes
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

import tkinter as tk

FEATURE_DIR = Path(__file__).resolve().parent
ROOT = FEATURE_DIR.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import console, platform as plat, settings_store as store, ui_kit as ui  # noqa: E402
from shared.ui_kit import C                                                          # noqa: E402

console.configure_stdio()

FEATURE_ID = "focus_mode"
BACKUP_FILE = FEATURE_DIR / "hosts_backup.txt"
ELEVATE_PAYLOAD_FILE = FEATURE_DIR / "elevate_payload.json"
ELEVATE_STATUS_FILE = FEATURE_DIR / "elevate_status.json"
HOSTS_PATH = (Path(r"C:\Windows\System32\drivers\etc\hosts") if plat.IS_WINDOWS
              else Path("/etc/hosts"))
MARK_BEGIN = "# BEGIN Accessibility4all Focus Mode"
MARK_END = "# END Accessibility4all Focus Mode"

BUBBLE_H = 52
DOT = 12
WATCH_MS = 700
TICK_MS = 1000


def log(msg: str):
    console.safe_print(f"[focus_mode] {msg}", flush=True)


def default_settings() -> dict:
    return dict(store.DEFAULTS[FEATURE_ID])


def load_settings() -> dict:
    return store.load(FEATURE_ID)


def save_settings(settings: dict):
    store.save(FEATURE_ID, settings)


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


def _write_apply(domains: list[str]):
    """The actual hosts-file write — assumes admin, called either directly
    (already elevated) or from inside the elevated worker process."""
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


def _write_remove():
    if not HOSTS_PATH.exists():
        return
    current = HOSTS_PATH.read_text(encoding="utf-8", errors="ignore")
    if MARK_BEGIN not in current:
        return
    HOSTS_PATH.write_text(_strip_block(current), encoding="utf-8")
    _flush_dns()


def _elevate_windows(action: str, domains: list[str] | None):
    """Relaunch this script elevated (one UAC prompt) to do just the hosts
    write, wait for it, and read its result from a status file — a
    runas-elevated child's stdout isn't capturable by the non-elevated
    parent, so exit code + status file is the only reliable channel."""
    import pywintypes
    import win32event
    import win32process
    from win32com.shell import shell, shellcon

    ELEVATE_PAYLOAD_FILE.write_text(
        json.dumps({"action": action, "domains": domains or []}), encoding="utf-8")
    if ELEVATE_STATUS_FILE.exists():
        ELEVATE_STATUS_FILE.unlink()

    try:
        info = shell.ShellExecuteEx(
            nShow=0,
            fMask=shellcon.SEE_MASK_NOCLOSEPROCESS,
            lpVerb="runas",
            lpFile=sys.executable,
            lpParameters=f'"{__file__}" --elevated-run "{ELEVATE_PAYLOAD_FILE}"',
        )
    except pywintypes.error as e:
        if e.winerror == 1223:  # ERROR_CANCELLED — user declined the UAC prompt
            raise PermissionError("Administrator permission was declined.")
        raise PermissionError(f"Could not request Administrator permission: {e}")

    win32event.WaitForSingleObject(info["hProcess"], win32event.INFINITE)
    code = win32process.GetExitCodeProcess(info["hProcess"])
    if code != 0 or not ELEVATE_STATUS_FILE.exists():
        raise PermissionError("Elevated hosts-file update failed.")
    status = json.loads(ELEVATE_STATUS_FILE.read_text(encoding="utf-8"))
    if not status.get("ok"):
        raise PermissionError(status.get("error") or "Elevated hosts-file update failed.")


def _elevate_mac(action: str, domains: list[str] | None):
    """osascript's `with administrator privileges` shows a native Touch
    ID/password prompt and, unlike Windows, gives a normal exit code/stdout
    back — no status-file indirection needed, but use the same payload file
    for a single consistent elevated-worker entry point on both platforms."""
    ELEVATE_PAYLOAD_FILE.write_text(
        json.dumps({"action": action, "domains": domains or []}), encoding="utf-8")
    script = (f'{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} '
              f'--elevated-run {shlex.quote(str(ELEVATE_PAYLOAD_FILE))}')
    result = subprocess.run(
        ["osascript", "-e", f'do shell script "{script}" with administrator privileges'],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise PermissionError(result.stderr.strip() or "Administrator permission was declined.")


def _elevate_hosts_action(action: str, domains: list[str] | None):
    if plat.IS_WINDOWS:
        _elevate_windows(action, domains)
    elif plat.IS_MAC:
        _elevate_mac(action, domains)
    else:
        raise PermissionError("Run as root to block or unblock sites.")


def apply_block(domains: list[str]):
    if not is_admin():
        _elevate_hosts_action("apply", domains)
        return
    _write_apply(domains)


def remove_block():
    if not HOSTS_PATH.exists():
        return
    current = HOSTS_PATH.read_text(encoding="utf-8", errors="ignore")
    if MARK_BEGIN not in current:
        return
    if not is_admin():
        _elevate_hosts_action("remove", None)
        return
    _write_remove()


def _run_elevated_worker(payload_path: str):
    """Entry point for the relaunched, elevated process — do only the hosts
    write and exit, never build the Tk GUI elevated."""
    try:
        payload = json.loads(Path(payload_path).read_text(encoding="utf-8"))
        action = payload.get("action")
        if action == "apply":
            _write_apply(payload.get("domains", []))
        elif action == "remove":
            _write_remove()
        else:
            raise ValueError(f"unknown elevated action {action!r}")
        ELEVATE_STATUS_FILE.write_text(json.dumps({"ok": True, "error": None}), encoding="utf-8")
    except Exception as e:
        try:
            ELEVATE_STATUS_FILE.write_text(json.dumps({"ok": False, "error": str(e)}), encoding="utf-8")
        except Exception:
            pass
        sys.exit(1)
    sys.exit(0)


class FocusModeApp(tk.Tk):
    """The bubble: hidden while idle, a pill with a live countdown while blocking."""

    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self._watcher = store.Watcher(FEATURE_ID)
        self._active = False
        self._end_time = 0.0
        self._tick_id: str | None = None

        self.fonts = ui.FontSet(1.0)
        self.title("Focus Mode")
        self.resizable(False, False)
        self.withdraw()
        self._width = 0
        self.canvas: tk.Canvas | None = None

        self._cleanup_stale_block()
        if bool(self.settings.get("active", False)):
            self._begin_session(resuming=True)
        save_settings(self.settings)
        self.after(WATCH_MS, self._watch_settings)
        self.protocol("WM_DELETE_WINDOW", self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT, self._shutdown)

    def _cleanup_stale_block(self):
        """Clear a block left over from a previous crash if we're not resuming it."""
        try:
            has_block = HOSTS_PATH.exists() and MARK_BEGIN in HOSTS_PATH.read_text(
                encoding="utf-8", errors="ignore")
            if has_block and not self.settings.get("active") and is_admin():
                remove_block()
                log("cleared stale block from a previous session")
        except Exception as e:
            log(f"stale-block cleanup skipped: {e}")

    # ── bubble drawing ──
    def _rebuild_canvas(self, text: str):
        text_w = self.fonts["ui"].measure(text)
        self._width = 20 * 2 + DOT + 12 + text_w
        if self.canvas is not None:
            self.canvas.destroy()
        transparent = ui.make_bubble(self, self._width, BUBBLE_H)
        self.canvas = ui.bubble_canvas(self, self._width, BUBBLE_H, transparent)
        self.canvas.pack(fill="both", expand=True)

    def _draw(self, text: str):
        self._rebuild_canvas(text)
        ui.pill(self.canvas, 1, 1, self._width - 1, BUBBLE_H - 1,
                fill=C["BUBBLE"], outline=C["ACCENT"], width=1)
        cy = BUBBLE_H / 2
        self.canvas.create_oval(20, cy - DOT / 2, 20 + DOT, cy + DOT / 2,
                                fill=C["STOP_BORDER"], outline="")
        self.canvas.create_text(20 + DOT + 12, cy, anchor="w",
                                text=text, font=self.fonts["ui"], fill=C["FG_BUBBLE"])
        ui.place_bottom_center(self, self._width, BUBBLE_H)
        ui.raise_bubble(self)

    # ── session lifecycle ──
    def _begin_session(self, resuming: bool = False):
        domains = self.settings.get("blocklist", [])
        minutes = max(1, int(self.settings.get("duration_minutes", 25)))
        end_time = float(self.settings.get("end_time", 0.0))
        if not resuming or end_time <= time.time():
            end_time = time.time() + minutes * 60
        already_blocked = HOSTS_PATH.exists() and MARK_BEGIN in HOSTS_PATH.read_text(
            encoding="utf-8", errors="ignore")
        if not already_blocked:
            # The hub applies the block itself before this process even
            # starts (see hub.py's _apply_focus_toggle) — skip a redundant
            # call here so a non-admin user isn't hit with a second
            # elevation prompt for the same toggle. Only actually apply if
            # somehow not already in place (e.g. this process started
            # independently of a hub toggle).
            try:
                apply_block(domains)
            except Exception as e:
                log(f"apply_block failed: {e}")
                self.settings["active"] = False
                save_settings(self.settings)
                return
        self._active = True
        self._end_time = end_time
        self.settings["active"] = True
        self.settings["end_time"] = end_time
        save_settings(self.settings)
        log(f"blocking {len(domains)} site(s) until {time.ctime(end_time)}")
        self._tick()

    def _end_session(self, reason: str):
        try:
            remove_block()
        except Exception as e:
            log(f"remove_block failed: {e}")
        self._active = False
        if self._tick_id:
            self.after_cancel(self._tick_id)
            self._tick_id = None
        self.settings["active"] = False
        self.settings["end_time"] = 0.0
        save_settings(self.settings)
        log(f"session ended: {reason}")
        self.withdraw()

    def _tick(self):
        if not self._active:
            return
        remaining = self._end_time - time.time()
        if remaining <= 0:
            self._end_session("timer elapsed")
            return
        mins, secs = divmod(int(remaining), 60)
        self._draw(f"Blocking — {mins}m {secs}s")
        self._tick_id = self.after(TICK_MS, self._tick)

    # ── settings (edited in the hub) ──
    def _watch_settings(self):
        if self._watcher.changed():
            self.settings = load_settings()
            want_active = bool(self.settings.get("active", False))
            if want_active and not self._active:
                log("settings changed in the hub — starting session")
                self._begin_session()
            elif not want_active and self._active:
                log("settings changed in the hub — stopping session")
                self._end_session("turned off in the hub")
        self.after(WATCH_MS, self._watch_settings)

    def _shutdown(self, *_args):
        log("shutting down")
        if self._active:
            try:
                remove_block()
            except Exception as e:
                log(f"could not clear block on shutdown: {e}")
            self.settings["active"] = False
            self.settings["end_time"] = 0.0
            save_settings(self.settings)
        try:
            self.destroy()
        except Exception:
            pass
        sys.exit(0)


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--elevated-run":
        _run_elevated_worker(sys.argv[2])
        return
    log("feature started")
    FocusModeApp().mainloop()


if __name__ == "__main__":
    main()
