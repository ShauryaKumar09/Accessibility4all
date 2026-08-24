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
import os
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
        """Clear a block left over from a previous crash if we're not resuming it."""
        try:
            has_block = HOSTS_PATH.exists() and MARK_BEGIN in HOSTS_PATH.read_text(
                encoding="utf-8", errors="ignore")
            if has_block and not self.settings.get("active") and is_admin():
                remove_block()
                log("cleared stale block from a previous session")
        except Exception as e:
            log(f"stale-block cleanup skipped: {e}")

    # ── session lifecycle ──
    def _begin_session(self, resuming: bool = False):
        domains = self.settings.get("blocklist", [])
        minutes = max(1, int(self.settings.get("duration_minutes", 25)))
        end_time = float(self.settings.get("end_time", 0.0))
        if not resuming or end_time <= time.time():
            end_time = time.time() + minutes * 60
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
    log("feature started")
    FocusModeApp().run()


if __name__ == "__main__":
    main()
