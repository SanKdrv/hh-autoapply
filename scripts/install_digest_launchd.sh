#!/usr/bin/env bash
# Устанавливает агент launchd: ежедневный анализ логов с отчётом в Telegram.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$PROJECT_DIR/.venv/bin/python"
LABEL="com.hhautoapply.digest"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGS="$PROJECT_DIR/logs"

if [ ! -x "$PYTHON" ]; then
  echo "Не найден $PYTHON — создай окружение."
  exit 1
fi

mkdir -p "$LOGS" "$HOME/Library/LaunchAgents"

HOUR="$("$PYTHON" - "$PROJECT_DIR/config.json" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1], encoding="utf-8"))
print(int(cfg.get("diagnostics", {}).get("hour", 21)))
PY
)"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$PROJECT_DIR/main.py</string>
        <string>digest</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$PROJECT_DIR</string>
    <key>StartCalendarInterval</key>
    <array>
        <dict><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>0</integer></dict>
    </array>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>$LOGS/digest.out.log</string>
    <key>StandardErrorPath</key>
    <string>$LOGS/digest.err.log</string>
</dict>
</plist>
PLISTEOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo "Установлено: $PLIST (ежедневно в $HOUR:00)"
echo "Логи: $LOGS/digest.out.log"
