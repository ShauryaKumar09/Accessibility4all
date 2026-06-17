"""Page Reader — OCR on-screen text and speak it aloud.

Runs as its own process when toggled ON in the hub. See features/README.md.
"""

from __future__ import annotations

import json
import os
import queue
import re
import signal
import sys
import threading
import time
from pathlib import Path

import pyttsx3
import tkinter as tk
from dotenv import load_dotenv
from groq import Groq
from pynput import keyboard, mouse
from tkinter import font as tkfont

FEATURE_DIR = Path(__file__).resolve().parent
ROOT = FEATURE_DIR.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import console, feature_bus, groq_vision, platform as plat, screen_ocr  # noqa: E402

console.configure_stdio()
plat.enable_dpi_awareness()
load_dotenv()

SETTINGS_FILE = FEATURE_DIR / "settings.json"
GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_TIMEOUT = 30
MAX_ELEMENTS = 120
READ_SECTION_PROMPT = """You help a blind user hear the right part of the screen read aloud.
You receive a numbered list of visible text lines from OCR and a spoken request about what to read.

Return ONLY valid JSON — no markdown, no explanation:
{"indices": [<int>, ...]}

Pick one or more consecutive line indices that best match what the user asked to hear.
If nothing matches, return {"indices": []}.
"""

BG = "#1a1a2e"
CARD = "#23233f"
FG = "#e0e0ff"
MUTED = "#8a8ab0"
ACCENT = "#748ffc"
OK = "#69db7c"
WARN = "#ffd166"
REC = "#ff6b6b"

WIN_W, WIN_H = 300, 248
VC_W, VC_H = 300, 176
MARGIN = 12


def log(msg: str):
    console.safe_print(f"[page_reader] {msg}", flush=True)


def default_settings() -> dict:
    return dict(feature_bus.DEFAULT_PAGE_READER_SETTINGS)


def load_settings() -> dict:
    s = default_settings()
    if SETTINGS_FILE.exists():
        try:
            loaded = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            s.update(loaded)
            s["hotkeys"] = {**default_settings()["hotkeys"], **loaded.get("hotkeys", {})}
            if "hover_to_read" not in loaded and loaded.get("click_to_read"):
                s["hover_to_read"] = True
            if loaded.get("tts_rate", 70) >= 85:
                s["tts_rate"] = 70
        except Exception as e:
            log(f"bad settings.json: {e} — using defaults")
    return s


def save_settings(settings: dict):
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def hotkey_to_pynput(spec: str) -> str | None:
    """Convert stored hotkey like 'ctrl+shift+a' or 'F9' to pynput GlobalHotKeys form."""
    spec = spec.strip()
    if not spec:
        return None
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    out = []
    for p in parts:
        if p in ("ctrl", "control"):
            out.append("<ctrl>")
        elif p in ("alt", "option"):
            out.append("<alt>")
        elif p in ("shift",):
            out.append("<shift>")
        elif p in ("cmd", "command"):
            out.append("<cmd>")
        elif p.startswith("f") and p[1:].isdigit():
            out.append(f"<{p}>")
        elif len(p) == 1:
            out.append(p)
        else:
            out.append(f"<{p}>")
    return "+".join(out) if out else None


def format_hotkey_from_event(key, modifiers: set) -> str:
    parts = []
    if keyboard.Key.ctrl_l in modifiers or keyboard.Key.ctrl_r in modifiers:
        parts.append("ctrl")
    if keyboard.Key.alt_l in modifiers or keyboard.Key.alt_r in modifiers:
        parts.append("alt")
    if keyboard.Key.shift_l in modifiers or keyboard.Key.shift_r in modifiers:
        parts.append("shift")
    if keyboard.Key.cmd in modifiers or keyboard.Key.cmd_r in modifiers:
        parts.append("command")
    if hasattr(key, "char") and key.char and key.char.isprintable():
        parts.append(key.char.lower())
    elif hasattr(key, "name") and key.name:
        name = key.name.replace("_l", "").replace("_r", "")
        if name in ("ctrl", "alt", "shift", "cmd"):
            return "+".join(parts) if parts else ""
        if name.startswith("f") and name[1:].isdigit():
            parts.append(name.upper())
        else:
            parts.append(name)
    return "+".join(parts)


