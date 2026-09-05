"""Accessibility4all — feature hub.

A pywebview app shell (`hub_ui/index.html` + `app.js` + `style.css`) backed
by this file. Each feature lives in its own folder under ./features/ and
runs as its own OS process:

    toggle ON   -> launch  features/<name>/<entry>  as a subprocess
    toggle OFF  -> terminate that subprocess

Features are discovered automatically, so separate developers can drop a new
folder into ./features/ and it appears here with no changes to this file. See
features/README.md for the contract every feature must follow.

This window is the app shell; every feature's own small always-on-top
"bubble" is a separate transparent web view in its own process (see
shared/webbubble.py). `Api` below is the one bridge between this process's
Python and the webview's JS: it owns
feature discovery, subprocess start/stop/poll, and reading/writing each
feature's settings.json (via shared/settings_store.py, which the running
feature processes already watch for changes).
"""

from __future__ import annotations

import collections
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

import webview
from dotenv import load_dotenv

from shared import console
from shared.console import configure_stdio, safe_print
from shared import autostart
from shared import cursors
from shared import hotkeys as hk
from shared import platform as plat
from shared import pynput_darwin
from shared import settings_store as store

configure_stdio()
pynput_darwin.install()
load_dotenv()

ROOT = Path(__file__).parent.resolve()
FEATURES_DIR = ROOT / "features"
UI_DIR = ROOT / "hub_ui"
# Inside hub_ui/ on purpose: WebView2 confines a file:// page to its own
# directory, so media anywhere else (even one level up via ../) fails to load
# with MEDIA_ERR_SRC_NOT_SUPPORTED and a zero-width image.
PREVIEW_DIR = UI_DIR / "preview_images"
STATE_FILE = ROOT / "hub_state.json"     # toggles + text scale
STDERR_KEEP_LINES = 25                   # tail kept per feature, to explain a crash

# Which preview_images/ folder belongs to which feature. The folders are named
# the way a person would name them ("cursor size", "tone and social cues
# images"), so the mapping is explicit rather than derived from the id.
PREVIEW_FOLDERS = {
    "voice_control": "voice control",
    "page_reader": "page reader",
    "tone_reader": "tone and social cues images",
    "colorblind_filter": "color_blind_pics",
    "focus_mode": "focus mode",
    "cursor_size": "cursor size",
}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
VIDEO_SUFFIXES = {".mov", ".mp4", ".webm", ".m4v"}


FEATURE_ORDER = ["voice_control", "page_reader", "tone_reader",
                 "colorblind_filter", "focus_mode", "cursor_size"]

