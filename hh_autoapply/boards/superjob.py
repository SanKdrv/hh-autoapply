"""SuperJob — через браузер (заявку на API не оформляем).

Свой профиль браузера: data/browser-profile-superjob. Вход — один раз:
`main.py login --board superjob`.

Вёрстка (проверена на superjob.ru/vacancy/search):
  элемент результата .f-test-search-result-item,
  ссылка вакансии   /vakansii/<slug>-<id>.html, компания — /clients/<slug>.html
"""
from __future__ import annotations

import random
import re
import time
import urllib.parse

from . import register
from .base import Board

PREFIX = "sj_"
VACANCY_RE = re.compile(r"/vakansii/[^/]*?-(\d+)\.html")


@register
class Superjob(Board):
    name = "superjob"
    title = "SuperJob"
    needs_browser = True
    login_url = "https://www.superjob.ru/auth/login/"

    # --- служебное -----------------------------------------------------
    def _url(self, path: str) -> str:
        host = self.cfg.get("host") or "www.superjob.ru"
        return f"https://{host}/{path.lstrip('/')}"

    # --- авторизация ---------------------------------------------------
    def is_logged_in(self, driver, navigate: bool = False) -> bool:
        """Признак входа.

        Надёжный сигнал — личные ссылки соискателя (/user/...). Их нет ни на
        странице входа, ни в форме с кодом из СМС, поэтому ложных срабатываний
        нет (раньше проверка по тексту «Войти» давала их, и окно закрывалось,
        пока пользователь ещё вводил код).
        """
        from selenium.webdriver.common.by import By

        if navigate:
            try:
                driver.get(self._url(""))
                time.sleep(3)
            except Exception:  # noqa: BLE001
                return False
        url = (driver.current_url or "").lower()
        if "/auth/" in url or "/login" in url or "/sms" in url:
            return False
        try:
            for sel in ('a[href*="/user/"]', 'a[href*="/applicant/"]'):
                if driver.find_elements(By.CSS_SELECTOR, sel):
                    return True
        except Exception:  # noqa: BLE001
            return False
        return False

    def ensure_login(self, driver, interactive=None, log=print) -> None:
        try:
            driver.get(self._url(""))
            time.sleep(3)
        except Exception:  # noqa: BLE001
            return
        if self.is_logged_in(driver):
            return
        log(f"  ! {self.title}: нет входа — отклики не будут отправляться. "
            f"Войди: main.py login --board superjob")

    # --- поиск ---------------------------------------------------------
    def search(self, log=print, driver=None):
        from selenium.webdriver.common.by import By

        own = driver is None
        if own:
            driver = self._own_driver()
        pages = int(self.cfg.get("search_pages", 3) or 3)
        found: dict[str, __import__("hh_autoapply.models", fromlist=["Vacancy"]).Vacancy] = {}
        try:
            for profile in self.cfg.get("searches") or []:
                text = profile.get("text") or profile.get("keyword") or ""
                if not text:
                    continue
                for page in range(pages):
                    # ищем только удалёнку: у SuperJob это отдельный раздел «на дому»,
                    # он понимает ключевые слова (для соискателя-удалёнщика офисные
                    # вакансии бесполезны, и автоотклик их всё равно не берёт)
                    params = {"keywords": text, "page": page}
                    url = self._url("vakansii/na-domu.html") + "?" + urllib.parse.urlencode(params)
                    try:
                        driver.get(url)
                        time.sleep(5)
                    except Exception as exc:  # noqa: BLE001
                        log(f"  ! {self.title}: «{text}» стр.{page} не открылась: {exc}")
                        break
                    items = driver.find_elements(By.CSS_SELECTOR, ".f-test-search-result-item")
                    log(f"  + {self.title} «{text}»: стр. {page}, {len(items)} вакансий")
                    for item in items:
                        vac = self._item_to_vacancy(item, profile)
                        if vac is not None:
                            found[vac.id] = vac
                    if not items:
                        break
                    time.sleep(random.uniform(1, 3))
        finally:
            if own:
                try:
                    driver.quit()
                except Exception:  # noqa: BLE001
                    pass
        return list(found.values())

    def _item_to_vacancy(self, item, profile: dict):
        from selenium.webdriver.common.by import By

        from ..models import Vacancy
        from ..util import parse_salary_full

        href = title = ""
        for a in item.find_elements(By.CSS_SELECTOR, 'a[href*="/vakansii/"]'):
            try:
                text = (a.text or "").strip()
                link = a.get_attribute("href") or ""
            except Exception:  # noqa: BLE001
                continue
            if text and VACANCY_RE.search(link):
                href, title = link, text
                break
        if not href:
            return None
        match = VACANCY_RE.search(href)
        company = ""
        for a in item.find_elements(By.CSS_SELECTOR, 'a[href*="/clients/"]'):
            try:
                if (a.text or "").strip():
                    company = (a.text or "").strip()
                    break
            except Exception:  # noqa: BLE001
                continue
        blob = ""
        try:
            blob = (item.text or "").replace("\n", " · ")
        except Exception:  # noqa: BLE001
            pass
        salary = ""
        money = re.search(r"(\d[\d\s ]{4,})\s*(?:₽|руб)", blob)
        if money:
            salary = money.group(1).strip() + " руб"
        vac = Vacancy(
            id=f"{PREFIX}{match.group(1)}",
            board=self.name,
            title=title,
            company=company,
            url=href,
            salary=salary,
            work_format="удалённо",  # мы ищем в разделе удалённой работы
            area=(profile.get("area") or ""),
            source="superjob_html",
            description=blob[:3000],
            funnel=(profile.get("funnel") or profile.get("text") or "")[:40],
        )
        if salary:
            vac.salary_from, vac.salary_to, vac.salary_kind = parse_salary_full(salary)
        return vac

    def fetch_detail(self, vid: str, use_cache: bool = True, url: str = "") -> dict:
        """Описание вакансии. URL берём из выдачи (там региональный поддомен и slug).

        Без браузера SuperJob часто отдаёт заглушку — тогда возвращаем пусто,
        скоринг обойдётся данными карточки.
        """
        if not url:
            return {}
        try:
            from ..util import http_get

            html = http_get(url, timeout=5) or ""
        except Exception:  # noqa: BLE001
            return {}
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text)
        low = text.lower()
        if "доступ ограничен" in low or "подтвердите, что вы не робот" in low or len(text) < 400:
            return {}
        return {"description": text[:6000], "url": url}

    # --- отклики -------------------------------------------------------
    def apply_queue(self, store, driver=None, limit=None, dry_run=False, log=print) -> dict:
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
                    time.sleep(7)
                    result = self._respond(driver, letter, log=log, url=row["url"])
                    if result == "external":
                        store.set_status(vid, "needs_manual",
                                         f"{self.title}: отклик только на сайте компании")
                        stats["skipped"] += 1
                        continue
                    if not result:
                        store.set_status(vid, "needs_manual", f"{self.title}: кнопка отклика не найдена")
                        stats["skipped"] += 1
                        continue
                    note = (f"{self.title}: отклик + письмо" if result == "letter"
                            else f"{self.title}: отклик без письма")
                    store.set_status(vid, "applied", note)
                    stats["applied"] += 1
                    log(f"  ✓ отклик {self.title}: {row['title'][:60]} — {note}")
                    time.sleep(random.uniform(45, 120))
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
        """Отклик на SuperJob.

        Кнопка «Откликнуться» отправляет отклик сразу (резюме уже приложено).
        Возвращает: "letter" (отклик + письмо), "noletter" (отклик без письма),
        "external" (компания принимает отклики только на своём сайте) или "" (кнопки нет).
        """
        from selenium.webdriver.common.by import By

        def _text() -> str:
            try:
                return (driver.find_element(By.TAG_NAME, "body").text or "").lower()
            except Exception:  # noqa: BLE001
                return ""

        already = "отклик отправлен" in _text()
        if not already:
            # На странице есть карусель похожих вакансий с такими же кнопками —
            # берём самую верхнюю (это основная вакансия).
            candidates = []
            for b in driver.find_elements(By.CSS_SELECTOR, ".f-test-vacancy-response-button"):
                try:
                    if (b.text or "").strip() == "Откликнуться" and b.is_displayed():
                        candidates.append((b.rect.get("y", 99999), b))
                except Exception:  # noqa: BLE001
                    continue
            if not candidates:
                return ""
            candidates.sort(key=lambda x: x[0])
            btn = candidates[0][1]
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'})", btn)
                time.sleep(1)
                driver.execute_script("arguments[0].click()", btn)
            except Exception as exc:  # noqa: BLE001
                log(f"    ! клик по «Откликнуться» не удался: {exc}")
                return ""
            time.sleep(7)
            cur = driver.current_url or ""
            if "superjob.ru" not in cur:
                log("    · компания принимает отклики на своём сайте — пропускаю")
                if url:
                    try:
                        driver.get(url)
                        time.sleep(4)
                    except Exception:  # noqa: BLE001
                        pass
                return "external"
        else:
            log("    · отклик уже был отправлен")

        if not letter:
            return "noletter"

        # Письмо: если открылась форма с полем — пишем туда, иначе пробуем чат.
        for el in driver.find_elements(By.CSSSelector if False else "css selector",
                                       "textarea, [contenteditable=true]"):
            try:
                if el.is_displayed():
                    driver.execute_script("arguments[0].focus()", el)
                    el.send_keys(letter)
                    time.sleep(1.5)
                    for b in driver.find_elements(By.CSSSelector if False else "css selector", "button"):
                        t = (b.text or "").strip().lower()
                        if t in ("отправить", "откликнуться", "send") and b.is_displayed():
                            driver.execute_script("arguments[0].click()", b)
                            time.sleep(4)
                            return "letter"
                    return "noletter"
            except Exception:  # noqa: BLE001
                continue
        return self._letter_via_chat(driver, letter, log=log)

    def _letter_via_chat(self, driver, letter: str, log=print) -> str:
        """Досылаем сопроводительное в чат по вакансии (если чат доступен)."""
        from selenium.webdriver.common.by import By

        chat = None
        for b in driver.find_elements(By.CSSSelector if False else "css selector", "button, a"):
            try:
                if (b.text or "").strip().lower() in ("чат", "перейти в чат") and b.is_displayed():
                    chat = b
                    break
            except Exception:  # noqa: BLE001
                continue
        if chat is None:
            return "noletter"
        try:
            driver.execute_script("arguments[0].click()", chat)
            time.sleep(6)
            for el in driver.find_elements(By.CssSelector if False else "css selector",
                                           "textarea, [contenteditable=true]"):
                if el.is_displayed():
                    el.click()
                    el.send_keys(letter)
                    time.sleep(1.5)
                    el.send_keys("\ue007")  # Enter
                    time.sleep(3)
                    return "letter"
        except Exception as exc:  # noqa: BLE001
            log(f"    ! письмо в чат не ушло: {str(exc)[:70]}")
        return "noletter"

    def _own_driver(self):
        from ..apply_selenium import build_driver, set_log

        set_log(print)
        return build_driver(self.cfg)
