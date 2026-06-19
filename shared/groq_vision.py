"""Groq vision helpers for summarizing page screenshots."""

from __future__ import annotations

import base64
import io
import re

from groq import Groq
from PIL import Image

VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
# Click localization is now the PRIMARY vision path for voice control. Llama-4
# Maverick is the stronger localizer, but it is NOT enabled on this Groq account
# (Scout is the only vision model available), so we use Scout here too. If/when
# Maverick is enabled, set this to "meta-llama/llama-4-maverick-17b-128e-instruct"
# and ask_groq_vision will prefer it and fall back to Scout automatically.
VISION_CLICK_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
GROQ_TIMEOUT = 45

PAGE_SUMMARY_PROMPT = """You are a screen reader. Read the VISIBLE TEXT in this Chrome screenshot aloud.

Rules:
- Output ONLY the actual words shown on the page (titles, paragraphs, labels, list items).
- For feeds/lists: one item per sentence, using the exact title text.
- For Google AI Overview: read the overview TEXT inside the box, not the label "AI Overview".
- Do NOT summarize, interpret, or comment. Never say "this page shows" or "you have".
- Skip browser tabs, address bar, and app menus.
- Plain text only — no bullets, markdown, or labels like "Summary:"."""


def image_to_data_url(img: Image.Image, max_size: int = 1280) -> str:
    img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > max_size:
        scale = max_size / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def summarize_page_image(client: Groq, img: Image.Image, context: str = "") -> str:
    """Return a spoken script of important on-page content from a screenshot."""
    data_url = image_to_data_url(img)
    user_text = PAGE_SUMMARY_PROMPT
    if context.strip():
        user_text += f"\n\nFocus only on text related to: {context.strip()}"

    response = client.chat.completions.create(
        model=VISION_MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
        temperature=0,
        max_tokens=1024,
        timeout=GROQ_TIMEOUT,
    )
    text = (response.choices[0].message.content or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def script_to_lines(script: str) -> list[str]:
    """Keep Groq output as few spoken chunks as possible (avoid over-splitting)."""
    script = re.sub(r"\s+", " ", (script or "").strip())
    if not script:
        return []
    # One continuous utterance unless the model used clear paragraph breaks.
    if "\n" in script:
        parts = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n+", script) if p.strip()]
        return parts if parts else [script]
    return [script]
