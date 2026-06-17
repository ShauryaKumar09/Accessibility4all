
# Suppress audioop deprecation warning from SpeechRecognition (removed in Py 3.13)
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning, message=".*audioop.*")

import os
import re
import sys
import json
import time
import queue
import difflib
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

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import console, feature_bus, platform as plat, screen_ocr  # noqa: E402

console.configure_stdio()

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
        console.safe_print(line, flush=True)

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
    return plat.activate_chrome(log_fn=log)

# ── Chrome keyboard shortcuts (macOS / Windows) ───────────────────────────────
def _scroll(direction: str, amount: int = 5) -> dict:
    return {"action": "scroll", "direction": direction, "amount": amount}

CHROME_SHORTCUTS: list[tuple[str, dict, str]] = plat.chrome_shortcut_table()
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
            return (_seq(plat.hotkey_action("mod", "l"), _type(payload, enter=True)),
                    f"search the web for {payload!r}")

    # 2) Navigate to a website:  "go to youtube.com" / "open netflix.com"
    #    ("go to X.com" just means: focus the address bar, type X.com, press Enter)
    m = re.match(r"(?i)^\s*(?:go to|navigate to|visit|open up|open|head to|take me to|bring up)\s+(.+)$", cmd)
    if m:
        raw = m.group(1).strip()
        norm = _normalize_url(raw)
        if _looks_like_url(raw) or _looks_like_url(norm):
            return (_seq(plat.hotkey_action("mod", "l"), _type(norm, enter=True)),
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
            return (_seq(plat.hotkey_action("mod", "l"), _type(payload, enter=enter)),
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
        m = plat.mod_key()
        nav = ([m, "r"], [m, "shift", "r"], [m, "["], [m, "]"], [m, "t"])
        if not plat.IS_MAC:
            nav = ([m, "r"], [m, "shift", "r"], ["alt", "left"], ["alt", "right"], [m, "t"])
        return keys in [list(x) for x in nav]
    return False

# ── Page Reader command forwarding ─────────────────────────────────────────────
_READ_SCREEN_RE = re.compile(r"^read\s+screen\s*$", re.I)
_STOP_READING_RE = re.compile(r"^stop\s+reading\s*$", re.I)
_READ_LAST_RE = re.compile(
    r"^read\s+(?:that\s+(?:again|section)|it\s+again)\s*$", re.I)
_READ_SECTION_RE = re.compile(r"^read(?:\s+out)?(?:\s+the)?\s+(.+)$", re.I)


def match_read_command(command: str) -> dict | None:
    """If command is a page-reader intent, return {cmd, ...} else None."""
    cmd = command.strip()
    if _READ_SCREEN_RE.match(cmd):
        return {"cmd": "read_screen"}
    if _STOP_READING_RE.match(cmd):
        return {"cmd": "stop"}
    if _READ_LAST_RE.match(cmd):
        return {"cmd": "read_last"}
    m = _READ_SECTION_RE.match(cmd)
    if m:
        return {"cmd": "read_section", "text": m.group(1).strip()}
    return None


def try_forward_to_page_reader(command: str) -> tuple[bool, str | None]:
    """Forward read intents to page_reader. Returns (handled, error_message)."""
    intent = match_read_command(command)
    if intent is None:
        return False, None
    if not feature_bus.is_feature_running("page_reader"):
        return True, "Turn on Page Reader first"
    settings = feature_bus.load_page_reader_settings()
    if not settings.get("voice_guided", True):
        return True, "Enable voice-guided read in Page Reader"
    feature_bus.append_command(from_feature="voice_control", **intent)
    log("READ", f"forwarded to page_reader: {intent}")
    return True, None

# ── Core pipeline functions ───────────────────────────────────────────────────
def capture_screen_elements() -> list[dict]:
    with Timer("SCREENSHOT", "delegating to shared OCR"):
        return screen_ocr.capture_screen_elements(log_fn=log)


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
    system_prompt = plat.chrome_system_prompt(
        screen_w, screen_h, screen_w // 2, screen_h // 2,
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


# ── Click-by-title matching (local, deterministic) ────────────────────────────
# "click on the video titled <X>" / "click on <X>" — match the spoken title
# against the on-screen text directly instead of asking the model to count
# items (which it does unreliably, e.g. "third video" hitting the second).
# Filler words that aren't part of a real title.
_STOP_MATCH = {
    "the", "a", "an", "that", "this", "one", "please", "click", "on", "to", "of",
    "and", "for", "with", "my", "it", "video", "link", "button", "tab", "icon",
    "menu", "option", "field", "box", "bar", "page", "result", "item", "thumbnail",
    "post", "article", "tile", "card", "story", "image", "picture", "called",
    "titled", "named", "labeled", "title", "says", "saying",
}
_ORDINALS = {"first", "second", "third", "fourth", "fifth", "sixth", "seventh",
             "eighth", "ninth", "tenth", "last", "next", "previous", "top", "bottom"}

def _norm_text(s: str) -> str:
    s = re.sub(r"[^\w\s]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()

def _word_in(w: str, ewords: list[str]) -> bool:
    """Is spoken word `w` present in an element's words (allowing fuzziness)?"""
    for ew in ewords:
        if w == ew:
            return True
        if len(w) >= 4 and (w in ew or ew in w):
            return True
        if len(w) >= 4 and difflib.SequenceMatcher(None, w, ew).ratio() >= 0.82:
            return True
    return False

def _extract_click_target(command: str) -> str | None:
    """Pull the title/description out of a click command, or None if it isn't one."""
    m = re.match(r"(?i)^\s*(?:click|tap|press|open|play|select|choose|hit)\b\s*"
                 r"(?:on\b\s*)?(.*)$", command.strip())
    if not m:
        return None
    rest = m.group(1)
    rest = re.sub(r"(?i)^\s*(?:the|that|this|a|an)\s+", "", rest)
    rest = re.sub(r"(?i)^\s*(?:video|link|button|result|item|option|thumbnail|tab|"
                  r"post|article|tile|card|story|image|picture)\s+", "", rest)
    rest = re.sub(r"(?i)^\s*(?:titled|called|named|labeled|label|title|that\s+says|"
                  r"saying|which\s+says|with\s+the\s+title|with\s+title)\s*[:\-]?\s*",
                  "", rest)
    return rest.strip(" '\"") or None

def _thumbnail_point(title: dict, elements: list[dict], screen_h: int) -> tuple[int, int]:
    """Pick a click point on the THUMBNAIL above a video's title line.

    A video's title is a link to the video, but it sits right above the channel
    name — clicking low can hit the channel and open the wrong page. The
    thumbnail (directly above the title) always opens the video, so we aim there.
    We estimate the thumbnail's height from the gap up to the nearest text above
    the title's column; if there's no clear gap we fall back to a fixed offset.
    """
    tx0, tx1, top = title["x0"], title["x1"], title["y0"]
    cx = (tx0 + tx1) // 2
    above_bottom = None
    for e in elements:
        if e is title or e["y1"] > top:                 # only elements above
            continue
        if e["x1"] >= tx0 - 20 and e["x0"] <= tx1 + 20:  # same column
            above_bottom = max(above_bottom or 0, e["y1"])
    if above_bottom is not None and (top - above_bottom) > 40:
        # thumbnail roughly spans [above_bottom, top]; aim at its lower-middle
        ty = int(above_bottom + (top - above_bottom) * 0.55)
    else:
        ty = int(top - max(60, 0.08 * screen_h))        # fixed fallback offset
    return cx, max(ty, 1)


def _first_video_title(elements: list[dict], screen_w: int, screen_h: int) -> dict | None:
    """Heuristic for a generic 'click a video': the topmost content-area line
    that looks like a video title (longish, multi-word, not sidebar/top bar)."""
    cands = [e for e in elements
             if len(e["text"]) >= 15 and len(e["text"].split()) >= 3
             and e["y"] > 0.08 * screen_h and e["x"] > 0.12 * screen_w]
    cands.sort(key=lambda e: (e["y"], e["x"]))
    return cands[0] if cands else None


def match_click_target(command: str, elements: list[dict]) -> dict | None:
    """Return a click action for a 'click on …' command, or None to defer to Groq.

    For video clicks we aim at the thumbnail (above the title) so it always opens
    the video, never the channel name. For other targets (buttons/links) we click
    the matched text itself."""
    is_video = bool(re.search(r"\b(video|thumbnail|clip|movie)\b", command, re.I)) \
        or bool(re.match(r"(?i)\s*play\b", command))
    screen_w, screen_h = pyautogui.size()

    target = _extract_click_target(command)
    words = []
    if target:
        words = [w for w in _norm_text(target).split()
                 if w not in _STOP_MATCH and w not in _ORDINALS
                 and not re.fullmatch(r"\d+(st|nd|rd|th)?", w)]

    if words:
        target_clean = " ".join(words)
        best, best_score = None, 0.0
        for e in elements:
            et = _norm_text(e["text"])
            if not et:
                continue
            if target_clean in et:
                score = 1.0                              # exact phrase contained
            else:
                matched = sum(1 for w in words if _word_in(w, et.split()))
                score = matched / len(words)             # fraction of words found
            if score > best_score:                       # ties keep the topmost
                best_score, best = score, e
        if best is not None and best_score >= 0.6:
            if is_video:
                x, y = _thumbnail_point(best, elements, screen_h)
                log("MATCH", f"title match {best_score:.2f}: {best['text'][:50]!r} "
                             f"-> clicking thumbnail at ({x}, {y})")
            else:
                x, y = best["x"], best["y"]
                log("MATCH", f"text match {best_score:.2f}: {best['text'][:50]!r} "
                             f"at ({x}, {y})")
            return {"action": "click", "x": x, "y": y}
        log("MATCH", f"no confident match for {target!r} (best {best_score:.2f})"
                     f" — falling back to Groq", "WARN")
        return None

    # No title given. If they said "click a video"/"play a video", pick the first
    # plausible video and click its thumbnail.
    if is_video:
        cand = _first_video_title(elements, screen_w, screen_h)
        if cand is not None:
            x, y = _thumbnail_point(cand, elements, screen_h)
            log("MATCH", f"generic video -> first title {cand['text'][:50]!r} "
                         f"-> clicking thumbnail at ({x}, {y})")
            return {"action": "click", "x": x, "y": y}
    return None        # let Groq handle anything else


def _paste_text(text: str):
    plat.paste_text(text)


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
        keys = plat.normalize_hotkey_keys(action.get("keys", []))
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
# Compact dark palette
BG = "#1a1a2e"; CARD = "#262642"; TEXT = "#e8e8ff"; MUTED = "#8a8ab0"
ACCENT = "#5b8cff"; REC = "#ff5a5f"; OK = "#4ade80"; WARN = "#ffb86b"
IDLE_DOT = "#3a3a55"
IDLE_MSG = "Ready"
METER_W, METER_H = 268, 6
TALK_KEYCODE = 50          # macOS keycode for the ` / ~ key (kVK_ANSI_Grave)


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
        self._reset_after = None  # pending "reset to idle" timer id
        self._key_q: queue.Queue = queue.Queue()
        self._grave_down = False
        self._build_ui()
        self._reset_idle()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        hint = plat.permission_hints("voice_control")
        if hint:
            log("STARTUP", hint)
        self._start_key_listener()

    def _on_close(self):
        feature_bus.remove_presence("voice_control")
        self.destroy()

    def _build_ui(self):
        self.title("Accessibility4all")
        self.resizable(False, False)
        self.configure(bg=BG)
        self.attributes("-topmost", True)

        # header: app name + mic dot (also a click-and-hold fallback for talk)
        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", padx=14, pady=(10, 2))
        tk.Label(header, text="Accessibility4all",
                 font=tkfont.Font(family="Helvetica", size=11, weight="bold"),
                 fg=TEXT, bg=BG).pack(side="left")
        self.mic_btn = tk.Label(header, text="●",
                                font=tkfont.Font(family="Helvetica", size=15),
                                fg="white", bg=IDLE_DOT, padx=7, cursor="hand2")
        self.mic_btn.pack(side="right")
        self.mic_btn.bind("<ButtonPress-1>", lambda e: self._start_recording())
        self.mic_btn.bind("<ButtonRelease-1>", lambda e: self._stop_and_process())

        # dynamic status line (the little "what it's doing" updates)
        self.status_var = tk.StringVar(value="")
        self.status_label = tk.Label(
            self, textvariable=self.status_var,
            font=tkfont.Font(family="Helvetica", size=13),
            fg=MUTED, bg=BG, wraplength=METER_W + 4, justify="left", anchor="w",
        )
        self.status_label.pack(fill="x", padx=16, pady=(6, 6))

        # thin audio meter (shows it's hearing you)
        self._meter_canvas = tk.Canvas(self, width=METER_W, height=METER_H,
                                       bg=CARD, highlightthickness=0)
        self._meter_canvas.pack(padx=16)
        self._meter_bar = self._meter_canvas.create_rectangle(
            0, 0, 0, METER_H, fill=ACCENT, outline="")

        # trial info + persistent hint
        self._trial_var = tk.StringVar(value="")
        tk.Label(self, textvariable=self._trial_var,
                 font=tkfont.Font(family="Helvetica", size=9),
                 fg="#5a5a78", bg=BG, anchor="w").pack(fill="x", padx=16, pady=(4, 0))
        tk.Label(self, text="hold   `   to talk",
                 font=tkfont.Font(family="Helvetica", size=9),
                 fg="#5a5a78", bg=BG, anchor="w").pack(fill="x", padx=16, pady=(0, 8))

        w, h = 300, 152
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{w}x{h}+{sw - w - 24}+{sh - h - 80}")
        self.lift()
        self.bind("<Configure>", lambda e: self.after(100, self._update_presence))
        self.after(100, self._update_presence)

    def _update_presence(self):
        feature_bus.update_presence(
            "voice_control",
            os.getpid(),
            {"x": self.winfo_x(), "y": self.winfo_y(), "w": 300, "h": 152},
        )

    # ── meter (polled on the main thread; never updated from audio thread) ──
    def _draw_meter(self, energy: float):
        max_energy = 4000
        width = min(int(energy / max_energy * METER_W), METER_W)
        color = (OK if width > METER_W * 0.55
                 else "#ffd166" if width > METER_W * 0.15 else ACCENT)
        self._meter_canvas.coords(self._meter_bar, 0, 0, width, METER_H)
        self._meter_canvas.itemconfig(self._meter_bar, fill=color)

    def _poll_meter(self):
        if not self._recording:
            self._draw_meter(0)
            return
        self._draw_meter(self._recorder.level)
        self.after(30, self._poll_meter)

    def _set_status(self, msg: str, color: str = ACCENT):
        # Marshal onto the main thread — safe to call from worker threads.
        self.after(0, lambda: (self.status_var.set(msg),
                               self.status_label.configure(fg=color)))

    def _set_trial_info(self, msg: str):
        self.after(0, lambda: self._trial_var.set(msg))

    def _set_indicator(self, state: str):
        color = {"idle": IDLE_DOT, "rec": REC, "busy": ACCENT, "done": OK}.get(state, IDLE_DOT)
        self.after(0, lambda: self.mic_btn.configure(bg=color))

    # ── global ` (backtick) push-to-talk ───────────────────────────────────────
    def _start_key_listener(self):
        """Listen for the ` key system-wide (Quartz on macOS, pynput elsewhere)."""
        if plat.IS_MAC:
            self._start_mac_quartz_listener()
        else:
            self._start_pynput_grave_listener()

    def _start_pynput_grave_listener(self):
        from pynput import keyboard as pkb

        def on_press(key):
            if self._busy or self._recording:
                return
            if hasattr(key, "char") and key.char == "`":
                self._key_q.put("down")

        def on_release(key):
            if hasattr(key, "char") and key.char == "`":
                self._key_q.put("up")

        listener = pkb.Listener(on_press=on_press, on_release=on_release)
        listener.start()
        log("HOTKEY", "global ` (backtick) push-to-talk active (pynput)")
        self._poll_keys()

    def _start_mac_quartz_listener(self):
        try:
            import Quartz
        except Exception as e:
            log("HOTKEY", f"Quartz unavailable — ` key disabled, use mic dot: {e}", "WARN")
            return

        MODS = (Quartz.kCGEventFlagMaskShift | Quartz.kCGEventFlagMaskCommand
                | Quartz.kCGEventFlagMaskControl | Quartz.kCGEventFlagMaskAlternate)

        def cb(proxy, etype, event, refcon):
            if etype in (Quartz.kCGEventTapDisabledByTimeout,
                         Quartz.kCGEventTapDisabledByUserInput):
                Quartz.CGEventTapEnable(self._tap, True)
                return event
            code = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
            if code == TALK_KEYCODE and not (Quartz.CGEventGetFlags(event) & MODS):
                if etype == Quartz.kCGEventKeyDown:
                    repeat = Quartz.CGEventGetIntegerValueField(
                        event, Quartz.kCGKeyboardEventAutorepeat)
                    if not repeat and not self._grave_down:
                        self._grave_down = True
                        self._key_q.put("down")
                elif etype == Quartz.kCGEventKeyUp and self._grave_down:
                    self._grave_down = False
                    self._key_q.put("up")
                return None        # suppress ` so it isn't typed anywhere
            return event

        def run():
            mask = (Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
                    | Quartz.CGEventMaskBit(Quartz.kCGEventKeyUp))
            tap = Quartz.CGEventTapCreate(
                Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
                Quartz.kCGEventTapOptionDefault, mask, cb, None)
            if not tap:
                log("HOTKEY", "no Accessibility permission — ` key off, use mic dot", "WARN")
                self._key_q.put("noperm")
                return
            self._tap = tap
            src = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
            Quartz.CFRunLoopAddSource(Quartz.CFRunLoopGetCurrent(), src,
                                      Quartz.kCFRunLoopCommonModes)
            Quartz.CGEventTapEnable(tap, True)
            log("HOTKEY", "global ` (backtick) push-to-talk active")
            Quartz.CFRunLoopRun()

        threading.Thread(target=run, daemon=True).start()
        self._poll_keys()

    def _poll_keys(self):
        try:
            while True:
                ev = self._key_q.get_nowait()
                if ev == "down":
                    self._start_recording()
                elif ev == "up":
                    self._stop_and_process()
                elif ev == "noperm":
                    self._set_status("Enable Accessibility for the ` key —\n"
                                     "for now, hold the ● dot to talk", WARN)
        except queue.Empty:
            pass
        self.after(20, self._poll_keys)

    # ── push-to-talk: record while held (` key or mic dot), act on release ──
    def _start_recording(self):
        if self._busy or self._recording:
            return
        if self._reset_after:                       # cancel a pending idle reset
            self.after_cancel(self._reset_after)
            self._reset_after = None
        log("UI", "talk started — recording")
        try:
            self._recorder.start()
        except Exception as e:
            log("RECORD", f"failed to open microphone: {e}", "ERROR")
            self._set_status(f"Mic error: {e}", REC)
            return
        self._recording = True
        self._set_indicator("rec")
        self._set_status("● Listening…", REC)
        self._poll_meter()

    def _stop_and_process(self):
        if not self._recording:
            return
        log("UI", "talk released — processing")
        self._recording = False
        audio = self._recorder.stop()
        self._draw_meter(0)
        if audio is None:
            self._set_indicator("idle")
            self._set_status("Too short — hold ` a moment longer", WARN)
            self._schedule_reset()
            return
        self._busy = True
        self._set_indicator("busy")
        self._set_status("Processing…", ACCENT)
        log("PIPELINE", "=== launching processing thread ===")
        threading.Thread(target=self._process, args=(audio,), daemon=True).start()

    # ── reset back to idle once a command finishes ──
    def _schedule_reset(self, delay_ms: int = 2500):
        if self._reset_after:
            self.after_cancel(self._reset_after)
        self._reset_after = self.after(delay_ms, self._reset_idle)

    def _reset_idle(self):
        self._reset_after = None
        if self._busy or self._recording:
            return
        self._set_indicator("idle")
        self.status_var.set(IDLE_MSG)
        self.status_label.configure(fg=MUTED)
        self._trial_var.set("")
        self._draw_meter(0)

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
            self._set_status("Transcribing…", ACCENT)
            try:
                with Timer("TRANSCRIBE"):
                    command = transcribe(audio)
            except sr.UnknownValueError:
                log("TRANSCRIBE", "Google STT could not understand the audio", "WARN")
                self._set_status("Didn't catch that — try again", WARN)
                _log_trial({"event": "unrecognized", "success": False})
                self._schedule_reset()
                return
            except sr.RequestError as e:
                log("TRANSCRIBE", f"STT request error (network/timeout?): {e}", "ERROR")
                self._set_status(f"Speech service error: {e}", REC)
                _log_trial({"event": "stt_request_error", "error": str(e), "success": False})
                self._schedule_reset()
                return

            trial["command"] = command
            self._set_status(f'Heard: "{command}"', ACCENT)

            handled, err = try_forward_to_page_reader(command)
            if handled:
                if err:
                    self._set_status(err, WARN)
                    trial["error"] = err
                    trial["success"] = False
                else:
                    self._set_status("Sent to Page Reader", OK)
                    trial["forwarded"] = "page_reader"
                    trial["success"] = True
                    self._set_trial_info("read command → page_reader")
                    log("PIPELINE", "SUCCESS — forwarded to page_reader")
                _log_trial(trial)
                return

            # ── Stage 2: make sure Chrome is the active app ──────────────────
            log("PIPELINE", "step 2 — focus Google Chrome")
            self._set_status("Focusing Chrome…", ACCENT)
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

                label = f"Step {i + 1}/{len(commands)} · " if len(commands) > 1 else ""
                log("PIPELINE", f"--- {label}{sub!r} ---")
                trial["_step"] = i + 1                  # for error reporting
                res = self._run_subcommand(sub, label)
                steps.append(res)

            trial["steps"] = [{k: s[k] for k in ("command", "action", "result", "method")}
                              for s in steps]
            trial["success"] = True
            trial.pop("_step", None)

            summary = (steps[-1]["result"] if len(commands) == 1
                       else f"{len(commands)} steps done")
            self._set_status(f"✓ {summary}", OK)
            _log_trial(trial)
            self._set_trial_info(f"logged → {TRIAL_LOG.name}")
            log("PIPELINE", f"SUCCESS — {summary}")

        except json.JSONDecodeError as e:
            log("GROQ", f"model returned non-JSON: {e}", "ERROR")
            trial["error"] = f"JSONDecodeError: {e}"
            trial["success"] = False
            self._set_status("AI gave a bad response — try again", WARN)
            _log_trial(trial)
        except Exception as e:
            step = trial.get("_step")
            where = f" (step {step})" if step else ""
            log("PIPELINE", f"unexpected error{where}: {type(e).__name__}: {e}", "ERROR")
            trial["error"] = str(e)
            trial["success"] = False
            self._set_status(f"Couldn't do that{where} — {e}", REC)
            _log_trial(trial)
        finally:
            done.set()
            total = time.perf_counter() - pipeline_t0
            log("PIPELINE", f"=== finished in {total:.2f}s ===")
            self._busy = False
            self._set_indicator("done" if trial.get("success") else "idle")
            self.after(0, self._schedule_reset)     # reset to idle shortly after

    def _run_subcommand(self, sub: str, label: str = "") -> dict:
        """Resolve ONE command and run it. Tries the shortcut/typing fast-path
        first; otherwise takes a FRESH screenshot and asks Groq what to do, so
        stacked steps act on the current screen (e.g. click after navigating).
        `label` prefixes the on-screen status (e.g. "Step 2/3 · ")."""
        action, desc = match_shortcut(sub)
        if action is not None:
            log("RESOLVE", f"{sub!r} -> shortcut: {desc}")
            self._set_status(f"{label}{desc}", ACCENT)
            method, ecount = "shortcut", None
        else:
            log("RESOLVE", f"{sub!r} -> no shortcut, taking a screenshot")
            self._set_status(f"{label}Looking at the screen…", ACCENT)
            elements = capture_screen_elements()
            ecount = len(elements)
            # Try matching the spoken title directly first (deterministic);
            # only ask Groq if no on-screen text matches confidently.
            local = match_click_target(sub, elements)
            if local is not None:
                action, method = local, "match"
            else:
                self._set_status(f"{label}Asking AI…", ACCENT)
                action = ask_groq(self.groq, sub, elements)
                method = "vision"
        with Timer("EXECUTE"):
            result = execute_action(action)
        log("EXECUTE", f"result: {result}")
        self._set_status(f"{label}✓ {result}", OK)
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
    log("STARTUP", "UI ready — hold the ` key or ● dot to talk")
    app.mainloop()
    feature_bus.remove_presence("voice_control")
    log("SHUTDOWN", "main loop exited")
