"""Accessibility4all — feature hub.

A launcher that shows one large row per feature. Each feature lives in its own
folder under ./features/ and runs as its own OS process:

    toggle ON   -> launch  features/<name>/<entry>  as a subprocess
    toggle OFF  -> terminate that subprocess

Features are discovered automatically, so separate developers can drop a new
folder into ./features/ and it appears here with no changes to this file. See
features/README.md for the contract every feature must follow.

The hub is also where every feature's settings and instructions now live: each
row opens a settings sheet, and each sheet can open a three-step walkthrough.
The feature processes themselves only draw a small desktop bubble.
"""

import sys
import json
import importlib.util
import subprocess
from pathlib import Path

import tkinter as tk
from tkinter import font as tkfont

from shared.console import configure_stdio, safe_print
from shared import hotkeys as hk
from shared import settings_store as store
from shared import ui_kit as ui
from shared import windows_fonts as winfonts
from shared.ui_kit import C

configure_stdio()

ROOT = Path(__file__).parent.resolve()
FEATURES_DIR = ROOT / "features"
STATE_FILE = ROOT / "hub_state.json"     # toggles, text size, walkthroughs seen


def _load_feature_module(feature_id: str):
    """Import a feature's main.py for its pure logic functions (no Tk app built).

    Both colorblind_filter and focus_mode are single-shot actions the hub can
    just call directly rather than round-tripping through settings.json polling.
    Loaded under a unique name (not "main") so the two don't collide in sys.modules.
    """
    path = FEATURES_DIR / feature_id / "main.py"
    spec = importlib.util.spec_from_file_location(f"a4a_feature_{feature_id}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── Plain-language copy (rewritten from the feature.json descriptions) ────────
FEATURE_ORDER = ["voice_control", "page_reader", "tone_reader", "dyslexia_font",
                 "colorblind_filter", "focus_mode"]

FEATURE_COPY = {
    "voice_control": ("Voice Control",
                      "Hold the ` key and tell Chrome what to do."),
    "page_reader": ("Page Reader",
                    "Reads what's on your screen out loud."),
    "tone_reader": ("Tone & Social Cues",
                    "Explains the tone behind a message you highlight."),
    "dyslexia_font": ("Dyslexia Font",
                      "Swaps websites to an easier-to-read font."),
    "colorblind_filter": ("Color Blind Filter",
                          "Makes colors on your screen easier to tell apart."),
    "focus_mode": ("Focus Mode",
                  "Blocks distracting sites for a set amount of time."),
}

WALKTHROUGHS = {
    "voice_control": [
        ("Hold the ` key and just talk",
         "Press and hold the key to the left of the 1. Say something like "
         "“open a new tab” or “scroll down”, then let go."),
        ("A small dot shows it heard you",
         "While you talk, a bubble at the bottom of the screen widens so you can "
         "see it's listening. It shrinks back on its own."),
        ("If it can't find something, say it differently",
         "Try naming what you see on screen — “click the blue Sign in "
         "button” works better than “click sign in”."),
    ],
    "page_reader": [
        ("Press F9 and it starts reading",
         "F9 reads whatever is on the screen out loud. F10 stops it. You can "
         "change both keys on this page."),
        ("A bubble shows the line being read",
         "A small bar sits at the bottom of the screen with the line it's on and "
         "how far through it is. The round button pauses and resumes."),
        ("Ask for one part instead of the whole page",
         "With Voice Control on too, say something like “read the billing "
         "information” and it reads just that part."),
    ],
    "tone_reader": [
        ("Highlight a message, then press the key",
         "Select any text in any app and press your shortcut. It reads the tone "
         "behind the words for you."),
        ("A small card explains it in two sentences",
         "The card appears next to what you highlighted: what the tone is, and "
         "what the person likely means."),
        ("Press Got it to make it go away",
         "The card closes when you press Got it, press Escape, or click "
         "somewhere else. Nothing stays on screen."),
    ],
    "dyslexia_font": [
        ("Pick the font that's easiest for you",
         "Try each one below. The preview underneath changes so you can see how "
         "it reads before you decide."),
        ("Give the letters more room",
         "The two −/+ buttons add space between letters and between lines. Small "
         "steps make a big difference."),
        ("Websites change as you browse",
         "With the Chrome extension installed, pages you open use your font. On "
         "Windows you can also swap the fonts apps use."),
    ],
    "colorblind_filter": [
        ("Pick the type that helps most",
         "Choose Deuteranopia, Protanopia, or Tritanopia. It changes your whole "
         "screen, not just one app."),
        ("Flip the switch any time",
         "It applies right away. If the screen doesn't change, try locking and "
         "unlocking (Win+L)."),
        ("It's a built-in Windows feature",
         "This uses Windows' own color filter, so it works the same way your "
         "system settings do."),
    ],
    "focus_mode": [
        ("Add the sites that distract you",
         "List them one per line, just the domain — like youtube.com."),
        ("Set how long, then flip the switch",
         "Pick a length with the −/+ buttons. Those sites are blocked "
         "everywhere, not just one browser tab."),
        ("It ends on its own",
         "The block clears when the timer runs out, or right away if you turn "
         "the switch off."),
    ],
}

# Copy for the option switches in each settings sheet: (settings key, label).
SHEET_OPTIONS = {
    "voice_control": [
        ("always_on", "Listen without holding the key"),
        ("dictation_mode", "Type what I say instead of running it"),
    ],
    "page_reader": [
        ("voice_guided", "Let me pick sections by voice"),
        ("hover_to_read", "Read when I rest the pointer"),
        ("use_groq_summary", "Skip the clutter"),
    ],
    "tone_reader": [("click_to_analyze", "Shift+Click a paragraph in Chrome")],
    "dyslexia_font": [("website_enabled", "Use this font on websites")],
    "colorblind_filter": [("enabled", "Turn the filter on")],
    "focus_mode": [("active", "Blocking now")],
}

# Shortcut rows per sheet: (hotkey key, label, editable).
SHEET_SHORTCUTS = {
    "voice_control": [(None, "Push-to-talk key", False)],
    "page_reader": [("read_screen", "Start reading", True),
                    ("stop", "Stop reading", True)],
    "tone_reader": [("analyze_selection", "Explain the tone", True)],
    "dyslexia_font": [],
    "colorblind_filter": [],
    "focus_mode": [],
}

TONE_SIZE_LABELS = [("small", "Small"), ("medium", "Medium"), ("large", "Large")]
FILTER_TYPE_LABELS = [
    ("Deuteranopia", "Deuteranopia"), ("Protanopia", "Protanopia"),
    ("Tritanopia", "Tritanopia"), ("Grayscale", "Grayscale"),
    ("Invert", "Invert"), ("Grayscale Inverted", "Gray + Invert"),
]

WINDOW_W = 800
WINDOW_MIN_H = 640


# ── Feature model + discovery ─────────────────────────────────────────────────
class Feature:
    """One feature folder, described by its feature.json manifest."""

    def __init__(self, dir_path: Path, manifest: dict):
        self.dir = dir_path
        self.id = dir_path.name                      # folder name = stable id
        name, description = FEATURE_COPY.get(self.id, (None, None))
        self.name = name or manifest.get("name") or dir_path.name
        self.description = description or manifest.get("description", "")
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


# ── Hub state persistence ─────────────────────────────────────────────────────
def load_state() -> dict:
    state = {"enabled": [], "font_scale": 1.0, "seen_walkthrough": []}
    if STATE_FILE.exists():
        try:
            loaded = json.loads(STATE_FILE.read_text())
            if isinstance(loaded, dict):
                state.update(loaded)
        except Exception:
            pass
    return state


def save_state(enabled: set[str], font_scale: float, seen: set[str]):
    try:
        STATE_FILE.write_text(json.dumps({
            "enabled": sorted(enabled),
            "font_scale": round(font_scale, 2),
            "seen_walkthrough": sorted(seen),
        }, indent=2))
    except Exception as e:
        safe_print(f"[hub] could not save state: {e}", flush=True)


# ── Small drawn controls ──────────────────────────────────────────────────────
class IconButton(ui.TapCanvas):
    """A square control whose glyph is drawn from rectangles: − + play pause."""

    def __init__(self, parent, kind: str, command, size: int = 60,
                 radius: int = 12, bar: int = 22, thickness: int = 4):
        self.kind = kind
        self.bar = bar
        self.thickness = thickness
        self.radius = radius
        super().__init__(parent, size, size, command=command)
        self.redraw()

    def redraw(self):
        self.delete("all")
        size = int(self["width"])
        border = C["ACCENT"] if (self.focused or self.hovered) else C["BORDER_CTRL"]
        ui.rounded_rect(self, 1, 1, size - 1, size - 1, self.radius,
                        fill=C["INSET"], outline=border, width=2)
        cx = cy = size / 2
        b, t = self.bar, self.thickness
        if self.kind in ("minus", "plus"):
            self.create_rectangle(cx - b / 2, cy - t / 2, cx + b / 2, cy + t / 2,
                                  fill=C["FG"], outline="")
            if self.kind == "plus":
                self.create_rectangle(cx - t / 2, cy - b / 2, cx + t / 2, cy + b / 2,
                                      fill=C["FG"], outline="")


class OptionRow(ui.TapCanvas):
    """A settings-sheet switch row: name, On/Off, and the 76x42 switch."""

    def __init__(self, parent, sheet: "SettingsSheet", key: str, label: str,
                 width: int):
        self.sheet = sheet
        self.key = key
        self.label = label
        self.fonts = sheet.fonts
        self.scale = sheet.hub.font_scale
        self._width = width
        super().__init__(parent, width, self._height(), command=self.toggle,
                         bg=C["BG"])
        self.redraw()

    @property
    def on(self) -> bool:
        return bool(self.sheet.settings.get(self.key))

    def _height(self) -> int:
        pad = ui.s(18, self.scale)
        text_w = self._width - ui.s(20, self.scale) * 2 - ui.SWITCH_W - ui.s(120, self.scale)
        lines = ui.wrap_lines(self.fonts["body_b"], self.label, max(120, text_w))
        line_h = self.fonts.px("body") + ui.s(6, self.scale)
        return max(ui.SWITCH_H + pad * 2, len(lines) * line_h + pad * 2)

    def toggle(self):
        self.sheet.set_value(self.key, not self.on)
        self.redraw()

    def redraw(self):
        self.delete("all")
        w, h = self._width, self._height()
        pad_x = ui.s(20, self.scale)
        border = C["ACCENT"] if self.focused else (
            C["ON"] if self.on else C["BORDER"])
        bg = C["CARD_HOVER"] if self.hovered else C["CARD"]
        ui.rounded_rect(self, 1, 1, w - 1, h - 1, 14, fill=bg,
                        outline=border, width=2)

        sw_x = w - pad_x - ui.SWITCH_W
        ui.draw_switch(self, sw_x, (h - ui.SWITCH_H) / 2, self.on)

        state_text = "On" if self.on else "Off"
        state_x = sw_x - ui.s(18, self.scale)
        self.create_text(state_x, h / 2, anchor="e", text=state_text,
                         font=self.fonts["ui_b"],
                         fill=C["ON"] if self.on else C["FG_MUTED"])

        text_w = max(120, state_x - self.fonts["ui_b"].measure("Off")
                     - ui.s(18, self.scale) - pad_x)
        lines = ui.wrap_lines(self.fonts["body_b"], self.label, text_w)
        line_h = self.fonts.px("body") + ui.s(6, self.scale)
        y = (h - len(lines) * line_h) / 2 + line_h / 2
        for line in lines:
            self.create_text(pad_x, y, anchor="w", text=line,
                             font=self.fonts["body_b"], fill=C["FG"])
            y += line_h


class FontCard(ui.TapCanvas):
    """One tappable font choice, rendered in the typeface it selects."""

    def __init__(self, parent, sheet: "SettingsSheet", name: str, note: str,
                 width: int):
        self.sheet = sheet
        self.name = name
        self.note = note
        self.fonts = sheet.fonts
        self.scale = sheet.hub.font_scale
        self._width = width
        self._height = max(84, ui.s(84, self.scale))
        self._sample = tkfont.Font(family=name, size=self.fonts.px("h4"),
                                    weight="bold")
        super().__init__(parent, width, self._height, command=self.choose,
                         bg=C["BG"])
        self.redraw()

    @property
    def selected(self) -> bool:
        return self.sheet.settings.get("font_family") == self.name

    def choose(self):
        self.sheet.set_value("font_family", self.name)
        self.sheet.refresh_font_cards()

    def redraw(self):
        self.delete("all")
        w, h = self._width, self._height
        pad = ui.s(18, self.scale)
        selected = self.selected
        fill = C["ACCENT_FILL"] if selected else C["CARD"]
        border = C["ACCENT"] if (selected or self.focused or self.hovered) \
            else C["BORDER_CTRL"]
        ui.rounded_rect(self, 1, 1, w - 1, h - 1, 14, fill=fill,
                        outline=border, width=2)
        avail = w - pad * 2
        # shrink the sample rather than cutting the font's own name in half
        size = self.fonts.px("h4")
        while size > self.fonts.px("ui_sm") and self._sample.measure(self.name) > avail:
            size -= 1
            self._sample.configure(size=size)
        self.create_text(pad, h / 2 - ui.s(10, self.scale), anchor="w",
                         text=ui.ellipsize(self._sample, self.name, avail),
                         font=self._sample,
                         fill=C["ACCENT_ON"] if selected else C["FG"])
        self.create_text(pad, h / 2 + ui.s(16, self.scale), anchor="w",
                         text=ui.ellipsize(self.fonts["ui_sm"], self.note, avail),
                         font=self.fonts["ui_sm"], fill=C["FG_SECOND"])


class PreviewBox(tk.Canvas):
    """Live font preview. Letter spacing is drawn per character, because
    tkinter fonts have no tracking control."""

    TEXT = ("The quick brown fox jumps over the lazy dog. "
            "Reading should feel calm, not cramped.")

    def __init__(self, parent, sheet: "SettingsSheet", width: int):
        self.sheet = sheet
        self.fonts = sheet.fonts
        self.scale = sheet.hub.font_scale
        self._width = width
        super().__init__(parent, width=width, height=10, bg=C["BG"],
                         highlightthickness=0, bd=0)
        self.redraw()

    def redraw(self):
        self.delete("all")
        s = self.sheet.settings
        size = self.fonts.px("body_lg")
        try:
            font = tkfont.Font(family=s.get("font_family", "Arial"), size=size)
        except tk.TclError:
            font = self.fonts["body_lg"]
        tracking = float(s.get("letter_spacing", 0.0)) * size
        line_h = int(size * float(s.get("line_height", 1.5)))
        pad = ui.s(20, self.scale)
        avail = self._width - pad * 2

        # wrap by measuring words with the extra per-character tracking
        def measure(text: str) -> float:
            return font.measure(text) + tracking * max(0, len(text) - 1)

        words, lines, cur = self.TEXT.split(), [], ""
        for word in words:
            trial = (cur + " " + word).strip()
            if not cur or measure(trial) <= avail:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)

        height = pad * 2 + line_h * len(lines)
        self.configure(height=height)
        ui.rounded_rect(self, 1, 1, self._width - 1, height - 1, 14,
                        fill=C["CARD"], outline=C["BORDER"], width=1)
        y = pad + line_h / 2
        for line in lines:
            if tracking <= 0.5:
                self.create_text(pad, y, anchor="w", text=line, font=font,
                                 fill=C["FG"])
            else:
                x = pad
                for ch in line:
                    self.create_text(x, y, anchor="w", text=ch, font=font,
                                     fill=C["FG"])
                    x += font.measure(ch) + tracking
            y += line_h


