# ruff: file-ignore[line-too-long]
"""Workforce planning events (Epic 44).

Epic 36 contract layer (AD-36/AD-39): frozen, slotted dataclasses; ``tenant_id`` first; ids only (no PII,
money as decimal ``str``); dispatched after commit. The events below are frozen
(``tests/contracts/test_events_catalogue.py``); the owner may add domain-local events to this file.

Epic 44 domain-local events (notifications only): ``WorkforcePlanSubmitted`` (approvers),
``WorkforcePlanDecided`` (owner), ``WorkforceSlotFilled`` (owner) and ``ClientForecastSubmitted`` (Account Manager).
"""

import dataclasses
import uuid

__all__ = [
    "ClientForecastSubmitted",
    "PlanLineApproved",
    "WorkforcePlanDecided",
    "WorkforcePlanSubmitted",
    "WorkforceSlotFilled",
]


@dataclasses.dataclass(frozen=True, slots=True)
class PlanLineApproved:
    tenant_id: uuid.UUID
    workforce_plan_id: uuid.UUID
    plan_line_id: uuid.UUID
    client_id: uuid.UUID | None
    headcount: int


# ---------------------------------------------------------------- Epic 44 domain-local
@dataclasses.dataclass(frozen=True, slots=True)
class WorkforcePlanSubmitted:
    tenant_id: uuid.UUID
    workforce_plan_id: uuid.UUID
    revision_no: int
    approval_request_id: uuid.UUID | None
    actor_user_id: uuid.UUID | None


@dataclasses.dataclass(frozen=True, slots=True)
class WorkforcePlanDecided:
    tenant_id: uuid.UUID
    workforce_plan_id: uuid.UUID
    revision_no: int
    outcome: str
    decided_by: uuid.UUID | None


@dataclasses.dataclass(frozen=True, slots=True)
class WorkforceSlotFilled:
    tenant_id: uuid.UUID
    workforce_plan_id: uuid.UUID
    slot_id: uuid.UUID
    placement_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class ClientForecastSubmitted:
    tenant_id: uuid.UUID
    workforce_plan_id: uuid.UUID
    client_id: uuid.UUID
