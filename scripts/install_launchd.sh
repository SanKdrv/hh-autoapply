#!/usr/bin/env bash
# Устанавливает агент launchd: полный цикл hh-autoapply по расписанию.
#
# Запуски ставятся на конкретные часы внутри рабочих часов из config.json
# (rate_limits.work_hours) с шагом schedule.every_hours — чтобы ночью
# система не просыпалась впустую.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$PROJECT_DIR/.venv/bin/python"
LABEL="com.hhautoapply.agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGS="$PROJECT_DIR/logs"

if [ ! -x "$PYTHON" ]; then
  echo "Не найден $PYTHON — создай окружение: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

mkdir -p "$LOGS" "$HOME/Library/LaunchAgents"

INFO="$("$PYTHON" - "$PROJECT_DIR" "$PLIST" "$LABEL" "$PYTHON" "$LOGS" <<'PY'
import json
import os
import sys

project, plist_path, label, python, logs = sys.argv[1:6]
cfg = json.load(open(os.path.join(project, "config.json"), encoding="utf-8"))

work = cfg.get("rate_limits", {}).get("work_hours") or [9, 22]
lo, hi = int(work[0]), int(work[1])
step = int(cfg.get("schedule", {}).get("every_hours", 4)) or 4
limit = int(cfg.get("schedule", {}).get("apply_limit", 10))

hours = list(range(lo + 1, hi, step)) or [max(lo, hi - 1)]
calendar = "\n".join(
    "        <dict><key>Hour</key><integer>%d</integer>"
    "<key>Minute</key><integer>0</integer></dict>" % h
    for h in hours
)

xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>{os.path.join(project, "main.py")}</string>
        <string>run</string>
        <string>--limit</string>
        <string>{limit}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{project}</string>
    <key>StartCalendarInterval</key>
    <array>
{calendar}
    </array>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>{os.path.join(logs, "agent.out.log")}</string>
    <key>StandardErrorPath</key>
    <string>{os.path.join(logs, "agent.err.log")}</string>
</dict>
</plist>
"""
with open(plist_path, "w", encoding="utf-8") as fh:
    fh.write(xml)

print(",".join(map(str, hours)))
print(limit)
PY
)"

HOURS="$(echo "$INFO" | sed -n '1p')"
APPLY_LIMIT="$(echo "$INFO" | sed -n '2p')"

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo "Установлено: $PLIST"
echo "Запуски в часы: $HOURS (лимит откликов за цикл: $APPLY_LIMIT)"
echo "Логи: $LOGS/agent.out.log"
echo
echo "Запустить сейчас: launchctl start $LABEL"
echo "Проверить:        launchctl list | grep hhautoapply"
