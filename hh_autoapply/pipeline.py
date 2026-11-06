"""Полный цикл: поиск → письма → отчёт → отклики → чтение ответов → уведомления.

Именно эту функцию вызывает планировщик (launchd/cron) через `main.py run`.
"""
from __future__ import annotations

import json
import os
import random
import time

from .ai import api_available, generate_via_api, signature_block, write_prompt
from .config import ensure_dirs, load_config, load_profile
from .filters import hard_filter
from .keywords import extract_keyword
from .letters import generate as generate_template
from .letters import write_letter
from .models import Vacancy
from .notify import Notifier
from .platform_support import SleepGuard, on_ac_power, platform_name
from .scoring import analyze, decide, detect_stack
from .sources import fetch_detail, search_all
from .store import Store
from .truthguard import claims_unknown, strip_unknown_claims
from .util import (
    digest,
    is_remote,
    net_of,
    now_iso,
    parse_salary_full,
    salary_net_estimate,
    salary_upper_bound,
)


def _board_obj(cfg: dict, board: str | None = None):
    """Адаптер биржи. Без board — None, и работает прежний hh-путь."""
    if not board:
        return None
    from .boards import get_board
    from .config import board_config

    name = _board_name(cfg, board)
    return get_board(name)(board_config(cfg, name))


def search_vacancies(cfg: dict, store: Store, log=print, max_detail: int = 140,
                     driver=None, board=None) -> dict:
    """Находит вакансии, тянет описания, фильтрует, скорит."""
    board_obj = _board_obj(cfg, board)
    if board_obj is not None:
        vacancies = board_obj.search(log=log, driver=driver)
        for vac in vacancies:
            if not getattr(vac, "board", ""):
                vac.board = board_obj.name
    else:
        vacancies = search_all(cfg, log=log, driver=driver)
    log(f"найдено уникальных вакансий: {len(vacancies)}")
    if max_detail and len(vacancies) > max_detail:
        vacancies = vacancies[:max_detail]

    stats = {"found": len(vacancies), "approved": 0, "skipped": 0, "new": 0}

    for i, vac in enumerate(vacancies, 1):
        existed = store.get(vac.id) is not None
        try:
            detail = (board_obj.fetch_detail(vac.id, url=vac.url) if board_obj is not None
                      else fetch_detail(cfg, vac.id))
        except Exception as exc:  # noqa: BLE001
            log(f"  ! {vac.id}: описание недоступно ({exc})")
            detail = {}
        time.sleep(random.uniform(0.2, 0.8))

        if detail.get("description"):
            vac.description = detail["description"]
        if detail.get("title"):
            vac.title = detail["title"]
        if detail.get("work_formats"):
            vac.work_format = detail["work_formats"]
        if not vac.company and detail.get("hiringOrganization"):
            vac.company = detail["hiringOrganization"]
        if detail.get("response_url"):
            vac.response_url = detail["response_url"]
        if detail.get("employer_id"):
            vac.employer_id = detail["employer_id"]

        vac.salary_from, vac.salary_to, vac.salary_kind = parse_salary_full(
            detail.get("salary_text") or vac.salary
        )
        salary_cfg = cfg.get("salary") or {}
        ndfl = float(salary_cfg.get("ndfl", 0.13))
        vac.salary_net = salary_net_estimate(vac.salary_from, vac.salary_to, vac.salary_kind, ndfl)
        vac.eligibility = "auto" if is_remote(vac.work_format) else "manual"
        vac.keyword = extract_keyword(vac.description)

        ok, reason = hard_filter(vac, cfg)
        if not ok:
            vac.decision, vac.reason = "skip", reason
            stats["skipped"] += 1
            store.upsert(vac)
            continue

        # Удалёнка: требуем минимальную зарплату «на руки».
        # Для AI/вайбкодинга порог не применяем: там зарплату часто не указывают,
        # а вакансии интересны сами по себе (exempt_funnels в config.salary).
        exempt = {str(x).strip().upper() for x in (salary_cfg.get("exempt_funnels") or [])}
        salary_exempt = str(vac.funnel or "").strip().upper() in exempt
        if not salary_exempt and vac.eligibility == "auto":
            min_net = int(salary_cfg.get("remote_min_net", 0) or 0)
            if min_net:
                floor_v = vac.salary_from or vac.salary_to
                ceil_v = vac.salary_to or vac.salary_from
                mode = str(salary_cfg.get("mode", "floor")).lower()
                check_v = ceil_v if mode == "ceiling" else floor_v
                net_check = net_of(check_v, vac.salary_kind, ndfl)
                if net_check is None:
                    if str(salary_cfg.get("unknown", "keep")).lower() == "skip":
                        vac.decision = "skip"
                        vac.reason = f"удалёнка, зарплата не указана (нужно от {min_net} на руки)"
                        stats["skipped"] += 1
                        store.upsert(vac)
                        continue
                elif net_check < min_net:
                    net_floor = net_of(floor_v, vac.salary_kind, ndfl)
                    net_ceil = net_of(ceil_v, vac.salary_kind, ndfl)
                    span = f"{net_floor}–{net_ceil}" if net_ceil and net_floor != net_ceil else str(net_check)
                    vac.decision = "skip"
                    vac.reason = (
                        f"удалёнка, на руки {span} < {min_net} (режим {mode}, {vac.salary})"
                    )
                    stats["skipped"] += 1
                    store.upsert(vac)
                    continue
        elif not salary_exempt:
            # Офис/гибрид: имеет смысл только если зарплата окупает переезд.
            relocate = int(salary_cfg.get("relocate_min_gross", 0) or 0)
            top = salary_upper_bound(vac.salary_from, vac.salary_to)
            gross_top = top
            if top and vac.salary_kind == "net":
                gross_top = int(round(top / (1 - ndfl)))
            if relocate and gross_top is not None and gross_top < relocate:
                vac.decision = "skip"
                vac.reason = (
                    f"офис и зарплата до {gross_top} gross < {relocate} (переезд не окупается)"
                )
                stats["skipped"] += 1
                store.upsert(vac)
                continue

        score, hits = analyze(vac, cfg)
        vac.score, vac.hits = score, hits
        vac.stack = detect_stack(vac, cfg)
        vac.decision, vac.reason = decide(vac, cfg, score)
        store.upsert(vac)
        if vac.decision == "approve":
            stats["approved"] += 1
            if not existed:
                stats["new"] += 1

        if i % 10 == 0:
            log(f"  обработано {i}/{len(vacancies)}")

    store.log_run("search", str(stats))
    return stats


