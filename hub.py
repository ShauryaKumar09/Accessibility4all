"""Accessibility4all — feature hub.

A launcher that shows one toggle per feature. Each feature lives in its own
folder under ./features/ and runs as its own OS process:

    toggle ON   -> launch  features/<name>/<entry>  as a subprocess
    toggle OFF  -> terminate that subprocess

Features are discovered automatically, so separate developers can drop a new
folder into ./features/ and it appears here with no changes to this file. See
features/README.md for the contract every feature must follow.
"""

import sys
import json
import subprocess
from pathlib import Path

import tkinter as tk
from tkinter import font as tkfont

ROOT = Path(__file__).parent.resolve()
FEATURES_DIR = ROOT / "features"
STATE_FILE = ROOT / "hub_state.json"     # remembers which toggles were on


# ── Feature model + discovery ─────────────────────────────────────────────────
class Feature:
    """One feature folder, described by its feature.json manifest."""

    def __init__(self, dir_path: Path, manifest: dict):
        self.dir = dir_path
        self.id = dir_path.name                      # folder name = stable id
        self.name = manifest.get("name") or dir_path.name
        self.description = manifest.get("description", "")
        self.entry = manifest.get("entry", "main.py")
        self.version = manifest.get("version", "")
        self.author = manifest.get("author", "")

    @property
    def entry_path(self) -> Path:
        return self.dir / self.entry


