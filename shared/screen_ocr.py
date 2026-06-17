"""Shared screen OCR helpers used by voice_control and page_reader."""

from __future__ import annotations

from typing import Callable

import pyautogui
import pytesseract

from shared import platform as plat

LogFn = Callable[[str, str, str], None]

_configured = False


def _ensure_tesseract():
    global _configured
    if not _configured:
        plat.configure_tesseract()
        _configured = True


def capture_screen_elements(log_fn: LogFn | None = None) -> list[dict]:
    """Screenshot the screen, OCR text, group words into lines, scale coords."""
    _ensure_tesseract()    def _log(stage: str, msg: str = "", level: str = "INFO"):
        if log_fn:
            log_fn(stage, msg, level)

    screenshot = pyautogui.screenshot()
    logical_w, logical_h = pyautogui.size()
    phys_w, phys_h = screenshot.size
    scale_x = logical_w / phys_w
    scale_y = logical_h / phys_h
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
            "x": int(cx * scale_x),
            "y": int(cy * scale_y),
            "x0": int(g["x0"] * scale_x),
            "y0": int(g["y0"] * scale_y),
            "x1": int(g["x1"] * scale_x),
            "y1": int(g["y1"] * scale_y),
        })

    sample = " | ".join(e["text"][:30] for e in elements[:10])
    _log("OCR", f"found {len(elements)} lines. sample: {sample}")
    return elements


def elements_at_point(elements: list[dict], x: int, y: int) -> dict | None:
    """Return the line element whose bbox contains (x, y), or nearest line."""
    hits = [e for e in elements
            if e["x0"] <= x <= e["x1"] and e["y0"] <= y <= e["y1"]]
    if hits:
        return min(hits, key=lambda e: (e["x1"] - e["x0"]) * (e["y1"] - e["y0"]))
    if not elements:
        return None
    return min(elements, key=lambda e: (e["x"] - x) ** 2 + (e["y"] - y) ** 2)


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
    """All line texts in OCR order (top-to-bottom)."""
    return [e["text"] for e in elements if e.get("text")]