def _ensure_keyword_first(path: str, keyword: str) -> int:
    """Гарантирует, что кодовое слово стоит первым в письме. 1 — если поправили."""
    try:
        with open(path, encoding="utf-8") as fh:
            content = fh.read()
    except OSError:
        return 0
    header, sep, rest = content.partition("\n\n")
    if not sep:
        return 0
    body = rest.lstrip()
    if body.lower().startswith(keyword.lower()):
        return 0
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"{header}\n\n{keyword}\n\n{body}")
    return 1


def generate_letters(cfg: dict, store: Store, profile: dict, log=print,
                     use_ai: bool = True, force: bool = False,
                     only: list[str] | None = None, limit: int | None = None) -> dict:
    """Генерирует письма: через LLM, если есть ключ, иначе шаблоном."""
    if force:
        rows = store.all("decision='approve'")
    else:
        rows = store.all("decision='approve' AND (letter_path IS NULL OR letter_path='')", ())
        # вакансии с кодовым словом проверяем всегда: письмо могло быть готово раньше
        tagged = store.all("decision='approve' AND keyword IS NOT NULL AND keyword != ''")
        known = {r["id"] for r in rows}
        rows = rows + [r for r in tagged if r["id"] not in known]
    if only:
        wanted = set(only)
        rows = [r for r in rows if r["id"] in wanted]
    if limit:
        rows = rows[:limit]

    stats = {"total": len(rows), "ai": 0, "template": 0, "prompts": 0, "errors": 0,
             "existing": 0, "keyword_patched": 0}
    if not rows:
        return stats

    ai_ready = use_ai and api_available(cfg)
    if use_ai and not ai_ready:
        log("ключ LLM не задан — письма будут шаблонными, промпты сохранены в out/prompts/")

    sig = signature_block(profile, cfg) if ai_ready else ""

    for row in rows:
        vac = Vacancy.from_row(row)
        vac.hits = {}
        # письмо уже на диске (например, сгенерировано LLM вручную) — просто привязываем
        existing = os.path.join(cfg["paths"]["letters_dir"], f"{vac.id}.txt")
        if os.path.exists(existing) and not force:
            if vac.keyword:
                stats["keyword_patched"] += _ensure_keyword_first(existing, vac.keyword)
            store.set_letter(vac.id, existing)
            stats["existing"] += 1
            continue
        try:
            if ai_ready:
                text = generate_via_api(cfg, row, profile, sig)
                stats["ai"] += 1
                # Страж фактов: вырезаем предложения о технологиях, которых нет
                # в профиле (иначе модель приписывает кандидату чужой опыт).
                text, dropped = strip_unknown_claims(text, profile, row)
                if dropped:
                    log(f"  · {vac.id}: убраны незнакомые технологии: {', '.join(dropped)}")
                    if len(text) < 500:
                        text = generate_template(vac, profile, cfg,
                                                 stack=row.get("stack") or None)
                        stats["template"] += 1
                        log(f"  · {vac.id}: письмо заменено шаблоном (после чистки пусто)")
            else:
                if use_ai:
                    write_prompt(cfg, row, profile)
                    stats["prompts"] += 1
                text = generate_template(vac, profile, cfg, stack=row.get("stack") or None)
                stats["template"] += 1
            path = write_letter(cfg, vac, text)
            store.set_letter(vac.id, path)
        except Exception as exc:  # noqa: BLE001
            log(f"  ! {vac.id}: {exc}")
            stats["errors"] += 1

    store.log_run("letters", str(stats))
    return stats


