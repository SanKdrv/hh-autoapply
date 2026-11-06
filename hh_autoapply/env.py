"""Загрузка переменных окружения из .env (без внешних зависимостей)."""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_dotenv(path: str | None = None, override: bool = False) -> dict:
    """Читает .env и раскладывает пары KEY=VALUE в os.environ.

    Возвращает словарь загруженных значений. Уже заданные переменные
    окружения по умолчанию не перезаписываются.
    """
    path = path or os.path.join(ROOT, ".env")
    loaded: dict[str, str] = {}
    if not os.path.exists(path):
        return loaded

    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if not key:
                continue
            loaded[key] = value
            if override or key not in os.environ:
                os.environ[key] = value
    return loaded


def env(name: str, default: str = "") -> str:
    """Читает переменную окружения, при первом обращении подтягивая .env.

    Так любой вход в систему (CLI, библиотека, планировщик) получает ключи,
    даже если забыл вызвать load_dotenv() явно.
    """
    if not _AUTO_LOADED["done"]:
        _AUTO_LOADED["done"] = True
        load_dotenv()
    return os.environ.get(name, default)


_AUTO_LOADED = {"done": False}
