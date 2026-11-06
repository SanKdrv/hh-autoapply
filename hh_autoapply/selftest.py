"""Самопроверка: последовательно тестирует все подсистемы.

Запуск:  .venv/bin/python main.py selftest
Ничего не изменяет в рабочей базе (использует временное хранилище).
"""
from __future__ import annotations

import json
import os
import tempfile
import urllib.error
import urllib.request

from .config import ensure_dirs, load_config, load_profile
from .keywords import extract_keyword
from .util import is_remote, norm, parse_salary

OK, WARN, FAIL = "ok", "warn", "fail"
ICON = {OK: "✅", WARN: "⚠️ ", FAIL: "❌"}


class Check:
    def __init__(self, name: str):
        self.name = name
        self.status = OK
        self.detail = ""

    def ok(self, detail: str = "") -> "Check":
        self.detail = detail
        return self

    def warn(self, detail: str = "") -> "Check":
        self.status = WARN
        self.detail = detail
        return self

    def fail(self, detail: str = "") -> "Check":
        self.status = FAIL
        self.detail = detail
        return self


def _check_config() -> Check:
    c = Check("Конфигурация и профиль")
    try:
        cfg = load_config()
        prof = load_profile()
        n_searches = len([s for s in cfg.get("searches", []) if s.get("enabled", True)])
        if not prof:
            return c.fail("profile.json пуст")
        return c.ok(f"поисковых профилей: {n_searches}, профиль: {prof.get('full_name', '—')}")
    except Exception as exc:  # noqa: BLE001
        return c.fail(str(exc))


def _check_env() -> Check:
    from .env import env, load_dotenv

    c = Check("Файл .env")
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if not os.path.exists(path):
        return c.warn(".env не найден — ключи LLM и Telegram не заданы")
    load_dotenv()
    llm = bool(env("DEEPSEEK_API_KEY"))
    tg = bool(env("TELEGRAM_BOT_TOKEN") and env("TELEGRAM_CHAT_ID"))
    missing = []
    if not llm:
        missing.append("DEEPSEEK_API_KEY")
    if not tg:
        missing.append("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID")
    if missing:
        return c.warn("не заполнено: " + ", ".join(missing))
    return c.ok("ключ LLM и Telegram заданы")


def _check_search(cfg) -> tuple[Check, dict | None]:
    from .sources import search_rss

    c = Check("Поиск вакансий (RSS hh)")
    try:
        profile = cfg["searches"][0]
        params = {"text": profile.get("text", ""), "area": profile.get("area"), "page": 0}
        items = search_rss(cfg, params)
        if not items:
            return c.fail("RSS вернул 0 вакансий"), None
        first = items[0].to_dict()
        return c.ok(f"получено {len(items)} вакансий, первая: {items[0].title[:45]}"), first
    except Exception as exc:  # noqa: BLE001
        return c.fail(str(exc)), None


def _check_detail(cfg, vacancy) -> Check:
    from .sources import fetch_detail

    c = Check("Карточка вакансии (описание, формат работы)")
    if not vacancy:
        return c.warn("нет вакансии для проверки")
    try:
        detail = fetch_detail(cfg, vacancy["id"])
        if not detail.get("description"):
            return c.fail("описание не получено")
        fmt = detail.get("work_formats") or "не указан"
        return c.ok(f"описание {len(detail['description'])} симв., формат: {fmt}")
    except Exception as exc:  # noqa: BLE001
        return c.fail(str(exc))


def _check_rules(cfg, vacancy) -> Check:
    from .filters import hard_filter
    from .models import Vacancy
    from .scoring import analyze, decide, detect_stack

    c = Check("Фильтры, скоринг, стек, формат, зарплата")
    if not vacancy:
        return c.warn("нет вакансии для проверки")
    try:
        vac = Vacancy.from_row(vacancy)
        vac.salary_from, vac.salary_to = parse_salary(vac.salary)
        vac.eligibility = "auto" if is_remote(vac.work_format) else "manual"
        keep, reason = hard_filter(vac, cfg)
        score, _ = analyze(vac, cfg)
        stack = detect_stack(vac, cfg)
        decision, _ = decide(vac, cfg, score)
        return c.ok(
            f"балл {score}, стек {stack or '—'}, отклик {vac.eligibility}, "
            f"решение {decision} (фильтр: {'пропуск' if keep else reason})"
        )
    except Exception as exc:  # noqa: BLE001
        return c.fail(str(exc))


def _check_keywords() -> Check:
    c = Check("Распознавание кодовых слов")
    cases = [
        ("укажите слово «ёжик»", "ёжик"),
        ("Начните письмо со слова Привет", "Привет"),
        ("Кодовое слово: БАРСИК", "БАРСИК"),
        ("Ключевые фразы для SEO", ""),
    ]
    bad = [(t, e, extract_keyword(t)) for t, e in cases if extract_keyword(t) != e]
    if bad:
        return c.fail(f"ошибок: {len(bad)} — {bad[:2]}")
    return c.ok(f"{len(cases)}/{len(cases)} примеров")


def _check_store() -> Check:
    from .models import Vacancy
    from .store import Store

    c = Check("Хранилище и защита от повторных откликов")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "t.sqlite3"))
            vac = Vacancy(id="1", title="Go-разработчик", decision="approve")
            store.upsert(vac)
            store.set_letter("1", "/tmp/x.txt")
            assert store.get("1")["title"] == "Go-разработчик"
            assert not store.has_responded("1")
            store.set_status("1", "applied", "тест")
            assert store.has_responded("1"), "повторный отклик не заблокирован"
            store.set_paused(True)
            assert store.is_paused()
            store.close()
        return c.ok("запись/чтение, блокировка повторов, пауза — работают")
    except Exception as exc:  # noqa: BLE001
        return c.fail(str(exc))


