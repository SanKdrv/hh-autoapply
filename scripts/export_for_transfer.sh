#!/usr/bin/env bash
# Собирает архив для переноса проекта на другой компьютер (Windows/Linux).
#
# Кладёт код, настройки, историю откликов и письма, но НЕ берёт:
#   - .venv           (окружение создаётся заново, оно зависит от ОС)
#   - browser-profile (куки зашифрованы под конкретную ОС, логин нужен заново)
#   - кэш браузера, дампы отладки
#
# Запуск:  ./scripts/export_for_transfer.sh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

STAMP="$(date +%Y%m%d)"
# По умолчанию кладём рядом с проектом: к папке Desktop у macOS ограничен доступ.
OUT_DIR="${1:-$(dirname "$PROJECT_DIR")}"
ARCHIVE="$OUT_DIR/hh-autoapply-transfer-$STAMP.zip"

echo "Проект: $PROJECT_DIR"
echo "Архив:  $ARCHIVE"
echo

if [ ! -f "data/state.sqlite3" ]; then
  echo "Предупреждение: не найдена база data/state.sqlite3 — история откликов не перенесётся."
fi

zip -r -q "$ARCHIVE" . \
  -x ".venv/*" \
  -x "data/browser-profile/*" \
  -x "data/browser-profile-test/*" \
  -x "out/debug/*" \
  -x "__pycache__/*" \
  -x "*/__pycache__/*" \
  -x ".git/*" \
  -x "logs/*" \
  -x "*.pyc"

SIZE="$(du -h "$ARCHIVE" | cut -f1)"
echo "Готово: $ARCHIVE ($SIZE)"
echo
echo "Что внутри: код, config.json, profile.json, .env, letters/, data/state.sqlite3, data/cache"
echo "Что не взято: .venv, профиль браузера, .git, логи"
echo
echo "ВАЖНО: в архиве лежит .env с ключами. После переноса удали архив."
