"""Domain-local events of the ``time`` domain (A11). The foundation events (``TimesheetSubmitted``,
``TimesheetApproved``, ``TimesheetRejected``, ``TimesheetMissing``) live in ``app.events.time``.
Frozen, slotted, ``tenant_id`` first, ids only; dispatched after commit."""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class TimesheetRecalled:
    tenant_id: uuid.UUID
    timesheet_id: uuid.UUID
    placement_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class TimesheetStepDecided:
    tenant_id: uuid.UUID
    timesheet_id: uuid.UUID
    placement_id: uuid.UUID
    step: str
    decision: str
    actor_kind: str


@dataclasses.dataclass(frozen=True, slots=True)
class TimesheetReopened:
    tenant_id: uuid.UUID
    timesheet_id: uuid.UUID
    placement_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class AdjustmentTimesheetCreated:
    tenant_id: uuid.UUID
    timesheet_id: uuid.UUID
    original_timesheet_id: uuid.UUID
    placement_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class RateAdjustmentsCreated:
    tenant_id: uuid.UUID
    placement_id: uuid.UUID
    segment_id: uuid.UUID
    count: int


@dataclasses.dataclass(frozen=True, slots=True)
class TimesheetImportApplied:
    tenant_id: uuid.UUID
    batch_id: uuid.UUID
    applied_rows: int


@dataclasses.dataclass(frozen=True, slots=True)
class TimesheetReminderDue:
    """A reminder to the Worker (``recipient_kind='candidate'``) or the current approver (``'user'``)."""

    tenant_id: uuid.UUID
    timesheet_id: uuid.UUID
    placement_id: uuid.UUID
    recipient_kind: str
    recipient_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class TimesheetApprovalsDigestDue:
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    pending_count: int
