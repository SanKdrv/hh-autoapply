"""Hirify (hirify.me) — поиск вакансий через Selenium, на бесплатном тарифе.

Зачем: агрегатор 700+ источников с нормальными фильтрами, а API у них только
за Plus. Здесь берём обычную выдачу бесплатно:
  • только удалёнка (формат в тексте карточки: remote / hybrid),
  • не junior/стажёр,
  • свежие (минуты/часы; дни тоже берём, если не старше MAX_AGE_DAYS),
  • фильтр ИИ как у Telegram-каналов (легальность + релевантность),
  • одна тихая сводка с кнопками на вакансии.
Отклик — вручную на сайте/через контакт: у бесплатного тарифа нет reveal.
"""
from __future__ import annotations

import re
import time

PAGES = (
    "https://hirify.me/jobs-in-russia",
    "https://hirify.me/backend-dev-jobs",
    "https://hirify.me/software-remote-jobs",
)

MAX_AGE_DAYS = 7


def _age_ok(text: str) -> bool:
    low = text.lower()
    if "недел" in low or "месяц" in low:
        return False
    num = re.search(r"(\d+)\s*дн", low)
    if num:
        return int(num.group(1)) <= MAX_AGE_DAYS
    return True  # минуты/часы


def _card_text(a) -> str:
    try:
        return " ".join((a.text or "").split())
    except Exception:  # noqa: BLE001
        return ""


def run(cfg: dict, log=print, dry_run: bool = False, limit: int = 10) -> dict:
    from .apply_selenium import build_driver, set_log
    from .channels import _send_digest, ai_check, draft_reply
    from .config import board_config, load_profile

    profile = load_profile()
    bcfg = board_config(cfg, "hh")
    bcfg = dict(bcfg)
    bcfg["browser"] = dict(bcfg["browser"])
    bcfg["browser"]["profile_dir"] = bcfg["browser"]["profile_dir"] + "-hirify"
    set_log(lambda m: None)

    stats = {"pages": 0, "cards": 0, "filtered": 0, "ai_rejected": 0,
             "relevant": 0, "sent": 0}
    cards: list[dict] = []
    seen: set[str] = set()
    driver = build_driver(bcfg)
    try:
        for url in PAGES:
            try:
                driver.get(url)
                time.sleep(9)
            except Exception as exc:  # noqa: BLE001
                log(f"  ! Hirify: {url} — {str(exc)[:60]}")
                continue
            stats["pages"] += 1
            # Сначала собираем кандидатов по карточкам: ссылки «протухают» при переходе.
            candidates: list[tuple[str, str]] = []
            for a in driver.find_elements("css selector", 'a[href*="/jobs/"]'):
                text = _card_text(a)
                try:
                    href = (a.get_attribute("href") or "").split("?")[0]
                except Exception:  # noqa: BLE001
                    continue
                if not text or not href or href in seen:
                    continue
                seen.add(href)
                stats["cards"] += 1
                low = text.lower()
                if "remote" not in low:
                    stats["filtered"] += 1
                    continue
                if any(bad in low for bad in ("junior", "trainee", "стажёр", "стажер")):
                    stats["filtered"] += 1
                    continue
                if not _age_ok(text):
                    stats["filtered"] += 1
                    continue
                candidates.append((href, text))

            # Потом открываем вакансию: в карточке нет зарплаты и описания.
            for href, text in candidates:
                try:
                    driver.get(href)
                    time.sleep(3)
                    full = " ".join(
                        (driver.find_element("tag name", "body").text or "").split())
                except Exception:  # noqa: BLE001
                    full = text
                verdict = ai_check(cfg, full[:4000] or text)
                if verdict is None:
                    # модель не ответила — не теряем вакансию из-за этого
                    log(f"  ? Hirify: ИИ не ответил, беру по грубому фильтру: {text[:60]!r}")
                    verdict = {"legal": True, "relevant": True}
                if not verdict.get("legal", True) or not verdict.get("relevant"):
                    stats["ai_rejected"] += 1
                    continue
                stats["relevant"] += 1
                draft = "" if dry_run else draft_reply(cfg, full[:2500], profile)
                cards.append({
                    "header": f"Hirify · {verdict.get('level', '?')}"
                              + (f" · {verdict['salary']}" if verdict.get("salary") else ""),
                    "body": full[:700] + f"\n{href}",
                    "short": text[:26],
                    "url": href,
                    "hr": "",
                    "draft": draft,
                })
                log(f"  ✓ Hirify: {text[:80]!r}")
                if stats["relevant"] >= limit:
                    break
            if stats["relevant"] >= limit:
                break
    finally:
        driver.quit()

    if not dry_run and cards:
        stats["sent"] = _send_digest(cfg, cards, log=log)
    elif dry_run:
        for card in cards:
            log(f"   (dry) {card['header']} | {card['url']}")
    return stats
