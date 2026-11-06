"""Мелкие утилиты: HTTP, работа с текстом, JSON, случайные паузы."""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from html import unescape

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_TAGS = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"[ \t\r\f\v]+")
_MULTINL = re.compile(r"\n{3,}")
_LATIN = re.compile(r"^[a-z0-9 .+/-]+$")


def kw_match(text_norm: str, kw: str) -> bool:
    """Поиск ключевого слова с учётом границ для коротких латинских токенов.

    «go» не должен находиться внутри «Google», «qa» — внутри «qatar»,
    а «intern» — внутри «internal».
    """
    kw_norm = norm(kw).strip()
    if not kw_norm:
        return False
    if _LATIN.match(kw_norm) and len(kw_norm) <= 4:
        return re.search(rf"(?<![a-z0-9]){re.escape(kw_norm)}(?![a-z0-9])", text_norm) is not None
    return kw_norm in text_norm


def count_occurrences(text_norm: str, kw: str) -> int:
    kw_norm = norm(kw).strip()
    if not kw_norm:
        return 0
    if _LATIN.match(kw_norm) and len(kw_norm) <= 4:
        return len(re.findall(rf"(?<![a-z0-9]){re.escape(kw_norm)}(?![a-z0-9])", text_norm))
    return text_norm.count(kw_norm)


class HttpError(Exception):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} для {url}")
        self.status = status
        self.url = url
        self.body = body


def http_get(
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: int = 30,
    retries: int = 3,
    backoff: float = 2.0,
) -> str:
    if params:
        clean = {k: v for k, v in params.items() if v not in (None, "")}
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(clean, doseq=True)

    h = {
        "User-Agent": DEFAULT_UA,
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Accept": "*/*",
    }
    if headers:
        h.update(headers)

    last: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            last = HttpError(exc.code, url, body)
            # Повторяем только на временных ошибках.
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
                continue
            raise last
        except Exception as exc:  # noqa: BLE001 — сетевые сбои
            last = exc
            time.sleep(backoff * (attempt + 1))
    raise last if last else RuntimeError("неизвестная ошибка HTTP")


def strip_html(html: str) -> str:
    """Грубо превращает HTML описания вакансии в читаемый текст."""
    if not html:
        return ""
    s = html
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</(p|li|ul|ol|div|h[1-6]|tr)>", "\n", s, flags=re.I)
    s = re.sub(r"<li[^>]*>", "\n• ", s, flags=re.I)
    s = _TAGS.sub("", s)
    s = unescape(s)
    s = _SPACES.sub(" ", s)
    s = re.sub(r" *\n *", "\n", s)
    s = _MULTINL.sub("\n\n", s)
    return s.strip()


def norm(text: str) -> str:
    """Нормализация для поиска ключевых слов (регистр, ё, лишние пробелы)."""
    if not text:
        return ""
    s = text.lower().replace("ё", "е")
    s = re.sub(r"\s+", " ", s)
    return s


def company_excluded(company: str, cfg: dict) -> bool:
    """True, если компания в списке «не связываться» (config.no_contact_companies)."""
    names = [norm(x) for x in (cfg.get("no_contact_companies") or []) if x]
    c = norm(company or "")
    return bool(c) and any(n in c for n in names)


def load_json(path: str, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def human_sleep(lo: float, hi: float, log=print) -> float:
    delay = random.uniform(lo, hi)
    if log:
        log(f"    … пауза {delay:.0f} c")
    time.sleep(delay)
    return delay


def digest(*parts) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(str(p).encode("utf-8"))
    return h.hexdigest()


def pick(seq, seed: str):
    """Детерминированный выбор из списка — чтобы письма не были клонами."""
    if not seq:
        return None
    idx = int(digest(seed)[:8], 16) % len(seq)
    return seq[idx]


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def is_remote(work_format: str) -> bool:
    """Формат работы содержит удалёнку (с учётом ё/е и nbsp)."""
    return "удаленно" in norm(work_format)


def _as_int(value) -> int | None:
    """Приводит значение из БД (часто строка) к int."""
    if value is None or value == "":
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def parse_salary_full(text: str) -> tuple[int | None, int | None, str]:
    """Разбирает строку зарплаты hh.

    Возвращает (от, до, вид), где вид: "net" (на руки), "gross" (до вычета налогов)
    или "" (не указано / не рубли).
    """
    if not text:
        return (None, None, "")

    low = norm(text)
    if "не указан" in low:
        return (None, None, "")

    # Не рубли: суммы в другой валюте не сравниваем с рублёвым порогом.
    foreign = any(marker in low for marker in ("$", "€", "usd", "eur", "тенге", "₸", "сум", "бел"))
    if foreign:
        return (None, None, "")

    if "на руки" in low:
        kind = "net"
    elif "до вычета" in low or "вычета налогов" in low or "gross" in low:
        kind = "gross"
    else:
        kind = ""

    nums = []
    for raw in re.findall(r"\d[\d\s\u00a0]{2,}", text):
        value = int(re.sub(r"\D", "", raw))
        if value >= 1000:
            nums.append(value)
    if not nums:
        return (None, None, kind)

    # Вилка «от X до Y» — сначала этот случай, иначе он перекрывается «от».
    if len(nums) >= 2:
        return (min(nums[0], nums[-1]), max(nums[0], nums[-1]), kind)

    head = low.lstrip()
    if head.startswith("до "):
        return (None, nums[0], kind)
    if head.startswith("от "):
        return (nums[0], None, kind)
    return (nums[0], nums[0], kind)


def parse_salary(text: str) -> tuple[int | None, int | None]:
    """Разбирает строку зарплаты hh в (от, до). Совместимость."""
    low_from, low_to, _ = parse_salary_full(text)
    return (low_from, low_to)


def net_of(value, kind: str, ndfl: float = 0.13) -> int | None:
    """Пересчитывает конкретную сумму в «на руки»."""
    number = _as_int(value)
    if not number:
        return None
    if kind == "net":
        return number
    return int(round(number * (1 - ndfl)))


def salary_net_estimate(salary_from, salary_to, kind: str, ndfl: float = 0.13) -> int | None:
    """Оценка «на руки» по гарантированному минимуму: «от», иначе «до»."""
    return net_of(_as_int(salary_from) or _as_int(salary_to), kind, ndfl)


def salary_upper_bound(salary_from, salary_to) -> int | None:
    """Верхняя известная граница зарплаты — по ней оцениваем «жирность»."""
    candidates = [v for v in (_as_int(salary_to), _as_int(salary_from)) if v]
    return max(candidates) if candidates else None


def work_format_label(work_format: str) -> str:
    """Короткий ярлык для отчёта: удалёнка / офис / гибрид."""
    w = norm(work_format)
    if not w:
        return ""
    tags = []
    if "удаленно" in w:
        tags.append("удалёнка")
    if "гибрид" in w:
        tags.append("гибрид")
    if "на месте" in w or "офис" in w:
        tags.append("офис")
    if "разъездн" in w or "полев" in w:
        tags.append("разъездная")
    return "/".join(tags)


def today() -> str:
    return time.strftime("%Y-%m-%d")
