
# Suppress audioop deprecation warning from SpeechRecognition (removed in Py 3.13)
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning, message=".*audioop.*")

import os
import re
import sys
import json
import time
import subprocess
import threading
import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd
import pyautogui
import speech_recognition as sr
import pytesseract
import tkinter as tk
from tkinter import font as tkfont
from groq import Groq
from dotenv import load_dotenv

load_dotenv()
pyautogui.FAILSAFE = False

# ── Trial logging ─────────────────────────────────────────────────────────────
TRIAL_LOG = Path(__file__).parent / "trials.jsonl"

def _log_trial(entry: dict):
    entry["ts"] = datetime.datetime.now().isoformat()
    with open(TRIAL_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")

# ── Terminal step logging ──────────────────────────────────────────────────────
# Prints every pipeline step to the terminal with a wall-clock timestamp so we
# can see exactly where the process is and how long each stage takes. This is
# what tells us *where* a "stuck on Processing" hang is actually happening.
_LOG_LOCK = threading.Lock()

def log(stage: str, msg: str = "", level: str = "INFO"):
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] [{level:<5}] {stage:<14} {msg}".rstrip()
    with _LOG_LOCK:
        print(line, flush=True)

class Timer:
    """Context manager that logs the start, end, and duration of a stage.

    Usage:
        with Timer("TRANSCRIBE"):
            command = transcribe(audio)

    On exit it prints how long the block took. If the block raises, it logs the
    error with the elapsed time so we can see which stage failed and when.
    """
    def __init__(self, stage: str, detail: str = ""):
        self.stage = stage
        self.detail = detail

    def __enter__(self):
        self.t0 = time.perf_counter()
        log(self.stage, f"START {self.detail}".rstrip())
        return self

    def __exit__(self, exc_type, exc, tb):
        dt = time.perf_counter() - self.t0
        if exc_type is None:
            log(self.stage, f"DONE  ({dt:.2f}s)")
        else:
            log(self.stage, f"FAILED ({dt:.2f}s) -> {exc_type.__name__}: {exc}",
                level="ERROR")
        return False  # never suppress the exception

# ── Constants ─────────────────────────────────────────────────────────────────
GROQ_MODEL = "llama-3.3-70b-versatile"
STT_TIMEOUT = 15          # seconds — max wait for Google speech-to-text
GROQ_TIMEOUT = 30         # seconds — max wait for the Groq API call
SYSTEM_PROMPT = """You are an accessibility assistant that controls a Mac (macOS) for disabled users, mostly inside Google Chrome.
The user gives you a voice command plus a list of visible text elements on the screen with their (x, y) positions. The screen is {screen_w} wide by {screen_h} tall; its center is about ({center_x}, {center_y}).

Your job is to decide what action to take and return ONLY a valid JSON object — no explanation, no markdown, no extra text.

The screen elements are given to you as a NUMBERED list, like `[7] "Sign in" at (412, 88)`.

Supported actions:
- {{"action": "hotkey", "keys": [<str>, ...]}}
- {{"action": "click", "index": <int>}}        ← click a listed element BY ITS NUMBER (preferred for clicks)
- {{"action": "scroll", "direction": "up"|"down"|"left"|"right", "amount": <int>}}
- {{"action": "type", "text": "<str>"}}

IMPORTANT — prefer keyboard shortcuts. This is a Mac, so the modifier key is "command" (NOT "ctrl"). For common browser actions ALWAYS use a hotkey instead of clicking:
- New tab: {{"action": "hotkey", "keys": ["command", "t"]}}
- Close tab: {{"action": "hotkey", "keys": ["command", "w"]}}
- Reopen closed tab: {{"action": "hotkey", "keys": ["command", "shift", "t"]}}
- New window: {{"action": "hotkey", "keys": ["command", "n"]}}
- Next/previous tab: ["command", "option", "right"] / ["command", "option", "left"]
- Reload page: ["command", "r"]
- Go back / forward: ["command", "["] / ["command", "]"]
- Focus the address bar (to type a URL or search): ["command", "l"]
- Find on page: ["command", "f"]
- Scroll to top / bottom: use the scroll action.

Rules:
- If the command is a common browser/system action, use a hotkey. Use "click" ONLY when the user clearly refers to a specific visible label/button that has no keyboard shortcut.
- To navigate to a website, return a hotkey to focus the address bar, OR a "type" action with the URL when the bar is already focused. Keep it to one action.
- When clicking, pick the element from the numbered list whose text best matches the user's intent and return its NUMBER as "index". NEVER invent coordinates — only choose from the numbered elements you were given.
- The elements are listed in reading order (roughly top-to-bottom, left-to-right). For ordinal/positional requests like "the first video", "the second link", "the top result", choose by that order among the relevant items. "The first video" usually means the first content title near the top of the list.
- For "click on this/that <thing>", choose the element whose text names that thing (e.g. a video title, button label, or link text).
- If NOTHING in the list matches the user's intent, do not guess a click. Prefer a hotkey, or return {{"action": "click", "index": -1}} to signal "no match".
- Return ONLY the raw JSON object.
"""

