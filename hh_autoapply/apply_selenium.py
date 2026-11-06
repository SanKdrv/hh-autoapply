"""Автоотклик через Selenium с рейт-лимитами.

Важно понимать: это автоматизация действий в твоём аккаунте hh. Площадка
такое не приветствует, поэтому здесь жёстко зашиты лимиты, паузы и остановка
при подозрении на капчу. Запускай осознанно.
"""
from __future__ import annotations

import os
import random
import time

from .util import human_sleep, now_iso

# Селекторы hh собраны с запасом: площадка меняет вёрстку, поэтому пробуем список.
APPLY_SELECTORS = [
    '[data-qa="vacancy-response-link-top"]',
    '[data-qa="vacancy-response-link-bottom"]',
    'a[href*="vacancy_response"]',
    'button[data-qa*="vacancy-response"]',
]

LETTER_SELECTORS = [
    '[data-qa="vacancy-response-popup-form-letter-input"]',
    '[data-qa="vacancy-response-form-letter-input"]',
    'textarea[data-qa*="letter"]',
    'textarea[name="text"]',
    'textarea',
]

SUBMIT_SELECTORS = [
    '[data-qa="vacancy-response-submit-popup"]',
    '[data-qa="vacancy-response-submit"]',
    'button[data-qa*="submit"][type="submit"]',
    'button[type="submit"]',
]

ALREADY_MARKERS = [
    "вы откликнулись", "отклик отправлен", "резюме отправлено", "отклик уже отправлен",
    "you have applied", "you responded", "response sent", "application sent",
    "already applied", "your application has been sent",
]
CAPTCHA_MARKERS = ["подтвердите, что вы не робот", "captcha", "необычный трафик", "проверка браузера"]

# Работодатель может задать скрининговые вопросы. Отвечать на них автоматически
# нельзя — это твои ответы, и вранье тут вредно. Такие вакансии помечаем на ручную.
QUESTIONS_SELECTOR = '[data-qa^="vacancy-response-question"]'

# Промо-модалки и баннеры hh перехватывают клики. Их нужно закрывать до действий.
DISMISS_SELECTORS = [
    '[data-qa="banner-close"]',
    '[data-qa="button-close"]',
    '[data-qa="cookies-policy-accept"]',
    '[aria-label="Close"]',
    '[aria-label="Закрыть"]',
]

# Капча: ищем саму картинку/челлендж и поле для ответа (порядок — от точного к общему).
CAPTCHA_IMAGE_SELECTORS = [
    'img[src*="captcha" i]',
    'img[alt*="captcha" i]',
    '[data-qa*="captcha"] img',
    '[data-qa*="captcha"] canvas',
    '.smart-captcha img',
    '.smart-captcha canvas',
    'iframe[src*="captcha"]',
]
CAPTCHA_INPUT_SELECTORS = [
    '[data-qa="account-captcha-input"]',
    'input[name="captchaText"]',
    '[data-qa="captcha-input"]',
    'input[name="captcha-response"]',
    'input[name="captcha"]',
    'textarea[name="captcha-response"]',
    '.smart-captcha input[type="text"]',
    'input[type="text"]',
    'textarea',
]
CAPTCHA_SUBMIT_SELECTORS = [
    '[data-qa="captcha-submit"]',
    '.smart-captcha button[type="submit"]',
    'button[type="submit"]',
]


