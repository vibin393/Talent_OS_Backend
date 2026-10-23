# ruff: file-ignore[line-too-long]
"""Skills graph Celery tasks (Epic 37). Every task starts with ``tenant_id``; queue ``maintenance`` (AD-54) except
``skills.infer_ai`` (queue ``ai``).

* ``skills.invalidate_graph(tenant_id, skill_keys)`` -- ``SkillGraphChanged``: drops the cached graph and marks the
  matches of open Demands referencing any key stale (all open Demands when ``skill_keys`` is empty).
* ``skills.baseline(tenant_id, candidate_id)`` -- ``CandidateSkillsChanged``: baseline evidence + recompute.
* ``skills.infer(tenant_id, candidate_id)`` / ``skills.infer_ai`` -- graph (and, when enabled, AI) suggestions,
  debounced per Candidate (Redis lock, 10 minutes).
* ``skills.assessment_scored`` / ``skills.project_ended`` -- evidence or suggestions from the foundation events.
"""

import uuid
from datetime import date
from typing import Any

from app.core.celery import celery_app
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.tasks.runtime import enqueue, run

QUEUE = "maintenance"


def _ctx(tid: uuid.UUID) -> RequestContext:
    return RequestContext(tenant_id=tid, actor_kind="system")


async def _invalidate(tid: uuid.UUID, keys: list[str]) -> int:
    from app.services.matching import runs
    from app.services.skills import service as skills_service

    skills_service.invalidate_cache(tid)
    async with tenant_session(tid) as db:
        demand_ids = await skills_service.demands_referencing(db, keys)
        marked = 0
        for demand_id in demand_ids:
            marked += await runs.mark_stale(db, demand_id)
    return marked


@celery_app.task(name="skills.invalidate_graph", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def invalidate_graph(tenant_id: str, skill_keys: list[str] | None = None) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _invalidate(tid, list(skill_keys or [])), tenant_id=tid) or 0)


async def _baseline(tid: uuid.UUID, candidate_id: uuid.UUID) -> int:
    from app.services.skills import service as skills_service

    async with tenant_session(tid) as db:
        n = await skills_service.on_candidate_skills_changed(_ctx(tid), db, candidate_id)
    return n


@celery_app.task(name="skills.baseline", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def baseline(tenant_id: str, candidate_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _baseline(tid, uuid.UUID(str(candidate_id))), tenant_id=tid) or 0)


async def _infer(tid: uuid.UUID, candidate_id: uuid.UUID, *, graph: bool, ai: bool) -> dict[str, Any]:
    from app.services.skills import service as skills_service

    async with tenant_session(tid) as db:
        out = await skills_service.infer(_ctx(tid), db, candidate_id, graph=graph, ai=ai)
    return out


@celery_app.task(name="skills.infer", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def infer(tenant_id: str, candidate_id: str) -> dict[str, Any]:
    """Graph suggestions (``maintenance``); hands the AI part to ``skills.infer_ai`` (queue ``ai``) when enabled."""
    tid = uuid.UUID(str(tenant_id))
    out = dict(
        run(lambda: _infer(tid, uuid.UUID(str(candidate_id)), graph=True, ai=False), tenant_id=tid) or {}
    )
    if out.get("ai_wanted"):
        enqueue(infer_ai, str(tid), str(candidate_id))
    return out


@celery_app.task(name="skills.infer_ai", queue="ai", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def infer_ai(tenant_id: str, candidate_id: str) -> dict[str, Any]:
    tid = uuid.UUID(str(tenant_id))
    return dict(
        run(lambda: _infer(tid, uuid.UUID(str(candidate_id)), graph=False, ai=True), tenant_id=tid) or {}
    )


class _Event:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


async def _assessment(tid: uuid.UUID, payload: dict[str, Any]) -> dict[str, int]:
    from app.services.skills import service as skills_service

    event = _Event(
        invitation_id=uuid.UUID(payload["invitation_id"]),
        candidate_id=uuid.UUID(payload["candidate_id"]),
        skill_scores=tuple((str(k), int(v)) for k, v in payload.get("skill_scores") or ()),
    )
    async with tenant_session(tid) as db:
        out = await skills_service.on_assessment_scored(_ctx(tid), db, event)
    return out


@celery_app.task(name="skills.assessment_scored", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def assessment_scored(tenant_id: str, payload: dict[str, Any]) -> dict[str, int]:
    tid = uuid.UUID(str(tenant_id))
    return dict(run(lambda: _assessment(tid, payload), tenant_id=tid) or {})


def _uuid_or_none(v: Any) -> uuid.UUID | None:
    return uuid.UUID(str(v)) if v else None


async def _project(tid: uuid.UUID, payload: dict[str, Any]) -> dict[str, int]:
    from app.services.skills import service as skills_service

    event = _Event(
        assignment_id=uuid.UUID(payload["assignment_id"]),
        project_id=_uuid_or_none(payload.get("project_id")),
        project_role_id=_uuid_or_none(payload.get("project_role_id")),
        placement_id=_uuid_or_none(payload.get("placement_id")),
        candidate_id=uuid.UUID(payload["candidate_id"]),
        ended_on=date.fromisoformat(payload["ended_on"]) if payload.get("ended_on") else None,
    )
    async with tenant_session(tid) as db:
        out = await skills_service.on_project_assignment_ended(_ctx(tid), db, event)
    return out


@celery_app.task(name="skills.project_ended", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def project_ended(tenant_id: str, payload: dict[str, Any]) -> dict[str, int]:
    tid = uuid.UUID(str(tenant_id))
    return dict(run(lambda: _project(tid, payload), tenant_id=tid) or {})