# ── Push-to-talk recorder (sounddevice; no PyAudio, no Tk from audio thread) ──
class PushToTalkRecorder:
    """Records raw audio while the talk button is held.

    The audio callback runs on PortAudio's thread, so it MUST NOT touch Tk.
    It only appends frames and stores the latest RMS level (a plain float
    assignment, atomic under the GIL); the Tk main thread polls `.level`.
    """
    SAMPLE_RATE = 16000
    SAMPLE_WIDTH = 2          # int16 = 2 bytes
    CHUNK = 1024
    MIN_SECONDS = 0.3         # ignore accidental taps shorter than this

    def __init__(self):
        self._frames: list[bytes] = []
        self._stream = None
        self.level = 0.0       # latest RMS — read by main thread only

    def start(self):
        self._frames = []
        self.level = 0.0
        log("RECORD", "opening microphone stream")
        self._stream = sd.RawInputStream(
            samplerate=self.SAMPLE_RATE, channels=1, dtype="int16",
            blocksize=self.CHUNK, callback=self._cb,
        )
        self._stream.start()
        log("RECORD", "microphone stream started — capturing audio")

    def _cb(self, indata, frames, time_info, status):
        data = bytes(indata)
        self._frames.append(data)          # list.append is atomic under the GIL
        arr = np.frombuffer(data, dtype=np.int16).astype(np.float32)
        self.level = float(np.sqrt(np.mean(arr ** 2))) if arr.size else 0.0

    def stop(self):
        """Stop the stream and return the captured audio, or None if too short."""
        if self._stream is None:
            return None
        self._stream.stop()
        self._stream.close()
        self._stream = None        # no further callbacks after this point
        raw = b"".join(self._frames)
        seconds = len(raw) / (self.SAMPLE_RATE * self.SAMPLE_WIDTH)
        log("RECORD", f"stream stopped — captured {seconds:.2f}s "
                      f"({len(raw)} bytes, {len(self._frames)} chunks)")
        min_bytes = int(self.SAMPLE_RATE * self.SAMPLE_WIDTH * self.MIN_SECONDS)
        if len(raw) < min_bytes:
            log("RECORD", f"too short (<{self.MIN_SECONDS}s) — discarding", "WARN")
            return None
        return sr.AudioData(raw, self.SAMPLE_RATE, self.SAMPLE_WIDTH)


def transcribe(audio: sr.AudioData) -> str:
    recognizer = sr.Recognizer()
    # operation_timeout bounds the network request to Google STT, so a stalled
    # connection raises sr.RequestError instead of hanging "Processing" forever.
    recognizer.operation_timeout = STT_TIMEOUT
    audio_seconds = len(audio.frame_data) / (audio.sample_rate * audio.sample_width)
    log("TRANSCRIBE", f"sending {audio_seconds:.2f}s of audio to Google STT "
                      f"(timeout {STT_TIMEOUT}s)")
    text = recognizer.recognize_google(audio)
    log("TRANSCRIBE", f"recognized: {text!r}")
    return text

# ── Chrome focus ──────────────────────────────────────────────────────────────
def activate_chrome() -> bool:
    """Bring Google Chrome to the foreground before acting on it.

    Uses AppleScript via `osascript`. `activate` also launches Chrome if it is
    not already running. Best-effort: a failure is logged but does not abort the
    pipeline. A short pause lets the window actually take focus before we send
    keystrokes / clicks.
    """
    try:
        subprocess.run(
            ["osascript", "-e", 'tell application "Google Chrome" to activate'],
            check=True, capture_output=True, timeout=5,
        )
        time.sleep(0.25)   # give the OS a moment to focus the window
        log("CHROME", "Google Chrome activated (foreground)")
        return True
    except Exception as e:
        log("CHROME", f"could not activate Chrome: {e}", "WARN")
        return False

# ── Chrome keyboard shortcuts (macOS) ─────────────────────────────────────────
# Researched from Chrome's macOS shortcut reference. Each entry maps a spoken
# intent (matched by regex on the lowercased command) to an action. The FIRST
# match wins, so more specific phrases must come before more general ones
# (e.g. "reopen closed tab" before "close tab", "incognito" before "new tab").
# `_k(...)` builds a hotkey action; `_scroll(...)` builds a scroll action.
def _k(*keys) -> dict:
    return {"action": "hotkey", "keys": list(keys)}

def _scroll(direction: str, amount: int = 5) -> dict:
    return {"action": "scroll", "direction": direction, "amount": amount}

