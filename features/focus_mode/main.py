"""Focus Mode — blocks distracting sites system-wide for a timed session.

Runs as its own process when toggled ON in the hub. Blocks system-wide (not
just Chrome) by redirecting configured domains to 127.0.0.1 in the OS hosts
file — works for every browser and app, not just one. Requires admin/root to
edit the hosts file.

The block-list, duration, and "Blocking now" switch all live in the hub's
settings sheet (features/README.md / shared/settings_store.py). This process
watches settings.json (shared.settings_store.Watcher, same mechanism voice
control's always_on/dictation_mode use) and owns the countdown + auto-unblock
timer on its own scheduler, since only it stays alive for the whole blocking
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

FEATURE_DIR = Path(__file__).resolve().parent
ROOT = FEATURE_DIR.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import console, feature_bus, platform as plat  # noqa: E402
from shared import settings_store as store, webbubble as wb  # noqa: E402

console.configure_stdio()

FEATURE_ID = "focus_mode"
BACKUP_FILE = FEATURE_DIR / "hosts_backup.txt"
ELEVATE_PAYLOAD_FILE = FEATURE_DIR / "elevate_payload.json"
ELEVATE_STATUS_FILE = FEATURE_DIR / "elevate_status.json"
HOSTS_PATH = (Path(r"C:\Windows\System32\drivers\etc\hosts") if plat.IS_WINDOWS
              else Path("/etc/hosts"))
MARK_BEGIN = "# BEGIN Accessibility4all Focus Mode"
MARK_END = "# END Accessibility4all Focus Mode"

# Hidden while idle; while a session runs it is the design's confirmation pill,
# with a red dot and a live countdown. Fixed width so a digit changing once a
# second never resizes the window under the user.
BUBBLE_W, BUBBLE_H = 236, 52
WATCH_MS = 700
TICK_MS = 1000

BODY = """
<div class="pill" id="pill">
  <span class="dot" id="dot"></span>
  <span class="label" id="label">Blocking</span>
</div>
"""
CSS = """
#pill { height: 52px; gap: 12px; padding: 0 20px; border-color: var(--accent); }
#dot  { background: var(--stop); }
/* the countdown ticks once a second — the digits must not jump around, so the
   figures are tabular and the line is centred in a fixed-width pill */
