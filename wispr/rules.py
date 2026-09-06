"""Rule-based cleanup floor. Always available, instant, offline.

This is the fallback ladder's bottom rung: every dictation can paste
through this path with zero network and near-zero latency.

2026-09-06: made conservative. The old filler list included "you know",
"i mean" and "like," and the self-correction pattern fired on the word
"actually" — which turned "this is what I mean" into "this is what." and
"I actually like the fact" into "like the fact". A rule that can eat a
word the user meant is worse than no rule: the rules layer now only
removes what is unambiguous (um/uh/erm/hmm and stutter repeats), and
only rewrites self-corrections that are spoken as explicit commands
("no wait", "scratch that", "correction"). Judgement calls belong to the
LLM pass, which sees the whole sentence.
"""
from __future__ import annotations

import re

FILLERS = re.compile(r"(?<![\w'])(?:um+|uh+|erm+|hmm+|uhm+|mm+)(?![\w']),?\s*",
                     re.IGNORECASE)

# "the the", "I I" -- a stutter repeat of a function word. Allow-list only:
# "had had", "that that", "very very" are all legitimate English.
STUTTER = re.compile(r"\b([A-Za-z]{1,4})(?:,)?\s+\1\b(?!\s+\1\b)", re.IGNORECASE)
_STUTTER_WORDS = {"i", "the", "a", "an", "to", "and", "we", "you", "they",
                  "in", "on", "of", "so", "but", "it", "my", "this", "for"}

# spoken punctuation -> symbol (only the unambiguous ones)
SPOKEN_PUNCT = [
    (re.compile(r"\bnew paragraph\b[.,]?", re.I), "\n\n"),
    (re.compile(r"\bnew line\b[.,]?", re.I), "\n"),
    (re.compile(r"\bfull stop\b", re.I), "."),
    (re.compile(r"\bquestion mark\b", re.I), "?"),
    (re.compile(r"\bexclamation (mark|point)\b", re.I), "!"),
]

# Explicit spoken self-corrections only. Group 1 is a SINGLE token:
# replacing more risks eating words ("we need two, no wait, three" must
# become "we need three").
BACKTRACK = re.compile(
    r"([\w'@.-]+)[,]?\s+"
    r"(?:no wait|no,? wait|scratch that|correction|strike that)[,]?\s+"
    r"([\w'@.-]+)",
    re.IGNORECASE,
)


def apply_corrections(text: str, corrections: dict[str, str]) -> str:
    """Apply learned wrong->right substitutions (whole-word, case-
    insensitive). Multi-word keys work too ("docked x" -> "docx")."""
    for wrong, right in corrections.items():
        if not wrong:
            continue
        pat = r"(?<![\w'])" + r"\s+".join(
            re.escape(w) for w in wrong.split()) + r"(?![\w'])"
        text = re.sub(pat, lambda _m, r=right: r, text, flags=re.IGNORECASE)
    return text


def _fix_stutter(m: re.Match) -> str:
    w = m.group(1)
    if w.lower() not in _STUTTER_WORDS:
        return m.group(0)
    return w


def clean(text: str, corrections: dict[str, str] | None = None) -> str:
    t = text.strip()
    if not t:
        return t
    t = FILLERS.sub("", t)
    t = STUTTER.sub(_fix_stutter, t)
    t = BACKTRACK.sub(r"\2", t)
    for pat, repl in SPOKEN_PUNCT:
        t = pat.sub(repl, t)
    if corrections:
        t = apply_corrections(t, corrections)
    # whitespace + spacing around punctuation
    t = re.sub(r"[ \t]+([,.!?;:])", r"\1", t)
    t = re.sub(r"([,.!?;:])\1+", r"\1", t)          # ",," from a removed filler
    t = re.sub(r",\s*([.!?])", r"\1", t)             # ",." from a removed filler
    t = re.sub(r"^[,.;:]\s*", "", t)                # leading orphan punctuation
    t = re.sub(r"\s+([,.!?;:])\s*$", r"\1", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    # sentence casing (light touch — the mode layer decides final casing)
    t = t[0].upper() + t[1:] if t and t[0].islower() else t
    t = re.sub(r"([.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), t)
    t = re.sub(r"\bi\b", "I", t)
    if t and t[-1] not in ".!?\n" and len(t.split()) > 3:
        t += "."
    return t.strip()
