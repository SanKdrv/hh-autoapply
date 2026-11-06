"""Ведение переписки с работодателем.

Система НЕ отвечает сама по себе: она готовит черновик ответа, а отправляет
его только после подтверждения в Telegram. Так исключены ошибки в переговорах.
"""
from __future__ import annotations

import json
import re

from .ai import chat
from .util import norm, work_format_label

# Подсказки для классификации входящего сообщения.
CATEGORY_HINTS = {
    "salary": ["вилк", "зарплат", "ожидан", "доход", "сколько", "salary", "compensation", "ставк"],
    "test_task": ["тестов", "test task", "задани", "assignment", "пробн"],
    "interview": ["собеседован", "интервью", "созвон", "встреч", "interview", "call", "звонок", "знакомств"],
    "availability": ["когда", "готовы начать", "срок", "занятост", "available", "start date"],
}


def classify(text: str) -> list[str]:
    """Грубая классификация вопроса — подсказывает модели, на что отвечать."""
    low = norm(text)
    return [cat for cat, hints in CATEGORY_HINTS.items() if any(h in low for h in hints)]


SYSTEM_PROMPT = """Ты ведёшь переписку с работодателем на hh.ru от имени backend-разработчика.
Твоя задача — довести диалог до тестового задания или собеседования.

ЖЁСТКИЕ ПРАВИЛА (нарушать нельзя):
- НИКОГДА не приписывай кандидату опыт с технологиями, которых нет в его профиле.
- Если вакансия требует технологий, которых в профиле нет (например, Kafka), можно
  написать, что кандидат готов их освоить — но только если это уместно и не звучит
  как оправдание. Формулируй как готовность учиться, а не как имеющийся опыт.
- Если речь о собеседовании — НЕ назначай время и не подтверждай слоты.
  Ответь, что кандидат сам предложит удобное время, и заверши письмо так,
  чтобы инициатива перешла к нему.
- Если предлагают тестовое задание — соглашайся. Срок называй трезво:
  небольшая задача — 2–3 дня, объёмная — до недели. Уточни, что важны чёткие
  критерии и что задача будет выполнена качественно.
- Если спрашивают про зарплату — назови диапазон (см. блок ниже).
- Не давай юридических и личных обещаний (визы, переезд в конкретную дату).

КОГДА ПЕРЕДАВАТЬ ЧЕЛОВЕКУ:
Если сообщение работодателя непонятно, нестандартно или требует личного решения
кандидата — не пиши ответ вообще. Вместо этого выведи ровно одну строку:
ESCALATE: <короткая причина на русском, что нужно решить кандидату>

Это нужно, когда:
- работодатель описывает непонятный или нестандартный сценарий воронки;
- предлагают заполнить анкеты, тесты на сторонних платформах, офферы с условиями;
- спрашивают личные данные (паспорт, адрес, дата рождения, гражданство);
- речь про конкретные даты, переезд, оформление, зарплату вне диапазона;
- вопрос содержит несколько условий сразу и требует обдумывания.
Если сомневаешься — эскалируй. Лучше передать человеку, чем ответить неверно.

СТИЛЬ:
- Русский, обращение на «вы», спокойно и по делу, без канцелярита и лести.
- 2–5 предложений. Это чат, а не письмо: коротко.
- Без приветствий вида «Уважаемый…» в каждом сообщении — переписка уже идёт.
- Не используй маркдаун и заголовки. Только текст сообщения.
- Не выдумывай детали вакансии, которых нет в переданных данных.
"""


def _vacancy_ceiling_net(cfg: dict, vac: dict) -> int | None:
    """Верхняя граница вилки вакансии в переводе на «на руки»."""
    from .util import net_of

    if not vac:
        return None
    ndfl = float((cfg.get("salary") or {}).get("ndfl", 0.13))
    kind = vac.get("salary_kind") or "gross"
    value = vac.get("salary_to") or vac.get("salary_from")
    return net_of(value, kind, ndfl)


def _salary_block(cfg: dict, vac: dict) -> str:
    sal = cfg.get("dialog", {}).get("salary", {})
    floor = int(sal.get("floor_net", 80000))
    cap = int(sal.get("hard_cap_net", 400000))
    uplift = float(sal.get("max_uplift", 0.25))
    vac_salary = (vac or {}).get("salary") or "не указана"
    vac_net = (vac or {}).get("salary_net")

    lines = [
        "## Зарплата",
        f"- Минимум кандидата: {floor} ₽ на руки. Ниже называть нельзя.",
        f"- Абсолютный потолок: {cap} ₽ на руки. Выше называть нельзя.",
        f"- Зарплата в вакансии: {vac_salary}",
    ]
    if vac_net:
        lines.append(f"- Нижняя граница вилки вакансии (на руки): около {vac_net} ₽")

    ceiling = _vacancy_ceiling_net(cfg, vac)
    default_range = sal.get("default_range") or []
    if ceiling:
        allowed_max = min(cap, int(round(ceiling * (1 + uplift))))
        lines.append(f"- Верхняя граница вилки вакансии (на руки): около {ceiling} ₽")
        lines.append(
            f"- Максимум, который можно назвать: {allowed_max} ₽ на руки "
            f"(не более чем на {int(round(uplift * 100))}% выше вилки вакансии)."
        )
    elif len(default_range) == 2:
        lo = max(floor, int(default_range[0]))
        hi = int(default_range[1])
        lines.append(
            f"- Вилки в вакансии нет. Используй ориентир по умолчанию: "
            f"{lo} – {hi} ₽ на руки. Выходить за него нельзя."
        )

    lines += [
        "",
        "Как считать вилку:",
        "- Оцени сложность вакансии (уровень, требования, стек, размер компании)"
        " и опыт кандидата (2,5 года, Python/Go, микросервисы, highload, NATS/ClickHouse).",
        "- Назови диапазон вида «X–Y ₽ на руки» — не одно число.",
        "- Если в вакансии указана вилка — отталкивайся от неё, не выходи за допустимый максимум.",
        "- Если вилки в вакансии нет — используй ориентир по умолчанию, приведённый выше.",
        "- Добавь короткое обоснование в одну фразу (опыт, стек), без оправданий.",
        "- Уточни, что цифры на руки.",
    ]
    return "\n".join(lines)


