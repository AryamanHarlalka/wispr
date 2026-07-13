"""Rule-based cleanup floor. Always available, instant, offline.

This is the fallback ladder's bottom rung (spec §3): every dictation can
paste through this path with zero network and near-zero latency.
"""
from __future__ import annotations

import re

FILLERS = re.compile(
    r"\b(um+|uh+|erm+|hmm+|you know|i mean|like,|sort of,|kind of,)\b[, ]*",
    re.IGNORECASE,
)

# spoken punctuation → symbol (only the unambiguous ones)
SPOKEN_PUNCT = [
    (re.compile(r"\bnew paragraph\b[.,]?", re.I), "\n\n"),
    (re.compile(r"\bnew line\b[.,]?", re.I), "\n"),
    (re.compile(r"\bcomma\b", re.I), ","),
    (re.compile(r"\bperiod\b", re.I), "."),
    (re.compile(r"\bfull stop\b", re.I), "."),
    (re.compile(r"\bquestion mark\b", re.I), "?"),
    (re.compile(r"\bexclamation (mark|point)\b", re.I), "!"),
]

# "2 actually 3" / "tuesday, no wait, wednesday" — keep the correction
BACKTRACK = re.compile(
    r"([\w'@.-]+(?:\s[\w'@.-]+)?)[,]?\s+"
    r"(?:actually|no wait|no,|i mean|scratch that|sorry)[,]?\s+"
    r"([\w'@.-]+)",
    re.IGNORECASE,
)


def apply_corrections(text: str, corrections: dict[str, str]) -> str:
    for wrong, right in corrections.items():
        text = re.sub(rf"\b{re.escape(wrong)}\b", right, text, flags=re.IGNORECASE)
    return text


def clean(text: str, corrections: dict[str, str] | None = None) -> str:
    t = text.strip()
    if not t:
        return t
    t = FILLERS.sub("", t)
    t = BACKTRACK.sub(r"\2", t)
    for pat, repl in SPOKEN_PUNCT:
        t = pat.sub(repl, t)
    if corrections:
        t = apply_corrections(t, corrections)
    # whitespace + spacing around punctuation
    t = re.sub(r"[ \t]+([,.!?;:])", r"\1", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    # sentence casing (light touch — don't fight intentional lowercase later;
    # the mode layer decides final casing for casual apps)
    t = t[0].upper() + t[1:] if t and t[0].islower() else t
    t = re.sub(r"([.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), t)
    t = re.sub(r"\bi\b", "I", t)
    if t and t[-1] not in ".!?\n" and len(t.split()) > 3:
        t += "."
    return t.strip()
