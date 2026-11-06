"""Telegram-каналы с вакансиями.

Читаем публичные превью (t.me/s/<канал>) — без входа в аккаунт, без риска бана.
Каждое сообщение прогоняем через:
  1) чёрный список (экстремизм, скам, «оплатите взнос» и т.п.) — жёсткий отсев;
  2) грубый предфильтр по стеку/грейду;
  3) проверку ИИ: легальная ли это IT-вакансия под профиль (Python/Go, middle+,
     удалёнка, деньги) — и, если да, готовим черновик ответа HR.
Отправка — только вручную: черновик приходит в Telegram, ты жмёшь «отправить».
"""
from __future__ import annotations

import html
import json
import os
import re
import time

from .util import http_get, now_iso

#: только каналы с живым потоком вакансий (проверено по свежим постам)
CHANNELS = ["godevjob", "job_python", "tproger", "forpython", "devjobs"]

# Жёсткий стоп-лист: не показываем и не отвечаем ни при каких условиях.
BLACKLIST = (
    "экстремизм", "терроризм", "террорист", "оружие", "наркотик", "наркоторгов",
    "обнал", "обналич", "схема заработка", "быстрый заработок", "доход за вечер",
    "оплатите", "оплатить обучение", "взнос", "депозит", "крипто-обмен", "криптообмен",
    "инвестируй", "интим", "эскорт", "казино", "букмекер", "ставки на спорт",
    "18+", "слив", "пробив", "дропы", "дроппер",
)

#: вакансии старше этого срока не берём вообще
MAX_AGE_DAYS = 7

INCLUDE = ("python", "golang", "go ", "go/", "backend", "бэкенд", "разработчик",
           "developer", "fastapi", "django", "asyncio", "микросервис")
EXCLUDE_BAD = ("дизайнер", "ux/ui", "ui/ux", "аналитик", "qa ", "тестировщик",
               "маркетолог", "smm", "рекрутер", "стажёр", "стажер", "intern",
               "junior", "джуниор", "менеджер", "продакт", "ассистент", "продавец")


def _state_path() -> str:
    from .config import ROOT

    return os.path.join(ROOT, "data", "channels_seen.json")


