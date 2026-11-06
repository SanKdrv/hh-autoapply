"""Досылка сопроводительных писем в переписку.

Нужна после инцидента 16.10.2026: отклики ушли без письма, потому что адрес
формы отклика hh обрабатывает как отклик «в один клик». Здесь письма
отправляются сообщением в чат — это допустимая практика.

Вакансии с приглашением (собеседование) по умолчанию пропускаются, чтобы
не мешать диалогу, который уже пошёл.
"""
from __future__ import annotations

import os
import random
import time

INVITED_STATES = {"INVITATION", "INVITED", "INTERVIEW", "APPOINTMENT"}


def _log(msg: str) -> None:
    print(f"[followup] {msg}", flush=True)


def _ensure_letter(cfg: dict, store, row: dict, log=_log) -> str:
    """Если письма нет — генерируем его, чтобы было что досылать."""
    try:
        from . import letters as letters_mod
        from .config import load_profile
        from .models import Vacancy

        vac = Vacancy.from_row(row)
        if not (getattr(vac, "keyword", "") or "").strip():
            from .keywords import extract_keyword

            vac.keyword = extract_keyword(row.get("description") or "")
            if vac.keyword:
                store.conn.execute(
                    "UPDATE vacancies SET keyword=? WHERE id=?", (vac.keyword, vac.id)
                )
                store.conn.commit()
        text = letters_mod.generate(vac, load_profile(), cfg, stack=row.get("stack"))
        path = letters_mod.write_letter(cfg, vac, text)
        store.set_letter(row["id"], path)
        log(f"  · письмо сгенерировано для {row['id']}")
        return path
    except Exception as exc:  # noqa: BLE001
        log(f"  ! не смог сгенерировать письмо для {row['id']}: {exc}")
        return ""


def collect_targets(cfg: dict, store, log=_log, driver=None) -> tuple[list[dict], list[dict]]:
    """Возвращает (кому досылать, кого пропускаем из-за приглашения)."""
    from .inbox import load_topics

    own_driver = driver is None
    if own_driver:
        from .apply_selenium import build_driver, ensure_login

        driver = build_driver(cfg)
        ensure_login(driver, cfg)
    try:
        topics = load_topics(driver, cfg)
    finally:
        if own_driver:
            driver.quit()

    by_vacancy: dict[str, dict] = {}
    for topic in topics:
        vid = str(topic.get("vacancyId") or "")
        if vid:
            by_vacancy[vid] = {
                "chat_id": str(topic.get("chatId") or ""),
                "state": (topic.get("lastState") or "").upper(),
            }

    # 'applied' с пометкой «без сопроводительного» И 'already' с «форма отклика
    # не найдена» — второе это те же отклики «в один клик», просто иначе помечены.
    rows = store.all(
        "(status IN ('applied','error','lettered') AND note LIKE '%без сопроводительного%') "
        "OR (status='already' AND note LIKE '%форма отклика не найдена%')"
    )
    targets, skipped = [], []
    for row in rows:
        from .util import company_excluded

        if company_excluded(row.get("company"), cfg):
            skipped.append({"title": row["title"], "reason": "в списке «не связываться»"})
            continue
        info = by_vacancy.get(str(row["id"]))
        if not info or not info["chat_id"]:
            skipped.append({"title": row["title"], "reason": "нет чата"})
            continue
        if info["state"] in INVITED_STATES:
            skipped.append({"title": row["title"], "reason": f"приглашение ({info['state']})"})
            continue
        path = row.get("letter_path") or ""
        if not (path and os.path.exists(path)):
            local = os.path.join(cfg["paths"]["letters_dir"], f"{row['id']}.txt")
            if os.path.exists(local):
                path = local
                store.set_letter(row["id"], local)
            else:
                path = _ensure_letter(cfg, store, row, log)
        if not path:
            skipped.append({"title": row["title"], "reason": "нет файла письма"})
            continue
        targets.append({**row, "chat_id": info["chat_id"], "state": info["state"]})
    return targets, skipped


def _letter_text(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        raw = fh.read()
    return raw.split("\n\n", 1)[1].strip() if "\n\n" in raw else raw.strip()


def send_missing_letters(cfg: dict, store, log=_log, dry_run: bool = False,
                         delay_range: tuple[int, int] = (25, 70), driver=None) -> dict:
    from .apply_selenium import build_driver, ensure_login
    from .chat_send import attach_cover_letter, send_message

    targets, skipped = collect_targets(cfg, store, log=log, driver=driver)

    log(f"к досылке: {len(targets)} | пропускаем: {len(skipped)}")
    for item in skipped:
        log(f"  · пропуск: {item['title'][:45]} — {item['reason']}")
    for item in targets:
        log(f"  → {item['title'][:50]} ({item['state'] or 'RESPONSE'})")

    if dry_run:
        log("режим проверки: ничего не отправлено")
        return {"targets": len(targets), "skipped": len(skipped), "sent": 0, "dry_run": True}

    if not targets:
        return {"targets": 0, "skipped": len(skipped), "sent": 0}

    own_driver = driver is None
    if own_driver:
        driver = build_driver(cfg)
    sent = 0
    try:
        ensure_login(driver, cfg)
        for i, item in enumerate(targets, 1):
            text = _letter_text(item["letter_path"])
            log(f"[{i}/{len(targets)}] {item['title'][:45]}")

            # Основной путь: приложить письмо к отклику («Add a cover letter»).
            result = "fail"
            try:
                result = attach_cover_letter(driver, cfg, item["chat_id"], text)
            except Exception as exc:  # noqa: BLE001
                log(f"    ! ошибка приложения письма: {exc}")
                result = "fail"

            if result == "ok":
                sent += 1
                # письмо приложилось → отклик реально существует: фиксируем как applied
                store.set_status(item["id"], "applied", "сопроводительное приложено к отклику")
                log("    ✓ письмо приложено к отклику")
            elif result == "already":
                store.conn.execute(
                    "UPDATE vacancies SET note=? WHERE id=?",
                    ("письмо уже есть у отклика — не дублирую", item["id"]),
                )
                store.conn.commit()
                log("    · письмо уже есть — не дублирую")
            else:
                # Запасной путь: сообщением в чат (если чат не отключён).
                ok = False
                try:
                    ok = send_message(driver, item["chat_id"], text)
                except Exception as exc:  # noqa: BLE001
                    log(f"    ! ошибка отправки в чат: {exc}")
                if ok:
                    sent += 1
                    store.conn.execute(
                        "UPDATE vacancies SET note=? WHERE id=?",
                        ("сопроводительное дослано в чат", item["id"]),
                    )
                    store.conn.commit()
                    log("    ✓ письмо отправлено в чат (запасной путь)")
                else:
                    log("    ? не удалось (чат, похоже, отключён работодателем)")

            if i < len(targets):
                pause = random.uniform(*delay_range)
                log(f"    … пауза {pause:.0f} c")
                time.sleep(pause)
    finally:
        if own_driver:
            driver.quit()

    store.log_run("followup", f"sent={sent}/{len(targets)}")
    log(f"итог: отправлено {sent} из {len(targets)}")
    return {"targets": len(targets), "skipped": len(skipped), "sent": sent}
