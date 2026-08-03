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

from shared.console import configure_stdio, safe_print

configure_stdio()

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
                safe_print(f"[hub] bad feature.json in '{child.name}': {e} — using defaults",
                      flush=True)
        feat = Feature(child, manifest)
        if not feat.entry_path.exists():
            safe_print(f"[hub] skipping '{child.name}': entry '{feat.entry}' not found",
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
        safe_print(f"[hub] could not save state: {e}", flush=True)


# ── UI ────────────────────────────────────────────────────────────────────────
# Two palettes: the default polished-dark theme, and an optional high-contrast
# theme (pure black/white + saturated status colors) toggled from the header.
PALETTE_NORMAL = dict(
    BG="#1a1a2e", CARD="#23233f", CARD_BORDER="#34345a",
    FG="#eaeaff", MUTED="#9c9cc4", ACCENT="#7c83fd",
    ON_COLOR="#5ce488", OFF_COLOR="#5a5a7a", CRASH_COLOR="#ff6b6b",
    FOCUS_RING="#7c83fd", KNOB="#ffffff", CTRL_BG="#2b2b4d",
)
PALETTE_HIGH_CONTRAST = dict(
    BG="#000000", CARD="#000000", CARD_BORDER="#ffffff",
    FG="#ffffff", MUTED="#e0e0e0", ACCENT="#00e5ff",
    ON_COLOR="#00ff66", OFF_COLOR="#aaaaaa", CRASH_COLOR="#ff5c5c",
    FOCUS_RING="#ffe600", KNOB="#000000", CTRL_BG="#000000",
)

# Small per-feature glyph so status/identity never rely on color alone.
FEATURE_ICONS = {
    "voice_control": "🎙",
    "page_reader": "🔊",
    "tone_reader": "🗣",
    "dyslexia_font": "🔤",
}
DEFAULT_ICON = "🧩"

# status key -> (glyph, palette key for color, display text)
STATUS_STYLE = {
    "running": ("●", "ON_COLOR", "Running"),
    "stopped": ("○", "MUTED", "Stopped"),
    "crashed": ("⚠", "CRASH_COLOR", "Crashed"),
    "failed": ("✕", "CRASH_COLOR", "Failed"),
}

FONT_SCALE_MIN, FONT_SCALE_MAX, FONT_SCALE_STEP = 0.85, 1.6, 0.15
BASE_SIZES = {
    "title": 22, "subtitle": 12, "icon": 20,
    "card_name": 15, "card_meta": 9, "card_desc": 11, "card_status": 11,
    "control": 11, "hint": 10,
}


def _rounded_rect_points(x1, y1, x2, y2, r):
    """Control points for a rounded rectangle via create_polygon(smooth=True)."""
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    return [
        x1 + r, y1, x2 - r, y1, x2, y1,
        x2, y1 + r, x2, y2 - r, x2, y2,
        x2 - r, y2, x1 + r, y2, x1, y2,
        x1, y2 - r, x1, y1 + r, x1, y1,
    ]


class PillButton(tk.Canvas):
    """A rounded, keyboard-focusable button used for header/footer controls.

    Click, Space, or Return all activate it; the focus ring is drawn as part
    of the pill outline so keyboard-only users can always see where they are.
    """

    HEIGHT = 34
    H_PAD = 16

    def __init__(self, parent, hub: "Hub", text: str, command):
        self.hub = hub
        self.command = command
        self._text = text
        self._focused = False
        self._btn_w = max(64, hub._fonts["control"].measure(text) + self.H_PAD * 2)
        super().__init__(parent, width=self._btn_w, height=self.HEIGHT,
                          bg=parent["bg"], highlightthickness=0, bd=0,
                          takefocus=1, cursor="hand2")
        self.bind("<Button-1>", lambda e: self.command())
        self.bind("<Return>", lambda e: self.command())
        self.bind("<space>", lambda e: self.command())
        self.bind("<FocusIn>", lambda e: self._set_focus(True))
        self.bind("<FocusOut>", lambda e: self._set_focus(False))
        self._redraw()

    def _set_focus(self, focused: bool):
        self._focused = focused
        self._redraw()

    def _redraw(self):
        self.delete("all")
        p = self.hub.palette
        border = p["FOCUS_RING"] if self._focused else p["CARD_BORDER"]
        pts = _rounded_rect_points(1, 1, self._btn_w - 1, self.HEIGHT - 1, self.HEIGHT // 2)
        self.create_polygon(pts, smooth=True, fill=p["CTRL_BG"],
                            outline=border, width=2.5 if self._focused else 1.5)
        self.create_text(self._btn_w / 2, self.HEIGHT / 2, text=self._text,
                         font=self.hub._fonts["control"], fill=p["FG"])


class FeatureCard(tk.Canvas):
    """One feature, drawn as a single rounded card.

    The whole card — not just a small switch — is the click/keyboard target:
    a bigger hit area is easier to use for anyone with limited fine motor
    control. Status is shown three redundant ways (a colored accent bar, a
    glyph, and text) so it's never conveyed by color alone.
    """

    RADIUS = 14
    PAD = 16
    ICON_COL_W = 34
    SWITCH_W, SWITCH_H = 50, 26

    def __init__(self, parent, hub: "Hub", feat: Feature):
        self.hub = hub
        self.feat = feat
        self.on = False
        self.kind = "stopped"
        self.detail = ""
        self._focused = False
        self._last_width = None
        super().__init__(parent, highlightthickness=0, bd=0, height=1,
                          bg=hub.palette["BG"], takefocus=1, cursor="hand2")
        self.bind("<Button-1>", self._activate)
        self.bind("<Return>", self._activate)
        self.bind("<space>", self._activate)
        self.bind("<FocusIn>", lambda e: self._set_focus(True))
        self.bind("<FocusOut>", lambda e: self._set_focus(False))
        self.bind("<Configure>", self._on_configure)
        self.pack(fill="x", padx=8, pady=6)

    def _activate(self, event=None):
        self.hub._toggle(self.feat)

    def _set_focus(self, focused: bool):
        self._focused = focused
        if self._last_width:
            self._redraw(self._last_width)

    def set_state(self, on: bool, kind: str, detail: str = ""):
        self.on, self.kind, self.detail = on, kind, detail
        if self._last_width:
            self._redraw(self._last_width)

    def _on_configure(self, event):
        if event.width == self._last_width:
            return
        self._last_width = event.width
        self._redraw(event.width)

    def _redraw(self, width: int):
        p = self.hub.palette
        f = self.hub._fonts
        self.delete("all")
        pad = self.PAD
        icon_x = pad
        text_x = icon_x + self.ICON_COL_W
        text_w = max(120, width - text_x - self.SWITCH_W - pad * 2)

        icon_item = self.create_text(icon_x, pad - 2, anchor="nw",
                                     text=FEATURE_ICONS.get(self.feat.id, DEFAULT_ICON),
                                     font=f["icon"], fill=p["FG"])

        name_item = self.create_text(text_x, pad - 4, anchor="nw",
                                     text=self.feat.name, font=f["card_name"], fill=p["FG"])
        name_bbox = self.bbox(name_item)
        meta = " ".join(part for part in (
            f"v{self.feat.version}" if self.feat.version else "",
            f"· {self.feat.author}" if self.feat.author else "") if part)
        if meta:
            self.create_text(name_bbox[2] + 8, pad - 4, anchor="nw", text=meta,
                             font=f["card_meta"], fill=p["MUTED"])
        y = name_bbox[3] + 6

        if self.feat.description:
            desc_item = self.create_text(text_x, y, anchor="nw", text=self.feat.description,
                                         font=f["card_desc"], fill=p["MUTED"],
                                         width=text_w, justify="left")
            y = self.bbox(desc_item)[3] + 8

        glyph, color_key, label = STATUS_STYLE[self.kind]
        status_text = f"{glyph} {label}" + (f" ({self.detail})" if self.detail else "")
        status_item = self.create_text(text_x, y, anchor="nw", text=status_text,
                                       font=f["card_status"], fill=p[color_key])
        y = self.bbox(status_item)[3]

        content_bottom = max(y, self.bbox(icon_item)[3]) + pad
        min_height = pad * 2 + self.SWITCH_H + 4
        height = max(content_bottom, min_height)

        border_color = {"running": p["ON_COLOR"], "crashed": p["CRASH_COLOR"],
                        "failed": p["CRASH_COLOR"]}.get(self.kind, p["CARD_BORDER"])
        if self._focused:
            border_color = p["FOCUS_RING"]
        bg_pts = _rounded_rect_points(1, 1, width - 1, height - 1, self.RADIUS)
        bg_item = self.create_polygon(bg_pts, smooth=True, fill=p["CARD"],
                                      outline=border_color, width=2.5 if self._focused else 1.5)
        self.tag_lower(bg_item)

        accent_color = {"running": p["ON_COLOR"], "crashed": p["CRASH_COLOR"],
                        "failed": p["CRASH_COLOR"]}.get(self.kind, p["OFF_COLOR"])
        self.create_rectangle(2, self.RADIUS, 6, height - self.RADIUS,
                              fill=accent_color, outline="")

        sx2 = width - pad
        sx1 = sx2 - self.SWITCH_W
        sy1 = (height - self.SWITCH_H) // 2
        sy2 = sy1 + self.SWITCH_H
        track_color = p["ON_COLOR"] if self.on else p["OFF_COLOR"]
        r2 = self.SWITCH_H // 2
        self.create_oval(sx1, sy1, sx1 + self.SWITCH_H, sy2, fill=track_color, outline="")
        self.create_oval(sx2 - self.SWITCH_H, sy1, sx2, sy2, fill=track_color, outline="")
        self.create_rectangle(sx1 + r2, sy1, sx2 - r2, sy2, fill=track_color, outline="")
        knob_d = self.SWITCH_H - 6
        knob_x = (sx2 - knob_d - 3) if self.on else (sx1 + 3)
        self.create_oval(knob_x, sy1 + 3, knob_x + knob_d, sy1 + 3 + knob_d,
                         fill=p["KNOB"], outline="")

        if int(float(self["height"])) != height:
            self.configure(height=height)
        self.configure(bg=p["BG"])


class Hub(tk.Tk):
    POLL_MS = 1000                       # how often we check feature liveness

    def __init__(self):
        super().__init__()
        self._procs: dict[str, subprocess.Popen] = {}
        self._enabled: set[str] = load_enabled()
        self._rows: dict[str, "FeatureCard"] = {}
        self._features: list[Feature] = []
        self.palette = PALETTE_NORMAL
        self.high_contrast = False
        self.font_scale = 1.0
        self._fonts = {
            name: tkfont.Font(family="Helvetica", size=size,
                              weight="bold" if name in ("title", "card_name") else "normal")
            for name, size in BASE_SIZES.items()
        }

        self.title("Accessibility4all")
        self.geometry("580x620")
        self.minsize(480, 380)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._full_rebuild()
        self._restore_enabled()
        self.after(self.POLL_MS, self._poll)

    # ── rebuild everything (used at startup and after a high-contrast toggle) ──
    def _full_rebuild(self):
        for child in self.winfo_children():
            child.destroy()
        self.configure(bg=self.palette["BG"])
        self._build_chrome()
        self._rebuild_features()

    # ── static chrome (header + scroll area + footer) ──
    def _build_chrome(self):
        p = self.palette

        header = tk.Frame(self, bg=p["BG"])
        header.pack(fill="x", padx=24, pady=(20, 12))

        title_row = tk.Frame(header, bg=p["BG"])
        title_row.pack(fill="x")
        tk.Label(title_row, text="Accessibility4all", font=self._fonts["title"],
                 fg=p["FG"], bg=p["BG"]).pack(side="left")

        controls = tk.Frame(title_row, bg=p["BG"])
        controls.pack(side="right")
        PillButton(controls, self, "A−", lambda: self._adjust_font_scale(-FONT_SCALE_STEP)
                   ).pack(side="left", padx=(0, 4))
        PillButton(controls, self, "A+", lambda: self._adjust_font_scale(FONT_SCALE_STEP)
                   ).pack(side="left", padx=(0, 10))
        PillButton(
            controls, self,
            "High contrast: On" if self.high_contrast else "High contrast: Off",
            self._toggle_high_contrast,
        ).pack(side="left")

        tk.Label(header, text="Toggle the features you need.", font=self._fonts["subtitle"],
                 fg=p["MUTED"], bg=p["BG"]).pack(anchor="w", pady=(6, 0))

        tk.Frame(self, bg=p["CARD_BORDER"], height=1).pack(fill="x", padx=24)

        # scrollable list of feature cards
        container = tk.Frame(self, bg=p["BG"])
        container.pack(fill="both", expand=True, padx=16, pady=8)
        canvas = tk.Canvas(container, bg=p["BG"], highlightthickness=0)
        scrollbar = tk.Scrollbar(container, orient="vertical", command=canvas.yview)
        self._list = tk.Frame(canvas, bg=p["BG"])
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

        tk.Frame(self, bg=p["CARD_BORDER"], height=1).pack(fill="x", padx=24)

        footer = tk.Frame(self, bg=p["BG"])
        footer.pack(fill="x", padx=24, pady=(12, 16))
        PillButton(footer, self, "Rescan features", self._rebuild_features
                   ).pack(side="left")
        self._hint = tk.Label(footer, text="", font=self._fonts["hint"],
                              fg=p["MUTED"], bg=p["BG"])
        self._hint.pack(side="right")

    # ── (re)build the list of feature cards ──
    def _rebuild_features(self):
        p = self.palette
        for child in self._list.winfo_children():
            child.destroy()
        self._rows.clear()
        self._features = discover_features()

        if not self._features:
            tk.Label(self._list,
                     text="No features found.\nAdd a folder under ./features/ "
                          "(see features/README.md).",
                     font=self._fonts["card_desc"],
                     fg=p["MUTED"], bg=p["BG"], justify="left").pack(anchor="w", padx=8, pady=20)
            self._update_hint()
            return

        for feat in self._features:
            card = FeatureCard(self._list, self, feat)
            self._rows[feat.id] = card
            # reflect any already-running process after a rescan
            running = feat.id in self._procs and self._procs[feat.id].poll() is None
            self._render_row(feat.id, running, "running" if running else "stopped")

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
        safe_print(f"[hub] starting '{feat.name}' -> {feat.entry_path}", flush=True)
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
            safe_print(f"[hub] failed to start '{feat.name}': {e}", flush=True)
            self._render_row(feat.id, False, "failed", detail=str(e))

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
                    self._render_row(feat.id, False, "crashed", detail=f"exit {code}")
                    safe_print(f"[hub] '{feat.name}' exited with code {code}", flush=True)
        self.after(self.POLL_MS, self._poll)

    # ── row rendering ──
    def _render_row(self, feat_id: str, on: bool, kind: str, detail: str = ""):
        row = self._rows.get(feat_id)
        if not row:
            return
        row.set_state(on, kind, detail)
        self._update_hint()

    def _update_hint(self):
        if not self._features:
            self._hint.configure(text="")
            return
        running = sum(1 for f in self._features
                     if f.id in self._procs and self._procs[f.id].poll() is None)
        self._hint.configure(text=f"{running} of {len(self._features)} running")

    # ── accessibility controls ──
    def _adjust_font_scale(self, delta: float):
        self.font_scale = round(min(FONT_SCALE_MAX, max(FONT_SCALE_MIN, self.font_scale + delta)), 2)
        for name, base_size in BASE_SIZES.items():
            self._fonts[name].configure(size=round(base_size * self.font_scale))
        # button widths and wrapped text extents depend on font metrics, so a
        # full rebuild is the simplest way to keep every widget's size in sync
        self._full_rebuild()

    def _toggle_high_contrast(self):
        self.high_contrast = not self.high_contrast
        self.palette = PALETTE_HIGH_CONTRAST if self.high_contrast else PALETTE_NORMAL
        self._full_rebuild()

    # ── shutdown ──
    def _on_close(self):
        for feat in self._features:
            self._stop(feat)
        self.destroy()


if __name__ == "__main__":
    safe_print(f"[hub] Accessibility4all hub starting | features dir: {FEATURES_DIR}",
               flush=True)
    Hub().mainloop()
    safe_print("[hub] hub closed", flush=True)

