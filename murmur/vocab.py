"""F3 — vault vocab pipeline. Reads the brain, emits ~/.murmur/vocab.txt.

Guardrail (spec F3): TERMS ONLY — names, companies, project slugs, domain
words. Never facts, never note content. This is the only brain-derived
artifact that ever reaches the API prompt.
"""
from __future__ import annotations

import re
from pathlib import Path

from .config import MURMUR_HOME, VAULT, ensure_home

SEED_TERMS = [
    "Alex", "Rao", "Maya", "Rao", "Sam", "Lee", "Anika",
    "Raj", "Priya", "Dev Rao", "Nina",
    "Atlas", "Northwind", "NASA", "Nest", "Murmur", "Nemo", "Neev",
    "Acme", "ACME", "Crossfit", "Arbor", "WhisperX", "Wispr Flow",
    "Superwhisper", "Vercel", "Supabase", "Anthropic", "Claude", "Fable",
    "Haiku", "Sonnet", "Opus", "Ottawa", "Bangalore", "PPAI",
]

_NAME_RE = re.compile(r"^[a-z0-9-]+$")


def _title_from_slug(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.split("-"))


def _frontmatter_aliases(text: str) -> list[str]:
    m = re.search(r"^aliases:\s*\[([^\]]*)\]", text, re.M)
    if not m:
        return []
    return [a.strip().strip("'\"") for a in m.group(1).split(",") if a.strip()]


def generate(vault: Path = VAULT) -> list[str]:
    terms: dict[str, None] = {t: None for t in SEED_TERMS}

    def add(t: str) -> None:
        t = t.strip()
        if 2 <= len(t) <= 40 and not t.startswith("["):
            terms[t] = None

    for sub in ("wiki/people", "wiki/companies"):
        d = vault / sub
        if d.is_dir():
            for f in d.glob("*.md"):
                if _NAME_RE.match(f.stem):
                    add(_title_from_slug(f.stem))
                try:
                    for a in _frontmatter_aliases(f.read_text(errors="ignore")[:2000]):
                        add(a)
                except OSError:
                    pass

    bench = vault / "ops/work/the-bench"
    if bench.is_dir():
        for d in bench.iterdir():
            if d.is_dir():
                add(_title_from_slug(d.name))
    work = vault / "ops/work"
    if work.is_dir():
        for d in work.iterdir():
            if d.is_dir() and d.name != "the-bench":
                add(_title_from_slug(d.name))

    return list(terms.keys())


def write_vocab(vault: Path = VAULT) -> Path:
    ensure_home()
    out = MURMUR_HOME / "vocab.txt"
    out.write_text("\n".join(generate(vault)) + "\n")
    return out


def initial_prompt(vocab: list[str], corrections: dict[str, str]) -> str:
    """Whisper bias prompt: a natural-ish sentence listing the terms.

    Kept deliberately small (B2): Whisper's prompt window is 224 tokens —
    beyond ~60 multi-token names the tail is truncated anyway, and every
    prompt token is re-encoded as decoder prefix on every dictation (and on
    every chunk once incremental decoding is on). Corrections' right-hand
    sides go first: they're terms the user explicitly flagged as misheard,
    so they're the highest-value bias targets.
    """
    terms = list(dict.fromkeys(list(corrections.values()) + vocab))[:60]
    return f"Notes mentioning {', '.join(terms)}."