CHROME_SHORTCUTS: list[tuple[str, dict, str]] = [
    # ── tabs & windows ──
    (r"(reopen|restore|undo clos\w*|bring back).*(tab)", _k("command", "shift", "t"), "reopen closed tab"),
    (r"(new|open).*(incognito|private)",                 _k("command", "shift", "n"), "new incognito window"),
    (r"(new|open|another).*(window)",                    _k("command", "n"),          "new window"),
    (r"(new|open|another|create).*(tab)",                _k("command", "t"),          "new tab"),
    (r"(close|exit).*(window)",                          _k("command", "shift", "w"), "close window"),
    (r"(close|exit).*(tab)",                             _k("command", "w"),          "close tab"),
    (r"(next|forward).*(tab)|(tab).*(right)",            _k("command", "option", "right"), "next tab"),
    (r"(previous|prev|last|back).*(tab)|(tab).*(left)",  _k("command", "option", "left"),  "previous tab"),
    (r"(quit|close).*(chrome|browser)",                  _k("command", "q"),          "quit Chrome"),
    (r"minimi[sz]e",                                     _k("command", "m"),          "minimize window"),
    # ── navigation ──
    (r"(hard|force|empty cache).*(reload|refresh)",      _k("command", "shift", "r"), "hard reload"),
    (r"(reload|refresh)",                                _k("command", "r"),          "reload page"),
    (r"(go )?forward",                                   _k("command", "]"),          "go forward"),
    (r"(go )?back",                                      _k("command", "["),          "go back"),
    (r"home ?page|go home",                              _k("command", "shift", "h"), "home page"),
    (r"address bar|url bar|location bar|search bar|focus.*address|type.*(url|address)|go to (a |the )?(website|url|page)|open (a |the )?(website|url)|search( the web| google| online)?\b", _k("command", "l"), "focus address bar"),
    # ── finding & page ──
    (r"find next",                                       _k("command", "g"),          "find next"),
    (r"find previous|find prev",                         _k("command", "shift", "g"), "find previous"),
    (r"find|search.*(on|in).*(page)",                    _k("command", "f"),          "find on page"),
    (r"\bprint\b",                                       _k("command", "p"),          "print"),
    (r"save.*(page|this)|^save",                         _k("command", "s"),          "save page"),
    (r"zoom in|make.*(big|larg)|increase.*(zoom|text|size)", _k("command", "="),      "zoom in"),
    (r"zoom out|make.*(small)|decrease.*(zoom|text|size)",   _k("command", "-"),      "zoom out"),
    (r"reset zoom|actual size|default zoom|normal size",    _k("command", "0"),       "reset zoom"),
    (r"full ?screen",                                    _k("command", "ctrl", "f"),  "toggle full screen"),
    (r"(scroll|go|jump).*(top|beginning|start)|^top",   _k("command", "up"),         "scroll to top"),
    (r"(scroll|go|jump).*(bottom|end)|^bottom",         _k("command", "down"),       "scroll to bottom"),
    (r"scroll (down|up)",                               None,                         "scroll"),  # handled specially below
    # ── bookmarks / history / downloads / tools ──
    (r"bookmark all",                                    _k("command", "shift", "d"), "bookmark all tabs"),
    (r"bookmark",                                        _k("command", "d"),          "bookmark page"),
    (r"(show|hide|toggle).*(bookmark bar|bookmarks bar)", _k("command", "shift", "b"),"toggle bookmarks bar"),
    (r"\bhistory\b",                                     _k("command", "y"),          "open history"),
    (r"\bdownloads?\b",                                  _k("command", "shift", "j"), "open downloads"),
    (r"clear.*(browsing|history|data)",                 _k("command", "shift", "backspace"), "clear browsing data"),
    (r"settings|preferences",                            _k("command", ","),          "open settings"),
    (r"dev(eloper)? tools|inspect",                      _k("command", "option", "i"),"open dev tools"),
    (r"view (page )?source",                             _k("command", "option", "u"),"view source"),
    # ── editing ──
    (r"select all",                                      _k("command", "a"),          "select all"),
    (r"\bcopy\b",                                        _k("command", "c"),          "copy"),
    (r"\bpaste\b",                                       _k("command", "v"),          "paste"),
    (r"\bcut\b",                                         _k("command", "x"),          "cut"),
    (r"\bredo\b",                                        _k("command", "shift", "z"), "redo"),
    (r"\bundo\b",                                        _k("command", "z"),          "undo"),
]
_COMPILED_SHORTCUTS = [(re.compile(p), a, d) for p, a, d in CHROME_SHORTCUTS]


# ── Typing / search / navigation intents ──────────────────────────────────────
# These commands carry TEXT to enter, so they can't be a plain hotkey — they
# focus a field (when needed), paste the text, and optionally press Enter. This
# must run BEFORE the shortcut table, otherwise "type X into the search bar"
# matches the address-bar shortcut and only focuses the bar without typing.
def _type(text: str, enter: bool = False) -> dict:
    return {"action": "type", "text": text, "enter": enter}

def _seq(*steps) -> dict:
    return {"action": "sequence", "steps": list(steps)}

# trailing phrases that name WHERE to type (not the content itself)
_TARGET = (r"(address bar|url bar|location bar|search bar|search box|search field"
           r"|text box|text field|search|omnibox|box|field|page)")
# payloads that refer to the field itself / are not real content to type
_NON_PAYLOAD = {"", "bar", "box", "field", "page", "the web", "web", "online",
                "the internet", "internet", "google", "it", "this", "that",
                "something", "the search bar", "the address bar"}

_TLD = r"(com|org|net|edu|gov|io|co|app|dev|ai|me|tv|uk|gg|xyz)"

