"""Color Blind Filter — toggles Windows' built-in system-wide color filter.

Runs as its own process when toggled ON in the hub. Every control (enable
switch, filter type) now lives in the hub's settings sheet, which calls
`set_filter()` directly since this is a single registry write, not continuous
state — see hub.py's `_apply_colorblind_filter`. This process draws one small
pill confirming the current filter, and re-applies it on startup in case the
setting changed while it wasn't running.

Applies system-wide (not just Chrome) by driving Windows' own Ease of
Access > Color Filters feature: set HKCU\\Software\\Microsoft\\ColorFiltering
directly (see `set_filter()` below for why a live-toggle shortcut isn't used).
See features/README.md.
"""

from __future__ import annotations

import signal
import sys
from pathlib import Path

FEATURE_DIR = Path(__file__).resolve().parent
ROOT = FEATURE_DIR.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import console, platform as plat, settings_store as store  # noqa: E402
from shared import webbubble as wb                                     # noqa: E402
from shared.ui_kit import C                                            # noqa: E402

console.configure_stdio()

FEATURE_ID = "colorblind_filter"
COLOR_FILTERING_PATH = r"Software\Microsoft\ColorFiltering"
ACCESSIBILITY_PATH = r"Software\Microsoft\Windows NT\CurrentVersion\Accessibility"

FILTER_TYPES = {
    "Deuteranopia": 3,
    "Protanopia": 4,
    "Tritanopia": 5,
    "Grayscale": 0,
    "Invert": 1,
    "Grayscale Inverted": 2,
}

# The design's confirmation bubble: a 52px pill, a status dot, one line.
# Sized for the longest label ("Filter: Grayscale Inverted") so the pill never
# has to resize itself while it is on screen.
BUBBLE_W, BUBBLE_H = 288, 52
SHOW_MS = 3000          # how long the confirmation stays up
WATCH_MS = 700          # how often we notice the hub editing settings.json

BODY = """
<div class="pill" id="pill">
  <span class="dot" id="dot"></span>
  <span class="label" id="label"></span>
</div>
"""
CSS = """
#pill { height: 52px; gap: 12px; padding: 0 20px; }
"""


def log(msg: str):
    console.safe_print(f"[colorblind_filter] {msg}", flush=True)


def default_settings() -> dict:
    return dict(store.DEFAULTS[FEATURE_ID])


def load_settings() -> dict:
    return store.load(FEATURE_ID)


def save_settings(settings: dict):
    store.save(FEATURE_ID, settings)


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


def _set_registry_string(root, path: str, name: str, value: str):
    import winreg

    with winreg.CreateKeyEx(root, path, 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)


def is_filter_active() -> bool:
    if not plat.IS_WINDOWS:
        return False
    import winreg

    return bool(_read_registry_dword(winreg.HKEY_CURRENT_USER, COLOR_FILTERING_PATH, "Active"))


def set_filter(enabled: bool, filter_type: int):
    """Write both registry locations Windows tracks for color filters.

    `ColorFiltering\\Active`/`FilterType` alone is not enough — Windows also
    tracks which assistive feature is active in `...\\Accessibility`'s
    `Configuration` string (`"colorfiltering"` when on, `""` when off).
    Writing only the first location left the filter invisible until a
    lock/unlock and every filter type looking identical, which is the exact
    bug a real user hit — Windows' own color-filter `.reg` exports touch
    both keys, confirmed via research.

    `atbroker.exe /colorfiltershortcut` (simulating the Win+Ctrl+C shortcut,
    tried with and without the `/resettransferkeys` flag) reliably returns
    exit code 1 and does nothing in this environment — re-tested live in a
    real interactive session, not just the earlier sandboxed one, same
    result both times. Not used: it's also a TOGGLE relative to Windows'
    own internal state, so even if it worked, calling it after we've just
    written the correct Active value risks flipping it right back off.
    """
    if not plat.IS_WINDOWS:
        raise RuntimeError("Color filters are only available on Windows.")
    import winreg

    _set_registry_dword(winreg.HKEY_CURRENT_USER, COLOR_FILTERING_PATH, "FilterType", filter_type)
    _set_registry_dword(winreg.HKEY_CURRENT_USER, COLOR_FILTERING_PATH, "Active", 1 if enabled else 0)
    _set_registry_string(winreg.HKEY_CURRENT_USER, ACCESSIBILITY_PATH, "Configuration",
                         "colorfiltering" if enabled else "")
    readback_active = _read_registry_dword(winreg.HKEY_CURRENT_USER, COLOR_FILTERING_PATH, "Active")
    readback_filter_type = _read_registry_dword(winreg.HKEY_CURRENT_USER, COLOR_FILTERING_PATH, "FilterType")
    verified = is_filter_active()
    console.log_event("colorblind_filter", "set_filter", requested_enabled=enabled,
                      requested_filter_type=filter_type,
                      written_active=1 if enabled else 0,
                      readback_active=readback_active,
                      readback_filter_type=readback_filter_type,
                      is_filter_active_result=verified)
    if verified != enabled:
        raise RuntimeError("Registry updated but Windows did not confirm the new state.")


class ColorblindFilterApp:
    """The bubble: a dot (coloured when on, muted when off) and the filter name."""

    def __init__(self):
        self.settings = load_settings()
        self._watcher = store.Watcher(FEATURE_ID)
        self._sched = wb.Scheduler(on_error=lambda e: log(f"timer failed: {e}"))
        self.bubble = wb.Bubble("Color Blind Filter", BODY, BUBBLE_W, BUBBLE_H,
                                css=CSS, sched=self._sched,
                                on_closed=self._sched.stop)
        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT, self._shutdown)

    def run(self):
        self.bubble.run(self._on_started)

    def _on_started(self):
        console.log_event("colorblind_subprocess", "apply_start", trigger="on_started",
                          settings=self.settings)
        self._apply_and_draw()
        save_settings(self.settings)
        self.bubble.place_stacked("colorblind_filter")
        self.bubble.show(for_ms=SHOW_MS)
        self._sched.after(WATCH_MS, self._watch_settings)

    def _label(self) -> str:
        if not plat.IS_WINDOWS:
            return "Windows only"
        if not self.settings.get("enabled"):
            return "Filter off"
        return f"Filter: {self.settings.get('filter_name', 'Deuteranopia')}"

    def _draw(self):
        on = bool(plat.IS_WINDOWS and self.settings.get("enabled"))
        self.bubble.set_text("label", self._label())
        self.bubble.set_style("dot", "background",
                              C["ON"] if on else C["DOT_IDLE"])

    def _apply_and_draw(self):
        if plat.IS_WINDOWS:
            try:
                set_filter(bool(self.settings.get("enabled", False)),
                          FILTER_TYPES[self.settings.get("filter_name", "Deuteranopia")])
            except Exception as e:
                log(f"apply failed: {e}")
        self._draw()

    def _watch_settings(self):
        if self._watcher.changed():
            self.settings = load_settings()
            log(f"settings changed in the hub — {self._label()}")
            console.log_event("colorblind_subprocess", "apply_start", trigger="watch_settings",
                              settings=self.settings)
            self._apply_and_draw()
            self.bubble.place_stacked("colorblind_filter")
        self.bubble.show(for_ms=SHOW_MS)
        self._sched.after(WATCH_MS, self._watch_settings)

    def _shutdown(self, *_args):
        log("shutting down")
        self._sched.stop()
        self.bubble.close()
        sys.exit(0)


def main():
    log("feature started")
    ColorblindFilterApp().run()


if __name__ == "__main__":
    main()
