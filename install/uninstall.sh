#!/usr/bin/env bash
#
# Removes the Wispr background service.
# Leaves ~/.wispr (your config, vocab, history) alone unless you pass --purge.
set -euo pipefail

LABEL="com.wispr.daemon"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
WISPR_HOME="${WISPR_HOME:-$HOME/.wispr}"

echo "==> Stopping Wispr"
# Remove EVERY com.wispr.* agent, not just the current label: older installs
# left differently-named plists behind, and a leftover one silently starts a
# second daemon that fights the first for the microphone.
# The glob is deliberately anchored to com.wispr.* -- a bare '*wispr*' would
# also match the unrelated Wispr Flow app's com.electron.wispr-flow jobs.
REMOVED=0
while IFS= read -r f; do
  [[ -z "$f" ]] && continue
  lbl="$(basename "$f" .plist)"
  launchctl bootout "gui/$UID/$lbl" 2>/dev/null || true
  launchctl unload  "$f"            2>/dev/null || true
  rm -f "$f"
  echo "  ✓ Removed $lbl"
  REMOVED=$((REMOVED + 1))
done < <(find "$HOME/Library/LaunchAgents" -maxdepth 1 \
              -iname 'com.wispr.*.plist' 2>/dev/null)
(( REMOVED )) || echo "  · No Wispr LaunchAgent was installed"

if [[ "${1:-}" == "--purge" ]]; then
  rm -rf "$WISPR_HOME"
  security delete-generic-password -s wispr-anthropic >/dev/null 2>&1 || true
  echo "  ✓ Purged $WISPR_HOME and removed the keychain entry"
else
  echo "  · Kept $WISPR_HOME (config, vocab, history)"
  echo "    Pass --purge to delete it and the keychain entry too."
fi

echo
echo "  Note: macOS keeps the Accessibility and Microphone entries for"
echo "  ~/Applications/Wispr.app. Remove them by hand in"
echo "  System Settings → Privacy & Security → Accessibility / Microphone."
echo