def _looks_like_url(s: str) -> bool:
    s = s.strip().lower()
    return (s.startswith(("http://", "https://", "www."))
            or bool(re.search(r"\." + _TLD + r"\b", s))
            or bool(re.search(r"\bdot\s+" + _TLD + r"\b", s)))

def _normalize_url(s: str) -> str:
    """Turn spoken URLs into real ones: 'youtube dot com slash feed' -> 'youtube.com/feed'."""
    s = s.strip()
    s = re.sub(r"\s+dot\s+", ".", s, flags=re.I)
    s = re.sub(r"\s+slash\s+", "/", s, flags=re.I)
    s = re.sub(r"\s+dash\s+", "-", s, flags=re.I)
    # if it now looks like an address, drop any stray spaces inside it
    if re.search(r"\." + _TLD + r"\b", s, re.I) or "/" in s:
        s = re.sub(r"\s+", "", s)
    return s

def _strip_target(text: str) -> tuple[str, str | None]:
    """Split off a trailing 'in/into the <target>' phrase. Returns (payload, target)."""
    m = re.search(r"\s+(?:in|into|on|to|inside)\s+(?:the\s+)?" + _TARGET + r"\s*$",
                  text, re.I)
    if m:
        return text[:m.start()].rstrip(), m.group(1).lower()
    return text, None

def _match_typing(command: str) -> tuple[dict | None, str | None]:
    cmd = command.strip()

    # 1) Web search:  "search for X" / "search X" / "google X" / "look up X"
    m = re.match(r"(?i)^\s*(?:search\s+for|search|google|look\s*up|bing)\s+(.+)$", cmd)
    if m:
        payload, _ = _strip_target(m.group(1))
        if payload.strip().lower() not in _NON_PAYLOAD:
            return (_seq(_k("command", "l"), _type(payload, enter=True)),
                    f"search the web for {payload!r}")

    # 2) Navigate to a website:  "go to youtube.com" / "open netflix.com"
    #    ("go to X.com" just means: focus the address bar, type X.com, press Enter)
    m = re.match(r"(?i)^\s*(?:go to|navigate to|visit|open up|open|head to|take me to|bring up)\s+(.+)$", cmd)
    if m:
        raw = m.group(1).strip()
        norm = _normalize_url(raw)
        if _looks_like_url(raw) or _looks_like_url(norm):
            return (_seq(_k("command", "l"), _type(norm, enter=True)),
                    f"open website {norm!r}")

    # 3) Type text:  "type X" / "enter X" / "paste X" [into the search/address bar]
    m = re.match(r"(?i)^\s*(?:type|enter|write|input|dictate|paste|put)\s+(.+)$", cmd)
    if m:
        body = m.group(1)
        enter = False
        m2 = re.search(r"\s+(?:and|then)\s+(?:search|enter|submit|go|hit enter|press enter)\s*$",
                       body, re.I)
        if m2:
            enter = True
            body = body[:m2.start()].rstrip()
        payload, target = _strip_target(body)
        if payload.strip().lower() in _NON_PAYLOAD:
            return None, None        # no actual content — let other paths handle it
        # If they named the address/search bar, focus it (Cmd+L) before pasting;
        # otherwise paste into whatever field already has focus.
        if target and re.search(r"address|url|location|omnibox|search", target):
            return (_seq(_k("command", "l"), _type(payload, enter=enter)),
                    f"type {payload!r} in the address bar")
        return _type(payload, enter=enter), f"type {payload!r}"

    return None, None


def match_shortcut(command: str) -> tuple[dict | None, str | None]:
    """Map a spoken command to a Chrome keyboard shortcut, if one applies.

    Returns (action, description) on a match, or (None, None) when the command
    needs visual context — in which case the caller falls back to screenshot +
    Groq. This is the fast path: no screenshot, no OCR, no model call.
    """
    # Typing/search/navigation intents carry text — handle them first.
    typed, typed_desc = _match_typing(command)
    if typed is not None:
        return typed, typed_desc

    text = command.lower().strip()
    for pattern, action, desc in _COMPILED_SHORTCUTS:
        if pattern.search(text):
            if action is None:                       # the generic scroll entry
                direction = "up" if "up" in text else "down"
                return _scroll(direction), f"scroll {direction}"
            return dict(action), desc                # copy so callers can't mutate the table
    return None, None


# ── Stacking multiple commands in one utterance ───────────────────────────────
# "open a new tab, go to youtube.com, and then click the first video" must run
# as THREE separate commands, each re-checking the screen. We split on spoken
# sequence words ("then", "and then") and on "and"/comma only when the next word
# starts a new command (an action verb) — so "search for cats and dogs" stays one
# command while "open a tab and go to youtube" becomes two.
_VERB = (r"(?:open|go|navigate|visit|head|take|bring|click|tap|press|select|search|"
         r"google|look|type|enter|write|input|paste|put|scroll|close|reopen|"
         r"bookmark|reload|refresh|zoom|copy|cut|undo|redo|find|play|pause|mute|"
         r"minimi[sz]e|quit|download|switch|hit)")

