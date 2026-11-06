"""Источники вакансий: официальный API, RSS-поиск, HTML-выдача.

Порядок такой: RSS — основной рабочий путь (не требует авторизации и,
что важно, работает из-за рубежа). API используется, если он доступен
(например, при российском IP или прокси). HTML — резервный парсер.
"""
from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET

from .models import Vacancy
from .util import HttpError, http_get, load_json, norm, save_json, strip_html

_LDJSON = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
_RESPONSE_LINK = re.compile(r"/applicant/vacancy_response\?vacancyId=(\d+)(?:&(?:amp;)?employerId=(\d+))?")
_ITEM = re.compile(r"<item>(.*?)</item>", re.S)
_TAG = re.compile(r"<(\w+)[^>]*>(.*?)</\1>", re.S)
_WORK_FORMAT = re.compile(r'data-qa="work-formats-text"[^>]*>(.*?)</p>', re.S)
_EMPLOYMENT = re.compile(r'data-qa="common-employment-text"[^>]*>(.*?)</div>', re.S)
_SALARY = re.compile(r'data-qa="vacancy-salary"[^>]*>(.*?)</(?:div|p|span)>', re.S)

# Версия формата кэша: при изменении структуры старый кэш игнорируется.
CACHE_VERSION = 3


def _clean_inline(raw: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>|<!-- -->", "", raw)).strip()


def _tag_text(xml: str, tag: str) -> str:
    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", xml, re.S)
    return m.group(1).strip() if m else ""


def _parse_rss_description(desc_html: str) -> dict:
    """RSS кладёт в description краткую сводку построчно."""
    text = strip_html(desc_html)
    data = {"company": "", "area": "", "salary": "", "created": ""}
    for line in text.split("\n"):
        line = line.strip()
        low = norm(line)
        if low.startswith("вакансия компании:"):
            data["company"] = line.split(":", 1)[1].strip()
        elif low.startswith("создана:"):
            data["created"] = line.split(":", 1)[1].strip()
        elif low.startswith("регион:"):
            data["area"] = line.split(":", 1)[1].strip()
        elif low.startswith("предполагаемый уровень месячного дохода:"):
            data["salary"] = line.split(":", 1)[1].strip()
    return data


def search_rss(cfg: dict, params: dict) -> list[Vacancy]:
    host = cfg["host"]
    xml = http_get(f"https://{host}/search/vacancy/rss", params=params)
    root = ET.fromstring(xml)
    out: list[Vacancy] = []
    for item in root.findall(".//item"):
        link = (item.findtext("link") or "").strip()
        m = re.search(r"/vacancy/(\d+)", link)
        if not m:
            continue
        vid = m.group(1)
        title = (item.findtext("title") or "").strip()
        desc_html = item.findtext("description") or ""
        meta = _parse_rss_description(desc_html)
        out.append(
            Vacancy(
                id=vid,
                title=title,
                company=meta["company"],
                area=meta["area"],
                salary=meta["salary"],
                url=link,
                published_at=(item.findtext("pubDate") or "").strip() or meta["created"],
                source="rss",
            )
        )
    return out


def search_api(cfg: dict, params: dict) -> list[Vacancy]:
    """Официальный API поиска. Часто недоступен вне РФ (403 геоблок)."""
    base = cfg["api_base"].rstrip("/")
    raw = http_get(f"{base}/vacancies", params=params, headers={"Accept": "application/json"})
    data = json.loads(raw)
    out: list[Vacancy] = []
    for it in data.get("items", []):
        salary = it.get("salary") or {}
        salary_str = ""
        if salary:
            salary_str = "—".join(
                str(x) for x in (salary.get("from"), salary.get("to")) if x
            )
            if salary_str and salary.get("currency"):
                salary_str = f"{salary_str} {salary.get('currency')}"
        out.append(
            Vacancy(
                id=str(it.get("id")),
                title=it.get("name", ""),
                company=(it.get("employer") or {}).get("name", ""),
                area=(it.get("area") or {}).get("name", ""),
                salary=salary_str or "не указан",
                url=it.get("alternate_url", ""),
                published_at=it.get("published_at", ""),
                source="api",
            )
        )
    return out


def _parse_serp(host: str, html: str) -> list[Vacancy]:
    """Парсит карточки из HTML выдачи поиска (общее для http и браузера)."""
    out: list[Vacancy] = []
    # Разбиваем на карточки по data-qa="vacancy-serp__vacancy"
    chunks = re.split(r'data-qa="vacancy-serp__vacancy"', html)[1:]
    for chunk in chunks:
        tail = chunk[:6000]
        vid_m = _RESPONSE_LINK.search(tail) or re.search(r"/vacancy/(\d+)", tail)
        if not vid_m:
            continue
        vid = vid_m.group(1)
        emp_m = _RESPONSE_LINK.search(tail)
        employer_id = emp_m.group(2) if emp_m and emp_m.lastindex and emp_m.lastindex >= 2 else ""
        title_m = re.search(r'data-qa="serp-item__title"[^>]*>(.*?)</a>', tail, re.S)
        if not title_m:
            title_m = re.search(r'<a[^>]*serp-item__title[^>]*>(.*?)</a>', tail, re.S)
        comp_m = re.search(r'data-qa="vacancy-serp__vacancy-employer[^"]*"[^>]*>(.*?)</a>', tail, re.S)
        sal_m = re.search(r'data-qa="vacancy-serp__vacancy-compensation"[^>]*>(.*?)</span>', tail, re.S)
        area_m = re.search(r'data-qa="vacancy-serp__vacancy-address[^"]*"[^>]*>(.*?)</span>', tail, re.S)
        out.append(
            Vacancy(
                id=vid,
                title=strip_html(title_m.group(1)) if title_m else "",
                company=strip_html(comp_m.group(1)) if comp_m else "",
                area=strip_html(area_m.group(1)) if area_m else "",
                salary=strip_html(sal_m.group(1)) if sal_m else "не указан",
                url=f"https://{host}/vacancy/{vid}",
                response_url=f"https://{host}/applicant/vacancy_response?vacancyId={vid}",
                employer_id=employer_id,
                source="html",
            )
        )
    return out


def search_html(cfg: dict, params: dict) -> list[Vacancy]:
    """Резервный парсер HTML-выдачи, если RSS недоступен."""
    html = http_get(f"https://{cfg['host']}/search/vacancy", params=params)
    return _parse_serp(cfg["host"], html)


def search_browser(cfg: dict, params: dict, driver, max_pages: int = 3, log=print) -> list[Vacancy]:
    """Поиск через залогиненный браузер.

    hh рендерит авторизованным больше карточек, чем отдаёт в HTML-строке
    (и умеет листать глубже). Возвращает уникальные вакансии по страницам.
    """
    import time
    from urllib.parse import urlencode

    from selenium.webdriver.common.by import By

    host = cfg["host"]
    clean = {k: v for k, v in params.items() if v not in (None, "") and k != "page"}
    items: list[Vacancy] = []
    known: set[str] = set()
    for page in range(max_pages):
        url = f"https://{host}/search/vacancy?" + urlencode({**clean, "page": page})
        driver.get(url)
        for _ in range(12):
            time.sleep(1)
            if driver.find_elements(By.CSS_SELECTOR, '[data-qa="vacancy-serp__vacancy"]'):
                break
        fresh = [v for v in _parse_serp(host, driver.page_source) if v.id not in known]
        if not fresh:
            break
        log(f"    + {len(fresh)} вакансий через браузер (стр. {page})")
        known.update(v.id for v in fresh)
        items.extend(fresh)
    return items


def funnel_from_profile(name: str) -> str:
    """Нормализует название поискового профиля в воронку для аналитики."""
    n = norm(name)
    # AI/вайбкодинг — отдельная воронка со своей дневной квотой. Без этой ветки
    # воронкой становилось всё имя профиля, и квота AI никогда не срабатывала.
    if (re.search(r"(?<![a-z0-9])(ai|llm|gpt|ml)(?![a-z0-9])", n)
            or "вайбкод" in n or "нейросет" in n):
        return "AI"
    has_go = bool(re.search(r"(?<![a-z])(go|golang)(?![a-z])", n))  # не «django»
    has_py = "python" in n
    if has_go and has_py:
        return "Go + Python"
    if "php" in n or "wordpress" in n:
        return "PHP / WordPress"
    if has_py:
        return "Python"
    if has_go:
        return "Go"
    if "backend" in n or "бэкенд" in n:
        return "Backend"
    return name.strip() or "Прочее"


def _fetch_api_pages(cfg: dict, base: dict, max_pages: int) -> list[Vacancy]:
    """API с постраничной догрузкой (per_page до 100). Работает при РФ-IP."""
    per_page = int(cfg.get("api_per_page", 100) or 100)
    items: list[Vacancy] = []
    known: set[str] = set()
    for page in range(max_pages):
        chunk = search_api(cfg, {**base, "page": page, "per_page": per_page})
        fresh = [v for v in chunk if v.id not in known]
        items.extend(fresh)
        known.update(v.id for v in fresh)
        if len(chunk) < per_page:
            break
    return items


def _fetch_profile(cfg: dict, base: dict, max_pages: int, log=print) -> list[Vacancy]:
    """Один поисковый профиль: сначала API (полный охват), иначе RSS + HTML-страницы."""
    # 1) API — лучший охват, но доступен только с российского IP.
    try:
        items = _fetch_api_pages(cfg, base, max_pages)
        if items:
            log(f"    + {len(items)} вакансий через API")
            return items
    except HttpError as exc:
        log(f"    ! API: HTTP {exc.status} — перехожу на RSS")
    except Exception as exc:  # noqa: BLE001
        log(f"    ! API: {exc} — перехожу на RSS")

    # 2) RSS (только стр. 0, пагинации нет) + догрузка страниц HTML-парсером.
    items = []
    for fetch in (search_rss, search_html):
        try:
            items = fetch(cfg, {**base, "page": 0})
            log(f"    + {len(items)} вакансий через {fetch.__name__} (стр. 0)")
            break
        except HttpError as exc:
            log(f"    ! {fetch.__name__}: HTTP {exc.status}")
        except Exception as exc:  # noqa: BLE001
            log(f"    ! {fetch.__name__}: {exc}")

    known = {v.id for v in items}
    for page in range(1, max_pages):
        try:
            more = search_html(cfg, {**base, "page": page})
        except Exception as exc:  # noqa: BLE001
            log(f"    ! search_html стр.{page}: {exc}")
            break
        fresh = [v for v in more if v.id not in known]
        if not fresh:
            break
        log(f"    + {len(fresh)} вакансий через search_html (стр. {page})")
        known.update(v.id for v in fresh)
        items.extend(fresh)
    return items


def search_all(cfg: dict, log=print, driver=None) -> list[Vacancy]:
    """Проходит по всем поисковым профилям, дедуплицирует результат.

    Если передан driver и в конфиге включён search_via_browser — ищем через
    браузер (hh отдаёт авторизованным больше карточек и листает глубже),
    иначе — через RSS + догрузку HTML-страниц.
    """
    seen: dict[str, Vacancy] = {}
    max_pages = max(1, int(cfg.get("search_pages", 3) or 3))
    use_browser = bool(cfg.get("search_via_browser")) and driver is not None
    for profile in cfg.get("searches", []):
        if not profile.get("enabled", True):
            continue
        funnel = funnel_from_profile(profile.get("name") or profile.get("text", ""))
        base = {
            "text": profile.get("text", ""),
            "area": profile.get("area"),
            "experience": profile.get("experience"),
            "schedule": profile.get("schedule"),
            "order_by": profile.get("order_by", "publication_time"),
        }
        log(f"[поиск] {profile.get('name', profile.get('text'))} → воронка «{funnel}»")
        items = None
        if use_browser:
            try:
                items = search_browser(cfg, base, driver, max_pages, log=log)
            except Exception as exc:  # noqa: BLE001
                log(f"    ! браузерный поиск: {exc} — перехожу на RSS/HTML")
                items = None
        if not items:
            items = _fetch_profile(cfg, base, max_pages, log=log)
        for vac in items:
            vac.funnel = funnel
            existing = seen.get(vac.id)
            if existing is None:
                seen[vac.id] = vac
            elif funnel == "AI" and (existing.funnel or "").strip().upper() != "AI":
                # Одна и та же вакансия часто находится и обычным профилем, и
                # AI-профилем. Воронка должна оставаться AI, иначе она уходит в
                # обычную квоту, а AI-квота простаивает.
                existing.funnel = "AI"
    return list(seen.values())


def fetch_detail(cfg: dict, vid: str, use_cache: bool = True) -> dict:
    """Полная карточка вакансии: описание из JSON-LD JobPosting."""
    cache_path = os.path.join(cfg["paths"]["cache_dir"], f"{vid}.json")
    if use_cache:
        cached = load_json(cache_path)
        if cached and cached.get("_v") == CACHE_VERSION:
            return cached

    host = cfg["host"]
    result = {"description": "", "title": "", "employmentType": "", "datePosted": "", "_v": CACHE_VERSION}
    try:
        html = http_get(f"https://{host}/vacancy/{vid}")
    except HttpError:
        return result

    for blob in _LDJSON.findall(html):
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "JobPosting":
            result = {
                "description": strip_html(data.get("description", "")),
                "title": data.get("title", ""),
                "employmentType": data.get("employmentType", ""),
                "datePosted": data.get("datePosted", ""),
                "hiringOrganization": (data.get("hiringOrganization") or {}).get("name", ""),
            }
            break

    response = _RESPONSE_LINK.search(html)
    if response:
        result["response_url"] = f"https://{host}/applicant/vacancy_response?vacancyId={response.group(1)}"
        if response.lastindex and response.lastindex >= 2 and response.group(2):
            result["employer_id"] = response.group(2)

    wf = _WORK_FORMAT.search(html)
    result["work_formats"] = (
        re.sub(r"^Формат работы:\s*", "", _clean_inline(wf.group(1))) if wf else ""
    )
    emp = _EMPLOYMENT.search(html)
    result["employment"] = _clean_inline(emp.group(1)) if emp else ""

    sal = _SALARY.search(html)
    result["salary_text"] = _clean_inline(sal.group(1)) if sal else ""

    if use_cache:
        save_json(cache_path, result)
    return result
