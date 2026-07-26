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

# NOTE: shipped defaults must stay impersonal — this file is public and this
# template is written verbatim into a new user's ~/.murmur/snippets.toml on
# first run. Personal snippets live only in that local file, never here.
DEFAULT_SNIPPETS = """\
# Murmur snippets (F5): spoken trigger -> inserted block.
# Edit freely — this file is yours and is never committed or uploaded.
[snippets]
# "my email"      = "you@example.com"
# "my work email" = "you@work.example.com"
# "sign off"      = "Best,\\nYour Name"
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


DEFAULT_WHISPER_MODEL = "small.en"


def whisper_model() -> str:
    """Whisper model knob (B2). Priority: MURMUR_WHISPER_MODEL env ->
    ~/.murmur/whisper-model (first line) -> small.en.

    Benchmarked 2026-07-16 (synthetic say-generated audio, isolated):
    distil-small.en was 2-8x slower than small.en on this CPU and
    hallucinated on technical audio — ruled out. base.en is ~3x faster
    but misheard vocab terms ("daemon.py" -> "demon.py"); opt-in only.
    """
    env = os.environ.get("MURMUR_WHISPER_MODEL", "").strip()
    if env:
        return env
    p = MURMUR_HOME / "whisper-model"
    try:
        if p.exists():
            name = p.read_text().strip().splitlines()[0].strip()
            if name:
                return name
    except OSError:
        pass
    return DEFAULT_WHISPER_MODEL


def load_config() -> dict:
    """~/.murmur/config.toml — general knobs (currently: [stt]). Missing file
    or missing keys just fall through to caller defaults."""
    ensure_home()
    p = MURMUR_HOME / "config.toml"
    if not p.exists():
        return {}
    try:
        with open(p, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


DEFAULT_STT_BACKEND = "faster-whisper"


def stt_backend() -> str:
    """STT backend knob (0c). Priority: MURMUR_STT_BACKEND env ->
    ~/.murmur/config.toml [stt].backend -> faster-whisper.

    Benchmarked 2026-07-23 on this (Intel, Iris Plus iGPU) machine: neither
    mlx-whisper (Apple Silicon only, no wheel for this platform) nor
    whisper.cpp's Metal backend (ggml explicitly compiles Metal out for
    Intel macOS) nor whisper.cpp's CPU/BLAS backend (slower than
    faster-whisper's CTranslate2 int8 at equal model size -- e.g. base.en
    encode 1.8s vs 0.73s total) beat faster-whisper here. faster-whisper
    stays the default; "whispercpp" is wired and working (see stt.py) for
    portability to Apple Silicon hardware later, where mlx-whisper or
    whisper.cpp+Metal would likely win instead.
    """
    env = os.environ.get("MURMUR_STT_BACKEND", "").strip()
    if env:
        return env
    v = load_config().get("stt", {}).get("backend", "").strip()
    return v or DEFAULT_STT_BACKEND


DEFAULT_STT_BEAM_SIZE = 1


def stt_beam_size() -> int:
    """Beam size knob (0d): 1 trades a little accuracy for materially
    faster decode; 5 (faster-whisper's own default) is slower. Priority:
    MURMUR_STT_BEAM_SIZE env -> ~/.murmur/config.toml [stt].beam_size -> 1."""
    env = os.environ.get("MURMUR_STT_BEAM_SIZE", "").strip()
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    v = load_config().get("stt", {}).get("beam_size")
    if isinstance(v, int):
        return v
    return DEFAULT_STT_BEAM_SIZE


def stt_model() -> str:
    """STT model knob (0c/B2). Priority: MURMUR_STT_MODEL env ->
    ~/.murmur/config.toml [stt].model -> legacy whisper_model() (env/
    ~/.murmur/whisper-model file) -> small.en."""
    env = os.environ.get("MURMUR_STT_MODEL", "").strip()
    if env:
        return env
    v = load_config().get("stt", {}).get("model", "").strip()
    if v:
        return v
    return whisper_model()


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
