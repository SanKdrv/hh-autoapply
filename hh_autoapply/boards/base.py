"""Базовый адаптер биржи.

Адаптер знает только «свою» площадку. Если метод не поддержан — он по умолчанию
ничего не делает и возвращает пустой результат, чтобы движок работал с любым
набором возможностей.
"""
from __future__ import annotations


class Board:
    #: короткое имя (используется в конфиге, БД, задачах планировщика)
    name = "?"
    #: человеческое название (для уведомлений и логов)
    title = "?"
    #: нужен ли браузер для поиска/откликов
    needs_browser = True
    #: умеет откликаться автоматически
    supports_apply = True

    def __init__(self, cfg: dict):
        self.cfg = cfg

    # --- поиск ---------------------------------------------------------
    def search(self, log=print, driver=None) -> list:
        """Поиск вакансий. Возвращает список models.Vacancy."""
        raise NotImplementedError

    def fetch_detail(self, vid: str, use_cache: bool = True, url: str = "") -> dict:
        """Описание вакансии (нужно скорингу). Пустой dict — «не умеем»."""
        return {}

    # --- авторизация ---------------------------------------------------
    def is_logged_in(self, driver) -> bool:
        return True

    def ensure_login(self, driver, interactive=None, log=print) -> None:
        return None

    # --- вход в видимом окне -------------------------------------------
    #: страница входа этой биржи (для ручного входа)
    login_url = ""

    def login_interactive(self, driver, log=print, timeout: int | None = None) -> bool:
        """Открыть страницу входа и дождаться, пока пользователь войдёт сам."""
        import time

        limit = int(timeout or (self.cfg.get("browser") or {}).get("login_timeout_sec", 300))
        # Браузер помнит позицию окна из профиля: если профиль создавался в скрытом
        # режиме, окно окажется за экраном. Ставим его на видное место.
        try:
            driver.set_window_position(60, 60)
            driver.maximize_window()
        except Exception:  # noqa: BLE001
            pass
        if self.login_url:
            driver.get(self.login_url)
        log(f"{self.title}: войди в открывшемся окне (жду до {limit // 60} мин)")
        started = time.time()
        hits = 0
        while time.time() - started < limit:
            time.sleep(5)
            try:
                ok = self.is_logged_in(driver)
            except Exception:  # noqa: BLE001
                ok = False
            # требуем подтверждения дважды: одиночное срабатывание бывает ложным
            # (переходы внутри формы входа), из-за этого окно закрывалось раньше времени
            hits = hits + 1 if ok else 0
            if hits >= 2:
                log(f"{self.title}: вход выполнен ✓ — сессия сохранена в профиле")
                return True
        log(f"{self.title}: вход не выполнен за {limit // 60} мин")
        return False

    # --- отклики -------------------------------------------------------
    def apply_queue(self, store, driver=None, limit=None, dry_run=False, log=print) -> dict:
        raise NotImplementedError

    # --- переписка -----------------------------------------------------
    def read_replies(self, store, driver=None, profile=None, log=print) -> dict:
        return {"dialogues": 0, "messages": 0, "new_incoming": 0, "new": []}

    def supports_replies(self) -> bool:
        """Умеем ли отвечать работодателю (иначе только читаем и уведомляем)."""
        return False

    def send_message(self, chat_id: str, text: str, driver=None, log=print) -> bool:
        return False

    # --- поднятие резюме -----------------------------------------------
    def boost(self) -> bool:
        """Есть ли у биржи поднятие резюме."""
        return False

    def raise_resume(self, driver, log=print) -> tuple[int, str, str]:
        """Поднять резюме. Возвращает (сколько нажали, текст ответа площадки, диагностика)."""
        raise NotImplementedError
