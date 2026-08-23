"""Dictate — hold a hotkey to speak, release to type the transcription.

Runs as its own process when toggled ON in the hub. See features/README.md.
"""

from __future__ import annotations

import io
import json
import os
import signal
import sys
import threading
import wave
from pathlib import Path

import sounddevice as sd
import speech_recognition as sr
import tkinter as tk
from dotenv import load_dotenv
from groq import Groq
from pynput import keyboard
from tkinter import font as tkfont

FEATURE_DIR = Path(__file__).resolve().parent
ROOT = FEATURE_DIR.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import console, feature_bus, platform as plat  # noqa: E402

console.configure_stdio()
plat.enable_dpi_awareness()
load_dotenv()

SETTINGS_FILE = FEATURE_DIR / "settings.json"
GROQ_TIMEOUT = 30
STT_TIMEOUT = 15
GROQ_WHISPER_MODEL = "whisper-large-v3-turbo"

BG = "#1a1a2e"
CARD = "#23233f"
FG = "#e0e0ff"
MUTED = "#8a8ab0"
ACCENT = "#748ffc"
OK = "#69db7c"
WARN = "#ffd166"
REC = "#ff6b6b"

WIN_W, WIN_H = 300, 200


def log(msg: str):
    console.safe_print(f"[dictate] {msg}", flush=True)


