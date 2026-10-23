# ruff: file-ignore[line-too-long]
"""Sourcing & Matching Agent API (Epic 56, spec §A6).

| Method + path | Permission |
|---|---|
| ``GET /demands/{id}/agent-shortlist`` (``?version=``) · ``GET /demands/{id}/agent-shortlist/versions`` | ``demand:read`` + ``match:read`` |
| ``POST /demands/{id}/sourcing-runs`` (Idempotency-Key, human only) | ``demand:read`` |
| ``POST /agent-shortlist-entries/{id}/decision`` | ``application:create`` (add) / ``match:write`` (dismiss, restore) |
| ``GET`` / ``PUT /settings/agents/sourcing`` | ``ai:agent_configure`` (PUT human only) |
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from app.core.actors import actor_from_ctx, require_human
from app.core.context import get_context
from app.core.idempotency import IdempotencyGuard, idempotency
from app.core.permissions import Principal, forbidden, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.sourcing_agent import (
    EntryDecisionIn,
    EntryOut,
    ShortlistEnvelope,
    ShortlistVersionOut,
    SourcingRunOut,
    SourcingSettingsIn,
    SourcingSettingsOut,
)
from app.services import sourcing_agent_service as svc

router = APIRouter(tags=["sourcing-agent"])
DB = TenantDBSessionDep
DemandRead = Annotated[Principal, Depends(require("demand:read"))]
MatchRead = Annotated[Principal, Depends(require("match:read"))]
Configure = Annotated[Principal, Depends(require("ai:agent_configure"))]


def _human() -> None:
    require_human(actor_from_ctx(get_context()))


@router.get("/demands/{demand_id}/agent-shortlist", response_model=ShortlistEnvelope)
async def get_shortlist(
    demand_id: uuid.UUID,
    _: DemandRead,
    __: MatchRead,
    db: DB,
    version: Annotated[int | None, Query(ge=1)] = None,
) -> ShortlistEnvelope:
    ctx = get_context()
    shortlist = (
        await svc.get_version(ctx, db, demand_id, version)
        if version is not None
        else await svc.get_current(ctx, db, demand_id)
    )
    enabled = await svc.agent_enabled(db)
    if shortlist is not None:
        from app.services import entity_refs

        run_owner = None
        if shortlist.run_id is not None:
            from app.services import agent_runtime_service as runtime

            try:
                run = await runtime.get_run(ctx, db, shortlist.run_id)
                run_owner = run.owner_user_id
            except Exception:
                run_owner = None
        owner = run_owner or await entity_refs.entity_owner_id(db, "demand", demand_id)
        info = await entity_refs.user(db, owner) if owner else None
        shortlist.owner_user_id, shortlist.owner_name = owner, info.name if info is not None else None
        shortlist.agent_enabled = enabled
    return ShortlistEnvelope(demand_id=demand_id, agent_enabled=enabled, shortlist=shortlist)


@router.get("/demands/{demand_id}/agent-shortlist/versions", response_model=list[ShortlistVersionOut])
async def list_versions(
    demand_id: uuid.UUID, _: DemandRead, __: MatchRead, db: DB
) -> list[ShortlistVersionOut]:
    return await svc.list_versions(get_context(), db, demand_id)


@router.post(
    "/demands/{demand_id}/sourcing-runs", response_model=SourcingRunOut, status_code=status.HTTP_202_ACCEPTED
)
async def start_run(
    demand_id: uuid.UUID,
    principal: DemandRead,
    db: DB,
    idem: Annotated[IdempotencyGuard, Depends(idempotency())],
) -> Any:
    _human()
    ctx = get_context()
    return await idem.run(lambda: svc.start_manual_run(ctx, db, demand_id, owner_user_id=principal.id))


@router.post("/agent-shortlist-entries/{entry_id}/decision", response_model=EntryOut)
async def decide_entry(entry_id: uuid.UUID, body: EntryDecisionIn, principal: DemandRead, db: DB) -> EntryOut:
    _human()
    needed = "application:create" if body.decision == "add_to_pipeline" else "match:write"
    if not principal.has(needed):
        raise forbidden(needed)
    return await svc.decide_entry(get_context(), db, entry_id, body.decision, body.reason)


@router.get("/settings/agents/sourcing", response_model=SourcingSettingsOut)
async def get_settings(_: Configure, db: DB) -> SourcingSettingsOut:
    return await svc.get_settings(get_context(), db)


@router.put("/settings/agents/sourcing", response_model=SourcingSettingsOut)
async def put_settings(body: SourcingSettingsIn, _: Configure, db: DB) -> SourcingSettingsOut:
    _human()
    return await svc.update_settings(get_context(), db, body)
