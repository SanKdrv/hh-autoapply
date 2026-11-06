"""Загрузка конфигурации и профиля."""
from __future__ import annotations

import os

from .util import load_json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULTS = {
    "host": "petrozavodsk.hh.ru",
    "api_base": "https://api.hh.ru",
    # Библиотека бирж. Базовые ключи выше — настройки hh (исторически так сложилось),
    # всё, что лежит в boards.<биржа>, переопределяет их для этой биржи.
    "boards": {
        "enabled": ["hh"],
        "hh": {"title": "hh.ru"},
        "superjob": {
            "title": "SuperJob",
            "host": "www.superjob.ru",
            "api_base": "https://api.superjob.ru/2.0",
            "profile_dir": "data/browser-profile-superjob",
            "searches": [],
        },
        "habr": {
            "title": "Хабр Карьера",
            "host": "career.habr.com",
            "profile_dir": "data/browser-profile-habr",
            "searches": [],
        },
    },
    "search_pages": 3,
    "topics_pages": 3,
    "search_via_browser": False,
    "no_contact_companies": [],
    "searches": [],
    "filters": {
        "min_score": 12,
        "require_any_stack": [],
        "exclude_title": [],
        "exclude_company": [],
        "exclude_text": [],
        "skip_if_test": False,
    },
    "scoring": {
        "must_have": [],
        "strong": [],
        "medium": [],
        "negative": [],
        "title_bonus": [],
        "stack_keywords": {},
    },
    "salary": {
        "remote_min_net": 80000,
        "relocate_min_gross": 180000,
        "ndfl": 0.13,
        "unknown": "keep",
        "mode": "floor",
    },
    "dialog": {
        "enabled": True,
        "mode": "approve",
        "max_tokens": 1200,
        "salary": {"floor_net": 80000, "hard_cap_net": 400000, "max_uplift": 0.25,
                   "default_range": [140000, 250000]},
        "auto_reply": {"enabled": True, "max_per_chat": 8},
    },
    "diagnostics": {
        "enabled": True,
        "tail_lines": 150,
        "hour": 21,
        "max_tokens": 1200,
        "send_ok": False,
    },
    "letters": {"max_chars": 1900, "time_tracker_ok": False},
    "ai": {
        "base_url": "",
        "model": "deepseek-chat",
        "api_key": "",
        "api_key_env": "DEEPSEEK_API_KEY",
        "session_header": "auto",
        "temperature": 0.6,
        "max_tokens": 1600,
        "timeout_sec": 120,
    },
    "rate_limits": {
        "daily_cap": 40,
        "max_per_run": 15,
        "min_delay_sec": 90,
        "max_delay_sec": 300,
        "cooldown_every": 8,
        "cooldown_sec": 1200,
        "work_hours": [9, 22],
    },
    "browser": {
        "name": "edge",
        "profile_dir": "data/browser-profile",
        "use_system_profile": False,
        "system_profile_dir": "",
        "profile_directory": "Default",
        "headless": False,
        "debug_port": 0,
        "reuse": False,
        "hidden": True,
        "login_timeout_sec": 300,
        "page_load_timeout_sec": 45,
        "keep_open": False,
    },
    "notify": {
        "telegram": {
            "enabled": True,
            "bot_token": "",
            "bot_token_env": "TELEGRAM_BOT_TOKEN",
            "chat_id": "",
            "chat_id_env": "TELEGRAM_CHAT_ID",
        }
    },
    "schedule": {"every_hours": 4, "start_hour": 10, "apply_limit": 12, "prevent_sleep": True,
                 "prevent_sleep_on_battery": True, "apply_only_on_ac": False},
    "captcha": {
        "try_ai": True,
        "manual_timeout_sec": 1800,
        "poll_sec": 5,
        "notify_screenshot": True,
    },
    "paths": {
        "db": "data/state.sqlite3",
        "letters_dir": "letters",
        "out_dir": "out",
        "cache_dir": "data/cache",
        "debug_dir": "out/debug",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _abs(path: str) -> str:
    """Абсолютный путь: относительные считаем от корня проекта."""
    path = path or ""
    return path if os.path.isabs(path) else os.path.join(ROOT, path)


def load_config(path: str | None = None) -> dict:
    path = path or os.path.join(ROOT, "config.json")
    cfg = _deep_merge(DEFAULTS, load_json(path, {}) or {})
    cfg["root"] = ROOT
    cfg["config_path"] = path
    for key in ("db", "letters_dir", "out_dir", "cache_dir", "debug_dir"):
        cfg["paths"][key] = _abs(cfg["paths"][key])
    cfg["browser"]["profile_dir"] = _abs(cfg["browser"]["profile_dir"])
    # у каждой биржи — свой профиль браузера (иначе два процесса не поделят один)
    for board in (cfg.get("boards") or {}).values():
        if isinstance(board, dict) and board.get("profile_dir"):
            board["profile_dir"] = _abs(board["profile_dir"])
    cfg["board"] = (cfg["boards"].get("enabled") or ["hh"])[0]
    # Профиль системного браузера зависит от ОС — задаём, если не указан явно.
    from .platform_support import default_system_profile_dir

    sys_dir = cfg["browser"].get("system_profile_dir") or default_system_profile_dir()
    cfg["browser"]["system_profile_dir"] = os.path.expanduser(sys_dir)
    return cfg


def board_names(cfg: dict) -> list[str]:
    """Включённые биржи (по умолчанию — только hh)."""
    names = (cfg.get("boards") or {}).get("enabled") or ["hh"]
    return [str(n).lower() for n in names]


def board_config(cfg: dict, board: str | None = None) -> dict:
    """Настройки одной биржи: базовые ключи + переопределения boards.<биржа>.

    База исторически описывает hh, поэтому для hh ничего не меняется, а другим
    биржам достаточно прописать host/api_base/searches/профиль в boards.<биржа>.
    """
    name = (board or cfg.get("board") or "hh").lower()
    overrides = (cfg.get("boards") or {}).get(name) or {}
    if not isinstance(overrides, dict):
        overrides = {}
    merged = _deep_merge({k: v for k, v in cfg.items() if k != "boards"}, overrides)
    merged["browser"] = dict(merged.get("browser") or {})
    merged["board"] = name
    merged["board_title"] = overrides.get("title") or name
    merged["boards"] = cfg.get("boards") or {}
    if name != "hh":
        merged["browser"]["profile_dir"] = _abs(
            overrides.get("profile_dir") or f"data/browser-profile-{name}"
        )
    return merged


def load_profile(path: str | None = None) -> dict:
    path = path or os.path.join(ROOT, "profile.json")
    return load_json(path, {}) or {}


def ensure_dirs(cfg: dict) -> None:
    for key in ("letters_dir", "out_dir", "cache_dir", "debug_dir"):
        os.makedirs(cfg["paths"][key], exist_ok=True)
    os.makedirs(os.path.dirname(cfg["paths"]["db"]), exist_ok=True)
