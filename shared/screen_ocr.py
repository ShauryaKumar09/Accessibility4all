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


def _virtual_desktop() -> tuple[int, int, int, int]:
    """The bounding box of ALL monitors, in logical pixels (Windows)."""
    import ctypes

    u = ctypes.windll.user32
    # SM_XVIRTUALSCREEN=76, SM_YVIRTUALSCREEN=77, CX=78, CY=79
    return (u.GetSystemMetrics(76), u.GetSystemMetrics(77),
            u.GetSystemMetrics(78), u.GetSystemMetrics(79))


def grab(region: tuple[int, int, int, int] | None = None) -> Image.Image:
    """Screenshot that works on a SECOND monitor, unlike `pyautogui`.

    `pyautogui.screenshot()` only ever sees the primary display: ask it for a
    region on a monitor to the right (or above/left of the origin) and it
    returns a pure-black image, which OCRs to zero elements — every click then
    fails with "model returned index -1, valid range 0..-1". `ImageGrab` with
    `all_screens=True` spans the whole virtual desktop, so it is the only
    capture path here. Coordinates stay in virtual-desktop space, which is the
    same space `pyautogui.click()` moves in, so callers need no extra offset.
    """
    if plat.IS_WINDOWS:
        from PIL import ImageGrab

        if region:
            left, top, width, height = region
            box = (left, top, left + max(1, width), top + max(1, height))
        else:
            vx, vy, vw, vh = _virtual_desktop()
            box = (vx, vy, vx + vw, vy + vh)
        return ImageGrab.grab(bbox=box, all_screens=True)
    return (pyautogui.screenshot(region=region) if region
            else pyautogui.screenshot())


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
        screenshot = grab((left, top, width, height))
        offset_x, offset_y = left, top
        _log("SCREENSHOT", f"region {width}x{height} at ({left}, {top})")
    else:
        screenshot = grab()
        if plat.IS_WINDOWS:
            # A full-screen grab spans every monitor, so it is measured against
            # the virtual desktop, not the primary display, and its top-left
            # can be negative when a monitor sits left of / above the origin.
            offset_x, offset_y, _, _ = _virtual_desktop()
    if plat.IS_WINDOWS and not region:
        _, _, logical_w, logical_h = _virtual_desktop()
    else:
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


# Chrome's own tab strip + toolbar/URL bar (not page content) sit in a fixed
# band at the top of every window, packed with small icons — favicons, tab
# close buttons, extension icons. Tesseract reads those as garbled 1-3 char
# tokens at moderate confidence (~44-80), not low enough to be caught by the
# normal conf<40 cutoff, and merge_line_elements/merge_paragraph_elements can
# then stitch that noise into nonsense lines that leak into whatever reads
# OCR'd Chrome text (page_reader, voice_control's click targeting,
# tone_reader's Shift+Click). None of those features want the browser's own UI
# anyway, so crop it out before OCR ever sees it. This is an approximation
# (tuned for Chrome's default theme/zoom at 100% display scale) — a few px
# of page content may get clipped at some zoom levels, which is a much
# smaller cost than reading garbage as if it were page text.
_CHROME_CHROME_PX = 116


