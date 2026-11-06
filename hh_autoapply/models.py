"""Модель вакансии."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass
class Vacancy:
    id: str
    title: str = ""
    company: str = ""
    area: str = ""
    work_format: str = ""
    url: str = ""
    response_url: str = ""
    employer_id: str = ""
    salary: str = ""
    salary_from: int | None = None
    salary_to: int | None = None
    salary_kind: str = ""          # net (на руки) / gross (до вычета) / ""
    salary_net: int | None = None  # оценка «на руки»
    eligibility: str = "auto"      # auto (можно автоотклик) / manual (только руками)
    published_at: str = ""
    source: str = ""
    board: str = "hh"              # биржа: hh / superjob / habr
    description: str = ""
    score: int = 0
    decision: str = "new"          # new / approve / skip
    reason: str = ""
    stack: str = ""
    funnel: str = ""
    keyword: str = ""
    letter_path: str = ""
    status: str = "new"            # new / lettered / applied / failed / already / test_required / error
    applied_at: str = ""
    note: str = ""
    hits: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_row(cls, row: dict) -> "Vacancy":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        data = {k: v for k, v in row.items() if k in known}
        return cls(**data)

    @property
    def text_blob(self) -> str:
        return f"{self.title} {self.company} {self.description}"
