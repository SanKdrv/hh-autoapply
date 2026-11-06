"""Хабр Карьера (career.habr.com) — через браузер.

Публичного API для соискателя нет (API включается только по заявке Хабром),
поэтому работаем Selenium в своём профиле браузера (data/browser-profile-habr).
Вход делается один раз: `main.py login --board habr` — окно открывается видимым,
входишь сам (почта/код), сессия остаётся в профиле.

Вёрстка (проверена на career.habr.com/vacancies):
  карточка  .vacancy-card, заголовок .vacancy-card__title a -> /vacancies/<id>
  компания  .vacancy-card__company, мета .vacancy-card__meta, навыки .vacancy-card__skills
"""
from __future__ import annotations

import re
import time
import urllib.parse

from . import register
from .base import Board

PREFIX = "hb_"
VACANCY_RE = re.compile(r"/vacancies/(\d+)")


@register
class Habr(Board):
    name = "habr"
    title = "Хабр Карьера"
    needs_browser = True
    login_url = "https://career.habr.com/login"

    # --- служебное -----------------------------------------------------
    def _url(self, path: str) -> str:
        host = self.cfg.get("host") or "career.habr.com"
        return f"https://{host}/{path.lstrip('/')}"

    # --- авторизация ---------------------------------------------------
    def is_logged_in(self, driver) -> bool:
        """Признак входа: в шапке нет пункта «Войти» и мы не на странице логина."""
        from selenium.webdriver.common.by import By

        url = (driver.current_url or "").lower()
        # любая страница авторизации (вход, регистрация, код) — ещё не вошли
        if "/login" in url or "/auth/" in url or "/register" in url:
            return False
        text = ""
        for sel in ("header", ".main-navbar", "nav", "body"):
            try:
                text = (driver.find_element(By.CSS_SELECTOR, sel).text or "").lower()
            except Exception:  # noqa: BLE001
                continue
            if text:
                break
        if not text:
            return False
        return "войти" not in text[:600] and "зарегистрироваться" not in text[:600]

    def ensure_login(self, driver, interactive=None, log=print) -> None:
        try:
            driver.get(self._url(""))
            time.sleep(3)
        except Exception:  # noqa: BLE001
            return
        if self.is_logged_in(driver):
            return
        log(f"  ! {self.title}: нет входа — поиск будет без авторизации, "
            f"отклики работать не будут. Войди: main.py login --board habr")

    # --- поиск ---------------------------------------------------------
    def search(self, log=print, driver=None):
        from selenium.webdriver.common.by import By

        from ..models import Vacancy

        own = driver is None
        if own:
            driver = self._own_driver()
        pages = int(self.cfg.get("search_pages", 3) or 3)
        found: dict[str, Vacancy] = {}
        try:
            for profile in self.cfg.get("searches") or []:
                text = profile.get("text") or profile.get("keyword") or ""
                if not text:
                    continue
                for page in range(1, pages + 1):
                    url = self._url("vacancies") + "?" + urllib.parse.urlencode(
                        {"q": text, "type": "all", "page": page}
                    )
                    try:
                        driver.get(url)
                        time.sleep(4)
                    except Exception as exc:  # noqa: BLE001
                        log(f"  ! {self.title}: «{text}» стр.{page} не открылась: {exc}")
                        break
                    cards = driver.find_elements(By.CSS_SELECTOR, ".vacancy-card")
                    log(f"  + {self.title} «{text}»: стр. {page}, {len(cards)} вакансий")
                    for card in cards:
                        vac = self._card_to_vacancy(card, profile)
                        if vac is not None:
                            found[vac.id] = vac
                    if len(cards) < 5:
                        break
                    time.sleep(1)
        finally:
            if own:
                try:
                    driver.quit()
                except Exception:  # noqa: BLE001
                    pass
        return list(found.values())

    def _card_to_vacancy(self, card, profile: dict):
        from selenium.webdriver.common.by import By

        from ..models import Vacancy
        from ..util import parse_salary_full

        try:
            link = card.find_element(By.CSS_SELECTOR, ".vacancy-card__title a")
            href = link.get_attribute("href") or ""
            title = (link.text or "").strip()
        except Exception:  # noqa: BLE001
            return None
        match = VACANCY_RE.search(href)
        if not title or not match:
            return None

        def _text(sel: str) -> str:
            try:
                return (card.find_element(By.CSS_SELECTOR, sel).text or "").strip()
            except Exception:  # noqa: BLE001
                return ""

        company = _text(".vacancy-card__company")
        meta = _text(".vacancy-card__meta")
        skills = ", ".join(
            (s.text or "").strip() for s in card.find_elements(By.CSS_SELECTOR, ".vacancy-card__skills a")
        )
        vac = Vacancy(
            id=f"{PREFIX}{match.group(1)}",
            board=self.name,
            title=title,
            company=company,
            work_format=meta,
            url=href,
            salary=meta,
            source="habr_html",
            description=skills,
            funnel=(profile.get("funnel") or profile.get("text") or "")[:40],
        )
        if meta:
            vac.salary_from, vac.salary_to, vac.salary_kind = parse_salary_full(meta)
        return vac

    def fetch_detail(self, vid: str, use_cache: bool = True, url: str = "") -> dict:
        raw = vid[len(PREFIX):] if vid.startswith(PREFIX) else vid
        page = url or self._url(f"vacancies/{raw}")
        try:
            from ..util import http_get

            html = http_get(page, timeout=15) or ""
        except Exception:  # noqa: BLE001
            return {}
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text)
        # формат работы нужен движку, чтобы пропускать вакансию в автоотклик
        low = text.lower()
        remote = any(k in low for k in ("можно удалённо", "удалённая работа", "удалённо"))
        return {"description": text[:6000], "url": page,
                "work_formats": "удалённо" if remote else ""}

    # --- отклики -------------------------------------------------------
    def apply_queue(self, store, driver=None, limit=None, dry_run=False, log=print) -> dict:
        """Отклик со сопроводительным письмом на странице вакансии."""
        from selenium.webdriver.common.by import By

        own = driver is None
        if own:
            driver = self._own_driver()
        stats = {"applied": 0, "skipped": 0, "errors": 0}
        rows = store.queue(limit=limit, only_auto=True, board=self.name)
        try:
            for row in rows:
                vid = row["id"]
                letter = ""
                if row.get("letter_path"):
                    from ..letters import read_letter

                    letter = read_letter(row["letter_path"])
                if dry_run:
                    log(f"  (dry-run) отклик {self.title}: {row['title'][:60]}")
                    stats["skipped"] += 1
                    continue
                try:
                    driver.get(row["url"])
                    time.sleep(4)
                    result = self._respond(driver, letter, log=log, url=row["url"])
                    if not result:
                        store.set_status(vid, "needs_manual", f"{self.title}: кнопка отклика не найдена")
                        stats["skipped"] += 1
                        continue
                    note = (f"{self.title}: отклик + письмо" if result == "letter"
                            else f"{self.title}: отклик без письма")
                    store.set_status(vid, "applied", note)
                    stats["applied"] += 1
                    log(f"  ✓ отклик {self.title}: {row['title'][:60]} — {note}")
                    time.sleep(random_delay())
                except Exception as exc:  # noqa: BLE001
                    store.set_status(vid, "error", str(exc)[:200])
                    stats["errors"] += 1
                    log(f"  ! {self.title}: отклик не ушёл ({exc})")
        finally:
            if own:
                try:
                    driver.quit()
                except Exception:  # noqa: BLE001
                    pass
        return stats

    def _respond(self, driver, letter: str, log=print, url: str = "") -> str:
        """Отклик на Хабре.

        Устроено так: «Откликнуться» отправляет отклик сразу (резюме без письма),
        а сопроводительное добавляется потом — «Редактировать» открывает форму с
        textarea[name=body], сохраняется кнопкой «Сохранить».

        Возвращает: "letter" (отклик + письмо), "noletter" (отклик ушёл, письмо не
        приложилось) или "" (откликнуться не удалось).
        """
        from selenium.webdriver.common.by import By

        def _btn(words: tuple[str, ...]):
            for b in driver.find_elements(By.CSS_SELECTOR, "button"):
                try:
                    text = (b.text or "").strip().lower()
                except Exception:  # noqa: BLE001
                    continue
                if any(w in text for w in words) and b.is_displayed() and b.is_enabled():
                    return b
            return None

        def _click(el) -> bool:
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'})", el)
                time.sleep(0.5)
                driver.execute_script("arguments[0].click()", el)
                return True
            except Exception:  # noqa: BLE001
                return False

        already = "посмотреть отклик" in (driver.find_element(By.TAG_NAME, "body").text or "").lower()
        if not already:
            btn = _btn(("откликнуться",))
            if btn is None:
                return ""
            if not _click(btn):
                return ""
            time.sleep(6)
            # после отправки перезагружаем страницу — так блок отклика точно отрисуется
            if url:
                try:
                    driver.get(url)
                    time.sleep(6)
                except Exception:  # noqa: BLE001
                    pass
        else:
            log("    · отклик уже был отправлен")

        if not letter:
            return "noletter"

        # Письмо прикладываем через редактирование отклика.
        def _find_field():
            for sel in ("textarea[name=body]", "textarea", "div[contenteditable=true]"):
                for el in driver.find_elements(By.CSS_SELECTOR, sel):
                    try:
                        if el.is_displayed() and el.is_enabled():
                            return el
                    except Exception:  # noqa: BLE001
                        continue
            return None

        edit = _btn(("редактировать", "дополнить отклик"))
        if edit is None:
            log("    ! кнопка «Редактировать» не найдена — письмо не приложено")
            return "noletter"
        field = None
        for attempt in range(2):
            _click(edit)
            for _ in range(20):  # форма появляется не мгновенно
                time.sleep(1)
                field = _find_field()
                if field is not None:
                    break
            if field is not None:
                break
            # не открылось — пробуем нажать «Редактировать» ещё раз
            edit = _btn(("редактировать", "дополнить отклик"))
            if edit is None:
                break
        if field is None:
            log("    ! поле письма не появилось — письмо не приложено")
            return "noletter"
        try:
            if (field.tag_name or "").lower() == "textarea":
                driver.execute_script(
                    """
                    const el = arguments[0], v = arguments[1];
                    const proto = window.HTMLTextAreaElement.prototype;
                    Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, v);
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    """, field, letter)
            else:  # contenteditable-редактор
                driver.execute_script(
                    """
                    const el = arguments[0], v = arguments[1];
                    el.focus();
                    el.innerText = v;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    """, field, letter)
        except Exception as exc:  # noqa: BLE001
            log(f"    ! письмо не вписалось: {exc}")
            return "noletter"
        time.sleep(1)
        save = _btn(("сохранить", "отправить"))
        if save is None:
            log("    ! кнопка «Сохранить» не найдена — письмо не приложено")
            return "noletter"
        if not _click(save):
            return "noletter"
        time.sleep(5)
        return "letter"

    def _own_driver(self):
        from ..apply_selenium import build_driver, set_log

        set_log(print)
        return build_driver(self.cfg)

    def supports_replies(self) -> bool:
        return False


def random_delay() -> float:
    import random

    return random.uniform(40, 110)
