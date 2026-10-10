"""Go-live import API shapes (Epic 35 A8, FR-185)."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

GoLiveKind = Literal[
    "workers", "placements", "opening_invoices", "leave_balances",
    # Epic 52 (FR-263): Phase 3 kinds (``services/golive/p3_imports.py``).
    "institutions", "assessment_items", "workforce_plan_lines", "projects", "project_roles", "project_assignments",
    "immigration_cases", "referral_history",
]  # fmt: skip
GoLiveStatus = Literal["validating", "previewed", "applied", "failed", "discarded"]
GoLiveRowStatus = Literal["valid", "invalid", "applied", "failed", "skipped"]


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True)


class GoLiveKindOut(_Out):
    kind: GoLiveKind
    label: str
    available: bool
    owner_epic: str | None
    depends_on: list[GoLiveKind]


class GoLiveColumnOut(_Out):
    column: str
    required: bool
    description: str
    example: str


class GoLiveRowError(_Out):
    row: int
    column: str | None
    code: str
    message: str


class GoLiveImportOut(_Out):
    id: uuid.UUID
    kind: GoLiveKind
    status: GoLiveStatus
    row_count: int
    valid_count: int
    error_count: int
    applied_count: int
    failed_count: int
    skipped_count: int
    errors: list[GoLiveRowError]
    created_at: datetime
    applied_at: datetime | None


class GoLiveRowOut(_Out):
    row_no: int
    status: GoLiveRowStatus
    errors: list[GoLiveRowError]
    result_ref: str | None


class GoLiveApplyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skip_invalid: bool = False
    confirm: str
