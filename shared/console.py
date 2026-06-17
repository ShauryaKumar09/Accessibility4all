"""Safe console output on Windows (cp1252) and other limited encodings."""

from __future__ import annotations

import sys

_configured = False


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