class Speaker:
    """Background TTS queue with stop support."""

    _STOP = object()

    def __init__(self, rate: int = 180, volume: float = 1.0):
        self._rate = rate
        self._volume = volume
        self._q: queue.Queue = queue.Queue()
        self._engine = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        self._engine = pyttsx3.init()
        self._engine.setProperty("rate", self._rate)
        self._engine.setProperty("volume", self._volume)
        try:
            voices = self._engine.getProperty("voices") or []
            for voice in voices:
                name = (voice.name or "").lower()
                vid = (voice.id or "").lower()
                if any(tag in name or tag in vid for tag in ("zira", "david", "samantha", "karen")):
                    self._engine.setProperty("voice", voice.id)
                    break
        except Exception:
            pass
        while True:
            item = self._q.get()
            if item is self._STOP:
                break
            if item is None:
                if self._engine:
                    self._engine.stop()
                continue
            try:
                self._engine.say(item)
                self._engine.runAndWait()
            except Exception as e:
                log(f"TTS error: {e}")

    def configure(self, rate: int, volume: float):
        self._rate, self._volume = rate, volume
        if self._engine:
            self._engine.setProperty("rate", rate)
            self._engine.setProperty("volume", volume)

    def speak_lines(self, lines: list[str]):
        self.stop()
        for line in lines:
            if line.strip():
                self._q.put(line.strip())

    def stop(self):
        self._q.put(None)

    def shutdown(self):
        self._q.put(self._STOP)


class PageReaderApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self._last_region: dict | None = None
        self._last_region_ts = 0.0
        self._bus_offset = 0
        self._hotkey_listener = None
        self._hover_listener = None
        self._hover_after_id = None
        self._pending_hover: tuple[int, int] | None = None
        self._last_hover_text = ""
        self._capture_listener = None
        self._capture_target: str | None = None
        self._modifiers: set = set()
        self._groq: Groq | None = None

        self._speaker = Speaker(
            rate=int(self.settings.get("tts_rate", 70)),
            volume=float(self.settings.get("tts_volume", 1.0)),
        )

        self._build_ui()
        save_settings(self.settings)
        self._position_window()
        self._update_presence()
        self._register_hotkeys()
        self._update_hover_listener()
        self._start_bus_listener()

        self.bind("<Configure>", self._on_configure)
        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT, self._shutdown)

    def _groq_client(self) -> Groq:
        if self._groq is None:
            key = os.getenv("GROQ_API_KEY")
            if not key:
                raise RuntimeError("GROQ_API_KEY required for voice-guided section read")
            self._groq = Groq(api_key=key, timeout=GROQ_TIMEOUT, max_retries=1)
        return self._groq

    def _build_ui(self):
        self.title("Page Reader")
        self.resizable(False, False)
        self.configure(bg=BG)
        self.attributes("-topmost", True)

        tk.Label(self, text="Page Reader",
                 font=tkfont.Font(family="Helvetica", size=11, weight="bold"),
                 fg=FG, bg=BG).pack(anchor="w", padx=14, pady=(10, 4))

        toggles = tk.Frame(self, bg=BG)
        toggles.pack(fill="x", padx=14)

        self._voice_var = tk.BooleanVar(value=self.settings.get("voice_guided", True))
        tk.Checkbutton(toggles, text="Voice-guided sections (needs Voice Control)",
                       variable=self._voice_var, command=self._on_toggle_change,
                       font=tkfont.Font(family="Helvetica", size=9),
                       fg=FG, bg=BG, selectcolor=CARD, activebackground=BG,
                       activeforeground=FG).pack(anchor="w")

        self._hover_var = tk.BooleanVar(value=self.settings.get("hover_to_read", False))
        tk.Checkbutton(toggles, text="Hover-to-read (pause over text)",
                       variable=self._hover_var, command=self._on_toggle_change,
                       font=tkfont.Font(family="Helvetica", size=9),
                       fg=FG, bg=BG, selectcolor=CARD, activebackground=BG,
                       activeforeground=FG).pack(anchor="w")

        self._groq_var = tk.BooleanVar(value=self.settings.get("use_groq_summary", True))
        tk.Checkbutton(toggles, text="Groq summary (important content only)",
                       variable=self._groq_var, command=self._on_toggle_change,
                       font=tkfont.Font(family="Helvetica", size=9),
                       fg=FG, bg=BG, selectcolor=CARD, activebackground=BG,
                       activeforeground=FG).pack(anchor="w")

        hk_frame = tk.Frame(self, bg=BG)
        hk_frame.pack(fill="x", padx=14, pady=(8, 0))

        self._read_hk_var = tk.StringVar(value=self.settings["hotkeys"]["read_screen"])
        self._stop_hk_var = tk.StringVar(value=self.settings["hotkeys"]["stop"])
        self._add_hotkey_row(hk_frame, "Read Chrome:", self._read_hk_var, "read_screen")
        self._add_hotkey_row(hk_frame, "Stop:", self._stop_hk_var, "stop")

        self.status_var = tk.StringVar(value="Idle")
        self.status_label = tk.Label(self, textvariable=self.status_var,
                 font=tkfont.Font(family="Helvetica", size=13, weight="bold"),
                 fg=MUTED, bg=BG, wraplength=WIN_W - 28, justify="left",
                 anchor="w")
        self.status_label.pack(fill="x", padx=14, pady=(10, 8))

        tk.Label(self, text="Use hotkeys or voice — no on-screen buttons.",
                 font=tkfont.Font(family="Helvetica", size=8),
                 fg="#5a5a78", bg=BG).pack(anchor="w", padx=14, pady=(0, 8))

    def _add_hotkey_row(self, parent, label: str, var: tk.StringVar, key: str):
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=2)
        tk.Label(row, text=label, width=12, anchor="w",
                 font=tkfont.Font(family="Helvetica", size=9),
                 fg=MUTED, bg=BG).pack(side="left")
        tk.Label(row, textvariable=var,
                 font=tkfont.Font(family="Helvetica", size=9, weight="bold"),
                 fg=FG, bg=BG, width=14, anchor="w").pack(side="left")
        tk.Button(row, text="Set", command=lambda: self._start_hotkey_capture(key),
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
            "page_reader",
            os.getpid(),
            {"x": self.winfo_x(), "y": self.winfo_y(), "w": WIN_W, "h": WIN_H},
        )

    def _on_toggle_change(self):
        self.settings["voice_guided"] = self._voice_var.get()
        self.settings["hover_to_read"] = self._hover_var.get()
        self.settings["use_groq_summary"] = self._groq_var.get()
        save_settings(self.settings)
        self._update_hover_listener()

    def _save_hotkeys(self):
        self.settings["hotkeys"]["read_screen"] = self._read_hk_var.get()
        self.settings["hotkeys"]["stop"] = self._stop_hk_var.get()
        save_settings(self.settings)
        self._register_hotkeys()

    def _register_hotkeys(self):
        if self._hotkey_listener:
            self._hotkey_listener.stop()
            self._hotkey_listener = None

        bindings = {}
        read_spec = hotkey_to_pynput(self.settings["hotkeys"]["read_screen"])
        stop_spec = hotkey_to_pynput(self.settings["hotkeys"]["stop"])
        if read_spec and read_spec != stop_spec:
            bindings[read_spec] = lambda: self.after(0, self.cmd_read_screen)
        if stop_spec:
            bindings[stop_spec] = lambda: self.after(0, self.cmd_stop)

        if bindings:
            self._hotkey_listener = keyboard.GlobalHotKeys(bindings)
            self._hotkey_listener.start()
            log(f"hotkeys: read={read_spec!r} stop={stop_spec!r}")

    def _start_hotkey_capture(self, target: str):
        if self._capture_listener:
            return
        self._capture_target = target
        self._set_status("Press key combo…", ACCENT)

        def on_press(key):
            if key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r,
                       keyboard.Key.alt_l, keyboard.Key.alt_r,
                       keyboard.Key.shift_l, keyboard.Key.shift_r,
                       keyboard.Key.cmd, keyboard.Key.cmd_r):
                self._modifiers.add(key)
                return
            combo = format_hotkey_from_event(key, self._modifiers)
            if not combo:
                return
            self._finish_hotkey_capture(combo)

        def on_release(key):
            if key in self._modifiers:
                self._modifiers.discard(key)

        self._capture_listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        self._capture_listener.start()

    def _finish_hotkey_capture(self, combo: str):
        if self._capture_listener:
            self._capture_listener.stop()
            self._capture_listener = None
        other_key = "stop" if self._capture_target == "read_screen" else "read_screen"
        other_combo = self.settings["hotkeys"][other_key]
        if combo.lower() == other_combo.lower():
            self._set_status("Hotkey already used by other action", WARN)
            self._capture_target = None
            return
        if self._capture_target == "read_screen":
            self._read_hk_var.set(combo)
        else:
            self._stop_hk_var.set(combo)
        self._save_hotkeys()
        self._set_status(f"Hotkey set: {combo}", OK)
        self._capture_target = None

    def _ocr_log(self, stage: str, msg: str, _level: str = "INFO"):
        log(f"{stage} {msg}")

    def _update_hover_listener(self):
        if self._hover_listener:
            self._hover_listener.stop()
            self._hover_listener = None
        if self._hover_after_id:
            self.after_cancel(self._hover_after_id)
            self._hover_after_id = None
        self._pending_hover = None
        if not self.settings.get("hover_to_read"):
            return

        def on_move(x, y):
            self.after(0, lambda: self._schedule_hover_read(int(x), int(y)))

        self._hover_listener = mouse.Listener(on_move=on_move)
        self._hover_listener.start()
        log("hover-to-read enabled")

    def _schedule_hover_read(self, x: int, y: int):
        self._pending_hover = (x, y)
        if self._hover_after_id:
            self.after_cancel(self._hover_after_id)
        delay = int(self.settings.get("hover_delay_ms", 500))
        self._hover_after_id = self.after(delay, self._trigger_hover_read)

    def _trigger_hover_read(self):
        self._hover_after_id = None
        if not self._pending_hover:
            return
        x, y = self._pending_hover
        self._pending_hover = None
        threading.Thread(target=self._do_hover_read, args=(x, y), daemon=True).start()

    def _start_bus_listener(self):
        def _poll():
            while True:
                try:
                    entries, self._bus_offset = feature_bus.read_commands_after(self._bus_offset)
                    for entry in entries:
                        self.after(0, lambda e=entry: self._handle_bus_command(e))
                except Exception as e:
                    log(f"bus poll error: {e}")
                time.sleep(0.2)

        threading.Thread(target=_poll, daemon=True).start()

    def _handle_bus_command(self, entry: dict):
        cmd = entry.get("cmd")
        if cmd == "read_screen":
            self.cmd_read_screen()
        elif cmd == "stop":
            self.cmd_stop()
        elif cmd == "read_last":
            self.cmd_read_last()
        elif cmd == "read_section":
            text = entry.get("text", "")
            if text:
                self.cmd_read_section(text)

    def _remember_region(self, elements: list[dict]):
        if not elements:
            return
        x0 = min(e["x0"] for e in elements)
        y0 = min(e["y0"] for e in elements)
        x1 = max(e["x1"] for e in elements)
        y1 = max(e["y1"] for e in elements)
        texts = [e["text"] for e in elements]
        self._last_region = {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "texts": texts}
        self._last_region_ts = time.time()

    def cmd_read_screen(self):
        self._set_status("Scanning Chrome…", ACCENT)
        threading.Thread(target=self._do_read_screen, daemon=True).start()

    def _do_read_screen(self):
        try:
            if self.settings.get("use_groq_summary", True):
                try:
                    self._do_read_screen_groq()
                    return
                except Exception as e:
                    log(f"Groq summary failed, falling back to OCR: {e}")
            self._do_read_screen_ocr()
        except Exception as e:
            log(f"read_screen failed: {e}")
            self._set_status(f"Error: {e}", REC)

    def _do_read_screen_groq(self):
        key = os.getenv("GROQ_API_KEY")
        if not key:
            raise RuntimeError("GROQ_API_KEY required for Groq page summary")
        self._set_status("Analyzing Chrome with AI…", ACCENT)
        img = screen_ocr.capture_chrome_screenshot()
        client = self._groq_client()
        script = groq_vision.summarize_page_image(client, img)
        lines = groq_vision.script_to_lines(script)
        if not lines:
            self._set_status("Nothing important found in Chrome", WARN)
            return
        self._last_region = {"texts": lines, "x0": 0, "y0": 0, "x1": 0, "y1": 0}
        self._last_region_ts = time.time()
        self._set_status("Reading summary…", ACCENT)
        self._speaker.speak_lines(lines)
        self._set_status(f"Read {len(lines)} summary part(s)", OK)

    def _do_read_screen_ocr(self):
        elements = screen_ocr.capture_chrome_elements(log_fn=self._ocr_log)
        lines = screen_ocr.all_reading_order(elements)
        if not lines:
            self._set_status("No text found in Chrome", WARN)
            return
        self._remember_region(elements)
        self._set_status("Reading Chrome…", ACCENT)
        self._speaker.speak_lines(lines)
        self._set_status(f"Read {len(lines)} lines from Chrome", OK)

    def cmd_stop(self):
        self._speaker.stop()
        self._set_status("Stopped", OK)

    def cmd_read_last(self):
        if not self._last_region:
            self._set_status("Nothing to re-read yet", WARN)
            return
        if time.time() - self._last_region_ts > 30:
            threading.Thread(target=self._do_read_last_refresh, daemon=True).start()
        else:
            texts = self._last_region["texts"]
            self._set_status("Reading last section…", ACCENT)
            self._speaker.speak_lines(texts)
            self._set_status("Done", OK)

    def _do_read_last_refresh(self):
        try:
            region = self._last_region
            elements = screen_ocr.capture_chrome_elements(log_fn=self._ocr_log)
            matched = screen_ocr.elements_in_region(
                elements, region["x0"], region["y0"], region["x1"], region["y1"])
            texts = [e["text"] for e in matched] if matched else region["texts"]
            self._remember_region(matched or [])
            if not texts:
                self._set_status("Last section no longer visible", WARN)
                return
            self._set_status("Reading last section…", ACCENT)
            self._speaker.speak_lines(texts)
            self._set_status("Done", OK)
        except Exception as e:
            self._set_status(f"Error: {e}", REC)

    def _do_hover_read(self, x: int, y: int):
        try:
            region = screen_ocr.region_around_point(x, y, pad_w=720, pad_h=280)
            elements = screen_ocr.capture_screen_elements(
                log_fn=self._ocr_log, region=region)
            el = screen_ocr.elements_at_point(elements, x, y)
            if not el:
                return
            text = el["text"].strip()
            if not text or text == self._last_hover_text:
                return
            self._last_hover_text = text
            self._remember_region([el])
            self._speaker.stop()
            self._speaker.speak_lines([text])
            preview = text if len(text) <= 48 else text[:45] + "…"
            self._set_status(f"Hover: {preview}", OK)
        except Exception as e:
            self._set_status(f"Error: {e}", REC)

    def cmd_read_section(self, query: str):
        if not self.settings.get("voice_guided", True):
            self._set_status("Voice-guided read is disabled", WARN)
            return
        self._set_status(f'Finding "{query}"…', ACCENT)
        threading.Thread(target=self._do_read_section, args=(query,), daemon=True).start()

    def _do_read_section(self, query: str):
        try:
            elements = screen_ocr.capture_chrome_elements(log_fn=self._ocr_log)
            shown = elements[:MAX_ELEMENTS]
            if not shown:
                self._set_status("No text on screen", WARN)
                return
            indices = self._ask_groq_section(query, shown)
            if not indices:
                self._set_status("No matching section found", WARN)
                return
            picked = [shown[i] for i in indices if 0 <= i < len(shown)]
            texts = [e["text"] for e in picked]
            self._remember_region(picked)
            self._set_status("Reading section…", ACCENT)
            self._speaker.speak_lines(texts)
            self._set_status("Done", OK)
        except Exception as e:
            log(f"read_section failed: {e}")
            self._set_status(f"Error: {e}", REC)

    def _ask_groq_section(self, query: str, elements: list[dict]) -> list[int]:
        lines = "\n".join(f'  [{i}] "{e["text"]}"' for i, e in enumerate(elements))
        user_msg = f'Read request: "{query}"\n\nVisible lines:\n{lines}'
        client = self._groq_client()
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": READ_SECTION_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0,
            max_tokens=128,
        )
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw.strip())
        indices = data.get("indices", [])
        return [i for i in indices if isinstance(i, int)]

    def _shutdown(self, signum=None, frame=None):
        log("shutting down")
        self._speaker.shutdown()
        if self._hotkey_listener:
            self._hotkey_listener.stop()
        if self._hover_listener:
            self._hover_listener.stop()
        if self._capture_listener:
            self._capture_listener.stop()
        feature_bus.remove_presence("page_reader")
        self.after(0, self.destroy)
        sys.exit(0)


def main():
    log("starting")
    hint = plat.permission_hints("page_reader")
    if hint:
        log(hint)
    app = PageReaderApp()
    app.mainloop()
    feature_bus.remove_presence("page_reader")
    log("exited")


if __name__ == "__main__":
    main()
