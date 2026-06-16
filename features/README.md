# Accessibility4all — Feature Developer Guide

This app is a **hub** (`../hub.py`) that shows one on/off toggle per feature.
Each feature is an independent folder in this directory that runs as its **own
process**. That means multiple people can build features in parallel without
touching each other's code or the hub.

---

## The contract (everything you must follow)

A feature is a folder under `features/` containing:

```
features/
└── my_feature/
    ├── feature.json      ← required: tells the hub how to show + run you
    ├── main.py           ← your entry point (runnable on its own)
    └── ...               ← any other files/subfolders you want
```

### 1. `feature.json` — the manifest

```json
{
  "name": "My Feature",
  "description": "One sentence shown under the toggle.",
  "entry": "main.py",
  "version": "0.1.0",
  "author": "your name"
}
```

| field         | required | meaning                                              |
|---------------|----------|------------------------------------------------------|
| `name`        | no\*     | label shown on the card (defaults to folder name)    |
| `description` | no       | short text under the name                            |
| `entry`       | no\*     | file the hub runs (defaults to `main.py`)            |
| `version`     | no       | shown as `v0.1.0`                                     |
| `author`      | no       | shown next to the version                            |

\* Optional, but provide them — a missing/invalid manifest falls back to the
folder name + `main.py`.

### 2. Your entry file must be runnable on its own

The hub launches it with the **same Python interpreter** (the project venv) like:

```bash
python features/my_feature/main.py
```

with the **working directory set to your feature folder**. So:

- Put a `if __name__ == "__main__":` block that starts your feature.
- Resolve files relative to your own folder (`Path(__file__).parent / "data.json"`).
- `load_dotenv()` still finds the project-root `.env` (it searches parent dirs),
  so shared secrets like API keys keep working.

### 3. Toggling off = your process is terminated

When the user switches your toggle OFF (or closes the hub), the hub sends your
process a **terminate signal**, then force-kills it after 3 seconds if it hasn't
exited. If you hold resources (camera, file handles, sockets), clean up in a
`SIGTERM` handler:

```python
import signal, sys
def shutdown(signum, frame):
    # release camera, save state, etc.
    sys.exit(0)
signal.signal(signal.SIGTERM, shutdown)
```

If you have nothing to clean up, you can ignore this — being killed is fine.

### 4. Logging

Anything you `print()` goes straight to the **hub's terminal**, so run the hub
from a terminal while developing and you'll see your feature's output inline.
Use `flush=True` so lines appear immediately.

---

## Add a new feature in 4 steps

1. Copy `_template/` to `features/your_feature/`.
2. Edit `feature.json` (name, description, author).
3. Replace `main.py` with your code (keep it runnable standalone).
4. Run the hub (`python ../hub.py`) and click **Rescan features** — your toggle
   appears. No hub code changes needed.

---

## Things to know / rules of the road

- **Folder naming:** the hub ignores any folder whose name starts with `_` or
  `.` — that's why `_template/` never shows up as a real toggle. Use plain names
  for real features (`eye_tracker`, `voice_control`, ...).
- **Isolation:** your feature runs in its own process. A crash in your code
  cannot take down the hub or other features. If your process exits on its own,
  the hub flips your toggle back to OFF and shows `crashed (exit N)`.
- **No shared in-memory state:** features can't call each other's functions.
  If two features must share data, go through files, a local socket, or another
  explicit channel — don't assume a shared process.
- **Dependencies:** list your Python deps in a `requirements.txt` inside your
  folder so others can install them. Everyone shares the one project venv.
- **GUI is fine:** because each feature is its own process, you can build a
  tkinter / OpenCV / Qt window freely without clashing with the hub or other
  features.
- **State persistence:** the hub remembers which toggles were on (in
  `../hub_state.json`) and auto-starts them next launch. You don't manage this.

See `voice_control/` for a complete real example and `_template/` for the
minimal starting point.
