"""~/.wispr/ config home: modes, snippets, vocab, corrections, history."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

WISPR_HOME = Path(os.environ.get("WISPR_HOME", Path.home() / ".wispr"))


def _default_vault() -> Path | None:
    """Optional Obsidian vault used only to harvest proper nouns for the
    STT prompt (see vocab.py). There is deliberately NO built-in default:
    this file ships to other people, so a hardcoded personal path would be
    both wrong for them and a privacy leak. Resolution order:
    WISPR_VAULT env -> ~/.wispr/vault-path (written by the installer) ->
    None, meaning 'no vault, use the built-in seed terms'."""
    env = os.environ.get("WISPR_VAULT", "").strip()
    if env:
        return Path(env).expanduser()
    try:
        p = WISPR_HOME / "vault-path"
        if p.exists():
            line = p.read_text().strip().splitlines()
            if line and line[0].strip():
                return Path(line[0].strip()).expanduser()
    except OSError:
        pass
    return None


VAULT = _default_vault()

DEFAULT_MODES = """\
# Wispr app-aware modes (F2). bundle id prefix -> mode.
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
# template is written verbatim into a new user's ~/.wispr/snippets.toml on
# first run. Personal snippets live only in that local file, never here.
DEFAULT_SNIPPETS = """\
# Wispr snippets (F5): spoken trigger -> inserted block.
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
    WISPR_HOME.mkdir(parents=True, exist_ok=True)
    modes = WISPR_HOME / "modes.toml"
    if not modes.exists():
        modes.write_text(DEFAULT_MODES)
    snippets = WISPR_HOME / "snippets.toml"
    if not snippets.exists():
        snippets.write_text(DEFAULT_SNIPPETS)
    (WISPR_HOME / "corrections.tsv").touch()
    (WISPR_HOME / "history.jsonl").touch()


def load_modes() -> dict[str, str]:
    ensure_home()
    with open(WISPR_HOME / "modes.toml", "rb") as f:
        return tomllib.load(f).get("modes", {})


def load_snippets() -> dict[str, str]:
    ensure_home()
    with open(WISPR_HOME / "snippets.toml", "rb") as f:
        return tomllib.load(f).get("snippets", {})


def load_corrections() -> dict[str, str]:
    ensure_home()
    out: dict[str, str] = {}
    for line in (WISPR_HOME / "corrections.tsv").read_text().splitlines():
        if "\t" in line:
            wrong, right = line.split("\t", 1)
            out[wrong.strip()] = right.strip()
    return out


def add_correction(wrong: str, right: str) -> None:
    ensure_home()
    with open(WISPR_HOME / "corrections.tsv", "a") as f:
        f.write(f"{wrong}\t{right}\n")


def load_vocab() -> list[str]:
    ensure_home()
    p = WISPR_HOME / "vocab.txt"
    if not p.exists():
        return []
    return [w.strip() for w in p.read_text().splitlines() if w.strip()]


DEFAULT_WHISPER_MODEL = "base.en"


def whisper_model() -> str:
    """Whisper model knob (B2). Priority: WISPR_WHISPER_MODEL env ->
    ~/.wispr/whisper-model (first line) -> base.en.

    Benchmarked 2026-07-16 (synthetic say-generated audio, isolated):
    distil-small.en was 2-8x slower than small.en on this CPU and
    hallucinated on technical audio — ruled out.

    Default changed small.en -> base.en on 2026-07-30. BENCHMARKS.md §2
    measured base.en at 2.5-3.9x faster on the whisper step (1,026 ms vs
    2,555 ms on the short clip), roughly halving total latency. The old
    objection -- base.en mishearing vocab terms ("daemon.py" -> "demon.py")
    -- predates the vocab/corrections layers, which now carry 125 terms and
    catch exactly that class of error before it reaches the paste. Latency
    was the dominant complaint in real use (median 4.4 s, 83% over 3 s over
    120 real dictations) and this is the single biggest lever available on
    Intel hardware, where no GPU backend exists.

    Revert with:  echo small.en > ~/.wispr/whisper-model
    """
    env = os.environ.get("WISPR_WHISPER_MODEL", "").strip()
    if env:
        return env
    p = WISPR_HOME / "whisper-model"
    try:
        if p.exists():
            name = p.read_text().strip().splitlines()[0].strip()
            if name:
                return name
    except OSError:
        pass
    return DEFAULT_WHISPER_MODEL


def load_config() -> dict:
    """~/.wispr/config.toml — general knobs (currently: [stt]). Missing file
    or missing keys just fall through to caller defaults."""
    ensure_home()
    p = WISPR_HOME / "config.toml"
    if not p.exists():
        return {}
    try:
        with open(p, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


DEFAULT_STT_BACKEND = "auto"

# Apple Silicon GPU default. large-v3-turbo is the M-series sweet spot:
# near-large-v3 accuracy at a fraction of the decode cost, and MLX runs it
# on the GPU where CTranslate2 cannot.
MLX_DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"


def _mlx_available() -> bool:
    """True only on Apple Silicon with mlx_whisper importable. MLX ships no
    wheel for Intel macOS, so this is False on every Intel Mac."""
    import platform
    if platform.machine() != "arm64":
        return False
    try:
        import importlib.util
        return importlib.util.find_spec("mlx_whisper") is not None
    except Exception:
        return False


def resolve_backend() -> str:
    """Turn the 'auto' setting into a concrete backend for THIS machine.

    The right backend is hardware-dependent, and this project is shared
    across machines, so the default resolves per-machine instead of being
    pinned to whatever the author happens to run:

    - Apple Silicon + mlx_whisper installed -> "mlx-whisper" (GPU).
    - Everything else                       -> "faster-whisper" (CPU).

    Measured 2026-07-23 on Intel (i5-1038NG7, Iris Plus): mlx-whisper has
    no Intel wheel at all, whisper.cpp's Metal backend is compiled OUT by
    ggml on Intel macOS, and whisper.cpp's CPU/BLAS path loses to
    CTranslate2's int8 kernels (base.en encode 1.8 s vs 0.73 s total). So
    on Intel there is no GPU option to choose and faster-whisper wins.
    On Apple Silicon that conclusion inverts, which is what 'auto' exists
    to handle.
    """
    return "mlx-whisper" if _mlx_available() else "faster-whisper"


def stt_backend() -> str:
    """STT backend knob (0c). Priority: WISPR_STT_BACKEND env ->
    ~/.wispr/config.toml [stt].backend -> auto-detect (resolve_backend()).
    An explicit value always wins, so a user can pin a backend for
    debugging; "auto" re-detects per machine."""
    env = os.environ.get("WISPR_STT_BACKEND", "").strip()
    if env and env != "auto":
        return env
    if not env:
        v = load_config().get("stt", {}).get("backend", "").strip()
        if v and v != "auto":
            return v
    return resolve_backend()


DEFAULT_STT_BEAM_SIZE = 1


def stt_beam_size() -> int:
    """Beam size knob (0d): 1 trades a little accuracy for materially
    faster decode; 5 (faster-whisper's own default) is slower. Priority:
    WISPR_STT_BEAM_SIZE env -> ~/.wispr/config.toml [stt].beam_size -> 1."""
    env = os.environ.get("WISPR_STT_BEAM_SIZE", "").strip()
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
    """STT model knob (0c/B2). Priority: WISPR_STT_MODEL env ->
    ~/.wispr/config.toml [stt].model -> backend-appropriate default.

    The default is backend-dependent because the naming schemes differ:
    faster-whisper takes a short model id ("base.en"), mlx-whisper takes an
    HF repo ("mlx-community/whisper-large-v3-turbo"). Handing a base.en
    string to MLX would fail, so 'auto' picks the matching default rather
    than assuming one namespace."""
    env = os.environ.get("WISPR_STT_MODEL", "").strip()
    if env:
        return env
    v = load_config().get("stt", {}).get("model", "").strip()
    if v:
        return v
    if stt_backend() == "mlx-whisper":
        return MLX_DEFAULT_MODEL
    return whisper_model()


def _bool_knob(env_name: str, section: str, key: str, default: bool) -> bool:
    env = os.environ.get(env_name, "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    if env in ("0", "false", "no", "off"):
        return False
    v = load_config().get(section, {}).get(key)
    if isinstance(v, bool):
        return v
    return default


def _float_knob(env_name: str, section: str, key: str, default: float) -> float:
    env = os.environ.get(env_name, "").strip()
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    v = load_config().get(section, {}).get(key)
    if isinstance(v, (int, float)):
        return float(v)
    return default


def hotkey() -> str:
    """Which key starts a dictation: "right_option" (default) or "fn".

    Fn cannot go through pynput -- macOS delivers the globe key only as a
    modifier bit on flagsChanged, never as a key event -- so the daemon
    switches to a Quartz tap when this is set. Choosing "fn" also requires
    System Settings -> Keyboard -> "Press globe key to" = "Do Nothing",
    since a listen-only tap leaves the system behaviour running.

        [hotkey]
        key = "fn"
    """
    env = os.environ.get("WISPR_HOTKEY", "").strip().lower()
    v = env or load_config().get("hotkey", {}).get("key")
    if isinstance(v, str) and v.strip().lower() in ("fn", "globe"):
        return "fn"
    return "right_option"


def paste_instant() -> bool:
    """Instant-paste mode (2026-07-30). ON by default.

    Old behaviour: transcribe -> wait for Haiku cleanup (up to 2.5 s) ->
    paste. Cleanup sat on the critical path, so every long dictation paid
    the full LLM round trip before a single character appeared, and 18% of
    those waited the entire budget only to fall back to the local rules
    result anyway -- the worst of both.

    Instant mode pastes the local rules result the moment transcription
    finishes, then quietly revises it in place if Haiku comes back with
    something better (see paste_revise). Perceived latency drops by the
    whole cleanup step.

    Disable with [paste] instant = false in ~/.wispr/config.toml."""
    return _bool_knob("WISPR_PASTE_INSTANT", "paste", "instant", True)


def paste_revise() -> bool:
    """Whether instant mode is allowed to rewrite what it pasted once Haiku
    returns. OFF by default since 2026-08-04; set true to opt back in.

    The revision deletes exactly the characters it pasted and re-pastes the
    improved text. It is heavily guarded (same app still frontmost, no new
    dictation started, within the window, text short enough, replacement
    staged on the clipboard before anything is deleted) because those
    keystrokes go into a live document.

    It used to default to ON, and that is the setting that produced the
    "text appears, then vanishes" incident: the retraction ran before the
    replacement was available, and a clipboard-restore thread overwrote
    the only remaining copy. Both of those are fixed (see
    daemon._revise_in_place), but one assumption cannot be fixed from
    here: the retraction still sends one backspace per character it
    believes it pasted. If the target app silently autocorrected a word,
    the count is wrong and the extra backspaces eat text that was never
    ours. That residual risk buys a slightly tidier sentence, so the
    default is now no. Turn it on with:

        [paste]
        revise = true
    """
    return _bool_knob("WISPR_PASTE_REVISE", "paste", "revise", False)


def revise_window_s() -> float:
    """How long after the instant paste a revision may still land. Past
    this the user has probably started typing and rewriting under them is
    worse than leaving slightly rougher text."""
    return _float_knob("WISPR_REVISE_WINDOW_S", "paste", "revise_window_s", 2.5)


def revise_max_chars() -> int:
    """Skip revision above this length. The revision deletes by sending one
    backspace per character; past a few hundred that is both slow and more
    exposure than the improvement is worth."""
    v = _float_knob("WISPR_REVISE_MAX_CHARS", "paste", "revise_max_chars", 1200)
    return int(v)


KEYCHAIN_SERVICE = "wispr-anthropic"


def anthropic_key() -> str | None:
    """Env first, then macOS keychain item 'wispr-anthropic'."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return None


def set_anthropic_key(key: str) -> None:
    """Store an API key in the login keychain (never in a file, never in
    the repo). -U updates in place if the item already exists."""
    key = key.strip()
    if not key:
        raise ValueError("empty key")
    subprocess.run(
        ["security", "add-generic-password", "-U",
         "-s", KEYCHAIN_SERVICE, "-a", os.environ.get("USER", "wispr"),
         "-w", key],
        check=True, capture_output=True, text=True,
    )


def clear_anthropic_key() -> bool:
    r = subprocess.run(
        ["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE],
        capture_output=True, text=True,
    )
    return r.returncode == 0
