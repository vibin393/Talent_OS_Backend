# ruff: noqa: E501
"""Registrations of the ``skills`` domain (autodiscovered by ``core.hooks``), Epic 37 A6.

* Event subscriptions -> ``app.tasks.skills`` (after commit): ``SkillGraphChanged`` (cache + stale matches),
  ``CandidateSkillsChanged`` (baseline evidence + debounced inference), ``CandidateResumeParsed`` (debounced
  inference), ``AssessmentScored`` (Epic 40 producer) and ``ProjectAssignmentEnded`` (Epic 45 producer).
* ``core.semantic.register_skill_expander(expand)`` so ``skill_match`` with ``expand=true`` uses the graph.
* Human-only declaration for the Epic 52 suite: ``candidate_skill`` -> ``verified`` (recruiter / assessment only).
* Settings hub entry "Skills graph". No notification type (suggestions surface in Candidate 360).
"""

import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.events import subscribe
from app.core.skill_refs import ExpandedSkill, SkillKey

INFER_DEBOUNCE_S = 600
INFER_DELAY_S = 30


def _eager() -> bool:
    from app.tasks.epic5_runtime import is_eager

    return is_eager()


def _schedule_infer(tenant_id: uuid.UUID, candidate_id: uuid.UUID) -> None:
    """One inference task per Candidate per 10 minutes (Redis lock); inline in eager mode."""
    from app.core import dirty_sets
    from app.tasks import skills as tasks
    from app.tasks.runtime import enqueue

    if _eager():
        enqueue(tasks.infer, str(tenant_id), str(candidate_id))
        return
    marked = dirty_sets.mark(f"skills:infer:{tenant_id}:{candidate_id}", (), INFER_DEBOUNCE_S)
    if marked is None:
        enqueue(tasks.infer, str(tenant_id), str(candidate_id))
    elif marked:
        try:
            tasks.infer.apply_async(args=[str(tenant_id), str(candidate_id)], countdown=INFER_DELAY_S)
        except Exception:
            logger.exception("Could not schedule skills.infer")


def _subscribe() -> None:
    from app.events.assessments import AssessmentScored
    from app.events.candidates import CandidateResumeParsed, CandidateSkillsChanged
    from app.events.projects import ProjectAssignmentEnded
    from app.events.skills import SkillGraphChanged
    from app.tasks import skills as tasks
    from app.tasks.runtime import enqueue

    def on_graph_changed(e: SkillGraphChanged) -> None:
        enqueue(tasks.invalidate_graph, str(e.tenant_id), list(e.skill_keys))

    def on_skills_changed(e: CandidateSkillsChanged) -> None:
        enqueue(tasks.baseline, str(e.tenant_id), str(e.candidate_id))
        _schedule_infer(e.tenant_id, e.candidate_id)

    def on_resume_parsed(e: CandidateResumeParsed) -> None:
        _schedule_infer(e.tenant_id, e.candidate_id)

    def on_assessment_scored(e: AssessmentScored) -> None:
        payload = {
            "invitation_id": str(e.invitation_id),
            "candidate_id": str(e.candidate_id),
            "skill_scores": [[str(k), int(v)] for k, v in e.skill_scores],
        }
        enqueue(tasks.assessment_scored, str(e.tenant_id), payload)

    def on_project_ended(e: ProjectAssignmentEnded) -> None:
        payload = {
            "assignment_id": str(e.assignment_id),
            "project_id": str(e.project_id) if e.project_id else None,
            "project_role_id": str(e.project_role_id) if e.project_role_id else None,
            "placement_id": str(e.placement_id) if e.placement_id else None,
            "candidate_id": str(e.candidate_id),
            "ended_on": e.ended_on.isoformat() if e.ended_on else None,
        }
        enqueue(tasks.project_ended, str(e.tenant_id), payload)

    on_graph_changed.__name__ = "skills_on_graph_changed"
    on_skills_changed.__name__ = "skills_on_candidate_skills_changed"
    on_resume_parsed.__name__ = "skills_on_resume_parsed"
    on_assessment_scored.__name__ = "skills_on_assessment_scored"
    on_project_ended.__name__ = "skills_on_project_assignment_ended"
    subscribe(SkillGraphChanged, on_graph_changed)
    subscribe(CandidateSkillsChanged, on_skills_changed)
    subscribe(CandidateResumeParsed, on_resume_parsed)
    subscribe(AssessmentScored, on_assessment_scored)
    subscribe(ProjectAssignmentEnded, on_project_ended)


_subscribe()


# ---------------------------------------------------------------- semantic skill_match expander
async def _expander(
    ctx: RequestContext, db: AsyncSession, refs: Sequence[SkillKey], depth: int
) -> list[ExpandedSkill]:
    from app.services.skills import service

    cfg = await service.matching_settings(db)
    return await service.expand(ctx, db, refs, depth=depth, min_weight=cfg.min_weight)


class _GraphPort:
    """``core.skill_graph_port`` provider: P1 matching / search reach the graph without importing ``skills``."""

    async def graph_version(self, ctx: RequestContext, db: AsyncSession) -> str:
        from app.services.skills import service

        return await service.graph_version(ctx, db)

    async def expand(
        self, ctx: RequestContext, db: AsyncSession, refs: Sequence[SkillKey], *, depth: int, min_weight: Decimal
    ) -> list[ExpandedSkill]:
        from app.services.skills import service

        return await service.expand(ctx, db, refs, depth=depth, min_weight=min_weight)

    async def explain_paths(
        self, ctx: RequestContext, db: AsyncSession, refs: Sequence[SkillKey], depth: int, *, min_weight: Decimal
    ) -> list[Any]:
        from app.services.skills import service

        return list(await service.explain_paths(ctx, db, refs, depth, min_weight=min_weight))

    async def matching_settings(self, db: AsyncSession) -> Any:
        from app.services.skills import service

        return await service.matching_settings(db)

    async def keys_for_ids(self, db: AsyncSession, ids: list[uuid.UUID]) -> list[SkillKey]:
        from app.services.skills import service

        return await service.keys_for_ids(db, ids)


def _register_expander() -> None:
    from app.core.semantic.operators import register_skill_expander
    from app.core.skill_graph_port import register_skill_graph

    register_skill_expander(_expander)
    register_skill_graph(_GraphPort())


_register_expander()


# ---------------------------------------------------------------- human-only, settings hub
def _register_catalogues() -> None:
    from app.services.settings_service import register_settings_entry
    from app.services.workflow.catalogue import register_human_only_transitions

    register_human_only_transitions("candidate_skill", ["verified"])
    register_settings_entry(
        "services",
        "skills_graph",
        "Skills graph",
        "Skill relationships, expansion defaults and inference for matching and search.",
        "/api/v1/skill-graph/settings",
        "skill_graph:manage",
        owner_epic="Epic 37",
    )


_register_catalogues()
