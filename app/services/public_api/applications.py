# ruff: file-ignore[line-too-long]
"""Public Applications (Epic 67, spec A4): ``pipeline.common.visible_applications`` (demand scope) for reads,
``pipeline.applications.add_candidates`` / ``transition`` for writes. ★ targets (every stage in
``human_only_transitions()["application"]``: shortlist, submit, select, offer, hire, reject, withdraw) are refused with
403 ``human_only_action`` before the domain is reached; the domain transition table refuses the rest."""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import actor_from_ctx, require_human
from app.core.errors import NotFound
from app.models.pipeline import Application, PipelineStage
from app.schemas.pipeline import ApplicationsCreate, TransitionIn
from app.schemas.public_v1.resources import ApplicationCreateIn, StageMoveIn
from app.services.pipeline import applications as pipeline_apps
from app.services.pipeline import stages as pipeline_stages
from app.services.pipeline.common import visible_applications
from app.services.public_api._core import (
    Order,
    base,
    ctx_principal,
    expose,
    finish_all,
    one,
    page,
    version_of,
)
from app.services.workflow.catalogue import human_only_transitions

OBJECT = "application"


def star_stages() -> frozenset[str]:
    return frozenset(human_only_transitions().get("application", frozenset()))


async def _stage_keys(db: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    return dict((await db.execute(select(PipelineStage.id, PipelineStage.key).where(PipelineStage.id.in_(ids)))).tuples().all())


async def _build(db: AsyncSession, rows: Sequence[Application]) -> list[dict[str, Any]]:
    _, principal = ctx_principal()
    keys = await _stage_keys(db, {r.stage_id for r in rows})
    await expose(principal, "candidate", [r.candidate_id for r in rows])
    return finish_all(principal, [
        {**base(r, OBJECT, human_id=r.human_id), "candidate_id": r.candidate_id, "demand_id": r.demand_id,
         "stage": keys.get(r.stage_id, ""), "stage_id": r.stage_id, "stage_entered_at": r.stage_entered_at,
         "source": r.source, "owner_id": r.owner_id}
        for r in rows
    ])  # fmt: skip


async def list_applications(
    db: AsyncSession, *, limit: int, cursor: str | None, order: Order, demand_id: uuid.UUID | None = None,
    candidate_id: uuid.UUID | None = None, stage: str | None = None, updated_since: datetime | None = None,
) -> dict[str, Any]:  # fmt: skip
    _, principal = ctx_principal()
    stmt = visible_applications(principal).where(Application.archived_at.is_(None))
    if demand_id is not None:
        stmt = stmt.where(Application.demand_id == demand_id)
    if candidate_id is not None:
        stmt = stmt.where(Application.candidate_id == candidate_id)
    if stage is not None:
        stmt = stmt.where(Application.stage_id.in_(select(PipelineStage.id).where(PipelineStage.key == stage)))
    if updated_since is not None:
        stmt = stmt.where(Application.updated_at >= updated_since)
    return await page(db, stmt, sort_col=Application.updated_at, id_col=Application.id, limit=limit, cursor=cursor,
                      order=order, build=lambda rows: _build(db, rows))  # fmt: skip


async def _row(db: AsyncSession, application_id: uuid.UUID) -> Application:
    _, principal = ctx_principal()
    return await one(db, visible_applications(principal).where(Application.id == application_id))


async def get_application(db: AsyncSession, application_id: uuid.UUID) -> dict[str, Any]:
    return (await _build(db, [await _row(db, application_id)]))[0]


async def current_version(db: AsyncSession, application_id: uuid.UUID) -> str:
    return version_of(await _row(db, application_id))


async def create_application(db: AsyncSession, data: ApplicationCreateIn) -> dict[str, Any]:
    ctx, _ = ctx_principal()
    out = await pipeline_apps.add_candidates(
        ctx, db, data.demand_id,
        ApplicationsCreate(candidate_ids=[data.candidate_id], source="public_api", source_ref=data.source_detail),
    )  # fmt: skip
    item = out.items[0]
    if item.application_id is None:  # pragma: no cover - a single add raises instead
        raise NotFound("Application not created")
    return await get_application(db, item.application_id)


async def move_stage(db: AsyncSession, application_id: uuid.UUID, data: StageMoveIn) -> dict[str, Any]:
    ctx, _ = ctx_principal()
    if data.to_stage in star_stages():
        require_human(actor_from_ctx(ctx))  # 403 human_only_action for app / api_key
    await _row(db, application_id)
    await pipeline_stages.ensure_default_stages(db)
    target = (await db.execute(select(PipelineStage).where(PipelineStage.key == data.to_stage))).scalar_one_or_none()
    if target is None:
        raise NotFound("Stage not found")
    await pipeline_apps.transition(ctx, db, application_id, TransitionIn(to_stage_id=target.id, note=data.note))
    await db.flush()
    return await get_application(db, application_id)


__all__ = ["create_application", "current_version", "get_application", "list_applications", "move_stage", "star_stages"]
