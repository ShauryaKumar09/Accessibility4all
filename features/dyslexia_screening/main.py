"""Dyslexia Screening — a short, non-diagnostic self-check quiz.

Runs as its own process when toggled ON in the hub. See features/README.md.

This is NOT a diagnostic tool. It cannot identify dyslexia. It only flags
patterns (letter-reversal confusion, rhyme recognition, reading speed) that
are sometimes associated with reading differences, so the disclaimer is
shown before the quiz starts and again with the result.
"""

from __future__ import annotations

import os
import random
import signal
import sys
import time
from pathlib import Path

import tkinter as tk
from tkinter import font as tkfont

FEATURE_DIR = Path(__file__).resolve().parent
ROOT = FEATURE_DIR.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import console, feature_bus  # noqa: E402

console.configure_stdio()

BG = "#1a1a2e"
CARD = "#23233f"
FG = "#e0e0ff"
MUTED = "#8a8ab0"
ACCENT = "#748ffc"
OK = "#69db7c"
WARN = "#ffd166"
REC = "#ff6b6b"

WIN_W, WIN_H = 440, 380

DISCLAIMER = (
    "This is NOT a diagnostic tool and cannot identify dyslexia. It only notices "
    "a few patterns that are sometimes associated with reading differences. If "
    "you have concerns about reading difficulty, please consult a qualified "
    "specialist such as an educational psychologist or learning-disabilities specialist."
)

CHOICE_QUESTIONS = [
    {"prompt": "Which one matches the target letter?\n\nTarget: b",
     "options": ["d", "b", "p", "q"], "correct": 1},
    {"prompt": "Which one matches the target letter?\n\nTarget: p",
     "options": ["q", "d", "p", "b"], "correct": 2},
    {"prompt": "Which word is spelled the same forwards and as shown?\n\nTarget: was",
     "options": ["saw", "was", "sae", "aws"], "correct": 1},
    {"prompt": "Which word does NOT rhyme with the others?",
     "options": ["cat", "hat", "dog", "bat"], "correct": 2},
    {"prompt": "Which word does NOT rhyme with the others?",
     "options": ["light", "night", "sight", "bench"], "correct": 3},
    {"prompt": "Put in order: which comes first alphabetically?",
     "options": ["dog", "cat", "ant", "elk"], "correct": 2},
]

READING_PASSAGE = (
    "The quick brown fox jumps over the lazy dog. Reading every day helps build "
    "stronger word recognition and comprehension over time."
)
# Rough typical adult range is ~200 wpm; used only as a loose, non-clinical reference.
TYPICAL_WPM = 200


def log(msg: str):
    console.safe_print(f"[dyslexia_screening] {msg}", flush=True)


class DyslexiaScreeningApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Dyslexia Screening")
        self.resizable(False, False)
        self.geometry(f"{WIN_W}x{WIN_H}")
        self.configure(bg=BG)
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self._shutdown)

        self._questions = random.sample(CHOICE_QUESTIONS, len(CHOICE_QUESTIONS))
        self._idx = 0
        self._flags = 0
        self._reading_start = 0.0

        self._frame = tk.Frame(self, bg=BG)
        self._frame.pack(fill="both", expand=True)
        self._show_intro()

        feature_bus.update_presence("dyslexia_screening", os.getpid())
        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT, self._shutdown)

    def _clear(self):
        for child in self._frame.winfo_children():
            child.destroy()

    def _title(self, text: str):
        tk.Label(self._frame, text=text,
                 font=tkfont.Font(family="Helvetica", size=15, weight="bold"),
                 fg=FG, bg=BG, wraplength=WIN_W - 40, justify="left").pack(
            anchor="w", padx=20, pady=(20, 8))

    def _disclaimer_label(self, parent):
        tk.Label(parent, text=DISCLAIMER, font=tkfont.Font(family="Helvetica", size=9),
                 fg=WARN, bg=BG, wraplength=WIN_W - 40, justify="left").pack(
            anchor="w", padx=20, pady=(0, 12))

    def _show_intro(self):
        self._clear()
        self._title("Dyslexia Screening")
        self._disclaimer_label(self._frame)
        tk.Label(self._frame,
                 text=f"{len(self._questions)} quick questions, then one short "
                      "timed reading task. Answer as best you can.",
                 font=tkfont.Font(family="Helvetica", size=10),
                 fg=MUTED, bg=BG, wraplength=WIN_W - 40, justify="left").pack(
            anchor="w", padx=20, pady=(0, 20))
        tk.Button(self._frame, text="Start", command=self._show_question,
                  font=tkfont.Font(family="Helvetica", size=10, weight="bold"),
                  bg=CARD, fg=FG, relief="flat", padx=16, pady=8,
                  cursor="hand2").pack(anchor="w", padx=20)

    def _show_question(self):
        if self._idx >= len(self._questions):
            self._show_reading_task()
            return
        self._clear()
        q = self._questions[self._idx]
        tk.Label(self._frame, text=f"Question {self._idx + 1} of {len(self._questions)}",
                 font=tkfont.Font(family="Helvetica", size=9),
                 fg=MUTED, bg=BG).pack(anchor="w", padx=20, pady=(16, 4))
        tk.Label(self._frame, text=q["prompt"],
                 font=tkfont.Font(family="Helvetica", size=13, weight="bold"),
                 fg=FG, bg=BG, wraplength=WIN_W - 40, justify="left").pack(
            anchor="w", padx=20, pady=(4, 16))

        options = list(enumerate(q["options"]))
        random.shuffle(options)
        for orig_idx, text in options:
            tk.Button(self._frame, text=text, anchor="w",
                      command=lambda oi=orig_idx: self._answer(q, oi),
                      font=tkfont.Font(family="Helvetica", size=11),
                      bg=CARD, fg=FG, relief="flat", padx=14, pady=8,
                      cursor="hand2").pack(fill="x", padx=20, pady=4)

    def _answer(self, q: dict, chosen_idx: int):
        if chosen_idx != q["correct"]:
            self._flags += 1
        self._idx += 1
        self._show_question()

    def _show_reading_task(self):
        self._clear()
        self._title("Reading speed")
        tk.Label(self._frame, text="Read the passage below aloud or silently, "
                                    "then click Done as soon as you finish.",
                 font=tkfont.Font(family="Helvetica", size=9),
                 fg=MUTED, bg=BG, wraplength=WIN_W - 40, justify="left").pack(
            anchor="w", padx=20, pady=(0, 10))
        tk.Label(self._frame, text=READING_PASSAGE,
                 font=tkfont.Font(family="Helvetica", size=12),
                 fg=FG, bg=CARD, wraplength=WIN_W - 60, justify="left",
                 padx=14, pady=14).pack(fill="x", padx=20)
        self._reading_start = time.perf_counter()
        tk.Button(self._frame, text="Done reading", command=self._finish_reading,
                  font=tkfont.Font(family="Helvetica", size=10, weight="bold"),
                  bg=CARD, fg=FG, relief="flat", padx=16, pady=8,
                  cursor="hand2").pack(anchor="w", padx=20, pady=16)

    def _finish_reading(self):
        elapsed = max(0.5, time.perf_counter() - self._reading_start)
        word_count = len(READING_PASSAGE.split())
        wpm = word_count / (elapsed / 60)
        if wpm < TYPICAL_WPM * 0.5:
            self._flags += 1
        self._show_result(wpm)

    def _show_result(self, wpm: float):
        self._clear()
        if self._flags <= 1:
            bucket, color, msg = "low", OK, "Few patterns showed up in your answers."
        elif self._flags <= 3:
            bucket, color, msg = "medium", WARN, "Some patterns showed up in your answers."
        else:
            bucket, color, msg = "high", REC, "Several patterns showed up in your answers."

        self._title("Result")
        tk.Label(self._frame, text=f"{msg} (reading speed: {wpm:.0f} words/min)",
                 font=tkfont.Font(family="Helvetica", size=12, weight="bold"),
                 fg=color, bg=BG, wraplength=WIN_W - 40, justify="left").pack(
            anchor="w", padx=20, pady=(4, 12))
        self._disclaimer_label(self._frame)
        tk.Button(self._frame, text="Try again", command=self._restart,
                  font=tkfont.Font(family="Helvetica", size=10),
                  bg=CARD, fg=FG, relief="flat", padx=14, pady=6,
                  cursor="hand2").pack(anchor="w", padx=20)
        log(f"result: bucket={bucket} flags={self._flags} wpm={wpm:.0f}")

    def _restart(self):
        self._questions = random.sample(CHOICE_QUESTIONS, len(CHOICE_QUESTIONS))
        self._idx = 0
        self._flags = 0
        self._show_intro()

    def _shutdown(self, *_args):
        log("shutting down")
        feature_bus.remove_presence("dyslexia_screening")
        try:
            self.destroy()
        except Exception:
            pass
        sys.exit(0)


def main():
    log("feature started")
    app = DyslexiaScreeningApp()
    app.mainloop()
    feature_bus.remove_presence("dyslexia_screening")
    log("exited")


if __name__ == "__main__":
    main()
