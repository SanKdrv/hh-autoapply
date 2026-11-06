"""Генерация писем через LLM (OpenAI-совместимый API).

Поддерживаются любые OpenAI-совместимые провайдеры: DeepSeek, OpenCode Go и др.
Настройки берутся из .env (LLM_BASE_URL, LLM_MODEL, DEEPSEEK_API_KEY) либо из
секции `ai` в config.json.

Особенность шлюза OpenCode: запросы идут с браузерным User-Agent (иначе
Cloudflare отдаёт 403 error 1010) и требуют заголовок x-opencode-session.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid

from .env import env
from .models import Vacancy
from .util import is_remote, work_format_label

WEB_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

SYSTEM_PROMPT = """Ты помогаешь backend-разработчику писать сопроводительные письма на hh.ru.

Правила:
- Пиши на русском, обращение на «вы», без канцелярита и без лести.
- Объём 900–1400 знаков. Не больше.
- Опирайся ТОЛЬКО на факты из профиля кандидата. Ничего не выдумывай:
  никаких несуществующих технологий, компаний, цифр и достижений.
- Обязательно зацепись за 1–3 конкретные технологии или задачи из текста вакансии.
- Не пересказывай вакансию и не копируй её формулировки.
- 2–3 достижения кандидата, релевантные именно этой вакансии.
- Не используй шаблонные «Ищу динамично развивающуюся компанию», «команда профессионалов».
- Не начинай со слов «Я пишу вам, потому что».
- Не добавляй заголовки, маркдаун и подпись — подпись добавит система.
- Если вакансия про PHP/WordPress, уместно упомянуть интерес к Go/бэкенд-архитектуре.
- Если чего-то из требований у кандидата нет — просто не упоминай это, не оправдывайся.
- Если работодатель просит указать в письме конкретные пункты (стек, реальные
  кейсы/проекты с твоей ролью, ожидания по зарплате, готовность работать по
  тайм-трекеру) — обязательно включи их в письмо.
- Если в вакансии есть список вопросов («в сопроводительном письме расскажите…»,
  «ответьте», «укажите», «приложите ссылки») — ответь на КАЖДЫЙ пункт по порядку,
  коротким абзацем на пункт. Не пропускай и не заменяй общими словами. Если по
  пункту данных нет — честно так и напиши, не выдумывай. Объём в этом случае можно
  увеличить до 1900 знаков.
- Если просят ссылки на проекты — вставь те, что есть в профиле (пет-проекты),
  а не «пришлю позже».
- Ожидания по зарплате и готовность к тайм-трекеру бери ТОЛЬКО из блока
  «Твои условия» (он ниже). Если там «не заданы» — не указывай и не выдумывай.
  Если вакансия прямо не спрашивает про зарплату/трекер — вообще их не пиши.
- Внимательно проверь описание вакансии на «ловушку»: работодатель может
  просить начать письмо с конкретного слова или фразы (например, «начни
  сопроводительное письмо со слова «Привет Компания»»). Если такое требование
  есть — начни письмо РОВНО с этой фразы/слова, даже если кодовое слово
  отдельно не задано. Читай весь текст вакансии до конца.
- Если вакансия про AI/LLM/RAG/рекомендательные системы — обязательно **явно**
  упомяни, что тема твоей дипломной работы — построение RAG-системы
  персонализированных рекомендаций (retrieval, эмбеддинги, векторный поиск).
