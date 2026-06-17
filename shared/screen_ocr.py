"""Shared screen OCR helpers used by voice_control and page_reader."""

from __future__ import annotations

import re
from typing import Callable

import pyautogui
import pytesseract
from PIL import Image

from shared import platform as plat

LogFn = Callable[[str, str, str], None]

_configured = False


def _ensure_tesseract():
    global _configured
    if not _configured:
        plat.configure_tesseract()
        _configured = True


def capture_screen_elements(
    log_fn: LogFn | None = None,
    region: tuple[int, int, int, int] | None = None,
) -> list[dict]:
    """Screenshot the screen (or a region), OCR text, group words into lines."""
    _ensure_tesseract()

    def _log(stage: str, msg: str = "", level: str = "INFO"):
        if log_fn:
            log_fn(stage, msg, level)

    offset_x = offset_y = 0
    if region:
        left, top, width, height = region
        width, height = max(1, width), max(1, height)
        screenshot = pyautogui.screenshot(region=(left, top, width, height))
        offset_x, offset_y = left, top
        _log("SCREENSHOT", f"region {width}x{height} at ({left}, {top})")
    else:
        screenshot = pyautogui.screenshot()
    logical_w, logical_h = pyautogui.size()
    phys_w, phys_h = screenshot.size
    if region:
        _, _, region_w, region_h = region
        region_w, region_h = max(1, region_w), max(1, region_h)
        scale_x = region_w / phys_w if phys_w else 1.0
        scale_y = region_h / phys_h if phys_h else 1.0
    else:
        scale_x = logical_w / phys_w if phys_w else 1.0
        scale_y = logical_h / phys_h if phys_h else 1.0
    if not region:
        _log("SCREENSHOT", f"captured {phys_w}x{phys_h} | logical {logical_w}x{logical_h}")

    data = pytesseract.image_to_data(screenshot, output_type=pytesseract.Output.DICT)

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
    for g in groups.values():
        text = " ".join(g["words"]).strip()
        if not text:
            continue
        cx = (g["x0"] + g["x1"]) / 2
        cy = (g["y0"] + g["y1"]) / 2
        elements.append({
            "text": text,
            "x": int(cx * scale_x) + offset_x,
            "y": int(cy * scale_y) + offset_y,
            "x0": int(g["x0"] * scale_x) + offset_x,
            "y0": int(g["y0"] * scale_y) + offset_y,
            "x1": int(g["x1"] * scale_x) + offset_x,
            "y1": int(g["y1"] * scale_y) + offset_y,
        })

    sample = " | ".join(e["text"][:30] for e in elements[:10])
    _log("OCR", f"found {len(elements)} lines. sample: {sample}")
    merged = merge_line_elements(elements)
    paragraphs = merge_paragraph_elements(merged)
    paragraphs = filter_overlay_elements(paragraphs)
    _log("OCR", f"merged to {len(merged)} lines, {len(paragraphs)} paragraphs")
    return paragraphs


# Text from our own feature panels — never click or read this as page content.
_OVERLAY_PHRASES = (
    "accessibility4all", "page reader", "always on", "hover-to-read",
    "voice-guided", "groq summary", "read chrome", "processing",
    "didn't catch", "speak a command", "hold `", "listening",
    "tone analysis", "social cues", "analyzing tone", "what it likely means",
)


def filter_overlay_elements(elements: list[dict]) -> list[dict]:
    """Drop OCR lines that belong to our assistive UI overlays."""
    out = []
    for e in elements:
        t = (e.get("text") or "").strip()
        if len(t) < 2:
            continue
        low = t.lower()
        if any(p in low for p in _OVERLAY_PHRASES):
            continue
        out.append(e)
    return out


def merge_line_elements(elements: list[dict],
                        y_tolerance: int = 18,
                        x_gap: int = 40) -> list[dict]:
    """Merge OCR boxes on the same visual row into one line chunk."""
    if not elements:
        return []
    ordered = sorted(elements, key=lambda e: (e["y0"], e["x0"]))
    merged: list[dict] = []
    current: dict | None = None
    for e in ordered:
        if current is None:
            current = dict(e)
            continue
        same_row = abs(e["y0"] - current["y0"]) <= y_tolerance
        close_x = e["x0"] - current["x1"] <= x_gap
        if same_row and close_x:
            current["text"] = f'{current["text"]} {e["text"]}'.strip()
            current["x1"] = max(current["x1"], e["x1"])
            current["y0"] = min(current["y0"], e["y0"])
            current["y1"] = max(current["y1"], e["y1"])
            current["x"] = (current["x0"] + current["x1"]) // 2
            current["y"] = (current["y0"] + current["y1"]) // 2
        else:
            merged.append(current)
            current = dict(e)
    if current is not None:
        merged.append(current)
    return merged


