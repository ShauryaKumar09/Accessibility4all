"""File-based IPC between Accessibility4all features."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUS_DIR = ROOT / "feature_bus"
COMMANDS_FILE = BUS_DIR / "commands.jsonl"
PRESENCE_FILE = BUS_DIR / "presence.json"
HUB_STATE_FILE = ROOT / "hub_state.json"
PAGE_READER_SETTINGS = ROOT / "features" / "page_reader" / "settings.json"

# One source of truth for every feature's defaults now lives in
# shared/settings_store.py (the hub edits those files). Kept here under the old
# name so existing callers — e.g. voice_control's read-aloud handoff — still work.
from shared.settings_store import DEFAULTS as _DEFAULTS  # noqa: E402

DEFAULT_PAGE_READER_SETTINGS = dict(_DEFAULTS["page_reader"])


def ensure_bus_dir():
    BUS_DIR.mkdir(parents=True, exist_ok=True)


def append_command(cmd: str, **kwargs):
    """Append a command for page_reader to execute."""
    ensure_bus_dir()
    entry = {
        "cmd": cmd,
        "ts": datetime.now(timezone.utc).isoformat(),
        **kwargs,
    }
    with open(COMMANDS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def current_offset() -> int:
    """Byte offset of the end of the command log — where a fresh listener starts.

    Starting at 0 would replay every command ever written (a "read the page"
    from a previous session firing the moment the feature comes up).
    """
    if not COMMANDS_FILE.exists():
        return 0
    try:
        return len(COMMANDS_FILE.read_text(encoding="utf-8"))
    except OSError:
        return 0


def command_age(entry: dict) -> float:
    """Seconds since the command was written; `inf` when it has no usable ts."""
    ts = entry.get("ts")
    if not ts:
        return float("inf")
    try:
        when = datetime.fromisoformat(ts)
    except ValueError:
        return float("inf")
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds()


def read_commands_after(offset: int) -> tuple[list[dict], int]:
    """Return new command entries after byte offset; new offset for next poll."""
    if not COMMANDS_FILE.exists():
        return [], 0
    text = COMMANDS_FILE.read_text(encoding="utf-8")
    if offset > len(text):
        offset = 0
    chunk = text[offset:]
    entries = []
    for line in chunk.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries, len(text)


def load_presence() -> dict:
    if not PRESENCE_FILE.exists():
        return {}
    try:
        return json.loads(PRESENCE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_presence(data: dict):
    ensure_bus_dir()
    PRESENCE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def update_presence(feature_id: str, pid: int, window: dict | None = None):
    data = load_presence()
    entry = {"pid": pid}
    if window:
        entry["window"] = window
    data[feature_id] = entry
    save_presence(data)


def remove_presence(feature_id: str):
    data = load_presence()
    data.pop(feature_id, None)
    save_presence(data)


def is_feature_running(feature_id: str) -> bool:
    entry = load_presence().get(feature_id)
    if not entry:
        return False
    pid = entry.get("pid")
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def load_page_reader_settings() -> dict:
    settings = dict(DEFAULT_PAGE_READER_SETTINGS)
    if PAGE_READER_SETTINGS.exists():
        try:
            loaded = json.loads(PAGE_READER_SETTINGS.read_text(encoding="utf-8"))
            settings.update(loaded)
            if "hotkeys" in loaded:
                settings["hotkeys"] = {**DEFAULT_PAGE_READER_SETTINGS["hotkeys"],
                                       **loaded["hotkeys"]}
        except Exception:
            pass
    return settings


def is_hub_feature_enabled(feature_id: str) -> bool:
    if not HUB_STATE_FILE.exists():
        return False
    try:
        enabled = json.loads(HUB_STATE_FILE.read_text(encoding="utf-8")).get("enabled", [])
        return feature_id in enabled
    except Exception:
        return False
