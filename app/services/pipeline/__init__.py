"""Pipeline service facade (Epic 6: stages, applications, screening, AI screening, submissions, feedback).

Public API for routers and other domains (``from app.services import pipeline as pipeline_service``):

* ``transition_application(ctx, db, application_id, to_stage_id | key, actor, reason_code, note,
  expected_stage_id)`` -- the only writer of an application's stage; ``TRANSITIONS`` is the §5.2 table.
* ``add_candidate`` / ``candidate_ids_for_demand`` -- used by matching (Epic 5) through ``PipelineGateway``.
* ``demand_progress_counts`` (Epic 4) and ``client_submission_summary`` (Client 360).
* ``send_submission`` and ``confirm_rejection`` are human-only (``HUMAN_ONLY``, FR-65).
* ``workflow_snapshot`` -- ids / enums for Epic 10 rule conditions.

Modules: ``stages``, ``applications``, ``screening``, ``ai_summary``, ``submissions``, ``feedback``,
``margin`` (pure), ``resume_pdf``, ``settings``, ``public`` (secure-link pages), ``handlers``.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.services.pipeline import (
    ai_summary,
    applications,
    feedback,
    handlers,
    links,
    margin,
    public,
    resume_pdf,
    screening,
    settings,
    stages,
    submissions,
)
from app.services.pipeline.applications import (
    add_candidate,
    add_candidates,
    candidate_ids_for_demand,
    demand_progress_counts,
    register_transition_guard,  # Epic 41 (additive)
    transition_application,
    workflow_snapshot,
)
from app.services.pipeline.feedback import client_submission_summary, confirm_rejection, record_feedback
from app.services.pipeline.stages import TRANSITIONS, allowed_actors, ensure_default_stages
from app.services.pipeline.submission_facts import (  # Epic 61 (additive): read re-exports
    client_first_submitter,
    counted_submissions,
    get_pipeline_settings,
    prior_submission,
)
from app.services.pipeline.submissions import send_submission


async def reject_application(
    ctx: RequestContext,
    db: AsyncSession,
    application_id: uuid.UUID,
    actor: str,
    reason_code: str = "not_qualified",
    note: str | None = None,
) -> Any:
    """Human-only reject (FR-65): ``transition_application(..., "rejected", actor, reason_code)``."""
    return await transition_application(ctx, db, application_id, "rejected", actor, reason_code, note)  # type: ignore[arg-type]


# FR-65 governance: callables that must refuse ``ai`` and ``workflow`` actors with 409 (Epic 12 suite).
HUMAN_ONLY: dict[str, Any] = {"application.reject": reject_application, "submission.send": send_submission}

_registered = False


async def _application_access(
    ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID, write: bool
) -> bool:
    """Activity / file access hook: may the caller see (or move) this application."""
    from app.core.errors import DomainError
    from app.services.pipeline.common import MOVE, READ, application_for

    try:
        await application_for(ctx, db, application_id, MOVE if write else READ)
    except DomainError:
        return False
    return True


def register() -> None:
    """Idempotent wiring: event handlers, approvals subject, file / activity access hooks, Epic 5 gateway."""
    global _registered  # process-wide wiring
    if _registered:
        return
    handlers.register()
    submissions.register_approvals()
    from app.services import files_service

    files_service.register_access_checker("submission", submissions.submission_access, plural="submissions")
    try:
        from app.services import activity_service
    except ImportError:  # pragma: no cover - Epic 2 absent
        pass
    else:
        activity_service.register_entity("application", _application_access, plural="applications")
        activity_service.register_entity("submission", submissions.submission_access, plural="submissions")
    _register_matching_gateway()
    _registered = True


class PipelineGateway:
    """Epic 5 ``PipelineGateway`` implementation (matches -> applications)."""

    def is_available(self) -> bool:
        return True

    async def add_candidate(
        self,
        ctx: RequestContext,
        db: AsyncSession,
        demand_id: uuid.UUID,
        candidate_id: uuid.UUID,
        source: str = "ai_match",
        match_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        return await add_candidate(
            ctx, db, demand_id, candidate_id, source=source, source_ref=str(match_id) if match_id else None
        )

    async def candidate_ids_for_demand(
        self, ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID
    ) -> set[uuid.UUID]:
        return await candidate_ids_for_demand(ctx, db, demand_id)


def _register_matching_gateway() -> None:
    try:
        from app.services import matching as matching_service
    except ImportError:  # Epic 5 absent
        return
    register_gateway = getattr(matching_service, "register_pipeline_gateway", None)
    if register_gateway is not None:
        register_gateway(PipelineGateway())


register()

__all__ = [
    "HUMAN_ONLY",
    "TRANSITIONS",
    "PipelineGateway",
    "add_candidate",
    "add_candidates",
    "ai_summary",
    "allowed_actors",
    "applications",
    "candidate_ids_for_demand",
    "client_first_submitter",
    "client_submission_summary",
    "confirm_rejection",
    "counted_submissions",
    "demand_progress_counts",
    "ensure_default_stages",
    "feedback",
    "get_pipeline_settings",
    "links",
    "margin",
    "prior_submission",
    "public",
    "record_feedback",
    "register",
    "register_transition_guard",
    "reject_application",
    "resume_pdf",
    "screening",
    "send_submission",
    "settings",
    "stages",
    "submissions",
    "transition_application",
    "workflow_snapshot",
]