- Никаких рассуждений в ответе: только готовый текст письма.
"""


# ── настройки ────────────────────────────────────────────────────────────

def base_url(cfg: dict) -> str:
    return (env("LLM_BASE_URL") or cfg.get("ai", {}).get("base_url") or "").strip()


def model_name(cfg: dict) -> str:
    return (env("LLM_MODEL") or cfg.get("ai", {}).get("model") or "deepseek-chat").strip()


def api_key(cfg: dict) -> str:
    ai = cfg.get("ai", {})
    env_name = ai.get("api_key_env") or "DEEPSEEK_API_KEY"
    return ai.get("api_key") or env(env_name)


def api_available(cfg: dict) -> bool:
    return bool(api_key(cfg) and base_url(cfg))


CAPTCHA_PROMPT = (
    "Это капча сайта hh.ru. На картинке почти всегда РУССКИЕ СЛОВА (обычно два), "
    "написанные с искажением, линиями и шумом; буквы могут быть наклонены или "
    "соединены. Читай по буквам слева направо, не угадывай слово по смыслу и не "
    "придумывай окончания. Ответь ТОЛЬКО распознанным текстом через пробел, "
    "без кавычек и пояснений. Если хотя бы одна буква не читается — ответь: UNKNOWN"
)


def solve_image_captcha(cfg: dict, image_png: bytes) -> str:
    """Распознаёт текст на капче через vision-модель. Пустая строка — не вышло."""
    import base64

    b64 = base64.b64encode(image_png).decode("ascii")
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": CAPTCHA_PROMPT},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
        ],
    }]
    # max_tokens большой намеренно: модель reasoning — иначе весь ответ уходит в
    # «размышления» и видимого текста не остаётся (finish_reason=length).
    max_tokens = int((cfg.get("captcha") or {}).get("ai_max_tokens", 2000))
    text = chat(cfg, messages, max_tokens=max_tokens, temperature=0.0)
    text = (text or "").strip().strip("\"'").strip()
    if not text or text.upper() == "UNKNOWN":
        return ""
    return text


def _headers(cfg: dict, key: str) -> dict:
    ai = cfg.get("ai", {})
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {key}",
        "User-Agent": ai.get("user_agent") or WEB_USER_AGENT,
    }
    # Шлюз OpenCode требует идентификатор сессии; для остальных провайдеров не нужен.
    mode = str(ai.get("session_header") or "auto").lower()
    if mode == "always" or (mode == "auto" and "opencode.ai" in base_url(cfg)):
        headers["x-opencode-session"] = ai.get("session_id") or str(uuid.uuid4())
    return headers


class EmptyReplyError(RuntimeError):
    """Модель вернула пустой текст — обычно из-за нехватки max_tokens на reasoning."""


class TransientLLMError(RuntimeError):
    """Временный сбой шлюза (401/429/5xx, обрыв) — имеет смысл повторить."""


RETRYABLE_STATUS = {401, 403, 408, 409, 425, 429, 500, 502, 503, 504}


def chat(cfg: dict, messages: list[dict], max_tokens: int | None = None,
         temperature: float | None = None) -> str:
    """Вызов chat/completions с повторами.

    - пустой ответ (reasoning съел лимит) → поднимаем max_tokens и пробуем снова;
    - временный сбой шлюза → пауза и повтор.
    """
    ai = cfg.get("ai", {})
    limit = int(max_tokens or ai.get("max_tokens", 1600))
    empty_left = int(ai.get("empty_reply_retries", 2)) + 1
    transient_left = int(ai.get("transient_retries", 4))
    pause = float(ai.get("retry_pause_sec", 2.0))
    last: Exception | None = None

    while empty_left > 0 and transient_left > 0:
        try:
            return _chat_once(cfg, messages, limit, temperature)
        except EmptyReplyError as exc:
            last = exc
            empty_left -= 1
            limit = min(limit * 2, 12000)
        except TransientLLMError as exc:
            last = exc
            transient_left -= 1
            time.sleep(pause)
    raise RuntimeError(str(last))


def _chat_once(cfg: dict, messages: list[dict], max_tokens: int,
               temperature: float | None) -> str:
    ai = cfg.get("ai", {})
    url = base_url(cfg).rstrip("/") + "/chat/completions"
    payload = {
        "model": model_name(cfg),
        "temperature": ai.get("temperature", 0.6) if temperature is None else temperature,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=_headers(cfg, api_key(cfg)),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=ai.get("timeout_sec", 120)) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(raw)
            message = detail.get("error", {}).get("message") or detail.get("message") or raw
        except Exception:  # noqa: BLE001
            message = raw
        error = f"LLM HTTP {exc.code}: {str(message)[:300]}"
        if exc.code in RETRYABLE_STATUS:
            raise TransientLLMError(error) from exc
        raise RuntimeError(error) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TransientLLMError(f"сбой сети при обращении к LLM: {exc}") from exc

    choice = (data.get("choices") or [{}])[0]
    text = ((choice.get("message") or {}).get("content") or "").strip()
    usage = data.get("usage") or {}
    finish = choice.get("finish_reason")

    if not text:
        raise EmptyReplyError(
            f"пустой ответ модели (finish_reason={finish}, "
            f"completion_tokens={usage.get('completion_tokens')}, лимит был {max_tokens})"
        )
    # Ответ мог быть обрезан лимитом — перезапрашиваем с большим лимитом.
    if finish == "length":
        raise EmptyReplyError(
            f"ответ обрезан по лимиту {max_tokens} "
            f"(получено {len(text)} символов) — увеличиваю лимит"
        )
    return text


# ── промпты ──────────────────────────────────────────────────────────────

def _profile_for_prompt(profile: dict) -> str:
    payload = {
        "имя": profile.get("full_name"),
        "возраст": profile.get("age"),
        "город": profile.get("location"),
        "позиция": profile.get("position"),
        "опыт_лет": profile.get("experience_years"),
        "о_себе": profile.get("summary"),
        "достижения_по_стекам": profile.get("stack_bullets"),
        "стек": profile.get("stack_summary"),
        "пет_проекты": profile.get("pet_projects"),
        "образование": profile.get("education"),
        "диплом": profile.get("thesis"),
        "ожидания_по_зарплате": profile.get("salary_expectations"),
        "готов_к_тайм_трекеру": profile.get("time_tracker_ok"),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


REQ_MARKERS = (
    "в сопроводительном", "как откликнуться", "ответьте", "расскажите",
    "укажите в письме", "приложите", "ответь на вопросы",
)


def letter_requirements(description: str) -> list[str]:
    """Вытаскивает список вопросов, на которые просят ответить в письме."""
    text = description or ""
    low = text.lower()
    start = -1
    for marker in ("в сопроводительном", "как откликнуться"):
        idx = low.find(marker)
        if idx >= 0:
            start = idx
            break
    if start < 0:
        for marker in ("ответьте", "расскажите", "укажите в письме", "приложите",
                       "ответь на вопросы"):
            idx = low.find(marker)
            if idx >= 0:
                start = idx
                break
    if start < 0:
        return []

    items: list[str] = []
    for line in text[start:].splitlines():
        s = line.strip()
        if not s:
            continue
        bullet = s[0] in "•-–—*"
        numbered = len(s) > 2 and s[0].isdigit() and s[1] in ")."
        if bullet or numbered:
            q = s.lstrip("•-–—*0123456789). \t").strip().rstrip(".")
            if len(q) >= 8:
                items.append(q)
        elif s.endswith("?") and len(s) > 8:
            items.append(s.rstrip("?"))

    unique: list[str] = []
    for q in items:
        if q not in unique:
            unique.append(q)
    return unique[:8]


def build_prompt(vac: dict | Vacancy, profile: dict, cfg: dict) -> str:
    get = (lambda k, d="": vac.get(k, d)) if isinstance(vac, dict) else (lambda k, d="": getattr(vac, k, d))
    work_format = get("work_format")
    format_note = work_format_label(work_format) or "не указан"
    if is_remote(work_format):
        format_note += " (кандидату подходит)"

    description = (get("description") or "").strip()
    if len(description) > 4000:
        description = description[:4000] + "…"

    keyword = (get("keyword") or "").strip()
    keyword_block = ""
    if keyword:
        keyword_block = f"""
