"""Генерация персональных сопроводительных писем.

Письмо собирается из блоков: приветствие → зацепка под конкретную вакансию →
релевантные достижения из профиля → предложение → подпись. Варианты фраз
выбираются детерминированно по id вакансии, поэтому письма не выглядят копипастой.
"""
from __future__ import annotations

import os

from .models import Vacancy
from .util import is_remote, norm, pick, truncate


def read_letter(path: str) -> str:
    """Текст письма без служебной шапки.

    Файл письма начинается со служебных строк («# Вакансия: …», «# Компания: …»),
    отделённых пустой строкой от самого письма. Работодателю уходит только текст.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return ""
    return raw.split("\n\n", 1)[1].strip() if "\n\n" in raw else raw.strip()

STACK_LABEL = {
    "go": "Go",
    "python": "Python",
    "php": "PHP",
    "node": "Node.js/TypeScript",
    "data": "работе с данными",
    "mixed": "Go и Python",
}

# Человекочитаемые названия технологий для зацепки.
TECH_DISPLAY = {
    "go": "Go",
    "golang": "Go",
    "python": "Python",
    "django": "Django",
    "symfony": "Symfony",
    "mysql": "MySQL",
    "typescript": "TypeScript",
    "node.js": "Node.js",
    "nodejs": "Node.js",
    "javascript": "JavaScript",
    "nats": "NATS/JetStream",
    "clickhouse": "ClickHouse",
    "grpc": "gRPC",
    "protobuf": "protobuf",
    "docker": "Docker",
    "kubernetes": "Kubernetes",
    "k8s": "Kubernetes",
    "postgres": "PostgreSQL",
    "postgresql": "PostgreSQL",
    "kafka": "Kafka",
    "redis": "Redis",
    "websocket": "WebSocket",
    "websockets": "WebSocket",
    "fastapi": "FastAPI",
    "flask": "Flask",
    "asyncio": "asyncio",
    "sqlalchemy": "SQLAlchemy",
    "php": "PHP",
    "yii": "Yii2",
    "laravel": "Laravel",
    "wordpress": "WordPress",
    "woocommerce": "WooCommerce",
    "битрикс": "Битрикс",
    "vue": "Vue",
    "linux": "Linux",
    "nginx": "Nginx",
    "sql": "SQL",
    "oracle": "Oracle",
    "микросервис": "микросервисная архитектура",
    "microservice": "микросервисная архитектура",
    "highload": "highload",
    "высоконагруж": "высоконагруженные системы",
    "ci/cd": "CI/CD",
    "gitlab ci": "GitLab CI",
    "rest api": "REST API",
    "очереди": "очереди сообщений",
}

INTROS = [
    "Меня заинтересовала вакансия «{title}»{company}. {hook}",
    "Увидел вакансию «{title}»{company} — она хорошо совпадает с тем, чем я занимаюсь. {hook}",
    "Хочу откликнуться на «{title}»{company}. {hook}",
]

HOOKS_WITH_TECH = [
    "В описании вижу {techs} — это ровно то, с чем я работаю в продакшене.",
    "Заметил у вас в стеке {techs} — с этим у меня есть коммерческий опыт.",
    "Откликается стек: {techs}. С ним я работал в реальных проектах, а не только в учебных.",
]

HOOKS_PLAIN = [
    "Последние 2,5 года пишу backend на {stack} и довожу сервисы до продакшена.",
    "Мой профиль — backend на {stack}: от прототипа до эксплуатации в проде.",
    "Основной опыт — backend на {stack}, распределённые системы и работа с потоками данных.",
]

CLOSINGS_REMOTE = [
    "Готов к удалённому формату и могу подключиться к задачам быстро. Буду рад обсудить детали и то, чем могу быть полезен команде.",
    "Формат работы — удалённо, это для меня комфортно. С удовольствием расскажу подробнее на коротком созвоне.",
    "Буду рад обсудить задачи команды и ожидания от кандидата. На связи в Telegram, отвечаю быстро.",
]

CLOSINGS_OFFICE = [
    "Готов работать в Санкт-Петербурге. Буду рад обсудить детали на созвоне.",
    "Рассматриваю работу в Санкт-Петербурге и готов к обсуждению формата. На связи в Telegram.",
    "Готов к работе в офисе в Санкт-Петербурге. Расскажу подробнее о своём опыте на созвоне.",
]

CLOSINGS_NEUTRAL = [
    "Буду рад обсудить задачи команды и ожидания от кандидата. На связи в Telegram, отвечаю быстро.",
    "С удовольствием расскажу подробнее на коротком созвоне.",
]

PHP_BRIDGE = (
    "При этом мне интересно развиваться в сторону Go и бэкенд-архитектуры, "
    "так что готов брать задачи шире текущего PHP-стека."
)


# Приоритет технологий в «зацепке» зависит от стека вакансии.
TECH_PRIORITY = {
    "go": ["golang", "nats", "clickhouse", "grpc", "protobuf", "kafka", "redis", "docker", "kubernetes", "websocket", "микросервис", "highload"],
    "python": ["python", "fastapi", "flask", "asyncio", "sqlalchemy", "postgres", "redis", "kafka", "docker", "микросервис"],
    "php": ["php", "yii", "laravel", "wordpress", "woocommerce", "битрикс", "mysql", "postgres", "docker"],
    "node": ["node.js", "typescript", "vue", "docker", "postgres", "микросервис"],
    "mixed": ["go", "golang", "python", "fastapi", "nats", "clickhouse", "grpc", "protobuf", "docker", "kafka", "redis", "микросервис"],
}


def _company_phrase(vac: Vacancy) -> str:
    return f" в компании {vac.company}" if vac.company else ""


def _find_techs(vac: Vacancy, stack: str = "", limit: int = 4) -> list[str]:
    blob = norm(vac.text_blob)
    order = TECH_PRIORITY.get(stack) or list(TECH_DISPLAY.keys())
    found: list[str] = []
    for key in order:
        if norm(key) in blob:
            display = TECH_DISPLAY.get(key, key)
            if display not in found:
                found.append(display)
        if len(found) >= limit:
            break
    # если по приоритету ничего не нашли — идём по общему словарю
    if not found:
        for key, display in TECH_DISPLAY.items():
            if norm(key) in blob and display not in found:
                found.append(display)
            if len(found) >= limit:
                break
    return found


def _join_ru(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " и " + items[-1]


def _bullets_for(stack: str, profile: dict) -> list[str]:
    stack_bullets = profile.get("stack_bullets", {})
    if stack == "mixed":
        out = stack_bullets.get("go", [])[:2] + stack_bullets.get("python", [])[:1]
    elif stack == "go":
        out = stack_bullets.get("go", [])[:3]
    elif stack == "python":
        out = stack_bullets.get("python", [])[:3]
    elif stack == "php":
        out = stack_bullets.get("php", [])[:2] + stack_bullets.get("go", [])[:1]
    elif stack == "node":
        out = stack_bullets.get("node", [])[:2] + stack_bullets.get("go", [])[:1]
    else:
        out = stack_bullets.get("go", [])[:2] + stack_bullets.get("python", [])[:1]
    return [b for b in out if b]


def generate(vac: Vacancy, profile: dict, cfg: dict, stack: str | None = None) -> str:
    stack = stack or vac.stack or "mixed"
    limits = cfg["letters"]
    max_chars = int(limits.get("max_chars", 1900))

    techs = _find_techs(vac, stack=stack)
    if techs:
        hook = pick(HOOKS_WITH_TECH, vac.id).format(techs=_join_ru(techs))
    else:
        hook = pick(HOOKS_PLAIN, vac.id).format(stack=STACK_LABEL.get(stack, "backend"))

    intro = pick(INTROS, f"intro{vac.id}").format(
        title=vac.title or "вакансию",
        company=_company_phrase(vac),
        hook=hook,
    )

    paragraphs = [limits.get("greeting", "Здравствуйте!"), intro]

    for bullet in _bullets_for(stack, profile):
        paragraphs.append(bullet)

    if stack == "php":
        paragraphs.append(PHP_BRIDGE)

    if is_remote(vac.work_format):
        closing_pool = CLOSINGS_REMOTE
    elif vac.work_format:
        closing_pool = CLOSINGS_OFFICE
    else:
        closing_pool = CLOSINGS_NEUTRAL
    paragraphs.append(pick(closing_pool, f"close{vac.id}"))

    # Работодатель иногда требует указать кодовое слово — ставим его первым.
    keyword = (getattr(vac, "keyword", "") or "").strip()
    achievements_from = 2
    if keyword:
        paragraphs.insert(0, keyword)
        achievements_from = 3

    contacts = profile.get("contacts", {})
    sign_lines = [profile.get("full_name", "")]
    if limits.get("signature_position"):
        sign_lines.append(limits["signature_position"])
    contact_parts = []
    if contacts.get("telegram"):
        contact_parts.append(f"Telegram: {contacts['telegram']}")
    if contacts.get("email"):
        contact_parts.append(contacts["email"])
    if contacts.get("phone"):
        contact_parts.append(contacts["phone"])
    if contact_parts:
        sign_lines.append(" · ".join(contact_parts))
    if limits.get("include_resume_link") and limits.get("resume_url"):
        sign_lines.append(limits["resume_url"])

    signature = "\n".join(s for s in sign_lines if s)
    body = "\n\n".join(paragraphs)

    text = f"{body}\n\n—\n{signature}"

    # Если превысили лимит — жертвуем достижениями, но не структурой письма.
    while len(text) > max_chars and len(paragraphs) > achievements_from + 2:
        idx = max(
            range(achievements_from, len(paragraphs) - 1),
            key=lambda i: len(paragraphs[i]),
        )
        paragraphs.pop(idx)
        body = "\n\n".join(paragraphs)
        text = f"{body}\n\n—\n{signature}"

    if len(text) > max_chars:
        text = truncate(text, max_chars)
    return text


def write_letter(cfg: dict, vac: Vacancy, text: str) -> str:
    letters_dir = cfg["paths"]["letters_dir"]
    os.makedirs(letters_dir, exist_ok=True)
    path = os.path.join(letters_dir, f"{vac.id}.txt")
    header = f"# Вакансия: {vac.title}\n# Компания: {vac.company}\n# Ссылка: {vac.url}\n\n"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(header + text + "\n")
    return path
