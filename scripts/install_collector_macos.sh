#!/usr/bin/env bash
# Install (or remove with `--remove`) the launchd agents that run the collector from this Mac:
# hourly at :09 and daily at 01:45 local time. Logs: ~/Library/Logs/crypto-monitor-*.log.
set -euo pipefail
cd "$(dirname "$0")/.."
DEST="$HOME/Library/LaunchAgents"
mkdir -p "$DEST"
for job in hourly daily; do
  label="com.wernerhl.crypto-monitor.$job"
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  if [ "${1:-}" = "--remove" ]; then rm -f "$DEST/$label.plist"; echo "removed $label"; continue; fi
  sed "s#/Users/whl/Documents/whl._Trading/crypto-monitor#$PWD#g" "scripts/launchd/$label.plist" > "$DEST/$label.plist"
  launchctl bootstrap "gui/$(id -u)" "$DEST/$label.plist"
  echo "installed $label"
done
launchctl list | grep crypto-monitor || true
