"""F3 — vault vocab pipeline. Reads the brain, emits ~/.wispr/vocab.txt.

Guardrail (spec F3): TERMS ONLY — names, companies, project slugs, domain
words. Never facts, never note content. This is the only brain-derived
artifact that ever reaches the API prompt.
"""
from __future__ import annotations

import re
from pathlib import Path

from .config import WISPR_HOME, VAULT, ensure_home

# Shipped seeds are generic tooling terms only. Personal seeds — people's
# names, private project codenames, employers — must NOT live in this file:
# it is committed and shared. Put those in ~/.wispr/seed-terms.txt (one per
# line), which is local-only and never leaves the machine.
SEED_TERMS = [
    "Whisper", "WhisperX", "Anthropic", "Claude", "Haiku", "Sonnet", "Opus",
    "Obsidian", "Supabase", "Vercel", "Postgres", "PostgreSQL", "Docker",
    "TypeScript", "Python", "GitHub", "LaunchAgent", "macOS", "API", "CLI",
    "repo", "PR", "webhook", "endpoint", "schema", "migration",
]


def _personal_seeds() -> list[str]:
    """Local-only seed terms from ~/.wispr/seed-terms.txt (one per line,
    '#' comments allowed). Absent on a fresh install — that's fine."""
    p = WISPR_HOME / "seed-terms.txt"
    if not p.exists():
        return []
    try:
        return [
            ln.strip() for ln in p.read_text().splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        ]
    except OSError:
        return []

_NAME_RE = re.compile(r"^[a-z0-9-]+$")


def _title_from_slug(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.split("-"))


def _frontmatter_aliases(text: str) -> list[str]:
    m = re.search(r"^aliases:\s*\[([^\]]*)\]", text, re.M)
    if not m:
        return []
    return [a.strip().strip("'\"") for a in m.group(1).split(",") if a.strip()]


def generate(vault: Path | None = None) -> list[str]:
    """Seed terms + local personal seeds + (optionally) proper nouns
    harvested from an Obsidian vault. `vault=None` means no vault is
    configured — the common case on a fresh install for someone who
    doesn't use Obsidian — and is not an error."""
    if vault is None:
        vault = VAULT
    terms: dict[str, None] = {t: None for t in SEED_TERMS}
    for t in _personal_seeds():
        terms.setdefault(t, None)
    if vault is None or not Path(vault).is_dir():
        return list(terms.keys())
    vault = Path(vault)

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


def write_vocab(vault: Path | None = None) -> Path:
    ensure_home()
    out = WISPR_HOME / "vocab.txt"
    out.write_text("\n".join(generate(vault)) + "\n")
    return out


def initial_prompt(vocab: list[str], corrections: dict[str, str],
                   dictionary: list[str] | None = None) -> str:
    """Whisper bias prompt: a natural sentence listing the terms.

    Kept deliberately small: Whisper's prompt window is 224 tokens —
    beyond ~60 multi-token names the tail is truncated anyway, and every
    prompt token is re-encoded as decoder prefix on every dictation (and
    on every chunk once incremental decoding is on). Priority order comes
    from dictionary.prompt_terms(): the personal dictionary (curated or
    learned from the user's own edits) first, then the right-hand sides
    of standing corrections, then vault-harvested vocab.
    """
    from . import dictionary as dict_mod
    terms = dict_mod.prompt_terms(vocab, corrections, dictionary, limit=60)
    return f"Notes mentioning {', '.join(terms)}."
