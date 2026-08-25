"""macOS keyboard-layout workaround for pynput listeners.

pynput's macOS listener reads the current keyboard layout from HIToolbox
(``TISCopyCurrentKeyboardInputSource``) on its own listener thread. Recent
macOS builds made that call assert it is running on the main dispatch queue,
so the whole process dies with ``SIGTRAP`` the moment a listener starts inside
a GUI app — which is what killed Page Reader and Tone Reader on toggle.

``install()`` reads the layout once on the main thread and hands the listener
that cached value instead. Call it at import time from the main thread, before
any listener starts. The layout is captured once, so switching input sources
mid-session needs a feature restart to pick up the new one.
"""

from __future__ import annotations

import contextlib
import sys
import threading

_installed = False


def install() -> bool:
    """Patch pynput to stop touching HIToolbox off the main thread.

    Returns True if the patch is in place (or was not needed).
    """
    global _installed
    if _installed or sys.platform != "darwin":
        return True
    if threading.current_thread() is not threading.main_thread():
        return False

    try:
        from pynput._util import darwin as _util_darwin
        from pynput.keyboard import _darwin as _keyboard_darwin
    except Exception:
        return False

    try:
        with _util_darwin.keycode_context() as context:
            cached = context
    except Exception:
        return False

    @contextlib.contextmanager
    def keycode_context():
        yield cached

    _util_darwin.keycode_context = keycode_context
    _keyboard_darwin.keycode_context = keycode_context
    _installed = True
    return True