def _load_seen() -> dict:
    try:
        with open(_state_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_seen(seen: dict) -> None:
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(seen, fh, ensure_ascii=False)


def _text_of(block: str) -> str:
    match = re.search(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.S)
    if not match:
        return ""
    raw = re.sub(r"<br\s*/?>", "\n", match.group(1))
    raw = re.sub(r"</?[^>]+>", "", raw)
    return html.unescape(raw).strip()


def fetch_channel(name: str, limit: int = 20) -> list[dict]:
    """Последние сообщения канала из публичного превью."""
    url = f"https://t.me/s/{name}"
    page = http_get(url, timeout=25) or ""
    from datetime import datetime, timedelta, timezone

    fresh_after = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    posts: list[dict] = []
    # Режем страницу по data-post: это якорь ровно одного сообщения.
    marks = list(re.finditer(r'data-post="([^"]+)"', page))
    for i, mark in enumerate(marks):
        start = mark.start()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(page)
        block = page[start:end]
        text = _text_of(block)
        if not text:
            continue
        # Дата поста обязательна: старые вакансии нам не нужны.
        stamp = re.search(r'<time[^>]+datetime="([^"]+)"', block)
        when = None
        if stamp:
            try:
                when = datetime.fromisoformat(stamp.group(1).replace("Z", "+00:00"))
            except ValueError:
                when = None
        if when is None or when < fresh_after:
            continue
        posts.append({
            "channel": name,
            "key": mark.group(1),
            "url": f"https://t.me/{mark.group(1)}",
            "text": text,
            "date": when.strftime("%Y-%m-%d %H:%M"),
        })
    return posts[-limit:]


def prefilter(text: str) -> tuple[bool, str]:
    """Грубый отсев до ИИ. Возвращает (проходит, причина отказа)."""
    low = " " + text.lower() + " "
    for bad in BLACKLIST:
        if bad in low:
            return False, f"чёрный список: {bad}"
    if any(bad in low for bad in EXCLUDE_BAD):
        return False, "не тот профиль (дизайн/QA/аналитика/джуниор и т.п.)"
    if not any(inc in low for inc in INCLUDE):
        return False, "нет стека в тексте"
    return True, ""


def ai_check(cfg: dict, text: str) -> dict:
    """Проверка ИИ: легальная ли вакансия и подходит ли под профиль."""
    from . import ai

    prompt = (
        "Ниже пост из Telegram-канала с вакансиями. Оцени его строго.\n"
        "Верни ТОЛЬКО JSON без пояснений:\n"
        '{"legal": true/false, "relevant": true/false, "level": "junior|middle|senior|?", '
        '"remote": true/false, "salary": "текст или пусто", "reason": "коротко"}\n'
        "legal=false — если это скам, обман, оплата «за трудоустройство», "
        "криминальная или запрещённая тема, либо не вакансия.\n"
        "relevant=true — если это backend-разработка (Python или Go), "
        "уровень junior и выше (без стажировок), удалённо или гибрид, и зарплата "
        "от 80 000 ₽ (или эквивалент).\n\n"
        f"ПОСТ:\n{text[:3500]}"
    )
    try:
        raw = (ai.chat(cfg, [{"role": "user", "content": prompt}]) or "").strip()
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
        start, end = raw.find("{"), raw.rfind("}")
        return json.loads(raw[start:end + 1]) if start >= 0 else {}
    except Exception:  # noqa: BLE001
        return None  # именно None: «модель не ответила», а не «нерелевантно»


def draft_reply(cfg: dict, text: str, profile: dict) -> str:
    """Черновик короткого сообщения HR (отправляет человек вручную)."""
    from . import ai

    prompt = (
        "Ты помогаешь кандидату ответить HR на вакансию в Telegram. Напиши короткое "
        "сообщение (3–5 предложений) от первого лица: кто он, релевантный опыт, "
        "готовность обсудить. Без воды, без выдумок, по-русски. Только текст сообщения.\n\n"
        f"ПРОФИЛЬ:\n{json.dumps(profile, ensure_ascii=False)[:2500]}\n\n"
        f"ВАКАНСИЯ:\n{text[:2500]}"
    )
    try:
        return (ai.chat(cfg, [{"role": "user", "content": prompt}]) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _send_digest(cfg: dict, cards: list[dict], log=print) -> int:
    """Одна тихая сводка (без звука) с кнопками «открыть чат с черновиком».

    Кнопка ведёт на t.me/<HR>?text=<черновик> — Telegram откроет чат с уже
    вставленным текстом, останется нажать «отправить». Авто-DM не делаем:
    бот не может писать незнакомым, а аккаунт за массовые сообщения банят.
    """
    import json
    import urllib.parse
    import urllib.request

    from .env import env

    token = env("TELEGRAM_BOT_TOKEN") or ""
    chat = env("TELEGRAM_CHAT_ID") or ""
    if not token or not chat or not cards:
        return 0
    lines = []
    buttons = []
    for card in cards:
        lines.append(card["header"] + "\n" + card["body"])
        if card.get("hr"):
            link = (f"https://t.me/{card['hr']}?text="
                    + urllib.parse.quote(card["draft"][:900], safe=""))
            buttons.append([{"text": f"✉️ {card['short']}", "url": link}])
        else:
            buttons.append([{"text": f"🔗 {card['short']}", "url": card["url"]}])
    text = "📬 Вакансии из Telegram\n\n" + "\n\n———\n\n".join(lines)
    payload = {
        "chat_id": chat,
        "text": text[:3900],
        "disable_notification": True,          # без звука
        "disable_web_page_preview": True,
        "reply_markup": json.dumps({"inline_keyboard": buttons[:8]}),
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            resp.read()
        log(f"  → сводка отправлена ({len(cards)} вакансий, без звука)")
        return 1
    except Exception as exc:  # noqa: BLE001
        log(f"  ! сводка не ушла: {str(exc)[:90]}")
        return 0


def run(cfg: dict, log=print, dry_run: bool = False, limit: int = 12) -> dict:
    from .config import load_profile
    from .notify import Notifier

    profile = load_profile()
    seen = _load_seen()
    notifier = Notifier(cfg, log=log)
    cards: list[dict] = []
    stats = {"posts": 0, "new": 0, "blacklist": 0, "prefilter": 0,
             "ai_rejected": 0, "relevant": 0, "sent": 0}

    for channel in CHANNELS:
        try:
            posts = fetch_channel(channel)
        except Exception as exc:  # noqa: BLE001
            log(f"  ! канал @{channel}: {exc}")
            continue
        stats["posts"] += len(posts)
        for post in posts:
            if post["key"] in seen:
                continue
            seen[post["key"]] = now_iso()
            stats["new"] += 1
            ok, why = prefilter(post["text"])
            if not ok:
                if why.startswith("чёрный"):
                    stats["blacklist"] += 1
                else:
                    stats["prefilter"] += 1
                continue
            verdict = ai_check(cfg, post["text"])
            if verdict is None:
                log(f"  ? @{channel}: ИИ не ответил — беру по грубому фильтру")
                verdict = {"legal": True, "relevant": True}
            if not verdict.get("legal", True):
                stats["ai_rejected"] += 1
                log(f"  ✗ @{channel}: {verdict.get('reason', 'не прошло проверку')}")
                continue
            if not verdict.get("relevant"):
                stats["ai_rejected"] += 1
                continue
            stats["relevant"] += 1
            draft = draft_reply(cfg, post["text"], profile) if not dry_run else ""
            hr_match = re.search(r"@([A-Za-z][A-Za-z0-9_]{4,})", post["text"])
            hr = hr_match.group(1) if hr_match else ""
            cards.append({
                "header": f"@{channel} · {verdict.get('level', '?')}"
                          + (f" · {verdict['salary']}" if verdict.get("salary") else ""),
                "body": post["text"][:700] + f"\n{post['url']}",
                "short": (post["text"].replace("\n", " ")[:26] or channel),
                "url": post["url"],
                "hr": hr,
                "draft": draft,
            })
            log(f"  ✓ @{channel}: {post['text'][:70]!r}")
            if stats["relevant"] >= limit:
                break
    if not dry_run and cards:
        stats["sent"] = _send_digest(cfg, cards, log=log)
    elif dry_run:
        for card in cards:
            log(f"   (dry) {card['header']} | {card['url']} | HR: {card['hr'] or '—'}")
    _save_seen(seen)
    return stats