#label { font-variant-numeric: tabular-nums; }
"""


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


def _marker_present() -> bool:
    try:
        return HOSTS_PATH.exists() and MARK_BEGIN in HOSTS_PATH.read_text(
            encoding="utf-8", errors="ignore")
    except Exception:
        return False


def apply_block(domains: list[str]):
    needed_elevation = not is_admin()
    try:
        if needed_elevation:
            _elevate_hosts_action("apply", domains)
        else:
            _write_apply(domains)
        console.log_event("focus_mode", "apply_block", domains=domains,
                          needed_elevation=needed_elevation, ok=True,
                          marker_present=_marker_present())
    except Exception as e:
        console.log_event("focus_mode", "apply_block", domains=domains,
                          needed_elevation=needed_elevation, ok=False,
                          error=str(e), marker_present=_marker_present())
        raise


def remove_block():
    if not HOSTS_PATH.exists():
        return
    current = HOSTS_PATH.read_text(encoding="utf-8", errors="ignore")
    if MARK_BEGIN not in current:
        return
    needed_elevation = not is_admin()
    try:
        if needed_elevation:
            _elevate_hosts_action("remove", None)
        else:
            _write_remove()
        console.log_event("focus_mode", "remove_block",
                          needed_elevation=needed_elevation, ok=True,
                          marker_present=_marker_present())
    except Exception as e:
        console.log_event("focus_mode", "remove_block",
                          needed_elevation=needed_elevation, ok=False,
                          error=str(e), marker_present=_marker_present())
        raise


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


class FocusModeApp:
    """The bubble: hidden while idle, a pill with a live countdown while blocking."""

    def __init__(self):
        self.settings = load_settings()
        self._watcher = store.Watcher(FEATURE_ID)
        self._active = False
        self._end_time = 0.0
        self._tick_id: int | None = None
        self._sched = wb.Scheduler(on_error=lambda e: log(f"timer failed: {e}"))
        self.bubble = wb.Bubble("Focus Mode", BODY, BUBBLE_W, BUBBLE_H,
                                css=CSS, hidden=True, sched=self._sched,
                                on_closed=self._sched.stop)
        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT, self._shutdown)

    def run(self):
        self.bubble.run(self._on_started)

    def _on_started(self):
        self._cleanup_stale_block()
        if bool(self.settings.get("active", False)):
            self._begin_session(resuming=True)
        save_settings(self.settings)
        self._sched.after(WATCH_MS, self._watch_settings)

    def _cleanup_stale_block(self):
        """Clear a block left over from a previous crash if we're not resuming it.

        Must go through remove_block()'s own elevation path, not just
        is_admin() — most launches aren't elevated, so gating on is_admin()
        alone meant a crash-left block only ever healed itself on an
        elevated run, leaving sites blocked indefinitely otherwise.
        """
        try:
            has_block = HOSTS_PATH.exists() and MARK_BEGIN in HOSTS_PATH.read_text(
                encoding="utf-8", errors="ignore")
            console.log_event("focus_mode_subprocess", "cleanup_stale_block_check",
                              has_block=has_block, settings_active=bool(self.settings.get("active")))
            if has_block and not self.settings.get("active"):
                remove_block()
                log("cleared stale block from a previous session")
        except Exception as e:
            log(f"stale-block cleanup failed: {e}")
            console.log_event("focus_mode_subprocess", "cleanup_stale_block_failed", error=str(e))

    # ── session lifecycle ──
    def _begin_session(self, resuming: bool = False):
        domains = self.settings.get("blocklist", [])
        minutes = max(1, int(self.settings.get("duration_minutes", 25)))
        end_time = float(self.settings.get("end_time", 0.0))
        if not resuming or end_time <= time.time():
            end_time = time.time() + minutes * 60
        already_blocked = HOSTS_PATH.exists() and MARK_BEGIN in HOSTS_PATH.read_text(
            encoding="utf-8", errors="ignore")
        console.log_event("focus_mode_subprocess", "begin_session", resuming=resuming,
                          domains=domains, already_blocked=already_blocked)
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
        self.bubble.place_stacked(FEATURE_ID)
        feature_bus.update_presence(FEATURE_ID, os.getpid(), self.bubble.rect())
        self.bubble.show()
        self._tick()

    def _end_session(self, reason: str):
        try:
            remove_block()
        except Exception as e:
            log(f"remove_block failed: {e}")
        self._active = False
        if self._tick_id:
            self._sched.after_cancel(self._tick_id)
            self._tick_id = None
        self.settings["active"] = False
        self.settings["end_time"] = 0.0
        save_settings(self.settings)
        log(f"session ended: {reason}")
        feature_bus.remove_presence(FEATURE_ID)
        self.bubble.hide()

    def _tick(self):
        if not self._active:
            return
        remaining = self._end_time - time.time()
        if remaining <= 0:
            self._end_session("timer elapsed")
            return
        mins, secs = divmod(int(remaining), 60)
        self.bubble.set_text("label", f"Blocking — {mins}m {secs}s")
        self._tick_id = self._sched.after(TICK_MS, self._tick)

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
        self._sched.after(WATCH_MS, self._watch_settings)

    def _shutdown(self, *_args):
        log("shutting down")
        feature_bus.remove_presence(FEATURE_ID)
        if self._active:
            try:
                remove_block()
            except Exception as e:
                log(f"could not clear block on shutdown: {e}")
            self.settings["active"] = False
            self.settings["end_time"] = 0.0
            save_settings(self.settings)
        self._sched.stop()
        self.bubble.close()
        sys.exit(0)


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--elevated-run":
        _run_elevated_worker(sys.argv[2])
        return
    log("feature started")
    FocusModeApp().run()


if __name__ == "__main__":
    main()
