"""26AS / AIS TDS reconciliation (Epic 51, FR-254)."""

import re
import uuid
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

from app.core.money import DecimalStr

Quarter = Literal["Q1", "Q2", "Q3", "Q4"]
StatementKind = Literal["26as", "ais"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TdsReconRunOut(BaseModel):
    id: uuid.UUID
    legal_entity_id: uuid.UUID
    fy: str
    quarter: Quarter
    statement_kind: StatementKind
    source: Literal["upload", "connector"]
    status: Literal["parsing", "ready", "failed", "superseded"]
    line_count: int
    error: str | None
    created_by: uuid.UUID | None
    created_at: datetime
    proposed: int = 0
    confirmed: int = 0
    unmatched: int = 0


class TdsDeductionRef(BaseModel):
    id: uuid.UUID
    client_id: uuid.UUID
    client_name: str | None = None
    section_code: str | None
    inr_amount: DecimalStr
    deduction_date: date
    quarter: str
    invoice_id: uuid.UUID | None = None


class TdsCandidateOut(TdsDeductionRef):
    score: int


class TdsMatchOut(BaseModel):
    id: uuid.UUID
    statement_line_id: uuid.UUID
    tds_deduction_id: uuid.UUID | None
    status: Literal["proposed", "confirmed", "rejected", "unmatched_ack"]
    score: int
    reasons: list[str]
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    note: str | None
    deduction: TdsDeductionRef | None = None


class TdsLineOut(BaseModel):
    id: uuid.UUID
    line_ref: str
    deductor_tan: str
    deductor_name: str
    section: str
    transaction_date: date
    amount_paid: DecimalStr
    tds_deducted: DecimalStr
    tds_deposited: DecimalStr | None
    booking_status: str | None
    client_id: uuid.UUID | None = None
    client_name: str | None = None
    match: TdsMatchOut | None = None


class TdsParseErrorOut(BaseModel):
    line_no: int
    message: str


class TdsReconRunDetailOut(BaseModel):
    run: TdsReconRunOut
    lines: list[TdsLineOut]
    errors: list[TdsParseErrorOut]


class TdsManualMatchIn(_In):
    tds_deduction_id: uuid.UUID
    record_tan_mapping: bool = False


class TdsAcknowledgeIn(_In):
    note: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


class TdsMatchDecisionIn(_In):
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)] | None = None


class TdsRecordedNotDepositedOut(TdsDeductionRef):
    reason: Literal["no_statement_line", "booking_unmatched"]


class TdsExceptionsOut(BaseModel):
    run_id: uuid.UUID | None
    deducted_not_recorded: list[TdsLineOut]
    recorded_not_deposited: list[TdsRecordedNotDepositedOut]


class TdsStatusRowOut(BaseModel):
    client_id: uuid.UUID
    client_name: str | None
    quarter: Quarter
    status: Literal["reconciled", "partially_reconciled", "exceptions", "not_started"]
    recorded_count: int
    recorded_amount: DecimalStr
    matched_count: int
    matched_amount: DecimalStr
    statement_count: int
    statement_amount: DecimalStr


class TdsDeductorTanIn(_In):
    client_id: uuid.UUID
    tan: Annotated[str, StringConstraints(strip_whitespace=True, min_length=10, max_length=10)]

    @field_validator("tan")
    @classmethod
    def _tan(cls, value: str) -> str:
        tan = value.upper()
        if not re.match(r"^[A-Z]{4}[0-9]{5}[A-Z]$", tan):
            msg = "TAN must look like ABCD12345E"
            raise ValueError(msg)
        return tan


class TdsDeductorTanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    client_id: uuid.UUID
    client_name: str | None = None
    tan: str
    created_at: datetime
