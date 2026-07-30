#!/usr/bin/env bash
#
# Murmur installer — sets up Murmur as a background LaunchAgent.
# After this runs, Murmur starts at login and needs no terminal, ever.
#
#   curl-free usage:  cd ~/code/murmur && ./install/install.sh
#
# Safe to re-run: it tears down the old agent before installing the new one.
set -euo pipefail

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GRN=$'\033[32m'
YLW=$'\033[33m'; BLU=$'\033[34m'; RST=$'\033[0m'

say()  { printf '%s\n' "$*"; }
step() { printf '\n%s==>%s %s%s\n' "$BLU" "$RST" "$BOLD" "$*$RST"; }
ok()   { printf '  %s✓%s %s\n' "$GRN" "$RST" "$*"; }
warn() { printf '  %s!%s %s\n' "$YLW" "$RST" "$*"; }
die()  { printf '\n  %sx%s %s\n\n' "$RED" "$RST" "$*"; exit 1; }

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO/.venv"
PY="$VENV/bin/python"
LABEL="com.murmur.daemon"
PLIST_SRC="$REPO/install/$LABEL.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
MURMUR_HOME="${MURMUR_HOME:-$HOME/.murmur}"

cat <<'BANNER'

  ┌─────────────────────────────────────────────┐
  │  Murmur — local voice dictation for macOS   │
  │  Hold Right Option, speak, release.         │
  └─────────────────────────────────────────────┘
BANNER

# ── 1. Preflight ──────────────────────────────────────────────────────────
step "Checking your Mac"

[[ "$(uname -s)" == "Darwin" ]] || die "Murmur is macOS-only (this is $(uname -s))."
ok "macOS $(sw_vers -productVersion)"

ARCH="$(uname -m)"
if [[ "$ARCH" == "arm64" ]]; then
  ok "Apple Silicon ($ARCH) — the GPU transcription backend is available"
  USE_MLX=1
else
  ok "Intel ($ARCH) — using the optimised CPU backend"
  USE_MLX=0
fi

