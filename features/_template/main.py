"""Example feature — a copy-me starter for new Accessibility4all features.

HOW FEATURES WORK
-----------------
The hub (../../hub.py) launches this file as its OWN process when its toggle is
switched ON, and terminates that process when the toggle is switched OFF. So:

  * This file must be runnable on its own:  python features/<your_feature>/main.py
  * It runs in its own process — a crash here will NOT take down the hub or any
    other feature.
  * You can use anything: tkinter, a background loop, OpenCV, sockets, etc.
  * When toggled OFF the hub sends a terminate signal. Do cleanup in a SIGTERM
    handler (see below) or an atexit hook if you hold resources (cameras, files).
  * Anything you print goes to the hub's terminal, which is great for debugging.

Replace everything below with your real feature. Keep the SIGTERM handler if you
need a clean shutdown.
"""

import signal
import sys
import time


def shutdown(signum, frame):
    # Called when the hub toggles this feature OFF (SIGTERM) or on Ctrl+C.
    print("[example] shutting down cleanly", flush=True)
    sys.exit(0)


def main():
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    print("[example] feature started — running until toggled off", flush=True)
    n = 0
    while True:
        n += 1
        print(f"[example] heartbeat {n}", flush=True)
        time.sleep(2)


if __name__ == "__main__":
    main()
