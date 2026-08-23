"""Reading Level Summarizer — summarize the Chrome page at a chosen reading level, speak it.

Runs as its own process when toggled ON in the hub. See features/README.md.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import queue
import signal
import sys
import threading
import time
from pathlib import Path

import edge_tts
import sounddevice as sd
import soundfile as sf
import tkinter as tk
from dotenv import load_dotenv
from groq import Groq
from pynput import keyboard
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
GROQ_TIMEOUT = 30
DEFAULT_TTS_VOICE = "en-US-AvaMultilingualNeural"

READING_LEVELS = {
    "5th_grade": ("5th grade", "Explain it the way you would to a 10-year-old: "
                                "short sentences, common words, no jargon."),
    "middle_school": ("Middle school", "Use clear, plain language a middle-schooler "
                                        "would understand; define any unavoidable technical terms."),
    "plain_adult": ("Plain adult", "Rewrite in plain, jargon-free adult language "
                                    "— clear and concise, no unnecessary complexity."),
}
LEVEL_ORDER = ["5th_grade", "middle_school", "plain_adult"]

BG = "#1a1a2e"
CARD = "#23233f"
FG = "#e0e0ff"
MUTED = "#8a8ab0"
ACCENT = "#748ffc"
OK = "#69db7c"
WARN = "#ffd166"
REC = "#ff6b6b"

WIN_W, WIN_H = 300, 232


def log(msg: str):
    console.safe_print(f"[reading_level_summarizer] {msg}", flush=True)


def default_settings() -> dict:
    return {
        "reading_level": "middle_school",
        "hotkeys": {"summarize": "F7", "stop": "F8"},
        "tts_rate": 145,
        "tts_volume": 1.0,
        "tts_voice": DEFAULT_TTS_VOICE,
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


def hotkey_to_pynput(spec: str) -> str | None:
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


class Speaker:
    """Killable TTS — neural voices via edge-tts, played through sounddevice."""

    _STOP = object()

    def __init__(self, rate: int = 145, volume: float = 1.0, voice: str = DEFAULT_TTS_VOICE):
        self._rate = rate
        self._volume = volume
        self._voice = voice
        self._gen = 0
        self._q: queue.Queue = queue.Queue()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _edge_rate(self) -> str:
        pct = max(-50, min(50, round((self._rate - 175) / 175 * 100)))
        return f"{pct:+d}%"

    async def _synth(self, text: str) -> bytes:
        communicate = edge_tts.Communicate(text, voice=self._voice, rate=self._edge_rate())
        buf = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.extend(chunk["data"])
        return bytes(buf)

    def _run(self):
        asyncio.set_event_loop(self._loop)
        while True:
            item = self._q.get()
            if item is self._STOP:
                break
            if not item:
                continue
            gen = self._gen
            try:
                mp3 = self._loop.run_until_complete(self._synth(item))
                data, sr_ = sf.read(io.BytesIO(mp3), dtype="float32")
            except Exception as e:
                log(f"TTS synth error: {e}")
                continue
            if gen != self._gen or data is None or len(data) == 0:
                continue
            data = data * self._volume
            sd.play(data, sr_)
            while True:
                stream = sd.get_stream()
                if stream is None or not stream.active:
                    break
                if gen != self._gen:
                    sd.stop()
                    break
                time.sleep(0.03)

    def speak_lines(self, lines: list[str]):
        self.stop()
        gen = self._gen
        for line in lines:
            if line.strip() and gen == self._gen:
                self._q.put(line.strip())

    def stop(self):
        self._gen += 1
        sd.stop()
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass

    def shutdown(self):
        self.stop()
        self._q.put(self._STOP)


class SummarizerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self._hotkey_listener = None
        self._groq: Groq | None = None

        self._speaker = Speaker(
            rate=int(self.settings.get("tts_rate", 145)),
            volume=float(self.settings.get("tts_volume", 1.0)),
            voice=self.settings.get("tts_voice", DEFAULT_TTS_VOICE),
        )

        self._build_ui()
        save_settings(self.settings)
        self._update_presence()
        self._register_hotkeys()

        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT, self._shutdown)

    def _groq_client(self) -> Groq:
        if self._groq is None:
            key = os.getenv("GROQ_API_KEY")
            if not key:
                raise RuntimeError("GROQ_API_KEY required for reading-level summaries")
            self._groq = Groq(api_key=key, timeout=GROQ_TIMEOUT, max_retries=1)
        return self._groq

    def _build_ui(self):
        self.title("Reading Level Summarizer")
        self.resizable(False, False)
        self.configure(bg=BG)
        self.attributes("-topmost", True)
        self.geometry(f"{WIN_W}x{WIN_H}")

        tk.Label(self, text="Reading Level Summarizer",
                 font=tkfont.Font(family="Helvetica", size=11, weight="bold"),
                 fg=FG, bg=BG).pack(anchor="w", padx=14, pady=(10, 4))

        tk.Label(self, text="Reading level", fg=MUTED, bg=BG,
                 font=tkfont.Font(family="Helvetica", size=9)).pack(anchor="w", padx=14, pady=(4, 0))

        self._level_var = tk.StringVar(value=self.settings.get("reading_level", "middle_school"))
        levels = tk.Frame(self, bg=BG)
        levels.pack(anchor="w", padx=14, pady=(2, 8))
        for level_id in LEVEL_ORDER:
            label, _ = READING_LEVELS[level_id]
            tk.Radiobutton(levels, text=label, variable=self._level_var, value=level_id,
                           command=self._on_level_change,
                           font=tkfont.Font(family="Helvetica", size=9),
                           fg=FG, bg=BG, selectcolor=CARD, activebackground=BG,
                           activeforeground=FG).pack(anchor="w")

        hk_frame = tk.Frame(self, bg=BG)
        hk_frame.pack(fill="x", padx=14, pady=(4, 0))
        self._sum_hk_var = tk.StringVar(value=self.settings["hotkeys"]["summarize"])
        self._stop_hk_var = tk.StringVar(value=self.settings["hotkeys"]["stop"])
        self._add_hotkey_row(hk_frame, "Summarize:", self._sum_hk_var, "summarize")
        self._add_hotkey_row(hk_frame, "Stop:", self._stop_hk_var, "stop")

        self.status_var = tk.StringVar(value="Idle")
        self.status_label = tk.Label(self, textvariable=self.status_var,
                 font=tkfont.Font(family="Helvetica", size=12, weight="bold"),
                 fg=MUTED, bg=BG, wraplength=WIN_W - 28, justify="left", anchor="w")
        self.status_label.pack(fill="x", padx=14, pady=(10, 8))

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

    def _update_presence(self):
        feature_bus.update_presence("reading_level_summarizer", os.getpid())

    def _on_level_change(self):
        self.settings["reading_level"] = self._level_var.get()
        save_settings(self.settings)

    def _register_hotkeys(self):
        if self._hotkey_listener:
            self._hotkey_listener.stop()
            self._hotkey_listener = None
        bindings = {}
        sum_spec = hotkey_to_pynput(self.settings["hotkeys"]["summarize"])
        stop_spec = hotkey_to_pynput(self.settings["hotkeys"]["stop"])
        if sum_spec and sum_spec != stop_spec:
            bindings[sum_spec] = lambda: self.after(0, self.cmd_summarize)
        if stop_spec:
            bindings[stop_spec] = lambda: self.after(0, self.cmd_stop)
        if bindings:
            self._hotkey_listener = keyboard.GlobalHotKeys(bindings)
            self._hotkey_listener.start()
            log(f"hotkeys: summarize={sum_spec!r} stop={stop_spec!r}")

    def _start_hotkey_capture(self, target: str):
        self._set_status("Press key combo…", ACCENT)
        modifiers: set = set()

        def on_press(key):
            if key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r,
                       keyboard.Key.alt_l, keyboard.Key.alt_r,
                       keyboard.Key.shift_l, keyboard.Key.shift_r,
                       keyboard.Key.cmd, keyboard.Key.cmd_r):
                modifiers.add(key)
                return
            parts = []
            if keyboard.Key.ctrl_l in modifiers or keyboard.Key.ctrl_r in modifiers:
                parts.append("ctrl")
            if keyboard.Key.alt_l in modifiers or keyboard.Key.alt_r in modifiers:
                parts.append("alt")
            if keyboard.Key.shift_l in modifiers or keyboard.Key.shift_r in modifiers:
                parts.append("shift")
            if hasattr(key, "char") and key.char and key.char.isprintable():
                parts.append(key.char.lower())
            elif hasattr(key, "name") and key.name:
                name = key.name.replace("_l", "").replace("_r", "")
                if name.startswith("f") and name[1:].isdigit():
                    parts.append(name.upper())
                else:
                    parts.append(name)
            combo = "+".join(parts)
            if not combo:
                return
            listener.stop()
            self._finish_hotkey_capture(target, combo)

        listener = keyboard.Listener(on_press=on_press)
        listener.start()

    def _finish_hotkey_capture(self, target: str, combo: str):
        other_key = "stop" if target == "summarize" else "summarize"
        other_combo = self.settings["hotkeys"][other_key]
        if combo.lower() == other_combo.lower():
            self._set_status("Hotkey already used by other action", WARN)
            return
        if target == "summarize":
            self._sum_hk_var.set(combo)
        else:
            self._stop_hk_var.set(combo)
        self.settings["hotkeys"]["summarize"] = self._sum_hk_var.get()
        self.settings["hotkeys"]["stop"] = self._stop_hk_var.get()
        save_settings(self.settings)
        self._register_hotkeys()
        self._set_status(f"Hotkey set: {combo}", OK)

    def cmd_summarize(self):
        self._set_status("Reading page…", ACCENT)
        threading.Thread(target=self._do_summarize, daemon=True).start()

    def _do_summarize(self):
        try:
            key = os.getenv("GROQ_API_KEY")
            if not key:
                self._set_status("GROQ_API_KEY required", WARN)
                return
            img = screen_ocr.capture_chrome_screenshot()
            client = self._groq_client()
            level_id = self.settings.get("reading_level", "middle_school")
            _, instruction = READING_LEVELS[level_id]
            self._set_status("Summarizing…", ACCENT)
            script = groq_vision.summarize_page_image(client, img, level_instruction=instruction)
            lines = groq_vision.script_to_lines(script)
            if not lines:
                self._set_status("Nothing to summarize", WARN)
                return
            self._set_status("Reading summary…", ACCENT)
            self._speaker.speak_lines(lines)
            self._set_status("Done", OK)
        except Exception as e:
            log(f"summarize failed: {e}")
            self._set_status(f"Error: {e}", REC)

    def cmd_stop(self):
        self._speaker.stop()
        self._set_status("Stopped", OK)

    def _shutdown(self, signum=None, frame=None):
        log("shutting down")
        self._speaker.shutdown()
        if self._hotkey_listener:
            self._hotkey_listener.stop()
        feature_bus.remove_presence("reading_level_summarizer")
        self.after(0, self.destroy)
        sys.exit(0)


def main():
    log("starting")
    hint = plat.permission_hints("reading_level_summarizer")
    if hint:
        log(hint)
    app = SummarizerApp()
    app.mainloop()
    feature_bus.remove_presence("reading_level_summarizer")
    log("exited")


if __name__ == "__main__":
    main()
