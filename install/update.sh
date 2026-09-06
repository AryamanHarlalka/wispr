#!/usr/bin/env bash
#
# Apply a code update to a running install: run the test suite against the
# repo's venv, restart the background service, and run the doctor.
#
# The LaunchAgent runs ~/Applications/Wispr.app (a copy of the interpreter)
# with PYTHONPATH pointing at this repo, so a code change only needs a
# restart -- no rebuild, and no re-granting of permissions. Rebuild the app
# (install/build-app.sh) only when the Python interpreter itself changes.
#
# Usage:  ./install/update.sh            # test, restart, doctor
#         ./install/update.sh --pull     # git pull first

set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$REPO/.venv/bin/python"
LABEL="com.wispr.daemon"

cd "$REPO"
[[ -x "$PY" ]] || { echo "no venv at $PY — run ./install/install.sh first"; exit 1; }

if [[ "${1:-}" == "--pull" ]]; then
  git pull --ff-only
fi

echo "== tests"
for t in tests/test_*.py; do
  PYTHONPATH="$REPO" "$PY" "$t" > /tmp/wispr-test.log 2>&1 \
    || { echo "FAILED: $t"; tail -20 /tmp/wispr-test.log; exit 1; }
  echo "  ok  $(basename "$t")"
done

echo "== restart"
launchctl kickstart -k "gui/$(id -u)/$LABEL" 2>/dev/null \
  || { echo "service not loaded — run ./install/install.sh"; exit 1; }
sleep 4

echo "== doctor"
PYTHONPATH="$REPO" "$PY" -m wispr doctor || true
echo
tail -3 "$HOME/.wispr/logs/daemon.out.log" 2>/dev/null || true
