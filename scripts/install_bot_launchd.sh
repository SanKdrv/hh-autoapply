#!/usr/bin/env bash
# Устанавливает агент launchd для Telegram-бота (держит его запущенным).
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$PROJECT_DIR/.venv/bin/python"
LABEL="com.hhautoapply.bot"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGS="$PROJECT_DIR/logs"

if [ ! -x "$PYTHON" ]; then
  echo "Не найден $PYTHON — создай окружение."
  exit 1
fi

mkdir -p "$LOGS" "$HOME/Library/LaunchAgents"

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
        <string>bot</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$PROJECT_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$LOGS/bot.out.log</string>
    <key>StandardErrorPath</key>
    <string>$LOGS/bot.err.log</string>
</dict>
</plist>
PLISTEOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo "Установлено: $PLIST (бот держится запущенным)"
echo "Логи: $LOGS/bot.out.log"
echo "Проверить: launchctl list | grep hhautoapply"
