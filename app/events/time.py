"""Timesheets and leave (Epics 25, 26).

Epic 13 contract layer (AD-36): frozen, slotted dataclasses; ``tenant_id`` first; ids only (no PII);
dispatched after commit. Field changes need a spine amendment (``tests/contracts/test_events_catalogue.py``).
"""

import dataclasses
import uuid
from datetime import date

__all__ = [
    "LeaveRequestDecided",
    "TimesheetApproved",
    "TimesheetMissing",
    "TimesheetRejected",
    "TimesheetSubmitted",
]


@dataclasses.dataclass(frozen=True, slots=True)
class TimesheetSubmitted:
    tenant_id: uuid.UUID
    timesheet_id: uuid.UUID
    placement_id: uuid.UUID
    period_start: date
    period_end: date


@dataclasses.dataclass(frozen=True, slots=True)
class TimesheetApproved:
    tenant_id: uuid.UUID
    timesheet_id: uuid.UUID
    placement_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class TimesheetRejected:
    tenant_id: uuid.UUID
    timesheet_id: uuid.UUID
    placement_id: uuid.UUID
    step_name: str


@dataclasses.dataclass(frozen=True, slots=True)
class TimesheetMissing:
    tenant_id: uuid.UUID
    timesheet_id: uuid.UUID
    placement_id: uuid.UUID
    period_end: date


@dataclasses.dataclass(frozen=True, slots=True)
class LeaveRequestDecided:
    tenant_id: uuid.UUID
    leave_request_id: uuid.UUID
    candidate_id: uuid.UUID
    outcome: str
