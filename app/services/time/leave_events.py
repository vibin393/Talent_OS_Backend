"""Domain-local events of Leave & Holidays (Epic 26, A8). The cross-domain ``LeaveRequestDecided`` lives in
``app.events.time``. Frozen, slotted, ``tenant_id`` first, ids only; dispatched after commit."""

import dataclasses
import uuid
from datetime import date


@dataclasses.dataclass(frozen=True, slots=True)
class LeaveRequested:
    tenant_id: uuid.UUID
    leave_request_id: uuid.UUID
    placement_id: uuid.UUID
    candidate_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class LeaveApproved:
    tenant_id: uuid.UUID
    leave_request_id: uuid.UUID
    placement_id: uuid.UUID
    start_date: date
    end_date: date


@dataclasses.dataclass(frozen=True, slots=True)
class LeaveRejected:
    tenant_id: uuid.UUID
    leave_request_id: uuid.UUID
    placement_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class LeaveWithdrawn:
    tenant_id: uuid.UUID
    leave_request_id: uuid.UUID
    placement_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class LeaveCancellationRequested:
    tenant_id: uuid.UUID
    leave_request_id: uuid.UUID
    placement_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class LeaveCancelled:
    tenant_id: uuid.UUID
    leave_request_id: uuid.UUID
    placement_id: uuid.UUID
    start_date: date
    end_date: date


@dataclasses.dataclass(frozen=True, slots=True)
class LeaveBalanceAdjusted:
    tenant_id: uuid.UUID
    candidate_id: uuid.UUID
    leave_type_id: uuid.UUID
    entry_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class LeaveAccrued:
    tenant_id: uuid.UUID
    month: str
    count: int


@dataclasses.dataclass(frozen=True, slots=True)
class HolidayCalendarChanged:
    tenant_id: uuid.UUID
    calendar_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class LeaveClientNotice:
    """``leave_client_mode='notify'``: tell the Client's users that approved leave is coming up."""

    tenant_id: uuid.UUID
    leave_request_id: uuid.UUID
    client_id: uuid.UUID