def split_commands(command: str) -> list[str]:
    text = command.strip()
    # 1) strong sequence markers — always a boundary
    parts = re.split(r"\s+(?:and\s+then|then|after\s+that|after\s+which|next)\s+",
                     text, flags=re.I)
    # 2) " and <verb>"  and  3) ", <verb>"  — boundary only before a new command
    out: list[str] = []
    for p in parts:
        for piece in re.split(r"\s+and\s+(?=" + _VERB + r"\b)", p, flags=re.I):
            out.extend(re.split(r"\s*,\s*(?=" + _VERB + r"\b)", piece, flags=re.I))
    return [s.strip().rstrip(".,") for s in out if s.strip()]


def _changes_page(action: dict) -> bool:
    """True if an action likely loads/changes the page, so the next stacked
    command should wait for it before screenshotting."""
    k = action.get("action")
    if k == "type":
        return bool(action.get("enter"))
    if k == "click":
        return True
    if k == "sequence":
        return any(_changes_page(s) for s in action.get("steps", []))
    if k == "hotkey":
        keys = list(action.get("keys", []))
        nav = (["command", "r"], ["command", "shift", "r"], ["command", "["],
               ["command", "]"], ["command", "t"])
        return keys in [list(x) for x in nav]
    return False

# ── Core pipeline functions ───────────────────────────────────────────────────
def capture_screen_elements() -> list[dict]:
    with Timer("SCREENSHOT"):
        screenshot = pyautogui.screenshot()
    # On Retina displays the screenshot is in PHYSICAL pixels (e.g. 2704x1756)
    # while pyautogui clicks in LOGICAL coordinates (e.g. 1352x878). OCR gives
    # coordinates in the screenshot's pixel space, so we must scale them down to
    # the logical space or every click lands in the wrong place / off-screen.
    logical_w, logical_h = pyautogui.size()
    phys_w, phys_h = screenshot.size
    scale_x = logical_w / phys_w
    scale_y = logical_h / phys_h
    log("SCREENSHOT", f"captured {phys_w}x{phys_h} image | "
                      f"logical screen {logical_w}x{logical_h} | "
                      f"scale x={scale_x:.3f} y={scale_y:.3f}")
    with Timer("OCR", "running pytesseract (this can be the slow stage)"):
        data = pytesseract.image_to_data(
            screenshot, output_type=pytesseract.Output.DICT
        )
    # pytesseract returns WORD-level boxes, so a title like "How to cook pasta"
    # arrives as 4 separate words. We group words back into LINES (same block /
    # paragraph / line) so each element is a whole label/title — otherwise the
    # model can't match "the second video" against scattered single words.
    groups: dict[tuple, dict] = {}
    for i in range(len(data["text"])):
        text = data["text"][i].strip()
        if not text or int(data["conf"][i]) < 40:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        left, top = data["left"][i], data["top"][i]
        right, bottom = left + data["width"][i], top + data["height"][i]
        g = groups.get(key)
        if g is None:
            groups[key] = {"words": [text], "x0": left, "y0": top,
                           "x1": right, "y1": bottom}
        else:
            g["words"].append(text)
            g["x0"], g["y0"] = min(g["x0"], left), min(g["y0"], top)
            g["x1"], g["y1"] = max(g["x1"], right), max(g["y1"], bottom)

    elements = []
    for g in groups.values():       # dict preserves OCR (top-to-bottom) order
        text = " ".join(g["words"]).strip()
        if not text:
            continue
        cx = (g["x0"] + g["x1"]) / 2     # click the CENTER of the whole line
        cy = (g["y0"] + g["y1"]) / 2
        elements.append({"text": text,
                         "x": int(cx * scale_x), "y": int(cy * scale_y)})
    sample = " | ".join(e["text"][:30] for e in elements[:10])
    log("OCR", f"found {len(elements)} line elements (grouped from words). "
               f"sample: {sample}")
    return elements


MAX_ELEMENTS = 120          # how many OCR line-elements we show the model

def ask_groq(client: Groq, command: str, elements: list[dict]) -> dict:
    shown = elements[:MAX_ELEMENTS]
    # Present elements as a NUMBERED list so the model selects by index instead
    # of inventing coordinates. We resolve the index back to the real (already
    # screen-scaled) coordinate ourselves — that is what stops random misclicks.
    element_lines = "\n".join(
        f'  [{i}] "{e["text"]}" at ({e["x"]}, {e["y"]})' for i, e in enumerate(shown)
    )
    user_message = f'Voice command: "{command}"\n\nVisible screen elements:\n{element_lines}'
    screen_w, screen_h = pyautogui.size()
    system_prompt = SYSTEM_PROMPT.format(
        screen_w=screen_w, screen_h=screen_h,
        center_x=screen_w // 2, center_y=screen_h // 2,
    )
    log("GROQ", f"calling {GROQ_MODEL} with {len(shown)} elements "
                f"(timeout {GROQ_TIMEOUT}s)")
    with Timer("GROQ", "waiting for model response"):
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            temperature=0,
            max_tokens=128,
        )
    raw = response.choices[0].message.content.strip()
    log("GROQ", f"raw response: {raw!r}")
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    action = json.loads(raw.strip())
    return _resolve_click_index(action, shown)


