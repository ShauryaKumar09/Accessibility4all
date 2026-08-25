"""Accessibility4all — feature hub.

A pywebview app shell (`hub_ui/index.html` + `app.js` + `style.css`) backed
by this file. Each feature lives in its own folder under ./features/ and
runs as its own OS process:

    toggle ON   -> launch  features/<name>/<entry>  as a subprocess
    toggle OFF  -> terminate that subprocess

Features are discovered automatically, so separate developers can drop a new
folder into ./features/ and it appears here with no changes to this file. See
features/README.md for the contract every feature must follow.

The hub window is the only part of this app that renders as HTML/CSS/JS —
every feature's own small always-on-top "bubble" is still drawn with
tkinter + shared/ui_kit.py in its own process, unchanged. `Api` below is the
one bridge between this process's Python and the webview's JS: it owns
feature discovery, subprocess start/stop/poll, and reading/writing each
feature's settings.json (via shared/settings_store.py, which the running
feature processes already watch for changes).
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import webview

from shared.console import configure_stdio, safe_print
from shared import hotkeys as hk
from shared import settings_store as store
from shared import windows_fonts as winfonts

configure_stdio()

ROOT = Path(__file__).parent.resolve()
FEATURES_DIR = ROOT / "features"
UI_DIR = ROOT / "hub_ui"
STATE_FILE = ROOT / "hub_state.json"     # toggles + text scale

FEATURE_ORDER = ["voice_control", "page_reader", "tone_reader", "dyslexia_font",
                 "colorblind_filter", "focus_mode"]

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
    "dyslexia_font": {
        "name": "Dyslexia",
        "description": "Easier fonts for websites, plus a quick reading screen.",
        "options": [
            {"key": "website_enabled", "label": "Website font",
             "info": "Applies your chosen font to pages you visit, via the "
                     "bundled Chrome extension."},
        ],
        "shortcuts": [],
        "settings_panel": "dyslexia",
        "instructions": [
            ["Pick a font", "The preview updates live so you can compare."],
            ["Adjust spacing", "Use +/- to space out letters and lines."],
            ["Browse normally",
             "Install the Chrome extension to carry your font to every page."],
            ["Take the screening test",
             "A short, non-diagnostic check further down this page."],
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
}

FILTER_TYPES = {
    "Deuteranopia": 3, "Protanopia": 4, "Tritanopia": 5,
    "Grayscale": 0, "Invert": 1, "Grayscale Inverted": 2,
}

# NOT a diagnostic tool. It cannot identify dyslexia. It only notices a few
# patterns sometimes associated with reading differences.
SCREENING_DISCLAIMER = (
    "Not a diagnosis — it can't identify dyslexia, only a few related patterns. "
    "If reading is a concern, talk to a specialist such as an educational psychologist."
)
SCREENING_QUESTIONS = [
    {"prompt": "Which one matches the target letter? Target: b",
     "options": ["d", "b", "p", "q"], "correct": 1},
    {"prompt": "Which one matches the target letter? Target: p",
     "options": ["q", "d", "p", "b"], "correct": 2},
    {"prompt": "Which word is spelled the same forwards and as shown? Target: was",
     "options": ["saw", "was", "sae", "aws"], "correct": 1},
    {"prompt": "Which word does NOT rhyme with the others?",
     "options": ["cat", "hat", "dog", "bat"], "correct": 2},
    {"prompt": "Which word does NOT rhyme with the others?",
     "options": ["light", "night", "sight", "bench"], "correct": 3},
    {"prompt": "Put in order: which comes first alphabetically?",
     "options": ["dog", "cat", "ant", "elk"], "correct": 2},
]
SCREENING_PASSAGE = (
    "The quick brown fox jumps over the lazy dog. Reading every day helps "
    "build stronger word recognition and comprehension over time."
)
SCREENING_TYPICAL_WPM = 200  # rough, non-clinical adult reference


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

    @property
    def entry_path(self) -> Path:
        return self.dir / self.entry


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
    state = {"enabled": [], "text_scale": 1.0}
    if STATE_FILE.exists():
        try:
            loaded = json.loads(STATE_FILE.read_text())
            if isinstance(loaded, dict):
                state.update(loaded)
        except Exception:
            pass
    return state


def save_state(enabled: set[str], text_scale: float):
    try:
        STATE_FILE.write_text(json.dumps({
            "enabled": sorted(enabled),
            "text_scale": round(text_scale, 2),
        }, indent=2))
    except Exception as e:
        safe_print(f"[hub] could not save state: {e}", flush=True)


class Api:
    """The one bridge between this process's Python and the webview's JS."""

    POLL_MS = 1000

    def __init__(self):
        state = load_state()
        self._procs: dict[str, subprocess.Popen] = {}
        self._enabled: set[str] = set(state.get("enabled", []))
        self.text_scale = float(state.get("text_scale", 1.0))
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
        if feature_id == "dyslexia_font":
            return {
                "fontChoices": [{"name": n, "note": winfonts.FONT_NOTES.get(n, "")}
                                for n in winfonts.FONT_CHOICES],
            }
        if feature_id == "colorblind_filter":
            return {"filterTypes": list(FILTER_TYPES)}
        if feature_id == "focus_mode":
            try:
                fm = _load_feature_module("focus_mode")
                return {"isAdmin": fm.is_admin()}
            except Exception as e:
                return {"isAdmin": False, "error": str(e)}
        return {}

    def get_screening(self) -> dict:
        return {
            "disclaimer": SCREENING_DISCLAIMER,
            "questions": SCREENING_QUESTIONS,
            "passage": SCREENING_PASSAGE,
            "typicalWpm": SCREENING_TYPICAL_WPM,
        }

    # ── settings (shared/settings_store.py — the running feature watches this) ──
    def get_settings(self, feature_id: str) -> dict:
        return store.load(feature_id)

    def save_setting(self, feature_id: str, key: str, value) -> dict:
        settings = store.load(feature_id)
        settings[key] = value
        store.save(feature_id, settings)
        if feature_id == "colorblind_filter" and key == "enabled":
            self._apply_colorblind_filter(settings)
        elif feature_id == "dyslexia_font" and key == "font_family" and self._is_running(feature_id):
            # Feature's already on — reapply immediately with the new font
            # instead of waiting for the next toggle.
            self._apply_dyslexia_toggle(True)
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

    def _apply_colorblind_filter(self, settings: dict):
        try:
            cf = _load_feature_module("colorblind_filter")
            cf.set_filter(bool(settings.get("enabled", False)),
                          FILTER_TYPES[settings.get("filter_name", "Deuteranopia")])
        except Exception as e:
            safe_print(f"[hub] colorblind filter apply failed: {e}", flush=True)

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
            save_state(self._enabled, self.text_scale)
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
        elif feature_id == "dyslexia_font":
            self._apply_dyslexia_toggle(turning_on)

    def _apply_dyslexia_toggle(self, turning_on: bool):
        """Windows app-font substitution now follows the sidebar switch
        automatically — no separate "Use this font in Windows apps" button.
        (`website_enabled` stays its own toggle: it controls the Chrome
        extension, a genuinely different mechanism the user may want
        independent of whether Windows apps are substituted.)"""
        try:
            if turning_on:
                settings = store.load("dyslexia_font")
                font_name = settings.get("font_family") or winfonts.FONT_CHOICES[0]
                winfonts.apply_windows_substitution(font_name, list(winfonts.SUBSTITUTION_TARGETS))
            else:
                if winfonts.BACKUP_FILE.exists():
                    winfonts.restore_windows_substitution()
        except Exception as e:
            safe_print(f"[hub] dyslexia font toggle failed: {e}", flush=True)
            self._notice = f"Dyslexia Font: {e}"

    def _apply_focus_toggle(self, turning_on: bool):
        try:
            fm = _load_feature_module("focus_mode")
            settings = store.load("focus_mode")
            if turning_on:
                domains = settings.get("blocklist", [])
                if not domains:
                    self._notice = "Focus Mode: add a site to block first."
                    return
                fm.apply_block(domains)
                settings["active"] = True
                settings["end_time"] = time.time() + int(
                    settings.get("duration_minutes", 25)) * 60
            else:
                fm.remove_block()
                settings["active"] = False
                settings["end_time"] = 0.0
            store.save("focus_mode", settings)
        except Exception as e:
            safe_print(f"[hub] focus mode toggle failed: {e}", flush=True)
            self._notice = f"Focus Mode: {e}"

    def _start(self, feat: Feature):
        if feat.id in self._procs and self._procs[feat.id].poll() is None:
            return
        safe_print(f"[hub] starting '{feat.name}' -> {feat.entry_path}", flush=True)
        try:
            proc = subprocess.Popen([sys.executable, str(feat.entry_path)],
                                    cwd=str(feat.dir))
            self._procs[feat.id] = proc
        except Exception as e:
            safe_print(f"[hub] failed to start '{feat.name}': {e}", flush=True)
            self._notice = f"{feat.name} could not start."

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

    def restore_enabled(self):
        known = {f.id for f in self._features}
        for feat in self._features:
            if feat.id in self._enabled and feat.id in known:
                # Re-apply the actual effect too, not just relaunch the
                # bubble — colorblind_filter/dyslexia_font's Windows-level
                # state doesn't survive a hub restart on its own now that
                # applying it lives here rather than in the subprocess.
                self._apply_single_toggle(feat.id, True)
                self._start(feat)
        self._enabled &= known
        save_state(self._enabled, self.text_scale)
        self._cleanup_stale_dyslexia_font()

    def _cleanup_stale_dyslexia_font(self):
        """If a previous session applied Windows font substitution and never
        restored it (crash, force-kill) and dyslexia_font isn't enabled this
        session, restore now — mirrors focus_mode's own stale-block cleanup,
        done here since applying/restoring the font now lives in the hub,
        not the feature's own subprocess."""
        if "dyslexia_font" in self._enabled:
            return
        try:
            if winfonts.BACKUP_FILE.exists():
                winfonts.restore_windows_substitution()
                safe_print("[hub] restored Windows font substitution left "
                          "over from a previous session", flush=True)
        except Exception as e:
            safe_print(f"[hub] stale dyslexia-font cleanup failed: {e}", flush=True)

    def poll(self) -> dict:
        """Called by JS on a timer — detects a crashed subprocess and reflects it."""
        for feat in self._features:
            proc = self._procs.get(feat.id)
            if proc is not None and proc.poll() is not None:
                code = proc.returncode
                self._procs.pop(feat.id, None)
                self._enabled.discard(feat.id)
                if code != 0:
                    self._notice = f"{feat.name} stopped unexpectedly."
                    safe_print(f"[hub] '{feat.name}' exited with code {code}", flush=True)
                save_state(self._enabled, self.text_scale)
        return self.get_state()

    # ── text size ──
    def set_text_scale(self, scale: float) -> float:
        self.text_scale = round(min(1.6, max(0.85, scale)), 2)
        save_state(self._enabled, self.text_scale)
        return self.text_scale

    def get_text_scale(self) -> float:
        return self.text_scale

    # ── shutdown ──
    def shutdown(self):
        for feat in self._features:
            self._stop(feat)


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
        api.restore_enabled()

    webview.start(_restore, debug=False)
    safe_print("[hub] hub closed", flush=True)


if __name__ == "__main__":
    main()
