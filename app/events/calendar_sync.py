# ruff: noqa: E501
"""Calendar Sync domain events (Epic 72; dispatched after commit). Ids and enums only -- no PII, no event content."""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class InterviewParticipantResponded:
    tenant_id: uuid.UUID
    interview_id: uuid.UUID
    participant_id: uuid.UUID
    response: str  # needs_action | accepted | tentative | declined


@dataclasses.dataclass(frozen=True, slots=True)
class CalendarChangeDetected:
    tenant_id: uuid.UUID
    interview_id: uuid.UUID
    link_id: uuid.UUID
    kind: str  # moved | deleted | declined
    participant_id: uuid.UUID | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class CalendarChannelStateChanged:
    tenant_id: uuid.UUID
    channel_id: uuid.UUID
    connection_id: uuid.UUID
    status: str


@dataclasses.dataclass(frozen=True, slots=True)
class CalendarSyncKick:
    """Internal: (re)ensure push channels after commit -- for one connection, or every connection of an Interview's
    organiser / internal participants, or (``backfill``) every active connection of the tenant."""

    tenant_id: uuid.UUID
    connection_id: uuid.UUID | None = None
    interview_id: uuid.UUID | None = None
    backfill: bool = False
