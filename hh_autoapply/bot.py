"""Telegram-бот: управление системой, отчёты и уведомления.

Запуск:  .venv/bin/python main.py bot
Бот отвечает только на сообщения из TELEGRAM_CHAT_ID.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request

from .env import env
from .notify import Telegram, _esc
from .platform_support import SleepGuard, on_ac_power

HELP = """<b>hh-autoapply</b>

<b>Статус и отчёты</b>
/status — состояние системы
/funnels — сводка по воронкам
/queue [N] — топ вакансий из очереди
/keywords — вакансии с кодовым словом
/runs — последние запуски
/report — ссылка на HTML-очередь

<b>Управление</b>
/run [N] — полный цикл (поиск, письма, отклики, ответы)
/apply [N] — только отправить отклики
/letters — перегенерировать письма
/pause — приостановить отправку откликов
/resume — возобновить
/help — эта справка

<b>Капча</b>
Если пришла капча — пришли мне её текст обычным сообщением
или командой /captcha &lt;ответ&gt;, и я введу его сам (ПК не нужен).
"""


class ControlBot:
    def __init__(self, cfg: dict, log=print):
        self.cfg = cfg
        self.log = log
        self.tg = Telegram.from_config(cfg, log=log)
        self.token = self.tg.token
        self.chat_id = self.tg.chat_id
        self.offset = 0
        self._busy = threading.Lock()
        self._poll_failures = 0
        self._awake_guard = None
        self._awake_lock = threading.Lock()

    # ── транспорт ───────────────────────────────────────────────────────

    def api(self, method: str, **params):
        params = {k: v for k, v in params.items() if v is not None}
        data = urllib.parse.urlencode(params).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/{method}", data=data, method="POST"
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def send(self, text: str, pre: bool = False) -> None:
        body = f"<pre>{_esc(text)}</pre>" if pre else _esc(text)
        try:
            self.api(
                "sendMessage",
                chat_id=self.chat_id,
                text=body,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            self.log(f"→ ответ отправлен ({len(text)} симв.)")
        except Exception as exc:  # noqa: BLE001
            self.log(f"!!! не удалось отправить сообщение: {exc}")

    # ── данные ──────────────────────────────────────────────────────────

    def store(self):
        from .store import Store

        return Store(self.cfg["paths"]["db"])

    # ── команды ─────────────────────────────────────────────────────────

    def cmd_status(self) -> str:
        store = self.store()
        try:
            stats = store.stats()
            paused = store.is_paused()
            halted = store.is_halted()
            lines = [
                f"⚠️ АВАРИЙНЫЙ СТОП: {'ДА — автоматика выключена' if halted else 'нет'}",
                f"Пауза откликов: {'да' if paused else 'нет'}",
                f"Отправлено сегодня: {stats['applied_today']}",
                f"По решениям: {stats['by_decision']}",
                f"По статусам: {stats['by_status']}",
                f"В очереди автоотклика: {len(store.queue(only_auto=True))}",
                f"Новых ответов: {len(store.unseen_incoming(200))}",
                f"Черновиков в ожидании: {len(store.pending_drafts(50))}",
            ]
            if halted:
                lines.append(f"Причина: {store.get_state('halted_reason', '—')} ({store.get_state('halted_at', '')})")
            return "\n".join(lines)
        finally:
            store.close()

    def cmd_funnels(self) -> str:
        from . import analytics

        store = self.store()
        try:
            return analytics.render(analytics.build(store))
        finally:
            store.close()

    def cmd_queue(self, limit: int = 10) -> str:
        store = self.store()
        try:
            rows = store.queue(limit=limit)
            if not rows:
                return "Очередь пуста."
            out = []
            for i, r in enumerate(rows, 1):
                flag = "ручной" if (r.get("eligibility") or "auto") == "manual" else "авто"
                kw = f" 🔑{r['keyword']}" if r.get("keyword") else ""
                out.append(f"{i}. [{r['score']}] {r['title'][:60]} — {r['company'][:30]} ({flag}){kw}")
                out.append(f"    {r['url']}")
            return "\n".join(out)
        finally:
            store.close()

    def cmd_keywords(self) -> str:
        store = self.store()
        try:
            rows = store.with_keywords()
            if not rows:
                return "Вакансий с кодовым словом не найдено."
            out = []
            for r in rows:
                out.append(f"🔑 {r['keyword']} — {r['title'][:50]} ({r['company'][:25]})")
            return "\n".join(out)
        finally:
            store.close()

    def cmd_runs(self) -> str:
        store = self.store()
        try:
            rows = store.last_runs(6)
            return "\n".join(f"{r['ts']} [{r['kind']}] {r['info'][:120]}" for r in rows) or "Пусто."
        finally:
            store.close()

    def cmd_report(self) -> str:
        import os

        path = os.path.join(self.cfg["paths"]["out_dir"], "queue.html")
        return f"Очередь: {path}" if os.path.exists(path) else "Отчёт ещё не собран."

    def cmd_pause(self) -> str:
        store = self.store()
        try:
            store.set_paused(True)
        finally:
            store.close()
        return "Отклики на паузе. /resume — возобновить."

    def cmd_resume(self) -> str:
        store = self.store()
        try:
            store.set_paused(False)
            was_halted = store.is_halted()
            store.set_halted(False)
        finally:
            store.close()
        return "Работа возобновлена." if not was_halted else "Аварийный стоп снят, автоматика возобновлена."

    def cmd_stop(self) -> str:
        """Аварийный стоп: полностью останавливает действия с браузером."""
        store = self.store()
        try:
            store.set_halted(True, "остановлено вручную через /stop")
            store.set_paused(True)
        finally:
            store.close()
        return (
            "🛑 Аварийный стоп.\n"
            "Действия с браузером прекращены: ни откликов, ни чтения переписки.\n"
            "Поиск и письма продолжают работать.\n\n"
            "Возобновить: /resume"
        )

    # ── черновики ответов работодателю ──────────────────────────────────

    def cmd_drafts(self) -> str:
        store = self.store()
        try:
            drafts = store.pending_drafts()
            if not drafts:
                return "Черновиков нет."
            out = []
            for d in drafts:
                vac = store.get(d["vacancy_id"]) if d["vacancy_id"] else None
                out.append(f"• {(vac or {}).get('title', d['chat_id'])[:50]}")
                out.append(f"  {d['text'][:200]}")
                out.append("")
            return "\n".join(out)
        finally:
            store.close()

    def send_draft(self, draft_id: str) -> str:
        store = self.store()
        try:
            draft = store.get_draft(draft_id)
            if not draft or draft["status"] != "pending":
                return "Черновик не найден или уже обработан."
        finally:
            store.close()
        return self._spawn("отправка ответа работодателю", self._do_send_draft, draft_id)

    def _do_send_draft(self, draft_id: str) -> dict:
        from .apply_selenium import build_driver, ensure_login
        from .chat_send import send_message
        from .util import digest

        store = self.store()
        driver = None
        try:
            draft = store.get_draft(draft_id)
            if not draft:
                return {"sent": False, "reason": "черновик исчез"}
            driver = build_driver(self.cfg)
            ensure_login(driver, self.cfg)
            ok = send_message(driver, draft["chat_id"], draft["text"])
            if ok:
                store.set_draft_status(draft_id, "sent")
                store.add_message(
                    digest(draft["chat_id"], draft["text"][:80]),
                    draft["chat_id"], draft["vacancy_id"], "out", draft["text"], "",
                )
            return {"отправлено": "да" if ok else "нет"}
        finally:
            if driver:
                driver.quit()
            store.close()

    def regenerate_draft(self, draft_id: str) -> str:
        return self._spawn("перегенерация ответа", self._do_regen_draft, draft_id)

    def _do_regen_draft(self, draft_id: str) -> dict:
        from . import replies
        from .config import load_profile

        store = self.store()
        try:
            draft = store.get_draft(draft_id)
            if not draft:
                return {"ok": False}
            vac = store.get(draft["vacancy_id"]) if draft["vacancy_id"] else None
            msgs = [
                m for m in store.all_messages(None)
                if str(m["negotiation_id"]) == str(draft["chat_id"])
            ]
            result = replies.draft_reply(self.cfg, load_profile(), vac or {}, msgs)
            if result.get("escalate"):
                from .notify import Notifier

                Notifier(self.cfg, log=self.log).send_escalation(
                    draft, vac, result.get("reason", ""),
                    next((m["text"] for m in reversed(msgs) if m.get("direction") == "in"), ""),
                )
                return {"передано тебе": result.get("reason", "")[:60]}
            text = result["text"]
            store.conn.execute("UPDATE drafts SET text=? WHERE id=?", (text, draft_id))
            store.conn.commit()
            from .notify import Notifier

            notifier = Notifier(self.cfg, log=self.log)
            notifier.send_draft(store.get_draft(draft_id), vac)
            return {"переписано": "да"}
        finally:
            store.close()

    def skip_draft(self, draft_id: str) -> str:
        store = self.store()
        try:
            store.set_draft_status(draft_id, "skipped")
        finally:
            store.close()
        return "Черновик пропущен."

    # ── капча через Telegram ────────────────────────────────────────────

    def _captcha_waiting(self) -> bool:
        store = self.store()
        try:
            return bool(store.get_state("captcha.request_ts", ""))
        finally:
            store.close()

    def _answer_captcha(self, text: str) -> None:
        answer = (text or "").strip()
        if not answer:
            self.send("Пустой ответ. Пришли символы с картинки капчи.")
            return
        store = self.store()
        try:
            store.set_state("captcha.answer", answer)
        finally:
            store.close()
        self.send(f"✅ Принял ответ капчи «{answer}» — ввожу и отправляю.")

    def handle_callback(self, data: str) -> None:
        parts = (data or "").split(":")
        if len(parts) != 3 or parts[0] != "draft":
            return
        action, draft_id = parts[1], parts[2]
        if action == "send":
            self.send(self.send_draft(draft_id))
        elif action == "regen":
            self.send(self.regenerate_draft(draft_id))
        elif action == "skip":
            self.send(self.skip_draft(draft_id))

    # ── запуск задач в фоне ─────────────────────────────────────────────

    def _spawn(self, name: str, func, *args) -> str:
        if not self._busy.acquire(blocking=False):
            return "Уже выполняется другая задача — подожди."
        def runner():
            try:
                self.send(f"▶️ Запускаю: {name}")
                result = func(*args)
                if isinstance(result, dict):
                    text = ", ".join(f"{k}={v}" for k, v in result.items() if k != "new")
                else:
                    text = str(result)
                self.send(f"✅ {name} завершено\n{text}")
            except Exception as exc:  # noqa: BLE001
                self.send(f"❌ {name}: ошибка\n{exc}")
            finally:
                self._busy.release()

        threading.Thread(target=runner, daemon=True).start()
        return ""

    def cmd_run(self, limit: int | None) -> str:
        from . import pipeline

        limit = limit if limit is not None else int(self.cfg.get("schedule", {}).get("apply_limit", 10))
        return self._spawn("полный цикл", pipeline.run_cycle, self.cfg, None, limit, True, True, 140, self.log)

    def cmd_apply(self, limit: int | None) -> str:
        from . import pipeline

        store = self.store()
        limit = limit if limit is not None else int(self.cfg.get("schedule", {}).get("apply_limit", 10))
        try:
            if store.is_paused():
                return "Стоит пауза. Сначала /resume."
        finally:
            store.close()

        from .pipeline import cycle_running

        if cycle_running(self.cfg):
            return "Сейчас идёт цикл — отклики уже отправляются. Подожди."
        return self._spawn(f"отклики (лимит {limit})", pipeline.apply_vacancies, self.cfg, self.store(), limit, self.log, False)

    def cmd_letters(self) -> str:
        from . import pipeline
        from .config import load_profile

        return self._spawn("генерация писем", pipeline.generate_letters, self.cfg, self.store(), load_profile(), self.log, True, True)

    def dispatch(self, text: str) -> None:
        parts = (text or "").strip().split()
        if not parts:
            return
        cmd = parts[0].split("@")[0].lower()
        arg = None
        if len(parts) > 1 and parts[1].isdigit():
            arg = int(parts[1])

        if cmd in ("/start", "/help"):
            self.send(HELP)
        elif cmd == "/status":
            self.send(self.cmd_status(), pre=True)
        elif cmd == "/funnels":
            self.send(self.cmd_funnels(), pre=True)
        elif cmd == "/queue":
            self.send(self.cmd_queue(arg or 10), pre=True)
        elif cmd == "/keywords":
            self.send(self.cmd_keywords(), pre=True)
        elif cmd == "/drafts":
            self.send(self.cmd_drafts(), pre=True)
        elif cmd == "/runs":
            self.send(self.cmd_runs(), pre=True)
        elif cmd == "/report":
            self.send(self.cmd_report())
        elif cmd == "/pause":
            self.send(self.cmd_pause())
        elif cmd == "/resume":
            self.send(self.cmd_resume())
        elif cmd == "/stop":
            self.send(self.cmd_stop())
        elif cmd == "/run":
            self.send(self.cmd_run(arg) or "Принято.")
        elif cmd == "/apply":
            self.send(self.cmd_apply(arg) or "Принято.")
        elif cmd == "/letters":
            self.send(self.cmd_letters() or "Принято.")
        elif cmd == "/captcha":
            payload = text.split(None, 1)[1].strip() if len(parts) > 1 else ""
            self._answer_captcha(payload)
        else:
            self.send("Неизвестная команда. /help")

    # ── удержание системы от сна (экран при этом гаснет) ────────────────

    def _start_keep_awake(self) -> None:
        """Пока бот работает — не даём системе уснуть по простою.

        От сети удержание включено (экран гаснет как обычно); от батареи —
        по настройке schedule.prevent_sleep_on_battery. Состояние питания
        перепроверяется, чтобы зря не жечь батарею.
        """
        schedule = self.cfg.get("schedule") or {}
        if not schedule.get("prevent_sleep", True):
            return
        allow_on_battery = bool(schedule.get("prevent_sleep_on_battery", False))

        def loop() -> None:
            while True:
                want = on_ac_power() or allow_on_battery
                try:
                    with self._awake_lock:
                        if want and self._awake_guard is None:
                            self._awake_guard = SleepGuard(log=self.log).start()
                            self.log("удержание сна включено: система не уснёт по простою (экран гаснет как обычно)")
                        elif not want and self._awake_guard is not None:
                            self._awake_guard.stop()
                            self._awake_guard = None
                            self.log("питание от батареи — удержание сна выключено")
                except Exception as exc:  # noqa: BLE001
                    self.log(f"сбой удержания сна: {exc}")
                time.sleep(30)

        threading.Thread(target=loop, daemon=True).start()

    def _stop_keep_awake(self) -> None:
        with self._awake_lock:
            if self._awake_guard is not None:
                self._awake_guard.stop()
                self._awake_guard = None

    # ── цикл опроса ─────────────────────────────────────────────────────

    def poll_forever(self) -> None:
        if not self.tg.enabled:
            self.log("Telegram не настроен: заполни TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env")
            return
        self.log(f"бот запущен, chat_id={self.chat_id}")
        self._start_keep_awake()
        try:
            self._loop()
        finally:
            self._stop_keep_awake()

    def _loop(self) -> None:
        try:
            self.api("setMyCommands", commands=json.dumps([
                {"command": "status", "description": "состояние системы"},
                {"command": "funnels", "description": "сводка по воронкам"},
                {"command": "queue", "description": "очередь вакансий"},
                {"command": "keywords", "description": "вакансии с кодовым словом"},
                {"command": "drafts", "description": "черновики ответов работодателю"},
                {"command": "run", "description": "полный цикл"},
                {"command": "apply", "description": "отправить отклики"},
                {"command": "pause", "description": "пауза откликов"},
                {"command": "resume", "description": "возобновить работу"},
                {"command": "stop", "description": "аварийный стоп всей автоматики"},
                {"command": "help", "description": "справка"},
            ]))
        except Exception:  # noqa: BLE001
            pass

        while True:
            try:
                updates = self.api("getUpdates", offset=self.offset, timeout=25)
                if self._poll_failures >= 3:
                    self.log(f"опрос восстановлен после {self._poll_failures} сбоев подряд")
                self._poll_failures = 0
                for upd in updates.get("result", []):
                    self.offset = upd["update_id"] + 1

                    # нажатие инлайн-кнопки
                    callback = upd.get("callback_query")
                    if callback:
                        chat = str(((callback.get("message") or {}).get("chat") or {}).get("id", ""))
                        if self.chat_id and chat != str(self.chat_id):
                            continue
                        try:
                            self.api("answerCallbackQuery", callback_query_id=callback["id"])
                        except Exception:  # noqa: BLE001
                            pass
                        data = callback.get("data") or ""
                        self.log(f"← кнопка {data}")
                        self.handle_callback(data)
                        continue

                    message = upd.get("message") or upd.get("edited_message") or {}
                    chat = str((message.get("chat") or {}).get("id", ""))
                    text = message.get("text") or ""
                    if not text:
                        continue
                    if self.chat_id and chat != str(self.chat_id):
                        self.log(f"игнорирую сообщение из чужого чата {chat}")
                        continue
                    self.log(f"← {text[:80]}")
                    if not text.startswith("/") and self._captcha_waiting():
                        self._answer_captcha(text)
                        continue
                    self.dispatch(text)
            except KeyboardInterrupt:
                self.log("бот остановлен")
                return
            except Exception as exc:  # noqa: BLE001
                # Обрыв long-polling — штатная ситуация, не засоряем лог.
                self._poll_failures += 1
                if self._poll_failures in (3, 10, 30) or self._poll_failures % 50 == 0:
                    self.log(
                        f"сбоев опроса подряд: {self._poll_failures} "
                        f"(последний: {type(exc).__name__}: {exc})"
                    )
                time.sleep(min(3 * self._poll_failures, 30))
