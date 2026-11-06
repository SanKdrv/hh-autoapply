"""Уведомления в Telegram + вывод в консоль."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid

from .env import env

MAX_LEN = 3900


class Telegram:
    def __init__(self, token: str = "", chat_id: str = "", enabled: bool = True, log=None):
        self.token = token
        self.chat_id = str(chat_id or "")
        self.enabled = bool(enabled and token and self.chat_id)
        self.log = log
        self.last_error = ""

    @classmethod
    def from_config(cls, cfg: dict, log=None) -> "Telegram":
        n = (cfg.get("notify") or {}).get("telegram") or {}
        token = n.get("bot_token") or env(n.get("bot_token_env", "TELEGRAM_BOT_TOKEN"))
        chat_id = n.get("chat_id") or env(n.get("chat_id_env", "TELEGRAM_CHAT_ID"))
        return cls(token=token, chat_id=chat_id, enabled=n.get("enabled", True), log=log)

    def send(self, text: str, silent: bool = False, reply_markup: dict | None = None) -> bool:
        if not self.enabled:
            self.last_error = "Telegram не настроен (нет токена или chat_id)"
            return False
        chunks = [text[i : i + MAX_LEN] for i in range(0, len(text), MAX_LEN)] or [""]
        for chunk in chunks:
            payload = {
                "chat_id": self.chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "disable_notification": silent,
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    if resp.status != 200:
                        self.last_error = f"HTTP {resp.status}"
                        return False
            except urllib.error.HTTPError as exc:
                try:
                    detail = json.loads(exc.read().decode("utf-8", "replace")).get("description", "")
                except Exception:  # noqa: BLE001
                    detail = ""
                self.last_error = f"HTTP {exc.code}: {detail}"
                if self.log:
                    self.log(f"Telegram: {self.last_error}")
                return False
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                if self.log:
                    self.log(f"Telegram: {self.last_error}")
                return False
        return True

    def send_photo(self, png: bytes, caption: str = "") -> bool:
        """Отправляет картинку (например, капчу). Только отправка, без парса html."""
        if not self.enabled:
            self.last_error = "Telegram не настроен (нет токена или chat_id)"
            return False
        boundary = "----hh" + uuid.uuid4().hex
        parts: list[bytes] = []

        def _field(name: str, value: str) -> None:
            parts.append(
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                    f"{value}\r\n"
                ).encode("utf-8")
            )

        _field("chat_id", self.chat_id)
        if caption:
            _field("caption", caption[:1000])
        parts.append(
            (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="photo"; filename="captcha.png"\r\n'
                "Content-Type: image/png\r\n\r\n"
            ).encode("utf-8")
        )
        parts.append(png)
        parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/sendPhoto",
            data=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status == 200
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            if self.log:
                self.log(f"Telegram: {self.last_error}")
            return False


def _esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


class Notifier:
    """Единая точка уведомлений: консоль + Telegram."""

    def __init__(self, cfg: dict, log=print):
        self.tg = Telegram.from_config(cfg, log=log)
        self.cfg = cfg
        self.log = log
        self.buffer: list[str] = []

    def line(self, text: str) -> None:
        self.log(text)

    def collect(self, text: str) -> None:
        self.buffer.append(text)

    def flush(self, title: str, silent: bool = False) -> bool:
        if not self.buffer:
            return False
        body = "\n".join(self.buffer)
        self.buffer = []
        message = f"<b>{_esc(title)}</b>\n\n{_esc(body)}"
        return self.tg.send(message, silent=silent)

    def send_now(self, title: str, body: str, silent: bool = False) -> bool:
        message = f"<b>{_esc(title)}</b>\n\n{_esc(body)}"
        return self.tg.send(message, silent=silent)

    def send_photo(self, png: bytes, caption: str = "") -> bool:
        return self.tg.send_photo(png, caption)

    def send_pre(self, title: str, body: str, silent: bool = False) -> bool:
        """Отправляет моноширинный блок — для таблиц и сводок."""
        message = f"<b>{_esc(title)}</b>\n<pre>{_esc(body)}</pre>"
        return self.tg.send(message, silent=silent)

    def send_escalation(self, draft: dict, vacancy: dict | None,
                        reason: str, incoming: str) -> bool:
        """Ситуация требует решения человека — просто уведомляем."""
        vac = vacancy or {}
        body = (
            f"<b>⚠️ Требует твоего решения</b>\n"
            f"{_esc(vac.get('title') or 'вакансия')} — {_esc(vac.get('company') or '')}\n\n"
            f"<b>Сообщение работодателя:</b>\n{_esc((incoming or '')[:900])}\n\n"
            f"<b>Почему передаю тебе:</b>\n{_esc(reason or 'нестандартная ситуация')}"
        )
        if vac.get("url"):
            body += f"\n\n{_esc(vac['url'])}"
        return self.tg.send(body)

    def send_draft(self, draft: dict, vacancy: dict | None) -> bool:
        """Черновик ответа работодателю с кнопками подтверждения."""
        vac = vacancy or {}
        title = vac.get("title") or "вакансия"
        company = vac.get("company") or ""
        body = (
            f"<b>💬 Ответ работодателю</b>\n"
            f"{_esc(title)} — {_esc(company)}\n\n"
            f"{_esc(draft['text'])}"
        )
        markup = {
            "inline_keyboard": [[
                {"text": "✅ Отправить", "callback_data": f"draft:send:{draft['id']}"},
                {"text": "🔄 Переписать", "callback_data": f"draft:regen:{draft['id']}"},
                {"text": "✖️ Пропустить", "callback_data": f"draft:skip:{draft['id']}"},
            ]]
        }
        return self.tg.send(body, reply_markup=markup)

    @property
    def enabled(self) -> bool:
        return self.tg.enabled
