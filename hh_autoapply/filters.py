"""Жёсткие фильтры: то, что отсекаем до скоринга."""
from __future__ import annotations

from .models import Vacancy
from .util import kw_match, norm


def hard_filter(vac: Vacancy, cfg: dict) -> tuple[bool, str]:
    """Возвращает (оставить, причина_отсева)."""
    f = cfg["filters"]
    title = norm(vac.title)
    company = norm(vac.company)
    blob = norm(vac.text_blob)

    for kw in f.get("exclude_title", []):
        if kw_match(title, kw):
            return False, f"стоп-слово в заголовке: {kw}"

    required_title = f.get("require_title_any", [])
    # AI/вайбкодинг: разрешаем заголовки, где есть и AI-слово, и девелоперское
    # («AI Developer», «Промпт-инженер», «Вайбкод-разработчик»), но не «Ai креатор»
    # и не «AI-контент-менеджер».
    ai_like = any(kw_match(title, kw) for kw in
                  ("ai", "llm", "gpt", "prompt", "нейросет", "вайбкод"))
    dev_like = any(kw_match(title, kw) for kw in
                   ("разработчик", "developer", "программист", "engineer", "инженер",
                    "backend", "бэкенд", "fullstack", "full-stack", "full stack",
                    "вайбкодер", "ml"))
    if (required_title and not any(kw_match(title, kw) for kw in required_title)
            and not (ai_like and dev_like)):
        return False, "заголовок не похож на девелоперскую роль"

    for kw in f.get("exclude_company", []):
        if kw_match(company, kw):
            return False, f"стоп-компания: {kw}"

    for kw in f.get("exclude_text", []):
        if kw_match(blob, kw):
            return False, f"стоп-слово в описании: {kw}"

    required = f.get("require_any_stack", [])
    # Вайбкодинг-вакансии часто вообще не перечисляют стек («собирать MVP
    # в Cursor») — для них требование профильного стека снимаем.
    if required and not (ai_like and dev_like) and not any(kw_match(blob, kw) for kw in required):
        return False, "нет упоминаний профильного стека"

    if f.get("skip_if_test") and "пройдите тест" in blob:
        return False, "требуется тест"

    return True, ""
