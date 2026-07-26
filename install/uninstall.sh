#!/usr/bin/env bash
#
# Removes the Murmur background service.
# Leaves ~/.murmur (your config, vocab, history) alone unless you pass --purge.
set -euo pipefail

LABEL="com.murmur.daemon"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
MURMUR_HOME="${MURMUR_HOME:-$HOME/.murmur}"

echo "==> Stopping Murmur"
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl unload  "$PLIST"          2>/dev/null || true
rm -f "$PLIST"
echo "  ✓ Service stopped and LaunchAgent removed"

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
