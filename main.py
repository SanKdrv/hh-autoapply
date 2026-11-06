#!/usr/bin/env python3
"""Точка входа: python main.py <command>."""
import sys


def _force_utf8() -> None:
    """UTF-8 для вывода.

    На Windows консоль по умолчанию в cp866/cp1251, и печать русских строк
    в лог падает с UnicodeEncodeError. Переключаем потоки на UTF-8.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


_force_utf8()

from hh_autoapply.cli import main  # noqa: E402  (после настройки кодировки)

if __name__ == "__main__":
    main()