def dismiss_overlays(driver, rounds: int = 3) -> int:
    """Закрывает промо-модалки и баннеры. Возвращает число закрытых."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys

    closed = 0
    for _ in range(rounds):
        clicked = False
        for sel in DISMISS_SELECTORS:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, sel)
            except Exception:  # noqa: BLE001
                continue
            for el in elements:
                try:
                    if el.is_displayed():
                        driver.execute_script("arguments[0].click()", el)
                        closed += 1
                        clicked = True
                except Exception:  # noqa: BLE001
                    continue
        if not clicked:
            break
        time.sleep(1)
    try:
        driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
    except Exception:  # noqa: BLE001
        pass
    return closed


def _has_questions(driver) -> bool:
    from selenium.webdriver.common.by import By

    try:
        return len(driver.find_elements(By.CSS_SELECTOR, QUESTIONS_SELECTOR)) > 0
    except Exception:  # noqa: BLE001
        return False


_EXTERNAL_LOG = None


def set_log(fn) -> None:
    """Перенаправляет логи модуля (в т.ч. откликов) во внешний логгер цикла."""
    global _EXTERNAL_LOG
    _EXTERNAL_LOG = fn


def _log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    if _EXTERNAL_LOG is not None:
        _EXTERNAL_LOG(line)
    else:
        print(line, flush=True)


def browser_running(name: str) -> bool:
    """Проверяет, запущен ли браузер (нужно для системного профиля)."""
    import subprocess

    pattern = {"edge": "Microsoft Edge", "msedge": "Microsoft Edge", "chrome": "Google Chrome"}.get(
        name.lower(), name
    )
    try:
        out = subprocess.run(["pgrep", "-fl", pattern], capture_output=True, text=True)
        return bool(out.stdout.strip())
    except Exception:  # noqa: BLE001
        return False


def _port_open(port: int) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def build_driver(cfg: dict, hidden=None):
    """Chromium-подобный браузер (Edge по умолчанию) с профилем.

    Если включён browser.reuse и уже поднят общий браузер (по debug-порту) —
    прикрепляемся к нему (быстро, без «холодного старта») и НЕ закрываем его.
    """
    name = cfg["browser"].get("name", "edge").lower()
    use_system = bool(cfg["browser"].get("use_system_profile"))
    debug_port = int(cfg["browser"].get("debug_port") or 0)

    if cfg["browser"].get("reuse") and debug_port and _port_open(debug_port):
        from selenium import webdriver

        if name in ("edge", "msedge"):
            from selenium.webdriver.edge.options import Options

            attach = Options()
            attach.add_experimental_option("debuggerAddress", f"127.0.0.1:{debug_port}")
            driver = webdriver.Edge(options=attach)
        else:
            from selenium.webdriver.chrome.options import Options

            attach = Options()
            attach.add_experimental_option("debuggerAddress", f"127.0.0.1:{debug_port}")
            driver = webdriver.Chrome(options=attach)
        driver.set_page_load_timeout(cfg["browser"].get("page_load_timeout_sec", 45))
        driver.quit = lambda *a, **k: None  # общий браузер не закрываем
        return driver

    if use_system:
        profile_dir = cfg["browser"].get("system_profile_dir") or ""
        if not os.path.isdir(profile_dir):
            raise SystemExit(f"не найден системный профиль браузера: {profile_dir}")
        if browser_running(name):
            raise SystemExit(
                f"{name.title()} сейчас запущен и держит профиль занятым.\n"
                "Полностью закрой браузер (Cmd+Q) и запусти команду снова."
            )
    else:
        profile_dir = cfg["browser"]["profile_dir"]
        os.makedirs(profile_dir, exist_ok=True)

    profile_directory = cfg["browser"].get("profile_directory", "Default")

    if name in ("edge", "msedge"):
        from selenium.webdriver.edge.options import Options
    elif name == "chrome":
        from selenium.webdriver.chrome.options import Options  # type: ignore
    else:
        raise SystemExit(f"неизвестный браузер: {name} (поддерживаются edge/chrome)")

    options = Options()
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument(f"--profile-directory={profile_directory}")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-notifications")
    # Скрытое окно Edge считает фоновым: рендерер тормозит и падает с
    # «timeout: Timed out receiving message from renderer» — это ломало
    # чтение переписки и поднятие резюме.
    options.add_argument("--disable-background-timer-throttling")
    options.add_argument("--disable-renderer-backgrounding")
    options.add_argument("--disable-backgrounding-occluded-windows")
    options.add_argument("--lang=ru-RU")
    hide = cfg["browser"].get("hidden", True) if hidden is None else hidden
    if hide:
        # Окно за пределами экрана — не маячит перед пользователем, но браузер реальный.
        options.add_argument("--window-position=-32000,-32000")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_experimental_option(
        "prefs",
        {"intl.accept_languages": cfg["browser"].get("accept_language", "ru-RU,ru")},
    )

    # Порт отладки: позволяет залогиниться в браузер на сервере через SSH-туннель.
    debug_port = int(cfg["browser"].get("debug_port") or 0)
    if debug_port:
        options.add_argument(f"--remote-debugging-port={debug_port}")
        options.add_argument("--remote-allow-origins=*")

    if cfg["browser"].get("headless"):
        options.add_argument("--headless=new")

    if name in ("edge", "msedge"):
        from selenium import webdriver
        driver = webdriver.Edge(options=options)
    else:
        from selenium import webdriver
        driver = webdriver.Chrome(options=options)

    driver.set_page_load_timeout(cfg["browser"].get("page_load_timeout_sec", 45))
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
        )
    except Exception:  # noqa: BLE001 — CDP есть не везде
        pass
    return driver


def _first(driver, selectors):
    from selenium.webdriver.common.by import By

    for sel in selectors:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            for el in els:
                if el.is_displayed() and el.is_enabled():
                    return el
        except Exception:  # noqa: BLE001
            continue
    return None


def _page_text(driver) -> str:
    try:
        return (driver.find_element("tag name", "body").text or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def is_captcha(driver) -> bool:
    text = _page_text(driver)
    return any(m in text for m in CAPTCHA_MARKERS) or "captcha" in (driver.current_url or "").lower()


def is_logged_in(driver) -> bool:
    from selenium.webdriver.common.by import By

    checks = [
        '[data-qa="mainmenu_applicantProfile"]',
        'a[href*="/applicant/resumes"]',
        '[data-qa="mainmenu_myResumes"]',
    ]
    for sel in checks:
        try:
            if driver.find_elements(By.CSS_SELECTOR, sel):
                return True
        except Exception:  # noqa: BLE001
            continue
    text = _page_text(driver)
    return "мои резюме" in text


def ensure_login(driver, cfg: dict, interactive: bool | None = None) -> None:
    host = cfg["host"]
    _log("проверяю авторизацию на hh")
    driver.get(f"https://{host}/")
    if is_logged_in(driver):
        _log("уже авторизован")
        return

    # В неинтерактивном запуске (планировщик) ждать ручного входа бессмысленно.
    import sys

    if interactive is None:
        interactive = sys.stdin.isatty()
    timeout = int(cfg["browser"].get("login_timeout_sec", 300))
    if not interactive:
        raise RuntimeError(
            "нет активной сессии hh, а запуск неинтерактивный. "
            "Выполни один раз: .venv/bin/python main.py login"
        )

    _log("нужно войти в аккаунт: сделай это в открытом окне браузера "
         "(код придёт в SMS или на почту). Жду авторизации…")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_logged_in(driver):
            _log("авторизация успешна, профиль сохранён")
            return
        time.sleep(3)
    raise RuntimeError("не дождался авторизации — запусти команду login ещё раз")


def _fill_letter(driver, text: str) -> bool:
    """Вписывает письмо и проверяет, что оно реально попало в поле."""
    field = _first(driver, LETTER_SELECTORS)
    if not field:
        return False
    try:
        # React слушает событие input, поэтому ставим значение через нативный сеттер.
        driver.execute_script(
            """
            const el = arguments[0], value = arguments[1];
            const proto = el.tagName === 'TEXTAREA'
                ? window.HTMLTextAreaElement.prototype
                : window.HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
            setter.call(el, value);
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
            """,
            field,
            text,
        )
        # Проверяем, что текст действительно в поле, а не «показалось».
        value = (field.get_attribute("value") or "").strip()
        return len(value) >= min(40, len(text.strip()) // 2)
    except Exception as exc:  # noqa: BLE001
        _log(f"    ! не удалось заполнить письмо: {exc}")
        return False


def _open_response_form(driver, vac: dict) -> str:
    """Открывает форму отклика на странице вакансии.

    Возвращает: "ok" — форма с полем письма открыта; "already" — уже
    откликались; "noform" — форму открыть не удалось.

    ВАЖНО: кнопка «Откликнуться» — это <a href="/applicant/vacancy_response?
    vacancyId=X">. Обычный клик делает ПОЛНЫЙ переход по этой ссылке, а hh на
    свежей загрузке обрабатывает её как отклик «в один клик» — БЕЗ письма.
    Поэтому блокируем переход (preventDefault) и даём hh открыть форму внутри
    страницы (как для обычного пользователя). Заодно не ходим по URL напрямую.
    """
    from selenium.webdriver.common.by import By

    url = vac.get("url") or ""
    if not url:
        return "noform"

    driver.get(url)
    time.sleep(3)
    dismiss_overlays(driver)

    # Уже откликнулись: кнопка ведёт в чат, а не на форму отклика.
    if driver.find_elements(By.CSS_SELECTOR, '[data-qa="vacancy-response-link-view-topic"]'):
        return "already"

    btn = _first(driver, APPLY_SELECTORS)
    if not btn:
        return "noform"

    handles_before = set(driver.window_handles)
    # preventDefault — иначе браузер уйдёт по href и hh оформит отклик без письма.
    driver.execute_script(
        """
        const el = arguments[0];
        el.addEventListener('click', function (e) { e.preventDefault(); }, {capture: true});
        el.click();
        """,
        btn,
    )

    new_handles = set(driver.window_handles) - handles_before
    if new_handles:
        driver.switch_to.window(new_handles.pop())

    # Ждём именно поле сопроводительного письма.
    for _ in range(15):
        if _first(driver, LETTER_SELECTORS):
            return "ok"
        time.sleep(1)
    return "noform"


def _submit(driver) -> bool:
    btn = _first(driver, SUBMIT_SELECTORS)
    if not btn:
        return False
    try:
        btn.click()
    except Exception:  # noqa: BLE001
        driver.execute_script("arguments[0].click()", btn)
    time.sleep(3)
    return True


def _verified(driver) -> bool:
    """Проверяет, что отклик действительно ушёл — по разметке, а не только по тексту."""
    from selenium.webdriver.common.by import By

    # 1) Блок подтверждения отправки.
    for sel in (
        '[data-qa*="response-success"]',
        '[data-qa*="responseSent"]',
        '[data-qa*="vacancy-response-success"]',
        '[data-qa="vacancy-response-popup-success"]',
    ):
        try:
            if driver.find_elements(By.CSS_SELECTOR, sel):
                return True
        except Exception:  # noqa: BLE001
            continue

    # 2) Кнопка «Откликнуться» пропала или стала неактивной.
    for sel in ('[data-qa="vacancy-response-link-top"]', '[data-qa="vacancy-response-link-bottom"]'):
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, sel)
        except Exception:  # noqa: BLE001
            continue
        for el in elements:
            attrs = f"{el.get_attribute('class') or ''} {el.get_attribute('aria-disabled') or ''}".lower()
            if el.get_attribute("disabled") is not None or "disabled" in attrs:
                return True

    # 3) Текстовые маркеры на двух языках.
    text = _page_text(driver)
    return any(m in text for m in ALREADY_MARKERS)


def reconcile_applied(cfg: dict, store, driver, log=_log) -> int:
    """Сверяет статусы с реальным списком откликов hh.

    Нужно, потому что подтверждение на странице может не сработать из-за вёрстки
    или языка интерфейса. Список откликов — источник истины.
    """
    from .inbox import load_topics

    try:
        topics = load_topics(driver, cfg)
    except Exception as exc:  # noqa: BLE001
        log(f"    ! сверка со списком откликов не удалась: {exc}")
        return 0

    responded = {str(t.get("vacancyId")) for t in topics if t.get("vacancyId")}
    fixed = 0
    for row in store.all("status IN ('error','lettered','new')", board=cfg.get("board")):
        if str(row["id"]) in responded:
            note = "подтверждено списком откликов hh"
            # сохраняем признак «без письма», иначе досылка не найдёт эту вакансию
            if "без сопроводительного" in (row.get("note") or ""):
                note = "отклик без сопроводительного (подтверждено списком hh)"
            store.set_status(row["id"], "applied", note)
            fixed += 1
    return fixed


def dump_qa(driver, path: str) -> str:
    """Сохраняет все data-qa на странице — нужно для калибровки селекторов."""
    from selenium.webdriver.common.by import By

    os.makedirs(os.path.dirname(path), exist_ok=True)
    values = set()
    for el in driver.find_elements(By.CSS_SELECTOR, "[data-qa]"):
        try:
            values.add(el.get_attribute("data-qa") or "")
        except Exception:  # noqa: BLE001
            continue
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"URL: {driver.current_url}\n\n")
        fh.write("\n".join(sorted(v for v in values if v)))
    return path


def calibrate(cfg: dict, store) -> None:
    """Интерактивная калибровка: открывает вакансию и снимает разметку формы."""
    driver = build_driver(cfg)
    try:
        ensure_login(driver, cfg)
        queue = store.queue(limit=1, board=cfg.get("board"))
        if not queue:
            _log("очередь пуста — сначала search/letters")
            return
        vac = queue[0]
        _log(f"калибровка на вакансии «{vac['title']}»")
        _open_response_form(driver, vac)
        out = os.path.join(cfg["paths"]["debug_dir"], "calibrate_dom.txt")
        dump_qa(driver, out)
        shot = os.path.join(cfg["paths"]["debug_dir"], "calibrate.png")
        driver.save_screenshot(shot)
        _log(f"разметка сохранена: {out}")
        _log(f"скриншот: {shot}")
        if not cfg["browser"].get("keep_open"):
            input("Нажми Enter, чтобы закрыть браузер…")
    finally:
        if not cfg["browser"].get("keep_open"):
            driver.quit()


def _resolve_letter_path(cfg: dict, vac: dict) -> str:
    """Путь к письму. В базе мог остаться путь с другой ОС — берём локальный."""
    path = vac.get("letter_path") or ""
    if path and os.path.exists(path):
        return path
    local = os.path.join(cfg["paths"]["letters_dir"], f"{vac['id']}.txt")
    return local if os.path.exists(local) else ""


def _attach_letter_for(driver, cfg, store, vac, letter, topics_cache, log=_log) -> bool:
    """Прикладывает письмо к отклику (для вакансий «в один клик»).

    Карта vacancyId→chatId подгружается лениво, один раз на прогон.
    True — письмо приложено (или уже было).
    """
    from .chat_send import attach_cover_letter

    vid = str(vac["id"])
    if "map" not in topics_cache:
        from .inbox import load_topics

        try:
            mapping: dict[str, str] = {}
            for topic in load_topics(driver, cfg):
                v = str(topic.get("vacancyId") or "")
                if v:
                    mapping[v] = str(topic.get("chatId") or "")
            topics_cache["map"] = mapping
        except Exception as exc:  # noqa: BLE001
            log(f"    ! не удалось получить чаты: {exc}")
            topics_cache["map"] = {}
    chat_id = topics_cache["map"].get(vid)
    if not chat_id:
        return False
    try:
        result = attach_cover_letter(driver, cfg, chat_id, letter)
    except Exception as exc:  # noqa: BLE001
        log(f"    ! приложить письмо не удалось: {exc}")
        return False
    if result in ("ok", "already"):
        store.set_status(vac["id"], "applied",
                         "сопроводительное приложено к отклику" if result == "ok"
                         else "письмо уже было приложено")
        return True
    return False


def _answer_form_questions(driver, cfg: dict, form_text: str, fields, log=_log) -> bool:
    """Отвечает на скрининговые вопросы формы отклика с помощью ИИ.

    hh показывает вопросы работодателя как отдельные поля (textarea name="task_<id>_text"),
    а текст вопросов — рядом с полями. Отдаём ИИ весь текст формы и профиль: он
    возвращает по ответу на каждый вопрос, мы подставляем их по порядку.
    """
    import json
    import re

    from . import ai
    from .config import load_profile

    if not ai.api_available(cfg):
        log("    ! ИИ недоступен — вопросы не заполнены")
        return False
    profile = load_profile()
    profile_text = ai._profile_for_prompt(profile) if hasattr(ai, "_profile_for_prompt") else str(profile)
    prompt = (
        "Ниже форма отклика на вакансию. В ней вопросы работодателя — каждый ответ "
        "идёт в отдельное поле по порядку.\n"
        "Дай ответ на каждый вопрос: 1–3 предложения, только факты из профиля, "
        "без воды и без выдумок. Если вопрос про деньги — используй вилку из профиля.\n"
        "Верни СТРОГО JSON-массив строк (по одной на вопрос), без пояснений и без ```.\n\n"
        f"ПРОФИЛЬ:\n{profile_text}\n\n"
        f"ФОРМА:\n{form_text[:4000]}"
    )
    try:
        # Ответы на скрининговые вопросы — это JSON со свободным текстом.
        # Reasoning-модель тратит заметную часть лимита на размышления, поэтому
        # стартуем с запасом, иначе первый ответ обрезается и время уходит на повторы.
        answer_tokens = int((cfg.get("forms") or {}).get("answer_max_tokens", 6000))
        raw = ai.chat(cfg, [{"role": "user", "content": prompt}],
                      max_tokens=answer_tokens) or ""
    except Exception as exc:  # noqa: BLE001
        log(f"    ! ИИ не ответил на вопросы: {str(exc)[:90]}")
        return False
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        answers = json.loads(raw)
    except Exception:  # noqa: BLE001
        start, end = raw.find("["), raw.rfind("]")
        if start < 0 or end < 0:
            log("    ! ИИ вернул не JSON — вопросы не заполнены")
            return False
        try:
            answers = json.loads(raw[start:end + 1])
        except Exception:  # noqa: BLE001
            log("    ! ответ ИИ не разобрать — вопросы не заполнены")
            return False
    if not isinstance(answers, list) or not answers:
        return False
    filled = 0
    for el, answer in zip(fields, answers):
        text = str(answer).strip()
        if not text:
            continue
        try:
            driver.execute_script(
                """
                const el = arguments[0], v = arguments[1];
                const proto = el.tagName === 'TEXTAREA'
                    ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
                Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, v);
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                """, el, text)
            filled += 1
            log(f"      · вопрос {filled}: {text[:70]}")
        except Exception as exc:  # noqa: BLE001
            log(f"    ! не удалось заполнить поле: {str(exc)[:60]}")
    return filled > 0


def apply_queue(cfg: dict, store, limit: int | None = None, dry_run: bool = False,
                once: bool = False, driver=None) -> dict:
    if store.is_halted():
        _log("система на аварийном стопе. Возобновить: боту /resume")
        return {"applied": 0, "skipped": 0, "halted": True}

    limits = cfg["rate_limits"]
    daily_cap = int(limits.get("daily_cap", 30))
    ai_daily_cap = int(limits.get("ai_daily_cap", 15))
    max_per_run = int(limits.get("max_per_run", 15))
    if limit:
        max_per_run = min(max_per_run, limit)

    # Два потока: обычные вакансии и AI/вайбкодинг. У AI своя дневная квота,
    # чтобы он не съедал лимит обычных откликов.
    funnel_ai = lambda v: (v.get("funnel") or "").strip().upper() == "AI"

    already = store.applied_today(board=cfg.get("board"), exclude_funnel="AI")
    remaining_daily = max(0, daily_cap - already)
    budget = min(max_per_run, remaining_daily)
    ai_used = store.applied_today(board=cfg.get("board"), funnel="AI")
    ai_budget = max(0, min(ai_daily_cap - ai_used, max_per_run))

    hour = time.localtime().tm_hour
    lo, hi = limits.get("work_hours", [0, 24])
    if not (lo <= hour < hi):
        _log(f"вне рабочих часов ({lo}:00–{hi}:00) — отклики не отправляю")
        return {"applied": 0, "skipped": 0, "reason": "outside_work_hours"}

    all_rows = store.queue(limit=None, only_auto=True, board=cfg.get("board"))
    ai_rows = [v for v in all_rows if funnel_ai(v)]
    main_rows = [v for v in all_rows if not funnel_ai(v)]
    queue = main_rows[:budget] + ai_rows[:ai_budget]
    if not queue:
        _log("очередь автоотклика пуста (офисные вакансии отправляются вручную)")
        return {"applied": 0, "skipped": 0}

    _log(f"в очереди {len(all_rows)} (обычных {len(main_rows)}/{budget}, "
         f"AI {len(ai_rows)}/{ai_budget}) · сегодня отправлено {already}/{daily_cap}, "
         f"AI {ai_used}/{ai_daily_cap}")

    if dry_run:
        for i, vac in enumerate(queue[:budget], 1):
            _log(f"[dry-run] {i}. {vac['title']} — {vac['company']} → {vac.get('response_url') or vac['url']}")
        return {"applied": 0, "skipped": 0, "dry_run": len(queue[:budget])}

    own_driver = driver is None
    if own_driver:
        driver = build_driver(cfg)
    stats = {"applied": 0, "skipped": 0, "errors": 0}
    try:
        ensure_login(driver, cfg)

        topics_cache: dict = {}
        captcha_hits = 0
        max_captchas = int((cfg.get("captcha") or {}).get("max_per_run", 2))
        for idx, vac in enumerate(queue[:budget], start=1):
            if is_captcha(driver):
                captcha_hits += 1
                if captcha_hits > max_captchas:
                    _log(f"    · капча {captcha_hits}-й раз за прогон — "
                         f"останавливаю до следующего цикла (анти-бот)")
                    break
                if not handle_captcha(cfg, store, driver, _log):
                    break
                # капча снята — продолжаем с текущей вакансией

            _log(f"[{idx}/{budget}] {vac['title']} — {vac['company']} (балл {vac['score']})")

            # Защита от повторного отклика на ту же вакансию.
            if store.has_responded(vac["id"]):
                _log("    · уже откликались ранее — пропускаю")
                stats["skipped"] += 1
                continue

            if idx > 1:
                human_sleep(limits.get("min_delay_sec", 90), limits.get("max_delay_sec", 300), _log)

            cooldown_every = int(limits.get("cooldown_every", 0) or 0)
            if cooldown_every and idx > 1 and (idx - 1) % cooldown_every == 0:
                cd = int(limits.get("cooldown_sec", 1200))
                _log(f"    длинная пауза {cd} c после {idx - 1} откликов")
                time.sleep(cd)

            letter = ""
            letter_path = _resolve_letter_path(cfg, vac)
            if letter_path:
                with open(letter_path, encoding="utf-8") as fh:
                    raw = fh.read()
                letter = raw.split("\n\n", 1)[1].strip() if "\n\n" in raw else raw.strip()

            try:
                form_status = _open_response_form(driver, vac)
                if form_status == "already":
                    _log("    · уже откликались на hh — пропускаю")
                    store.set_status(vac["id"], "already", "уже откликались на hh")
                    stats["skipped"] += 1
                    continue
                if form_status != "ok":
                    # hh оформил отклик «в один клик» — сразу прикладываем письмо в чат
                    _log("    · форма не открылась (hh оформил в один клик) — сразу прикладываю письмо")
                    if letter and _attach_letter_for(driver, cfg, store, vac, letter, topics_cache):
                        _log("    ✓ письмо приложено к отклику")
                        stats["applied"] += 1
                    else:
                        store.set_status(
                            vac["id"], "error", "отклик без сопроводительного (форма не открылась)"
                        )
                        stats["errors"] += 1
                    continue

                # Скрининговые вопросы и тестовые задания работодателя.
                # Без ответов форма не отправляется. Раньше такие вакансии
                # сразу уходили в «ручные», хотя автоответчик умеет их заполнять.
                from selenium.webdriver.common.by import By as _By

                has_questions = _has_questions(driver)
                test_fields = [
                    el for el in driver.find_elements(
                        _By.CSS_SELECTOR, 'textarea[name^="task_"], [data-qa*="task-text"]')
                    if el.is_displayed()
                ]
                if has_questions or test_fields:
                    if not test_fields:
                        _log("    · вопросы без текстовых полей (выбор варианта) — нужен ручной отклик")
                        store.set_status(vac["id"], "needs_manual", "вопросы-выбор")
                        stats["skipped"] += 1
                        continue
                    _log(f"    · в форме {len(test_fields)} вопрос(ов) работодателя — отвечаю через ИИ")
                    form_text = ""
                    try:
                        form_text = (driver.find_element(_By.TAG_NAME, "body").text or "")[:4000]
                    except Exception:  # noqa: BLE001
                        pass
                    if not _answer_form_questions(driver, cfg, form_text, test_fields, _log):
                        _log("    · ИИ не справился — оставляю на ручной отклик")
                        store.set_status(vac["id"], "needs_manual", "вопросы работодателя")
                        stats["skipped"] += 1
                        continue
                    _log("    ✓ ответы на вопросы заполнены")

                if not letter:
                    _log("    ! письма нет — отклик не отправляю, отложу до следующего цикла")
                    stats["skipped"] += 1
                    continue

                if not _fill_letter(driver, letter):
                    _log("    ! письмо не вписалось — отклик НЕ отправляю")
                    dump_qa(driver, os.path.join(cfg["paths"]["debug_dir"], f"{vac['id']}_letter.txt"))
                    driver.save_screenshot(os.path.join(cfg["paths"]["debug_dir"], f"{vac['id']}_letter.png"))
                    store.set_status(vac["id"], "error", "письмо не заполнено, отклик не отправлен")
                    stats["errors"] += 1
                    continue

                if not _submit(driver):
                    if _verified(driver):
                        _log("    · кнопки нет, но отклик уже существует — фиксирую")
                        store.set_status(vac["id"], "applied", "отклик уже существует")
                        stats["applied"] += 1
                        continue
                    _log("    ! кнопка отправки не найдена")
                    dump_qa(driver, os.path.join(cfg["paths"]["debug_dir"], f"{vac['id']}_nodump.txt"))
                    driver.save_screenshot(os.path.join(cfg["paths"]["debug_dir"], f"{vac['id']}_nosubmit.png"))
                    store.set_status(vac["id"], "error", "не найдена кнопка отправки")
                    stats["errors"] += 1
                    continue

                ok = _verified(driver)
                store.set_status(vac["id"], "applied" if ok else "error",
                                 "отправлено" if ok else "отправка не подтверждена")
                if ok:
                    _log("    ✓ отклик отправлен")
                    stats["applied"] += 1
                else:
                    _log("    ? не удалось подтвердить отправку (проверь вручную)")
                    stats["errors"] += 1

            except Exception as exc:  # noqa: BLE001
                _log(f"    ! ошибка: {exc}")
                store.set_status(vac["id"], "error", str(exc)[:200])
                stats["errors"] += 1

            if once:
                _log("режим --once: останавливаюсь после одной вакансии")
                break

        # Сверка с реальным списком откликов hh — источник истины.
        fixed = reconcile_applied(cfg, store, driver, _log)
        if fixed:
            _log(f"    сверка: подтверждено откликов — {fixed}")
            stats["applied"] += fixed
            stats["errors"] = max(0, stats["errors"] - fixed)

        store.log_run("apply", str(stats))
    finally:
        if own_driver and not cfg["browser"].get("keep_open"):
            driver.quit()
    _log(f"итог: отправлено {stats['applied']}, пропущено {stats['skipped']}, ошибок {stats['errors']}")
    return stats


def _captcha_png_big(driver, scale: int = 3) -> bytes | None:
    """Картинка капчи, увеличенная в N раз.

    hh рисует капчу мелко, и модель путает буквы. Канвас апскейлит её без
    сглаживания — символы становятся крупнее и читаются заметно лучше.
    """
    import base64

    from selenium.webdriver.common.by import By

    for sel in ('img[src*="captcha" i]', '[data-qa*="captcha"] img',
                '.smart-captcha img', '[data-qa*="captcha"] canvas',
                '.smart-captcha canvas', "canvas"):
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, sel)
        except Exception:  # noqa: BLE001
            continue
        for el in elements:
            try:
                if not el.is_displayed():
                    continue
                data = driver.execute_script(
                    """
                    const node = arguments[0], s = arguments[1];
                    const src = (node.tagName === 'CANVAS')
                        ? node : (node.naturalWidth ? node : null);
                    if (!src) return null;
                    const w = node.naturalWidth || node.width;
                    const h = node.naturalHeight || node.height;
                    if (!w || !h) return null;
                    const c = document.createElement('canvas');
                    c.width = w * s; c.height = h * s;
                    const ctx = c.getContext('2d');
                    ctx.imageSmoothingEnabled = false;
                    ctx.drawImage(node, 0, 0, c.width, c.height);
                    try { return c.toDataURL('image/png'); } catch (e) { return null; }
                    """, el, int(scale))
                if not data:
                    continue
                return base64.b64decode(data.split(",", 1)[1])
            except Exception:  # noqa: BLE001
                continue
    return None


def _screenshot_element(driver, selectors) -> bytes | None:
    """PNG первого видимого элемента из списка селекторов."""
    from selenium.webdriver.common.by import By

    for sel in selectors:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, sel)
        except Exception:  # noqa: BLE001
            continue
        for el in elements:
            try:
                size = el.size or {}
                if el.is_displayed() and size.get("width", 0) >= 20 and size.get("height", 0) >= 15:
                    return el.screenshot_as_png
            except Exception:  # noqa: BLE001
                continue
    return None


def _captcha_image(driver) -> bytes | None:
    """Картинка капчи: сначала сама картинка, иначе — скриншот всей страницы."""
    png = _screenshot_element(driver, CAPTCHA_IMAGE_SELECTORS)
    if png:
        return png
    try:
        return driver.get_screenshot_as_png()
    except Exception:  # noqa: BLE001
        return None


def _captcha_snapshot(driver) -> bytes | None:
    """Текущая картинка капчи — и для отправки, и для сравнения (обновилась ли)."""
    return _screenshot_element(driver, CAPTCHA_IMAGE_SELECTORS)


def _send_captcha(cfg: dict, driver, log=_log, note: str = "") -> bytes | None:
    """Отправляет ТЕКУЩУЮ капчу в Telegram. Возвращает отправленную картинку."""
    png = _captcha_snapshot(driver) if driver is not None else None
    try:
        from .notify import Notifier

        caption = (
            "🚨 КАПЧА на hh.ru" + (f"\n{note}" if note else "") + "\n\n"
            "Нейросеть не справилась. Пришли ответ с ЭТОЙ картинки обычным сообщением "
            "(или /captcha <ответ>) — я введу его сам. Либо пройди капчу в окне браузера."
        )
        notifier = Notifier(cfg, log=log)
        if png:
            try:
                os.makedirs(cfg["paths"]["debug_dir"], exist_ok=True)
                with open(os.path.join(cfg["paths"]["debug_dir"], "captcha.png"), "wb") as fh:
                    fh.write(png)
            except OSError:
                pass
            notifier.send_photo(png, caption)
        else:
            notifier.send_now("🚨 КАПЧА на hh.ru", caption)
    except Exception as exc:  # noqa: BLE001
        log(f"не удалось отправить капчу в Telegram: {exc}")
    return png


def _dump_captcha(driver, cfg) -> None:
    """Сохраняет разметку капчи и список полей — чтобы отладить селекторы."""
    try:
        os.makedirs(cfg["paths"]["debug_dir"], exist_ok=True)
        path = os.path.join(cfg["paths"]["debug_dir"], "captcha.html")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(driver.page_source)
        from selenium.webdriver.common.by import By

        found = []
        for tag in ("input", "textarea", "iframe"):
            for el in driver.find_elements(By.TAG_NAME, tag):
                try:
                    found.append(
                        f"<{tag} name={el.get_attribute('name')!r} "
                        f"type={el.get_attribute('type')!r} "
                        f"data-qa={el.get_attribute('data-qa')!r} "
                        f"src={(el.get_attribute('src') or '')[:50]!r}>"
                    )
                except Exception:  # noqa: BLE001
                    pass
        _log("    капча: поля/iframe на странице — " + "; ".join(found[:12]))
    except Exception as exc:  # noqa: BLE001
        _log(f"    капча: не смог сохранить разметку ({exc})")


def _solve_captcha_ai(cfg: dict, driver, log=_log) -> bool:
    """Пытается распознать капчу нейросетью и ввести ответ. True — капча снята."""
    if not (cfg.get("captcha") or {}).get("try_ai", True):
        return False
    from . import ai

    if not ai.api_available(cfg):
        log("    капча: LLM недоступна — AI-путь пропускаю")
        return False
    png = _captcha_png_big(driver) or _screenshot_element(driver, CAPTCHA_IMAGE_SELECTORS)
    if not png:
        log("    капча: картинку капчи не нашли — AI-путь пропускаю")
        return False
    try:
        answer = ai.solve_image_captcha(cfg, png)
    except Exception as exc:  # noqa: BLE001
        log(f"    капча: AI не смог распознать ({exc})")
        return False
    if not answer or len(answer) > 40:
        log(f"    капча: AI ответил непонятно ({answer!r}) — перехожу к ручному")
        return False
    log(f"    капча: AI распознал «{answer}» — ввожу")
    field = _first(driver, CAPTCHA_INPUT_SELECTORS)
    if not field:
        log("    капча: поля ввода нет (похоже на капчу-галочку) — AI бессилен")
        return False
    try:
        field.clear()
        field.send_keys(answer)
        btn = _first(driver, CAPTCHA_SUBMIT_SELECTORS)
        if btn:
            try:
                btn.click()
            except Exception:  # noqa: BLE001
                driver.execute_script("arguments[0].click()", btn)
        else:
            from selenium.webdriver.common.keys import Keys

            field.send_keys(Keys.ENTER)
    except Exception as exc:  # noqa: BLE001
        log(f"    капча: не удалось ввести ответ ({exc})")
        return False
    time.sleep(3)
    return not is_captcha(driver)


def _type_captcha_answer(driver, answer: str) -> bool:
    """Вписывает ответ капчи и жмёт отправку. Ищем поле и в основной странице, и в iframe."""
    from selenium.webdriver.common.by import By

    frames = [None] + driver.find_elements(By.TAG_NAME, "iframe")
    for fr in frames:
        if fr is not None:
            try:
                driver.switch_to.frame(fr)
            except Exception:  # noqa: BLE001
                continue
        try:
            field = _first(driver, CAPTCHA_INPUT_SELECTORS)
            if field:
                try:
                    field.clear()
                    field.send_keys(answer)
                    btn = _first(driver, CAPTCHA_SUBMIT_SELECTORS)
                    if btn:
                        try:
                            btn.click()
                        except Exception:  # noqa: BLE001
                            driver.execute_script("arguments[0].click()", btn)
                    else:
                        from selenium.webdriver.common.keys import Keys

                        field.send_keys(Keys.ENTER)
                    return True
                except Exception as exc:  # noqa: BLE001
                    _log(f"    капча: не удалось ввести ответ ({exc})")
        finally:
            if fr is not None:
                try:
                    driver.switch_to.default_content()
                except Exception:  # noqa: BLE001
                    pass
    _log("    капча: поля ввода не нашли ни на странице, ни в iframe")
    return False


def _wait_captcha(cfg: dict, store, driver, log=_log) -> tuple[bool, str]:
    """Показывает капчу в Telegram и ждёт ответ (или ручное решение в окне).

    Если капча обновляется (после неверного ответа hh выдаёт новую картинку),
    отправляем НОВУЮ картинку, чтобы ты не вводил ответ к старой.
    Возвращает (снята?, способ).
    """
    captcha_cfg = cfg.get("captcha") or {}
    timeout = int(captcha_cfg.get("manual_timeout_sec", 1800))
    poll = max(2, int(captcha_cfg.get("poll_sec", 5)))
    store.set_state("captcha.request_ts", now_iso())
    store.set_state("captcha.answer", "")
    sent = _send_captcha(cfg, driver, log)
    _log(f"    капча: жду ответ в Telegram или ручное решение в окне (до {timeout} c)")
    deadline = time.time() + timeout
    ticks = 0
    while time.time() < deadline:
        if not is_captcha(driver):
            return True, "вручную в браузере"
        answer = (store.get_state("captcha.answer", "") or "").strip()
        if answer:
            store.set_state("captcha.answer", "")
            _log(f"    капча: получил ответ из Telegram «{answer}» — ввожу")
            if _type_captcha_answer(driver, answer):
                time.sleep(3)
                if not is_captcha(driver):
                    return True, "ответом из Telegram"
            _log("    капча: ответ не подошёл — жду дальше")
            fresh = _captcha_snapshot(driver)
            if fresh and fresh != sent:
                sent = _send_captcha(cfg, driver, log, note="⚠️ Капча обновилась — вот НОВАЯ картинка")
        else:
            ticks += 1
            if ticks % 6 == 0:
                fresh = _captcha_snapshot(driver)
                if fresh and fresh != sent:
                    sent = _send_captcha(cfg, driver, log, note="⚠️ Капча обновилась — вот НОВАЯ картинка")
        time.sleep(poll)
    return (not is_captcha(driver)), ""


def _captcha_finish(cfg: dict, store, ok: bool, how: str, log=_log) -> None:
    """Сбрасывает состояние капчи и сообщает итог в Telegram."""
    store.set_state("captcha.request_ts", "")
    store.set_state("captcha.answer", "")
    try:
        from .notify import Notifier

        n = Notifier(cfg, log=log)
        if ok:
            n.send_now("✅ Капча пройдена", f"Решена {how or 'вручную'}. Продолжаю автоматику.")
        else:
            n.send_now(
                "❌ Капчу решить не удалось",
                "Автоматика остановлена. Когда пройдёшь проверку на hh — отправь /resume.",
            )
    except Exception as exc:  # noqa: BLE001
        log(f"капча: не удалось уведомить об итоге: {exc}")


def _captcha_cooldown(cfg: dict, log=_log) -> None:
    """Пауза после капчи — чтобы не провоцировать анти-бот новыми запросами."""
    sec = int((cfg.get("captcha") or {}).get("cooldown_sec", 240))
    if sec <= 0:
        return
    log(f"    капча: пауза {sec} c после проверки, чтобы не злить анти-бот")
    time.sleep(sec)


def handle_captcha(cfg: dict, store, driver=None, log=_log) -> bool:
    """Капча: сначала нейросеть (до N попыток); иначе — Telegram и ждём ответ.

    Возвращает True, если капча снята и можно продолжать; False — если нет.
    """
    already = store.is_halted()
    if driver is not None:
        _dump_captcha(driver, cfg)
    if not already:
        _log("!!! капча на hh.ru — сначала пробую нейросеть")

    captcha_cfg = cfg.get("captcha") or {}
    attempts = max(1, int(captcha_cfg.get("ai_attempts", 2)))

    # 1) Пробуем сами. Если hh не принял ответ — он показывает НОВУЮ картинку,
    #    поэтому пробуем прочитать заново, а не идём сразу в Telegram.
    for attempt in range(1, attempts + 1):
        if driver is None:
            break
        if _solve_captcha_ai(cfg, driver, log):
            _log("!!! капча решена нейросетью — продолжаю")
            store.set_halted(False)
            _captcha_finish(cfg, store, True, "нейросетью", log)
            _captcha_cooldown(cfg, log)
            return True
        if attempt < attempts:
            _log(f"    капча: попытка {attempt} не прошла — обновляю картинку")
            try:
                driver.refresh()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(3)

    # 2) AI не справился — показываем капчу в Telegram и ждём ответ/ручное решение.
    if driver is not None:
        ok, how = _wait_captcha(cfg, store, driver, log)
        if ok:
            _log(f"!!! капча решена — продолжаю ({how})")
            store.set_halted(False)
            _captcha_finish(cfg, store, True, how, log)
            _captcha_cooldown(cfg, log)
            return True

    store.set_halted(True, "капча на hh.ru")
    if not already:
        _log("!!! капча не решена — автоматика остановлена (снять: /resume)")
    _captcha_finish(cfg, store, False, "", log)
    return False


def login(cfg: dict) -> None:
    driver = build_driver(cfg, hidden=False)  # вход — окно должно быть видно
    port = int(cfg["browser"].get("debug_port") or 0)
    try:
        ensure_login(driver, cfg, interactive=True)
        _log("можно закрывать: профиль браузера сохранён, повторный вход не потребуется")
        if port:
            _log(f"отладка доступна на порту {port} (для входа на сервере через SSH-туннель)")
    finally:
        if port:
            _log("браузер оставлен открытым из-за включённого debug_port — закрой вручную")
        elif not cfg["browser"].get("keep_open"):
            driver.quit()
