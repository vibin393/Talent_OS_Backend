"""Search projection refresh on candidate events (Epic 5, story 5.1): every candidate change enqueues
``search.tasks.refresh_docs(tenant_id, ids)`` (idempotent upsert). Registered before the embedding /
matching handlers so eager runs see a fresh projection."""

import uuid

from loguru import logger

from app.core.events import subscribe
from app.events.candidates import (
    AIConsentGranted,
    AIConsentWithdrawn,
    CandidateArchived,
    CandidateCreated,
    CandidateMergeUndone,
    CandidateResumeParsed,
    CandidateSkillsChanged,
    CandidatesMerged,
    CandidateUpdated,
)

_registered = False


def enqueue_refresh(tenant_id: uuid.UUID, candidate_ids: list[uuid.UUID]) -> None:
    from app.tasks import search as search_tasks

    try:
        search_tasks.refresh_docs.delay(str(tenant_id), [str(c) for c in candidate_ids])
    except Exception:
        logger.exception("Could not enqueue search refresh for {} candidate(s)", len(candidate_ids))


def _one(e: CandidateCreated) -> None:  # also the other single-candidate events (same shape)
    enqueue_refresh(e.tenant_id, [e.candidate_id])


def _merged(e: CandidatesMerged | CandidateMergeUndone) -> None:
    enqueue_refresh(e.tenant_id, [e.survivor_id, e.merged_id])


def register() -> None:
    global _registered  # idempotent process-wide wiring
    if _registered:
        return
    for event_type in (
        CandidateCreated,
        CandidateUpdated,
        CandidateSkillsChanged,
        CandidateResumeParsed,
        CandidateArchived,
        AIConsentWithdrawn,
        AIConsentGranted,
    ):
        subscribe(event_type, _one)  # type: ignore[arg-type]
    subscribe(CandidatesMerged, _merged)
    subscribe(CandidateMergeUndone, _merged)
    _registered = True
