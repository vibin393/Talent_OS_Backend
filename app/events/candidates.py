"""Candidate domain events (dispatched after commit, see ``app.core.events``)."""

import dataclasses
import uuid


@dataclasses.dataclass(frozen=True, slots=True)
class CandidatesMerged:
    tenant_id: uuid.UUID
    merge_id: uuid.UUID
    survivor_id: uuid.UUID
    merged_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class CandidateMergeUndone:
    tenant_id: uuid.UUID
    merge_id: uuid.UUID
    survivor_id: uuid.UUID
    merged_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class ImportItemsQueued:
    """Import items were committed with status ``queued``; the handler enqueues ``process_import_item``."""

    tenant_id: uuid.UUID
    batch_id: uuid.UUID
    item_ids: tuple[uuid.UUID, ...]


@dataclasses.dataclass(frozen=True, slots=True)
class ImportCompleted:
    """Every item of a bulk import batch reached a terminal status (Epic 10 notifies the importer)."""

    tenant_id: uuid.UUID
    batch_id: uuid.UUID
    created_by: uuid.UUID | None
    counts: dict[str, int]


# ---------------------------------------------------------------- Epic 5 (search / embeddings / matching)
# Emitted by ``app.services.candidates.change_events`` (a flush listener that sees every ORM write to a
# candidate or its child rows), so every write path (API, parsing, import, merge, privacy) is covered.
@dataclasses.dataclass(frozen=True, slots=True)
class CandidateCreated:
    tenant_id: uuid.UUID
    candidate_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class CandidateUpdated:
    tenant_id: uuid.UUID
    candidate_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class CandidateSkillsChanged:
    tenant_id: uuid.UUID
    candidate_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class CandidateResumeParsed:
    tenant_id: uuid.UUID
    candidate_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class CandidateArchived:
    tenant_id: uuid.UUID
    candidate_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class AIConsentWithdrawn:
    """``candidates.ai_consent_state`` became ``withdrawn`` (Epic 12 flag)."""

    tenant_id: uuid.UUID
    candidate_id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class AIConsentGranted:
    """``candidates.ai_consent_state`` left ``withdrawn`` / ``pending`` for ``granted`` / ``not_required``."""

    tenant_id: uuid.UUID
    candidate_id: uuid.UUID
