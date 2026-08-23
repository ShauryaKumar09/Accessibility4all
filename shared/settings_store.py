"""Per-feature settings, shared between the hub and the feature processes.

The redesign moves every control out of the feature windows and into the hub's
settings sheets, so two processes now touch the same `features/<id>/settings.json`:
the hub writes it, the running feature re-reads it. Features poll `Watcher` for
mtime changes instead of holding the file open, which keeps this as loose as the
rest of the file-based bus.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FEATURES_DIR = ROOT / "features"

DEFAULTS: dict[str, dict] = {
    "voice_control": {
        "always_on": False,
    },
    "page_reader": {
        "voice_guided": True,
        "hover_to_read": False,
        "use_groq_summary": True,
        "hotkeys": {"read_screen": "F9", "stop": "F10"},
        "tts_rate": 145,
        "tts_volume": 1.0,
        "hover_delay_ms": 200,
    },
    "tone_reader": {
        "click_to_analyze": False,
        "hotkeys": {"analyze_selection": "ctrl+shift+y"},
        # Replaces the old free-form spinbox: three named sizes.
        "text_size": "medium",          # small 18 / medium 22 / large 28
        "show_raw_on_parse_error": True,
    },
    "dyslexia_font": {
        "website_enabled": True,
        "font_family": "OpenDyslexic",
        "letter_spacing": 0.03,
        "line_height": 1.55,
        "font_weight": "inherit",
        "windows_targets": ["Arial", "Calibri", "Segoe UI", "Tahoma",
                            "Times New Roman", "Verdana"],
    },
}

TONE_TEXT_SIZES = {"small": 18, "medium": 22, "large": 28}


def settings_path(feature_id: str) -> Path:
    return FEATURES_DIR / feature_id / "settings.json"


def load(feature_id: str) -> dict:
    """Defaults merged with whatever is on disk (nested hotkeys merged too)."""
    settings = json.loads(json.dumps(DEFAULTS.get(feature_id, {})))
    path = settings_path(feature_id)
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                hotkeys = {**settings.get("hotkeys", {}),
                           **(loaded.get("hotkeys") or {})}
                settings.update(loaded)
                if hotkeys:
                    settings["hotkeys"] = hotkeys
        except Exception:
            pass
    return settings


def save(feature_id: str, settings: dict):
    path = settings_path(feature_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")


class Watcher:
    """Poll a feature's settings.json and report when it changed on disk."""

    def __init__(self, feature_id: str):
        self.feature_id = feature_id
        self._path = settings_path(feature_id)
        self._stamp = self._read_stamp()

    def _read_stamp(self):
        try:
            st = self._path.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def changed(self) -> bool:
        stamp = self._read_stamp()
        if stamp != self._stamp:
            self._stamp = stamp
            return True
        return False
