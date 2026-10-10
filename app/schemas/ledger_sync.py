# ruff: noqa: E501
"""Ledger sync API DTOs (Epic 71, spec A11). Records carry ids, statuses, external ids and sanitised messages only."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MappingKind = Literal["revenue", "gst_output", "tds_receivable", "round_off", "customer", "item", "bank_account", "discount"]
RunKind = Literal["push", "tally_file", "pull_payments", "status", "masters"]


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class LedgerSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_push_on_issue: bool | None = None
    push_payments: bool | None = None
    pull_payments_every_minutes: int | None = Field(default=None, ge=30, le=1440)
    suggestion_stale_days: int | None = Field(default=None, ge=1, le=365)


class LedgerSettingsOut(_Out):
    auto_push_on_issue: bool
    push_payments: bool
    pull_payments_every_minutes: int
    suggestion_stale_days: int


class LedgerMappingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: MappingKind
    key: str = Field(min_length=1, max_length=120)
    external_ref: str = Field(min_length=1, max_length=200)
    external_label: str | None = Field(default=None, max_length=200)


class LedgerMappingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[LedgerMappingIn] = Field(max_length=1000)


class LedgerMappingOut(_Out):
    id: uuid.UUID
    kind: str
    key: str
    external_ref: str
    external_label: str | None


class LedgerMappingPageOut(BaseModel):
    installation_id: uuid.UUID
    legal_entity_id: uuid.UUID
    connector_key: str
    items: list[LedgerMappingOut]
    categories: list[str]
    clients: list[dict[str, Any]]
    sac_codes: list[str]
    bank_accounts: list[dict[str, Any]]


class MastersOut(BaseModel):
    options: dict[str, str]


class LedgerRecordOut(_Out):
    id: uuid.UUID
    installation_id: uuid.UUID
    legal_entity_id: uuid.UUID
    document_kind: str
    document_id: uuid.UUID
    document_version: int
    status: str
    external_ref: str | None
    external_number: str | None
    message: str | None
    synced_at: datetime | None
    skip_reason: str | None
    tally_run_id: uuid.UUID | None
    doc_date: date
    updated_at: datetime
    document_number: str | None = None
    client_name: str | None = None
    amount: Decimal | None = None
    currency: str | None = None


class SkipIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=2, max_length=200)


class RunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: RunKind
    legal_entity_id: uuid.UUID
    installation_id: uuid.UUID | None = None
    from_date: date | None = None
    to_date: date | None = None
    include_masters: bool = False

    @model_validator(mode="after")
    def _range(self) -> "RunIn":
        if self.from_date and self.to_date and self.from_date > self.to_date:
            msg = "from_date must be on or before to_date"
            raise ValueError(msg)
        if self.kind == "tally_file" and not (self.from_date and self.to_date):
            msg = "A Tally file needs a period"
            raise ValueError(msg)
        return self


class LedgerRunOut(_Out):
    id: uuid.UUID
    installation_id: uuid.UUID
    legal_entity_id: uuid.UUID
    kind: str
    from_date: date | None
    to_date: date | None
    include_masters: bool
    status: str
    counts: dict[str, Any]
    file_id: uuid.UUID | None
    created_by: uuid.UUID | None
    created_at: datetime
    finished_at: datetime | None
    created_by_name: str | None = None


class PaymentSuggestionOut(_Out):
    id: uuid.UUID
    installation_id: uuid.UUID
    legal_entity_id: uuid.UUID
    external_ref: str
    invoice_id: uuid.UUID | None
    invoice_number: str
    client_id: uuid.UUID | None
    amount: Decimal
    currency: str
    paid_on: date
    tds_amount: Decimal | None
    mode: str | None
    reference: str | None
    status: str
    payment_id: uuid.UUID | None
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    reject_reason: str | None
    match_note: str | None
    created_at: datetime
    client_name: str | None = None
    prefill: dict[str, Any] | None = None


class RejectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=2, max_length=500)


ReconFlag = Literal["amount_differs", "status_differs", "only_in_talent_os", "only_in_ledger"]


class ReconciliationRowOut(BaseModel):
    invoice_id: uuid.UUID | None
    invoice_number: str
    talent_os_amount: Decimal | None
    talent_os_outstanding: Decimal | None
    talent_os_status: str | None
    ledger_ref: str | None
    ledger_amount: Decimal | None
    ledger_balance: Decimal | None
    ledger_status: str | None
    difference: Decimal | None
    flags: list[str]


class ReconciliationOut(BaseModel):
    legal_entity_id: uuid.UUID
    from_date: date | None
    to_date: date | None
    rows: list[ReconciliationRowOut]
    ledger_available: bool


class DocumentPanelOut(BaseModel):
    document_kind: str
    document_id: uuid.UUID
    record: LedgerRecordOut | None
    connector_key: str | None
