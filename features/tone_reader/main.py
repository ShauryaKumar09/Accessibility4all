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

from shared import (console, feature_bus, platform as plat, screen_ocr,  # noqa: E402
                    settings_store as store, ui_kit as ui)
from shared.ui_kit import C  # noqa: E402

console.configure_stdio()
plat.enable_dpi_awareness()
load_dotenv()

SETTINGS_FILE = FEATURE_DIR / "settings.json"
GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_TIMEOUT = 30
MAX_INPUT_CHARS = 4000

TONE_SYSTEM_PROMPT = """You explain the social and emotional subtext of a message to an autistic adult who finds implied meaning hard to read.

You will receive one snippet of text the user selected or clicked. Explain ONLY that text. Do not invent context that is not present.

Look for what is genuinely there: the overall tone, sarcasm or irony, urgency, an indirect request, passive aggression, politeness that softens a firmer point, or feeling the writer did not state outright.

CRITICAL RULES:
- NEVER fabricate subtext. If the text is plainly literal, say so.
- Hedge. Say "likely", "seems", "may" — you are reading probabilities, not facts.
- Stay grounded in the actual words, and quote a few of them when it helps.
- Write plainly for a person, not a report: no cue-type names, no confidence
  scores, no headings, no lists.
- AT MOST TWO SENTENCES in "answer". Short, ordinary words.

Return ONLY valid JSON — no markdown, no commentary, in exactly this shape:
{
  "tone": "<a few plain words naming the tone, lower case, e.g. polite but impatient>",
  "answer": "<at most two sentences explaining what they likely mean>"
}
"""

# The answer card is the whole UI now: one tone chip, at most two sentences,
# and a single Got it button. The old 320x260 window and its 520x600 panel are
# both gone, and the settings they held moved into the hub.
FG = C["FG"]
MUTED = C["FG_MUTED"]
ACCENT = C["ACCENT"]
OK = C["ON"]
WARN = C["WARM_TEXT"]
REC = C["STOP_BORDER"]

CARD_W = 400
CARD_PAD_X, CARD_PAD_Y = 22, 20
CARD_GAP = 12
BUTTON_H = 52
MARGIN = 12
SETTINGS_WATCH_MS = 700
# Three named sizes replace the old free-form spinbox.
TEXT_SIZES = store.TONE_TEXT_SIZES

_MODIFIER_TOKENS = ("ctrl", "alt", "shift", "cmd")


def log(msg: str):
    console.safe_print(f"[tone_reader] {msg}", flush=True)


class ParseError(Exception):
    """Groq returned something we couldn't parse as the expected JSON."""

    def __init__(self, raw: str):
        super().__init__("could not parse analysis JSON")
        self.raw = raw


def default_settings() -> dict:
    # Ctrl+Shift+Y avoids Chrome's Ctrl/Cmd+Shift+T (reopen closed tab).
    return dict(store.DEFAULTS["tone_reader"])


def load_settings() -> dict:
    return store.load("tone_reader")


