"""``tds_statement`` source port (AD-29, Epic 36 contract 0.8; owned by Epic 51): the TDS credit statement
(Form 26AS / TRACES) for a TAN and quarter. ``manual`` asks for a TRACES upload (parsed by Epic 51); ``fake``
returns deterministic lines. Every adapter also implements the generic ``ConnectorPort``. Signatures are
frozen."""

import dataclasses
from datetime import date
from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable

__all__ = ["Quarter", "TdsStatementDTO", "TdsStatementLineDTO", "TdsStatementSource"]

Quarter = Literal["Q1", "Q2", "Q3", "Q4"]


@dataclasses.dataclass(frozen=True, slots=True)
class TdsStatementLineDTO:
    line_ref: str
    deductor_tan: str
    deductor_name: str
    section: str
    transaction_date: date
    amount_paid: Decimal
    tds_deducted: Decimal
    booking_status: str


@dataclasses.dataclass(frozen=True, slots=True)
class TdsStatementDTO:
    tan: str
    fy: str
    quarter: Quarter
    lines: tuple[TdsStatementLineDTO, ...]


@runtime_checkable
class TdsStatementSource(Protocol):
    key: str

    async def fetch(self, tan: str, fy: str, quarter: Quarter) -> TdsStatementDTO: ...