def _history_block(messages: list[dict]) -> str:
    if not messages:
        return "(переписка пуста)"
    lines = []
    for msg in messages[-12:]:
        who = "Кандидат" if msg.get("direction") == "out" else "Работодатель"
        text = (msg.get("text") or "").strip()
        if text:
            lines.append(f"{who}: {text}")
    return "\n".join(lines) or "(переписка пуста)"


def build_prompt(cfg: dict, profile: dict, vac: dict, messages: list[dict], lore: str = "") -> str:
    vac = vac or {}
    last_incoming = ""
    for msg in reversed(messages or []):
        if msg.get("direction") == "in" and (msg.get("text") or "").strip():
            last_incoming = msg["text"].strip()
            break

    cats = classify(last_incoming)
    hints = {
        "salary": "похоже, спрашивают про зарплату → назови диапазон",
        "test_task": "похоже, речь о тестовом задании → соглашайся и назови срок",
        "interview": "похоже, приглашают на собеседование → НЕ назначай время, передай инициативу кандидату",
        "availability": "спрашивают про сроки/доступность → ответь аккуратно, конкретные даты оставь кандидату",
    }
    hint_text = "\n".join(f"- {hints[c]}" for c in cats) or "- обычный уточняющий вопрос"

    facts = {
        "имя": profile.get("full_name"),
        "город": profile.get("location"),
        "позиция": profile.get("position"),
        "опыт_лет": profile.get("experience_years"),
        "о_себе": profile.get("summary"),
        "стек": profile.get("stack_summary"),
        "достижения": profile.get("stack_bullets"),
        "оговорки_по_стеку": profile.get("stack_notes"),
    }

    from .truthguard import impossible_block

    forbidden_block = impossible_block(
        f"{vac.get('title') or ''}\n{(vac.get('description') or '')[:1500]}\n{last_incoming}",
        profile, {"company": vac.get("company") or "", "title": vac.get("title") or ""},
    )

    return f"""Напиши ответ работодателю в переписке по вакансии.

## Вакансия
- Название: {vac.get('title') or '—'}
- Компания: {vac.get('company') or '—'}
- Формат: {work_format_label(vac.get('work_format') or '') or '—'}
- Стек/описание (кратко): {(vac.get('description') or '')[:1500]}

{_salary_block(cfg, vac)}

## Профиль кандидата
{json.dumps(facts, ensure_ascii=False, indent=2)}

## Переписка
{_history_block(messages)}

## Последнее сообщение работодателя
{last_incoming or '(нет)'}
{forbidden_block}

## Что мы уже сообщали этой компании в других чатах (НЕ противоречь этому!)
{lore or '(нет)'}

## Подсказки по типу вопроса
{hint_text}

## Ответ кандидата
Напиши ТОЛЬКО текст ответа, без пояснений и без подписи.
"""


def _clamp_salary(text: str, cfg: dict, vac: dict | None = None) -> str:
    """Страховка: числа в ответе не должны выходить за рамки политики.

    Нижняя граница — минимум кандидата. Верхняя — не выше вилки вакансии
    более чем на max_uplift, и не выше абсолютного потолка.
    """
    import re as _re

    sal = cfg.get("dialog", {}).get("salary", {})
    floor = int(sal.get("floor_net", 80000))
    cap = int(sal.get("hard_cap_net", 400000))
    uplift = float(sal.get("max_uplift", 0.25))

    ceiling = _vacancy_ceiling_net(cfg, vac or {})
    default_range = sal.get("default_range") or []
    if ceiling:
        cap = min(cap, int(round(ceiling * (1 + uplift))))
    elif len(default_range) == 2:
        # В вакансии вилки нет — держимся ориентира по умолчанию.
        cap = min(cap, int(default_range[1]))
    cap = max(cap, floor)

    def fix(match: _re.Match) -> str:
        digits = int(_re.sub(r"\D", "", match.group("num")))
        if digits <= 0:
            return match.group(0)
        if digits < floor:
            digits = floor
        elif digits > cap:
            digits = cap
        pretty = f"{digits:,}".replace(",", " ")
        return match.group(0).replace(match.group("num"), pretty)

    # суммы вида «120 000», «120000», «120 000 ₽»
    return _re.sub(r"(?P<num>\d[\d\s\u00a0]{2,6}\d)(?=\s*₽)", fix, text)


ESCALATE_RE = re.compile(r"^\s*ESCALATE\s*:\s*(.+?)\s*$", re.I | re.M)


def draft_reply(cfg: dict, profile: dict, vac: dict, messages: list[dict], lore: str = "") -> dict:
    """Готовит ответ. Возвращает {"text", "escalate", "reason"}.

    escalate=True означает, что ситуацию должен разобрать человек.
    """
    raw = chat(
        cfg,
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_prompt(cfg, profile, vac, messages, lore=lore)},
        ],
        max_tokens=cfg.get("dialog", {}).get("max_tokens", 1200),
    )
    raw = raw.strip()

    marker = ESCALATE_RE.search(raw)
    if marker:
        return {"text": "", "escalate": True, "reason": marker.group(1).strip()[:300]}

    text = _clamp_salary(raw.strip().strip('"'), cfg, vac)
    return {"text": text, "escalate": False, "reason": ""}