def _resolve_click_index(action: dict, shown: list[dict]) -> dict:
    """Turn a {"action":"click","index":N} into real (x, y) from the element list.

    This is the anti-misclick guard: the model may only point at an element it
    was shown, so we never click coordinates the model made up. An out-of-range
    or -1 index means "no element matched" and raises, so the pipeline reports a
    clear error instead of clicking somewhere random.
    """
    if action.get("action") != "click":
        return action
    if "index" in action:
        idx = action["index"]
        if not isinstance(idx, int) or not (0 <= idx < len(shown)):
            raise ValueError(
                f"no on-screen element matched the command "
                f"(model returned index {idx!r}, valid range 0..{len(shown) - 1})"
            )
        el = shown[idx]
        log("GROQ", f"click resolved -> element [{idx}] {el['text']!r} "
                    f"at ({el['x']}, {el['y']})")
        return {"action": "click", "x": el["x"], "y": el["y"]}
    # Legacy/fallback: model returned raw x/y. Snap to the nearest shown element
    # so a slightly-off guess still lands on real text instead of empty space.
    if "x" in action and "y" in action and shown:
        ax, ay = int(action["x"]), int(action["y"])
        nearest = min(shown, key=lambda e: (e["x"] - ax) ** 2 + (e["y"] - ay) ** 2)
        dist = ((nearest["x"] - ax) ** 2 + (nearest["y"] - ay) ** 2) ** 0.5
        if dist <= 60:        # within ~60px of a real element — snap to it
            log("GROQ", f"raw click ({ax}, {ay}) snapped to {nearest['text']!r} "
                        f"at ({nearest['x']}, {nearest['y']})")
            return {"action": "click", "x": nearest["x"], "y": nearest["y"]}
        log("GROQ", f"raw click ({ax}, {ay}) has no nearby element "
                    f"(closest {dist:.0f}px away) — using as-is", "WARN")
    return action


def _paste_text(text: str):
    """Enter text by putting it on the clipboard and pasting it (Cmd+V).

    On macOS simulated keystrokes (pyautogui.typewrite) are unreliable — they
    often drop characters or type nothing — while Cmd+V is rock solid. We save
    and restore the user's existing clipboard so we don't clobber it.
    """
    saved = None
    try:
        saved = subprocess.run(["pbpaste"], capture_output=True, timeout=2).stdout
    except Exception:
        pass
    subprocess.run(["pbcopy"], input=text.encode("utf-8"), timeout=2)
    time.sleep(0.05)
    pyautogui.hotkey("command", "v")
    time.sleep(0.15)                      # let the paste land before restoring
    if saved is not None:
        try:
            subprocess.run(["pbcopy"], input=saved, timeout=2)
        except Exception:
            pass


def execute_action(action: dict) -> str:
    kind = action.get("action")
    log("EXECUTE", f"performing action: {json.dumps(action)}")
    if kind == "sequence":
        # Run sub-actions in order (e.g. focus address bar -> paste -> Enter).
        results = []
        for i, step in enumerate(action.get("steps", [])):
            if i:
                time.sleep(0.25)          # let focus settle between steps
            results.append(execute_action(step))
        return " → ".join(results)
    if kind == "click":
        x, y = int(action["x"]), int(action["y"])
        pyautogui.moveTo(x, y, duration=0.3)
        pyautogui.click(x, y)
        return f"Clicked ({x}, {y})"
    elif kind == "scroll":
        direction = action.get("direction", "down")
        amount = int(action.get("amount", 3))
        if direction == "up":
            pyautogui.scroll(amount)
        elif direction == "down":
            pyautogui.scroll(-amount)
        elif direction == "left":
            pyautogui.hscroll(-amount)
        elif direction == "right":
            pyautogui.hscroll(amount)
        return f"Scrolled {direction} by {amount}"
    elif kind == "hotkey":
        keys = action.get("keys", [])
        pyautogui.hotkey(*keys)
        return f"Hotkey: {'+'.join(keys)}"
    elif kind == "type":
        text = action.get("text", "")
        _paste_text(text)
        if action.get("enter"):
            time.sleep(0.15)
            pyautogui.press("enter")
            return f'Typed "{text}" + Enter'
        return f'Typed "{text}"'
    else:
        return f"Unknown action: {kind}"

