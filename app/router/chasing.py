# ruff: noqa: E501
"""Compliance chasing and BGV follow-ups API (Epic 62 A8). Internal principals; reads only ("Chase now" / "Follow up
now" use the foundation's ``POST /agent-runs``).

| Method + path | Permission |
|---|---|
| ``GET /compliance/pre-start-risk`` | ``compliance:read`` + ``placement:read`` (placement data scope) |
| ``GET /candidates/{id}/chases`` | ``compliance:read`` (+ candidate visibility) |
| ``GET /placements/{id}/chases`` | ``compliance:read`` + ``placement:read`` (placement visibility) |
| ``GET /bgv/cases/{id}/follow-ups`` | ``bgv:read`` |
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.context import get_context
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.chasing import CaseFollowUpsOut, ChaseOut, RiskListOut, SuppressedChannel
from app.services import chasing_service as svc
from app.services import entity_refs

router = APIRouter(tags=["chasing"])
DB = TenantDBSessionDep
CompRead = Annotated[Principal, Depends(require("compliance:read"))]
PlacementRead = Annotated[Principal, Depends(require("placement:read"))]
BgvRead = Annotated[Principal, Depends(require("bgv:read"))]


async def _out(db: object, rows: list[object]) -> list[ChaseOut]:
    owners: dict[uuid.UUID, str] = {}
    out = []
    for r in rows:
        name = None
        if r.task_id is not None:  # type: ignore[attr-defined]
            from app.models.tasks import Task

            task = await db.get(Task, r.task_id)  # type: ignore[attr-defined]
            if task is not None:
                if task.owner_id not in owners:
                    info = await entity_refs.user(db, task.owner_id)  # type: ignore[arg-type]
                    owners[task.owner_id] = info.name if info else ""
                name = owners[task.owner_id] or None
        out.append(
            ChaseOut(
                id=r.id, candidate_id=r.candidate_id, placement_id=r.placement_id, item_kind=r.item_kind, item_ref=r.item_ref,  # type: ignore[attr-defined]
                item_label=r.item_label, due_basis=r.due_basis, due_on=r.due_on, offset_days=r.offset_days,  # type: ignore[attr-defined]
                channels=list(r.channels), suppressed=[SuppressedChannel(**s) for s in (r.suppressed or [])],  # type: ignore[attr-defined]
                escalated=r.escalated, task_id=r.task_id, escalated_to_name=name, agent_run_id=r.agent_run_id, sent_at=r.sent_at,  # type: ignore[attr-defined]
            )
        )  # fmt: skip
    return out


@router.get("/compliance/pre-start-risk", response_model=RiskListOut)
async def pre_start_risk(
    _: CompRead, __: PlacementRead, db: DB, days: Annotated[int, Query(ge=1, le=60)] = 14,
    client_id: uuid.UUID | None = None, escalated_only: bool = False, candidate: uuid.UUID | None = None,
) -> RiskListOut:  # fmt: skip
    return await svc.risk_list(
        get_context(),
        db,
        days=days,
        client_id=client_id,
        escalated_only=escalated_only,
        candidate_id=candidate,
    )


@router.get("/candidates/{candidate_id}/chases", response_model=list[ChaseOut])
async def candidate_chases(candidate_id: uuid.UUID, _: CompRead, db: DB) -> list[ChaseOut]:
    await entity_refs.require_readable(get_context(), db, "candidate", candidate_id)
    return await _out(db, await svc.chases.list_chases(db, candidate_id=candidate_id))  # type: ignore[arg-type]


@router.get("/placements/{placement_id}/chases", response_model=list[ChaseOut])
async def placement_chases(placement_id: uuid.UUID, _: CompRead, __: PlacementRead, db: DB) -> list[ChaseOut]:
    from app.services.placements.lists import visible

    await visible(get_context(), db, placement_id)
    return await _out(db, await svc.chases.list_chases(db, placement_id=placement_id))  # type: ignore[arg-type]


@router.get("/bgv/cases/{case_id}/follow-ups", response_model=CaseFollowUpsOut)
async def case_follow_ups(case_id: uuid.UUID, _: BgvRead, db: DB) -> CaseFollowUpsOut:
    return await svc.bgv_follow_ups.case_follow_ups(db, case_id, await svc.chases.local_today(db))