# ── 1b. Clear out any older/competing install ─────────────────────────────
# Two daemons on one microphone produce intermittent CoreAudio failures that
# present as "it randomly stops working" and are near-impossible to diagnose
# from the symptoms. This cost two weeks once; never again.
STALE=()
while IFS= read -r f; do [[ -n "$f" ]] && STALE+=("$f"); done < <(
  find "$HOME/Library/LaunchAgents" -maxdepth 1 -iname '*murmur*.plist' \
       -o -maxdepth 1 -iname '*wispr*.plist' 2>/dev/null | grep -v "/$LABEL.plist$" || true
)
if (( ${#STALE[@]} )); then
  step "Removing ${#STALE[@]} older Murmur service(s)"
  for f in "${STALE[@]}"; do
    lbl="$(basename "$f" .plist)"
    launchctl bootout "gui/$UID/$lbl" 2>/dev/null || true
    rm -f "$f"
    ok "Removed $lbl"
  done
fi

# Find a Python >= 3.10. Don't trust `command -v python3`: on stock macOS that
# resolves to /usr/bin/python3 (the 3.9 Xcode stub) because /usr/bin precedes
# /usr/local/bin in a non-login PATH, even when a newer Homebrew build exists.
pyok() { [[ -x "$1" ]] && "$1" -c 'import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)' 2>/dev/null; }

PYBIN=""
# An existing venv already encodes a known-good choice — prefer it.
if pyok "$PY"; then
  PYBIN="$PY"
else
  for cand in \
    "$(command -v python3.13 || true)" "$(command -v python3.12 || true)" \
    "$(command -v python3.11 || true)" "$(command -v python3.10 || true)" \
    /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 \
    /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3 \
    /usr/local/bin/python3.13 /usr/local/bin/python3.12 \
    /usr/local/bin/python3.11 /usr/local/bin/python3 \
    "$(command -v python3 || true)"
  do
    if pyok "$cand"; then PYBIN="$cand"; break; fi
  done
fi

if [[ -z "$PYBIN" ]]; then
  FOUND="$(/usr/bin/python3 -V 2>&1 || echo none)"
  die "Python 3.10+ required (best found: $FOUND).
     Install it with:  brew install python@3.12
     Then re-run this script."
fi
PYVER="$("$PYBIN" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])')"
ok "Python $PYVER at $PYBIN"

if ! xcode-select -p >/dev/null 2>&1; then
  warn "Xcode command line tools missing — some wheels may need to compile."
  say  "     Install them with:  xcode-select --install"
fi

# ── 2. Virtualenv + dependencies ──────────────────────────────────────────
step "Installing dependencies (this can take a few minutes on first run)"

if [[ ! -x "$PY" ]]; then
  "$PYBIN" -m venv "$VENV"
  ok "Created virtualenv at .venv"
else
  ok "Reusing existing virtualenv"
fi

"$PY" -m pip install --quiet --upgrade pip wheel
"$PY" -m pip install --quiet -r "$REPO/requirements.txt"
ok "Core dependencies installed"

# rumps powers the optional menu-bar icon; not fatal if it fails.
if "$PY" -m pip install --quiet rumps 2>/dev/null; then
  ok "Menu-bar support installed (rumps)"
else
  warn "rumps failed to install — menu bar icon unavailable, dictation unaffected"
fi

# Apple Silicon only: MLX runs Whisper on the GPU and is several times faster
# than the CPU backend. There is no Intel wheel, so this is skipped there and
# murmur.config.resolve_backend() falls back to faster-whisper automatically.
if (( USE_MLX )); then
  if "$PY" -m pip install --quiet mlx-whisper 2>/dev/null; then
    ok "GPU transcription installed (mlx-whisper) — Murmur will use it automatically"
  else
    warn "mlx-whisper unavailable — falling back to the CPU backend (still works)"
  fi
fi

mkdir -p "$MURMUR_HOME/logs"
ok "Config home ready at $MURMUR_HOME"

# ── 3. Anthropic key (optional, for Haiku cleanup) ────────────────────────
step "Haiku cleanup (optional)"

say "  Murmur transcribes ${BOLD}entirely on your Mac${RST} — your audio never leaves it,"
say "  with or without a key."
say ""
say "  Optionally, it can send the resulting ${BOLD}text${RST} (never the audio) to Claude"
say "  Haiku to strip filler, fix punctuation and apply spoken corrections"
say "  (\"Tuesday, no wait, Wednesday\" becomes \"Wednesday\"). It costs a fraction"
say "  of a cent per dictation and uses ${BOLD}your own${RST} API key, billed to you."
say ""
say "  ${DIM}Get a key: https://console.anthropic.com/settings/keys${RST}"
say "  ${DIM}Skip this and it uses local cleanup rules — good, just less polished.${RST}"
say "  ${DIM}Stored in your macOS Keychain. Never written to disk or to the repo.${RST}"
say ""

if security find-generic-password -s murmur-anthropic -w >/dev/null 2>&1; then
  ok "Anthropic key already in your keychain — skipping"
elif [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
  security add-generic-password -U -s murmur-anthropic -a "$USER" -w "$ANTHROPIC_API_KEY"
  ok "Stored ANTHROPIC_API_KEY from your environment into the keychain"
elif [[ ! -t 0 ]]; then
  warn "No key found and not running interactively — skipping (local rules only)"
else
  say "  Paste an Anthropic API key (starts with sk-ant-), or press Enter to skip:"
  printf '  key> '
  read -rs USERKEY || true
  echo
  if [[ -n "${USERKEY:-}" ]]; then
    security add-generic-password -U -s murmur-anthropic -a "$USER" -w "$USERKEY"
    ok "Key stored in macOS keychain as 'murmur-anthropic'"
  else
    warn "Skipped — Murmur will use local cleanup rules only"
    say  "     Add one later with: security add-generic-password -U -s murmur-anthropic -a \$USER -w sk-ant-..."
  fi
  unset USERKEY
fi

# ── 4. Vocabulary (optional, Obsidian users) ──────────────────────────────
step "Custom vocabulary (optional)"

if [[ -n "${MURMUR_VAULT:-}" && -d "${MURMUR_VAULT:-}" ]]; then
  "$PY" -m murmur vocab >/dev/null 2>&1 && ok "Vocab built from $MURMUR_VAULT" \
    || warn "Vocab build failed — continuing with built-in terms"
else
  say "  Murmur can learn names and jargon from an Obsidian vault so it stops"
  say "  mishearing them. ${DIM}Only proper nouns are extracted, never note content.${RST}"
  say ""
  if [[ ! -t 0 ]]; then
    warn "Not running interactively — skipping vault prompt"
    VAULTPATH=""
  else
    say "  Path to your vault (or press Enter to skip):"
    printf '  path> '
    read -r VAULTPATH || true
  fi
  if [[ -n "${VAULTPATH:-}" ]]; then
    VAULTPATH="${VAULTPATH/#\~/$HOME}"
    if [[ -d "$VAULTPATH" ]]; then
      MURMUR_VAULT="$VAULTPATH" "$PY" -m murmur vocab >/dev/null 2>&1 \
        && ok "Vocab built from $VAULTPATH" || warn "Vocab build failed — continuing"
      printf '%s\n' "$VAULTPATH" > "$MURMUR_HOME/vault-path"
    else
      warn "No folder at $VAULTPATH — skipping vocab"
    fi
  else
    ok "Skipped — Murmur uses its built-in term list"
  fi
fi

# ── 5. Warm the model so the first dictation isn't slow ───────────────────
step "Downloading the speech model"

BACKEND="$("$PY" -c 'from murmur.config import stt_backend; print(stt_backend())' 2>/dev/null || echo faster-whisper)"
MODEL="$("$PY" -c 'from murmur.config import stt_model; print(stt_model())' 2>/dev/null || echo base.en)"
say "  Backend: ${BOLD}$BACKEND${RST}  ·  model: ${BOLD}$MODEL${RST}"
say "  ${DIM}Downloading once, then cached forever. This is the slow step.${RST}"
# Backend-aware: the two engines take different model namespaces, so this
# must go through the same resolution the daemon uses rather than assuming.
if "$PY" - <<'PYEOF' 2>/dev/null
from murmur.config import stt_backend, stt_model
b, m = stt_backend(), stt_model()
if b == "mlx-whisper":
    import mlx_whisper, numpy as np
    mlx_whisper.transcribe(np.zeros(16000, dtype="float32"), path_or_hf_repo=m)
else:
    from faster_whisper import WhisperModel
    WhisperModel(m, device="cpu", compute_type="int8")
PYEOF
then
  ok "Model '$MODEL' cached and ready"
else
  warn "Model prefetch failed — it'll download on your first dictation instead"
fi

# ── 6. Install the LaunchAgent ────────────────────────────────────────────
step "Installing the background service"

mkdir -p "$HOME/Library/LaunchAgents"

# Tear down any previous copy so re-runs are clean.
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl unload  "$PLIST_DST"      2>/dev/null || true

REAL_PY="$(cd "$(dirname "$PY")" && pwd)/$(basename "$PY")"
sed -e "s|__PYTHON__|$REAL_PY|g" \
    -e "s|__WORKDIR__|$REPO|g" \
    -e "s|__HOME__|$HOME|g" \
    "$PLIST_SRC" > "$PLIST_DST"
plutil -lint "$PLIST_DST" >/dev/null || die "Generated plist is malformed — this is a bug, please report it."
ok "LaunchAgent written to $PLIST_DST"

launchctl bootstrap "gui/$UID" "$PLIST_DST" 2>/dev/null \
  || launchctl load "$PLIST_DST" 2>/dev/null \
  || die "Could not start the service. Check $MURMUR_HOME/logs/daemon.err.log"
launchctl kickstart -k "gui/$UID/$LABEL" 2>/dev/null || true
ok "Service started, and will start automatically at login"

# ── 7. Permissions — the part people get stuck on ─────────────────────────
step "Two permissions to grant (macOS requires you to do this by hand)"

# TCC attributes trust to the REAL binary, so resolve symlinks — granting the
# venv's symlinked python and then running the resolved one silently fails.
TCC_BIN="$("$PY" -c 'import os,sys;print(os.path.realpath(sys.executable))' 2>/dev/null || echo "$REAL_PY")"
printf '%s' "$TCC_BIN" | pbcopy 2>/dev/null || true

cat <<EOF

  macOS will not let ${BOLD}any${RST} app read your keyboard or microphone without
  explicit consent, and Apple deliberately makes it un-scriptable. Two grants,
  once each, and you never think about it again.

  ${BOLD}1. Accessibility${RST}  ${DIM}(to detect the hotkey and paste the text)${RST}

     a. System Settings → Privacy & Security → ${BOLD}Accessibility${RST}
     b. Click the ${BOLD}[ + ]${RST} button
     c. Press ${BOLD}Cmd-Shift-G${RST}, paste this path (already on your clipboard):

          ${BOLD}$TCC_BIN${RST}

     d. Click Open, then make sure the toggle next to it is ${BOLD}ON${RST}

  ${BOLD}2. Microphone${RST}
     The first time you hold Right Option, macOS asks. Click OK.
     ${DIM}(Or pre-grant it: System Settings → Privacy & Security → Microphone)${RST}

EOF

if [[ -t 0 ]]; then
  say "  I'll open Accessibility settings for you."
  printf '  Press Enter when you have granted it (or type s to skip)… '
  read -r GRANTED || true
  if [[ "${GRANTED:-}" != "s" ]]; then
    open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility" 2>/dev/null || true
    printf '  Press Enter once the toggle is on… '
    read -r _ || true
  fi
fi

# ── 8. Verify — assert the capability, not just the PID ───────────────────
# A daemon can hold a healthy PID while its hotkey listener is dead, so
# "is it running?" is not a sufficient check. Hand off to doctor, which
# tests Accessibility, the microphone, the model and the key for real.
step "Checking everything actually works"
launchctl kickstart -k "gui/$UID/$LABEL" 2>/dev/null || true
sleep 4

set +e
"$PY" -m murmur doctor
DOCTOR_RC=$?
set -e

cat <<EOF

  ${BOLD}Use it:${RST}   Hold ${BOLD}Right Option${RST}, speak, release. Text pastes at your cursor.
            Double-tap Right Option for hands-free; tap once to stop.

  ${BOLD}If anything misbehaves, run this first:${RST}
            ${BOLD}$PY -m murmur doctor${RST}
            It names the problem and prints the exact fix.

  ${BOLD}Add an API key later:${RST}  $PY -m murmur set-key
  ${BOLD}Restart:${RST}              $PY -m murmur restart
  ${BOLD}Remove completely:${RST}    ./install/uninstall.sh

EOF

if (( DOCTOR_RC != 0 )); then
  warn "Some checks failed above — fix those and re-run doctor."
  say  "     Most often this is just Accessibility not toggled on yet."
else
  printf '  %s%sAll set. No terminal needed from here on.%s\n\n' "$GRN" "$BOLD" "$RST"
fi
