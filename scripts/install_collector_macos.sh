#!/usr/bin/env bash
# Install (or remove with `--remove`) the launchd agents that run the collector from this Mac:
# hourly at :09 and daily at 01:45 local time. Logs: ~/Library/Logs/crypto-monitor-*.log.
# Run it from the clone the agents should use. That clone must live OUTSIDE ~/Documents,
# ~/Desktop and ~/Downloads: macOS privacy protection (TCC) blocks launchd jobs from those
# folders ("Operation not permitted") unless bash is granted Full Disk Access. A separate
# clone such as ~/crypto-monitor also keeps the sync agent's duplicate copies away from it.
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