def _check_letters(cfg, profile) -> Check:
    from .letters import generate
    from .models import Vacancy

    c = Check("Генерация письма (шаблон + кодовое слово)")
    try:
        vac = Vacancy(
            id="selftest", title="Go-разработчик", company="Тест",
            description="Ищем Go-разработчика. NATS, ClickHouse, микросервисы.",
            work_format="удалённо", stack="go",
        )
        plain = generate(vac, profile, cfg, stack="go")
        vac.keyword = "ёжик"
        with_kw = generate(vac, profile, cfg, stack="go")
        assert "Здравствуйте" in plain
        assert with_kw.strip().startswith("ёжик"), "кодовое слово не первое"
        assert len(plain) > 400
        return c.ok(f"обычное {len(plain)} симв., с кодовым словом — первым")
    except Exception as exc:  # noqa: BLE001
        return c.fail(str(exc))


def _check_llm(cfg) -> Check:
    from .ai import api_available, chat, model_name

    c = Check("LLM для писем")
    if not api_available(cfg):
        return c.warn("ключ не задан — письма будут шаблонными, промпты в out/prompts/")
    try:
        text = chat(
            cfg,
            [{"role": "user", "content": "Ответь одним словом: работает"}],
            max_tokens=300,
        )
        return c.ok(f"модель «{model_name(cfg)}» отвечает: {text[:50]}")
    except Exception as exc:  # noqa: BLE001
        return c.fail(str(exc)[:220])


def _check_telegram(cfg) -> Check:
    from .notify import Telegram

    c = Check("Telegram")
    tg = Telegram.from_config(cfg)
    if not tg.enabled:
        return c.warn("не настроен — заполни TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID")
    try:
        ok = tg.send("hh-autoapply: самопроверка — связь установлена")
        return c.ok("тестовое сообщение отправлено") if ok else c.fail("sendMessage не удался")
    except Exception as exc:  # noqa: BLE001
        return c.fail(str(exc))


def _check_reports(cfg, store_rows=None) -> Check:
    from . import analytics
    from .report import build_html, build_markdown

    c = Check("Отчёты (HTML, Markdown, сводка по воронкам)")
    try:
        sample = store_rows or [
            {"id": "1", "title": "Тест", "company": "ООО", "score": 42, "stack": "go",
             "status": "lettered", "url": "https://hh.ru/vacancy/1", "area": "Москва",
             "salary": "от 200 000 ₽", "work_format": "удалённо", "eligibility": "auto",
             "funnel": "Go", "keyword": "", "letter_path": ""},
        ]
        html = build_html(sample)
        md = build_markdown(sample)
        assert "<html" in html and "Тест" in md

        class _Fake:
            def all(self, *a, **k):
                return sample

            def all_messages(self, *a, **k):
                return []

            def get_state(self, *a, **k):
                return ""

        text = analytics.render(analytics.build(_Fake()))
        assert "Воронка" in text
        return c.ok("HTML, Markdown и таблица воронок формируются")
    except Exception as exc:  # noqa: BLE001
        return c.fail(str(exc))


def _check_browser(cfg) -> Check:
    c = Check("Браузер (Selenium) и страница вакансии")
    local = dict(cfg)
    local["browser"] = dict(cfg["browser"])
    local["browser"]["headless"] = True
    try:
        from .apply_selenium import APPLY_SELECTORS, _first, build_driver, is_captcha, is_logged_in

        driver = build_driver(local)
        try:
            driver.get(f"https://{cfg['host']}/vacancy/138234549")
            btn = _first(driver, APPLY_SELECTORS)
            logged = is_logged_in(driver)
            captcha = is_captcha(driver)
            mode = "системный" if cfg["browser"].get("use_system_profile") else "отдельный"
            if captcha:
                return c.warn("сработала капча защиты hh")
            return c.ok(
                f"профиль {mode}, кнопка «Откликнуться» найдена: {bool(btn)}, "
                f"вход в аккаунт: {'да' if logged else 'нет (нужен main.py login)'}"
            )
        finally:
            driver.quit()
    except SystemExit as exc:
        return c.warn(str(exc))
    except Exception as exc:  # noqa: BLE001
        return c.fail(str(exc))


def run_all(log=print) -> list[Check]:
    cfg = load_config()
    ensure_dirs(cfg)
    profile = load_profile()

    checks: list[Check] = []
    checks.append(_check_config())
    checks.append(_check_env())

    search_check, vacancy = _check_search(cfg)
    checks.append(search_check)
    checks.append(_check_detail(cfg, vacancy))
    checks.append(_check_rules(cfg, vacancy))
    checks.append(_check_keywords())
    checks.append(_check_store())
    checks.append(_check_letters(cfg, profile))
    checks.append(_check_llm(cfg))
    checks.append(_check_telegram(cfg))
    checks.append(_check_reports(cfg))
    checks.append(_check_browser(cfg))

    log("")
    log("═══ Самопроверка hh-autoapply ═══")
    for chk in checks:
        log(f"  {ICON[chk.status]} {chk.name}: {chk.detail}")

    failed = [c for c in checks if c.status == FAIL]
    warned = [c for c in checks if c.status == WARN]
    log("")
    log(f"Итого: {len(checks) - len(failed) - len(warned)} ок, {len(warned)} предупреждений, {len(failed)} ошибок")
    return checks
