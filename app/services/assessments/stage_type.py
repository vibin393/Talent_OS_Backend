# ruff: noqa: E501
"""Assessment interview stages (Epic 40 A9): stage-plan validation and the scheduling guard.

The foundation added ``interview_stages.kind = 'assessment'`` and ``assessment_id`` (CHECK ``assessment_id IS NULL OR
kind = 'assessment'``). Registered through the P1 interviews hooks (``plan.register_stage_plan_validator`` /
``register_schedule_guard``):

* kind ``assessment`` requires a **published** ``assessment_id`` (422 ``stage_assessment_required``);
* ``assessment_id`` on another kind -> 422 ``stage_assessment_not_allowed``;
* scheduling an interview in an assessment stage -> 409 ``stage_is_assessment``.

Automatic invitations on stage entry are a workflow rule (``ApplicationStageChanged`` -> ``assessments.invite``,
actor ``workflow``), never a system invite (coordinator decision 10).
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.assessments import Assessment
from app.services.assessments.common import conflict, invalid


async def validate_plan(ctx: RequestContext, db: AsyncSession, stages: list[Any]) -> None:  # noqa: ARG001
    for i, stage in enumerate(stages):
        assessment_id = getattr(stage, "assessment_id", None)
        if stage.kind != "assessment":
            if assessment_id is not None:
                raise invalid(
                    "stage_assessment_not_allowed",
                    "Only assessment stages link an assessment",
                    f"stages[{i}].assessment_id",
                )
            continue
        assessment = await db.get(Assessment, assessment_id) if assessment_id else None
        if assessment is None or assessment.status != "published":
            raise invalid(
                "stage_assessment_required",
                "An assessment stage needs a published assessment",
                f"stages[{i}].assessment_id",
            )


async def schedule_guard(db: AsyncSession, stage: Any) -> None:  # noqa: ARG001
    if getattr(stage, "kind", None) == "assessment":
        raise conflict(
            "stage_is_assessment", "This stage is an assessment: invite the candidate instead of scheduling"
        )


def register() -> None:
    from app.services.interviews import plan

    plan.register_stage_plan_validator(validate_plan)
    plan.register_schedule_guard(schedule_guard)


__all__ = ["register", "schedule_guard", "validate_plan"]
