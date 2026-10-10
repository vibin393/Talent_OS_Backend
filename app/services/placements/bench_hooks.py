"""Epic 24 registrations, called once from ``placements/hooks.py`` (autodiscovered).

* Event subscriptions -> ``placements.bench_event`` task (queue ``interactive``; idempotent, AD-9):
  ``PlacementEnded``, ``PlacementActivated``, ``SubmissionCreated``, ``ApplicationStageChanged`` (to
  ``rejected|withdrawn``), ``MatchRunCompleted``, ``BenchEntryCreated``.
* Notification type ``bench.strong_match`` (the demand's assigned recruiters), ``BenchStrongMatchFound``.
* Workflow condition ``bench.matches_reviewed`` (R6) and triggers ``placements.bench_entry_created`` /
  ``placements.bench_status_changed``.
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.events import subscribe

_done = False


def _enqueue(event: Any, kind: str, payload: dict[str, Any]) -> None:
    from app.tasks import placements as tasks
    from app.tasks.runtime import enqueue

    enqueue(tasks.bench_event, str(event.tenant_id), kind, payload)


def _payload(event: Any) -> dict[str, Any]:
    from app.services.notifications_service import event_payload

    return dict(event_payload(event))


def _on_ended(event: Any) -> None:
    if getattr(event, "bench_eligible", False):
        _enqueue(event, "placement_ended", _payload(event))


def _on_activated(event: Any) -> None:
    _enqueue(event, "placement_activated", _payload(event))


def _on_submission(event: Any) -> None:
    _enqueue(event, "submission_created", {"application_id": str(event.application_id)})


def _on_stage(event: Any) -> None:
    from app.services.placements.bench import CLOSED_STAGES

    if event.to_key == "submitted":  # proposed to the client (the submission stage)
        _enqueue(event, "submission_created", {"application_id": str(event.application_id)})
    if event.to_key in CLOSED_STAGES:
        _enqueue(
            event,
            "application_closed",
            {"application_id": str(event.application_id), "candidate_id": str(event.candidate_id)},
        )


def _on_match_run(event: Any) -> None:
    if getattr(event, "status", "completed") == "completed":
        _enqueue(event, "match_run", {"demand_id": str(event.demand_id)})


def _on_created(event: Any) -> None:
    _enqueue(event, "entry_created", {"candidate_id": str(event.candidate_id)})


async def _assignees(db: AsyncSession, payload: Any) -> set[uuid.UUID]:
    from app.services.demands import matching_port as demands

    raw = payload.get("demand_id")
    if not raw:
        return set()
    return set(await demands.get_assignees(db, uuid.UUID(str(raw))))


async def _render(db: AsyncSession, payload: Any) -> Any:
    from sqlalchemy import select

    from app.models.candidates import Candidate
    from app.services.notifications_service import Content

    did = payload.get("demand_id")
    cid = payload.get("candidate_id")
    name = None
    if cid:
        name = (
            await db.execute(select(Candidate.full_name).where(Candidate.id == uuid.UUID(str(cid))))
        ).scalar_one_or_none()
    return Content(
        title=f"Strong bench match: {name or 'a bench worker'} scores {payload.get('overall')} on a demand"[
            :300
        ],
        entity_type="demand",
        entity_id=uuid.UUID(str(did)) if did else None,
        link_path=f"/demands/{did}?tab=matching" if did else None,
    )


async def _matches_reviewed(ctx: RequestContext, db: AsyncSession, demand_id: Any = None, **_: Any) -> bool:
    from app.services.placements import bench_matching

    if demand_id is None:
        return True
    return await bench_matching.matches_reviewed(ctx, db, uuid.UUID(str(demand_id)))


def register() -> None:
    global _done
    if _done:
        return
    _done = True
    from app.events.matching import MatchRunCompleted
    from app.events.pipeline import ApplicationStageChanged, SubmissionCreated
    from app.events.placements import PlacementActivated, PlacementEnded
    from app.services.placements.bench_events import BenchEntryCreated, BenchStrongMatchFound

    subscribe(PlacementEnded, _on_ended)
    subscribe(PlacementActivated, _on_activated)
    subscribe(SubmissionCreated, _on_submission)
    subscribe(ApplicationStageChanged, _on_stage)
    subscribe(MatchRunCompleted, _on_match_run)
    subscribe(BenchEntryCreated, _on_created)

    from app.services.notifications_service import (
        NotificationType,
        register_event_source,
        register_notification_type,
    )

    try:
        register_notification_type(
            "bench.strong_match",
            NotificationType("Strong bench match", "Workforce", "both", False, _assignees, _render),
        )
        register_event_source(BenchStrongMatchFound, "bench.strong_match")
    except Exception:
        logger.debug("bench.strong_match already registered")

    from app.services.workflow.registry import Trigger, register_condition, register_trigger

    try:
        register_condition("bench.matches_reviewed", _matches_reviewed)
    except Exception:
        logger.debug("bench.matches_reviewed already registered")
    for trig in (
        Trigger(
            "placements.bench_entry_created",
            "Worker added to the bench",
            "app.services.placements.bench_events",
            "BenchEntryCreated",
            "candidate",
            "candidate_id",
        ),
        Trigger(
            "placements.bench_status_changed",
            "Bench status changed",
            "app.events.placements",
            "BenchEntryStatusChanged",
            "candidate",
            "candidate_id",
        ),
    ):
        try:
            register_trigger(trig.key, trig)
        except Exception:
            logger.debug("trigger {} already registered", trig.key)
