"""Биржи вакансий: hh, SuperJob, Хабр Карьера — единый интерфейс.

Каждая площадка — это адаптер (см. base.Board): поиск, карточка вакансии,
отклики, чтение переписки, поднятие резюме. Общие вещи (генерация писем и
черновиков, уведомления, скоринг, фильтры, БД, аналитика) живут вне адаптеров
и переиспользуются всеми биржами.

Регистрация адаптера — декоратором @register в его модуле:

    @register
    class Habr(Board):
        name = "habr"
"""
from __future__ import annotations

REGISTRY: dict[str, type] = {}


def register(cls):
    """Регистрирует адаптер биржи по cls.name."""
    REGISTRY[cls.name] = cls
    return cls


def get_board(name: str) -> type:
    if not REGISTRY:
        _load()
    key = (name or "hh").lower()
    if key not in REGISTRY:
        raise KeyError(
            f"неизвестная биржа: {name} (есть: {', '.join(sorted(REGISTRY))})"
        )
    return REGISTRY[key]


def board_names() -> list[str]:
    if not REGISTRY:
        _load()
    return sorted(REGISTRY)


def _load() -> None:
    from . import habr, hh, superjob  # noqa: F401
