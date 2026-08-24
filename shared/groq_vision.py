"""Groq vision helpers for summarizing page screenshots."""

from __future__ import annotations

import base64
import io
import re

from groq import Groq
from PIL import Image

from shared.groq_models import (MIN_MAX_TOKENS, VISION_ARGS, VISION_MODEL,
                                strip_reasoning)

# Click localization is the PRIMARY vision path for voice control. It runs on
# the same model as page summaries — see shared/groq_models.py for which one
# and why.
VISION_CLICK_MODEL = VISION_MODEL
GROQ_TIMEOUT = 45

PAGE_SUMMARY_PROMPT = """You help a blind user understand a Chrome web page quickly.

Look at the screenshot and write a SHORT spoken summary of what matters most (2–5 sentences).

Rules:
- Pick the important content only — main headline, key message, top results, or action needed.
- Do NOT read the whole page word-for-word or list every visible line.
- For feeds/search: briefly mention the top few relevant items, not everything.
- For articles: summarize the topic and main takeaway in plain language.
- Skip browser tabs, address bar, menus, ads, and sidebar clutter.
- Plain spoken sentences only. No markdown, bullets, or labels like "Summary:"."""


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


def summarize_page_image(
    client: Groq, img: Image.Image, context: str = "", level_instruction: str = "",
) -> str:
    """Return a spoken script of important on-page content from a screenshot."""
    data_url = image_to_data_url(img)
    user_text = PAGE_SUMMARY_PROMPT
    if context.strip():
        user_text += f"\n\nFocus only on text related to: {context.strip()}"
    if level_instruction.strip():
        user_text += f"\n\n{level_instruction.strip()}"

    response = client.chat.completions.create(
        model=VISION_MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
        temperature=0.2,
        max_tokens=MIN_MAX_TOKENS,
        timeout=GROQ_TIMEOUT,
        **VISION_ARGS,
    )
    text = strip_reasoning(response.choices[0].message.content or "")
    text = re.sub(r"\s+", " ", text)
    return text


ANSWER_QUESTION_PROMPT = """You help a blind user understand a Chrome web page by answering their question.

Look at the screenshot and answer the user's question in 1-3 short spoken sentences.

Rules:
- Answer directly and specifically using only what's visible in the screenshot.
- If the answer isn't visible on screen, say so plainly instead of guessing.
- Plain spoken sentences only. No markdown, bullets, or labels like "Answer:"."""


def answer_page_question(client: Groq, img: Image.Image, question: str) -> str:
    """Return a short spoken answer to a free-form question about the page."""
    data_url = image_to_data_url(img)
    user_text = f"{ANSWER_QUESTION_PROMPT}\n\nQuestion: {question.strip()}"

    response = client.chat.completions.create(
        model=VISION_MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
        temperature=0.2,
        max_tokens=MIN_MAX_TOKENS,
        timeout=GROQ_TIMEOUT,
        **VISION_ARGS,
    )
    text = strip_reasoning(response.choices[0].message.content or "")
    return re.sub(r"\s+", " ", text)


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
