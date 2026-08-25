"""One place to name the Groq models every feature uses.

Groq retires hosted models, and when one goes the whole account loses it at
once — `llama-3.3-70b-versatile` and `llama-4-scout` both 404'd here after
they were decommissioned. Keeping the ids in a single module means a
retirement is a one-line fix instead of a hunt through five files.

Both current models reason before answering, which costs tokens and prints
`<think>` blocks the callers would have to parse. `TEXT_ARGS` / `VISION_ARGS`
turn that down to the least the model allows, and `strip_reasoning` cleans up
anything that leaks through anyway.

Check what an account can actually reach with:

    client.models.list()
"""

from __future__ import annotations

import re

# Chat / JSON extraction. gpt-oss needs reasoning_effort="low" to answer
# directly; without it, short max_tokens budgets are spent reasoning and the
# reply comes back truncated and unparseable.
TEXT_MODEL = "openai/gpt-oss-120b"
TEXT_ARGS = {"reasoning_effort": "low"}

# Screenshot understanding. qwen3.6 is the only vision-capable model on the
# account; it rejects "low" and accepts only "none" or "default".
VISION_MODEL = "qwen/qwen3.6-27b"
VISION_ARGS = {"reasoning_effort": "none"}

# Reasoning tokens are billed against max_tokens, so anything below this
# risks a reply that stops mid-JSON.
MIN_MAX_TOKENS = 512

_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)


def strip_reasoning(text: str) -> str:
    """Drop a leading <think>…</think> block a reasoning model may emit."""
    if not text:
        return ""
    cleaned = _THINK_RE.sub("", text)
    # An unclosed block means the reply was cut off mid-thought; there is no
    # answer after it to keep.
    if "<think>" in cleaned.lower():
        cleaned = cleaned[:cleaned.lower().index("<think>")]
    return cleaned.strip()
