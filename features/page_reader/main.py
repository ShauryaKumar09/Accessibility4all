"""Page Reader — OCR on-screen text and speak it aloud.

Runs as its own process when toggled ON in the hub. See features/README.md.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import queue
import re
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
from pynput import keyboard, mouse

FEATURE_DIR = Path(__file__).resolve().parent
ROOT = FEATURE_DIR.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import (console, feature_bus, groq_models, groq_vision,  # noqa: E402
                    platform as plat, pynput_darwin, screen_ocr,
                    settings_store as store, ui_kit as ui)
from shared.ui_kit import C  # noqa: E402

console.configure_stdio()
plat.enable_dpi_awareness()
pynput_darwin.install()
load_dotenv()

SETTINGS_FILE = FEATURE_DIR / "settings.json"
GROQ_MODEL = groq_models.TEXT_MODEL
GROQ_TIMEOUT = 30
MAX_ELEMENTS = 120
DEFAULT_TTS_VOICE = "en-US-AvaMultilingualNeural"

READING_LEVELS = {
    "Normal": "",
    "5th grade": ("Explain it the way you would to a 10-year-old: "
                  "short sentences, common words, no jargon."),
    "Middle school": ("Use clear, plain language a middle-schooler would understand; "
                       "define any unavoidable technical terms."),
    "Plain adult": ("Rewrite in plain, jargon-free adult language "
                     "— clear and concise, no unnecessary complexity."),
}
READ_SECTION_PROMPT = """You pick which OCR lines to read aloud for a blind user.

You get a numbered list of EXACT text lines from the screen and what the user asked to hear.

Return ONLY JSON: {"indices": [<int>, ...]}