def merge_paragraph_elements(elements: list[dict]) -> list[dict]:
    """Merge wrapped lines into one paragraph block (multi-line sentences)."""
    if not elements:
        return []
    ordered = sorted(elements, key=lambda e: (e["y0"], e["x0"]))
    out: list[dict] = []
    current: dict | None = None
    for e in ordered:
        if current is None:
            current = dict(e)
            continue
        line_h = max(current["y1"] - current["y0"], 10)
        v_gap = e["y0"] - current["y1"]
        x_align = abs(e["x0"] - current["x0"]) <= 80
        continues = False
        if x_align and v_gap <= line_h * 2.8:
            if not re.search(r'[.!?]["\']?\s*$', current["text"].strip()):
                continues = True
            elif e["text"][:1].islower():
                continues = True
            elif v_gap <= line_h * 0.8:
                continues = True
        if continues:
            joiner = "" if current["text"].endswith("-") else " "
            current["text"] = (current["text"].rstrip("-") + joiner + e["text"]).strip()
            current["x1"] = max(current["x1"], e["x1"])
            current["y1"] = max(current["y1"], e["y1"])
            current["x"] = (current["x0"] + current["x1"]) // 2
            current["y"] = (current["y0"] + current["y1"]) // 2
        else:
            out.append(current)
            current = dict(e)
    if current is not None:
        out.append(current)
    return out


def expand_to_paragraph(elements: list[dict], seed: dict) -> dict:
    """Return the full paragraph chunk containing seed (may be seed itself)."""
    ordered = sorted(elements, key=lambda e: (e["y0"], e["x0"]))
    try:
        idx = next(i for i, e in enumerate(ordered) if e is seed or e["text"] == seed["text"]
                     and e["x0"] == seed["x0"] and e["y0"] == seed["y0"])
    except StopIteration:
        return seed
    low, high = idx, idx
    while low > 0:
        trial = merge_paragraph_elements([ordered[low - 1], ordered[high]])
        if len(trial) == 1:
            low -= 1
        else:
            break
    while high + 1 < len(ordered):
        trial = merge_paragraph_elements([ordered[low], ordered[high + 1]])
        if len(trial) == 1:
            high += 1
        else:
            break
    merged = merge_paragraph_elements(ordered[low:high + 1])
    return merged[0] if merged else seed


def capture_chrome_screenshot() -> Image.Image:
    """Screenshot the front Chrome window only."""
    from PIL import Image

    plat.activate_chrome()
    bounds = plat.get_chrome_window_bounds()
    shot = pyautogui.screenshot(region=bounds) if bounds else pyautogui.screenshot()
    return shot


def capture_chrome_elements(log_fn: LogFn | None = None) -> list[dict]:
    """Focus Chrome and OCR only the front Chrome window."""
    plat.activate_chrome(log_fn=log_fn)
    bounds = plat.get_chrome_window_bounds(log_fn=log_fn)
    if bounds:
        return capture_screen_elements(log_fn=log_fn, region=bounds)
    return capture_screen_elements(log_fn=log_fn)


def region_around_point(x: int, y: int,
                        pad_w: int = 420, pad_h: int = 200) -> tuple[int, int, int, int]:
    """Screen region centered on (x, y), clamped to the display."""
    sw, sh = pyautogui.size()
    left = max(0, x - pad_w // 2)
    top = max(0, y - pad_h // 2)
    right = min(sw, x + pad_w // 2)
    bottom = min(sh, y + pad_h // 2)
    return left, top, max(1, right - left), max(1, bottom - top)


def elements_at_point(elements: list[dict], x: int, y: int) -> dict | None:
    """Return the paragraph chunk at (x, y), or nearest paragraph."""
    hits = [e for e in elements
            if e["x0"] <= x <= e["x1"] and e["y0"] <= y <= e["y1"]]
    if hits:
        seed = max(hits, key=lambda e: len(e.get("text", "")))
        return expand_to_paragraph(elements, seed)
    if not elements:
        return None
    nearest = min(elements, key=lambda e: (e["x"] - x) ** 2 + (e["y"] - y) ** 2)
    if (nearest["x"] - x) ** 2 + (nearest["y"] - y) ** 2 > 140 ** 2:
        return None
    return expand_to_paragraph(elements, nearest)


def click_point_for_element(el: dict) -> tuple[int, int]:
    """Center of element bbox in logical screen coordinates."""
    return (el["x0"] + el["x1"]) // 2, (el["y0"] + el["y1"]) // 2


def elements_in_region(elements: list[dict],
                       x0: int, y0: int, x1: int, y1: int) -> list[dict]:
    """Lines whose bbox intersects the given region."""
    out = []
    for e in elements:
        if e["x1"] < x0 or e["x0"] > x1 or e["y1"] < y0 or e["y0"] > y1:
            continue
        out.append(e)
    return out


def all_reading_order(elements: list[dict]) -> list[str]:
    """Paragraph texts in reading order — one entry per merged block."""
    ordered = sorted(elements, key=lambda e: (e["y0"], e["x0"]))
    lines = []
    for e in ordered:
        text = (e.get("text") or "").strip()
        if len(text) < 2:
            continue
        if lines and text == lines[-1]:
            continue
        lines.append(text)
    return lines
