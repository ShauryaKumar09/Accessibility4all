"""Tone & Social Cue Identification — read the social subtext of a piece of text.

An assistive mode aimed at autistic users (and anyone who finds implied/non-literal
meaning hard to parse). When ON, the user picks some text two ways:

  * Highlight text in ANY app and press a global hotkey (default Ctrl+Shift+Y). We
    copy the selection and analyze it.
  * Turn on "Shift+Click to analyze in Chrome" and Shift+Click a paragraph. We OCR
    that block and analyze it. Plain clicks are left untouched.

The text is sent to Groq, which returns a calibrated, hedged read of the tone and
any social cues (sarcasm, urgency, indirect requests, passive aggression, politeness
softeners, hidden emotion). The result is shown in a panel — it never fabricates
subtext and flags uncertainty rather than over-claiming.

Runs as its own process when toggled ON in the hub. See features/README.md. Mirrors
the structure of features/page_reader/main.py.

THREAD-SAFETY (critical, see CLAUDE.md): tkinter is not thread-safe and touching a
widget from a non-main thread segfaults on macOS. The pynput keyboard/mouse listener
callbacks run on their own threads and must ONLY mutate plain Python state; every UI
update or worker dispatch is marshalled onto the main thread via self.after(0, ...).
We also run at most one keyboard listener + one mouse listener at a time (stacking
multiple keyboard event taps is what crashed earlier versions on macOS).
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

import pyautogui
import pyperclip
import tkinter as tk
from dotenv import load_dotenv
from groq import Groq
from pynput import keyboard, mouse
from tkinter import font as tkfont

FEATURE_DIR = Path(__file__).resolve().parent
ROOT = FEATURE_DIR.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import console, feature_bus, platform as plat, screen_ocr  # noqa: E402

console.configure_stdio()
plat.enable_dpi_awareness()
load_dotenv()

SETTINGS_FILE = FEATURE_DIR / "settings.json"
GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_TIMEOUT = 30
MAX_INPUT_CHARS = 4000

TONE_SYSTEM_PROMPT = """You are a literal, calibrated interpreter of social and emotional subtext, helping an autistic adult understand what a piece of text might mean beneath its surface words.

You will receive one snippet of text the user selected or clicked. Analyze ONLY that text. Do not invent context that is not present.

Identify, when genuinely present:
- overall tone (e.g. neutral, warm, cold, frustrated, anxious, formal, joking)
- sarcasm or irony (literal words vs. likely intended meaning)
- urgency or time pressure
- indirect or implied requests (asking for something without stating it plainly)
- passive aggression
- politeness softeners (hedging that masks a firmer point, e.g. "just", "no worries if not")
- emotional subtext the writer may not have stated outright

CRITICAL RULES:
- NEVER fabricate subtext. If the text is plainly literal with no notable social cues, say so clearly and return an empty "cues" list.
- Always hedge. Use words like "likely", "may", "could", "seems". You are reading probabilities, not facts. You cannot know the writer's true intent.
- Stay grounded in the actual words. Every cue MUST quote the exact span of text it is based on.
- Be concrete and plain. Explain "what it says" vs "what it likely means" in simple language. Avoid jargon.
- Do not give advice on how to respond unless the interpretation itself requires it.

Return ONLY valid JSON — no markdown, no commentary, in exactly this shape:
{
  "summary": "<one or two plain sentences on the overall tone and whether anything notable is present>",
  "tone": "<a few words naming the overall tone>",
  "cues": [
    {
      "type": "<sarcasm|urgency|indirect_request|passive_aggression|politeness_softener|emotional_subtext|other>",
      "quote": "<exact text this cue is based on>",
      "interpretation": "<what it likely means, hedged>",
      "confidence": "<low|medium|high>"
    }
  ]
}