# ── Feature row ───────────────────────────────────────────────────────────────
class FeatureRow(ui.TapCanvas):
    """One feature, drawn as a single large rounded row.

    The whole row is the click/keyboard target — a bigger hit area is easier to
    use with limited fine motor control. State is carried by the word On/Off,
    the switch position, and the green border, so it is never colour alone.
    """

    def __init__(self, parent, hub: "Hub", feat: Feature, width: int):
        self.hub = hub
        self.feat = feat
        self.on = False
        self._width = width
        self.scale = hub.font_scale
        self.fonts = hub.fonts
        super().__init__(parent, width, 10, command=self._toggle, bg=C["BG"])
        self.settings_btn = ui.TextButton(
            self, hub.fonts, "Settings", lambda: hub.open_settings(self.feat),
            role="ui_sm", height=max(ui.MIN_TARGET, ui.s(46, self.scale)),
            h_pad=ui.s(16, self.scale), radius=12)
        self._btn_window = self.create_window(0, 0, window=self.settings_btn,
                                              anchor="e")
        self.configure(height=self._height())
        self.redraw()

    def _toggle(self):
        self.hub.toggle(self.feat)

    def set_on(self, on: bool):
        self.on = on
        self.redraw()

    # geometry -----------------------------------------------------------
    def _pads(self):
        return ui.s(26, self.scale), ui.s(24, self.scale)

    def _text_width(self) -> int:
        pad_x, _ = self._pads()
        right = (ui.SWITCH_W + ui.s(18, self.scale)
                 + max(ui.s(36, self.scale), self.fonts["body_sm_b"].measure("Off"))
                 + ui.s(24, self.scale) + int(self.settings_btn["width"])
                 + ui.s(24, self.scale))
        return max(160, self._width - pad_x * 2 - right)

    def _lines(self):
        name_lines = ui.wrap_lines(self.fonts["h3_b"], self.feat.name,
                                   self._text_width())
        desc_lines = ui.wrap_lines(self.fonts["body_sm"], self.feat.description,
                                   self._text_width())
        return name_lines, desc_lines

    def _height(self) -> int:
        _, pad_y = self._pads()
        name_lines, desc_lines = self._lines()
        name_h = len(name_lines) * (self.fonts.px("h3") + ui.s(6, self.scale))
        desc_h = len(desc_lines) * int(self.fonts.px("body_sm") * 1.4)
        content = name_h + ui.s(5, self.scale) + desc_h
        control_h = max(ui.SWITCH_H, int(self.settings_btn["height"]))
        return int(max(content + pad_y * 2, control_h + pad_y * 2,
                       ui.s(110, self.scale)))

    def redraw(self):
        self.delete("row")
        w, h = self._width, self._height()
        if int(float(self["height"])) != h:
            self.configure(height=h)
        pad_x, pad_y = self._pads()

        border = C["ACCENT"] if self.focused else (
            C["ON"] if self.on else C["BORDER"])
        bg = C["CARD_HOVER"] if self.hovered else C["CARD"]
        bg_item = ui.rounded_rect(self, 1, 1, w - 1, h - 1, 16, fill=bg,
                                  outline=border, width=2, tags="row")
        self.tag_lower(bg_item)

        # right cluster: switch, then the On/Off word, then Settings
        sw_x = w - pad_x - ui.SWITCH_W
        ui.draw_switch(self, sw_x, (h - ui.SWITCH_H) / 2, self.on, tags="row")

        label_right = sw_x - ui.s(18, self.scale)
        self.create_text(label_right, h / 2, anchor="e",
                         text="On" if self.on else "Off",
                         font=self.fonts["body_sm_b"],
                         fill=C["ON"] if self.on else C["FG_MUTED"], tags="row")
        label_w = max(ui.s(36, self.scale),
                      self.fonts["body_sm_b"].measure("Off"))
        btn_right = label_right - label_w - ui.s(24, self.scale)
        self.coords(self._btn_window, btn_right, h / 2)

        # left column: name + description
        name_lines, desc_lines = self._lines()
        name_line_h = self.fonts.px("h3") + ui.s(6, self.scale)
        desc_line_h = int(self.fonts.px("body_sm") * 1.4)
        total = (len(name_lines) * name_line_h + ui.s(5, self.scale)
                 + len(desc_lines) * desc_line_h)
        y = (h - total) / 2 + name_line_h / 2
        for line in name_lines:
            self.create_text(pad_x, y, anchor="w", text=line,
                             font=self.fonts["h3_b"], fill=C["FG"], tags="row")
            y += name_line_h
        y += ui.s(5, self.scale) - name_line_h / 2 + desc_line_h / 2
        for line in desc_lines:
            self.create_text(pad_x, y, anchor="w", text=line,
                             font=self.fonts["body_sm"], fill=C["FG_SECOND"],
                             tags="row")
            y += desc_line_h


