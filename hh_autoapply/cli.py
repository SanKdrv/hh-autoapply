"""CLI: search → letters → report → apply → inbox → run (для планировщика)."""
from __future__ import annotations

import argparse
import os
import sys

from .config import board_config, ensure_dirs, load_config, load_profile
from .env import load_dotenv
from .store import Store
from .util import now_iso


def _log(msg: str) -> None:
    print(msg, flush=True)


def _file_logger(path: str):
    """Лог одновременно в консоль и в файл.

    Нужен боту: он запускается скрыто (pythonw), консоли нет, поэтому вывод
    дублируем в logs/bot.out.log — этот файл читает diagnostics.
    """
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    def log(msg: str) -> None:
        line = f"{now_iso()}  {msg}"
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
        print(line, flush=True)

    return log


def cmd_search(args) -> None:
    from . import pipeline

    cfg = load_config(args.config)
    ensure_dirs(cfg)
    board = getattr(args, "board", None)
    bcfg = board_config(cfg, board) if board else cfg
    store = Store(cfg["paths"]["db"])
    driver = None
    if bcfg.get("search_via_browser"):
        try:
            from .apply_selenium import build_driver, ensure_login

            driver = build_driver(bcfg)
            if board:
                from .boards import get_board

                get_board(board)(bcfg).ensure_login(driver)
            else:
                ensure_login(driver, bcfg)
        except Exception as exc:  # noqa: BLE001
            _log(f"браузер для поиска не поднялся: {exc}")
    try:
        stats = pipeline.search_vacancies(
            cfg, store, log=_log, max_detail=args.max_detail, driver=driver, board=board
        )
    finally:
        if driver is not None:
            driver.quit()
    _log(f"готово: одобрено {stats['approved']}, отсеяно {stats['skipped']}, новых {stats['new']}")
    _log("дальше: python main.py letters --ai && python main.py report")
    store.close()


def cmd_letters(args) -> None:
    from . import pipeline

    cfg = load_config(args.config)
    profile = load_profile(args.profile)
    ensure_dirs(cfg)
    store = Store(cfg["paths"]["db"])
    stats = pipeline.generate_letters(
        cfg, store, profile, log=_log,
        use_ai=args.ai, force=args.force, only=args.only, limit=args.limit,
    )
    _log(
        f"письма: ИИ {stats['ai']}, шаблон {stats['template']}, "
        f"привязано готовых {stats['existing']}, промптов {stats['prompts']}, "
        f"ошибок {stats['errors']}"
    )
    store.close()


def cmd_report(args) -> None:
    cfg = load_config(args.config)
    ensure_dirs(cfg)
    store = Store(cfg["paths"]["db"])
    rows = store.queue(limit=args.limit)
    from .report import write_reports

    html_path, md_path = write_reports(cfg, rows)
    _log(f"очередь: {len(rows)} вакансий")
    _log(f"  HTML: {html_path}")
    _log(f"  MD:   {md_path}")
    store.close()


def cmd_stats(args) -> None:
    cfg = load_config(args.config)
    store = Store(cfg["paths"]["db"])
    stats = store.stats()
    _log(f"по решениям: {stats['by_decision']}")
    _log(f"по статусам: {stats['by_status']}")
    _log(f"отправлено сегодня: {stats['applied_today']}")
    store.close()


def cmd_login(args) -> None:
    """Вход в аккаунт. Для других бирж открывает видимое окно с их страницей входа."""
    cfg = load_config(args.config)
    if getattr(args, "timeout", None):
        cfg["browser"]["login_timeout_sec"] = args.timeout
    board = getattr(args, "board", None)
    if board and board != "hh":
        from .apply_selenium import build_driver
        from .boards import get_board

        bcfg = board_config(cfg, board)
        if getattr(args, "timeout", None):
            bcfg["browser"]["login_timeout_sec"] = args.timeout
        log = _file_logger(os.path.join(cfg["root"], "logs", "agent.out.log"))
        driver = build_driver(bcfg, hidden=False)  # вход — окно должно быть видно
        try:
            get_board(board)(bcfg).login_interactive(driver, log=log)
        finally:
            try:
                driver.quit()
            except Exception:  # noqa: BLE001
                pass
        return
    from .apply_selenium import login

    login(cfg)


def cmd_calibrate(args) -> None:
    cfg = load_config(args.config)
    ensure_dirs(cfg)
    store = Store(cfg["paths"]["db"])
    from .apply_selenium import calibrate

    calibrate(cfg, store)
    store.close()


def cmd_apply(args) -> None:
    from . import pipeline

    cfg = load_config(args.config)
    ensure_dirs(cfg)
    store = Store(cfg["paths"]["db"])
    limit = 1 if args.once else args.limit
    pipeline.apply_vacancies(cfg, store, limit, log=_log, dry_run=args.dry_run,
                             board=getattr(args, "board", None))
    store.close()


def cmd_inbox(args) -> None:
    from . import pipeline

    cfg = load_config(args.config)
    ensure_dirs(cfg)
    profile = load_profile(args.profile)
    store = Store(cfg["paths"]["db"])
    stats = pipeline.read_inbox(cfg, store, profile, log=_log,
                                board=getattr(args, "board", None))
    _log(f"переписок: {stats.get('dialogues', 0)}, новых входящих: {stats.get('new_incoming', 0)}")
    for msg in stats.get("new", []):
        _log(f"  💬 {msg['text'][:120]}")
    store.close()


def cmd_check(args) -> None:
    """Лёгкая проверка переписки: новые ответы → Telegram + черновики (без поиска/откликов)."""
    from . import pipeline

    cfg = load_config(args.config)
    ensure_dirs(cfg)
    log = _file_logger(os.path.join(cfg["root"], "logs", "agent.out.log"))
    profile = load_profile(args.profile)
    store = Store(cfg["paths"]["db"])
    board = getattr(args, "board", None)
    bcfg = board_config(cfg, board) if board else cfg
    board_title = bcfg.get("board_title") or (board or "hh")
    is_hh = (board or "hh") == "hh"
    driver = None
    try:
        if store.is_halted():
            log("система на аварийном стопе — переписку не читаю (возобновить: /resume)")
            return
        from .pipeline import cycle_running

        if cycle_running(cfg, board):
            log(f"идёт цикл «{board_title}» (браузер занят) — проверку переписки пропускаю")
            return
        from .apply_selenium import build_driver, ensure_login
        from .notify import Notifier

        try:
            driver = build_driver(bcfg)
            if board:
                from .boards import get_board

                get_board(board)(bcfg).ensure_login(driver)
            else:
                ensure_login(driver, bcfg)
        except Exception as exc:  # noqa: BLE001
            log(f"браузер не поднялся: {exc}")
            driver = None

        stats = pipeline.read_inbox(cfg, store, profile, log=log, driver=driver, board=board)
        log(
            f"переписка: диалогов {stats.get('dialogues', 0)}, "
            f"новых входящих {stats.get('new_incoming', 0)}"
        )
        notifier = Notifier(cfg, log=log)
        pending = store.unseen_incoming(50)
        if pending:
            lines = pipeline._reply_lines(cfg, store, pending)
            for line in lines:
                notifier.send_now(f"Ответ на {board_title}", line)
            skipped = len(pending) - len(lines)
            if skipped:
                log(f"отказов (в Telegram не шлём): {skipped}")
            store.mark_messages_notified([m["id"] for m in pending])
            log(f"уведомлений об ответах отправлено: {len(lines)}")
        if not is_hh:
            log(f"{board_title}: черновики ответов пока только для hh")
        if is_hh and (cfg.get("dialog") or {}).get("enabled", True):
            try:
                drafts = pipeline.prepare_replies(
                    cfg, store, profile, notifier=notifier, log=log
                )
                log(f"черновиков ответов: {drafts.get('created', 0)}")
            except Exception as exc:  # noqa: BLE001
                log(f"  ! черновики не подготовлены: {exc}")
            try:
                acts = pipeline.auto_answer_assistant(cfg, store, profile, log=log, driver=driver)
                if acts.get("sent"):
                    log(f"авто-ответов ИИ-помощнику: {acts['sent']}")
            except Exception as exc:  # noqa: BLE001
                log(f"  ! авто-ответы не сработали: {exc}")
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:  # noqa: BLE001
                pass
        store.close()


def cmd_browser(args) -> None:
    """Держит ОБЩИЙ браузер открытым (его переиспользуют задачи — без холодного старта)."""
    import time

    cfg = load_config(args.config)
    ensure_dirs(cfg)
    log = _file_logger(os.path.join(cfg["root"], "logs", "agent.log"))
    from .apply_selenium import build_driver, ensure_login

    cfg["browser"]["reuse"] = False  # именно мы запускаем браузер
    driver = build_driver(cfg, hidden=cfg["browser"].get("hidden", True))
    try:
        ensure_login(driver, cfg)
        log(f"общий браузер поднят (debug-порт {cfg['browser'].get('debug_port')}), держу открытым")
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log("общий браузер остановлен")
    finally:
        try:
            driver.quit()
        except Exception:  # noqa: BLE001
            pass


def cmd_channels(args) -> None:
    """Вакансии из Telegram-каналов: фильтр + проверка ИИ + черновик отклика."""
    from .channels import run

    cfg = load_config(args.config)
    ensure_dirs(cfg)
    log = _file_logger(os.path.join(cfg["root"], "logs", "agent.out.log"))
    stats = run(cfg, log=log, dry_run=args.dry_run, limit=args.limit)
    log(
        f"telegram-каналы: новых {stats['new']}, чёрный список {stats['blacklist']}, "
        f"не профиль {stats['prefilter']}, ИИ отсеял {stats['ai_rejected']}, "
        f"релевантных {stats['relevant']}, отправлено {stats['sent']}"
    )


def cmd_hirify(args) -> None:
    """Вакансии с Hirify (Selenium, бесплатный тариф)."""
    from .hirify import run

    cfg = load_config(args.config)
    ensure_dirs(cfg)
    log = _file_logger(os.path.join(cfg["root"], "logs", "agent.out.log"))
    stats = run(cfg, log=log, dry_run=args.dry_run, limit=args.limit)
    log(
        f"hirify: страниц {stats['pages']}, карточек {stats['cards']}, "
        f"отфильтровано {stats['filtered']}, ИИ отсеял {stats['ai_rejected']}, "
        f"релевантных {stats['relevant']}, отправлено {stats['sent']}"
    )


def cmd_boost(args) -> None:
    """Поднять резюме в поиске.

    Общий механизм (окно площадки, замок, повтор) — в hh_autoapply/boost.py,
    разметка конкретной биржи — в hh_autoapply/boards/<биржа>.py.
    """
    from .boost import run_boost

    cfg = load_config(args.config)
    ensure_dirs(cfg)
    log = _file_logger(os.path.join(cfg["root"], "logs", "agent.out.log"))
    board = getattr(args, "board", None) or cfg.get("board") or "hh"
    run_boost(cfg, board, log=log, force=getattr(args, "force", False))


def cmd_funnels(args) -> None:
    cfg = load_config(args.config)
    store = Store(cfg["paths"]["db"])
    from . import analytics

    bundle = analytics.build(store)
    text = analytics.render(bundle)
    _log(text)
    if args.send:
        from .notify import Notifier

        notifier = Notifier(cfg, log=_log)
        if notifier.enabled:
            ok = notifier.send_pre("hh-autoapply: сводка по воронкам", text, silent=True)
            _log("отправлено в Telegram" if ok else "не удалось отправить")
        else:
            _log("Telegram не настроен (см. .env)")
    store.close()


def cmd_selftest(args) -> None:
    from .selftest import run_all

    run_all(log=_log)


def cmd_bot(args) -> None:
    cfg = load_config(args.config)
    ensure_dirs(cfg)
    from .bot import ControlBot

    log = _file_logger(os.path.join(cfg["root"], "logs", "bot.out.log"))
    log("hh-autoapply: запуск бота (лог: logs/bot.out.log)")
    ControlBot(cfg, log=log).poll_forever()


def cmd_followup(args) -> None:
    from .followup import send_missing_letters

    cfg = load_config(args.config)
    ensure_dirs(cfg)
    log = _file_logger(os.path.join(cfg["root"], "logs", "agent.out.log"))
    store = Store(cfg["paths"]["db"])
    from .pipeline import cycle_running

    if cycle_running(cfg):
        log("идёт цикл (браузер занят) — досылку пропускаю, сделаю в следующий раз")
        store.close()
        return
    send_missing_letters(cfg, store, log=log, dry_run=args.dry_run)
    store.close()


def cmd_stop(args) -> None:
    """Аварийный стоп: прекращает все действия с браузером."""
    cfg = load_config(args.config)
    store = Store(cfg["paths"]["db"])
    try:
        store.set_halted(True, "остановлено вручную через CLI")
        store.set_paused(True)
        _log("🛑 Аварийный стоп установлен: ни откликов, ни чтения переписки.")
        _log("Возобновить: .venv/bin/python main.py resume")
        if args.all:
            import subprocess
            import sys

            from .platform_support import platform_name

            if platform_name() == "macos":
                for label in ("com.hhautoapply.agent", "com.hhautoapply.bot", "com.hhautoapply.digest"):
                    subprocess.run(
                        ["launchctl", "unload", os.path.expanduser(f"~/Library/LaunchAgents/{label}.plist")],
                        capture_output=True,
                    )
                    _log(f"  агент остановлен: {label}")
                _log("Запустить обратно: ./scripts/install_launchd.sh и другие скрипты")
            elif platform_name() == "windows":
                for name in ("hh-autoapply-cycle", "hh-autoapply-bot", "hh-autoapply-inbox",
                             "hh-autoapply-boost", "hh-autoapply-followup", "hh-autoapply-digest"):
                    subprocess.run(["schtasks", "/End", "/TN", name], capture_output=True)
                    subprocess.run(["schtasks", "/Change", "/TN", name, "/DISABLE"], capture_output=True)
                    _log(f"  задача остановлена и отключена: {name}")
                _log("Включить обратно: schtasks /Change /TN hh-autoapply-cycle /ENABLE "
                     "(и для bot, digest)")
            else:
                for name in ("hh-autoapply-agent", "hh-autoapply-bot", "hh-autoapply-digest"):
                    subprocess.run(["systemctl", "--user", "stop", name], capture_output=True)
                    _log(f"  служба остановлена: {name}")
    finally:
        store.close()


def cmd_resume(args) -> None:
    cfg = load_config(args.config)
    store = Store(cfg["paths"]["db"])
    try:
        store.set_halted(False)
        store.set_paused(False)
    finally:
        store.close()
    _log("Работа возобновлена: аварийный стоп и пауза сняты.")


def cmd_notify_test(args) -> None:
    cfg = load_config(args.config)
    from .notify import Notifier

    notifier = Notifier(cfg, log=_log)
    if not notifier.enabled:
        _log("Telegram не настроен. Заполни TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env")
        return
    ok = notifier.send_now("hh-autoapply: проверка связи", f"Уведомления работают. {now_iso()}")
    _log("отправлено" if ok else "не удалось отправить")


def cmd_set_secrets(args) -> None:
    """Записывает ключи в .env со скрытым вводом — чтобы они не попадали в чат."""
    import getpass
    import os

    from .env import ROOT

    path = os.path.join(ROOT, ".env")
    keys = ["DEEPSEEK_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
    _log("Введи значения (ввод скрыт, Enter без текста — оставить как есть):")

    updates: dict[str, str] = {}
    for key in keys:
        try:
            value = getpass.getpass(f"  {key}: ").strip()
        except (EOFError, KeyboardInterrupt):
            _log("\nпрервано")
            return
        if value:
            updates[key] = value

    if not updates:
        _log("ничего не изменилось")
        return

    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()

    out: list[str] = []
    done: set[str] = set()
    for line in lines:
        if "=" in line and not line.strip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                done.add(key)
                continue
        out.append(line)
    for key, value in updates.items():
        if key not in done:
            out.append(f"{key}={value}")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    _log(f"обновлено в .env: {', '.join(sorted(updates))}")


def cmd_tg_discover(args) -> None:
    """Определяет chat_id по последним сообщениям боту и пишет его в .env."""
    import json
    import os
    import urllib.request

    from .env import ROOT
    from .notify import Telegram

    cfg = load_config(args.config)
    tg = Telegram.from_config(cfg)
    if not tg.token:
        _log("TELEGRAM_BOT_TOKEN не задан в .env")
        return

    url = f"https://api.telegram.org/bot{tg.token}/getUpdates"
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        _log(f"не удалось получить обновления: {exc}")
        return

    chats: dict[str, str] = {}
    for upd in data.get("result", []):
        message = upd.get("message") or upd.get("edited_message") or {}
        chat = message.get("chat") or {}
        if chat.get("id"):
            name = " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
            chats[str(chat["id"])] = name or chat.get("username") or chat.get("type", "")

    if not chats:
        _log("сообщений боту нет. Открой бота в Telegram и нажми Start, затем повтори.")
        return

    _log("найденные чаты:")
    for chat_id, name in chats.items():
        _log(f"  {chat_id} — {name}")

    chosen = args.chat_id or (list(chats)[-1])
    path = os.path.join(ROOT, ".env")
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    out, done = [], False
    for line in lines:
        if line.strip().startswith("TELEGRAM_CHAT_ID="):
            out.append(f"TELEGRAM_CHAT_ID={chosen}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"TELEGRAM_CHAT_ID={chosen}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    _log(f"записал TELEGRAM_CHAT_ID={chosen} в .env")


def cmd_digest(args) -> None:
    from .diagnostics import run_digest

    cfg = load_config(args.config)
    ensure_dirs(cfg)
    log = _file_logger(os.path.join(cfg["root"], "logs", "agent.out.log"))
    run_digest(cfg, log=log)


def cmd_drafts(args) -> None:
    cfg = load_config(args.config)
    store = Store(cfg["paths"]["db"])
    drafts = store.pending_drafts(limit=args.limit or 20)
    if not drafts:
        _log("черновиков в ожидании нет")
    for d in drafts:
        vac = store.get(d["vacancy_id"]) if d["vacancy_id"] else None
        _log("─" * 60)
        _log(f"{(vac or {}).get('title', d['chat_id'])[:60]} | id={d['id']}")
        _log(d["text"])
    store.close()


def cmd_run(args) -> None:
    from . import pipeline

    cfg = load_config(args.config)
    ensure_dirs(cfg)
    log = _file_logger(os.path.join(cfg["root"], "logs", "agent.out.log"))
    limit = args.limit
    if limit is None:
        try:
            limit = int(os.environ.get("HH_APPLY_LIMIT", "10"))
        except ValueError:
            limit = 10
    pipeline.run_cycle(
        cfg=cfg,
        apply_limit=limit,
        do_apply=not args.no_apply,
        use_ai=not args.no_ai,
        max_detail=args.max_detail,
        log=log,
        board=getattr(args, "board", None),
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hh-autoapply", description="Поиск, письма и автоотклик на hh.ru")
    p.add_argument("--config", default=None, help="путь к config.json")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("search", help="найти вакансии, отфильтровать и оценить")
    s.add_argument("--no-detail", action="store_true", help="не тянуть полные описания")
    s.add_argument("--max-detail", type=int, default=140, help="сколько вакансий детально обработать")
    s.add_argument("--board", default=None, help="биржа: hh / superjob / habr")
    s.set_defaults(func=cmd_search)

    l = sub.add_parser("letters", help="сгенерировать сопроводительные")
    l.add_argument("--profile", default=None)
    l.add_argument("--only", nargs="*", help="только эти id вакансий")
    l.add_argument("--limit", type=int, default=None)
    l.add_argument("--force", action="store_true", help="перегенерировать письма заново")
    l.add_argument("--ai", action="store_true", help="письма через LLM (ключ из .env)")
    l.set_defaults(func=cmd_letters)

    r = sub.add_parser("report", help="собрать HTML/MD-очередь")
    r.add_argument("--limit", type=int, default=None)
    r.set_defaults(func=cmd_report)

    st = sub.add_parser("stats", help="статистика по базе")
    st.set_defaults(func=cmd_stats)

    lg = sub.add_parser("login", help="проверить/выполнить вход в аккаунт биржи")
    lg.add_argument("--timeout", type=int, default=None, help="сколько секунд ждать вход")
    lg.add_argument("--board", default=None, help="биржа: hh / superjob / habr")
    lg.set_defaults(func=cmd_login)

    c = sub.add_parser("calibrate", help="снять разметку формы отклика (для отладки селекторов)")
    c.set_defaults(func=cmd_calibrate)

    a = sub.add_parser("apply", help="отправить отклики (Selenium)")
    a.add_argument("--limit", type=int, default=None, help="сколько откликов максимум за запуск")
    a.add_argument("--dry-run", action="store_true", help="только показать план, без браузера")
    a.add_argument("--once", action="store_true", help="обработать ровно одну вакансию")
    a.add_argument("--board", default=None, help="биржа: hh / superjob / habr")
    a.set_defaults(func=cmd_apply)

    ib = sub.add_parser("inbox", help="прочитать ответы работодателей")
    ib.add_argument("--profile", default=None)
    ib.add_argument("--board", default=None, help="биржа: hh / superjob / habr")
    ib.set_defaults(func=cmd_inbox)

    ck = sub.add_parser("check", help="проверить переписку: новые ответы в Telegram + черновики")
    ck.add_argument("--profile", default=None)
    ck.add_argument("--board", default=None, help="биржа: hh / superjob / habr")
    ck.set_defaults(func=cmd_check)

    br = sub.add_parser("browser", help="держать общий браузер открытым (ускорение задач)")
    br.set_defaults(func=cmd_browser)

    hi = sub.add_parser("hirify", help="вакансии с Hirify (Selenium, бесплатный тариф)")
    hi.add_argument("--dry-run", action="store_true", help="только показать, без отправки")
    hi.add_argument("--limit", type=int, default=10)
    hi.set_defaults(func=cmd_hirify)

    ch = sub.add_parser("channels", help="вакансии из Telegram-каналов (черновики в Telegram)")
    ch.add_argument("--dry-run", action="store_true", help="только показать, без отправки")
    ch.add_argument("--limit", type=int, default=12)
    ch.set_defaults(func=cmd_channels)

    bo = sub.add_parser("boost", help="поднять резюме в поиске (раз в 4 часа)")
    bo.add_argument("--force", action="store_true",
                    help="поднять сейчас, не глядя на время следующего окна площадки")
    bo.add_argument("--board", default=None, help="биржа: hh / superjob / habr")
    bo.set_defaults(func=cmd_boost)

    nt = sub.add_parser("notify-test", help="проверить уведомления в Telegram")
    nt.set_defaults(func=cmd_notify_test)

    sp = sub.add_parser("stop", help="аварийный стоп всей автоматики")
    sp.add_argument("--all", action="store_true", help="ещё и выгрузить агентов launchd")
    sp.set_defaults(func=cmd_stop)

    fu = sub.add_parser("followup", help="дослать сопроводительные в переписку")
    fu.add_argument("--dry-run", action="store_true", help="только показать, кому досылать")
    fu.set_defaults(func=cmd_followup)

    rs = sub.add_parser("resume", help="снять аварийный стоп и паузу")
    rs.set_defaults(func=cmd_resume)

    fn = sub.add_parser("funnels", help="сводка по воронкам (где больше ответов)")
    fn.add_argument("--send", action="store_true", help="отправить сводку в Telegram")
    fn.set_defaults(func=cmd_funnels)

    bt = sub.add_parser("bot", help="Telegram-бот с управлением")
    bt.set_defaults(func=cmd_bot)

    se = sub.add_parser("selftest", help="самопроверка всех подсистем")
    se.set_defaults(func=cmd_selftest)

    ss = sub.add_parser("set-secrets", help="записать ключи в .env (скрытый ввод)")
    ss.set_defaults(func=cmd_set_secrets)

    tg = sub.add_parser("tg-discover", help="определить chat_id и записать в .env")
    tg.add_argument("--chat-id", default=None, help="выбрать конкретный chat_id")
    tg.set_defaults(func=cmd_tg_discover)

    dg = sub.add_parser("digest", help="проанализировать логи и прислать аномалии в Telegram")
    dg.set_defaults(func=cmd_digest)

    dr = sub.add_parser("drafts", help="показать черновики ответов работодателю")
    dr.add_argument("--limit", type=int, default=20)
    dr.set_defaults(func=cmd_drafts)

    rn = sub.add_parser("run", help="полный цикл (для планировщика)")
    rn.add_argument("--limit", type=int, default=None, help="лимит откликов за цикл")
    rn.add_argument("--no-apply", action="store_true", help="не отправлять отклики")
    rn.add_argument("--no-ai", action="store_true", help="не использовать LLM")
    rn.add_argument("--max-detail", type=int, default=140)
    rn.add_argument("--board", default=None, help="биржа: hh / superjob / habr")
    rn.set_defaults(func=cmd_run)

    return p


def main(argv=None) -> None:
    # UTF-8 на выводе — иначе на Windows кириллица в логах роняет программу.
    import io
    import sys as _sys

    # Под pythonw консоли нет: sys.stdout/stderr == None, и любая запись или
    # проверка isatty() в сторонних библиотеках (logging, selenium) роняет процесс.
    if _sys.stdout is None:
        _sys.stdout = io.StringIO()
    if _sys.stderr is None:
        _sys.stderr = io.StringIO()

    for _stream in (_sys.stdout, _sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    load_dotenv()  # ключи LLM и Telegram из .env
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "no_detail", False):
        args.max_detail = 0
    _log(f"hh-autoapply · {now_iso()}")
    try:
        args.func(args)
    except KeyboardInterrupt:
        _log("прервано пользователем")
        sys.exit(130)


if __name__ == "__main__":
    main()
