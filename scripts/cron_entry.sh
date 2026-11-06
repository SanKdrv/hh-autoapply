#!/usr/bin/env bash
# Обёртка для cron: выполняет один полный цикл.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

# .env подхватывается внутри приложения
exec "$PROJECT_DIR/.venv/bin/python" main.py run --limit "${HH_APPLY_LIMIT:-10}" >> "$PROJECT_DIR/logs/cron.log" 2>&1