If nothing notable is found, return a clear "summary" saying the text reads as literal/straightforward, set "tone" accordingly, and set "cues" to [].
"""

# Palette — copied from the other features so this looks like it belongs.
BG = "#1a1a2e"
CARD = "#23233f"
FG = "#e0e0ff"
MUTED = "#8a8ab0"
ACCENT = "#748ffc"
OK = "#69db7c"
WARN = "#ffd166"
REC = "#ff6b6b"

WIN_W, WIN_H = 320, 260
VC_W, VC_H = 300, 176
PANEL_W, PANEL_H = 520, 600
MARGIN = 12

_CONFIDENCE = ("low", "medium", "high")
_MODIFIER_TOKENS = ("ctrl", "alt", "shift", "cmd")
_CUE_LABELS = {
    "sarcasm": "Sarcasm / irony",
    "urgency": "Urgency / pressure",
    "indirect_request": "Indirect request",
    "passive_aggression": "Passive aggression",
    "politeness_softener": "Politeness softener",
    "emotional_subtext": "Emotional subtext",
    "other": "Social cue",
}


def log(msg: str):
    console.safe_print(f"[tone_reader] {msg}", flush=True)


class ParseError(Exception):
    """Groq returned something we couldn't parse as the expected JSON."""

    def __init__(self, raw: str):
        super().__init__("could not parse analysis JSON")
        self.raw = raw


def default_settings() -> dict:
    return {
        "click_to_analyze": False,
        # Ctrl+Shift+Y avoids Chrome's Ctrl/Cmd+Shift+T (reopen closed tab).
        "hotkeys": {"analyze_selection": "ctrl+shift+y"},
        "panel_font_size": 12,
        "show_raw_on_parse_error": True,
    }


def load_settings() -> dict:
    s = default_settings()
    if SETTINGS_FILE.exists():
        try:
            loaded = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            s.update(loaded)
            s["hotkeys"] = {**default_settings()["hotkeys"], **loaded.get("hotkeys", {})}
        except Exception as e:
            log(f"bad settings.json: {e} — using defaults")
    return s


def save_settings(settings: dict):
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def _norm_key(key) -> str | None:
    """Map a pynput key event to a stable token: a modifier name, a char, or 'f7'."""
    if key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
        return "ctrl"
    if key in (keyboard.Key.alt_l, keyboard.Key.alt_r):
        return "alt"
    if key in (keyboard.Key.shift_l, keyboard.Key.shift_r):
        return "shift"
    if key in (keyboard.Key.cmd, keyboard.Key.cmd_r):
        return "cmd"
    if hasattr(key, "char") and key.char and key.char.isprintable():
        return key.char.lower()
    if hasattr(key, "name") and key.name:
        return key.name.lower()
    return None


def parse_combo(spec: str) -> frozenset[str]:
    """Parse 'ctrl+shift+y' / 'command+shift+y' / 'F7' into a set of key tokens."""
    toks: set[str] = set()
    for p in (part.strip().lower() for part in spec.split("+") if part.strip()):
        if p in ("ctrl", "control"):
            toks.add("ctrl")
        elif p in ("alt", "option"):
            toks.add("alt")
        elif p == "shift":
            toks.add("shift")
        elif p in ("cmd", "command"):
            toks.add("cmd")
        else:
            toks.add(p)
    return frozenset(toks)


def combo_to_string(held: set[str], trigger: str) -> str:
    """Build a canonical 'ctrl+shift+y' style label from held modifiers + a key."""
    parts = []
    if "ctrl" in held:
        parts.append("ctrl")
    if "alt" in held:
        parts.append("alt")
    if "shift" in held:
        parts.append("shift")
    if "cmd" in held:
        parts.append("command")
    if len(trigger) == 1:
        parts.append(trigger.lower())
    elif trigger.startswith("f") and trigger[1:].isdigit():
        parts.append(trigger.upper())
    else:
        parts.append(trigger)
    return "+".join(parts)


class ToneReaderApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.settings = load_settings()

        # Input state — only ever mutated from the listener threads (plain data).
        self._held: set[str] = set()
        self._shift_down = False
        self._combo_target: frozenset[str] = parse_combo(
            self.settings["hotkeys"]["analyze_selection"])
        self._combo_armed = False          # debounce: fire once per full chord press
        self._capturing = False            # True while the "Set" button waits for keys

        self._kbd_listener = None          # one keyboard tap: hotkey + shift + capture
        self._mouse_listener = None        # one mouse tap: Shift+Click (when enabled)

        self._groq: Groq | None = None
        self._panel: tk.Toplevel | None = None
        self._panel_body: tk.Text | None = None
        self._last_analysis: dict | None = None
        self._busy = False

        self._build_ui()
        save_settings(self.settings)
        self._position_window()
        self._update_presence()
        self._start_keyboard_listener()
        self._update_click_listener()

        self.bind("<Configure>", self._on_configure)
        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT, self._shutdown)

    def _groq_client(self) -> Groq:
        if self._groq is None:
            key = os.getenv("GROQ_API_KEY")
            if not key:
                raise RuntimeError("GROQ_API_KEY required for tone analysis")
            self._groq = Groq(api_key=key, timeout=GROQ_TIMEOUT, max_retries=1)
        return self._groq

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        self.title("Tone & Social Cues")
        self.resizable(False, False)
        self.configure(bg=BG)
        self.attributes("-topmost", True)

        tk.Label(self, text="Tone & Social Cues",
                 font=tkfont.Font(family="Helvetica", size=11, weight="bold"),
                 fg=FG, bg=BG).pack(anchor="w", padx=14, pady=(10, 4))

        toggles = tk.Frame(self, bg=BG)
        toggles.pack(fill="x", padx=14)

        self._click_var = tk.BooleanVar(value=self.settings.get("click_to_analyze", False))
        tk.Checkbutton(toggles, text="Shift+Click to analyze in Chrome",
                       variable=self._click_var, command=self._on_toggle_change,
                       font=tkfont.Font(family="Helvetica", size=9),
                       fg=FG, bg=BG, selectcolor=CARD, activebackground=BG,
                       activeforeground=FG).pack(anchor="w")

        hk_frame = tk.Frame(self, bg=BG)
        hk_frame.pack(fill="x", padx=14, pady=(8, 0))
        self._analyze_hk_var = tk.StringVar(
            value=self.settings["hotkeys"]["analyze_selection"])
        self._add_hotkey_row(hk_frame, "Analyze:", self._analyze_hk_var)

        font_frame = tk.Frame(self, bg=BG)
        font_frame.pack(fill="x", padx=14, pady=(8, 0))
        tk.Label(font_frame, text="Panel text size:", width=14, anchor="w",
                 font=tkfont.Font(family="Helvetica", size=9),
                 fg=MUTED, bg=BG).pack(side="left")
        self._font_var = tk.IntVar(value=int(self.settings.get("panel_font_size", 12)))
        tk.Spinbox(font_frame, from_=9, to=22, width=4, textvariable=self._font_var,
                   command=self._on_font_change,
                   font=tkfont.Font(family="Helvetica", size=9),
                   bg=CARD, fg=FG, buttonbackground=CARD,
                   relief="flat", justify="center").pack(side="left")

        self.status_var = tk.StringVar(value="Idle")
        self.status_label = tk.Label(self, textvariable=self.status_var,
                 font=tkfont.Font(family="Helvetica", size=13, weight="bold"),
                 fg=MUTED, bg=BG, wraplength=WIN_W - 28, justify="left",
                 anchor="w")
        self.status_label.pack(fill="x", padx=14, pady=(10, 6))

        tk.Label(self,
                 text="Highlight text anywhere, then press your hotkey.",
                 font=tkfont.Font(family="Helvetica", size=8),
                 fg="#5a5a78", bg=BG, wraplength=WIN_W - 28, justify="left",
                 anchor="w").pack(anchor="w", padx=14, pady=(0, 8))

    def _add_hotkey_row(self, parent, label: str, var: tk.StringVar):
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=2)
        tk.Label(row, text=label, width=12, anchor="w",
                 font=tkfont.Font(family="Helvetica", size=9),
                 fg=MUTED, bg=BG).pack(side="left")
        tk.Label(row, textvariable=var,
                 font=tkfont.Font(family="Helvetica", size=9, weight="bold"),
                 fg=FG, bg=BG, width=14, anchor="w").pack(side="left")
        tk.Button(row, text="Set", command=self._start_hotkey_capture,
                  font=tkfont.Font(family="Helvetica", size=8),
                  bg=CARD, fg=FG, relief="flat", padx=6, cursor="hand2").pack(side="right")

    def _set_status(self, msg: str, color: str = MUTED):
        def _apply():
            self.status_var.set(msg)
            self.status_label.configure(fg=color)
        self.after(0, _apply)

    def _on_configure(self, _event=None):
        self.after(100, self._update_presence)

    def _position_window(self):
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        x = sw - WIN_W - 24
        y = sh - WIN_H - 80

        presence = feature_bus.load_presence()
        vc = presence.get("voice_control")
        if vc and feature_bus.is_feature_running("voice_control"):
            win = vc.get("window") or {}
            vx = win.get("x", sw - VC_W - 24)
            vy = win.get("y", sh - VC_H - 80)
            y = vy - WIN_H - MARGIN
            x = vx

        self.geometry(f"{WIN_W}x{WIN_H}+{x}+{y}")
        self.lift()

    def _update_presence(self):
        feature_bus.update_presence(
            "tone_reader",
            os.getpid(),
            {"x": self.winfo_x(), "y": self.winfo_y(), "w": WIN_W, "h": WIN_H},
        )

    def _on_toggle_change(self):
        self.settings["click_to_analyze"] = self._click_var.get()
        save_settings(self.settings)
        self._update_click_listener()

    def _on_font_change(self):
        try:
            size = int(self._font_var.get())
        except (tk.TclError, ValueError):
            return
        self.settings["panel_font_size"] = size
        save_settings(self.settings)
        if self._panel_body is not None and self._last_analysis is not None:
            self._render_analysis(self._last_analysis)

    # ------------------------------------------------------ keyboard input
    # ONE keyboard listener does three jobs: detect the analyze hotkey, track
    # Shift for Shift+Click, and capture a new hotkey when "Set" is pressed.
    # Its callbacks only touch plain state; anything UI-facing goes through
    # self.after(0, ...) so it runs on the main thread.

    def _start_keyboard_listener(self):
        if self._kbd_listener:
            return
        self._kbd_listener = keyboard.Listener(
            on_press=self._on_key_press, on_release=self._on_key_release)
        self._kbd_listener.start()
        log(f"hotkey: analyze={'+'.join(sorted(self._combo_target))!r}")

    def _on_key_press(self, key):
        tok = _norm_key(key)
        if tok is None:
            return
        self._held.add(tok)
        self._shift_down = "shift" in self._held

        if self._capturing:
            if tok in _MODIFIER_TOKENS:
                return  # wait for a non-modifier to complete the chord
            combo = combo_to_string(self._held, tok)
            self._capturing = False
            self.after(0, lambda c=combo: self._finish_hotkey_capture(c))
            return

        if self._combo_target and self._combo_target.issubset(self._held):
            if not self._combo_armed:
                self._combo_armed = True
                self.after(0, self.cmd_analyze_selection)

    def _on_key_release(self, key):
        tok = _norm_key(key)
        if tok is None:
            return
        self._held.discard(tok)
        self._shift_down = "shift" in self._held
        if not (self._combo_target and self._combo_target.issubset(self._held)):
            self._combo_armed = False

    def _start_hotkey_capture(self):
        if self._capturing:
            return
        self._held.clear()
        self._capturing = True
        self._set_status("Press key combo…", ACCENT)

    def _finish_hotkey_capture(self, combo: str):
        # Runs on the main thread — safe to touch tkinter and settings.
        self.settings["hotkeys"]["analyze_selection"] = combo
        self._analyze_hk_var.set(combo)
        self._combo_target = parse_combo(combo)
        self._combo_armed = False
        save_settings(self.settings)
        self._set_status(f"Hotkey set: {combo}", OK)

    # -------------------------------------------------- Trigger B: Shift+Click

    def _ocr_log(self, stage: str, msg: str, _level: str = "INFO"):
        log(f"{stage} {msg}")

    def _update_click_listener(self):
        if self._mouse_listener:
            self._mouse_listener.stop()
            self._mouse_listener = None
        if not self.settings.get("click_to_analyze"):
            return
        self._mouse_listener = mouse.Listener(on_click=self._on_click)
        self._mouse_listener.start()
        log("Shift+Click-to-analyze enabled")

    def _on_click(self, x, y, button, pressed):
        if not pressed or button != mouse.Button.left:
            return
        if not self._shift_down or self._busy:
            return
        self.after(0, lambda: threading.Thread(
            target=self._do_click_analyze, args=(int(x), int(y)), daemon=True).start())

    def _do_click_analyze(self, x: int, y: int):
        try:
            region = screen_ocr.region_around_point(x, y, pad_w=720, pad_h=280)
            elements = screen_ocr.capture_screen_elements(
                log_fn=self._ocr_log, region=region)
            el = screen_ocr.elements_at_point(elements, x, y)
            if not el or not el.get("text", "").strip():
                self._set_status("No text under that click", WARN)
                return
            self._analyze(el["text"].strip())
        except Exception as e:
            log(f"click analyze failed: {e}")
            self._set_status(f"Error: {e}", REC)

    # ----------------------------------------------- Trigger A: highlight+key

    def cmd_analyze_selection(self):
        if self._busy:
            return
        log("hotkey fired — capturing selection")
        self._set_status("Capturing selection…", ACCENT)
        threading.Thread(target=self._do_analyze_selection, daemon=True).start()

    def _send_copy(self):
        """Trigger a clean Copy in the foreground app (independent of held keys)."""
        # Method 1: pyautogui posts a synthetic Command+C (needs Accessibility).
        try:
            pyautogui.hotkey(plat.mod_key(), "c")
        except Exception as e:
            log(f"pyautogui copy failed: {e}")
        # Method 2 (mac): System Events keystroke as a fallback; log the real error
        # so permission problems are obvious.
        if plat.IS_MAC:
            import subprocess
            r = subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to keystroke "c" using command down'],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0:
                log(f"osascript copy error: {r.stderr.strip()}")

    def _capture_selection(self) -> str:
        """Copy the current selection in the foreground app, restoring the clipboard."""
        # The hotkey's own modifiers (e.g. Shift+Command) are still physically held
        # when this runs. Wait for them to clear, then force-release as a safety net,
        # so our synthetic Copy isn't contaminated (e.g. Shift+Command+C).
        deadline = time.time() + 1.5
        while time.time() < deadline and (self._held & set(_MODIFIER_TOKENS)):
            time.sleep(0.02)
        for k in ("shift", "ctrl", "alt", "option", "command"):
            try:
                pyautogui.keyUp(k)
            except Exception:
                pass
        log(f"modifiers cleared (held={sorted(self._held)}) — sending copy")

        saved = None
        try:
            saved = pyperclip.paste()
        except Exception:
            pass
        # Clear to a sentinel so we can tell "nothing was selected" apart from a
        # stale clipboard.
        try:
            pyperclip.copy("")
        except Exception:
            pass
        time.sleep(0.05)
        try:
            self._send_copy()
        except Exception as e:
            log(f"copy keystroke failed: {e}")
        time.sleep(0.25)  # the pasteboard write is async, especially on macOS
        try:
            text = pyperclip.paste() or ""
        except Exception:
            text = ""
        log(f"captured {len(text)} chars: {text[:60]!r}")
        if saved is not None:
            try:
                pyperclip.copy(saved)
            except Exception:
                pass
        return text.strip()

    def _do_analyze_selection(self):
        try:
            text = self._capture_selection()
            if not text:
                self._set_status("No text selected — highlight something first", WARN)
                return
            if len(text) > MAX_INPUT_CHARS:
                text = text[:MAX_INPUT_CHARS]
                self._set_status("Long selection — analyzing the first part…", ACCENT)
            self._analyze(text)
        except Exception as e:
            log(f"selection analyze failed: {e}")
            self._set_status(f"Error: {e}", REC)

    # ----------------------------------------------------------- analysis

    def _analyze(self, text: str):
        """Shared path for both triggers: show loading panel, call Groq, render."""
        self._busy = True
        self._set_status("Analyzing tone…", ACCENT)
        self.after(0, self._open_panel_loading)
        try:
            data = self._run_analysis(text)
            self._last_analysis = data
            self.after(0, lambda: self._render_analysis(data))
            self._set_status("Done", OK)
        except ParseError as e:
            log("Groq returned unparseable JSON")
            raw = e.raw if self.settings.get("show_raw_on_parse_error") else None
            self.after(0, lambda: self._render_error(
                "I couldn't read the analysis cleanly. Try again.", raw))
            self._set_status("Couldn't read the AI response", WARN)
        except RuntimeError as e:
            log(f"config error: {e}")
            self.after(0, lambda: self._render_error(
                "GROQ_API_KEY is not set — add it to your .env file.", None))
            self._set_status("Missing API key", WARN)
        except Exception as e:
            log(f"analysis failed: {e}")
            self.after(0, lambda: self._render_error(
                "Couldn't reach the analysis service. Check your internet and try again.",
                None))
            self._set_status("Analysis failed", REC)
        finally:
            self._busy = False

    def _run_analysis(self, text: str) -> dict:
        client = self._groq_client()
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": TONE_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            temperature=0,
            max_tokens=1024,
        )
        raw = (response.choices[0].message.content or "").strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return self._parse_analysis(raw.strip())

    def _parse_analysis(self, raw: str) -> dict:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise ParseError(raw)
        if not isinstance(data, dict):
            raise ParseError(raw)
        cues = []
        for c in data.get("cues", []) or []:
            if not isinstance(c, dict):
                continue
            conf = str(c.get("confidence", "low")).lower()
            if conf not in _CONFIDENCE:
                conf = "low"
            cues.append({
                "type": str(c.get("type", "other")),
                "quote": str(c.get("quote", "")),
                "interpretation": str(c.get("interpretation", "")),
                "confidence": conf,
            })
        return {
            "summary": str(data.get("summary", "")).strip(),
            "tone": str(data.get("tone", "")).strip(),
            "cues": cues,
        }

    # --------------------------------------------------------------- panel
    # NOTE: tkinter exposes no ARIA / accessibility tree. This panel is a
    # best-effort keyboard-and-contrast approximation (focusable + scrollable
    # body, Esc to dismiss, high-contrast palette, adjustable font) — it is not
    # a screen-reader-native surface.

    def _ensure_panel(self) -> tk.Text:
        if self._panel is not None and self._panel.winfo_exists():
            for child in self._panel.winfo_children():
                child.destroy()
        else:
            self._panel = tk.Toplevel(self)
            self._panel.title("Tone analysis")
            self._panel.configure(bg=BG)
            self._panel.attributes("-topmost", True)
            self._panel.protocol("WM_DELETE_WINDOW", self._close_panel)
            self._panel.bind("<Escape>", lambda e: self._close_panel())
            sw = self.winfo_screenwidth()
            sh = self.winfo_screenheight()
            x = max(20, (sw - PANEL_W) // 2)
            y = max(20, (sh - PANEL_H) // 2)
            self._panel.geometry(f"{PANEL_W}x{PANEL_H}+{x}+{y}")

        tk.Label(self._panel, text="Tone analysis",
                 font=tkfont.Font(family="Helvetica", size=13, weight="bold"),
                 fg=FG, bg=BG).pack(anchor="w", padx=16, pady=(12, 6))

        body_frame = tk.Frame(self._panel, bg=BG)
        body_frame.pack(fill="both", expand=True, padx=16, pady=(0, 6))
        size = int(self.settings.get("panel_font_size", 12))
        body = tk.Text(body_frame, wrap="word", bg=CARD, fg=FG,
                       insertbackground=FG, relief="flat", padx=12, pady=10,
                       font=tkfont.Font(family="Helvetica", size=size),
                       takefocus=True, cursor="arrow")
        scroll = tk.Scrollbar(body_frame, command=body.yview)
        body.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        body.pack(side="left", fill="both", expand=True)

        body.tag_configure("h", font=tkfont.Font(family="Helvetica", size=size + 1,
                                                  weight="bold"), foreground=FG)
        body.tag_configure("label", font=tkfont.Font(family="Helvetica", size=size,
                                                      weight="bold"), foreground=ACCENT)
        body.tag_configure("quote", foreground=MUTED,
                           font=tkfont.Font(family="Helvetica", size=size, slant="italic"))
        body.tag_configure("meta", foreground=MUTED,
                           font=tkfont.Font(family="Helvetica", size=max(8, size - 2)))
        body.tag_configure("body", foreground=FG)

        tk.Button(self._panel, text="Close (Esc)", command=self._close_panel,
                  font=tkfont.Font(family="Helvetica", size=11),
                  bg=ACCENT, fg=BG, relief="flat", padx=12, pady=4,
                  cursor="hand2", activebackground=ACCENT).pack(pady=(0, 12))

        self._panel_body = body
        self._panel.lift()
        body.focus_set()
        return body

    def _open_panel_loading(self):
        body = self._ensure_panel()
        body.configure(state="normal")
        body.delete("1.0", "end")
        body.insert("end", "Analyzing tone…\n", "h")
        body.insert("end", "Reading the social subtext of your text.", "meta")
        body.configure(state="disabled")

    def _render_analysis(self, data: dict):
        body = self._ensure_panel()
        body.configure(state="normal")
        body.delete("1.0", "end")

        summary = data.get("summary") or "No summary returned."
        body.insert("end", "Summary\n", "h")
        body.insert("end", summary + "\n\n", "body")

        tone = data.get("tone")
        if tone:
            body.insert("end", "Tone: ", "label")
            body.insert("end", tone + "\n\n", "body")

        cues = data.get("cues") or []
        if not cues:
            body.insert("end",
                        "Nothing notable — this reads as straightforward.\n", "body")
        else:
            body.insert("end", "What stood out\n", "h")
            for c in cues:
                label = _CUE_LABELS.get(c.get("type", "other"), "Social cue")
                body.insert("end", f"\n{label}\n", "label")
                if c.get("quote"):
                    body.insert("end", "What it says: ", "meta")
                    body.insert("end", c["quote"] + "\n", "quote")
                if c.get("interpretation"):
                    body.insert("end", "What it likely means: ", "meta")
                    body.insert("end", c["interpretation"] + "\n", "body")
                body.insert("end", f"Confidence: {c.get('confidence', 'low')}\n", "meta")

        body.configure(state="disabled")
        body.focus_set()

    def _render_error(self, message: str, raw: str | None):
        body = self._ensure_panel()
        body.configure(state="normal")
        body.delete("1.0", "end")
        body.insert("end", "Couldn't analyze this text\n", "h")
        body.insert("end", message + "\n", "body")
        if raw:
            body.insert("end", "\nRaw response:\n", "meta")
            body.insert("end", raw, "quote")
        body.configure(state="disabled")

    def _close_panel(self):
        if self._panel is not None and self._panel.winfo_exists():
            self._panel.destroy()
        self._panel = None
        self._panel_body = None

    # ------------------------------------------------------------ shutdown

    def _shutdown(self, signum=None, frame=None):
        log("shutting down")
        if self._kbd_listener:
            self._kbd_listener.stop()
        if self._mouse_listener:
            self._mouse_listener.stop()
        feature_bus.remove_presence("tone_reader")
        self.after(0, self.destroy)
        sys.exit(0)


def main():
    log("starting")
    hint = plat.permission_hints("tone_reader")
    if not hint and plat.IS_MAC:
        hint = ("macOS: grant Accessibility (global hotkey + copy) and Screen "
                "Recording (Shift+Click OCR) in System Settings → Privacy & Security.")
    if hint:
        log(hint)
    app = ToneReaderApp()
    app.mainloop()
    feature_bus.remove_presence("tone_reader")
    log("exited")


if __name__ == "__main__":
    main()
