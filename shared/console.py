"""Safe console output on Windows (cp1252) and other limited encodings."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

_configured = False
_LOG_FILE = Path(__file__).resolve().parent.parent / "apply_log.jsonl"


def configure_stdio() -> None:
    """Prefer UTF-8 stdout/stderr so Unicode log text does not crash features."""
    global _configured
    if _configured:
        return
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    _configured = True


def safe_print(*args, sep: str = " ", end: str = "\n", flush: bool = False) -> None:
    configure_stdio()
    text = sep.join(str(a) for a in args) + end
    try:
        print(text, end="", flush=flush)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        sys.stdout.buffer.write(text.encode(enc, errors="replace"))
        if flush:
            sys.stdout.flush()


def log_event(source: str, action: str, **fields) -> None:
    """Append one structured, timestamped record to apply_log.jsonl.

    For features that write OS state directly (registry, hosts file) the
    hub's own terminal scrolls away and is per-process, so this is the one
    place to look when a toggle claimed to apply but the machine didn't
    visibly change — every apply/verify call logs what it asked for and
    what it actually read back.
    """
    record = {"ts": time.time(), "pid": __import__("os").getpid(),
              "source": source, "action": action, **fields}
    try:
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass
