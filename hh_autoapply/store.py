"""Хранилище состояния в SQLite: что нашли, что откликнули, что осталось."""
from __future__ import annotations

import json
import os
import sqlite3

from .models import Vacancy
from .util import now_iso, today


def _norm_msg(text: str) -> str:
    """Нормализация текста сообщения для сверки «наше это или чужое»."""
    return " ".join((text or "").split()).strip().lower()[:400]

COLUMNS = [
    "id", "title", "company", "area", "work_format", "url", "response_url", "employer_id",
    "salary", "salary_from", "salary_to", "salary_kind", "salary_net", "eligibility",
    "published_at", "source", "description", "score", "decision",
    "reason", "stack", "funnel", "keyword", "letter_path", "status", "applied_at", "note", "hits",
    "board",
]

#: биржа по умолчанию (у старых записей поле пустое — считаем их hh)
DEFAULT_BOARD = "hh"

# Поля, которые обновляются при повторном поиске (остальное — «история»).
REFRESHABLE = [
    "title", "company", "area", "work_format", "url", "response_url", "employer_id",
    "salary", "salary_from", "salary_to", "salary_kind", "salary_net", "eligibility",
    "published_at", "source", "description", "score", "decision", "reason",
    "stack", "funnel", "keyword", "hits",
]


class Store:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        cols = ", ".join(f"{c} TEXT" for c in COLUMNS)
        self.conn.execute(f"CREATE TABLE IF NOT EXISTS vacancies ({cols}, PRIMARY KEY (id))")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS runs (ts TEXT, kind TEXT, info TEXT)"
        )
        # переписка по откликам: что нам ответили и что мы отправляли
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS messages (
                   id TEXT PRIMARY KEY,
                   negotiation_id TEXT,
                   vacancy_id TEXT,
                   direction TEXT,
                   author TEXT,
                   text TEXT,
                   ts TEXT,
                   notified INTEGER DEFAULT 0
               )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS state (
                   key TEXT PRIMARY KEY,
                   value TEXT,
                   updated_at TEXT
               )"""
        )
        # черновики ответов работодателю (отправляются только после подтверждения)
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS drafts (
                   id TEXT PRIMARY KEY,
                   chat_id TEXT,
                   vacancy_id TEXT,
                   in_message_id TEXT,
                   text TEXT,
                   status TEXT,
                   created_at TEXT,
                   sent_at TEXT
               )"""
        )
        # мягкая миграция: добавляем новые колонки в уже существующую базу
        existing = {row[1] for row in self.conn.execute("PRAGMA table_info(vacancies)")}
        for col in COLUMNS:
            if col not in existing:
                self.conn.execute(f"ALTER TABLE vacancies ADD COLUMN {col} TEXT")
        # всё, что было до появления мультибиржевости, — записи hh
        self.conn.execute(
            f"UPDATE vacancies SET board='{DEFAULT_BOARD}' "
            "WHERE board IS NULL OR board='' "
        )
        self.conn.commit()

    @staticmethod
    def _board_sql(board: str | None, column: str = "board") -> tuple[str, tuple]:
        """Фильтр «только эта биржа» (пустое поле считаем hh)."""
        if not board:
            return "", ()
        return f" AND COALESCE({column},'{DEFAULT_BOARD}')=?", (board,)

    def upsert(self, vac: Vacancy) -> None:
        data = vac.to_dict()
        data["hits"] = json.dumps(vac.hits, ensure_ascii=False)
        existing = self.get(vac.id)
        if existing:
            sets = ", ".join(f"{c}=?" for c in REFRESHABLE)
            values = [data.get(c) for c in REFRESHABLE] + [vac.id]
            self.conn.execute(f"UPDATE vacancies SET {sets} WHERE id=?", values)
        else:
            cols = ", ".join(COLUMNS)
            placeholders = ", ".join("?" for _ in COLUMNS)
            self.conn.execute(
                f"INSERT INTO vacancies ({cols}) VALUES ({placeholders})",
                [data.get(c) for c in COLUMNS],
            )
        self.conn.commit()

    def get(self, vid: str) -> dict | None:
        cur = self.conn.execute("SELECT * FROM vacancies WHERE id=?", (vid,))
        row = cur.fetchone()
        return dict(row) if row else None

    def all(self, where: str = "", params: tuple = (), board: str | None = None) -> list[dict]:
        sql = "SELECT * FROM vacancies"
        extra, bparams = self._board_sql(board)
        if where:
            sql += f" WHERE {where}" + extra
            params = tuple(params) + bparams
        elif extra:
            sql += " WHERE 1=1" + extra
            params = bparams
        sql += " ORDER BY score DESC, id DESC"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def set_letter(self, vid: str, path: str) -> None:
        # Статус откликнувшейся вакансии не понижаем: иначе она вернётся
        # в очередь автоотклика и система попробует откликнуться повторно.
        self.conn.execute(
            "UPDATE vacancies SET letter_path=?, "
            "status=CASE WHEN status IN ('applied','already') THEN status ELSE 'lettered' END "
            "WHERE id=?",
            (path, vid),
        )
        self.conn.commit()

    def set_status(self, vid: str, status: str, note: str = "") -> None:
        applied = now_iso() if status == "applied" else None
        if applied:
            self.conn.execute(
                "UPDATE vacancies SET status=?, note=?, applied_at=? WHERE id=?",
                (status, note, applied, vid),
            )
        else:
            self.conn.execute(
                "UPDATE vacancies SET status=?, note=? WHERE id=?", (status, note, vid)
            )
        self.conn.commit()

    def set_decision(self, vid: str, decision: str, reason: str) -> None:
        self.conn.execute(
            "UPDATE vacancies SET decision=?, reason=? WHERE id=?", (decision, reason, vid)
        )
        self.conn.commit()

    def queue(self, limit: int | None = None, only_auto: bool = False,
              board: str | None = None) -> list[dict]:
        sql = (
            "SELECT * FROM vacancies WHERE decision='approve' "
            "AND status IN ('new','lettered','approved')"
        )
        extra, params = self._board_sql(board)
        sql += extra
        if only_auto:
            sql += (
                " AND (eligibility IS NULL OR eligibility='auto')"
                " AND (applied_at IS NULL OR applied_at='')"
            )
        sql += " ORDER BY score DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def applied_today(self, board: str | None = None, funnel: str | None = None,
                      exclude_funnel: str | None = None) -> int:
        extra, params = self._board_sql(board)
        sql = "SELECT COUNT(*) AS c FROM vacancies WHERE status='applied' AND applied_at LIKE ?" + extra
        params = (today() + "%",) + params
        if funnel:
            sql += " AND UPPER(TRIM(COALESCE(funnel,'')))=?"
            params = params + (funnel.upper(),)
        if exclude_funnel:
            sql += " AND UPPER(TRIM(COALESCE(funnel,'')))<>?"
            params = params + (exclude_funnel.upper(),)
        row = self.conn.execute(sql, params).fetchone()
        return int(row["c"]) if row else 0

    def log_run(self, kind: str, info: str = "") -> None:
        self.conn.execute("INSERT INTO runs (ts, kind, info) VALUES (?,?,?)", (now_iso(), kind, info))
        self.conn.commit()

    # ── служебное состояние (пауза, режимы) ─────────────────────────────

    def get_state(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO state (key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, str(value), now_iso()),
        )
        self.conn.commit()

    def is_paused(self) -> bool:
        return self.get_state("paused", "0") == "1"

    def set_paused(self, paused: bool) -> None:
        self.set_state("paused", "1" if paused else "0")

    def is_halted(self) -> bool:
        """Аварийный стоп: автоматика полностью остановлена."""
        return self.get_state("halted", "0") == "1"

    def set_halted(self, halted: bool, reason: str = "") -> None:
        self.set_state("halted", "1" if halted else "0")
        if halted:
            self.set_state("halted_reason", reason)
            self.set_state("halted_at", now_iso())

    def last_runs(self, limit: int = 5) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM runs ORDER BY rowid DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def with_keywords(self) -> list[dict]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM vacancies WHERE keyword IS NOT NULL AND keyword != '' "
                "AND decision='approve' ORDER BY score DESC"
            ).fetchall()
        ]

    # ── защита от повторных откликов ────────────────────────────────────

    def has_responded(self, vid: str) -> bool:
        """Уже откликались на эту вакансию (или hh сообщил, что отклик есть)."""
        row = self.conn.execute(
            "SELECT status, applied_at FROM vacancies WHERE id=?", (str(vid),)
        ).fetchone()
        if not row:
            return False
        return row["status"] in ("applied", "already") or bool(row["applied_at"])

    def responded_ids(self) -> set[str]:
        rows = self.conn.execute(
            "SELECT id FROM vacancies WHERE status IN ('applied','already')"
        ).fetchall()
        return {r["id"] for r in rows}

    # ── переписка ───────────────────────────────────────────────────────

    def add_message(
        self, mid: str, negotiation_id: str, vacancy_id: str,
        direction: str, text: str, ts: str = "", author: str = "",
    ) -> bool:
        """Добавляет сообщение. True — если оно новое."""
        exists = self.conn.execute("SELECT 1 FROM messages WHERE id=?", (mid,)).fetchone()
        if exists:
            return False
        self.conn.execute(
            "INSERT INTO messages (id, negotiation_id, vacancy_id, direction, author, text, ts, notified)"
            " VALUES (?,?,?,?,?,?,?,0)",
            (mid, negotiation_id, vacancy_id, direction, author, text, ts or now_iso()),
        )
        self.conn.commit()
        return True

    def unseen_incoming(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM messages WHERE direction='in' AND notified=0 "
            "ORDER BY ts ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_messages_notified(self, ids: list[str]) -> None:
        if not ids:
            return
        marks = ",".join("?" for _ in ids)
        self.conn.execute(f"UPDATE messages SET notified=1 WHERE id IN ({marks})", ids)
        self.conn.commit()

    def messages_for(self, vacancy_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM messages WHERE vacancy_id=? ORDER BY ts ASC", (str(vacancy_id),)
        ).fetchall()
        return [dict(r) for r in rows]

    def out_texts(self, chat_id: str) -> set[str]:
        """Тексты наших сообщений в чате — чтобы узнавать эхо hh.

        hh иногда отдаёт наше же отправленное сообщение без разметки исходящего,
        и оно попадает в «входящие». Сверка по тексту это лечит.
        """
        rows = self.conn.execute(
            "SELECT text FROM messages WHERE negotiation_id=? AND direction='out'",
            (str(chat_id),),
        ).fetchall()
        return {_norm_msg(r["text"]) for r in rows if (r["text"] or "").strip()}

    # ── черновики ответов работодателю ──────────────────────────────────

    def add_draft(self, draft_id: str, chat_id: str, vacancy_id: str,
                  in_message_id: str, text: str, status: str = "pending") -> bool:
        exists = self.conn.execute("SELECT 1 FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if exists:
            return False
        self.conn.execute(
            "INSERT INTO drafts (id, chat_id, vacancy_id, in_message_id, text, status, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (draft_id, str(chat_id), str(vacancy_id), str(in_message_id), text, status, now_iso()),
        )
        self.conn.commit()
        return True

    def pending_drafts(self, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM drafts WHERE status='pending' ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_draft(self, draft_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        return dict(row) if row else None

    def get_draft_for_message(self, in_message_id: str, statuses=("pending", "sent")) -> dict | None:
        marks = ",".join("?" for _ in statuses)
        row = self.conn.execute(
            f"SELECT * FROM drafts WHERE in_message_id=? AND status IN ({marks}) LIMIT 1",
            (str(in_message_id), *statuses),
        ).fetchone()
        return dict(row) if row else None

    def set_draft_status(self, draft_id: str, status: str) -> None:
        sent = now_iso() if status == "sent" else None
        self.conn.execute(
            "UPDATE drafts SET status=?, sent_at=? WHERE id=?", (status, sent, draft_id)
        )
        self.conn.commit()

    def all_messages(self, direction: str | None = "in") -> list[dict]:
        # Порядок — по порядку добавления (rowid): время hh приходит то как
        # "13:01", то как ISO, и строковая сортировка по ts путает порядок.
        if direction:
            rows = self.conn.execute(
                "SELECT rowid AS _seq, * FROM messages WHERE direction=? ORDER BY rowid",
                (direction,),
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT rowid AS _seq, * FROM messages ORDER BY rowid").fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        rows = self.conn.execute("SELECT status, COUNT(*) c FROM vacancies GROUP BY status").fetchall()
        by_status = {r["status"]: r["c"] for r in rows}
        rows = self.conn.execute("SELECT decision, COUNT(*) c FROM vacancies GROUP BY decision").fetchall()
        by_decision = {r["decision"]: r["c"] for r in rows}
        return {"by_status": by_status, "by_decision": by_decision, "applied_today": self.applied_today()}

    def close(self) -> None:
        self.conn.close()
