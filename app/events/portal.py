"""Career Portal / Candidate Portal domain events (Epic 9; dispatched after commit, ids only)."""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class CareerApplicationSubmitted:
    """A public apply was submitted (also when the application already existed: ``already_applied``)."""

    tenant_id: uuid.UUID
    application_id: uuid.UUID
    already_applied: bool = False


@dataclasses.dataclass(frozen=True, slots=True)
class CandidateSignedIn:
    tenant_id: uuid.UUID
    candidate_id: uuid.UUID
    session_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class CandidateProfileUpdated:
    tenant_id: uuid.UUID
    candidate_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class ApplicationWithdrawnByCandidate:
    tenant_id: uuid.UUID
    application_id: uuid.UUID
    candidate_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class DsrRequested:
    """A DSR was filed from the portal. ``export_job_id`` is set for self-service data exports (access)."""

    tenant_id: uuid.UUID
    dsr_id: uuid.UUID
    kind: str
    export_job_id: uuid.UUID | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class DataExportReady:
    tenant_id: uuid.UUID
    job_id: uuid.UUID
    candidate_id: uuid.UUID
