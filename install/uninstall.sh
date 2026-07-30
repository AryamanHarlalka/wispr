#!/usr/bin/env bash
#
# Removes the Murmur background service.
# Leaves ~/.murmur (your config, vocab, history) alone unless you pass --purge.
set -euo pipefail

LABEL="com.murmur.daemon"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
MURMUR_HOME="${MURMUR_HOME:-$HOME/.murmur}"

echo "==> Stopping Murmur"
# Remove EVERY murmur/wispr agent, not just the current label: older installs
# left differently-named plists behind, and a leftover one silently starts a
# second daemon that fights the first for the microphone.
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
              \( -iname '*murmur*.plist' -o -iname '*wispr*.plist' \) 2>/dev/null)
(( REMOVED )) || echo "  · No Murmur LaunchAgent was installed"

if [[ "${1:-}" == "--purge" ]]; then
  rm -rf "$MURMUR_HOME"
  security delete-generic-password -s murmur-anthropic >/dev/null 2>&1 || true
  echo "  ✓ Purged $MURMUR_HOME and removed the keychain entry"
else
  echo "  · Kept $MURMUR_HOME (config, vocab, history)"
  echo "    Pass --purge to delete it and the keychain entry too."
fi

echo
echo "  Note: macOS keeps the Accessibility entry for the old Python binary."
echo "  Remove it by hand in System Settings → Privacy & Security → Accessibility."
echo
