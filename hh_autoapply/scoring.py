"""Скоринг релевантности и определение основного стека вакансии."""
from __future__ import annotations

from .models import Vacancy
from .util import count_occurrences, is_remote, kw_match, norm


def detect_stack(vac: Vacancy, cfg: dict) -> str:
    """Определяет, под какой стек писать письмо."""
    blob = norm(vac.text_blob)
    keywords = cfg["scoring"].get("stack_keywords", {})
    counts = {
        stack: sum(count_occurrences(blob, kw) for kw in kws)
        for stack, kws in keywords.items()
    }

    go_c = counts.get("go", 0)
    py_c = counts.get("python", 0)

    # Если в вакансии заметны и Go, и Python — пишем смешанное письмо.
    if go_c >= 2 and py_c >= 2:
        return "mixed"

    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    if not ranked or ranked[0][1] == 0:
        return ""
    return ranked[0][0]


def analyze(vac: Vacancy, cfg: dict) -> tuple[int, dict]:
    """Считает балл релевантности и собирает найденные ключевые слова."""
    sc = cfg["scoring"]
    blob = norm(vac.text_blob)
    title = norm(vac.title)

    hits = {"must": [], "strong": [], "medium": [], "negative": []}
    for group, key in (("must", "must_have"), ("strong", "strong"), ("medium", "medium"), ("negative", "negative")):
        for kw in sc.get(key, []):
            if kw_match(blob, kw):
                hits[group].append(kw)

    score = 0
    score += 10 * len(hits["must"])
    score += 6 * len(hits["strong"])
    score += 3 * len(hits["medium"])
    score -= 5 * len(hits["negative"])

    for kw in sc.get("title_bonus", []):
        if kw_match(title, kw):
            score += 8
            break

    if is_remote(vac.work_format):
        score += 3
    if vac.salary and "не указан" not in norm(vac.salary):
        score += 2
    if hits["must"]:
        score += 4

    # Вакансии с тестовым заданием: пройти их реально проще, чем собеседование,
    # поэтому поднимаем в приоритет — такие отклики уходят первыми.
    test_markers = sc.get("test_task_markers", [])
    test_hits = [kw for kw in test_markers if kw_match(blob, kw)]
    if test_hits:
        hits["test"] = test_hits
        score += int(sc.get("test_task_bonus", 30))

    return score, hits


def decide(vac: Vacancy, cfg: dict, score: int) -> tuple[str, str]:
    min_score = cfg["filters"].get("min_score", 14)
    if score >= min_score:
        return "approve", f"релевантность {score} ≥ {min_score}"
    return "skip", f"низкая релевантность {score} < {min_score}"