def save_settings(settings: dict):
    store.save("tone_reader", settings)


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

        self._kbd_listener = None          # one keyboard tap: hotkey + shift + capture
        self._mouse_listener = None        # one mouse tap: Shift+Click (when enabled)

        self._groq: Groq | None = None
        self._last_analysis: dict | None = None
        self._busy = False
        self._visible = False
        self._bounds: tuple[int, int, int, int] | None = None
        self._watcher = store.Watcher("tone_reader")

        self._build_ui()
        save_settings(self.settings)
        self._update_presence()
        self._start_keyboard_listener()
        self._ensure_mouse_listener()
        self.after(SETTINGS_WATCH_MS, self._watch_settings)
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
        """The whole UI is one card that appears next to what you highlighted.

        Tone chip, at most two sentences, one big Got it. The Shift+Click
        option, the hotkey row and the text-size control all moved into the
        hub's settings sheet.
        """
        self.title("Tone & Social Cues")
        self.resizable(False, False)
        self.fonts = ui.FontSet(1.0)
        self._card_h = 200
        self._visible = False
        self._transparent = ui.make_bubble(self, CARD_W, self._card_h)
        self.canvas = ui.bubble_canvas(self, CARD_W, self._card_h,
                                       self._transparent)
        self.canvas.pack(fill="both", expand=True)

        self._dismiss = ui.TextButton(
            self.canvas, self.fonts, "Got it", self.hide_card, role="body_sm",
            height=BUTTON_H, width=CARD_W - CARD_PAD_X * 2, radius=12)
        self.canvas.create_window(CARD_PAD_X, 0, window=self._dismiss,
                                  anchor="nw", tags="dismiss")

        self.bind("<Escape>", lambda e: self.hide_card())
        self.withdraw()

    def _answer_font(self):
        size = TEXT_SIZES.get(self.settings.get("text_size", "medium"), 22)
        return tkfont.Font(family="Helvetica", size=size)

    def show_card(self, tone: str, answer: str, tone_color: bool = True):
        """Draw the card and put it next to the pointer, on screen."""
        font = self._answer_font()
        line_h = int(font.metrics("linespace") * 1.45)
        inner = CARD_W - CARD_PAD_X * 2

        chip_font = self.fonts["ui_b"]
        chip_h = chip_font.metrics("linespace") + 12 if tone else 0
        lines = ui.wrap_lines(font, answer, inner)
        height = (CARD_PAD_Y * 2 + (chip_h + CARD_GAP if tone else 0)
                  + len(lines) * line_h + CARD_GAP + BUTTON_H)

        self._card_h = height
        self.canvas.configure(width=CARD_W, height=height)
        self.canvas.delete("card")
        ui.rounded_rect(self.canvas, 1, 1, CARD_W - 1, height - 1, 16,
                        fill=C["BUBBLE"], outline=C["BORDER_CTRL"], width=1,
                        tags="card")
        self.canvas.tag_lower("card")

        y = CARD_PAD_Y
        if tone:
            chip_w = chip_font.measure(tone) + 28
            fill = C["WARM_BG"] if tone_color else C["CHIP"]
            border = C["WARM_BORDER"] if tone_color else C["BORDER_CHIP"]
            text_col = C["WARM_TEXT"] if tone_color else C["FG_SECOND"]
            ui.pill(self.canvas, CARD_PAD_X, y, CARD_PAD_X + chip_w, y + chip_h,
                    fill=fill, outline=border, width=1, tags="card")
            self.canvas.create_text(CARD_PAD_X + chip_w / 2, y + chip_h / 2,
                                    text=tone, font=chip_font, fill=text_col,
                                    tags="card")
            y += chip_h + CARD_GAP

        for line in lines:
            self.canvas.create_text(CARD_PAD_X, y + line_h / 2, anchor="w",
                                    text=line, font=font, fill=C["FG"],
                                    tags="card")
            y += line_h
        y += CARD_GAP
        self.canvas.coords("dismiss", CARD_PAD_X, y)

        self._place_near_pointer(height)
        self._visible = True
        ui.raise_bubble(self)
        self._ensure_mouse_listener()

    def _place_near_pointer(self, height: int):
        """Next to the selection — i.e. where the pointer is — clamped on screen."""
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        try:
            # pyautogui, pynput and Tk all speak logical points on macOS, so the
            # pointer position needs no Retina scaling here.
            px, py = pyautogui.position()
        except Exception:
            px, py = sw // 2, sh // 2
        x = min(max(MARGIN, px - CARD_W // 2), sw - CARD_W - MARGIN)
        y = py + 24
        if y + height > sh - MARGIN:
            y = max(MARGIN, py - height - 24)
        self.geometry(f"{CARD_W}x{height}+{int(x)}+{int(y)}")
        self._bounds = (x, y, x + CARD_W, y + height)

    def hide_card(self):
        self._visible = False
        self.withdraw()
        self._ensure_mouse_listener()

    def _update_presence(self):
        feature_bus.update_presence(
            "tone_reader", os.getpid(),
            {"x": self.winfo_x(), "y": self.winfo_y(), "w": CARD_W,
             "h": self._card_h},
        )

    def _set_status(self, msg: str, color: str = MUTED):
        """There is no status line any more: progress and errors go to the
        terminal, and anything the user must see is shown on the card."""
        log(msg)

    def _watch_settings(self):
        """Pick up edits the hub made to settings.json while we were running."""
        if self._watcher.changed():
            self.settings = load_settings()
            combo = self.settings["hotkeys"]["analyze_selection"]
            self._combo_target = parse_combo(combo)
            self._combo_armed = False
            log(f"settings changed in the hub — hotkey={combo!r}")
            self._ensure_mouse_listener()
            if self._visible and self._last_analysis:
                self.show_card(self._last_analysis.get("tone", ""),
                               self._last_analysis.get("answer", ""))
        self.after(SETTINGS_WATCH_MS, self._watch_settings)

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

    # -------------------------------------------------- Trigger B: Shift+Click

    def _ocr_log(self, stage: str, msg: str, _level: str = "INFO"):
        log(f"{stage} {msg}")

    def _ensure_mouse_listener(self):
        """One mouse tap, two jobs: Shift+Click to analyze, click away to dismiss.

        Stacking listeners is what crashed earlier versions on macOS, so this is
        the only mouse listener the feature ever runs.
        """
        wanted = bool(self.settings.get("click_to_analyze")) or self._visible
        if wanted and self._mouse_listener is None:
            self._mouse_listener = mouse.Listener(on_click=self._on_click)
            self._mouse_listener.start()
            log("mouse listener on (Shift+Click / dismiss)")
        elif not wanted and self._mouse_listener is not None:
            self._mouse_listener.stop()
            self._mouse_listener = None

    def _on_click(self, x, y, button, pressed):
        if not pressed or button != mouse.Button.left:
            return
        if self._visible and not self._point_in_card(int(x), int(y)):
            self.after(0, self.hide_card)
            return
        if not self.settings.get("click_to_analyze"):
            return
        if not self._shift_down or self._busy:
            return
        self.after(0, lambda: threading.Thread(
            target=self._do_click_analyze, args=(int(x), int(y)), daemon=True).start())

    def _point_in_card(self, x: int, y: int) -> bool:
        if not self._bounds:
            return False
        x0, y0, x1, y1 = self._bounds
        return x0 <= x <= x1 and y0 <= y <= y1

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
        answer = str(data.get("answer", "")).strip()
        if not answer:
            raise ParseError(raw)
        return {"tone": str(data.get("tone", "")).strip().lower(), "answer": answer}

    # --------------------------------------------------------------- panel
    # NOTE: tkinter exposes no ARIA / accessibility tree. This panel is a
    # best-effort keyboard-and-contrast approximation (focusable + scrollable
    # body, Esc to dismiss, high-contrast palette, adjustable font) — it is not
    # a screen-reader-native surface.

    def _open_panel_loading(self):
        self.show_card("", "Reading the tone…", tone_color=False)

    def _render_analysis(self, data: dict):
        self.show_card(data.get("tone", ""), data.get("answer", ""))

    def _render_error(self, message: str, raw: str | None):
        if raw:
            log(f"raw response: {raw[:400]}")
        self.show_card("", message, tone_color=False)

    def _close_panel(self):
        self.hide_card()

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