Rules:
- Pick lines whose text actually matches the request (same topic, heading, or keywords).
- Return ALL consecutive matching lines as one block (include lines directly above/below the heading).
- Use only indices from the list. If nothing matches, return {"indices": []}.
- Do not invent text — only pick indices."""

_SECTION_SKIP = re.compile(
    r"^(home|shorts|subscriptions|explore|library|history|sign in|menu|search|"
    r"images?|videos?|news|shopping|maps|more|tools|all|about|ai overview)$",
    re.I,
)

# The bubble replaces the old 300x248 window: one button, one line, one bar.
BG = C["BUBBLE"]
FG = C["FG_BUBBLE"]
MUTED = C["FG_MUTED"]
ACCENT = C["ACCENT"]
OK = C["ON"]
WARN = C["WARM_TEXT"]
REC = C["STOP_BORDER"]

BUBBLE_H = 64
BUTTON_D = 48
COLUMN_W = 260
PAD = 12
GAP = 16
WIN_W = PAD * 2 + BUTTON_D + GAP + COLUMN_W
WIN_H = BUBBLE_H
VC_H = 60                 # the voice bubble's height, for stacking
MARGIN = 12
IDLE_LINE = "Press {key} to read the screen"
SETTINGS_WATCH_MS = 700


def log(msg: str):
    console.safe_print(f"[page_reader] {msg}", flush=True)


def default_settings() -> dict:
    return dict(store.DEFAULTS["page_reader"])


def load_settings() -> dict:
    s = default_settings()
    if SETTINGS_FILE.exists():
        try:
            loaded = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            s.update(loaded)
            s["hotkeys"] = {**default_settings()["hotkeys"], **loaded.get("hotkeys", {})}
            if "hover_to_read" not in loaded and loaded.get("click_to_read"):
                s["hover_to_read"] = True
            if loaded.get("tts_rate", 145) < 120:
                s["tts_rate"] = 145
                save_settings(s)
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


def _norm_words(s: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) > 2]


def _match_section_local(query: str, elements: list[dict]) -> list[dict] | None:
    """Fuzzy match query to on-screen lines; expand to nearby paragraph lines."""
    qwords = _norm_words(query)
    if not qwords:
        return None
    best_i, best_score = -1, 0.0
    for i, e in enumerate(elements):
        t = e.get("text", "")
        if _SECTION_SKIP.match(t.strip()):
            continue
        words = _norm_words(t)
        if not words:
            continue
        hits = sum(1 for w in qwords if any(w in tw or tw in w for tw in words))
        score = hits / len(qwords)
        if any(w in t.lower() for w in qwords):
            score += 0.25
        if score > best_score:
            best_score, best_i = score, i
    if best_i < 0 or best_score < 0.45:
        return None
    ordered = sorted(elements, key=lambda e: (e["y0"], e["x0"]))
    seed = elements[best_i]
    si = next(i for i, e in enumerate(ordered) if e is seed or e["text"] == seed["text"])
    lo, hi = si, si
    line_h = max(ordered[si]["y1"] - ordered[si]["y0"], 14)
    while lo > 0 and ordered[lo]["y0"] - ordered[lo - 1]["y1"] <= line_h * 2.5:
        lo -= 1
    while hi + 1 < len(ordered) and ordered[hi + 1]["y0"] - ordered[hi]["y1"] <= line_h * 2.5:
        hi += 1
    return ordered[lo:hi + 1]


class Speaker:
    """Killable TTS — neural voices via edge-tts, played through sounddevice."""

    _STOP = object()

    def __init__(self, rate: int = 145, volume: float = 1.0, voice: str = DEFAULT_TTS_VOICE):
        self._rate = rate
        self._volume = volume
        self._voice = voice
        self._gen = 0
        # What is being read, so the bubble can show the line and how far in we
        # are, and so pause can resume from the same place.
        self._lines: list[str] = []
        self._pos = 0
        self._paused = False
        self._busy = False
        self.on_progress = None        # called from the TTS thread: (pos, total, text)
        self._q: queue.Queue = queue.Queue()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._started = False

    def start(self):
        """Start the TTS worker (deferred until the window exists, see call site)."""
        if not self._started:
            self._started = True
            self._thread.start()

    def _edge_rate(self) -> str:
        """Map configured rate (~80-220 wpm) to edge-tts percent (0% = ~175 wpm)."""
        pct = max(-50, min(50, round((self._rate - 175) / 175 * 100)))
        return f"{pct:+d}%"

    async def _synth(self, text: str) -> bytes:
        communicate = edge_tts.Communicate(text, voice=self._voice, rate=self._edge_rate())
        buf = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.extend(chunk["data"])
        return bytes(buf)

    def _report(self, pos: int, text: str):
        self._pos = pos
        if self.on_progress:
            try:
                self.on_progress(pos, len(self._lines), text)
            except Exception:
                pass

    def _run(self):
        asyncio.set_event_loop(self._loop)
        while True:
            item = self._q.get()
            if item is self._STOP:
                self._loop.close()
                break
            if not item:
                continue
            pos, text = item
            gen = self._gen
            self._busy = True
            self._report(pos, text)
            try:
                mp3 = self._loop.run_until_complete(self._synth(text))
                data, sr_ = sf.read(io.BytesIO(mp3), dtype="float32")
            except Exception as e:
                log(f"TTS synth error: {e}")
                self._busy = False
                continue
            if gen != self._gen or data is None or len(data) == 0:
                self._busy = False
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
            self._busy = False

    def configure(self, rate: int, volume: float, voice: str | None = None):
        self._rate, self._volume = rate, volume
        if voice:
            self._voice = voice

    def speak_lines(self, lines: list[str]):
        self.stop()
        self._lines = [line.strip() for line in lines if line.strip()]
        self._pos = 0
        self._paused = False
        self._enqueue_from(0)

    def speak_one(self, text: str):
        """Stop everything and speak a single line (hover)."""
        self.speak_lines([text])

    def _enqueue_from(self, start: int):
        gen = self._gen
        for i in range(start, len(self._lines)):
            if gen != self._gen:
                return
            self._q.put((i, self._lines[i]))

    def _halt(self):
        """Silence the engine now, without forgetting what we were reading."""
        self._gen += 1
        sd.stop()
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass
        self._busy = False

    def pause(self):
        if self._paused or not self._lines:
            return
        self._paused = True
        self._halt()

    def resume(self):
        if not self._paused:
            return
        self._paused = False
        self._enqueue_from(self._pos)

    def toggle_pause(self):
        self.resume() if self._paused else self.pause()

    @property
    def paused(self) -> bool:
        return self._paused

    def state(self) -> str:
        """`reading`, `paused`, or `idle` — what the bubble button shows."""
        if self._paused:
            return "paused"
        if self._busy or not self._q.empty():
            return "reading"
        return "idle"

    def progress(self) -> tuple[int, int]:
        return self._pos + 1 if self._lines else 0, len(self._lines)

    def stop(self):
        self._halt()
        self._lines = []
        self._pos = 0
        self._paused = False

    def shutdown(self):
        self.stop()
        self._q.put(self._STOP)
        if self._started:
            self._thread.join(timeout=2)


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
        self._hover_gen = 0
        self._last_hover_text = ""
        self._last_hover_pos: tuple[int, int] | None = None
        self._groq: Groq | None = None
        self._watcher = store.Watcher("page_reader")

        self._speaker = Speaker(
            rate=int(self.settings.get("tts_rate", 145)),
            volume=float(self.settings.get("tts_volume", 1.0)),
            voice=self.settings.get("tts_voice", DEFAULT_TTS_VOICE),
        )

        self._build_ui()
        # the speech engine only starts once the window exists (see Speaker.start)
        self.after(400, self._speaker.start)
        save_settings(self.settings)
        self._position_window()
        self._update_presence()
        self._register_hotkeys()
        self._update_hover_listener()
        self._start_bus_listener()
        self.after(SETTINGS_WATCH_MS, self._watch_settings)
        self.after(120, lambda: ui.raise_bubble(self))

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
        """The bubble: a pause/resume button, the line being read, a progress bar.

        Everything that used to be here — three checkboxes and two hotkey rows —
        now lives in the hub's Page Reader settings sheet.
        """
        self.title("Page Reader")
        self.resizable(False, False)
        self.fonts = ui.FontSet(1.0)
        self._transparent = ui.make_bubble(self, WIN_W, WIN_H)
        self.canvas = ui.bubble_canvas(self, WIN_W, WIN_H, self._transparent)
        self.canvas.pack(fill="both", expand=True)

        self._button = ui.CircleButton(
            self.canvas, BUTTON_D, "play", command=self._on_button,
            fill=C["ACCENT_FILL"], border=C["ACCENT"], glyph=C["ACCENT_ON"])
        self.canvas.create_window(PAD, WIN_H / 2, window=self._button,
                                  anchor="w")

        self._line = self._idle_line()
        self._fraction = 0.0
        self._line_color = C["FG_BUBBLE"]
        self._speaker.on_progress = self._on_progress
        self._draw()

    def _idle_line(self) -> str:
        return IDLE_LINE.format(key=self.settings["hotkeys"]["read_screen"])

    def _draw(self):
        self.canvas.delete("bubble")
        ui.pill(self.canvas, 1, 1, WIN_W - 1, WIN_H - 1, fill=C["BUBBLE"],
                outline=C["BORDER_CTRL"], width=1, tags="bubble")
        self.canvas.tag_lower("bubble")

        col_x = PAD + BUTTON_D + GAP
        text = ui.ellipsize(self.fonts["ui"], self._line, COLUMN_W)
        self.canvas.create_text(col_x, WIN_H / 2 - 10, anchor="w", text=text,
                                font=self.fonts["ui"], fill=self._line_color,
                                tags="bubble")
        ui.progress_bar(self.canvas, col_x, WIN_H / 2 + 9, col_x + COLUMN_W, 5,
                        self._fraction, tags="bubble")
        state = self._speaker.state()
        self._button.set_style(kind="pause" if state == "reading" else "play")

    def _on_button(self):
        """One button: pause what is being read, or start the screen fresh."""
        state = self._speaker.state()
        if state == "idle":
            self.cmd_read_screen()
        else:
            self._speaker.toggle_pause()
            if self._speaker.paused:
                self._set_status("Paused", MUTED)
            else:
                self._set_status(self._line, C["FG_BUBBLE"])
        self._draw()

    def _on_progress(self, pos: int, total: int, text: str):
        """Called on the TTS thread — marshal onto the main thread."""
        def _apply():
            self._line = text
            self._line_color = C["FG_BUBBLE"]
            self._fraction = (pos + 1) / total if total else 0.0
            self._draw()
        self.after(0, _apply)

    def _watch_settings(self):
        """Pick up edits the hub made to settings.json while we were running."""
        if self._watcher.changed():
            self.settings = load_settings()
            log("settings changed in the hub — reapplying")
            self._speaker.configure(int(self.settings.get("tts_rate", 145)),
                                    float(self.settings.get("tts_volume", 1.0)),
                                    voice=self.settings.get("tts_voice", DEFAULT_TTS_VOICE))
            self._register_hotkeys()
            self._update_hover_listener()
            if self._speaker.state() == "idle":
                self._line = self._idle_line()
                self._draw()
        self.after(SETTINGS_WATCH_MS, self._watch_settings)

    def _set_status(self, msg: str, color: str = MUTED):
        def _apply():
            self._line = msg
            self._line_color = color
            if self._speaker.state() == "idle" and color in (OK, MUTED):
                self._fraction = 0.0
            self._draw()
        self.after(0, _apply)

    def _on_configure(self, _event=None):
        self.after(100, self._update_presence)

    def _position_window(self):
        """Bottom-centre, stacked above the Voice Control bubble when it's up."""
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        x = max(0, (sw - WIN_W) // 2)
        y = sh - WIN_H - 80

        presence = feature_bus.load_presence()
        vc = presence.get("voice_control")
        if vc and feature_bus.is_feature_running("voice_control"):
            win = vc.get("window") or {}
            vy = win.get("y", sh - VC_H - 80)
            y = vy - WIN_H - MARGIN

        self.geometry(f"{WIN_W}x{WIN_H}+{x}+{max(0, y)}")
        self.lift()

    def _update_presence(self):
        feature_bus.update_presence(
            "page_reader",
            os.getpid(),
            {"x": self.winfo_x(), "y": self.winfo_y(), "w": WIN_W, "h": WIN_H},
        )

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
        moved = (
            self._last_hover_pos is None
            or abs(x - self._last_hover_pos[0]) > 6
            or abs(y - self._last_hover_pos[1]) > 6
        )
        if moved:
            self._speaker.stop()
            self._hover_gen += 1
            self._last_hover_text = ""
        self._pending_hover = (x, y)
        if self._hover_after_id:
            self.after_cancel(self._hover_after_id)
        delay = int(self.settings.get("hover_delay_ms", 200))
        self._hover_after_id = self.after(delay, self._trigger_hover_read)

    def _trigger_hover_read(self):
        self._hover_after_id = None
        if not self._pending_hover:
            return
        x, y = self._pending_hover
        self._pending_hover = None
        gen = self._hover_gen
        threading.Thread(target=self._do_hover_read, args=(x, y, gen), daemon=True).start()

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
        level_instruction = READING_LEVELS.get(self.settings.get("reading_level", "Normal"), "")
        script = groq_vision.summarize_page_image(client, img, level_instruction=level_instruction)
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
        self._hover_gen += 1
        self._speaker.stop()
        self._last_hover_text = ""
        if self._hover_after_id:
            self.after_cancel(self._hover_after_id)
            self._hover_after_id = None
        self._pending_hover = None
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

    def _do_hover_read(self, x: int, y: int, gen: int):
        try:
            if gen != self._hover_gen:
                return
            region = screen_ocr.region_around_point(x, y, pad_w=720, pad_h=280)
            elements = screen_ocr.capture_screen_elements(
                log_fn=self._ocr_log, region=region)
            if gen != self._hover_gen:
                return
            el = screen_ocr.elements_at_point(elements, x, y)
            if not el:
                return
            text = el["text"].strip()
            if not text:
                return
            if text == self._last_hover_text and self._last_hover_pos:
                if abs(x - self._last_hover_pos[0]) <= 6 and abs(y - self._last_hover_pos[1]) <= 6:
                    return
            if gen != self._hover_gen:
                return
            self._last_hover_text = text
            self._last_hover_pos = (x, y)
            self._remember_region([el])
            self._speaker.speak_one(text)
            preview = text if len(text) <= 48 else text[:45] + "…"
            self._set_status(f"Hover: {preview}", OK)
        except Exception as e:
            if gen == self._hover_gen:
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
            if not elements:
                self._set_status("No text on screen", WARN)
                return
            picked = _match_section_local(query, elements)
            if picked:
                log(f"section local match: {len(picked)} lines for {query!r}")
            else:
                shown = elements[:MAX_ELEMENTS]
                indices = self._ask_groq_section(query, shown)
                if not indices:
                    self._set_status("No matching section found", WARN)
                    return
                picked = [shown[i] for i in indices if 0 <= i < len(shown)]
            texts = [e["text"] for e in picked]
            if not texts:
                self._set_status("No matching section found", WARN)
                return
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
            max_tokens=groq_models.MIN_MAX_TOKENS,
            **groq_models.TEXT_ARGS,
        )
        raw = groq_models.strip_reasoning(response.choices[0].message.content)
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
