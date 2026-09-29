#!/bin/bash
# Installs Dromac's Mac side:
#   - the server + dashboard into ~/Library/Application Support/Dromac
#   - the launcher app at ~/Applications/Dromac.app
# Safe to re-run to update: runtime state (paired phone, logs, pending
# photos, the dashboard's Chrome profile) lives alongside the code in that
# folder and is left untouched.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
support="$HOME/Library/Application Support/Dromac"
app="$HOME/Applications/Dromac.app"

mkdir -p "$support"
cp "$here/server.py" "$here/ble_helper.py" "$support/"
rsync -a --delete "$here/static/" "$support/static/"

mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
cp "$here/app/Info.plist" "$app/Contents/Info.plist"
cp "$here/app/launcher" "$app/Contents/MacOS/launcher"
chmod +x "$app/Contents/MacOS/launcher"
cp "$here/app/icon.icns" "$app/Contents/Resources/icon.icns"
# Ad-hoc signature, so macOS keeps a stable identity for the Bluetooth
# permission prompt across updates.
codesign --force --sign - "$app" >/dev/null 2>&1 || true

# The launcher only starts the server if nothing is answering on its port,
# so an already-running old server would otherwise keep serving old code.
if pids="$(lsof -tiTCP:8811 -sTCP:LISTEN 2>/dev/null)" && [ -n "$pids" ]; then
  kill $pids
  echo "Stopped the running Dromac server so the new version starts on next launch."
fi

echo "Installed. Open Dromac from ~/Applications."
