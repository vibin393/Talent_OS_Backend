"""Event handlers for embeddings and matching refresh (Epic 5, AD-9; refresh <= 5 min).

* Candidate created / updated / skills / resume / consent granted / merge undone -> embed (debounced 20 s) and
  re-score against every Open demand (debounced 20 s). ``CandidateEmbeddingUpdated`` -> re-score.
* Archived / merged-away / ``AIConsentWithdrawn`` -> delete embeddings and matches (immediately).
* ``DemandOpened`` -> scoring run (trigger ``opened``, 30 s delay) -> ``RediscoveryCompleted`` on completion.
  ``DemandRequirementsChanged`` / re-open -> run (debounced 30 s). ``DemandStatusChanged`` away from Open ->
  matches marked stale.
* ``MatchRunRequested`` -> ``run_for_demand``; ``MatchRunCompleted`` -> explanations;
  ``RediscoveryCompleted`` -> demand timeline entry + notification intents.

Debounce: ids go into Redis sets (``emb:dirty:<tenant>``, ``match:dirty:cand:<tenant>``,
``match:dirty:demand:<tenant>``); one delayed drain task per window. In eager mode (dev/tests) everything runs
inline; when Redis is unreachable the work is enqueued directly. ``matching.tasks.sweep_dirty`` (beat) drains
leftovers.
"""

import uuid
from collections.abc import Callable
from typing import Any

from loguru import logger

from app.core import dirty_sets
from app.core.events import subscribe
from app.events.ai import CandidateEmbeddingUpdated
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
from app.events.demands import DemandOpened, DemandRequirementsChanged, DemandStatusChanged
from app.events.matching import (
    MatchAddedToPipeline,
    MatchDismissed,
    MatchRestored,
    MatchRunCompleted,
    MatchRunRequested,
    RediscoveryCompleted,
)

CANDIDATE_DEBOUNCE_S = 20
DEMAND_DEBOUNCE_S = 30
REOPEN_FROM = frozenset({"on_hold", "filled"})

_registered = False


def _eager() -> bool:
    from app.tasks.epic5_runtime import is_eager

    return is_eager()


def _safe(fn: Callable[[], Any], what: str) -> None:
    try:
        fn()
    except Exception:
        logger.exception("Could not enqueue {}", what)


def _debounced(
    key: str, tenant_id: uuid.UUID, ids: list[uuid.UUID], drain: Any, direct: Callable[[], Any], delay: int
) -> None:
    if _eager():
        direct()
        return
    marked = dirty_sets.mark(key, [str(i) for i in ids], delay)
    if marked is None:
        direct()
    elif marked:
        drain.apply_async(args=[str(tenant_id)], countdown=delay)


def embed_later(tenant_id: uuid.UUID, ids: list[uuid.UUID]) -> None:
    from app.tasks import ai as ai_tasks

    _safe(
        lambda: _debounced(
            ai_tasks.dirty_key(tenant_id),
            tenant_id,
            ids,
            ai_tasks.embed_dirty,
            lambda: ai_tasks.embed_candidates.delay(str(tenant_id), [str(i) for i in ids]),
            CANDIDATE_DEBOUNCE_S,
        ),
        "candidate embedding",
    )


def score_later(tenant_id: uuid.UUID, ids: list[uuid.UUID]) -> None:
    from app.tasks import matching as tasks

    _safe(
        lambda: _debounced(
            tasks.cand_dirty_key(tenant_id),
            tenant_id,
            ids,
            tasks.score_dirty,
            lambda: tasks.score_candidates.delay(str(tenant_id), [str(i) for i in ids]),
            CANDIDATE_DEBOUNCE_S,
        ),
        "candidate re-scoring",
    )


def forget(tenant_id: uuid.UUID, ids: list[uuid.UUID]) -> None:
    from app.tasks import ai as ai_tasks
    from app.tasks import matching as tasks

    str_ids = [str(i) for i in ids]
    _safe(lambda: ai_tasks.delete_candidate_embeddings.delay(str(tenant_id), str_ids), "embedding deletion")
    _safe(lambda: tasks.delete_candidate_matches.delay(str(tenant_id), str_ids), "match deletion")