def discover_features() -> list[Feature]:
    """Find every runnable feature folder under ./features/.

    Folders whose names start with '_' or '.' are ignored (e.g. _template).
    A folder needs an existing entry file to be listed; a missing or invalid
    feature.json falls back to sensible defaults (folder name + main.py).
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
                print(f"[hub] bad feature.json in '{child.name}': {e} — using defaults",
                      flush=True)
        feat = Feature(child, manifest)
        if not feat.entry_path.exists():
            print(f"[hub] skipping '{child.name}': entry '{feat.entry}' not found",
                  flush=True)
            continue
        features.append(feat)
    return features


# ── Toggle-state persistence ──────────────────────────────────────────────────
def load_enabled() -> set[str]:
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text()).get("enabled", []))
        except Exception:
            return set()
    return set()


def save_enabled(enabled: set[str]):
    try:
        STATE_FILE.write_text(json.dumps({"enabled": sorted(enabled)}, indent=2))
    except Exception as e:
        print(f"[hub] could not save state: {e}", flush=True)


# ── UI ────────────────────────────────────────────────────────────────────────
BG = "#1a1a2e"
CARD = "#23233f"
FG = "#e0e0ff"
MUTED = "#8a8ab0"
ON_COLOR = "#69db7c"
OFF_COLOR = "#6c757d"
CRASH_COLOR = "#ff6b6b"


class Hub(tk.Tk):
    POLL_MS = 1000                       # how often we check feature liveness

    def __init__(self):
        super().__init__()
        self._procs: dict[str, subprocess.Popen] = {}
        self._enabled: set[str] = load_enabled()
        self._rows: dict[str, dict] = {}          # feature id -> {btn, status}
        self._features: list[Feature] = []

        self.title("Accessibility4all")
        self.configure(bg=BG)
        self.geometry("520x560")
        self.minsize(440, 360)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_chrome()
        self._rebuild_features()
        self._restore_enabled()
        self.after(self.POLL_MS, self._poll)

    # ── static chrome (header + scroll area + footer) ──
    def _build_chrome(self):
        title_font = tkfont.Font(family="Helvetica", size=22, weight="bold")
        sub_font = tkfont.Font(family="Helvetica", size=12)

        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", padx=24, pady=(20, 8))
        tk.Label(header, text="Accessibility4all", font=title_font,
                 fg=FG, bg=BG).pack(anchor="w")
        tk.Label(header, text="Toggle the features you need.", font=sub_font,
                 fg=MUTED, bg=BG).pack(anchor="w")

        # scrollable list of feature cards
        container = tk.Frame(self, bg=BG)
        container.pack(fill="both", expand=True, padx=16, pady=8)
        canvas = tk.Canvas(container, bg=BG, highlightthickness=0)
        scrollbar = tk.Scrollbar(container, orient="vertical", command=canvas.yview)
        self._list = tk.Frame(canvas, bg=BG)
        self._list.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        self._list_window = canvas.create_window((0, 0), window=self._list, anchor="nw")
        canvas.bind(
            "<Configure>",
            lambda e: canvas.itemconfig(self._list_window, width=e.width),
        )
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        footer = tk.Frame(self, bg=BG)
        footer.pack(fill="x", padx=24, pady=(4, 16))
        tk.Button(footer, text="Rescan features", command=self._rebuild_features,
                  font=tkfont.Font(family="Helvetica", size=11),
                  bg=CARD, fg=FG, activebackground="#33335a", activeforeground=FG,
                  relief="flat", padx=14, pady=6, cursor="hand2").pack(side="left")
        self._hint = tk.Label(
            footer, text="", font=tkfont.Font(family="Helvetica", size=10),
            fg=MUTED, bg=BG)
        self._hint.pack(side="right")

    # ── (re)build the list of feature cards ──
    def _rebuild_features(self):
        for child in self._list.winfo_children():
            child.destroy()
        self._rows.clear()
        self._features = discover_features()

        if not self._features:
            tk.Label(self._list,
                     text="No features found.\nAdd a folder under ./features/ "
                          "(see features/README.md).",
                     font=tkfont.Font(family="Helvetica", size=12),
                     fg=MUTED, bg=BG, justify="left").pack(anchor="w", padx=8, pady=20)
            return

        for feat in self._features:
            self._build_card(feat)
            # reflect any already-running process after a rescan
            running = feat.id in self._procs and self._procs[feat.id].poll() is None
            self._render_row(feat.id, running,
                             "running" if running else "stopped")

    def _build_card(self, feat: Feature):
        card = tk.Frame(self._list, bg=CARD, bd=0, highlightthickness=0)
        card.pack(fill="x", padx=8, pady=6)

        left = tk.Frame(card, bg=CARD)
        left.pack(side="left", fill="both", expand=True, padx=14, pady=12)

        name_row = tk.Frame(left, bg=CARD)
        name_row.pack(anchor="w", fill="x")
        tk.Label(name_row, text=feat.name,
                 font=tkfont.Font(family="Helvetica", size=15, weight="bold"),
                 fg=FG, bg=CARD).pack(side="left")
        meta = " ".join(p for p in (f"v{feat.version}" if feat.version else "",
                                    f"· {feat.author}" if feat.author else "") if p)
        if meta:
            tk.Label(name_row, text="  " + meta,
                     font=tkfont.Font(family="Helvetica", size=9),
                     fg=MUTED, bg=CARD).pack(side="left")

        if feat.description:
            tk.Label(left, text=feat.description,
                     font=tkfont.Font(family="Helvetica", size=11),
                     fg=MUTED, bg=CARD, wraplength=300, justify="left").pack(
                anchor="w", pady=(4, 0))

        status = tk.Label(left, text="stopped",
                          font=tkfont.Font(family="Helvetica", size=10, weight="bold"),
                          fg=MUTED, bg=CARD)
        status.pack(anchor="w", pady=(6, 0))

        btn = tk.Button(card, text="OFF",
                        font=tkfont.Font(family="Helvetica", size=13, weight="bold"),
                        width=6, bg=OFF_COLOR, fg="white",
                        activebackground="#5563f0", activeforeground="white",
                        relief="flat", cursor="hand2",
                        command=lambda f=feat: self._toggle(f))
        btn.pack(side="right", padx=14, pady=12)

        self._rows[feat.id] = {"btn": btn, "status": status}

    # ── toggle / process control ──
    def _toggle(self, feat: Feature):
        running = feat.id in self._procs and self._procs[feat.id].poll() is None
        if running:
            self._stop(feat)
            self._enabled.discard(feat.id)
        else:
            self._start(feat)
            self._enabled.add(feat.id)
        save_enabled(self._enabled)

    def _start(self, feat: Feature):
        if feat.id in self._procs and self._procs[feat.id].poll() is None:
            return                                  # already running
        print(f"[hub] starting '{feat.name}' -> {feat.entry_path}", flush=True)
        try:
            # Same interpreter (venv), cwd = the feature folder so relative paths
            # and .env resolve as the developer expects. stdout/stderr inherit
            # the hub's terminal for easy debugging.
            proc = subprocess.Popen(
                [sys.executable, str(feat.entry_path)],
                cwd=str(feat.dir),
            )
            self._procs[feat.id] = proc
            self._render_row(feat.id, True, "running")
        except Exception as e:
            print(f"[hub] failed to start '{feat.name}': {e}", flush=True)
            self._render_row(feat.id, False, f"failed: {e}")

    def _stop(self, feat: Feature):
        proc = self._procs.get(feat.id)
        if proc and proc.poll() is None:
            print(f"[hub] stopping '{feat.name}'", flush=True)
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                print(f"[hub] '{feat.name}' didn't exit, killing", flush=True)
                proc.kill()
        self._procs.pop(feat.id, None)
        self._render_row(feat.id, False, "stopped")

    def _restore_enabled(self):
        """Auto-start features that were on last time (if they still exist)."""
        known = {f.id for f in self._features}
        for feat in self._features:
            if feat.id in self._enabled and feat.id in known:
                self._start(feat)
        # drop remembered ids whose folders are gone
        self._enabled &= known
        save_enabled(self._enabled)

    # ── liveness polling ──
    def _poll(self):
        for feat in self._features:
            proc = self._procs.get(feat.id)
            if proc is not None and proc.poll() is not None:
                code = proc.returncode
                self._procs.pop(feat.id, None)
                self._enabled.discard(feat.id)
                save_enabled(self._enabled)
                if code == 0:
                    self._render_row(feat.id, False, "stopped")
                else:
                    self._render_row(feat.id, False, f"crashed (exit {code})",
                                     crashed=True)
                    print(f"[hub] '{feat.name}' exited with code {code}", flush=True)
        self.after(self.POLL_MS, self._poll)

    # ── row rendering ──
    def _render_row(self, feat_id: str, on: bool, status: str, crashed: bool = False):
        row = self._rows.get(feat_id)
        if not row:
            return
        row["btn"].configure(text="ON" if on else "OFF",
                             bg=ON_COLOR if on else OFF_COLOR)
        color = ON_COLOR if on else (CRASH_COLOR if crashed else MUTED)
        row["status"].configure(text=status, fg=color)

    # ── shutdown ──
    def _on_close(self):
        for feat in self._features:
            self._stop(feat)
        self.destroy()


if __name__ == "__main__":
    print(f"[hub] Accessibility4all hub starting | features dir: {FEATURES_DIR}",
          flush=True)
    Hub().mainloop()
    print("[hub] hub closed", flush=True)
