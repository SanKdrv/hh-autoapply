"""Ежедневный анализ логов: ищем аномалии и отправляем их в Telegram.

Логи не отдаём целиком — только хвосты и сводки из базы, чтобы уложиться
в разумный объём и не тратить токены впустую.
"""
from __future__ import annotations

import os
import re

from .ai import chat

SYSTEM_PROMPT = """Ты дежурный инженер системы автоматизации откликов на hh.ru.
На вход дают хвосты логов и сводки из базы за сутки.

Твоя задача — найти АНОМАЛИИ и вернуть их коротким списком:
- ошибки и исключения (особенно повторяющиеся);
- регрессии в поведении (например, отклики перестали отправляться);
- подозрительные признаки: капчи, блокировки, рост пустых ответов LLM;
- проблемы с расписанием: цикл не запускался, завис, длился аномально долго;
- расхождения в цифрах (например, много откликов — ноль ответов там, где раньше были).

ФОРМАТ ОТВЕТА:
- Если всё в порядке — ровно одна строка: OK
- Иначе — от 1 до 5 пунктов, каждый с новой строки в виде:
  [КРИТ|ВАЖНО|ИНФО] суть проблемы → что проверить
- Только факты из данных. Не выдумывай проблемы, которых нет в логах.
- Пиши по-русски, конкретно, без вступлений.
"""

MAX_CHARS = 14000


def _tail(path: str, lines: int) -> str:
    if not os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            data = fh.readlines()
    except OSError:
        return ""
    return "".join(data[-lines:])


def collect_logs(cfg: dict, lines: int = 150) -> str:
    logs_dir = os.path.join(cfg["root"], "logs")
    parts = []
    for name in ("agent.out.log", "agent.err.log", "bot.out.log", "bot.err.log", "cron.log"):
        chunk = _tail(os.path.join(logs_dir, name), lines)
        if chunk.strip():
            parts.append(f"### {name}\n{chunk.strip()}")
    return "\n\n".join(parts)


def collect_state(cfg: dict) -> str:
    from .store import Store

    store = Store(cfg["paths"]["db"])
    try:
        stats = store.stats()
        lines = [
            f"решения: {stats['by_decision']}",
            f"статусы: {stats['by_status']}",
            f"отправлено сегодня: {stats['applied_today']}",
            f"непрочитанных ответов: {len(store.unseen_incoming(200))}",
            f"черновиков в ожидании: {len(store.pending_drafts(50))}",
        ]
        errors = [r for r in store.all("status='error'")]
        if errors:
            lines.append(f"вакансий со статусом error: {len(errors)}")
            for row in errors[:5]:
                lines.append(f"  - {(row.get('title') or '')[:50]}: {(row.get('note') or '')[:80]}")
        lines.append("последние запуски:")
        for run in store.last_runs(12):
            lines.append(f"  {run['ts']} {run['kind']}: {run['info'][:120]}")
        return "\n".join(lines)
    finally:
        store.close()


def _redact(text: str) -> str:
    """Убираем из логов то, что не должно уйти наружу."""
    text = re.sub(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b", "<telegram-token>", text)
    text = re.sub(r"\boc_sk_[A-Za-z0-9_-]+", "<llm-key>", text)
    text = re.sub(r"Bearer\s+[A-Za-z0-9._-]{10,}", "Bearer <redacted>", text)
    return text


def analyze(cfg: dict) -> str:
    """Возвращает текст отчёта: OK или список аномалий."""
    lines = int(cfg.get("diagnostics", {}).get("tail_lines", 150))
    payload = f"## Логи\n{collect_logs(cfg, lines)}\n\n## Состояние базы\n{collect_state(cfg)}"
    payload = _redact(payload)[:MAX_CHARS]

    result = chat(
        cfg,
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": payload},
        ],
        max_tokens=cfg.get("diagnostics", {}).get("max_tokens", 1200),
    )
    return result.strip()


def run_digest(cfg: dict, log=print) -> dict:
    from .notify import Notifier

    notifier = Notifier(cfg, log=log)
    try:
        report = analyze(cfg)
    except Exception as exc:  # noqa: BLE001
        log(f"анализ логов не удался: {exc}")
        notifier.send_now("hh-autoapply: анализ логов не удался", str(exc)[:500])
        return {"ok": False, "error": str(exc)}

    if report.strip().upper().startswith("OK"):
        log("аномалий не найдено")
        if cfg.get("diagnostics", {}).get("send_ok", False):
            notifier.send_now("hh-autoapply: логи в порядке", report[:1000])
        return {"ok": True, "anomalies": 0}

    log(report)
    notifier.send_pre("hh-autoapply: аномалии в логах", report[:3500])
    return {"ok": True, "anomalies": len([l for l in report.splitlines() if l.strip()])}