def _company_lore(store: Store, vac_id, exclude_chat: str = "") -> str:
    """Что мы уже говорили этой компании (в других её чатах) — для консистентности."""
    vac = store.get(vac_id) if vac_id else None
    company = ((vac or {}).get("company") or "").strip()
    if not company:
        return ""
    ids = {str(r["id"]) for r in store.all("company = ?", (company,))}
    lines: list[str] = []
    for m in store.all_messages(None):
        if str(m.get("vacancy_id") or "") not in ids:
            continue
        if exclude_chat and str(m.get("negotiation_id") or "") == str(exclude_chat):
            continue
        text = (m.get("text") or "").strip()
        if not text:
            continue
        who = "Мы" if m.get("direction") == "out" else "Компания"
        lines.append(f"{who}: {text[:400]}")
    return "\n".join(lines[-20:])


def prepare_replies(cfg: dict, store: Store, profile: dict, notifier=None,
                    log=print, limit: int = 5) -> dict:
    """Готовит черновики ответов на последние входящие сообщения.

    Отправка не выполняется: черновик уходит в Telegram с кнопками.
    """
    if not (cfg.get("dialog") or {}).get("enabled", True):
        return {"created": 0, "skipped": 0}
    from . import replies

    by_chat: dict[str, list[dict]] = {}
    for msg in store.all_messages(None):
        by_chat.setdefault(str(msg.get("negotiation_id") or ""), []).append(msg)

    stats = {"created": 0, "skipped": 0, "errors": 0}
    for chat_id, msgs in by_chat.items():
        if not chat_id:
            continue
        msgs = msgs  # порядок = порядок добавления (rowid), ts у hh неоднородный
        last = msgs[-1]
        if last.get("direction") != "in" or not (last.get("text") or "").strip():
            stats["skipped"] += 1
            continue
        if _is_assistant_chat(msgs):
            # вопросы ИИ-помощника обрабатывает авто-ответчик (auto_answer_assistant)
            stats["skipped"] += 1
            continue
        if store.get_draft_for_message(last["id"]):
            stats["skipped"] += 1
            continue
        if stats["created"] >= limit:
            break

        vac = store.get(last["vacancy_id"]) if last.get("vacancy_id") else None

        from .util import company_excluded

        if vac and company_excluded(vac.get("company"), cfg):
            log(f"  ⏭ {(vac.get('company') or '')[:30]} — в списке «не связываться», ответ не готовлю")
            stats["skipped"] += 1
            continue

        # На отказы не отвечаем, а приглашения разбирает человек.
        state = store.get_state(f"neg_state:{last.get('vacancy_id')}", "").upper()
        if state in ("DISCARD", "DISCARDED", "REJECTION"):
            stats["skipped"] += 1
            continue
        if state in ("INVITATION", "INVITED", "INTERVIEW", "APPOINTMENT"):
            draft_id = digest(chat_id, last["id"])[:16]
            if store.add_draft(draft_id, chat_id, last.get("vacancy_id") or "",
                               last["id"], "", status="escalated"):
                stats["escalated"] = stats.get("escalated", 0) + 1
                log(f"  🎯 приглашение: {(vac or {}).get('title', chat_id)[:45]} — отвечаешь сам")
                if notifier is not None:
                    notifier.send_now(
                        "🎯 Приглашение на собеседование",
                        f"{(vac or {}).get('title') or 'вакансия'} — {(vac or {}).get('company') or ''}\n\n"
                        f"Ответ не отправляю: время собеседования ты выбираешь сам.\n"
                        f"{(vac or {}).get('url') or ''}",
                    )
            continue

        try:
            result = replies.draft_reply(cfg, profile, vac or {}, msgs,
                                         lore=_company_lore(store, last.get("vacancy_id"), chat_id))
        except Exception as exc:  # noqa: BLE001
            log(f"  ! черновик для чата {chat_id}: {exc}")
            stats["errors"] += 1
            continue

        draft_id = digest(chat_id, last["id"])[:16]
        vacancy_id = last.get("vacancy_id") or ""

        if result.get("escalate"):
            if store.add_draft(draft_id, chat_id, vacancy_id, last["id"], "", status="escalated"):
                stats["escalated"] = stats.get("escalated", 0) + 1
                log(f"  ⚠️ передаю тебе: {result.get('reason', '')[:70]}")
                if notifier is not None:
                    notifier.send_escalation(
                        store.get_draft(draft_id), vac, result.get("reason", ""), last.get("text", "")
                    )
            continue

        if store.add_draft(draft_id, chat_id, vacancy_id, last["id"], result["text"]):
            stats["created"] += 1
            log(f"  ✍️ черновик готов: {(vac or {}).get('title', chat_id)[:45]}")
            if notifier is not None:
                notifier.send_draft(store.get_draft(draft_id), vac)

    store.log_run("drafts", str(stats))
    return stats


