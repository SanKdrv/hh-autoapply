"""Поднятие резюме — общий механизм для всех бирж.

Окно поднятия у каждой площадки своё (hh разрешает раз в 4 часа), поэтому задача
планировщика запускается часто, а браузер поднимается только когда окно открылось.
Время следующего окна хранится в data/boost_next.txt (hh) или
data/boost_next-<биржа>.txt — его площадка сообщает сама («Можно сегодня в 19:21»).
"""
from __future__ import annotations

import os
import re
import time


def due_path(cfg: dict, board: str = "hh") -> str:
    name = "boost_next.txt" if board == "hh" else f"boost_next-{board}.txt"
    return os.path.join(os.path.dirname(cfg["paths"]["db"]), name)


def read_due(cfg: dict, board: str = "hh") -> float:
    try:
        with open(due_path(cfg, board), encoding="utf-8") as fh:
            return float((fh.read() or "0").strip())
    except (OSError, ValueError):
        return 0.0


def save_due(cfg: dict, board: str, ts: float) -> None:
    try:
        with open(due_path(cfg, board), "w", encoding="utf-8") as fh:
            fh.write(str(int(ts)))
    except OSError:
        pass


def next_boost_time(text: str, fallback: float) -> float:
    """Время следующего окна из строки площадки («Можно сегодня в 19:21»)."""
    from datetime import datetime, timedelta

    match = re.search(r"(\d{1,2}):(\d{2})", text or "")
    if not match:
        return fallback
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return fallback
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now + timedelta(minutes=1):
        target += timedelta(days=1)
    return target.timestamp()


def kill_stale_browsers(profile_dir: str, log=print) -> None:
    """Прибить зависшие процессы браузера этой биржи.

    Сравниваем именно имя папки профиля (browser-profile / browser-profile-habr),
    поэтому личный браузер пользователя и другие биржи не затрагиваются.
    """
    import subprocess

    from .platform_support import platform_name

    base = os.path.basename(os.path.normpath(profile_dir))
    try:
        if platform_name() == "windows":
            script = (
                "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
                "Where-Object { $_.CommandLine -match '--user-data-dir=[\"'']?([^\"'']+)' -and "
                f"[IO.Path]::GetFileName($matches[1]) -eq '{base}' }} | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", script],
                           capture_output=True, timeout=90)
        else:
            subprocess.run(["pkill", "-f", base], capture_output=True, timeout=30)
        time.sleep(3)
    except Exception as exc:  # noqa: BLE001
        log(f"  ! не удалось почистить зависшие браузеры: {exc}")


def _alert_login(cfg: dict, title: str, log=print) -> None:
    """Сообщить в Telegram, что нужен вход (не чаще раза в 6 часов)."""
    from .notify import Notifier

    path = os.path.join(os.path.dirname(cfg["paths"]["db"]), "auth_alert.txt")
    now = time.time()
    try:
        with open(path, encoding="utf-8") as fh:
            last = float((fh.read() or "0").strip())
    except (OSError, ValueError):
        last = 0.0
    if now - last < 6 * 3600:
        return
    try:
        Notifier(cfg, log=log).send_now(
            f"{title}: нужен вход",
            f"Автоматика не может войти в {title} — сессия истекла.\n"
            f"Войди вручную:\n  cd {cfg['root']}\n"
            f"  .venv\\Scripts\\python main.py login\n"
            f"(hh попросит код из SMS или почты).\n"
            f"Пока не войдёшь: поднятие резюме и отклики на этой бирже не работают.",
        )
        log(f"  → алерт о входе отправлен в Telegram")
    except Exception as exc:  # noqa: BLE001
        log(f"  ! алерт о входе не ушёл: {str(exc)[:70]}")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(str(now))
    except OSError:
        pass


def run_boost(cfg: dict, board: str = "hh", log=print, force: bool = False) -> None:
    """Поднять резюме, если окно площадки уже открылось."""
    from .boards import get_board
    from .config import board_config
    from .pipeline import acquire_lock, release_lock

    bcfg = board_config(cfg, board)
    board_obj = get_board(board)(bcfg)
    now = time.time()

    if not board_obj.boost():
        log(f"{board_obj.title}: поднятие резюме не поддерживается")
        return
    if not force:
        due = read_due(cfg, board)
        if due and now < due - 60:
            return  # окно ещё закрыто — выходим мгновенно, браузер не поднимаем

    if not acquire_lock(bcfg, board):
        log(f"{board_obj.title}: занят цикл откликов — поднятие отложено")
        return
    try:
        from .apply_selenium import build_driver

        try:
            driver = build_driver(bcfg)
        except Exception as exc:  # noqa: BLE001
            log(f"браузер не поднялся ({str(exc)[:70]}) — чищу зависшие процессы и пробую снова")
            kill_stale_browsers(bcfg["browser"]["profile_dir"], log=log)
            try:
                driver = build_driver(bcfg)
            except Exception as exc2:  # noqa: BLE001
                log(f"{board_obj.title}: браузер не поднялся ({str(exc2)[:70]}) — попробую позже")
                save_due(cfg, board, now + 15 * 60)
                return
        try:
            board_obj.ensure_login(driver)
        except Exception as exc:  # noqa: BLE001
            log(f"{board_obj.title}: нет входа — {str(exc)[:80]}")
            _alert_login(cfg, board_obj.title, log=log)
            return
        try:
            clicked, outcome, details = board_obj.raise_resume(driver, log=log)
        finally:
            try:
                driver.quit()
            except Exception:  # noqa: BLE001
                pass

        if clicked:
            log(f"поднятие резюме ({board_obj.title}): клик {clicked}; {outcome}"[:170])
            nxt = next_boost_time(outcome, now + 4 * 3600)
        else:
            log(f"поднятие резюме ({board_obj.title}): кнопка недоступна; {outcome}; {details}"[:320])
            nxt = next_boost_time(outcome, now + 15 * 60)
            if nxt > now + 4 * 3600 + 300:
                nxt = now + 15 * 60  # разобрали что-то подозрительно далёкое
        save_due(cfg, board, nxt)
    finally:
        release_lock(bcfg, board)
