"""Аналитика по воронкам: где больше откликов и где чаще отвечают."""
from __future__ import annotations

from .util import norm, now_iso

INVITE_MARKERS = [
    "приглаша", "собеседован", "интервью", "созвон", "встреч", "тестовое",
    "готовы обсудить", "предлагаем", "заинтересова", "хотим пообщаться",
]
REJECT_MARKERS = [
    "к сожалению", "отказ", "не подходите", "выбран другой", "решили продолжить",
    "не готовы", "неактуально", "не актуально", "позиция закрыта", "уже нашли",
    "остановились на", "не сможем",
]


def classify(text: str) -> str:
    """invite / reject / mixed / other."""
    t = norm(text)
    invite = any(m in t for m in INVITE_MARKERS)
    reject = any(m in t for m in REJECT_MARKERS)
    if invite and reject:
        return "mixed"
    if invite:
        return "invite"
    if reject:
        return "reject"
    return "other"


def build(store) -> dict:
    """Собирает статистику воронок по вакансиям и входящим сообщениям."""
    vacancies = store.all("decision='approve' OR status IN ('applied','already')")
    messages = store.all_messages("in")

    by_vacancy: dict[str, list[dict]] = {}
    for msg in messages:
        vid = str(msg.get("vacancy_id") or "")
        if vid:
            by_vacancy.setdefault(vid, []).append(msg)

    rows: dict[str, dict] = {}
    for vac in vacancies:
        funnel = vac.get("funnel") or "Прочее"
        row = rows.setdefault(
            funnel,
            {"funnel": funnel, "total": 0, "sent": 0, "replied": 0, "invites": 0, "rejects": 0},
        )
        row["total"] += 1

        responded = vac.get("status") in ("applied", "already") or bool(
            by_vacancy.get(str(vac["id"]))
        )
        if responded:
            row["sent"] += 1

        found = by_vacancy.get(str(vac["id"]))
        if found:
            row["replied"] += 1

        # Приоритет — точное состояние отклика с hh, иначе классификация по тексту.
        state = store.get_state(f"neg_state:{vac['id']}", "").upper()
        if state in ("INVITATION", "INVITED", "APPOINTMENT", "INTERVIEW"):
            row["invites"] += 1
        elif state in ("DISCARD", "DISCARDED", "REJECTION"):
            row["rejects"] += 1
        elif found:
            kinds = {classify(m["text"]) for m in found}
            if kinds & {"invite", "mixed"}:
                row["invites"] += 1
            elif kinds == {"reject"}:
                row["rejects"] += 1

    ordered = sorted(rows.values(), key=lambda r: (-r["replied"], -r["sent"]))
    totals = {
        key: sum(r[key] for r in ordered)
        for key in ("total", "sent", "replied", "invites", "rejects")
    }
    return {"rows": ordered, "totals": totals, "built_at": now_iso()}


def _pct(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "—"


def render(bundle: dict, title: str = "Сводка по воронкам") -> str:
    """Текстовая таблица (моноширинная — для Telegram <pre>)."""
    rows = bundle.get("rows") or []
    totals = bundle.get("totals") or {}
    if not rows:
        return "Пока нет данных: отправь отклики и прочитай переписку."

    header = f"{'Воронка':<20}{'Откл':>5}{'Отв':>5}{'Отв%':>6}{'Пригл':>7}{'Отказ':>7}"
    lines = [title, "", header, "-" * len(header)]
    for r in rows:
        lines.append(
            f"{r['funnel'][:20]:<20}{r['sent']:>5}{r['replied']:>5}"
            f"{_pct(r['replied'], r['sent']):>6}{r['invites']:>7}{r['rejects']:>7}"
        )
    lines.append("-" * len(header))
    lines.append(
        f"{'ИТОГО':<20}{totals.get('sent', 0):>5}{totals.get('replied', 0):>5}"
        f"{_pct(totals.get('replied', 0), totals.get('sent', 0)):>6}"
        f"{totals.get('invites', 0):>7}{totals.get('rejects', 0):>7}"
    )

    best = max(rows, key=lambda r: (r["replied"], r["invites"]))
    if best["replied"]:
        lines += [
            "",
            f"Лучше всего откликаются в воронке «{best['funnel']}»: "
            f"{best['replied']} ответ(ов) на {best['sent']} отклик(ов).",
        ]
    return "\n".join(lines)