def run_later(tenant_id: uuid.UUID, demand_id: uuid.UUID, trigger: str) -> None:
    from app.tasks import matching as tasks

    if trigger == "opened":
        countdown = None if _eager() else DEMAND_DEBOUNCE_S
        _safe(
            lambda: tasks.start_run.apply_async(
                args=[str(tenant_id), str(demand_id), trigger], countdown=countdown
            ),
            "match run",
        )
        return
    _safe(
        lambda: _debounced(
            tasks.demand_dirty_key(tenant_id),
            tenant_id,
            [demand_id],
            tasks.start_dirty_runs,
            lambda: tasks.start_run.delay(str(tenant_id), str(demand_id), trigger),
            DEMAND_DEBOUNCE_S,
        ),
        "match run",
    )


# ================================================================ handlers
def refresh_later(tenant_id: uuid.UUID, ids: list[uuid.UUID]) -> None:
    """Embed then re-score: one combined task in eager mode, two debounced tasks otherwise."""
    if _eager():
        from app.tasks import matching as tasks

        str_ids = [str(i) for i in ids]
        _safe(lambda: tasks.refresh_candidates.delay(str(tenant_id), str_ids), "candidate refresh")
        return
    embed_later(tenant_id, ids)
    score_later(tenant_id, ids)


def _changed(e: Any) -> None:
    refresh_later(e.tenant_id, [e.candidate_id])


def _gone(e: Any) -> None:
    forget(e.tenant_id, [e.candidate_id])


def _merged(e: CandidatesMerged) -> None:
    forget(e.tenant_id, [e.merged_id])
    refresh_later(e.tenant_id, [e.survivor_id])


def _merge_undone(e: CandidateMergeUndone) -> None:
    refresh_later(e.tenant_id, [e.survivor_id, e.merged_id])


def _embedding_updated(e: CandidateEmbeddingUpdated) -> None:
    if not _eager():  # eager refreshes score right after embedding (``refresh_candidates``)
        score_later(e.tenant_id, [e.candidate_id])


def _demand_opened(e: DemandOpened) -> None:
    run_later(e.tenant_id, e.demand_id, "opened")


def _requirements_changed(e: DemandRequirementsChanged) -> None:
    run_later(e.tenant_id, e.demand_id, "requirements_changed")


def _status_changed(e: DemandStatusChanged) -> None:
    from app.tasks import matching as tasks

    if e.from_status == "open" and e.to_status != "open":
        _safe(lambda: tasks.mark_stale.delay(str(e.tenant_id), str(e.demand_id)), "stale marking")
    elif e.to_status == "open" and e.from_status in REOPEN_FROM:
        run_later(e.tenant_id, e.demand_id, "requirements_changed")


def _run_requested(e: MatchRunRequested) -> None:
    from app.tasks import matching as tasks

    _safe(lambda: tasks.run_for_demand.delay(str(e.tenant_id), str(e.demand_id), str(e.run_id)), "match run")


def _run_completed(e: MatchRunCompleted) -> None:
    from app.tasks import matching as tasks

    _safe(lambda: tasks.explain_matches.delay(str(e.tenant_id), str(e.demand_id)), "match explanations")


def _rediscovery(e: RediscoveryCompleted) -> None:
    from app.tasks import matching as tasks

    _safe(
        lambda: tasks.rediscovery_timeline.delay(
            str(e.tenant_id), str(e.demand_id), dict(e.funnel), [str(u) for u in e.assignee_ids]
        ),
        "rediscovery timeline",
    )


def _log(e: Any) -> None:
    logger.info("{}: match={} demand={}", type(e).__name__, e.match_id, e.demand_id)


def register() -> None:
    global _registered  # idempotent process-wide wiring
    if _registered:
        return
    from app.services.search import handlers as search_handlers

    search_handlers.register()  # projection first: runs read it
    for event_type in (
        CandidateCreated,
        CandidateUpdated,
        CandidateSkillsChanged,
        CandidateResumeParsed,
        AIConsentGranted,
    ):
        subscribe(event_type, _changed)  # type: ignore[arg-type]
    subscribe(CandidateArchived, _gone)
    subscribe(AIConsentWithdrawn, _gone)
    subscribe(CandidatesMerged, _merged)
    subscribe(CandidateMergeUndone, _merge_undone)
    subscribe(CandidateEmbeddingUpdated, _embedding_updated)
    subscribe(DemandOpened, _demand_opened)
    subscribe(DemandRequirementsChanged, _requirements_changed)
    subscribe(DemandStatusChanged, _status_changed)
    subscribe(MatchRunRequested, _run_requested)
    subscribe(MatchRunCompleted, _run_completed)
    subscribe(RediscoveryCompleted, _rediscovery)
    for event_type in (MatchDismissed, MatchRestored, MatchAddedToPipeline):
        subscribe(event_type, _log)  # type: ignore[arg-type]
    _registered = True