## ВАЖНОЕ ТРЕБОВАНИЕ ВАКАНСИИ
Работодатель просит указать в сопроводительном письме кодовое слово: «{keyword}».
НАЧНИ письмо именно с этого слова — оно должно быть самым первым словом текста,
до приветствия. Без него письмо может быть отфильтровано автоматически.
"""

    tracker_ok = bool((cfg.get("letters") or {}).get("time_tracker_ok", False))
    salary_block = ""
    try:
        from .replies import _salary_block

        salary_block = _salary_block(cfg, vac if isinstance(vac, dict) else {})
    except Exception:  # noqa: BLE001 — не роняем письмо из-за блока зарплаты
        salary_block = ""
    conditions_block = f"""
## Твои условия (используй, ТОЛЬКО если вакансия об этом прямо спрашивает)
- Готовность работать по тайм-трекеру: {'готов' if tracker_ok else 'не готов'}
{salary_block}
"""

    from .truthguard import impossible_block

    forbidden_block = impossible_block(
        f"{get('title')}\n{description}", profile,
        {"company": get("company"), "title": get("title")},
    )

    reqs = letter_requirements(description)
    req_block = ""
    if reqs:
        listing = "\n".join(f"{i}. {q}" for i, q in enumerate(reqs, 1))
        req_block = f"""
