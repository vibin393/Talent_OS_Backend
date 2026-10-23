# ruff: file-ignore[line-too-long]
"""Workflow action ``assessments.invite`` (Epic 40 A9, foundation 0.7).

Params ``{assessment_id, deadline_days, gating}``; trigger entity types ``application`` (context ``application``) and
``candidate`` (context ``direct``); runs ``invite`` with the workflow actor (section 5.1 allows ``user`` and
``workflow``). Idempotent per rule execution and entity. It performs only ``assessment_invitation -> invited`` --
never a human-only (star) transition, so ``register_action`` accepts it. Automatic invitation on stage entry = a rule
on ``application.stage_changed`` with this action.
"""

import uuid
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import DomainError
from app.schemas.assessments import AssessmentInviteIn
from app.services.assessments.common import now

KEY = "assessments.invite"


class InviteParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assessment_id: uuid.UUID
    deadline_days: int = Field(default=5, ge=1, le=60)
    gating: bool | None = None


async def _plan(ac: Any) -> tuple[uuid.UUID, uuid.UUID | None, str]:
    from app.models.assessments import Assessment
    from app.models.pipeline import Application
    from app.services.workflow.actions import ActionError

    assessment = await ac.db.get(Assessment, ac.params.assessment_id)
    if assessment is None or assessment.status != "published":
        raise ActionError("assessment_not_published", "The assessment is not published")
    if ac.entity_type == "application":
        app = await ac.db.get(Application, ac.entity_id)
        if app is None:
            raise ActionError("application_not_found", "The application no longer exists")
        return app.candidate_id, app.id, assessment.name
    return ac.entity_id, None, assessment.name


async def _run(ac: Any) -> str:
    from app.services.assessments import invitations
    from app.services.workflow.actions import ActionError

    candidate_id, application_id, name = await _plan(ac)
    marker = ac.execution_id or ac.rule_id
    req = AssessmentInviteIn(
        assessment_id=ac.params.assessment_id,
        candidate_id=candidate_id,
        context_kind="application" if application_id else "direct",
        application_id=application_id,
        gating=ac.params.gating,
        deadline_at=now() + timedelta(days=ac.params.deadline_days),
        idempotency_key=f"wf:{marker}:{ac.entity_id}:{ac.params.assessment_id}"[:200],
    )
    try:
        await invitations.invite(ac.ctx, ac.db, req)
    except DomainError as exc:
        raise ActionError(exc.code, exc.message) from exc
    return f"Invited the candidate to the assessment {name}"


async def _dry(ac: Any) -> str:
    _, _, name = await _plan(ac)
    return f"Would invite the candidate to the assessment {name}"


def register() -> None:
    from app.services.workflow import actions

    if KEY in actions.ACTIONS:
        return
    actions.register_action(
        KEY,
        actions.ActionDef(
            "Invite to assessment",
            InviteParams,
            actions._entity_types("application", "candidate"),
            _run,
            _dry,
            entity_types=("application", "candidate"),
            performs_transitions=(("assessment_invitation", "invited"),),
        ),
    )


__all__ = ["KEY", "InviteParams", "register"]
