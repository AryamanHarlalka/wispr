"""~/.murmur/ config home: modes, snippets, vocab, corrections, history."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

MURMUR_HOME = Path(os.environ.get("MURMUR_HOME", Path.home() / ".murmur"))
VAULT = Path(
    os.environ.get("MURMUR_VAULT", Path.home() / "Desktop" / "Alex's AI Brain")
)

DEFAULT_MODES = """\
# Murmur app-aware modes (F2). bundle id prefix -> mode.
# modes: casual | email | technical | neutral
[modes]
"com.tinyspeck.slackmacgap" = "casual"
"com.apple.MobileSMS" = "casual"
"net.whatsapp.WhatsApp" = "casual"
"com.apple.mail" = "email"
"com.google.Chrome" = "neutral"
"com.todesktop" = "technical"   # Cursor
"com.microsoft.VSCode" = "technical"
"com.apple.Terminal" = "technical"
"com.googlecode.iterm2" = "technical"
default = "neutral"
"""

DEFAULT_SNIPPETS = """\
# Murmur snippets (F5): spoken trigger -> inserted block.
[snippets]
"my email" = "you@example.com"
"my work email" = "you@work.example.com"
"sign off" = "Best,\\nYour Name"
"""

STYLE_BLOCKS = {
    "casual": (
        "Destination is a chat app. Lowercase-casual: no capital letters at "
        "sentence starts unless proper nouns, no trailing period on the last "
        "line, no greeting, no sign-off. Terse."
    ),
    "email": (
        "Destination is email. Proper sentences, short paragraphs. If the "
        "dictation clearly opens a message, keep any greeting as spoken; do "
        "not invent one. No sign-off unless spoken. Terse, direct, no filler."
    ),
    "technical": (
        "Destination is a code editor or terminal. Preserve technical tokens "
        "exactly: snake_case, camelCase, paths, flags, commands. Do not add "
        "punctuation to things that look like code. Minimal cleanup only."
    ),
    "neutral": (
        "General destination. Clean, terse sentences. Proper punctuation and "
        "casing. No filler."
    ),
}


def ensure_home() -> None:
    MURMUR_HOME.mkdir(parents=True, exist_ok=True)
    modes = MURMUR_HOME / "modes.toml"
    if not modes.exists():
        modes.write_text(DEFAULT_MODES)
    snippets = MURMUR_HOME / "snippets.toml"
    if not snippets.exists():
        snippets.write_text(DEFAULT_SNIPPETS)
    (MURMUR_HOME / "corrections.tsv").touch()
    (MURMUR_HOME / "history.jsonl").touch()


def load_modes() -> dict[str, str]:
    ensure_home()
    with open(MURMUR_HOME / "modes.toml", "rb") as f:
        return tomllib.load(f).get("modes", {})


def load_snippets() -> dict[str, str]:
    ensure_home()
    with open(MURMUR_HOME / "snippets.toml", "rb") as f:
        return tomllib.load(f).get("snippets", {})


def load_corrections() -> dict[str, str]:
    ensure_home()
    out: dict[str, str] = {}
    for line in (MURMUR_HOME / "corrections.tsv").read_text().splitlines():
        if "\t" in line:
            wrong, right = line.split("\t", 1)
            out[wrong.strip()] = right.strip()
    return out


def add_correction(wrong: str, right: str) -> None:
    ensure_home()
    with open(MURMUR_HOME / "corrections.tsv", "a") as f:
        f.write(f"{wrong}\t{right}\n")


def load_vocab() -> list[str]:
    ensure_home()
    p = MURMUR_HOME / "vocab.txt"
    if not p.exists():
        return []
    return [w.strip() for w in p.read_text().splitlines() if w.strip()]


def anthropic_key() -> str | None:
    """Env first, then macOS keychain item 'murmur-anthropic'."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", "murmur-anthropic", "-w"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return None