def _below_chrome_ui(bounds: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    left, top, width, height = bounds
    crop = min(_CHROME_CHROME_PX, max(0, height - 50))
    return left, top + crop, width, max(1, height - crop)


def capture_chrome_screenshot() -> Image.Image:
    """Screenshot the front Chrome window's page area (tabs/toolbar excluded)."""
    from PIL import Image

    plat.activate_chrome()
    bounds = plat.get_chrome_window_bounds()
    if bounds:
        bounds = _below_chrome_ui(bounds)
    shot = grab(bounds) if bounds else grab()
    return shot


def capture_chrome_elements(log_fn: LogFn | None = None) -> list[dict]:
    """Focus Chrome and OCR only its page area (tabs/toolbar excluded — see
    `_below_chrome_ui`, they're not page content and Tesseract reads their
    icons as garbled text)."""
    plat.activate_chrome(log_fn=log_fn)
    bounds = plat.get_chrome_window_bounds(log_fn=log_fn)
    if bounds:
        return capture_screen_elements(log_fn=log_fn, region=_below_chrome_ui(bounds))
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


_BOILERPLATE_SKIP = re.compile(
    r"^(home|shorts|subscriptions|explore|library|history|sign in|sign up|log in|menu|search|"
    r"images?|videos?|news|shopping|maps|more|tools|all|about|ai overview|"
    r"accept(\s+all)?|reject(\s+all)?|cookies?|privacy policy|terms of service|"
    r"sponsored|advertisement|ad|skip ad|share|save|subscribe|follow|notifications)$",
    re.I,
)


def all_reading_order(elements: list[dict]) -> list[str]:
    """Paragraph texts in reading order — one entry per merged block, nav/boilerplate skipped."""
    ordered = sorted(elements, key=lambda e: (e["y0"], e["x0"]))
    lines = []
    for e in ordered:
        text = (e.get("text") or "").strip()
        if len(text) < 2:
            continue
        if _BOILERPLATE_SKIP.match(text.strip()):
            continue
        if lines and text == lines[-1]:
            continue
        lines.append(text)
    return lines


# ── finding an input field by how it LOOKS, not what it says ──────────────────
# OCR only finds a search box when the placeholder text happens to say "search",
# which plenty of sites don't (Amazon's says "Ask Alexa a question") and plenty
# more leave empty. Asking a vision model to point at it is unreliable in the
# other direction — it answers with a plausible-looking coordinate even when it
# has no idea, and a wrong coordinate means a click on whatever is there.
#
# A text input has a shape worth looking for directly: a wide, short, evenly
# filled rectangle, bounded on every side by something that isn't it. That is
# cheap to find in the pixels and, unlike the model, it can say "nothing here".
_FIELD_MIN_W = 180          # narrower than this is a button, not a field
_FIELD_MIN_H, _FIELD_MAX_H = 20, 70
_FIELD_MAX_W_FRAC = 0.7     # wider than this is a banner or the page itself
_FIELD_EDGE_GAP = 10        # a field never runs to the viewport edge
_FIELD_FLAT_TOL = 12        # max channel-sum change between neighbouring pixels
_FIELD_MIN_SIDE_CONTRAST = 25
_FIELD_TOP_FRACTION = 0.35  # search bars live in the top third of the page


def find_input_field(img: "Image.Image",
                     top_fraction: float = _FIELD_TOP_FRACTION) -> tuple | None:
    """Locate the most input-shaped rectangle near the top of a page image.

    Returns (x0, y0, x1, y1) in image coordinates, or None when nothing in the
    band looks like a field — the None is the point of this function, so callers
    can fall back instead of clicking a guess.
    """
    try:
        import numpy as np
    except ImportError:
        return None

    a = np.asarray(img.convert("RGB")).astype(np.int16)
    height, width, _ = a.shape
    limit = max(1, int(height * top_fraction))
    band = a[:limit]

    # a pixel is "flat" when it barely differs from the pixel to its right; a
    # filled input is a solid run of them, text and images are not
    flat = np.abs(band[:, 1:, :] - band[:, :-1, :]).sum(axis=2) < _FIELD_FLAT_TOL

    runs: dict[int, list[tuple[int, int]]] = {}
    for y in range(limit):
        row, x = flat[y], 0
        while x < width - 1:
            if row[x]:
                start = x
                while x < width - 1 and row[x]:
                    x += 1
                if x - start >= _FIELD_MIN_W:
                    runs.setdefault(y, []).append((start, x))
            x += 1

    best, best_score = None, 0.0
    claimed: set[tuple[int, int]] = set()
    for y in sorted(runs):
        for x0, x1 in runs[y]:
            if (y, x0) in claimed:
                continue
            # stack the rows below whose runs still overlap this one
            lo, hi, bottom = x0, x1, y + 1
            while bottom in runs:
                match = next(((a0, a1) for a0, a1 in runs[bottom]
                              if min(hi, a1) - max(lo, a0) > 0.7 * (hi - lo)), None)
                if match is None:
                    break
                claimed.add((bottom, match[0]))
                lo, hi = max(lo, match[0]), min(hi, match[1])
                bottom += 1

            w, h = hi - lo, bottom - y
            if not (_FIELD_MIN_H <= h <= _FIELD_MAX_H and w >= _FIELD_MIN_W):
                continue
            if (lo < _FIELD_EDGE_GAP or hi > width - _FIELD_EDGE_GAP
                    or w > _FIELD_MAX_W_FRAC * width):
                continue

            inner = a[y:bottom, lo:hi].mean(axis=(0, 1))
            left = a[y:bottom, max(0, lo - 10):lo].reshape(-1, 3)
            right = a[y:bottom, hi:min(width, hi + 10)].reshape(-1, 3)
            above = a[max(0, y - 12):y, lo:hi].reshape(-1, 3)
            below = a[bottom:min(height, bottom + 12), lo:hi].reshape(-1, 3)
            if not len(left) or not len(right) or not len(above) + len(below):
                continue
            side = (np.abs(inner - left.mean(axis=0)).sum()
                    + np.abs(inner - right.mean(axis=0)).sum()) / 2
            if side < _FIELD_MIN_SIDE_CONTRAST:
                continue
            outer = np.concatenate([p for p in (above, below) if len(p)])
            updown = np.abs(inner - outer.mean(axis=0)).sum()

            score = (w / 10 + updown / 6 + side / 10
                     - (y / limit) * 30 + (28 - abs(h - 34)))
            if score > best_score:
                best, best_score = (int(lo), int(y), int(hi), int(bottom)), score
    return best
