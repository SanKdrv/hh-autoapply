"""hh.ru — адаптер поверх готовых модулей (sources / apply_selenium / inbox).

Ничего не переписываем: площадка hh остаётся ровно тем кодом, что и был, просто
теперь он вызывается через общий интерфейс Board.
"""
from __future__ import annotations

import time

from . import register
from .base import Board


@register
class Hh(Board):
    name = "hh"
    title = "hh.ru"
    needs_browser = True

    # --- поиск ---------------------------------------------------------
    def search(self, log=print, driver=None):
        from .. import sources

        vacancies = sources.search_all(self.cfg, log=log, driver=driver)
        # Персональные рекомендации hh живут на главной странице — это отдельный,
        # постоянно обновляемый поток вакансий под профиль.
        try:
            recs = self._recommendations(driver, log=log)
            if recs:
                log(f"  + рекомендации hh: {len(recs)} вакансий")
            vacancies += recs
        except Exception as exc:  # noqa: BLE001
            log(f"  ! рекомендации hh не прочитаны: {str(exc)[:80]}")
        return vacancies

    def _recommendations(self, driver, log=print):
        """Вакансии с главной страницы hh (блок персональных рекомендаций)."""
        import re

        from selenium.webdriver.common.by import By

        from ..models import Vacancy

        own = driver is None
        if own:
            from ..apply_selenium import build_driver, set_log

            set_log(log)
            driver = build_driver(self.cfg)
        try:
            driver.get(f"https://{self.cfg['host']}/")
            time.sleep(6)
            found: dict[str, Vacancy] = {}
            for a in driver.find_elements(By.CSS_SELECTOR, 'a[href*="from=applicant_recommended"]'):
                try:
                    href = a.get_attribute("href") or ""
                    title = (a.text or "").strip()
                except Exception:  # noqa: BLE001
                    continue
                match = re.search(r"/vacancy/(\d+)", href)
                if not match or not title:
                    continue
                vid = match.group(1)
                found[vid] = Vacancy(
                    id=vid,
                    board=self.name,
                    title=title,
                    url=f"https://{self.cfg['host']}/vacancy/{vid}",
                    source="hh_recommended",
                    funnel="Рекомендации",
                )
            return list(found.values())
        finally:
            if own:
                try:
                    driver.quit()
                except Exception:  # noqa: BLE001
                    pass

    def fetch_detail(self, vid: str, use_cache: bool = True, url: str = "") -> dict:
        from .. import sources

        return sources.fetch_detail(self.cfg, vid, use_cache=use_cache)

    # --- авторизация ---------------------------------------------------
    def is_logged_in(self, driver) -> bool:
        from ..apply_selenium import is_logged_in

        return is_logged_in(driver)

    def ensure_login(self, driver, interactive=None) -> None:
        from ..apply_selenium import ensure_login

        ensure_login(driver, self.cfg, interactive=interactive)

    # --- отклики -------------------------------------------------------
    def apply_queue(self, store, driver=None, limit=None, dry_run=False, log=print) -> dict:
        from ..apply_selenium import apply_queue, set_log

        set_log(log)
        return apply_queue(self.cfg, store, limit=limit, dry_run=dry_run, driver=driver)

    # --- переписка -----------------------------------------------------
    def read_replies(self, store, driver=None, profile=None, log=print) -> dict:
        from ..inbox import read_replies

        return read_replies(self.cfg, store, driver=driver, profile=profile)

    def supports_replies(self) -> bool:
        return True

    def send_message(self, chat_id: str, text: str, driver=None, log=print) -> bool:
        from ..chat_send import send_message

        own = driver is None
        if own:
            from ..apply_selenium import build_driver, ensure_login

            driver = build_driver(self.cfg)
            ensure_login(driver, self.cfg)
        try:
            return bool(send_message(driver, chat_id, text))
        finally:
            if own:
                try:
                    driver.quit()
                except Exception:  # noqa: BLE001
                    pass

    # --- поднятие резюме -----------------------------------------------
    def boost(self) -> bool:
        return True

    def raise_resume(self, driver, log=print) -> tuple[int, str, str]:
        """Поднять резюме в поиске. Возвращает (кликов, текст площадки, диагностика)."""
        from selenium.webdriver.common.by import By

        driver.get(f"https://{self.cfg['host']}/applicant/resumes")
        time.sleep(7)

        # Кнопка ручного поднятия. Пункты «Поднимать автоматически» — это сервис
        # hh (платная подписка), их не трогаем: иначе клик уходит не туда.
        clicked = 0
        seen: list[str] = []
        for btn in driver.find_elements(By.CSS_SELECTOR, '[data-qa*="update-button"], [data-qa*="raise"]'):
            try:
                txt = (btn.text or "").strip()
                seen.append(f"{(btn.get_attribute('data-qa') or '?')}«{txt[:18]}»")
                if "автомат" in txt.lower():
                    continue
                if not btn.is_enabled():
                    continue
                driver.execute_script("arguments[0].click()", btn)
                clicked += 1
                time.sleep(4)
            except Exception as exc:  # noqa: BLE001
                log(f"  ! поднять не удалось: {exc}")

        time.sleep(2)
        text = driver.find_element(By.TAG_NAME, "body").text
        outcome = ""
        for line in text.split("\n"):
            low = line.lower()
            if ("поднять" in low or "подня" in low) and ("можно" in low or "следующ" in low):
                outcome = line.strip()
                break

        # Диагностика: если кнопка не нашлась, в лог попадут все «поднять»-элементы,
        # чтобы было видно, как площадка назвала кнопку на самом деле.
        others: list[str] = []
        if not clicked:
            for el in driver.find_elements(By.XPATH, "//button|//a|//*[@role='button']"):
                try:
                    txt = (el.text or "").strip()
                except Exception:  # noqa: BLE001
                    continue
                if "однять" in txt and len(txt) < 40:
                    others.append(f"{(el.get_attribute('data-qa') or el.tag_name)}«{txt[:20]}»")
        details = f"по селектору: {', '.join(seen[:3])}"
        if others:
            details += f"; на странице: {', '.join(others[:6])}"
        return clicked, outcome, details
