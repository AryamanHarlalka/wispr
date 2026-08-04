#!/usr/bin/env bash
#
# Builds ~/Applications/Wispr.app — the bundle the LaunchAgent actually runs.
#
# WHY THIS EXISTS (do not delete this step from install.sh):
#
#   macOS attributes TCC consent (Microphone, Accessibility) to a *code
#   signing identity*, not to a path. Running the daemon as
#   .venv/bin/python means the identity is whatever the framework's
#   Python.app is signed as — and the stock python.org / Homebrew
#   Python.app is signed with the HARDENED RUNTIME but WITHOUT the
#   com.apple.security.device.audio-input entitlement.
#
#   The failure mode that causes is vicious: the microphone permission
#   appears granted, sounddevice opens the input device without error, and
#   every callback returns a buffer of zeros. Dictation "works" and
#   transcribes silence forever. There is no error message anywhere.
#
#   The fix is to run from our own bundle: a plain copy of the framework
#   interpreter, our own Info.plist (so macOS knows what to ask for and
#   what to name in the dialog), and an AD-HOC signature with NO
#   --options runtime. Without the hardened runtime the entitlement is not
#   required and the mic actually delivers audio.
#
#   Consequence to be aware of: the signing identity is the bundle id, so
#   changing the bundle id or the signature resets Accessibility and
#   Microphone consent. That is expected; re-grant once.
#
# Usage:  ./install/build-app.sh [/path/to/python]
#         Defaults to the repo venv's interpreter.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${1:-$REPO/.venv/bin/python}"

APP_NAME="Wispr"
BUNDLE_ID="com.wispr.dictation"
APP="$HOME/Applications/$APP_NAME.app"

[[ -x "$PY" ]] || { echo "x  No interpreter at $PY" >&2; exit 1; }

# The bundle must contain a copy of the *framework* interpreter binary
# (Resources/Python.app/Contents/MacOS/Python), not the bare `python3`
# shim: only the former is a real GUI-capable app binary.
PY_APP_BIN="$("$PY" - <<'PYEOF'
import os, sys
p = os.path.join(sys.base_prefix, "Resources", "Python.app", "Contents", "MacOS", "Python")
print(p if os.path.exists(p) else "")
PYEOF
)"

if [[ -z "$PY_APP_BIN" ]]; then
  echo "x  Could not find Python.app inside this interpreter's framework." >&2
  echo "   Wispr needs a framework build (python.org installer or" >&2
  echo "   'brew install python@3.12'). A bare/static python3 cannot be" >&2
  echo "   granted the microphone." >&2
  exit 1
fi

PYTHONHOME="$("$PY" -c 'import sys; print(sys.base_prefix)')"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cp "$PY_APP_BIN" "$APP/Contents/MacOS/$APP_NAME"
chmod +x "$APP/Contents/MacOS/$APP_NAME"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>              <string>$APP_NAME</string>
    <key>CFBundleDisplayName</key>       <string>$APP_NAME</string>
    <key>CFBundleExecutable</key>        <string>$APP_NAME</string>
    <key>CFBundleIdentifier</key>        <string>$BUNDLE_ID</string>
    <key>CFBundleVersion</key>           <string>3.0</string>
    <key>CFBundleShortVersionString</key><string>3.0</string>
    <key>CFBundlePackageType</key>       <string>APPL</string>
    <key>NSHighResolutionCapable</key>   <true/>

    <!-- Menu-bar only: no Dock icon, no app switcher entry. -->
    <key>LSUIElement</key>               <true/>

    <!-- The string macOS shows in the permission dialog. Without this key
         there is no dialog at all, which is the entire bug. -->
    <key>NSMicrophoneUsageDescription</key>
    <string>Wispr transcribes your speech into text on this Mac. Audio is processed locally and never leaves your computer.</string>
</dict>
</plist>
PLIST

plutil -lint "$APP/Contents/Info.plist" >/dev/null

# Ad-hoc signature, identifier pinned to the bundle id so TCC has a stable
# identity across rebuilds. NO `--options runtime`: see the header comment —
# the hardened runtime without an audio-input entitlement is exactly what
# makes the microphone return silence.
codesign --force --sign - --identifier "$BUNDLE_ID" "$APP"
codesign -dv "$APP" 2>&1 | grep -E '^(Identifier|CodeDirectory)' || true

echo "  ok  Built $APP ($BUNDLE_ID, ad-hoc, no hardened runtime)"
