"""Отчёты: человекочитаемая очередь вакансий с письмами."""
from __future__ import annotations

import html
import os

from .util import is_remote, now_iso, work_format_label


def _read_letter(path: str) -> str:
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        # отрезаем служебный заголовок
        parts = text.split("\n\n", 1)
        return parts[1] if len(parts) == 2 else text
    return ""


def _salary_text(row: dict) -> str:
    """Строка зарплаты с оценкой «на руки»."""
    from .util import _as_int

    base = row.get("salary") or "—"
    net = _as_int(row.get("salary_net"))
    if net:
        kind = row.get("salary_kind") or "gross"
        mark = "на руки" if kind == "net" else "≈ на руки"
        return f"{base} ({mark} {net:,} ₽)".replace(",", " ")
    return base


def build_markdown(rows: list[dict]) -> str:
    lines = [f"# Очередь откликов", "", f"_сформировано {now_iso()}_, всего: {len(rows)}", ""]
    for i, row in enumerate(rows, 1):
        lines.append(f"## {i}. {row['title']} — {row['company']}")
        lines.append(f"- Балл: **{row['score']}** · стек: {row.get('stack') or '—'} · статус: {row.get('status')}")
        lines.append(f"- Зарплата: {_salary_text(row)} · регион: {row.get('area') or '—'}")
        lines.append(f"- Формат работы: {row.get('work_format') or '—'}")
        if (row.get("eligibility") or "auto") == "manual":
            lines.append("- Отправка: **только вручную** (офис/гибрид)")
        lines.append(f"- Вакансия: {row.get('url')}")
        lines.append(f"- Откликнуться: {row.get('response_url') or row.get('url')}")
        letter = _read_letter(row.get("letter_path", ""))
        if letter:
            lines.append("")
            lines.append("```")
            lines.append(letter.strip())
            lines.append("```")
        lines.append("")
    return "\n".join(lines)


def build_html(rows: list[dict]) -> str:
    cards = []
    for i, row in enumerate(rows, 1):
        letter = _read_letter(row.get("letter_path", ""))
        apply_url = row.get("response_url") or row.get("url") or ""
        wf_raw = row.get("work_format") or ""
        wf = work_format_label(wf_raw)
        wf_class = "remote" if is_remote(wf_raw) else ("office" if wf else "")
        manual = (row.get("eligibility") or "auto") == "manual"
        manual_badge = '<span class="badge manual">только вручную</span>' if manual else ""
        keyword = (row.get("keyword") or "").strip()
        keyword_badge = (
            f'<span class="badge keyword">🔑 {html.escape(keyword)}</span>' if keyword else ""
        )
        cards.append(f"""
    <article class="card">
      <div class="head">
        <span class="num">{i}</span>
        <div>
          <h2><a href="{html.escape(row.get('url') or '')}" target="_blank">{html.escape(row['title'])}</a></h2>
          <div class="meta">{html.escape(row.get('company') or '—')} · {html.escape(row.get('area') or '—')} · {html.escape(_salary_text(row))}</div>
        </div>
        <div class="badges">
          <span class="badge score">{row['score']}</span>
          <span class="badge {wf_class}">{html.escape(wf or 'формат н/д')}</span>
          {manual_badge}
          {keyword_badge}
          <span class="badge stack">{html.escape(row.get('stack') or '—')}</span>
          <span class="badge status">{html.escape(row.get('status') or '')}</span>
        </div>
      </div>
      <div class="actions">
        <a class="btn primary" href="{html.escape(apply_url)}" target="_blank">Откликнуться на hh</a>
        <a class="btn" href="{html.escape(row.get('url') or '')}" target="_blank">Открыть вакансию</a>
        <button class="btn copy" data-target="letter-{i}">Скопировать письмо</button>
      </div>
      <pre id="letter-{i}" class="letter">{html.escape(letter.strip())}</pre>
    </article>""")

    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Очередь откликов hh</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 24px; background: #f5f6f8; color: #1a1a1a; }}
  h1 {{ margin: 0 0 4px; font-size: 22px; }}
  .sub {{ color: #666; font-size: 13px; margin-bottom: 20px; }}
  .card {{ background: #fff; border: 1px solid #e3e5e8; border-radius: 12px; padding: 16px; margin-bottom: 14px; }}
  .head {{ display: flex; gap: 12px; align-items: flex-start; }}
  .num {{ background: #1a1a1a; color: #fff; border-radius: 50%; width: 26px; height: 26px; display: flex; align-items: center; justify-content: center; font-size: 13px; flex: none; }}
  h2 {{ margin: 0; font-size: 17px; }}
  h2 a {{ color: #1a56db; text-decoration: none; }}
  .meta {{ color: #666; font-size: 13px; margin-top: 2px; }}
  .badges {{ margin-left: auto; display: flex; gap: 6px; flex: none; }}
  .badge {{ font-size: 12px; padding: 2px 8px; border-radius: 999px; background: #eef0f3; }}
  .badge.score {{ background: #dcfce7; color: #166534; font-weight: 600; }}
  .badge.stack {{ background: #e0e7ff; color: #3730a3; }}
  .badge.remote {{ background: #d1fae5; color: #065f46; }}
  .badge.office {{ background: #ffedd5; color: #9a3412; }}
  .badge.manual {{ background: #fee2e2; color: #991b1b; }}
  .badge.keyword {{ background: #fef3c7; color: #92400e; }}
  .actions {{ display: flex; gap: 8px; margin: 12px 0; }}
  .btn {{ font-size: 13px; padding: 7px 12px; border-radius: 8px; border: 1px solid #d5d8dd; background: #fff; color: #1a1a1a; text-decoration: none; cursor: pointer; }}
  .btn.primary {{ background: #1a56db; border-color: #1a56db; color: #fff; }}
  .letter {{ white-space: pre-wrap; background: #fafafa; border: 1px dashed #dcdfe4; border-radius: 8px; padding: 12px; font-size: 13px; line-height: 1.5; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
</style>
</head>
<body>
  <h1>Очередь откликов hh</h1>
  <div class="sub">Сформировано {now_iso()} · вакансий: {len(rows)}</div>
  {''.join(cards) if cards else '<p>Очередь пуста. Сначала выполните search и letters.</p>'}
<script>
document.querySelectorAll('button.copy').forEach(function (b) {{
  b.addEventListener('click', function () {{
    var el = document.getElementById(b.dataset.target);
    if (!el) return;
    navigator.clipboard.writeText(el.innerText).then(function () {{
      var old = b.innerText; b.innerText = 'Скопировано'; setTimeout(function () {{ b.innerText = old; }}, 1200);
    }});
  }});
}});
</script>
</body>
</html>"""


def write_reports(cfg: dict, rows: list[dict]) -> tuple[str, str]:
    out_dir = cfg["paths"]["out_dir"]
    os.makedirs(out_dir, exist_ok=True)
    html_path = os.path.join(out_dir, "queue.html")
    md_path = os.path.join(out_dir, "queue.md")
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(build_html(rows))
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(build_markdown(rows))
    return html_path, md_path
