"""Color Blind Filter — toggles Windows' built-in system-wide color filter.

Runs as its own process when toggled ON in the hub. Every control (enable
switch, filter type) now lives in the hub's settings sheet, which calls
`set_filter()` directly since this is a single registry write, not continuous
state — see hub.py's `_apply_colorblind_filter`. This process draws one small
pill confirming the current filter, and re-applies it on startup in case the
setting changed while it wasn't running.

Applies system-wide (not just Chrome) by driving the OS's own colour-filter
feature: on Windows the Ease of Access filter, toggled with its real
Win+Ctrl+C shortcut because a registry write alone does not repaint the
screen (see `_set_filter_windows`); on macOS the Accessibility Color Filters
checkbox, scripted as a best effort since Apple exposes no API for it.
See features/README.md.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import time
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

# How long to wait for Windows to write `Active` back after the shortcut fires.
TOGGLE_SETTLE_S = 1.5
TOGGLE_POLL_S = 0.05

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


def _remember_mac_state(enabled: bool):
    """macOS gives us nothing to read back, so record what we last applied."""
    settings = load_settings()
    settings["_mac_applied_on"] = bool(enabled)
    save_settings(settings)


def is_filter_active() -> bool:
    """Whether the filter is on right now.

    On Windows this reads `Active`, which is truthful precisely because
    `_set_filter_windows` never writes it — only the OS shortcut does.
    macOS has no equivalent readback, so we fall back to what we last
    applied ourselves.
    """
    if plat.IS_WINDOWS:
        import winreg

        return bool(_read_registry_dword(winreg.HKEY_CURRENT_USER,
                                         COLOR_FILTERING_PATH, "Active"))
    if plat.IS_MAC:
        return bool(load_settings().get("_mac_applied_on", False))
    return False


def _press_toggle_shortcut():
    """Fire Windows' own Win+Ctrl+C color-filter shortcut.

    This is the ONLY thing that changes the filter live. Writing
    `ColorFiltering\\Active` by hand updates what Windows will use at its
    next sync point but does not repaint the screen — Microsoft's docs say
    a sign-out is required for a registry-only change, and a real user hit
    exactly that: the registry read back "off" while the screen was still
    grey, and stayed grey until this shortcut was pressed.
    """
    import pyautogui

    pyautogui.hotkey("win", "ctrl", "c")


def _wait_for_active(target: bool) -> bool:
    """Wait for Windows to write `Active` back after the shortcut fired."""
    deadline = time.monotonic() + TOGGLE_SETTLE_S
    while time.monotonic() < deadline:
        if is_filter_active() == target:
            return True
        time.sleep(TOGGLE_POLL_S)
    return is_filter_active() == target


def _set_filter_windows(enabled: bool, filter_type: int):
    """Drive Windows' real color-filter toggle, then verify it took.

    Deliberately does NOT write `Active` itself. Because the OS shortcut is
    the only thing that changes it, `Active` stays a truthful mirror of what
    is actually on screen — which is what keeps this in sync. Writing it
    directly is what desynced a real user into a stuck-grey screen: the app
    set it to 0, the screen stayed on, and every later toggle reasoned from
    a value that no longer described reality.

    Known limitation: if the filter is toggled outside this app (Windows
    Settings, or the user pressing Win+Ctrl+C themselves) between our read
    and our keypress, we can still flip the wrong way. Windows exposes no
    live "is the filter painting right now" signal to close that window.
    """
    import winreg

    # Off by default on a fresh Windows install — without this the shortcut
    # below does nothing at all, which is the difference between "works on
    # my machine" and "works on the machine we ship to".
    _set_registry_dword(winreg.HKEY_CURRENT_USER, COLOR_FILTERING_PATH, "HotkeyEnabled", 1)
    # Safe to write directly: it only says WHICH filter, and is read when the
    # filter turns on.
    _set_registry_dword(winreg.HKEY_CURRENT_USER, COLOR_FILTERING_PATH, "FilterType", filter_type)
    _set_registry_string(winreg.HKEY_CURRENT_USER, ACCESSIBILITY_PATH, "Configuration",
                         "colorfiltering" if enabled else "")

    was_active = is_filter_active()
    presses = 0
    if enabled and was_active:
        # Already on, but the type may have just changed — a running filter
        # does not re-read FilterType, so cycle it off and back on. This is
        # the fix for "I clicked through every filter and nothing changed".
        _press_toggle_shortcut()
        _wait_for_active(False)
        _press_toggle_shortcut()
        presses = 2
    elif was_active != enabled:
        _press_toggle_shortcut()
        presses = 1

    verified = _wait_for_active(enabled) if presses else (was_active == enabled)
    console.log_event("colorblind_filter", "set_filter", platform="windows",
                      requested_enabled=enabled, requested_filter_type=filter_type,
                      was_active=was_active, shortcut_presses=presses,
                      verified_active=is_filter_active(), ok=verified)
    if not verified:
        raise RuntimeError(
            "Windows did not apply the color filter. Check that Color Filters "
            "are allowed in Settings > Accessibility > Color filters.")


# macOS's own Color Filters popup (Accessibility > Display) offers these exact
# labels, read off the live menu — NOT the same wording as our Windows-facing
# FILTER_TYPES names. "Invert" / "Grayscale Inverted" have no equivalent here:
# macOS's "Invert colors" switch lives in a different section of the same
# page, far from the "Color Filters" anchor, and System Settings' SwiftUI
# list only materializes accessibility elements for rows that are actually
# scrolled into view — so once the anchor scrolls to Color Filters, "Invert
# colors" often silently isn't in the tree at all (confirmed on 26.5.2: it
# was found once right after opening the pane, then vanished from the same
# query moments later with no UI change of ours in between). Scripting it
# would be flaky in a way scripting the anchored controls below is not, so
# it's left unsupported here rather than pretending to work.
_MAC_FILTER_LABELS = {
    "Deuteranopia": "Green/Red filter (Deuteranopia)",
    "Protanopia": "Red/Green filter (Protanopia)",
    "Tritanopia": "Blue/Yellow filter (Tritanopia)",
    "Grayscale": "Grayscale",
}


def _set_filter_mac(enabled: bool, filter_type_name: str):
    """Toggle macOS' Color Filters by scripting the Accessibility > Display pane.

    BEST EFFORT. macOS exposes no public API for this. The previous version
    used `reveal anchor "Seeing_ColorFilters" of pane id
    "com.apple.preference.universalaccess"` — the old System *Preferences*
    AppleScript API, which System *Settings* (Ventura+) no longer implements
    for this pane: it fails with "Can't get pane id ... (-1728)" on every
    current macOS (verified on 26.5.2), so nothing was ever toggled. It also
    clicked "the first checkbox found anywhere in the window", which was
    never guaranteed to be the right one even on a version where the reveal
    worked. This version opens the pane with the `open` URL scheme (the
    replacement Apple ships for deep-linking System Settings) and finds
    "Color filters" / "Filter type" by their real accessibility name, so a
    failure here means "Apple renamed a control", not "Apple moved the
    checkbox".

    Requires the user to grant Accessibility permission to whatever runs
    Python (System Settings > Privacy & Security > Accessibility).
    """
    if filter_type_name not in _MAC_FILTER_LABELS:
        raise RuntimeError(
            f"{filter_type_name!r} has no equivalent in macOS's Color Filters — "
            "use Deuteranopia, Protanopia, Tritanopia, or Grayscale on macOS.")
    want_filter_on = enabled
    filter_label = _MAC_FILTER_LABELS[filter_type_name]

    subprocess.run(
        ["open", "x-apple.systempreferences:com.apple.Accessibility-Settings.extension"
                  "?Seeing_ColorFilters"],
        capture_output=True)
    # `open` re-navigates to the anchor even if the pane is already showing
    # (e.g. re-applying while System Settings never closed), which restarts
    # the same scroll/reflow animation as a cold open. Without this, the
    # element search below can grab a reference mid-reflow: the popup opens
    # and a menu item gets clicked, but the selection never commits, because
    # the AXPopUpButton it clicked was about to be replaced by the reflow.
    time.sleep(0.6)

    script = f'''
    tell application "System Settings" to activate
    delay 0.3
    tell application "System Events"
        tell process "System Settings"
            set filterCB to missing value
            repeat 20 times
                try
                    set els to entire contents of window 1
                    repeat with el in els
                        try
                            if (role of el as text) is "AXCheckBox" and (name of el as text) is "Color filters" then
                                set filterCB to el
                                exit repeat
                            end if
                        end try
                    end repeat
                end try
                if filterCB is not missing value then exit repeat
                delay 0.3
            end repeat
            if filterCB is missing value then
                error "could not locate the Color filters control (Apple may have renamed it)"
            end if
            if ((value of filterCB) as integer) is not {1 if want_filter_on else 0} then
                click filterCB
                -- toggling this checkbox inserts a new "Intensity" slider row
                -- right above the popup, which briefly reflows the list; grab
                -- the popup only after that settles, or the click below can
                -- land on a stale/mid-transition reference and silently miss.
                delay 0.5
            end if

            if {1 if want_filter_on else 0} is 1 then
                set popupOk to false
                repeat 3 times
                    -- re-fetch fresh each attempt: a reference held across a
                    -- prior failed click can be stale, and a stale reference
                    -- accepts "click" and "click menu item" without error
                    -- while doing nothing — the exact failure mode that made
                    -- the previous version silently leave the wrong filter
                    -- type selected.
                    set thePopup to missing value
                    repeat 20 times
                        try
                            set els2 to entire contents of window 1
                            repeat with el in els2
                                try
                                    if (role of el as text) is "AXPopUpButton" and (name of el as text) is "Filter type" then
                                        set thePopup to el
                                        exit repeat
                                    end if
                                end try
                            end repeat
                        end try
                        if thePopup is not missing value then exit repeat
                        delay 0.3
                    end repeat
                    if thePopup is missing value then
                        error "could not locate the Filter type control (Apple may have renamed it)"
                    end if
                    if (value of thePopup as text) is "{filter_label}" then
                        set popupOk to true
                        exit repeat
                    end if
                    click thePopup
                    delay 0.3
                    click menu item "{filter_label}" of menu 1 of thePopup
                    delay 0.3
                end repeat
                if not popupOk then
                    error "Filter type selection did not take after 3 attempts"
                end if
            end if
        end tell
    end tell
    tell application "System Settings" to quit
    '''
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    ok = result.returncode == 0
    console.log_event("colorblind_filter", "set_filter", platform="mac",
                      requested_enabled=enabled, filter_type_name=filter_type_name,
                      ok=ok, error=(result.stderr or "").strip() or None)
    if not ok:
        raise RuntimeError(
            "Could not toggle Color Filters. Grant Accessibility permission to "
            "Python in System Settings > Privacy & Security > Accessibility.")
    _remember_mac_state(enabled)


def set_filter(enabled: bool, filter_type: int):
    """Apply the colour filter on whichever OS we are on."""
    if plat.IS_WINDOWS:
        _set_filter_windows(enabled, filter_type)
    elif plat.IS_MAC:
        name = next((n for n, v in FILTER_TYPES.items() if v == filter_type), "Grayscale")
        _set_filter_mac(enabled, name)
    else:
        raise RuntimeError("Color filters are only available on Windows and macOS.")


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
        console.log_event("colorblind_subprocess", "on_started", settings=self.settings)
        self._draw()
        self.bubble.place_stacked("colorblind_filter")
        self.bubble.show(for_ms=SHOW_MS)
        self._sched.after(WATCH_MS, self._watch_settings)

    def _label(self) -> str:
        if not (plat.IS_WINDOWS or plat.IS_MAC):
            return "Not supported here"
        if not self.settings.get("enabled"):
            return "Filter off"
        return f"Filter: {self.settings.get('filter_name', 'Deuteranopia')}"

    def _draw(self):
        on = bool((plat.IS_WINDOWS or plat.IS_MAC) and self.settings.get("enabled"))
        self.bubble.set_text("label", self._label())
        self.bubble.set_style("dot", "background",
                              C["ON"] if on else C["DOT_IDLE"])

    def _watch_settings(self):
        # Display only. The hub owns every call to set_filter() — applying
        # from here too would fight it, and since turning the filter on is a
        # real keystroke to the OS, a second apply visibly flickers the
        # screen off and back on.
        if self._watcher.changed():
            self.settings = load_settings()
            log(f"settings changed in the hub — {self._label()}")
            self._draw()
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