def default_settings() -> dict:
    return {
        "stt_engine": "google",
        "hotkeys": {"talk": "F6"},
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


def _key_spec(k) -> str | None:
    """Normalize a pressed pynput key into the same string form as a stored hotkey."""
    if hasattr(k, "char") and k.char and k.char.isprintable():
        return k.char.lower()
    if hasattr(k, "name") and k.name:
        return k.name.replace("_l", "").replace("_r", "").lower()
    return None


class PushToTalkRecorder:
    """Records raw audio while the talk key is held (mirrors voice_control's recorder)."""

    SAMPLE_RATE = 16000
    SAMPLE_WIDTH = 2
    CHUNK = 1024
    MIN_SECONDS = 0.3

    def __init__(self):
        self._frames: list[bytes] = []
        self._stream = None

    def start(self):
        self._frames = []
        self._stream = sd.RawInputStream(
            samplerate=self.SAMPLE_RATE, channels=1, dtype="int16",
            blocksize=self.CHUNK, callback=self._cb,
        )
        self._stream.start()

    def _cb(self, indata, frames, time_info, status):
        self._frames.append(bytes(indata))

    def stop(self) -> sr.AudioData | None:
        if self._stream is None:
            return None
        self._stream.stop()
        self._stream.close()
        self._stream = None
        raw = b"".join(self._frames)
        min_bytes = int(self.SAMPLE_RATE * self.SAMPLE_WIDTH * self.MIN_SECONDS)
        if len(raw) < min_bytes:
            return None
        return sr.AudioData(raw, self.SAMPLE_RATE, self.SAMPLE_WIDTH)


def transcribe_google(audio: sr.AudioData) -> str:
    recognizer = sr.Recognizer()
    recognizer.operation_timeout = STT_TIMEOUT
    return recognizer.recognize_google(audio)


def audio_to_wav_bytes(audio: sr.AudioData) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(audio.sample_width)
        wf.setframerate(audio.sample_rate)
        wf.writeframes(audio.frame_data)
    return buf.getvalue()


def transcribe_groq(client: Groq, audio: sr.AudioData) -> str:
    wav_bytes = audio_to_wav_bytes(audio)
    resp = client.audio.transcriptions.create(
        model=GROQ_WHISPER_MODEL, file=("audio.wav", wav_bytes), timeout=GROQ_TIMEOUT,
    )
    return (resp.text or "").strip()


class DictateApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self._recorder = PushToTalkRecorder()
        self._key_listener = None
        self._holding = False
        self._groq: Groq | None = None

        self._build_ui()
        save_settings(self.settings)
        self._update_presence()
        self._register_hotkey()

        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT, self._shutdown)

    def _groq_client(self) -> Groq:
        if self._groq is None:
            key = os.getenv("GROQ_API_KEY")
            if not key:
                raise RuntimeError("GROQ_API_KEY required for Groq Whisper")
            self._groq = Groq(api_key=key, timeout=GROQ_TIMEOUT, max_retries=1)
        return self._groq

    def _build_ui(self):
        self.title("Dictate")
        self.resizable(False, False)
        self.configure(bg=BG)
        self.attributes("-topmost", True)
        self.geometry(f"{WIN_W}x{WIN_H}")

        tk.Label(self, text="Dictate",
                 font=tkfont.Font(family="Helvetica", size=11, weight="bold"),
                 fg=FG, bg=BG).pack(anchor="w", padx=14, pady=(10, 4))

        tk.Label(self, text="Engine", fg=MUTED, bg=BG,
                 font=tkfont.Font(family="Helvetica", size=9)).pack(anchor="w", padx=14, pady=(4, 0))
        self._engine_var = tk.StringVar(value=self.settings.get("stt_engine", "google"))
        engines = tk.Frame(self, bg=BG)
        engines.pack(anchor="w", padx=14, pady=(2, 8))
        for value, label in (("google", "Google (free)"), ("groq", "Groq Whisper")):
            tk.Radiobutton(engines, text=label, variable=self._engine_var, value=value,
                           command=self._on_engine_change,
                           font=tkfont.Font(family="Helvetica", size=9),
                           fg=FG, bg=BG, selectcolor=CARD, activebackground=BG,
                           activeforeground=FG).pack(anchor="w")

        hk_frame = tk.Frame(self, bg=BG)
        hk_frame.pack(fill="x", padx=14, pady=(4, 0))
        self._talk_hk_var = tk.StringVar(value=self.settings["hotkeys"]["talk"])
        row = tk.Frame(hk_frame, bg=BG)
        row.pack(fill="x", pady=2)
        tk.Label(row, text="Hold to talk:", width=12, anchor="w",
                 font=tkfont.Font(family="Helvetica", size=9),
                 fg=MUTED, bg=BG).pack(side="left")
        tk.Label(row, textvariable=self._talk_hk_var,
                 font=tkfont.Font(family="Helvetica", size=9, weight="bold"),
                 fg=FG, bg=BG, width=14, anchor="w").pack(side="left")
        tk.Button(row, text="Set", command=self._start_hotkey_capture,
                  font=tkfont.Font(family="Helvetica", size=8),
                  bg=CARD, fg=FG, relief="flat", padx=6, cursor="hand2").pack(side="right")

        self.status_var = tk.StringVar(value="Idle")
        self.status_label = tk.Label(self, textvariable=self.status_var,
                 font=tkfont.Font(family="Helvetica", size=12, weight="bold"),
                 fg=MUTED, bg=BG, wraplength=WIN_W - 28, justify="left", anchor="w")
        self.status_label.pack(fill="x", padx=14, pady=(10, 8))

    def _set_status(self, msg: str, color: str = MUTED):
        def _apply():
            self.status_var.set(msg)
            self.status_label.configure(fg=color)
        self.after(0, _apply)

    def _update_presence(self):
        feature_bus.update_presence("dictate", os.getpid())

    def _on_engine_change(self):
        self.settings["stt_engine"] = self._engine_var.get()
        save_settings(self.settings)

    def _register_hotkey(self):
        if self._key_listener:
            self._key_listener.stop()
            self._key_listener = None
        target = self.settings["hotkeys"]["talk"].strip().lower()
        if not target:
            return

        def on_press(k):
            if _key_spec(k) == target and not self._holding:
                self._holding = True
                self.after(0, self._on_talk_start)

        def on_release(k):
            if _key_spec(k) == target and self._holding:
                self._holding = False
                self.after(0, self._on_talk_stop)

        self._key_listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        self._key_listener.start()
        log(f"hold-to-talk key: {target!r}")

    def _start_hotkey_capture(self):
        self._set_status("Press a key…", ACCENT)

        def on_press(k):
            spec = _key_spec(k)
            if not spec:
                return
            listener.stop()
            self.after(0, lambda: self._finish_hotkey_capture(spec))

        listener = keyboard.Listener(on_press=on_press)
        listener.start()

    def _finish_hotkey_capture(self, spec: str):
        self._talk_hk_var.set(spec.upper() if len(spec) > 1 else spec)
        self.settings["hotkeys"]["talk"] = spec
        save_settings(self.settings)
        self._register_hotkey()
        self._set_status(f"Hotkey set: {spec}", OK)

    def _on_talk_start(self):
        self._set_status("Recording…", REC)
        self._recorder.start()

    def _on_talk_stop(self):
        audio = self._recorder.stop()
        if audio is None:
            self._set_status("Idle", MUTED)
            return
        self._set_status("Transcribing…", ACCENT)
        threading.Thread(target=self._do_transcribe, args=(audio,), daemon=True).start()

    def _do_transcribe(self, audio: sr.AudioData):
        try:
            engine = self.settings.get("stt_engine", "google")
            if engine == "groq":
                text = transcribe_groq(self._groq_client(), audio)
            else:
                text = transcribe_google(audio)
            text = (text or "").strip()
            if not text:
                self._set_status("Didn't catch that", WARN)
                return
            plat.paste_text(text)
            preview = text if len(text) <= 60 else text[:57] + "…"
            self._set_status(f"Typed: {preview}", OK)
        except sr.UnknownValueError:
            self._set_status("Didn't catch that", WARN)
        except Exception as e:
            log(f"transcribe failed: {e}")
            self._set_status(f"Error: {e}", REC)

    def _shutdown(self, signum=None, frame=None):
        log("shutting down")
        if self._key_listener:
            self._key_listener.stop()
        feature_bus.remove_presence("dictate")
        self.after(0, self.destroy)
        sys.exit(0)


def main():
    log("starting")
    hint = plat.permission_hints("dictate")
    if hint:
        log(hint)
    app = DictateApp()
    app.mainloop()
    feature_bus.remove_presence("dictate")
    log("exited")


if __name__ == "__main__":
    main()