# One entry per feature: display copy, settings-sheet options/shortcuts (with
# the info-popup text for each), the numbered instructions shown on its page,
# and which bespoke settings panel (if any) the frontend should render.
FEATURE_DATA = {
    "voice_control": {
        "name": "Voice Control",
        "description": "Hold the ` key and tell Chrome what to do.",
        "options": [
            {"key": "always_on", "label": "Always on",
             "info": "Keeps listening without holding `. Uses more CPU and "
                     "battery the whole time it's on."},
            {"key": "dictation_mode", "label": "Dictation mode",
             "info": "Types your speech into whatever's focused, instead of "
                     "treating it as a command for Chrome."},
        ],
        "shortcuts": [
            {"key": None, "label": "Hotkey", "editable": False, "static": "`"},
        ],
        "instructions": [
            ["Hold ` to talk",
             "Press and hold the key left of 1, say your command, let go."],
            ["Watch the dot",
             "It grows into a bar while listening, shrinks back once done."],
            ["Be specific",
             "Name what you see: “click the blue Sign in button,” "
             "not “click sign in.”"],
        ],
    },
    "page_reader": {
        "name": "Page Reader",
        "description": "Reads what's on your screen out loud.",
        "options": [
            {"key": "voice_guided", "label": "Voice sections",
             "info": "With Voice Control also on, say things like “read "
                     "the billing information” to read just that part."},
            {"key": "hover_to_read", "label": "Hover to read",
             "info": "Reads whatever line your cursor is resting on, without "
                     "pressing a key."},
            {"key": "use_groq_summary", "label": "Skip clutter",
             "info": "Reads a short summary of what matters instead of every "
                     "line on the page."},
        ],
        "shortcuts": [
            {"key": "read_screen", "label": "Read", "editable": True},
            {"key": "stop", "label": "Stop", "editable": True},
        ],
        "instructions": [
            ["Press F9 to read",
             "Reads the screen out loud. F10 stops it."],
            ["Watch the bar",
             "Shows the current line and how far through it is. Tap the "
             "button to pause or resume."],
            ["Ask for one section",
             "With Voice Control on, say “read the billing information”."],
        ],
    },
    "tone_reader": {
        "name": "Tone & Social Cues",
        "description": "Explains the tone behind a message you highlight.",
        "options": [
            {"key": "click_to_analyze", "label": "Shift+Click to analyze",
             "info": "Shift+Click any paragraph on a page to analyze it, "
                     "instead of only working on selected text."},
            {"key": "read_aloud", "label": "Read the answer aloud",
             "info": "Speaks the tone and explanation as soon as the card "
                     "appears, using the same neural voice as Page Reader."},
        ],
        "shortcuts": [
            {"key": "analyze_selection", "label": "Analyze", "editable": True},
        ],
        "instructions": [
            ["Highlight, then press your key",
             "Select text anywhere, press your shortcut."],
            ["Read the card",
             "Tone plus what the person likely means, in two sentences."],
            ["Dismiss it",
             "Press Got it, Escape, or click elsewhere."],
        ],
    },
    "colorblind_filter": {
        "name": "Color Blind Filter",
        "description": "Makes colors on your screen easier to tell apart.",
        "options": [],
        "shortcuts": [],
        "settings_panel": "colorblind",
        "instructions": [
            ["Pick your type", "Deuteranopia, Protanopia, or Tritanopia."],
            ["Turn it on", "Use the switch in the sidebar — applies right away."],
            ["Turn it off", "Same switch. Colors go back to normal instantly."],
        ],
    },
    "focus_mode": {
        "name": "Focus Mode",
        "description": "Blocks distracting sites for a set amount of time.",
        "options": [],
        "shortcuts": [],
        "settings_panel": "focus",
        "instructions": [
            ["List your sites", "One domain per line, like youtube.com."],
            ["Set a length", "Use +/- to pick minutes."],
            ["Turn it on",
             "Use the switch in the sidebar. Blocked everywhere until time's "
             "up or you switch it off."],
        ],
    },
    "cursor_size": {
        "name": "Cursor Size",
        "description": "Makes your mouse pointer bigger or smaller, everywhere.",
        "options": [],
        "shortcuts": [],
        "settings_panel": "cursor",
        "instructions": [
            ["Turn it on", "Use the switch in the sidebar."],
            ["Pick a size", "Use +/- below — 1 is normal, 15 is largest. The "
             "arrow resizes fully; the text and loading pointers may not."],
            ["Turn it off", "Puts the pointer back to its normal size."],
        ],
    },
}

FILTER_TYPES = {
    "Deuteranopia": 3, "Protanopia": 4, "Tritanopia": 5,
    "Grayscale": 0, "Invert": 1, "Grayscale Inverted": 2,
}


