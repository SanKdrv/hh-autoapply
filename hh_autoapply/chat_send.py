"""Отправка сообщений в переписку hh (chatik.hh.ru).

Поле ввода: `textarea[data-qa="text-input"]` (placeholder "Message").
Отправка — Enter или кнопка отправки, если она есть.
"""
from __future__ import annotations

import time

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from .apply_selenium import _log

INPUT_SELECTORS = [
    'textarea[data-qa="text-input"]',
    '[data-qa="chatik-message-input"] textarea',
    'textarea[placeholder="Message"]',
    'textarea[placeholder="Сообщение"]',
    "textarea",
]

SEND_SELECTORS = [
    '[data-qa="chat-send-button"]',
    '[data-qa*="send-message"]',
    'button[type="submit"]',
]


def _first(driver, selectors):
    from selenium.webdriver.common.by import By

    for sel in selectors:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                if el.is_displayed() and el.is_enabled():
                    return el
        except Exception:  # noqa: BLE001
            continue
    return None


def open_chat(driver, chat_id: str, cfg: dict | None = None) -> None:
    driver.get(f"https://chatik.hh.ru/chat/{chat_id}")
    time.sleep(5)


def fill_message(driver, text: str) -> bool:
    """Вписывает текст в поле ввода (как это делает React)."""
    field = _first(driver, INPUT_SELECTORS)
    if not field:
        return False
    try:
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
            el.focus();
            """,
            field,
            text,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        _log(f"    ! не удалось вписать сообщение: {exc}")
        return False


def _messages_count(driver) -> int:
    from selenium.webdriver.common.by import By

    try:
        return len(driver.find_elements(By.CSS_SELECTOR, '[data-qa^="chatik-chat-message-"]'))
    except Exception:  # noqa: BLE001
        return -1


def send_message(driver, chat_id: str, text: str, dry_run: bool = False) -> bool:
    """Отправляет сообщение в чат. dry_run — только вписать, не отправлять."""
    from selenium.webdriver.common.keys import Keys

    if not text.strip():
        return False

    open_chat(driver, chat_id)
    before = _messages_count(driver)

    if not fill_message(driver, text.strip()):
        _log("    ! поле ввода сообщения не найдено")
        return False

    if dry_run:
        _log("    (dry-run) текст вписан, отправка не выполнена")
        return True

    button = _first(driver, SEND_SELECTORS)
    if button:
        try:
            button.click()
        except Exception:  # noqa: BLE001
            driver.execute_script("arguments[0].click()", button)
    else:
        field = _first(driver, INPUT_SELECTORS)
        if not field:
            return False
        field.send_keys(Keys.ENTER)

    # Ждём появления нового сообщения в переписке.
    for _ in range(12):
        time.sleep(1)
        if _messages_count(driver) > before:
            return True
    _log("    ? отправка не подтверждена разметкой")
    return False


# ── приложить сопроводительное к уже отправленному отклику ──────────────

COVER_ACTION_SELECTOR = '[data-qa="chatik-chat-message-applicant-action"]'
COVER_SEND_SELECTORS = [
    '[data-qa="chatik-do-send-message"]',
    '[data-qa="chat-send-button"]',
    'button[type="submit"]',
]


def _is_cover_letter_action(text: str) -> bool:
    low = (text or "").lower()
    return "cover letter" in low or "сопроводительн" in low


def _cover_actions(driver):
    return [el for el in driver.find_elements(By.CSS_SELECTOR, COVER_ACTION_SELECTOR)
            if _is_cover_letter_action(el.text)]


def attach_cover_letter(driver, cfg: dict, chat_id: str, text: str, dry_run: bool = False) -> str:
    """Прикладывает сопроводительное к отклику через ссылку «Add a cover letter».

    Нужно, когда отклик ушёл без письма (hh «в один клик»), а чат может быть
    отключён работодателем — тогда сообщение не отправить, а письмо приложить можно.

    Возвращает: 'ok' — приложено; 'already' — письмо уже есть / ссылки нет;
    'fail' — не получилось.
    """
    if not text.strip():
        return "fail"
    driver.get(f"https://{cfg['host']}/chat/{chat_id}")
    time.sleep(5)
    action = next(iter(_cover_actions(driver)), None)
    if action is None:
        # ссылки «добавить письмо» нет — значит письмо уже приложено (не дублируем)
        return "already"
    try:
        driver.execute_script("arguments[0].click()", action)
    except Exception:  # noqa: BLE001
        return "fail"
    time.sleep(3)
    if not fill_message(driver, text.strip()):
        _log("    ! поле сопроводительного не найдено")
        return "fail"
    if dry_run:
        return "ok"
    time.sleep(1)
    button = _first(driver, COVER_SEND_SELECTORS)
    if button:
        try:
            button.click()
        except Exception:  # noqa: BLE001
            driver.execute_script("arguments[0].click()", button)
    else:
        field = _first(driver, INPUT_SELECTORS)
        if not field:
            return "fail"
        field.send_keys(Keys.ENTER)

    # письмо приложено, когда ссылка «Add a cover letter» исчезла
    for _ in range(10):
        time.sleep(1)
        if not _cover_actions(driver):
            return "ok"
    return "fail"
