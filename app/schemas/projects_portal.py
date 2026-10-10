"""Client Portal project views (Epic 45, FR-231 / FR-80): ``/api/v1/client-portal/projects``.

Cost-free by construction: no bill / pay rates, cost, margin, budget, burn, vendor or internal notes. Every
response model is registered with ``register_portal_schema("client_user")`` (walked by
``tests/portal/test_field_visibility.py``).
"""

import uuid
from datetime import date, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

from app.core.field_visibility import register_portal_schema
from app.core.money import MoneyOut

__all__ = [
    "DeliverablePortalOut",
    "DeliverableRejectPortalIn",
    "MilestonePortalOut",
    "ProjectPortalDetail",
    "ProjectPortalOut",
    "RolePortalOut",
]


@register_portal_schema("client_user")
class ProjectPortalOut(BaseModel):
    id: uuid.UUID
    human_id: str
    name: str
    status: str
    start_date: date | None
    end_date: date | None
    awaiting_acceptance: int = 0


@register_portal_schema("client_user")
class RolePortalOut(BaseModel):
    id: uuid.UUID
    title: str
    headcount: int
    status: str
    start_date: date | None
    end_date: date | None
    workers: list[str]


@register_portal_schema("client_user")
class MilestonePortalOut(BaseModel):
    id: uuid.UUID
    name: str
    due_date: date | None
    amount: MoneyOut
    status: str
    billed: bool
    invoice_id: uuid.UUID | None = None
    invoice_number: str | None = None


@register_portal_schema("client_user")
class DeliverablePortalOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    milestone_id: uuid.UUID | None
    name: str
    description: str | None
    acceptance_criteria: str | None
    due_date: date | None
    status: str
    submitted_at: datetime | None
    accepted_at: datetime | None
    last_rejection_reason: str | None


@register_portal_schema("client_user")
class ProjectPortalDetail(ProjectPortalOut):
    roles: list[RolePortalOut]
    milestones: list[MilestonePortalOut]
    deliverables: list[DeliverablePortalOut]


class DeliverableRejectPortalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=2000)]