def read_inbox(cfg: dict, store: Store, profile: dict, log=print, driver=None,
               board=None) -> dict:
    """Читает ответы работодателей."""
    board_obj = _board_obj(cfg, board)

    try:
        if board_obj is not None:
            return board_obj.read_replies(store, driver=driver, profile=profile, log=log)
        from .inbox import read_replies

        return read_replies(cfg, store, driver=driver, profile=profile)
    except (Exception, SystemExit) as exc:  # noqa: BLE001
        log(f"  ! чтение переписки не удалось: {exc}")
        return {"dialogues": 0, "messages": 0, "new_incoming": 0, "new": [], "error": str(exc)}


ASSISTANT_MARKERS = (
    "ии-помощник", "ии помощник", "ai-помощник", "ии-ассистент", "помощник hh",
)


def _is_assistant_chat(msgs: list[dict]) -> bool:
    return any(
        any(mk in (m.get("text") or "").lower() for mk in ASSISTANT_MARKERS)
        for m in msgs
    )


def _looks_like_question(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if "?" in t:
        return True
    low = t.lower()
    return any(low.startswith(p) for p in (
        "расскажите", "следующий вопрос", "как ", "что ", "используете",
        "насколько", "работали", "есть ли", "какой", "какие",
    ))


def auto_answer_assistant(cfg: dict, store: Store, profile: dict, log=print,
                          driver=None, limit: int = 3) -> dict:
    """Авто-ответы на вопросы ИИ-помощника hh.

    Анти-цикл: отвечаем только если последнее сообщение — входящее; только в
    чатах, где пишет именно ИИ-помощник; не больше «max_per_chat» наших ответов;
    на отказы/приглашения не отвечаем.
    """
    auto = (cfg.get("dialog") or {}).get("auto_reply") or {}
    if not auto.get("enabled", False):
        return {"sent": 0, "skipped": "disabled"}
    if store.is_halted():
        return {"sent": 0, "skipped": "halted"}
    max_per_chat = int(auto.get("max_per_chat", 4))

    by_chat: dict[str, list[dict]] = {}
    for m in store.all_messages(None):
        by_chat.setdefault(str(m.get("negotiation_id") or ""), []).append(m)

    from . import replies as replies_mod
    from .chat_send import send_message

    driver_local = driver
    created = False
    sent = 0
    for chat_id, msgs in by_chat.items():
        if not chat_id or sent >= limit:
            continue
        last = msgs[-1]
        if last.get("direction") != "in":
            continue  # последнее слово за нами — не зацикливаемся
        if not _is_assistant_chat(msgs):
            continue
        if not _looks_like_question(last.get("text") or ""):
            continue
        our = sum(1 for m in msgs if m.get("direction") == "out")
        if our >= max_per_chat:
            log(f"  · {chat_id}: уже {our} наших ответов — анти-цикл, стоп")
            continue
        state = store.get_state(f"neg_state:{last.get('vacancy_id')}", "").upper()
        if state in ("DISCARD", "DISCARDED", "REJECTION", "INVITATION", "INVITED",
                     "INTERVIEW", "APPOINTMENT"):
            continue
        vac = store.get(last.get("vacancy_id")) if last.get("vacancy_id") else None
        try:
            res = replies_mod.draft_reply(
                cfg, profile, vac or {}, msgs,
                lore=_company_lore(store, last.get("vacancy_id"), chat_id),
            )
        except Exception as exc:  # noqa: BLE001
            log(f"  ! авто-ответ {chat_id}: {exc}")
            continue
        if res.get("escalate"):
            log(f"  · {chat_id}: вопрос нестандартный — оставляю тебе")
            continue
        text = (res.get("text") or "").strip()
        if not text:
            continue
        bad = claims_unknown(text, profile, vac or {})
        if bad:
            log(f"  · {chat_id}: в ответе незнакомая технология ({', '.join(bad)}) — оставляю тебе")
            continue
        if driver_local is None:
            from .apply_selenium import build_driver, ensure_login

            driver_local = build_driver(cfg)
            ensure_login(driver_local, cfg)
            created = True
        try:
            ok = send_message(driver_local, chat_id, text)
        except Exception as exc:  # noqa: BLE001
            log(f"  ! отправить авто-ответ {chat_id}: {exc}")
            ok = False
        if ok:
            from .util import digest as _digest
            from .util import now_iso as _now

            store.add_message(_digest(chat_id, text, _now()), chat_id,
                              last.get("vacancy_id") or "", "out", text, _now(), "")
            sent += 1
            log(f"  ✓ авто-ответ помощнику ({chat_id}): {text[:60]}")
    if created and driver_local is not None:
        try:
            driver_local.quit()
        except Exception:  # noqa: BLE001
            pass
    store.log_run("autoanswer", f"sent={sent}")
    return {"sent": sent}


def apply_vacancies(cfg: dict, store: Store, limit: int | None, log=print,
                    dry_run: bool = False, driver=None, board=None) -> dict:
    from .apply_selenium import apply_queue, set_log

    set_log(log)  # логи откликов (и капчи) — в тот же лог, что и цикл
    board_obj = _board_obj(cfg, board)
    try:
        if board_obj is not None:
            return board_obj.apply_queue(store, driver=driver, limit=limit,
                                         dry_run=dry_run, log=log)
        return apply_queue(cfg, store, limit=limit, dry_run=dry_run, driver=driver)
    except (Exception, SystemExit) as exc:  # noqa: BLE001
        log(f"  ! автоотклик не выполнен: {exc}")
        return {"applied": 0, "skipped": 0, "errors": 1, "error": str(exc)}


REJECT_MARKERS = (
    "к сожалению", "не готовы пригласить", "вынуждены отказать",
    "не готовы рассмотреть", "приняли решение отказать", "отказываем вам",
    "не готовы продолжить", "к сожалению, нет",
)


def _is_rejection(store: Store, msg: dict) -> bool:
    """Отказ работодателя: не шлём его в Telegram по каждому сообщению."""
    state = store.get_state(f"neg_state:{msg.get('vacancy_id')}", "").upper()
    if state in ("DISCARD", "DISCARDED", "REJECTION"):
        return True
    low = (msg.get("text") or "").lower()
    return any(marker in low for marker in REJECT_MARKERS)


def _reply_lines(cfg: dict, store: Store, messages: list[dict]) -> list[str]:
    lines = []
    for msg in messages:
        if _is_rejection(store, msg):
            continue  # отказы в телегу не шлём — они учитываются в сводке
        vac = store.get(msg["vacancy_id"]) if msg.get("vacancy_id") else None
        title = (vac or {}).get("title") or "вакансия"
        company = (vac or {}).get("company") or ""
        url = (vac or {}).get("url") or ""
        lines.append(f"💬 {title} — {company}\n{msg['text'][:500]}\n{url}")
    return lines


def _pid_alive(pid: int) -> bool:
    """Жив ли процесс.

    ВАЖНО: на Windows os.kill(pid, 0) — это не проверка, а TerminateProcess,
    то есть проверка занятости убивала работающий цикл. Поэтому на Windows
    спрашиваем у системы состояние процесса через OpenProcess/GetExitCodeProcess.
    """
    if not pid:
        return False
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                return bool(ok) and code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _board_name(cfg: dict, board: str | None = None) -> str:
    return (board or cfg.get("board") or "hh").lower()


def _lock_path(cfg: dict, board: str | None = None) -> str:
    """Файл-блокировка. У каждой биржи своя, чтобы циклы шли параллельно."""
    name = _board_name(cfg, board)
    fname = "run.lock" if name == "hh" else f"run-{name}.lock"
    return os.path.join(os.path.dirname(cfg["paths"]["db"]), fname)


def acquire_lock(cfg: dict, board: str | None = None) -> bool:
    """Защита от наложения циклов: один запуск на биржу в момент времени.

    Цикл с паузами идёт ~2 часа, поэтому пересечение запусков реально.
    """
    path = _lock_path(cfg, board)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                pid = int((fh.read() or "0").strip())
        except (OSError, ValueError):
            pid = 0
        if pid and _pid_alive(pid):
            return False
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    except OSError:
        return True  # не смогли создать — не блокируем работу
    return True


def release_lock(cfg: dict, board: str | None = None) -> None:
    path = _lock_path(cfg, board)
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                pid = int((fh.read() or "0").strip())
            if pid == os.getpid():
                os.remove(path)
    except (OSError, ValueError):
        pass


def cycle_running(cfg: dict, board: str | None = None) -> bool:
    """Идёт ли сейчас другой цикл (для проверки из бота)."""
    path = _lock_path(cfg, board)
    if not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8") as fh:
            pid = int((fh.read() or "0").strip())
    except (OSError, ValueError):
        return False
    return bool(pid) and _pid_alive(pid)


def _ac_power() -> bool:
    """Питание от сети (кроссплатформенно)."""
    return on_ac_power()


def _start_sleep_guard(cfg: dict, log=print):
    """Не даёт системе уснуть, пока идёт цикл.

    macOS — caffeinate, Windows — SetThreadExecutionState, Linux — systemd-inhibit.
    Экран при этом гаснет как обычно.
    """
    schedule = cfg.get("schedule") or {}
    if not schedule.get("prevent_sleep", True):
        return None

    on_ac = on_ac_power()
    if not on_ac and not schedule.get("prevent_sleep_on_battery", True):
        log("питание от батареи — удержание сна выключено (prevent_sleep_on_battery=false)")
        return None

    guard = SleepGuard(log=log).start()
    source = "от сети" if on_ac else "от батареи (расходует заряд)"
    log(f"удержание сна включено ({platform_name()}), питание {source}; экран гаснет как обычно")
    return guard


def _stop_sleep_guard(guard, log=print) -> None:
    if guard:
        guard.stop()


BROWSER_DEAD_MARKERS = (
    "tab crashed", "invalid session id", "disconnected", "chrome instance exited",
    "session deleted", "browser has closed", "not connected to devtools",
    "no such window", "target window already closed",
)


def _browser_dead(exc: Exception) -> bool:
    """Похоже ли, что браузер умер (Edge обновился/упал рендерер)."""
    text = str(exc).lower()
    return any(marker in text for marker in BROWSER_DEAD_MARKERS)


def _restart_driver(cfg: dict, driver, board: str | None):
    """Перезапускает браузер биржи после падения."""
    try:
        driver.quit()
    except Exception:  # noqa: BLE001
        pass
    from .apply_selenium import build_driver, ensure_login
    from .config import board_config

    bcfg = board_config(cfg, board) if board else cfg
    fresh = build_driver(bcfg)
    ensure_login(fresh, bcfg)
    return fresh


def run_cycle(cfg: dict | None = None, profile: dict | None = None,
              apply_limit: int | None = None, do_apply: bool = True,
              use_ai: bool = True, max_detail: int = 140, log=print,
              board: str | None = None) -> dict:
    """Один полный прогон системы по одной бирже."""
    cfg = cfg or load_config()
    profile = profile or load_profile()
    ensure_dirs(cfg)

    board_obj = _board_obj(cfg, board)
    board_name = board_obj.name if board_obj is not None else _board_name(cfg, board)
    bcfg = board_obj.cfg if board_obj is not None else cfg

    if not acquire_lock(cfg, board):
        log(f"цикл по «{board_name}» уже выполняется в другом процессе — пропускаю запуск")
        return {"skipped": "cycle_running"}

    sleep_guard = _start_sleep_guard(cfg, log)
    store = Store(cfg["paths"]["db"])
    notifier = Notifier(cfg, log=log)

    # Один браузер на цикл: используется и для поиска (если включён), и для
    # откликов, и для чтения переписки — так меньше запусков и меньше капчи.
    driver = None
    need_browser = bool(bcfg.get("search_via_browser")) or bool(
        board_obj is not None and board_obj.needs_browser
    )
    if need_browser:
        try:
            from .apply_selenium import build_driver, ensure_login

            driver = build_driver(bcfg)
            if board_obj is not None:
                board_obj.ensure_login(driver)
            else:
                ensure_login(driver, bcfg)
        except Exception as exc:  # noqa: BLE001
            log(f"браузер для поиска не поднялся: {exc}")
            driver = None

    summary: dict = {"started": now_iso()}
    errors: list[str] = []
    html_path = "out/queue.html"

    log(f"=== цикл {board_name} {summary['started']} ===")
    if not notifier.enabled:
        log("Telegram не настроен: заполни TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env")

    halted = store.is_halted()
    if halted:
        log("⚠️ система на аварийном стопе: действия с браузером пропускаю")
        do_apply = False

    schedule = cfg.get("schedule") or {}
    on_ac = _ac_power()
    if do_apply and schedule.get("apply_only_on_ac", False) and not on_ac:
        log("питание от батареи — отклики пропускаю (apply_only_on_ac=true)")
        do_apply = False
        summary["apply_skipped"] = "on_battery"
    elif not on_ac:
        log("питание от батареи — цикл будет коротким, следи за зарядом")

    try:
        s = search_vacancies(cfg, store, log=log, max_detail=max_detail, driver=driver,
                             board=board)
        summary["search"] = s

        l = generate_letters(cfg, store, profile, log=log, use_ai=use_ai)
        summary["letters"] = l

        from .report import write_reports

        rows = store.queue(board=board_name)
        html_path, _ = write_reports(cfg, rows)
        summary["queue"] = len(rows)

        def _with_restart(fn, label: str):
            """Выполняет шаг; при падении браузера перезапускает его и повторяет."""
            nonlocal driver
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001
                if not _browser_dead(exc):
                    raise
                log(f"  ! браузер упал во время «{label}» — перезапускаю и повторяю")
                driver = _restart_driver(cfg, driver, board_name)
                return fn()

        if do_apply:
            if store.is_paused():
                log("отклики на паузе — отправку пропускаю")
                summary["apply"] = {"applied": 0, "skipped": 0, "paused": True}
            else:
                a = _with_restart(
                    lambda: apply_vacancies(cfg, store, apply_limit, log=log,
                                            driver=driver, board=board),
                    "отправка откликов",
                )
                summary["apply"] = a
                if board_name == "hh":
                    # «Отклики в один клик» у hh уходят без письма — сразу досылаем.
                    try:
                        from .followup import send_missing_letters

                        fu = _with_restart(
                            lambda: send_missing_letters(cfg, store, log=log, driver=driver,
                                                         delay_range=(3, 10)),
                            "досылка писем",
                        )
                        if fu.get("sent"):
                            log(f"досылка писем: приложено {fu['sent']}")
                    except Exception as exc:  # noqa: BLE001
                        log(f"  ! досылка писем не удалась: {exc}")

        inbox_stats = _with_restart(
            lambda: read_inbox(cfg, store, profile, log=log, driver=driver, board=board),
            "чтение переписки",
        ) if not halted else {"skipped": "halted"}
        summary["inbox"] = {k: v for k, v in inbox_stats.items() if k != "new"}

        # Уведомляем обо ВСЕХ непрочитанных ответах, а не только найденных в этом цикле.
        pending = store.unseen_incoming(50)
        if pending:
            lines = _reply_lines(cfg, store, pending)
            for line in lines:
                notifier.send_now(f"Ответ на {board_obj.title if board_obj else 'hh'}", line)
            skipped_rejects = len(pending) - len(lines)
            if skipped_rejects:
                log(f"отказов (в Telegram не шлём): {skipped_rejects}")
            store.mark_messages_notified([m["id"] for m in pending])
            summary["notified_replies"] = len(lines)

        # Черновики ответов работодателю — уходят в Telegram с кнопками.
        can_reply = board_obj is None or board_obj.supports_replies()
        if not halted and can_reply and (cfg.get("dialog") or {}).get("enabled", True):
            try:
                summary["drafts"] = prepare_replies(cfg, store, profile, notifier=notifier, log=log)
            except Exception as exc:  # noqa: BLE001
                log(f"  ! черновики не подготовлены: {exc}")
    except Exception as exc:  # noqa: BLE001
        errors.append(str(exc))
        log(f"!!! ошибка цикла: {exc}")
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:  # noqa: BLE001
                pass
        summary["errors"] = errors

        lines = [
            f"Найдено: {summary.get('search', {}).get('found', 0)}",
            f"В очереди: {summary.get('queue', 0)}",
            f"Писем сгенерировано: {summary.get('letters', {}).get('ai', 0)} (ИИ) / "
            f"{summary.get('letters', {}).get('template', 0)} (шаблон)",
        ]
        try:
            test_cnt = 0
            for r in store.queue(board=board_name):
                raw = r.get("hits")
                if not raw:
                    continue
                try:
                    hits = json.loads(raw) if isinstance(raw, str) else (raw or {})
                except (TypeError, ValueError):
                    hits = {}
                if hits.get("test"):
                    test_cnt += 1
        except Exception:  # noqa: BLE001
            test_cnt = 0
        if test_cnt:
            lines.append(f"С тестовым заданием в очереди: {test_cnt} (идут первыми)")
        if "apply" in summary:
            a = summary["apply"]
            lines.append(f"Откликов отправлено: {a.get('applied', 0)}, ошибок: {a.get('errors', 0)}")
        inc = summary.get("inbox", {}).get("new_incoming", 0)
        lines.append(f"Новых ответов получено: {inc}")
        if summary.get("notified_replies"):
            lines.append(f"Отправлено уведомлений об ответах: {summary['notified_replies']}")
        if summary.get("drafts", {}).get("created"):
            lines.append(f"Черновиков ответов готово: {summary['drafts']['created']}")
        if errors:
            lines.append("Ошибки: " + "; ".join(errors)[:500])
        lines.append(f"Отчёт: {html_path}")

        # Отчёт шлём один раз: по hh — всегда, по остальным биржам — только если
        # там реально были отклики или ошибки. Иначе Хабр/SuperJob каждый час
        # присылают по пустому отчёту, и в Telegram кажется, что он задвоился.
        board_label = board_name or "hh"
        applied_now = int((summary.get("apply") or {}).get("applied", 0) or 0)
        visible_errors = len(errors)
        if board_name in (None, "hh") or applied_now or visible_errors:
            notifier.send_now(f"hh-autoapply: цикл завершён ({board_label})", "\n".join(lines))
        else:
            log(f"отчёт в Telegram не шлю: на «{board_label}» без откликов и ошибок")

        # Сводка по воронкам: только по hh, чтобы не дублировать её на каждой бирже.
        try:
            from . import analytics

            bundle = analytics.build(store)
            summary["funnels"] = bundle["totals"]
            if board_name in (None, "hh") and (bundle["totals"]["sent"] or bundle["rows"]):
                notifier.send_pre("hh-autoapply: сводка по воронкам", analytics.render(bundle), silent=True)
        except Exception as exc:  # noqa: BLE001
            log(f"  ! сводка по воронкам не собралась: {exc}")

        store.log_run("cycle", str(summary.get("search", {})))
        store.close()
        _stop_sleep_guard(sleep_guard, log)
        release_lock(cfg, board)

    return summary