# ── UI ────────────────────────────────────────────────────────────────────────
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY not found in environment / .env file")
        self.groq = Groq(api_key=api_key, timeout=GROQ_TIMEOUT, max_retries=1)
        self._recorder = PushToTalkRecorder()
        self._recording = False
        self._busy = False        # True while transcribing / acting on a command
        self._build_ui()

    def _build_ui(self):
        self.title("Accessibility4all")
        self.resizable(False, False)
        self.configure(bg="#1a1a2e")

        pad = {"padx": 30, "pady": 20}
        title_font = tkfont.Font(family="Helvetica", size=22, weight="bold")
        status_font = tkfont.Font(family="Helvetica", size=13)
        btn_font = tkfont.Font(family="Helvetica", size=16, weight="bold")

        tk.Label(self, text="Accessibility4all", font=title_font,
                 fg="#e0e0ff", bg="#1a1a2e").pack(**pad)

        self.status_var = tk.StringVar(value="Press the button and speak a command")
        self.status_label = tk.Label(
            self, textvariable=self.status_var, font=status_font,
            fg="#a0c4ff", bg="#1a1a2e", wraplength=380, justify="center",
        )
        self.status_label.pack(padx=30, pady=(0, 10))

        meter_frame = tk.Frame(self, bg="#0d0d1a", bd=0, highlightthickness=0)
        meter_frame.pack(padx=30, pady=(0, 8), fill="x")
        self._meter_canvas = tk.Canvas(
            meter_frame, width=380, height=18, bg="#0d0d1a", highlightthickness=0
        )
        self._meter_canvas.pack()
        self._meter_bar = self._meter_canvas.create_rectangle(
            0, 0, 0, 18, fill="#4361ee", outline=""
        )

        self.talk_btn = tk.Button(
            self, text="Hold to Talk", font=btn_font,
            bg="#4361ee", fg="white",
            activebackground="#3a0ca3", activeforeground="white",
            relief="flat", padx=30, pady=18, cursor="hand2",
        )
        # True push-to-talk: record while held, process on release.
        self.talk_btn.bind("<ButtonPress-1>", self._on_press)
        self.talk_btn.bind("<ButtonRelease-1>", self._on_release)
        self.talk_btn.pack(padx=30, pady=(6, 20))

        self._trial_var = tk.StringVar(value="")
        tk.Label(self, textvariable=self._trial_var, font=tkfont.Font(family="Helvetica", size=10),
                 fg="#666688", bg="#1a1a2e").pack(padx=30, pady=(0, 12))

        self.geometry("440x340")
        self.lift()
        self.attributes("-topmost", True)

    # ── meter (polled on the main thread; never updated from audio thread) ──
    def _draw_meter(self, energy: float):
        max_energy = 4000
        width = min(int(energy / max_energy * 380), 380)
        color = "#69db7c" if width > 200 else "#ffe066" if width > 60 else "#4361ee"
        self._meter_canvas.coords(self._meter_bar, 0, 0, width, 18)
        self._meter_canvas.itemconfig(self._meter_bar, fill=color)

    def _poll_meter(self):
        if not self._recording:
            self._draw_meter(0)
            return
        self._draw_meter(self._recorder.level)
        self.after(30, self._poll_meter)

    def _set_status(self, msg: str, color: str = "#a0c4ff"):
        # Marshal onto the main thread — safe to call from worker threads.
        self.after(0, lambda: (self.status_var.set(msg),
                               self.status_label.configure(fg=color)))

    def _set_trial_info(self, msg: str):
        self.after(0, lambda: self._trial_var.set(msg))

    # ── push-to-talk: record while held, transcribe + act on release ──
    def _on_press(self, _event):
        if self._busy:
            log("UI", "button press ignored — still busy processing previous command",
                "WARN")
            return
        log("UI", "button pressed — starting recording")
        try:
            self._recorder.start()
        except Exception as e:
            log("RECORD", f"failed to open microphone: {e}", "ERROR")
            self._set_status(f"Mic error: {e}", "#ff6b6b")
            return
        self._recording = True
        self.talk_btn.configure(text="Listening… (release to send)", bg="#e63946")
        self._set_status("Listening — speak now, then release.", "#ffe066")
        self._poll_meter()

    def _on_release(self, _event):
        if not self._recording:
            return
        log("UI", "button released — stopping recording")
        self._recording = False
        self.talk_btn.configure(text="Hold to Talk", bg="#6c757d")
        audio = self._recorder.stop()
        self._draw_meter(0)
        if audio is None:
            self._set_status("Too short — hold the button while speaking.", "#ff6b6b")
            self.talk_btn.configure(bg="#4361ee")
            return
        self._busy = True
        self._set_status("Processing…", "#a0c4ff")
        log("PIPELINE", "=== launching processing thread ===")
        threading.Thread(target=self._process, args=(audio,), daemon=True).start()

    def _process(self, audio):
        trial: dict = {}
        pipeline_t0 = time.perf_counter()
        # Watchdog: if the whole pipeline hasn't finished in time, say so in the
        # terminal. This makes a hang obvious instead of a silent "Processing…".
        done = threading.Event()
        self._start_watchdog(done, pipeline_t0)
        log("PIPELINE", "processing started")
        try:
            # ── Stage 1: speech-to-text ──────────────────────────────────────
            log("PIPELINE", "step 1/4 — transcribe audio")
            try:
                with Timer("TRANSCRIBE"):
                    command = transcribe(audio)
            except sr.UnknownValueError:
                log("TRANSCRIBE", "Google STT could not understand the audio", "WARN")
                self._set_status("Could not understand speech. Try again.", "#ff6b6b")
                _log_trial({"event": "unrecognized", "success": False})
                self._set_trial_info("Trial logged — unrecognized speech")
                return
            except sr.RequestError as e:
                log("TRANSCRIBE", f"STT request error (network/timeout?): {e}", "ERROR")
                self._set_status(f"Speech service error: {e}", "#ff6b6b")
                _log_trial({"event": "stt_request_error", "error": str(e), "success": False})
                self._set_trial_info("Trial logged — STT request error")
                return

            trial["command"] = command
            self._set_status(f'Heard: "{command}"\nProcessing…', "#a0c4ff")

            # ── Stage 2: make sure Chrome is the active app ──────────────────
            log("PIPELINE", "step 2 — focus Google Chrome")
            trial["chrome_activated"] = activate_chrome()

            # ── Stage 3: split into stacked sub-commands and run each ─────────
            commands = split_commands(command)
            trial["commands"] = commands
            log("PIPELINE", f"{len(commands)} sub-command(s): {commands}")

            steps: list[dict] = []
            for i, sub in enumerate(commands):
                if i:
                    # Let the previous step settle — longer if it loaded a page,
                    # so a following "click" sees the new screen.
                    wait = 2.5 if steps[-1]["changed_page"] else 0.7
                    log("PIPELINE", f"waiting {wait:.1f}s for previous step to settle")
                    time.sleep(wait)

                label = f"Step {i + 1}/{len(commands)}" if len(commands) > 1 else "Working"
                log("PIPELINE", f"--- {label}: {sub!r} ---")
                self._set_status(f'{label}: "{sub}"', "#a0c4ff")
                trial["_step"] = i + 1                  # for error reporting
                res = self._run_subcommand(sub)
                steps.append(res)
                self._set_status(f"{label} ✓ {res['result']}", "#69db7c")

            trial["steps"] = [{k: s[k] for k in ("command", "action", "result", "method")}
                              for s in steps]
            trial["success"] = True
            trial.pop("_step", None)

            summary = (steps[-1]["result"] if len(commands) == 1
                       else f"{len(commands)} steps done")
            self._set_status(f"Done — {summary}", "#69db7c")
            _log_trial(trial)
            self._set_trial_info(f"Trial logged — {TRIAL_LOG.name}")
            log("PIPELINE", f"SUCCESS — {summary}")

        except json.JSONDecodeError as e:
            log("GROQ", f"model returned non-JSON: {e}", "ERROR")
            trial["error"] = f"JSONDecodeError: {e}"
            trial["success"] = False
            self._set_status(f"Bad response from AI: {e}", "#ff6b6b")
            _log_trial(trial)
            self._set_trial_info("Trial logged — AI parse error")
        except Exception as e:
            step = trial.get("_step")
            where = f" (step {step})" if step else ""
            log("PIPELINE", f"unexpected error{where}: {type(e).__name__}: {e}", "ERROR")
            trial["error"] = str(e)
            trial["success"] = False
            self._set_status(f"Error{where}: {e}", "#ff6b6b")
            _log_trial(trial)
            self._set_trial_info("Trial logged — error")
        finally:
            done.set()
            total = time.perf_counter() - pipeline_t0
            log("PIPELINE", f"=== finished in {total:.2f}s ===")
            self._busy = False
            self.after(0, lambda: self.talk_btn.configure(state="normal", bg="#4361ee"))

    def _run_subcommand(self, sub: str) -> dict:
        """Resolve ONE command and run it. Tries the shortcut/typing fast-path
        first; otherwise takes a FRESH screenshot and asks Groq what to do, so
        stacked steps act on the current screen (e.g. click after navigating)."""
        action, desc = match_shortcut(sub)
        if action is not None:
            log("RESOLVE", f"{sub!r} -> shortcut: {desc}")
            method, ecount = "shortcut", None
        else:
            log("RESOLVE", f"{sub!r} -> no shortcut, using screenshot + Groq")
            elements = capture_screen_elements()
            ecount = len(elements)
            action = ask_groq(self.groq, sub, elements)
            method = "vision"
        with Timer("EXECUTE"):
            result = execute_action(action)
        log("EXECUTE", f"result: {result}")
        return {"command": sub, "action": action, "result": result,
                "method": method, "element_count": ecount,
                "changed_page": _changes_page(action)}

    def _start_watchdog(self, done: threading.Event, t0: float, warn_after: float = 20.0):
        """Print a terminal warning if the pipeline runs longer than warn_after.

        Repeats every warn_after seconds until the pipeline signals `done`, so a
        true hang produces an ongoing "still stuck" heartbeat in the terminal
        showing which stage was last reached.
        """
        def _watch():
            n = 0
            while not done.wait(warn_after):
                n += 1
                elapsed = time.perf_counter() - t0
                log("WATCHDOG",
                    f"pipeline still running after {elapsed:.0f}s — "
                    f"last logged stage above is where it is stuck", "WARN")
        threading.Thread(target=_watch, daemon=True).start()


if __name__ == "__main__":
    log("STARTUP", "Accessibility4all starting — terminal step logging enabled")
    log("STARTUP", f"Python {sys.version.split()[0]} | trial log: {TRIAL_LOG}")
    try:
        app = App()
    except Exception as e:
        log("STARTUP", f"failed to start: {type(e).__name__}: {e}", "ERROR")
        raise
    log("STARTUP", "UI ready — hold the button and speak")
    app.mainloop()
    log("SHUTDOWN", "main loop exited")
