#!/usr/bin/env bash
# Удаляет агент launchd.
set -euo pipefail

LABEL="com.hhautoapply.agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl unload "$PLIST" 2>/dev/null || true
rm -f "$PLIST"
echo "Агент $LABEL удалён."
