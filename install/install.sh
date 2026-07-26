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
  ok "Apple Silicon ($ARCH) — transcription will be quick"
else
  ok "Intel ($ARCH)"
  warn "Intel Macs transcribe slower. If it drags, run: echo base.en > $MURMUR_HOME/whisper-model"
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

mkdir -p "$MURMUR_HOME/logs"
ok "Config home ready at $MURMUR_HOME"

# ── 3. Anthropic key (optional, for Haiku cleanup) ────────────────────────
step "Haiku cleanup (optional)"

say "  Murmur transcribes locally. It can optionally send the ${BOLD}text${RST} (never audio)"
say "  to Claude Haiku for a fast grammar/filler cleanup pass."
say "  ${DIM}Without a key it falls back to local rules — still good, just less polished.${RST}"
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

MODEL="$("$PY" -c 'from murmur.config import stt_model; print(stt_model())' 2>/dev/null || echo small.en)"
say "  Fetching '$MODEL' (~500MB on first run, cached afterwards)…"
if "$PY" - <<'PYEOF' 2>/dev/null
from faster_whisper import WhisperModel
from murmur.config import stt_model
WhisperModel(stt_model(), device="cpu", compute_type="int8")
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
step "Two permissions to grant by hand"

# Resolve through symlinks: TCC attributes to the real binary.
TCC_BIN="$(python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$REAL_PY" 2>/dev/null || echo "$REAL_PY")"

cat <<EOF

  macOS will not let ${BOLD}any${RST} app read your keyboard or microphone without
  explicit consent, and it cannot be scripted. Two grants, once:

  ${BOLD}1. Microphone${RST}
     The first time you hold Right Option, macOS shows a prompt. Click OK.
     If you miss it: System Settings → Privacy & Security → Microphone.

  ${BOLD}2. Accessibility${RST}  ${DIM}(needed to detect the hotkey and paste)${RST}
     System Settings → Privacy & Security → Accessibility → [ + ]
     Press ${BOLD}Cmd-Shift-G${RST} in the file picker and paste exactly:

       ${BOLD}$TCC_BIN${RST}

     Then make sure its toggle is ${BOLD}on${RST}.

  ${DIM}This path is on your clipboard now.${RST}
EOF

printf '%s' "$TCC_BIN" | pbcopy 2>/dev/null || true

# ── 8. Verify ─────────────────────────────────────────────────────────────
step "Checking it came up"
sleep 3

if launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1; then
  PID="$(launchctl print "gui/$UID/$LABEL" 2>/dev/null | awk '/^\tpid = /{print $3}')"
  if [[ -n "${PID:-}" ]]; then
    ok "Murmur is running (pid $PID)"
  else
    warn "Registered but not running yet — usually means Accessibility isn't granted."
    say  "     Grant it above, then run:  launchctl kickstart -k gui/$UID/$LABEL"
  fi
else
  warn "Service didn't register. See $MURMUR_HOME/logs/daemon.err.log"
fi

cat <<EOF

  ${GRN}${BOLD}Done.${RST}

  ${BOLD}Use it:${RST}   Hold ${BOLD}Right Option${RST}, speak, release. Text pastes at your cursor.
            Double-tap Right Option for hands-free; tap once to stop.

  ${BOLD}Logs:${RST}     tail -f $MURMUR_HOME/logs/daemon.err.log
  ${BOLD}Restart:${RST}  launchctl kickstart -k gui/$UID/$LABEL
  ${BOLD}Remove:${RST}   ./install/uninstall.sh

  No terminal needed from here on.

EOF
