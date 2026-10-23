# ruff: file-ignore[line-too-long]
"""Domain-local events of the campus domain (Epic 43). The frozen ``app/events`` catalogue is untouched; these are
dispatched after commit like any other event (``core.events.emit``). Ids only, no PII."""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class RegistrationReceived:
    tenant_id: uuid.UUID
    drive_id: uuid.UUID
    registration_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class EligibilityDefinitionChanged:
    tenant_id: uuid.UUID
    drive_id: uuid.UUID
    revision: int


@dataclasses.dataclass(frozen=True, slots=True)
class CampusRegistrationsDigest:
    """Daily digest for the drive owner (notification ``campus_registration_received``)."""

    tenant_id: uuid.UUID
    drive_id: uuid.UUID
    owner_id: uuid.UUID
    count: int


@dataclasses.dataclass(frozen=True, slots=True)
class CampusSlotAssigned:
    """A student got an interview slot (notification ``campus_slot_assigned``: interviewers in-app; the student
    gets the P1 interview invite email / ICS)."""

    tenant_id: uuid.UUID
    registration_id: uuid.UUID
    interview_id: uuid.UUID
    interviewer_user_ids: tuple[uuid.UUID, ...]


@dataclasses.dataclass(frozen=True, slots=True)
class QueueTurn:
    """A queue entry was called (notification ``queue_turn``: the Candidate's portal; SMS / WhatsApp go through P2
    messaging in the request)."""

    tenant_id: uuid.UUID
    event_id: uuid.UUID
    entry_id: uuid.UUID
    candidate_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class CampusOffersCreated:
    """Bulk offers were created for a drive (domain-local notification ``campus_offers_created``)."""

    tenant_id: uuid.UUID
    drive_id: uuid.UUID
    owner_id: uuid.UUID
    count: int
