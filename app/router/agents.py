# ruff: noqa: E501
"""AI agents (Epic 53, spec 0.16): enablement, kill switch, agent catalogue / configuration, runs and payload reads.

Permissions (FR-328): ``ai:govern`` (console reads), ``ai:agent_configure`` (enable, modes, routing, budgets),
``ai:kill_switch``, ``ai:payload_read`` (stored content, with a reason, audited). "Human only" routes refuse API keys and
automated actors (403 ``human_only_action``, FR-329)."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from app.core.actors import actor_from_ctx, require_human
from app.core.context import get_context
from app.core.idempotency import IdempotencyGuard, idempotency
from app.core.pagination import Page
from app.core.permissions import Principal, require, require_any
from app.dependencies.auth import CurrentUserDep, TenantDBSessionDep
from app.schemas.agentic import (
    ActionPolicyIn,
    AgentCatalogOut,
    AgentConfigIn,
    AgentEnabledIn,
    AgenticStateOut,
    EnablementIn,
    KillSwitchIn,
    PayloadReadIn,
    RunCancelIn,
    RunOut,
    RunPayloadOut,
    RunQuery,
    RunRef,
    RunStartIn,
    RunSummary,
    ToolCatalogOut,
)
from app.services import agent_runtime_service as svc

router = APIRouter(tags=["ai-agents"])
DB = TenantDBSessionDep
Govern = Annotated[Principal, Depends(require("ai:govern"))]
GovernOrConfigure = Annotated[Principal, Depends(require_any("ai:govern", "ai:agent_configure"))]
Configure = Annotated[Principal, Depends(require("ai:agent_configure"))]
Kill = Annotated[Principal, Depends(require("ai:kill_switch"))]
PayloadRead = Annotated[Principal, Depends(require("ai:payload_read"))]


def _human() -> None:
    require_human(actor_from_ctx(get_context()))


@router.get("/ai/agentic", response_model=AgenticStateOut)
async def agentic_state(_: GovernOrConfigure, db: DB) -> AgenticStateOut:
    return await svc.agentic_state(get_context(), db)


@router.post("/ai/agentic/enable", response_model=AgenticStateOut)
async def enable_agentic(body: EnablementIn, _: Configure, db: DB) -> AgenticStateOut:
    _human()
    return await svc.acknowledge_enablement(get_context(), db, notice_version=body.notice_version)


@router.post("/ai/kill-switch", response_model=AgenticStateOut)
async def kill_switch(body: KillSwitchIn, _: Kill, db: DB) -> AgenticStateOut:
    _human()
    return await svc.set_kill_switch(get_context(), db, on=body.on, reason=body.reason)


@router.get("/agents", response_model=list[AgentCatalogOut])
async def list_agents(_: GovernOrConfigure, db: DB) -> list[AgentCatalogOut]:
    return await svc.get_agents(get_context(), db)


@router.get("/agents/tools", response_model=list[ToolCatalogOut])
async def tools(_: Govern) -> list[ToolCatalogOut]:
    return svc.tool_catalogue()


@router.put("/agents/{key}/enabled", response_model=AgentCatalogOut)
async def set_enabled(key: str, body: AgentEnabledIn, _: Configure, db: DB) -> AgentCatalogOut:
    _human()
    return await svc.set_agent_enabled(get_context(), db, key, body.enabled)


@router.patch("/agents/{key}", response_model=AgentCatalogOut)
async def update_agent(key: str, body: AgentConfigIn, _: Configure, db: DB) -> AgentCatalogOut:
    _human()
    return await svc.update_config(get_context(), db, key, body)


@router.put("/agents/{key}/actions/{tool_key}", response_model=AgentCatalogOut)
async def set_action(key: str, tool_key: str, body: ActionPolicyIn, _: Configure, db: DB) -> AgentCatalogOut:
    _human()
    return await svc.set_action_policy(get_context(), db, key, tool_key, body)


# ---------------------------------------------------------------- runs
@router.get("/agent-runs", response_model=Page[RunSummary])
async def list_runs(_: CurrentUserDep, db: DB, q: Annotated[RunQuery, Query()]) -> Page[RunSummary]:
    return await svc.list_runs(get_context(), db, q)


@router.get("/agent-runs/{run_id}", response_model=RunOut)
async def get_run(run_id: uuid.UUID, _: CurrentUserDep, db: DB) -> RunOut:
    return await svc.get_run(get_context(), db, run_id)


@router.post("/agent-runs/{run_id}/payload", response_model=RunPayloadOut)
async def read_payload(run_id: uuid.UUID, body: PayloadReadIn, _: PayloadRead, db: DB) -> RunPayloadOut:
    _human()
    return await svc.read_payload(get_context(), db, run_id, reason=body.reason)


@router.post("/agent-runs", response_model=RunRef, status_code=status.HTTP_202_ACCEPTED)
async def start_run(
    body: RunStartIn,
    principal: CurrentUserDep,
    db: DB,
    idem: Annotated[IdempotencyGuard, Depends(idempotency())],
) -> Any:
    _human()
    from app.core.errors import NotFound
    from app.services import entity_refs

    ctx = get_context()
    if not await entity_refs.can_read(ctx, db, body.subject_type, body.subject_id):
        raise NotFound("Resource not found")
    return await idem.run(
        lambda: svc.start_run(
            ctx,
            db,
            agent_key=body.agent_key,
            trigger_kind="manual",
            subject=(body.subject_type, body.subject_id),
            owner_user_id=principal.id,
        )
    )


@router.post("/agent-runs/{run_id}/cancel", response_model=RunRef)
async def cancel_run(run_id: uuid.UUID, body: RunCancelIn, _: CurrentUserDep, db: DB) -> RunRef:
    _human()
    return await svc.cancel_run(get_context(), db, run_id, reason=body.reason, reason_code=body.reason_code)