# ── Settings sheet ────────────────────────────────────────────────────────────
class SettingsSheet(tk.Toplevel):
    """Per-feature settings, moved here out of the feature's own window.

    Writes `features/<id>/settings.json`; a running feature notices the change
    through shared.settings_store.Watcher and re-applies it live.
    """

    WIDTH = 480

    def __init__(self, hub: "Hub", feat: Feature):
        super().__init__(hub)
        self.hub = hub
        self.feat = feat
        self.fonts = hub.fonts
        self.scale = hub.font_scale
        self.settings = store.load(feat.id)
        self._capture_key: str | None = None
        self._shortcut_rows: dict[str, tk.Canvas] = {}
        self._font_cards: list[FontCard] = []
        self._preview: PreviewBox | None = None
        self._size_buttons: list[tuple[str, ui.TextButton]] = []
        self._status_var = tk.StringVar(value="")

        self.title(f"{feat.name} settings")
        self.configure(bg=C["BG"])
        self.transient(hub)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda e: self.close())
        self.bind("<KeyPress>", self._on_key)

        self._width = round(self.WIDTH * max(1.0, self.scale))
        self._build()
        self._place()

    # ── layout ──
    def _build(self):
        pad_x = ui.s(24, self.scale)
        header = tk.Frame(self, bg=C["BG"])
        header.pack(fill="x", padx=pad_x, pady=(ui.s(22, self.scale),
                                                ui.s(18, self.scale)))
        tk.Label(header, text=self.feat.name, font=self.fonts["h4_b"],
                 fg=C["FG"], bg=C["BG"]).pack(side="left")
        tk.Label(header, text="Settings", font=self.fonts["ui"],
                 fg=C["FG_MUTED"], bg=C["BG"]).pack(side="right")
        tk.Frame(self, bg=C["DIVIDER"], height=1).pack(fill="x")

        # a scroll host so a tall sheet (Dyslexia Font) still fits small screens
        outer = tk.Frame(self, bg=C["BG"])
        outer.pack(fill="both", expand=True)
        self._canvas = tk.Canvas(outer, bg=C["BG"], highlightthickness=0, bd=0)
        self._canvas.pack(side="left", fill="both", expand=True)
        self.body = tk.Frame(self._canvas, bg=C["BG"])
        self._body_window = self._canvas.create_window((0, 0), window=self.body,
                                                       anchor="nw")
        self.body.bind("<Configure>", lambda e: self._canvas.configure(
            scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>", lambda e: self._canvas.itemconfig(
            self._body_window, width=e.width))
        self._canvas.bind_all("<MouseWheel>", self._on_wheel)

        inner_w = self._width - pad_x * 2
        gap = ui.s(12, self.scale)

        for key, label in SHEET_OPTIONS.get(self.feat.id, []):
            OptionRow(self.body, self, key, label, inner_w).pack(
                fill="x", padx=pad_x, pady=(gap, 0))

        for key, label, editable in SHEET_SHORTCUTS.get(self.feat.id, []):
            self._shortcut_row(inner_w, key, label, editable, pad_x, gap)

        if self.feat.id == "tone_reader":
            self._text_size_row(inner_w, pad_x, gap)
        if self.feat.id == "dyslexia_font":
            self._dyslexia_controls(inner_w, pad_x, gap)
        if self.feat.id == "colorblind_filter":
            self._colorblind_controls(inner_w, pad_x, gap)
        if self.feat.id == "focus_mode":
            self._focus_controls(inner_w, pad_x, gap)

        ui.TextButton(self.body, self.fonts, "Show me how to use it",
                      lambda: self.hub.open_walkthrough(self.feat.id),
                      role="body_sm", height=ui.s(60, self.scale),
                      width=inner_w, radius=12).pack(
            padx=pad_x, pady=(ui.s(20, self.scale), ui.s(24, self.scale)))

    def _on_wheel(self, event):
        if self._canvas.winfo_exists():
            delta = -1 * (event.delta // 120 if abs(event.delta) >= 120 else event.delta)
            self._canvas.yview_scroll(delta, "units")

    def _place(self):
        self.update_idletasks()
        height = min(self.body.winfo_reqheight() + ui.s(90, self.scale),
                     self.winfo_screenheight() - 120)
        x = self.hub.winfo_x() + self.hub.winfo_width() + 20
        if x + self._width > self.winfo_screenwidth():
            x = max(20, self.hub.winfo_x() + (self.hub.winfo_width() - self._width) // 2)
        y = max(40, self.hub.winfo_y() + 40)
        self.geometry(f"{self._width}x{int(height)}+{x}+{y}")
        self.minsize(self._width, min(400, int(height)))
        self.lift()
        self.focus_force()

    # ── rows ──
    def _shortcut_row(self, width: int, key: str | None, label: str,
                      editable: bool, pad_x: int, gap: int):
        height = max(ui.s(84, self.scale), ui.MIN_TARGET + ui.s(32, self.scale))
        canvas = tk.Canvas(self.body, width=width, height=height, bg=C["BG"],
                           highlightthickness=0, bd=0)
        canvas.pack(fill="x", padx=pad_x, pady=(gap, 0))
        if editable:
            button = ui.TextButton(
                canvas, self.fonts, "Change",
                lambda k=key: self._start_capture(k), role="ui",
                height=ui.s(52, self.scale), h_pad=ui.s(20, self.scale))
            canvas.create_window(width - ui.s(20, self.scale), height / 2,
                                 window=button, anchor="e")
            right_edge = width - ui.s(20, self.scale) - int(button["width"]) \
                - ui.s(14, self.scale)
        else:
            right_edge = width - ui.s(20, self.scale)
        canvas._right_edge = right_edge
        canvas._label = label
        canvas._key = key
        self._shortcut_rows[key or "_static"] = canvas
        self._draw_shortcut(canvas)

    def _draw_shortcut(self, canvas: tk.Canvas):
        canvas.delete("sc")
        width = int(canvas["width"])
        height = int(canvas["height"])
        ui.rounded_rect(canvas, 1, 1, width - 1, height - 1, 14, fill=C["CARD"],
                        outline=C["BORDER"], width=1, tags="sc")
        canvas.create_text(ui.s(20, self.scale), height / 2, anchor="w",
                           text=canvas._label, font=self.fonts["body_sm"],
                           fill=C["FG"], tags="sc")
        key = canvas._key
        if key is None:
            spec = "`"
        elif self._capture_key == key:
            spec = "Press keys…"
        else:
            spec = hk.pretty(self.settings.get("hotkeys", {}).get(key, ""))
        chip_font = self.fonts["body_lg_b"]
        chip_w = chip_font.measure(spec) + ui.s(16, self.scale) * 2
        chip_h = chip_font.metrics("linespace") + ui.s(8, self.scale) * 2
        x2 = canvas._right_edge
        x1 = x2 - chip_w
        y1 = (height - chip_h) / 2
        ui.rounded_rect(canvas, x1, y1, x2, y1 + chip_h, 10, fill=C["CHIP"],
                        outline=C["BORDER_CHIP"], width=1, tags="sc")
        canvas.create_text((x1 + x2) / 2, height / 2, text=spec, font=chip_font,
                           fill=C["FG"], tags="sc")

    def _text_size_row(self, width: int, pad_x: int, gap: int):
        tk.Label(self.body, text="Text size in the answer card",
                 font=self.fonts["body_sm"], fg=C["FG_SECOND"], bg=C["BG"],
                 anchor="w").pack(fill="x", padx=pad_x,
                                  pady=(ui.s(18, self.scale), ui.s(8, self.scale)))
        row = tk.Frame(self.body, bg=C["BG"])
        row.pack(fill="x", padx=pad_x)
        self._size_buttons = []
        for value, label in TONE_SIZE_LABELS:
            btn = ui.TextButton(
                row, self.fonts, label, lambda v=value: self._set_text_size(v),
                role="ui", height=ui.s(60, self.scale),
                width=(width - ui.s(24, self.scale)) // 3,
                primary=self.settings.get("text_size") == value)
            btn.pack(side="left", padx=(0, ui.s(12, self.scale)))
            self._size_buttons.append((value, btn))

    def _set_text_size(self, value: str):
        self.set_value("text_size", value)
        for val, btn in self._size_buttons:
            btn.primary = (val == value)
            btn.role = "ui_b" if btn.primary else "ui"
            btn.redraw()

    def _dyslexia_controls(self, width: int, pad_x: int, gap: int):
        tk.Label(self.body, text="Pick a font", font=self.fonts["body_sm"],
                 fg=C["FG_SECOND"], bg=C["BG"], anchor="w").pack(
            fill="x", padx=pad_x, pady=(ui.s(20, self.scale), ui.s(10, self.scale)))
        grid = tk.Frame(self.body, bg=C["BG"])
        grid.pack(fill="x", padx=pad_x)
        card_w = (width - ui.s(12, self.scale)) // 2
        self._font_cards = []
        for i, name in enumerate(winfonts.FONT_CHOICES):
            card = FontCard(grid, self, name, winfonts.FONT_NOTES.get(name, ""),
                            card_w)
            card.grid(row=i // 2, column=i % 2, padx=(0, ui.s(12, self.scale)),
                      pady=(0, ui.s(12, self.scale)), sticky="w")
            self._font_cards.append(card)

        self._stepper_row(width, pad_x, "Space between letters", "letter_spacing",
                          0.01, 0.0, 0.12, lambda v: f"{v:.2f} em")
        self._stepper_row(width, pad_x, "Space between lines", "line_height",
                          0.05, 1.0, 2.2, lambda v: f"{v:.2f}×")

        tk.Label(self.body, text="Preview", font=self.fonts["ui_sm"],
                 fg=C["FG_MUTED"], bg=C["BG"], anchor="w").pack(
            fill="x", padx=pad_x, pady=(ui.s(20, self.scale), ui.s(10, self.scale)))
        self._preview = PreviewBox(self.body, self, width)
        self._preview.pack(padx=pad_x)

        tk.Label(self.body, textvariable=self._status_var,
                 font=self.fonts["ui_sm"], fg=C["WARM_TEXT"], bg=C["BG"],
                 anchor="w", justify="left",
                 wraplength=width).pack(fill="x", padx=pad_x,
                                        pady=(ui.s(18, self.scale), 0))
        self._refresh_font_status()

        chrome_row = tk.Frame(self.body, bg=C["BG"])
        chrome_row.pack(fill="x", padx=pad_x, pady=(ui.s(12, self.scale), 0))
        ui.TextButton(chrome_row, self.fonts, "Open the extension folder",
                      self._open_extension_folder, role="ui",
                      height=ui.s(60, self.scale), width=width).pack(fill="x")
        ui.TextButton(chrome_row, self.fonts, "Open Chrome extensions",
                      self._open_chrome_extensions, role="ui",
                      height=ui.s(60, self.scale), width=width).pack(
            fill="x", pady=(ui.s(12, self.scale), 0))

        win_row = tk.Frame(self.body, bg=C["BG"])
        win_row.pack(fill="x", padx=pad_x, pady=(ui.s(12, self.scale), 0))
        self._apply_btn = ui.TextButton(
            win_row, self.fonts, "Use this font in Windows apps",
            self._apply_windows, role="ui", height=ui.s(60, self.scale),
            width=width, disabled=not winfonts.is_font_installed(
                self.settings.get("font_family", "")))
        self._apply_btn.pack(fill="x")
        ui.TextButton(win_row, self.fonts, "Put the Windows fonts back",
                      self._restore_windows, role="ui",
                      height=ui.s(60, self.scale), width=width).pack(
            fill="x", pady=(ui.s(12, self.scale), 0))

        ui.TextButton(self.body, self.fonts, "Take screening test",
                      lambda: ScreeningDialog(self.hub), role="ui",
                      height=ui.s(60, self.scale), width=width).pack(
            padx=pad_x, pady=(ui.s(12, self.scale), 0))

    def _colorblind_controls(self, width: int, pad_x: int, gap: int):
        tk.Label(self.body, text="Filter type", font=self.fonts["body_sm"],
                 fg=C["FG_SECOND"], bg=C["BG"], anchor="w").pack(
            fill="x", padx=pad_x, pady=(ui.s(18, self.scale), ui.s(8, self.scale)))
        btn_w = (width - ui.s(12, self.scale) * 2) // 3
        self._filter_buttons: list[tuple[str, ui.TextButton]] = []
        for row_start in (0, 3):
            row = tk.Frame(self.body, bg=C["BG"])
            row.pack(fill="x", padx=pad_x, pady=(0, ui.s(12, self.scale)))
            for value, label in FILTER_TYPE_LABELS[row_start:row_start + 3]:
                btn = ui.TextButton(
                    row, self.fonts, label, lambda v=value: self._set_filter_type(v),
                    role="ui", height=ui.s(60, self.scale), width=btn_w,
                    primary=self.settings.get("filter_name") == value)
                btn.pack(side="left", padx=(0, ui.s(12, self.scale)))
                self._filter_buttons.append((value, btn))
        tk.Label(self.body, textvariable=self._status_var,
                 font=self.fonts["ui_sm"], fg=C["WARM_TEXT"], bg=C["BG"],
                 anchor="w", justify="left", wraplength=width).pack(
            fill="x", padx=pad_x, pady=(ui.s(6, self.scale), 0))
        self._apply_colorblind_filter()

    def _set_filter_type(self, value: str):
        self.set_value("filter_name", value)
        for val, btn in self._filter_buttons:
            btn.primary = (val == value)
            btn.role = "ui_b" if btn.primary else "ui"
            btn.redraw()
        self._apply_colorblind_filter()

    def _apply_colorblind_filter(self):
        try:
            cf = _load_feature_module("colorblind_filter")
            cf.set_filter(bool(self.settings.get("enabled", False)),
                          cf.FILTER_TYPES[self.settings.get("filter_name", "Deuteranopia")])
            self._status_var.set(
                "Applied. If nothing changes, lock and unlock (Win+L)."
                if self.settings.get("enabled") else "Filter off.")
        except Exception as e:
            self._status_var.set(str(e))

    def _focus_controls(self, width: int, pad_x: int, gap: int):
        # The "Blocking now" switch itself is a plain OptionRow, added via
        # SHEET_OPTIONS like every other feature's toggles — the running
        # focus_mode bubble watches settings.json (shared.settings_store.Watcher,
        # same mechanism voice_control's always_on/dictation_mode use) and owns
        # the countdown/auto-unblock timer on its own Tk mainloop, since only it
        # stays alive for the whole session regardless of whether this sheet is
        # open.
        tk.Label(self.body, text="Blocked sites (one per line)",
                 font=self.fonts["body_sm"], fg=C["FG_SECOND"], bg=C["BG"],
                 anchor="w").pack(fill="x", padx=pad_x,
                                  pady=(ui.s(18, self.scale), ui.s(8, self.scale)))
        # A rounded card (matching every other row's radius-14 card) with the
        # Text widget embedded inside, rather than a bare square box.
        text_h = ui.s(110, self.scale)
        inset = ui.s(14, self.scale)
        card_h = text_h + inset * 2
        card = tk.Canvas(self.body, width=width, height=card_h, bg=C["BG"],
                         highlightthickness=0, bd=0)
        card.pack(fill="x", padx=pad_x)
        ui.rounded_rect(card, 1, 1, width - 1, card_h - 1, 14,
                        fill=C["CARD"], outline=C["BORDER"], width=1)
        self._blocklist_text = tk.Text(
            card, bg=C["CARD"], fg=C["FG"], insertbackground=C["FG"],
            relief="flat", bd=0, highlightthickness=0, wrap="word",
            font=self.fonts["body_sm"])
        card.create_window(inset, inset, anchor="nw", window=self._blocklist_text,
                           width=width - inset * 2, height=text_h)
        self._blocklist_text.insert("1.0", "\n".join(self.settings.get("blocklist", [])))
        self._blocklist_text.bind("<FocusOut>", lambda e: self._save_blocklist())

        self._stepper_row(width, pad_x, "Session length", "duration_minutes",
                          5, 5, 480, lambda v: f"{int(v)} min")

        self._focus_status_var = tk.StringVar(value="")
        tk.Label(self.body, textvariable=self._focus_status_var,
                 font=self.fonts["ui_sm"], fg=C["WARM_TEXT"], bg=C["BG"],
                 anchor="w", justify="left", wraplength=width).pack(
            fill="x", padx=pad_x, pady=(ui.s(6, self.scale), 0))
        try:
            fm = _load_feature_module("focus_mode")
            if not fm.is_admin():
                self._focus_status_var.set(
                    "Run the hub as Administrator, or blocking will fail.")
        except Exception as e:
            self._focus_status_var.set(str(e))

    def _save_blocklist(self):
        domains = [d.strip() for d in
                  self._blocklist_text.get("1.0", "end").splitlines() if d.strip()]
        self.set_value("blocklist", domains)

    def _stepper_row(self, width: int, pad_x: int, label: str, key: str,
                     step: float, low: float, high: float, fmt):
        height = max(ui.s(88, self.scale), ui.s(60, self.scale) + ui.s(28, self.scale))
        canvas = tk.Canvas(self.body, width=width, height=height, bg=C["BG"],
                           highlightthickness=0, bd=0)
        canvas.pack(fill="x", padx=pad_x, pady=(ui.s(12, self.scale), 0))
        size = ui.s(60, self.scale)

        def nudge(delta):
            value = round(min(high, max(low, float(self.settings.get(key, low))
                                        + delta)), 2)
            self.set_value(key, value)
            draw()
            if self._preview:
                self._preview.redraw()

        minus = IconButton(canvas, "minus", lambda: nudge(-step), size=size)
        plus = IconButton(canvas, "plus", lambda: nudge(step), size=size)
        canvas.create_window(width - ui.s(18, self.scale), height / 2,
                             window=plus, anchor="e")
        canvas.create_window(width - ui.s(18, self.scale) - size - ui.s(16, self.scale),
                             height / 2, window=minus, anchor="e")

        def draw():
            canvas.delete("st")
            ui.rounded_rect(canvas, 1, 1, width - 1, height - 1, 14,
                            fill=C["CARD"], outline=C["BORDER"], width=1,
                            tags="st")
            canvas.tag_lower("st")
            text_w = max(80, width - ui.s(18, self.scale) * 2 - size * 2
                         - ui.s(16, self.scale) * 2)
            canvas.create_text(ui.s(18, self.scale), height / 2 - ui.s(11, self.scale),
                               anchor="w",
                               text=ui.ellipsize(self.fonts["body_b"], label, text_w),
                               font=self.fonts["body_b"], fill=C["FG"], tags="st")
            canvas.create_text(ui.s(18, self.scale), height / 2 + ui.s(14, self.scale),
                               anchor="w", text=fmt(float(self.settings.get(key, low))),
                               font=self.fonts["ui_sm"], fill=C["FG_SECOND"],
                               tags="st")

        draw()

    # ── behaviour ──
    def set_value(self, key: str, value):
        self.settings[key] = value
        store.save(self.feat.id, self.settings)
        if key == "font_family":
            self._refresh_font_status()
        if self.feat.id == "colorblind_filter" and key == "enabled":
            self._apply_colorblind_filter()

    def refresh_font_cards(self):
        for card in self._font_cards:
            card.redraw()
        if self._preview:
            self._preview.redraw()

    def _refresh_font_status(self):
        font_name = self.settings.get("font_family", "")
        if not font_name:
            return
        self._status_var.set(winfonts.font_status_text(font_name))
        if getattr(self, "_apply_btn", None):
            self._apply_btn.set_disabled(not winfonts.is_font_installed(font_name))

    def _start_capture(self, key: str):
        self._capture_key = key
        self._draw_shortcut(self._shortcut_rows[key])
        self.focus_force()

    def _on_key(self, event):
        if not self._capture_key:
            return
        combo = hk.format_from_tk_event(event)
        if not combo:
            return
        key = self._capture_key
        hotkeys = dict(self.settings.get("hotkeys", {}))
        clash = [k for k, v in hotkeys.items()
                 if k != key and v.lower() == combo.lower()]
        self._capture_key = None
        if clash:
            self._status_var.set("That key is already used by another action.")
        else:
            hotkeys[key] = combo
            self.settings["hotkeys"] = hotkeys
            store.save(self.feat.id, self.settings)
        self._draw_shortcut(self._shortcut_rows[key])
        return "break"

    def _open_extension_folder(self):
        try:
            winfonts.open_extension_folder()
        except Exception as e:
            self._status_var.set(f"Could not open the extension folder: {e}")

    def _open_chrome_extensions(self):
        try:
            winfonts.open_chrome_extensions()
        except Exception as e:
            self._status_var.set(f"Could not open Chrome extensions: {e}")

    def _apply_windows(self):
        try:
            winfonts.apply_windows_substitution(
                self.settings.get("font_family", ""),
                list(self.settings.get("windows_targets",
                                       winfonts.SUBSTITUTION_TARGETS)))
            self._status_var.set("Windows apps will use this font. Sign out and "
                                 "back in if some apps have not changed.")
        except Exception as e:
            self._status_var.set(str(e))

    def _restore_windows(self):
        try:
            winfonts.restore_windows_substitution()
            self._status_var.set("The Windows fonts were put back.")
        except Exception as e:
            self._status_var.set(str(e))

    def close(self):
        try:
            self._canvas.unbind_all("<MouseWheel>")
        except Exception:
            pass
        self.hub.forget_sheet(self.feat.id)
        self.destroy()


# ── Dyslexia screening test ─────────────────────────────────────────────────
# NOT a diagnostic tool. It cannot identify dyslexia. It only notices a few
# patterns sometimes associated with reading differences, so the disclaimer is
# shown before the quiz starts and again with the result. Self-contained —
# no dependency on the dyslexia_font feature process, so it lives here.
import random as _random   # noqa: E402
import time as _time       # noqa: E402

SCREENING_DISCLAIMER = (
    "Not a diagnosis — it can't identify dyslexia, only a few related patterns. "
    "If reading is a concern, talk to a specialist such as an educational psychologist."
)

SCREENING_QUESTIONS = [
    {"prompt": "Which one matches the target letter?\n\nTarget: b",
     "options": ["d", "b", "p", "q"], "correct": 1},
    {"prompt": "Which one matches the target letter?\n\nTarget: p",
     "options": ["q", "d", "p", "b"], "correct": 2},
    {"prompt": "Which word is spelled the same forwards and as shown?\n\nTarget: was",
     "options": ["saw", "was", "sae", "aws"], "correct": 1},
    {"prompt": "Which word does NOT rhyme with the others?",
     "options": ["cat", "hat", "dog", "bat"], "correct": 2},
    {"prompt": "Which word does NOT rhyme with the others?",
     "options": ["light", "night", "sight", "bench"], "correct": 3},
    {"prompt": "Put in order: which comes first alphabetically?",
     "options": ["dog", "cat", "ant", "elk"], "correct": 2},
]

SCREENING_PASSAGE = (
    "The quick brown fox jumps over the lazy dog. Reading every day helps build "
    "stronger word recognition and comprehension over time."
)
SCREENING_TYPICAL_WPM = 200  # rough, non-clinical adult reference


class ScreeningDialog(tk.Toplevel):
    """Short non-diagnostic quiz (letter reversal, rhyme, reading speed)."""

    WIDTH = 480

    def __init__(self, hub: "Hub"):
        super().__init__(hub)
        self.hub = hub
        self.fonts = hub.fonts
        self.scale = hub.font_scale
        self.title("Dyslexia Screening")
        self.configure(bg=C["BG"])
        self.transient(hub)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda e: self.destroy())

        self._questions = _random.sample(SCREENING_QUESTIONS, len(SCREENING_QUESTIONS))
        self._idx = 0
        self._flags = 0
        self._reading_start = 0.0

        self._width = round(self.WIDTH * max(1.0, self.scale))
        self.body = tk.Frame(self, bg=C["BG"])
        self.body.pack(fill="both", expand=True)
        self._show_intro()
        self._place()

    def _place(self):
        self.update_idletasks()
        height = self.body.winfo_reqheight() + ui.s(60, self.scale)
        x = self.hub.winfo_x() + (self.hub.winfo_width() - self._width) // 2
        y = max(40, self.hub.winfo_y() + 40)
        self.geometry(f"{self._width}x{int(height)}+{x}+{y}")
        self.lift()
        self.focus_force()

    def _clear(self):
        for child in self.body.winfo_children():
            child.destroy()

    def _title(self, text: str):
        tk.Label(self.body, text=text, font=self.fonts["h4_b"], fg=C["FG"], bg=C["BG"],
                 wraplength=self._width - ui.s(40, self.scale), justify="left").pack(
            anchor="w", padx=ui.s(20, self.scale), pady=(ui.s(20, self.scale), ui.s(8, self.scale)))

    def _disclaimer_label(self):
        tk.Label(self.body, text=SCREENING_DISCLAIMER, font=self.fonts["ui_sm"],
                 fg=C["WARM_TEXT"], bg=C["BG"],
                 wraplength=self._width - ui.s(40, self.scale), justify="left").pack(
            anchor="w", padx=ui.s(20, self.scale), pady=(0, ui.s(12, self.scale)))

    def _show_intro(self):
        self._clear()
        self._title("Dyslexia Screening")
        self._disclaimer_label()
        tk.Label(self.body,
                 text=f"{len(self._questions)} quick questions, then a short timed reading task.",
                 font=self.fonts["body_sm"], fg=C["FG_SECOND"], bg=C["BG"],
                 wraplength=self._width - ui.s(40, self.scale), justify="left").pack(
            anchor="w", padx=ui.s(20, self.scale), pady=(0, ui.s(20, self.scale)))
        ui.TextButton(self.body, self.fonts, "Start", self._show_question,
                      role="ui", primary=True, height=ui.s(60, self.scale)).pack(
            anchor="w", padx=ui.s(20, self.scale), pady=(0, ui.s(20, self.scale)))
        self._place()

    def _show_question(self):
        if self._idx >= len(self._questions):
            self._show_reading_task()
            return
        self._clear()
        q = self._questions[self._idx]
        tk.Label(self.body, text=f"Question {self._idx + 1} of {len(self._questions)}",
                 font=self.fonts["ui_sm"], fg=C["FG_MUTED"], bg=C["BG"]).pack(
            anchor="w", padx=ui.s(20, self.scale), pady=(ui.s(20, self.scale), ui.s(6, self.scale)))
        tk.Label(self.body, text=q["prompt"], font=self.fonts["body_b"], fg=C["FG"], bg=C["BG"],
                 wraplength=self._width - ui.s(40, self.scale), justify="left").pack(
            anchor="w", padx=ui.s(20, self.scale), pady=(0, ui.s(16, self.scale)))

        options = list(enumerate(q["options"]))
        _random.shuffle(options)
        btn_w = self._width - ui.s(40, self.scale)
        for orig_idx, text in options:
            ui.TextButton(self.body, self.fonts, text,
                          lambda oi=orig_idx: self._answer(q, oi),
                          role="ui", width=btn_w, height=ui.s(56, self.scale)).pack(
                padx=ui.s(20, self.scale), pady=(0, ui.s(8, self.scale)))
        tk.Frame(self.body, bg=C["BG"], height=ui.s(12, self.scale)).pack()
        self._place()

    def _answer(self, q: dict, chosen_idx: int):
        if chosen_idx != q["correct"]:
            self._flags += 1
        self._idx += 1
        self._show_question()

    def _show_reading_task(self):
        self._clear()
        self._title("Reading speed")
        tk.Label(self.body, text="Read the passage, then press Done.",
                 font=self.fonts["ui_sm"], fg=C["FG_MUTED"], bg=C["BG"],
                 wraplength=self._width - ui.s(40, self.scale), justify="left").pack(
            anchor="w", padx=ui.s(20, self.scale), pady=(0, ui.s(10, self.scale)))
        # A rounded card (matching every other card's radius-14 look) with the
        # passage text embedded inside, rather than a bare square box.
        card_w = self._width - ui.s(40, self.scale)
        pad = ui.s(14, self.scale)
        label = tk.Label(self.body, text=SCREENING_PASSAGE, font=self.fonts["body"],
                         fg=C["FG"], bg=C["CARD"], wraplength=card_w - pad * 2, justify="left")
        label.update_idletasks()
        card_h = label.winfo_reqheight() + pad * 2
        card = tk.Canvas(self.body, width=card_w, height=card_h, bg=C["BG"],
                         highlightthickness=0, bd=0)
        card.pack(padx=ui.s(20, self.scale))
        ui.rounded_rect(card, 1, 1, card_w - 1, card_h - 1, 14,
                        fill=C["CARD"], outline=C["BORDER"], width=1)
        card.create_window(pad, pad, anchor="nw", window=label)
        self._reading_start = _time.perf_counter()
        ui.TextButton(self.body, self.fonts, "Done reading", self._finish_reading,
                      role="ui", primary=True, height=ui.s(60, self.scale)).pack(
            anchor="w", padx=ui.s(20, self.scale), pady=(ui.s(16, self.scale), ui.s(20, self.scale)))
        self._place()

    def _finish_reading(self):
        elapsed = max(0.5, _time.perf_counter() - self._reading_start)
        word_count = len(SCREENING_PASSAGE.split())
        wpm = word_count / (elapsed / 60)
        if wpm < SCREENING_TYPICAL_WPM * 0.5:
            self._flags += 1
        self._show_result(wpm)

    def _show_result(self, wpm: float):
        self._clear()
        if self._flags <= 1:
            color, msg = C["ON"], "Few patterns showed up in your answers."
        elif self._flags <= 3:
            color, msg = C["WARM_TEXT"], "Some patterns showed up in your answers."
        else:
            color, msg = C["STOP_BORDER"], "Several patterns showed up in your answers."

        self._title("Result")
        tk.Label(self.body, text=f"{msg} (reading speed: {wpm:.0f} words/min)",
                 font=self.fonts["body_b"], fg=color, bg=C["BG"],
                 wraplength=self._width - ui.s(40, self.scale), justify="left").pack(
            anchor="w", padx=ui.s(20, self.scale), pady=(0, ui.s(12, self.scale)))
        self._disclaimer_label()
        ui.TextButton(self.body, self.fonts, "Try again", self._restart,
                      role="ui", height=ui.s(52, self.scale)).pack(
            anchor="w", padx=ui.s(20, self.scale), pady=(0, ui.s(20, self.scale)))
        self._place()

    def _restart(self):
        self._questions = _random.sample(SCREENING_QUESTIONS, len(SCREENING_QUESTIONS))
        self._idx = 0
        self._flags = 0
        self._show_intro()


# ── Walkthrough ───────────────────────────────────────────────────────────────
class Walkthrough(tk.Frame):
    """Three-step instructions, shown as a modal card over the hub."""

    CARD_W = 560

    def __init__(self, hub: "Hub", feature_id: str):
        super().__init__(hub, bg=C["SCRIM"])
        self.hub = hub
        self.fonts = hub.fonts
        self.scale = hub.font_scale
        self.steps = WALKTHROUGHS.get(feature_id) or [
            (hub.feature_name(feature_id), "No instructions yet for this feature.")]
        self.index = 0

        self.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.card_w = min(round(self.CARD_W * max(1.0, self.scale)),
                          hub.winfo_width() - ui.s(56, self.scale))
        self.canvas = tk.Canvas(self, bg=C["SCRIM"], highlightthickness=0, bd=0,
                                width=self.card_w, height=10)
        self.canvas.place(relx=0.5, rely=0.5, anchor="center")

        self.back_btn = ui.TextButton(
            self.canvas, self.fonts, "Back", self.back, role="body_sm",
            height=ui.s(60, self.scale), h_pad=ui.s(24, self.scale))
        self.next_btn = ui.TextButton(
            self.canvas, self.fonts, "Next", self.next, role="body_lg",
            height=ui.s(60, self.scale), primary=True, width=10)
        self.canvas.create_window(0, 0, window=self.back_btn, anchor="nw",
                                  tags="back")
        self.canvas.create_window(0, 0, window=self.next_btn, anchor="nw",
                                  tags="next")

        hub.bind("<Escape>", lambda e: self.close())
        self.redraw()
        self.next_btn.focus_set()

    def redraw(self):
        self.canvas.delete("card")
        pad = ui.s(32, self.scale)
        inner = self.card_w - pad * 2
        title, body = self.steps[self.index]

        count_h = self.fonts.px("caption") + ui.s(10, self.scale)
        title_lines = ui.wrap_lines(self.fonts["h2_b"], title, inner)
        title_line_h = self.fonts.px("h2") + ui.s(8, self.scale)
        body_lines = ui.wrap_lines(self.fonts["body_lg"], body, inner)
        body_line_h = int(self.fonts.px("body_lg") * 1.5)
        gap = ui.s(20, self.scale)
        dots_h = ui.s(10, self.scale)
        btn_h = ui.s(60, self.scale)

        height = (pad * 2 + count_h + gap + len(title_lines) * title_line_h
                  + gap + len(body_lines) * body_line_h + gap + dots_h
                  + gap + btn_h)
        self.canvas.configure(height=height)

        ui.rounded_rect(self.canvas, 1, 1, self.card_w - 1, height - 1, 20,
                        fill=C["CARD"], outline=C["BORDER_CTRL"], width=1,
                        tags="card")
        y = pad
        self.canvas.create_text(pad, y, anchor="nw",
                                text=f"Step {self.index + 1} of {len(self.steps)}",
                                font=self.fonts["caption"], fill=C["FG_MUTED"],
                                tags="card")
        y += count_h + gap
        for line in title_lines:
            self.canvas.create_text(pad, y, anchor="nw", text=line,
                                    font=self.fonts["h2_b"], fill=C["FG"],
                                    tags="card")
            y += title_line_h
        y += gap
        for line in body_lines:
            self.canvas.create_text(pad, y, anchor="nw", text=line,
                                    font=self.fonts["body_lg"],
                                    fill=C["FG_BUBBLE"], tags="card")
            y += body_line_h
        y += gap
        dot = ui.s(10, self.scale)
        for i in range(len(self.steps)):
            x = pad + i * (dot + ui.s(8, self.scale))
            self.canvas.create_oval(x, y, x + dot, y + dot, outline="",
                                    fill=C["ACCENT"] if i == self.index
                                    else C["BORDER_CHIP"], tags="card")
        y += dots_h + gap

        last = self.index == len(self.steps) - 1
        self.back_btn.disabled = self.index == 0
        self.back_btn.redraw()
        self.next_btn.set_text("Got it" if last else "Next")
        back_w = int(self.back_btn["width"])
        self.next_btn.configure(width=inner - back_w - ui.s(12, self.scale))
        self.next_btn.redraw()
        self.canvas.coords("back", pad, y)
        self.canvas.coords("next", pad + back_w + ui.s(12, self.scale), y)

    def back(self):
        if self.index > 0:
            self.index -= 1
            self.redraw()

    def next(self):
        if self.index < len(self.steps) - 1:
            self.index += 1
            self.redraw()
        else:
            self.close()

    def close(self):
        self.hub.unbind("<Escape>")
        self.hub.forget_walkthrough()
        self.destroy()


# ── Hub window ────────────────────────────────────────────────────────────────
class Hub(tk.Tk):
    POLL_MS = 1000                       # how often we check feature liveness

    def __init__(self):
        super().__init__()
        state = load_state()
        self._procs: dict[str, subprocess.Popen] = {}
        self._enabled: set[str] = set(state.get("enabled", []))
        self._seen: set[str] = set(state.get("seen_walkthrough", []))
        self.font_scale = float(state.get("font_scale", 1.0))
        self.font_scale = min(ui.FONT_SCALE_MAX, max(ui.FONT_SCALE_MIN,
                                                     self.font_scale))
        self._rows: dict[str, FeatureRow] = {}
        self._features: list[Feature] = []
        self._sheets: dict[str, SettingsSheet] = {}
        self._walkthrough: Walkthrough | None = None
        self._notice = ""                # crash line shown in the footer
        self.fonts = ui.FontSet(self.font_scale)

        self.title("Accessibility4all")
        self.configure(bg=C["BG"])
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._full_rebuild()
        self._restore_enabled()
        self.after(self.POLL_MS, self._poll)

    # ── geometry ──
    def _window_width(self) -> int:
        """The window grows with the text size — rows must never be clipped."""
        wanted = round(WINDOW_W * self.font_scale)
        return min(wanted, self.winfo_screenwidth() - 80)

    def _fit_height(self):
        self.update_idletasks()
        height = max(WINDOW_MIN_H, self.winfo_reqheight())
        height = min(height, self.winfo_screenheight() - 100)
        self.geometry(f"{self._window_width()}x{height}")
        self.minsize(self._window_width(), min(WINDOW_MIN_H, height))

    # ── build ──
    def _full_rebuild(self):
        if self._walkthrough:
            self._walkthrough.destroy()
            self._walkthrough = None
        for child in self.winfo_children():
            if isinstance(child, tk.Toplevel):
                continue
            child.destroy()
        self._build_chrome()
        self._rebuild_features()
        self._fit_height()

    def _build_chrome(self):
        pad_x = ui.s(44, self.font_scale)

        header = tk.Frame(self, bg=C["BG"])
        header.pack(fill="x", padx=pad_x, pady=(ui.s(36, self.font_scale),
                                                ui.s(24, self.font_scale)))
        tk.Label(header, text="Accessibility4all", font=self.fonts["display_b"],
                 fg=C["FG"], bg=C["BG"], anchor="w").pack(fill="x")
        tk.Label(header, text="Turn on the help you need.",
                 font=self.fonts["body"], fg=C["FG_SECOND"], bg=C["BG"],
                 anchor="w").pack(fill="x", pady=(ui.s(8, self.font_scale), 0))

        self._list = tk.Frame(self, bg=C["BG"])
        self._list.pack(fill="both", expand=True, padx=pad_x,
                        pady=(0, ui.s(8, self.font_scale)))

        tk.Frame(self, bg=C["DIVIDER"], height=1).pack(fill="x")

        footer = tk.Frame(self, bg=C["BG"])
        footer.pack(fill="x", padx=pad_x, pady=(ui.s(18, self.font_scale),
                                                ui.s(22, self.font_scale)))
        self._hint = tk.Label(footer, text="", font=self.fonts["ui_sm"],
                              fg=C["FG_MUTED"], bg=C["BG"], anchor="w",
                              justify="left")
        self._hint.pack(side="left")

        controls = tk.Frame(footer, bg=C["BG"])
        controls.pack(side="right")
        ui.TextButton(controls, self.fonts, "A+",
                      lambda: self._adjust_font_scale(ui.FONT_SCALE_STEP),
                      role="h4", height=ui.s(46, self.font_scale),
                      width=ui.s(52, self.font_scale), fill=C["CARD"]).pack(
            side="right")
        ui.TextButton(controls, self.fonts, "A−",
                      lambda: self._adjust_font_scale(-ui.FONT_SCALE_STEP),
                      role="body_sm", height=ui.s(46, self.font_scale),
                      width=ui.s(52, self.font_scale), fill=C["CARD"]).pack(
            side="right", padx=(0, ui.s(10, self.font_scale)))
        tk.Label(controls, text="Text size", font=self.fonts["ui_sm"],
                 fg=C["FG_MUTED"], bg=C["BG"]).pack(
            side="right", padx=(0, ui.s(10, self.font_scale)))

    def _rebuild_features(self):
        for child in self._list.winfo_children():
            child.destroy()
        self._rows.clear()
        self._features = discover_features()

        if not self._features:
            tk.Label(self._list,
                     text="No features found.\nAdd a folder under ./features/ "
                          "(see features/README.md).",
                     font=self.fonts["body_sm"], fg=C["FG_SECOND"], bg=C["BG"],
                     justify="left").pack(anchor="w", pady=ui.s(20, self.font_scale))
            self._update_hint()
            return

        row_w = self._window_width() - ui.s(44, self.font_scale) * 2
        for feat in self._features:
            row = FeatureRow(self._list, self, feat, row_w)
            row.pack(fill="x", pady=(0, ui.s(14, self.font_scale)))
            self._rows[feat.id] = row
            running = feat.id in self._procs and self._procs[feat.id].poll() is None
            row.set_on(running)
        self._update_hint()

    def feature_name(self, feature_id: str) -> str:
        for feat in self._features:
            if feat.id == feature_id:
                return feat.name
        return feature_id

    # ── toggle / process control ──
    def toggle(self, feat: Feature):
        running = feat.id in self._procs and self._procs[feat.id].poll() is None
        if running:
            self._stop(feat)
            self._enabled.discard(feat.id)
        else:
            self._notice = ""
            self._start(feat)
            self._enabled.add(feat.id)
            if feat.id not in self._seen:
                self._seen.add(feat.id)
                self.open_walkthrough(feat.id)
        self._persist()

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
            self._set_row(feat.id, True)
        except Exception as e:
            safe_print(f"[hub] failed to start '{feat.name}': {e}", flush=True)
            self._notice = f"{feat.name} could not start."
            self._set_row(feat.id, False)

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
        self._set_row(feat.id, False)

    def _restore_enabled(self):
        """Auto-start features that were on last time (if they still exist)."""
        known = {f.id for f in self._features}
        for feat in self._features:
            if feat.id in self._enabled and feat.id in known:
                self._start(feat)
        self._enabled &= known
        self._persist()

    def _persist(self):
        save_state(self._enabled, self.font_scale, self._seen)
        self._update_hint()

    # ── liveness polling ──
    def _poll(self):
        for feat in self._features:
            proc = self._procs.get(feat.id)
            if proc is not None and proc.poll() is not None:
                code = proc.returncode
                self._procs.pop(feat.id, None)
                self._enabled.discard(feat.id)
                self._set_row(feat.id, False)
                if code != 0:
                    # No status glyph any more — the row flips to Off and the
                    # footer explains it in one plain line.
                    self._notice = f"{feat.name} stopped unexpectedly."
                    safe_print(f"[hub] '{feat.name}' exited with code {code}",
                               flush=True)
                self._persist()
        self.after(self.POLL_MS, self._poll)

    def _set_row(self, feat_id: str, on: bool):
        row = self._rows.get(feat_id)
        if row:
            row.set_on(on)
        self._update_hint()

    def _update_hint(self):
        if not self._features:
            self._hint.configure(text="")
            return
        running = sum(1 for f in self._features
                      if f.id in self._procs and self._procs[f.id].poll() is None)
        text = f"{running} of {len(self._features)} features on"
        if self._notice:
            text = f"{text}   ·   {self._notice}"
        self._hint.configure(text=text)

    # ── settings + walkthrough ──
    def open_settings(self, feat: Feature):
        sheet = self._sheets.get(feat.id)
        if sheet and sheet.winfo_exists():
            sheet.lift()
            sheet.focus_force()
            return
        self._sheets[feat.id] = SettingsSheet(self, feat)

    def forget_sheet(self, feature_id: str):
        self._sheets.pop(feature_id, None)

    def open_walkthrough(self, feature_id: str):
        if self._walkthrough:
            self._walkthrough.destroy()
        self.lift()
        self._walkthrough = Walkthrough(self, feature_id)
        self._seen.add(feature_id)
        save_state(self._enabled, self.font_scale, self._seen)

    def forget_walkthrough(self):
        self._walkthrough = None

    # ── text size ──
    def _adjust_font_scale(self, delta: float):
        scale = round(min(ui.FONT_SCALE_MAX,
                          max(ui.FONT_SCALE_MIN, self.font_scale + delta)), 2)
        if scale == self.font_scale:
            return
        self.font_scale = scale
        self.fonts.set_scale(scale)
        # button widths, wrapped text and row heights all depend on font
        # metrics, so rebuilding is the simplest way to keep them in sync — and
        # the window is resized afterwards so nothing is clipped.
        self._full_rebuild()
        for feat in self._features:
            running = feat.id in self._procs and self._procs[feat.id].poll() is None
            self._set_row(feat.id, running)
        self._persist()

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
