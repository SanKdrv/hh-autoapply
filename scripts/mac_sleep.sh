#!/usr/bin/env bash
# Управление сном MacBook от сети — нужно, чтобы планировщик отрабатывал
# запуски вовремя. От батареи настройки НЕ меняются.
#
#   ./scripts/mac_sleep.sh off     # не засыпать от сети (нужно для расписания)
#   ./scripts/mac_sleep.sh on      # вернуть как было (сон через 1 минуту)
#   ./scripts/mac_sleep.sh status  # показать текущее
set -euo pipefail

ACTION="${1:-status}"

case "$ACTION" in
  off)
    sudo pmset -c sleep 0
    echo "Готово: от сети Mac больше не засыпает сам (sleep 0)."
    echo "Откатить: ./scripts/mac_sleep.sh on"
    ;;
  on)
    sudo pmset -c sleep 1
    echo "Готово: возвращено исходное поведение — сон от сети через 1 минуту (sleep 1)."
    ;;
  status)
    echo "Текущие настройки (AC Power = питание от сети):"
    pmset -g custom | sed -n '/AC Power:/,/^$/p' | grep -E "^\s*(sleep|displaysleep)" | sed 's/^/  /'
    echo
    echo "sleep 0 = не спит сам (для расписания), sleep 1 = спит через минуту (по умолчанию)."
    ;;
  *)
    echo "использование: $0 {off|on|status}" >&2
    exit 1
    ;;
esac
