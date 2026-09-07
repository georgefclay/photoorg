#!/bin/bash
# Install the inference service as a LaunchAgent (runs at login, restarts on crash).
#
#   ops/install-launchagent.sh            install and start
#   ops/install-launchagent.sh uninstall  stop and remove
#
# Requires automatic login to be on, or the service will not come back after a
# reboot: System Settings -> Users & Groups -> Automatic login.
set -euo pipefail

LABEL="com.photoorg.inference"
SERVICE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ "${1:-install}" == "uninstall" ]]; then
    launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
    rm -f "$TARGET"
    echo "Removed $TARGET"
    exit 0
fi

if [[ ! -x "$SERVICE_DIR/.venv/bin/python" ]]; then
    echo "No .venv in $SERVICE_DIR — run the setup steps in README.md first." >&2
    exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$SERVICE_DIR/logs"
sed -e "s|__SERVICE_DIR__|$SERVICE_DIR|g" -e "s|__HOME__|$HOME|g" \
    "$SERVICE_DIR/ops/$LABEL.plist" > "$TARGET"

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$TARGET"
launchctl kickstart -k "gui/$UID/$LABEL"

echo "Installed $TARGET"
echo "Status:  launchctl print gui/$UID/$LABEL | head -20"
echo "Health:  curl -s http://localhost:8500/health"