## Работодатель просит ответить в сопроводительном письме
{listing}

Ответь на КАЖДЫЙ пункт выше — по порядку, отдельным коротким абзацем.
Не пропускай пункты и не заменяй их общими словами. Если по пункту данных нет —
честно напиши об этом. Объём письма в этом случае — до 1900 знаков.
"""

    return f"""Сгенерируй сопроводительное письмо под конкретную вакансию hh.ru.

## Вакансия
- Название: {get('title')}
- Компания: {get('company')}
- Регион: {get('area')}
- Формат работы: {format_note}
- Зарплата: {get('salary') or 'не указана'}
- Ссылка: {get('url')}

### Описание вакансии
{description or '(описание недоступно)'}
{forbidden_block}{req_block}{keyword_block}
## Профиль кандидата
{_profile_for_prompt(profile)}
{conditions_block}
## Подпись (добавится автоматически, в тексте её не дублируй)
{profile.get('full_name')} — {cfg.get('letters', {}).get('signature_position', '')}
"""


def prompts_dir(cfg: dict) -> str:
    path = os.path.join(cfg["paths"]["out_dir"], "prompts")
    os.makedirs(path, exist_ok=True)
    return path


def write_prompt(cfg: dict, vac: dict, profile: dict) -> str:
    path = os.path.join(prompts_dir(cfg), f"{vac['id']}.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(build_prompt(vac, profile, cfg))
    return path


def generate_via_api(cfg: dict, vac: dict, profile: dict, signature: str) -> str:
    ai = cfg.get("ai", {})
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_prompt(vac, profile, cfg)},
    ]
    max_tok = int(ai.get("max_tokens", 1600))
    if letter_requirements(vac.get("description") or ""):
        # письмо-ответ на список вопросов длиннее обычного
        max_tok = max(max_tok, int(ai.get("max_tokens_with_questions", 2600)))
    text = chat(cfg, messages, max_tokens=max_tok)

    # Модель иногда отвечает слишком коротко — просим дописать до нужного объёма.
    min_chars = int(ai.get("min_letter_chars", 700))
    if len(text) < min_chars:
        messages = messages + [
            {"role": "assistant", "content": text},
            {
                "role": "user",
                "content": (
                    "Слишком коротко. Перепиши письмо целиком: 900–1400 знаков, "
                    "2–3 конкретных достижения из профиля под эту вакансию, "
                    "без выдумок. Только текст письма."
                ),
            },
        ]
        try:
            longer = chat(cfg, messages, max_tokens=ai.get("max_tokens", 1600))
            if len(longer) > len(text):
                text = longer
        except Exception:  # noqa: BLE001 — оставляем первую версию
            pass

    return f"{text}\n\n—\n{signature}"


def signature_block(profile: dict, cfg: dict) -> str:
    contacts = profile.get("contacts", {})
    lines = [profile.get("full_name", "")]
    if cfg.get("letters", {}).get("signature_position"):
        lines.append(cfg["letters"]["signature_position"])
    parts = []
    if contacts.get("telegram"):
        parts.append(f"Telegram: {contacts['telegram']}")
    if contacts.get("email"):
        parts.append(contacts["email"])
    if contacts.get("phone"):
        parts.append(contacts["phone"])
    if parts:
        lines.append(" · ".join(parts))
    resume_url = cfg.get("letters", {}).get("resume_url")
    if resume_url and cfg.get("letters", {}).get("include_resume_link"):
        lines.append(resume_url)
    return "\n".join(x for x in lines if x)
