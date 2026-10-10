# ruff: noqa: E501
"""Job board distribution domain events (Epic 70, spec A7). Public Events ``board_posting.*`` are built from these."""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class BoardPostingPublished:
    tenant_id: uuid.UUID
    board_posting_id: uuid.UUID
    demand_id: uuid.UUID
    posting_id: uuid.UUID
    board_key: str
    status: str


@dataclasses.dataclass(frozen=True, slots=True)
class BoardPostingStatusChanged:
    tenant_id: uuid.UUID
    board_posting_id: uuid.UUID
    demand_id: uuid.UUID
    posting_id: uuid.UUID
    board_key: str
    from_status: str
    to_status: str


@dataclasses.dataclass(frozen=True, slots=True)
class BoardPostingClosed:
    tenant_id: uuid.UUID
    board_posting_id: uuid.UUID
    demand_id: uuid.UUID
    posting_id: uuid.UUID
    board_key: str
    status: str


@dataclasses.dataclass(frozen=True, slots=True)
class BoardApplicantIngested:
    tenant_id: uuid.UUID
    board_posting_id: uuid.UUID | None
    application_id: uuid.UUID | None
    candidate_id: uuid.UUID | None
    created_candidate: bool
    board_key: str = ""
    notify_email: bool = False