def _load_feature_module(feature_id: str):
    """Import a feature's main.py for its pure logic functions (no Tk app built).

    colorblind_filter and focus_mode expose plain functions (set_filter,
    apply_block/remove_block, is_admin) with no Tk dependency, so the hub
    calls them directly for single-shot actions instead of round-tripping
    through settings.json polling. Loaded under a unique name (not "main")
    so the two don't collide in sys.modules.
    """
    import importlib.util

    path = FEATURES_DIR / feature_id / "main.py"
    spec = importlib.util.spec_from_file_location(f"a4a_feature_{feature_id}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Feature:
    """One feature folder, described by its feature.json manifest."""

    def __init__(self, dir_path: Path, manifest: dict):
        self.dir = dir_path
        self.id = dir_path.name
        data = FEATURE_DATA.get(self.id, {})
        self.name = data.get("name") or manifest.get("name") or dir_path.name
        self.description = data.get("description") or manifest.get("description", "")
        self.entry = manifest.get("entry", "main.py")
        requires = manifest.get("requires_env") or []
        self.requires_env = [str(k) for k in requires if isinstance(k, str)]

    @property
    def entry_path(self) -> Path:
        return self.dir / self.entry

    def missing_env(self) -> list[str]:
        """Required keys the feature would crash on, checked before launching."""
        return [k for k in self.requires_env if not os.getenv(k)]


def discover_features() -> list[Feature]:
    """Find every runnable feature folder under ./features/.

    Folders whose names start with '_' or '.' are ignored (e.g. _template).
    A folder needs an existing entry file to be listed; a missing or invalid
    feature.json falls back to sensible defaults (folder name + main.py).
    Known features are listed in the designed order; anything new is appended.
    """
    features: list[Feature] = []
    if not FEATURES_DIR.exists():
        return features

    for child in sorted(FEATURES_DIR.iterdir()):
        if not child.is_dir() or child.name.startswith(("_", ".")):
            continue
        manifest: dict = {}
        manifest_path = child / "feature.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text())
            except Exception as e:
                safe_print(f"[hub] bad feature.json in '{child.name}': {e} — using defaults",
                           flush=True)
        feat = Feature(child, manifest)
        if not feat.entry_path.exists():
            safe_print(f"[hub] skipping '{child.name}': entry '{feat.entry}' not found",
                       flush=True)
            continue
        features.append(feat)

    def sort_key(f: Feature):
        return (FEATURE_ORDER.index(f.id) if f.id in FEATURE_ORDER
                else len(FEATURE_ORDER), f.name.lower())

    return sorted(features, key=sort_key)


def load_state() -> dict:
    state = {"enabled": [], "text_scale": 1.0, "launch_at_startup": False}
    if STATE_FILE.exists():
        try:
            loaded = json.loads(STATE_FILE.read_text())
            if isinstance(loaded, dict):
                state.update(loaded)
        except Exception:
            pass
    return state


def save_state(enabled: set[str], text_scale: float, launch_at_startup: bool = False):
    try:
        STATE_FILE.write_text(json.dumps({
            "enabled": sorted(enabled),
            "text_scale": round(text_scale, 2),
            "launch_at_startup": bool(launch_at_startup),
        }, indent=2))
    except Exception as e:
        safe_print(f"[hub] could not save state: {e}", flush=True)


class Api:
    """The one bridge between this process's Python and the webview's JS."""

    POLL_MS = 1000

    def __init__(self):
        state = load_state()
        self._procs: dict[str, subprocess.Popen] = {}
        self._stderr: dict[str, collections.deque] = {}
        self._enabled: set[str] = set(state.get("enabled", []))
        self.text_scale = float(state.get("text_scale", 1.0))
        # The OS is the source of truth here, not our own state file — the
        # user may have removed the login entry outside the app.
        try:
            self.launch_at_startup = autostart.is_enabled()
        except Exception:
            self.launch_at_startup = bool(state.get("launch_at_startup", False))
        self._notice = ""
        self._features: list[Feature] = discover_features()
        self._lock = threading.Lock()

    # ── feature list + static content ──
    def list_features(self) -> list[dict]:
        out = []
        for feat in self._features:
            data = FEATURE_DATA.get(feat.id, {})
            out.append({
                "id": feat.id,
                "name": feat.name,
                "description": feat.description,
                "options": data.get("options", []),
                "shortcuts": data.get("shortcuts", []),
                "instructions": data.get("instructions", []),
                "settingsPanel": data.get("settings_panel"),
            })
        return out

    def get_panel_data(self, feature_id: str) -> dict:
        """Extra data a bespoke settings panel needs beyond options/shortcuts."""
        if feature_id == "colorblind_filter":
            return {"filterTypes": list(FILTER_TYPES)}
        if feature_id == "focus_mode":
            try:
                fm = _load_feature_module("focus_mode")
                return {"isAdmin": fm.is_admin()}
            except Exception as e:
                return {"isAdmin": False, "error": str(e)}
        if feature_id == "cursor_size":
            return {"isWindows": plat.IS_WINDOWS}
        return {}

    # ── settings (shared/settings_store.py — the running feature watches this) ──
    def get_settings(self, feature_id: str) -> dict:
        return store.load(feature_id)

    def save_setting(self, feature_id: str, key: str, value) -> dict:
        settings = store.load(feature_id)
        settings[key] = value
        store.save(feature_id, settings)
        if feature_id == "colorblind_filter" and key in ("enabled", "filter_name"):
            # filter_name matters as much as enabled: a running filter does
            # not re-read its type, so without this, picking a different
            # filter changed nothing on screen.
            if key == "enabled" or self._is_running(feature_id):
                self._apply_colorblind_filter(settings)
        elif feature_id == "cursor_size" and key == "size" and self._is_running(feature_id):
            self._apply_cursor_size(int(value))
        return settings

    def _is_running(self, feature_id: str) -> bool:
        proc = self._procs.get(feature_id)
        return proc is not None and proc.poll() is None

    def save_hotkey(self, feature_id: str, key: str, combo: str) -> dict:
        settings = store.load(feature_id)
        hotkeys = dict(settings.get("hotkeys", {}))
        clash = [k for k, v in hotkeys.items()
                 if k != key and v.lower() == combo.lower()]
        if clash:
            return {"ok": False, "error": "That key is already used by another action."}
        hotkeys[key] = combo
        settings["hotkeys"] = hotkeys
        store.save(feature_id, settings)
        return {"ok": True, "hotkeys": hotkeys}

    def capture_hotkey(self) -> dict:
        """Blocks (on the JS call's own thread) until a key combo is pressed."""
        combo = hk.capture_hotkey(timeout=8.0)
        if not combo:  # None = timed out, "" = key couldn't be cleanly identified
            return {"ok": False, "error": "Could not read that key — try a different one."}
        return {"ok": True, "combo": combo, "label": hk.pretty(combo)}

    def pretty_hotkey(self, spec: str) -> str:
        return hk.pretty(spec)

    # ── start at login ──
    def get_launch_at_startup(self) -> dict:
        try:
            self.launch_at_startup = autostart.is_enabled()
        except Exception as e:
            safe_print(f"[hub] could not read start-at-login state: {e}", flush=True)
        return {"enabled": self.launch_at_startup,
                "supported": autostart.is_supported()}

    def get_preview_media(self, feature_id: str) -> dict:
        """The screenshots / screen recordings that show this feature working.

        Returned as file:// URLs the page loads directly — the hub is itself
        served from a file:// URL, so no local server is needed. Images from a
        folder holding several are meant to be cycled through; a single image
        is just shown. Videos are handed over as-is: the recordings are H.264
        in a QuickTime container, which WebView2 plays natively.
        """
        folder = PREVIEW_FOLDERS.get(feature_id)
        if not folder:
            return {"images": [], "videos": []}
        base = PREVIEW_DIR / folder
        if not base.is_dir():
            return {"images": [], "videos": []}
        images, videos = [], []
        # rglob: some folders nest the media one level deeper.
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.name.startswith("."):
                continue
            suffix = path.suffix.lower()
            if suffix in IMAGE_SUFFIXES:
                images.append(path)
            elif suffix in VIDEO_SUFFIXES:
                videos.append(path)
        return {
            "images": [_page_url(p) for p in images],
            "videos": [_page_url(p) for p in videos],
            "labels": [_media_label(p) for p in images],
        }

    def set_launch_at_startup(self, enabled: bool) -> dict:
        try:
            autostart.set_enabled(bool(enabled))
            self.launch_at_startup = bool(enabled)
            save_state(self._enabled, self.text_scale, self.launch_at_startup)
            return {"ok": True, "enabled": self.launch_at_startup}
        except Exception as e:
            safe_print(f"[hub] start-at-login change failed: {e}", flush=True)
            self._notice = f"Start at login: {e}"
            return {"ok": False, "enabled": autostart.is_enabled(), "error": str(e)}

    def _apply_colorblind_filter(self, settings: dict):
        requested_enabled = bool(settings.get("enabled", False))
        requested_filter = settings.get("filter_name", "Deuteranopia")
        console.log_event("hub", "apply_start", feature="colorblind_filter",
                          requested_enabled=requested_enabled,
                          requested_filter=requested_filter)
        try:
            cf = _load_feature_module("colorblind_filter")
            cf.set_filter(requested_enabled, FILTER_TYPES[requested_filter])
            verified = cf.is_filter_active()
            console.log_event("hub", "apply_result", feature="colorblind_filter",
                              ok=True, verified_active=verified)
        except Exception as e:
            safe_print(f"[hub] colorblind filter apply failed: {e}", flush=True)
            console.log_event("hub", "apply_result", feature="colorblind_filter",
                              ok=False, error=str(e))

    # ── toggle / process control ──
    def get_state(self) -> dict:
        enabled = {feat.id: (feat.id in self._procs and self._procs[feat.id].poll() is None)
                   for feat in self._features}
        return {"enabled": enabled, "notice": self._notice}

    def toggle_feature(self, feature_id: str) -> dict:
        feat = next((f for f in self._features if f.id == feature_id), None)
        if feat is None:
            return self.get_state()
        with self._lock:
            running = feat.id in self._procs and self._procs[feat.id].poll() is None
            turning_on = not running
            console.log_event("hub", "toggle", feature=feat.id, turning_on=turning_on)
            self._notice = ""
            # Settle settings.json + the actual filter/block BEFORE the
            # subprocess (re)starts, so its own startup read sees the final
            # state instead of racing it — and so its own redundant apply on
            # launch (see focus_mode's _begin_session) can see the block is
            # already there and skip re-triggering an elevation prompt. This
            # runs after clearing _notice above but before _start/_stop, so
            # an error it sets (e.g. "add a site to block first") survives
            # instead of being wiped by the old turning-on reset below.
            self._apply_single_toggle(feat.id, turning_on)
            if running:
                self._stop(feat)
                self._enabled.discard(feat.id)
            else:
                self._start(feat)
                self._enabled.add(feat.id)
            save_state(self._enabled, self.text_scale, self.launch_at_startup)
        return self.get_state()

    def _apply_single_toggle(self, feature_id: str, turning_on: bool):
        """colorblind_filter and focus_mode collapse "is the bubble process
        running" and "is the effect actually active" into one user-visible
        switch — no separate in-page toggle for either. Called directly
        rather than only through settings.json + the bubble's own watcher,
        since on Windows `Popen.terminate()` maps straight to
        `TerminateProcess` with no signal delivery, so a subprocess killed by
        the hub can't reliably run its own shutdown cleanup — the block/
        filter must be applied or removed from here to be guaranteed.
        """
        if feature_id == "colorblind_filter":
            settings = store.load(feature_id)
            settings["enabled"] = turning_on
            store.save(feature_id, settings)
            self._apply_colorblind_filter(settings)
        elif feature_id == "focus_mode":
            self._apply_focus_toggle(turning_on)
        elif feature_id == "cursor_size":
            settings = store.load(feature_id)
            self._apply_cursor_size(int(settings.get("size", 1)) if turning_on else 1)

    def _apply_cursor_size(self, size: int):
        """Resize the pointer now, not just in the registry.

        Applied from here rather than the subprocess for the same reason as
        focus_mode/colorblind_filter: Popen.terminate() on Windows is
        TerminateProcess with no signal delivery, so a subprocess cannot be
        relied on to put the pointer back when the hub stops it.

        See shared/cursors.py for why the registry values alone did nothing
        visible — the pointer only changes when the cursor images are
        reloaded at the new size.
        """
        if not plat.IS_WINDOWS:
            return
        size = cursors.clamp(size)
        console.log_event("hub", "apply_start", feature="cursor_size", requested_size=size)
        try:
            changed = cursors.apply_size(size)
            console.log_event("hub", "apply_result", feature="cursor_size",
                              ok=changed > 0, requested_size=size,
                              requested_base_px=cursors.pixels_for(size),
                              cursors_changed=changed,
                              verified_cursor_size=cursors.read_size())
            if changed == 0:
                self._notice = "Cursor Size: Windows did not accept the new pointer size."
        except Exception as e:
            safe_print(f"[hub] cursor size apply failed: {e}", flush=True)
            self._notice = f"Cursor Size: {e}"
            console.log_event("hub", "apply_result", feature="cursor_size", ok=False,
                              requested_size=size, error=str(e))

    def _apply_focus_toggle(self, turning_on: bool):
        fm = None
        domains = []
        try:
            fm = _load_feature_module("focus_mode")
            settings = store.load("focus_mode")
            if turning_on:
                domains = settings.get("blocklist", [])
                console.log_event("hub", "apply_start", feature="focus_mode",
                                  turning_on=True, domains=domains)
                if not domains:
                    self._notice = "Focus Mode: add a site to block first."
                    console.log_event("hub", "apply_result", feature="focus_mode",
                                      ok=False, error="no domains in blocklist")
                    return
                was_admin = fm.is_admin()
                fm.apply_block(domains)
                settings["active"] = True
                settings["end_time"] = time.time() + int(
                    settings.get("duration_minutes", 25)) * 60
            else:
                console.log_event("hub", "apply_start", feature="focus_mode",
                                  turning_on=False, domains=domains)
                was_admin = fm.is_admin()
                fm.remove_block()
                settings["active"] = False
                settings["end_time"] = 0.0
            store.save("focus_mode", settings)
            hosts_text = fm.HOSTS_PATH.read_text(encoding="utf-8", errors="ignore") \
                if fm.HOSTS_PATH.exists() else ""
            marker_present = fm.MARK_BEGIN in hosts_text
            console.log_event("hub", "apply_result", feature="focus_mode",
                              ok=(marker_present == turning_on),
                              turning_on=turning_on, domains=domains,
                              was_admin=was_admin, needed_elevation=not was_admin,
                              marker_present_in_hosts=marker_present)
            # The hosts file is the source of truth. If it disagrees with what
            # was asked for, the toggle failed even though nothing raised —
            # say so instead of leaving a switch that looks on and does nothing.
            if marker_present != turning_on:
                self._notice = ("Focus Mode: the block did not take effect. "
                                "Try again and accept the permission prompt.")
            elif turning_on:
                self._notice = ("Focus Mode: blocking now. Tabs already open to a "
                                "blocked site need a refresh — browsers cache their "
                                "own lookups.")
        except Exception as e:
            safe_print(f"[hub] focus mode toggle failed: {e}", flush=True)
            self._notice = f"Focus Mode: {e}"
            console.log_event("hub", "apply_result", feature="focus_mode", ok=False,
                              turning_on=turning_on, domains=domains, error=str(e))

    def _start(self, feat: Feature):
        if feat.id in self._procs and self._procs[feat.id].poll() is None:
            return

        missing = feat.missing_env()
        if missing:
            keys = ", ".join(missing)
            safe_print(f"[hub] '{feat.name}' needs {keys} in .env — not starting",
                       flush=True)
            self._notice = (f"{feat.name} needs {keys}. Add it to the .env file "
                            f"next to hub.py, then toggle again.")
            return

        safe_print(f"[hub] starting '{feat.name}' -> {feat.entry_path}", flush=True)
        try:
            proc = subprocess.Popen([sys.executable, str(feat.entry_path)],
                                    cwd=str(feat.dir),
                                    stderr=subprocess.PIPE,
                                    encoding="utf-8", errors="replace")
            self._procs[feat.id] = proc
            self._stderr[feat.id] = collections.deque(maxlen=STDERR_KEEP_LINES)
            threading.Thread(target=self._drain_stderr, args=(feat, proc),
                             daemon=True).start()
        except Exception as e:
            safe_print(f"[hub] failed to start '{feat.name}': {e}", flush=True)
            self._notice = f"{feat.name} could not start."

    def _drain_stderr(self, feat: Feature, proc: subprocess.Popen):
        """Echo the child's stderr to our terminal and keep the tail of it.

        Capturing the pipe is what lets `poll` report *why* a feature died
        instead of a bare exit code; echoing keeps the terminal as useful as
        it was when stderr was inherited.
        """
        buf = self._stderr.get(feat.id)
        try:
            for line in proc.stderr:
                line = line.rstrip()
                safe_print(f"[{feat.id}] {line}", flush=True)
                if buf is not None and line.strip():
                    buf.append(line.strip())
        except Exception:
            pass

    def _stop(self, feat: Feature):
        proc = self._procs.get(feat.id)
        if proc and proc.poll() is None:
            safe_print(f"[hub] stopping '{feat.name}'", flush=True)
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                safe_print(f"[hub] '{feat.name}' didn't exit, killing", flush=True)
                proc.kill()
        self._procs.pop(feat.id, None)
        self._stderr.pop(feat.id, None)

    def restore_enabled(self):
        known = {f.id for f in self._features}
        for feat in self._features:
            if feat.id in self._enabled and feat.id in known:
                # Re-apply the actual effect too, not just relaunch the
                # bubble — colorblind_filter's Windows-level state doesn't
                # survive a hub restart on its own now that applying it lives
                # here rather than in the subprocess.
                self._apply_single_toggle(feat.id, True)
                self._start(feat)
        self._enabled &= known
        save_state(self._enabled, self.text_scale, self.launch_at_startup)

    def poll(self) -> dict:
        """Called by JS on a timer — detects a crashed subprocess and reflects it."""
        for feat in self._features:
            proc = self._procs.get(feat.id)
            if proc is not None and proc.poll() is not None:
                code = proc.returncode
                self._procs.pop(feat.id, None)
                self._enabled.discard(feat.id)
                reason = self._crash_reason(feat.id, code)
                self._stderr.pop(feat.id, None)
                if code != 0:
                    self._notice = f"{feat.name} stopped: {reason}"
                    safe_print(f"[hub] '{feat.name}' exited with code {code} "
                               f"({reason})", flush=True)
                save_state(self._enabled, self.text_scale, self.launch_at_startup)
        return self.get_state()

    def _crash_reason(self, feature_id: str, code: int) -> str:
        """Turn a dead child into one line a user can act on.

        Prefers the exception line at the end of a traceback; falls back to the
        last thing it said, then to the signal or exit code.
        """
        lines = list(self._stderr.get(feature_id) or [])
        for line in reversed(lines):
            m = re.match(r"^(?:\w+\.)*(\w*(?:Error|Exception))\b\s*:?\s*(.*)$", line)
            if m:
                detail = m.group(2).strip()
                return detail or m.group(1)
        if lines:
            return lines[-1]
        if code < 0:
            return f"killed by signal {-code}"
        return f"exit code {code}"

    # ── text size ──
    def set_text_scale(self, scale: float) -> float:
        self.text_scale = round(min(1.6, max(0.85, scale)), 2)
        save_state(self._enabled, self.text_scale, self.launch_at_startup)
        return self.text_scale

    def get_text_scale(self) -> float:
        return self.text_scale

    # ── shutdown ──
    def shutdown(self):
        # Tear down the actual OS-level effects before killing the
        # subprocesses, not just after — closing the app used `_stop` alone,
        # which is `Popen.terminate()` (raw `TerminateProcess` on Windows,
        # see `_apply_single_toggle`'s docstring), so a feature's own
        # shutdown cleanup never ran. Focus Mode's hosts-file block in
        # particular stayed in place, silently, until the hub was next
        # launched — closing the app should unban sites, not just stop
        # showing the countdown.
        for feature_id in list(self._enabled):
            self._apply_single_toggle(feature_id, False)
        for feat in self._features:
            self._stop(feat)


def _bring_to_front(window):
    """Open in front of whatever the user was doing.

    Launching the hub and having its window appear silently behind the
    browser reads as "it didn't start" — which is exactly how it was
    reported. pywebview has no cross-platform raise, so nudge it: a brief
    always-on-top flip is enough for the window manager to lift it, and it
    must not STAY on top or it would sit over the user's work.
    """
    try:
        window.on_top = True
        window.on_top = False
    except Exception:
        pass



def _page_url(path: Path) -> str:
    """A URL for `index.html` to load, relative to hub_ui/.

    Relative rather than absolute file://, because that is what stays inside
    the directory WebView2 will serve from — and each segment is percent-
    encoded, since every one of these folders has a space in its name.
    """
    rel = path.resolve().relative_to(UI_DIR.resolve())
    return "/".join(quote(part) for part in rel.parts)


def _media_label(path: Path) -> str:
    """A readable caption from the file name: "image_grayscale_inverted" →
    "Grayscale inverted".

    Camera and screenshot names carry nothing worth showing ("IMG_6454",
    "image", "min"), so those get no caption at all rather than a meaningless
    one under the picture.
    """
    stem = path.stem
    for prefix in ("image_", "image-"):
        if stem.lower().startswith(prefix):
            stem = stem[len(prefix):]
            break
    else:
        # A bare camera/screenshot name describes nothing.
        if (stem.lower() in {"image", "min", "max", "screenshot"}
                or re.fullmatch(r"(?:IMG|DSC|PXL|Screenshot)[ _-]?[0-9 _-]+", stem, re.I)):
            return ""
    stem = stem.replace("_", " ").replace("-", " ").strip()
    return stem[:1].upper() + stem[1:] if stem else ""


def main():
    safe_print(f"[hub] Accessibility4all hub starting | features dir: {FEATURES_DIR}",
               flush=True)
    api = Api()
    window = webview.create_window(
        "Accessibility4all",
        str(UI_DIR / "index.html"),
        js_api=api,
        width=1180, height=780, min_size=(920, 580),
        background_color="#14161b",
    )
    window.events.closed += api.shutdown

    def _restore():
        _bring_to_front(window)
        api.restore_enabled()

    webview.start(_restore, debug=False)
    safe_print("[hub] hub closed", flush=True)


if __name__ == "__main__":
    main()
