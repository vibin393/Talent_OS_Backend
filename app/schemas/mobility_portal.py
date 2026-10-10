"""Worker-safe internal mobility schemas (Epic 41, ``/api/v1/portal/work/opportunities*``).

Only board fields: no client, no rates / compensation, no hiring-manager contact, no internal user ids.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.schemas.mobility import MobilityStatus, OpeningSkill


class WorkerOpeningOut(BaseModel):
    demand_id: uuid.UUID
    title: str
    location_text: str | None
    work_mode: str | None
    employment_type: str | None
    skills: list[OpeningSkill]
    visibility: Literal["internal", "both"]
    opened_at: datetime
    closes_at: datetime | None
    eligible: bool
    reasons: list[str]
    fit_pct: int | None
    fit_reasons: list[str]
    application_id: uuid.UUID | None = None
    application_status: MobilityStatus | None = None


class WorkerOpeningDetailOut(WorkerOpeningOut):
    summary_html: str
    disclosure_text: str
    can_apply: bool


class WorkerOpeningPage(BaseModel):
    items: list[WorkerOpeningOut]
    total: int
    page: int
    page_size: int
    board_enabled: bool


class WorkerApplicationOut(BaseModel):
    id: uuid.UUID
    demand_id: uuid.UUID
    demand_title: str
    status: MobilityStatus
    stage_name: str | None
    rejection_reason: str | None
    submitted_at: datetime
    decided_at: datetime | None
    can_withdraw: bool


# Epic 52 (phase-close gap, tests/portal/test_field_visibility.py): Worker portal response models of the internal
# job board are registered under the worker field-visibility rules.
from app.core.field_visibility import register_portal_schema  # noqa: E402

for _model in (WorkerOpeningOut, WorkerOpeningDetailOut, WorkerOpeningPage, WorkerApplicationOut):
    register_portal_schema("worker")(_model)
