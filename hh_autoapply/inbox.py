"""Чтение переписки по откликам.

Официальный API откликов соискателя закрыт, поэтому данные берём из двух мест:
1. Встроенный JSON `applicantNegotiations.topicList` на странице
   `/applicant/negotiations` — там chatId, vacancyId, состояние отклика.
2. Сам чат по прямому адресу `https://chatik.hh.ru/chat/{chatId}` — сообщения
   помечены `data-qa="chatik-chat-message-<id>"`, направление видно по классу
   `chat-bubble_outgoing` / `chat-bubble_incoming`.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time

TOPIC_MARK = '"topicList":['

MESSAGE_PREFIX = "chatik-chat-message-"
MESSAGE_ID_RE = re.compile(r"^chatik-chat-message-(\d+)$")

# Состояния отклика из topicList, которые считаем содержательным результатом.
STATE_INVITE = {"INVITATION", "INVITED", "APPOINTMENT", "INTERVIEW"}
STATE_REJECT = {"DISCARD", "DISCARDED", "REJECTION"}


def _log(msg: str) -> None:
    print(f"[inbox] {msg}", flush=True)


def _mid(chat_id: str, message_id: str) -> str:
    return hashlib.sha1(f"{chat_id}:{message_id}".encode("utf-8")).hexdigest()


def extract_topics(html: str) -> list[dict]:
    """Достаёт список откликов из встроенного JSON страницы."""
    start = html.find(TOPIC_MARK)
    if start < 0:
        return []
    arr_start = html.find("[", start + len(TOPIC_MARK) - 1)
    if arr_start < 0:
        return []
    try:
        data, _ = json.JSONDecoder().raw_decode(html[arr_start:])
    except json.JSONDecodeError:
        return []
    return [item for item in data if isinstance(item, dict)]


def load_topics(driver, cfg: dict) -> list[dict]:
    host = cfg["host"]
    topics: list[dict] = []
    seen: set[str] = set()
    # Список переписок у hh пагинируется (?page=N) и отсортирован по свежести:
    # изменённые/непрочитанные чаты — сверху. Грузим только первые страницы
    # (topics_pages), иначе со временем список рос бы до бесконечности.
    max_pages = max(1, int(cfg.get("topics_pages", 3) or 3))
    for page in range(0, max_pages):
        url = f"https://{host}/applicant/negotiations"
        if page:
            url += f"?page={page}"
        driver.get(url)
        time.sleep(4)
        chunk = extract_topics(driver.page_source)
        new = 0
        for topic in chunk:
            key = str(topic.get("chatId") or topic.get("vacancyId") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            topics.append(topic)
            new += 1
        if new == 0:
            break
    return topics


OUT_MARKERS = ("chat-bubble_outgoing", "message_my--")
IN_MARKERS = (
    "chat-bubble_incoming",
    "chat-bubble_bot",
    "chat-bubble_with-left-tail",
    "chat-bubble-author-name",
)


def _direction(outer_html: str) -> str:
    """Определяет автора сообщения по классам пузыря.

    Наши сообщения всегда помечены message_my/chat-bubble_outgoing. Если этих
    меток нет — сообщение чужое. Раньше по умолчанию возвращалось «out», из-за
    чего вопросы работодателя записывались как наши и черновики не создавались.
    """
    if any(marker in outer_html for marker in OUT_MARKERS):
        return "out"
    low = outer_html.lower()
    # Сообщения, отправленные вручную из веба/приложения hh, иногда приходят без
    # меток message_my: подписываем их сами (фамилия кандидата или «Вы»), иначе
    # бот принимал собственное письмо за ответ работодателя и предлагал ответить себе.
    if "кудряшов" in low or "вы:" in low or ">вы<" in low:
        return "out"
    if any(marker in outer_html for marker in IN_MARKERS):
        return "in"
    return "in"


SYSTEM_MESSAGE_MARKERS = (
    "no cover letter",
    "vacancy application",
    "add a cover letter",
    "резюме отправлено",
    "joined the chat",
    "left the chat",
    "покинул чат",
)


def _is_system_message(text: str) -> bool:
    """Служебные плашки hh в чате — это не сообщения работодателя."""
    if len(text) > 300:
        return False
    low = text.lower()
    return any(marker in low for marker in SYSTEM_MESSAGE_MARKERS)


def _full_text(driver, element) -> str:
    """Полный текст сообщения.

    `element.text` в Selenium отдаёт только ВИДИМУЮ часть: hh визуально
    обрезает длинные сообщения, и мы теряли половину письма. innerText
    возвращает текст целиком независимо от отображения.
    """
    from selenium.webdriver.common.by import By

    try:
        node = element.find_element(By.CSS_SELECTOR, '[data-qa="chat-bubble-text"]')
    except Exception:  # noqa: BLE001
        node = element
    try:
        text = driver.execute_script(
            "return arguments[0].innerText || arguments[0].textContent || ''", node
        )
        return (text or "").strip()
    except Exception:  # noqa: BLE001
        return (node.text or "").strip()


def read_chat(driver, chat_id: str, vacancy_id: str = "") -> list[dict]:
    """Открывает чат и возвращает сообщения."""
    from selenium.webdriver.common.by import By

    driver.get(f"https://chatik.hh.ru/chat/{chat_id}")
    time.sleep(5)

    messages: list[dict] = []
    seen: set[str] = set()
    try:
        elements = driver.find_elements(By.CSS_SELECTOR, f'[data-qa^="{MESSAGE_PREFIX}"]')
    except Exception:  # noqa: BLE001
        return messages

    for el in elements:
        qa = el.get_attribute("data-qa") or ""
        match = MESSAGE_ID_RE.match(qa)
        if not match:
            continue  # служебные блоки и дубли текста
        message_id = match.group(1)
        if message_id in seen:
            continue
        seen.add(message_id)

        outer = el.get_attribute("outerHTML") or ""
        text = _full_text(driver, el)
        if not text or _is_system_message(text):
            continue  # пустые пузыри и служебные блоки hh
        ts = ""
        try:
            ts = el.find_element(By.CSS_SELECTOR, '[data-qa="chat-buble-display-time"]').text
        except Exception:  # noqa: BLE001
            pass

        messages.append(
            {
                "id": _mid(chat_id, message_id),
                "message_id": message_id,
                "negotiation_id": str(chat_id),
                "vacancy_id": str(vacancy_id or ""),
                "direction": _direction(outer),
                "text": text,
                "ts": ts,
            }
        )
    return messages


def dump_page(driver, cfg: dict, name: str = "inbox_dump.html") -> str:
    path = os.path.join(cfg["paths"]["debug_dir"], name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(driver.page_source)
    return path


def _register_external(cfg: dict, store, vacancy_id: str, state: str) -> None:
    """Заводит отклик, отправленный вручную и не попавший в наш поиск."""
    from .models import Vacancy
    from .sources import fetch_detail

    title = company = work_format = ""
    try:
        detail = fetch_detail(cfg, vacancy_id)
        title = detail.get("title", "")
        company = detail.get("hiringOrganization", "")
        work_format = detail.get("work_formats", "")
    except Exception:  # noqa: BLE001
        pass

    vac = Vacancy(
        id=str(vacancy_id),
        title=title or f"Вакансия {vacancy_id}",
        company=company or "—",
        url=f"https://{cfg['host']}/vacancy/{vacancy_id}",
        work_format=work_format,
        source="inbox",
        decision="skip",
        reason="отклик отправлен вне нашей очереди",
        funnel="Вне поиска",
        eligibility="manual",
        status="applied",
        note="импортировано из списка откликов hh",
    )
    store.upsert(vac)


def _state_kind(state: str) -> str:
    state = (state or "").upper()
    if state in STATE_INVITE:
        return "invite"
    if state in STATE_REJECT:
        return "reject"
    return ""


def read_replies(cfg: dict, store, driver=None, profile: dict | None = None) -> dict:
    """Читает переписку и сохраняет новые сообщения.

    Возвращает статистику и список новых входящих.
    """
    from .apply_selenium import build_driver, ensure_login

    own_driver = driver is None
    if own_driver:
        driver = build_driver(cfg)
        ensure_login(driver, cfg)

    from .apply_selenium import is_captcha

    stats = {"dialogues": 0, "messages": 0, "new_incoming": 0, "new": [], "states": {}}
    if store.is_halted():
        _log("система на аварийном стопе — переписку не читаю (возобновить: /resume)")
        if own_driver:
            driver.quit()
        return stats
    try:
        topics = load_topics(driver, cfg)
        if is_captcha(driver):
            from .apply_selenium import handle_captcha

            if not handle_captcha(cfg, store, driver, _log):
                return stats
            topics = load_topics(driver, cfg)
        stats["dialogues"] = len(topics)
        if not topics:
            path = dump_page(driver, cfg)
            _log(f"отклики не распознаны — сохранил разметку: {path}")
            return stats

        for topic in topics:
            chat_id = str(topic.get("chatId") or "")
            vacancy_id = str(topic.get("vacancyId") or "")
            state = topic.get("lastState") or topic.get("initialState") or ""

            # Отклик по этой вакансии точно существует — фиксируем как отправленный.
            if vacancy_id and store.get(vacancy_id) is None:
                _register_external(cfg, store, vacancy_id, state)
            if vacancy_id and store.get(vacancy_id) and not store.has_responded(vacancy_id):
                store.set_status(vacancy_id, "applied", "есть отклик в списке hh")
            if vacancy_id:
                store.set_state(f"neg_state:{vacancy_id}", state)

            kind = _state_kind(state)
            if kind:
                stats["states"][kind] = stats["states"].get(kind, 0) + 1

            if not chat_id:
                continue
            # Читаем чат, только если он непрочитан/изменился или вакансия у нас в ошибке.
            last_mod = str(topic.get("lastModifiedMillis") or topic.get("lastModified") or "")
            seen = store.get_state(f"chat_seen:{chat_id}", "")
            vac_row = store.get(vacancy_id) if vacancy_id else None
            need_read = (
                bool(topic.get("hasNewMessages"))
                or (bool(last_mod) and last_mod != seen)
                or (vac_row is not None and (vac_row.get("status") or "") == "error")
            )
            if not need_read:
                stats["skipped_unchanged"] = stats.get("skipped_unchanged", 0) + 1
                continue
            if last_mod:
                store.set_state(f"chat_seen:{chat_id}", last_mod)
            try:
                messages = read_chat(driver, chat_id, vacancy_id)
            except Exception as exc:  # noqa: BLE001
                _log(f"  ! чат {chat_id}: {exc}")
                continue

            own_texts = store.out_texts(chat_id)
            for msg in messages:
                stats["messages"] += 1
                # Эхо собственного сообщения: hh отдаёт наш текст как «входящее».
                if msg["direction"] == "in":
                    norm = " ".join((msg["text"] or "").split()).strip().lower()[:400]
                    if norm and norm in own_texts:
                        msg["direction"] = "out"
                is_new = store.add_message(
                    msg["id"], msg["negotiation_id"], msg["vacancy_id"],
                    msg["direction"], msg["text"], msg["ts"],
                )
                if is_new and msg["direction"] == "in":
                    stats["new_incoming"] += 1
                    stats["new"].append(msg)
    finally:
        if own_driver:
            driver.quit()

    store.log_run("inbox", json.dumps({k: v for k, v in stats.items() if k != "new"}, ensure_ascii=False))
    return stats
