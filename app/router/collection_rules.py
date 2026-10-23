# ruff: file-ignore[line-too-long]
"""Collection rules, Collections Agent holds, plan and log API (Epic 63, spec A8) and the "Prepare invoice run" start.
Internal principals only.

| Method + path | Permission |
|---|---|
| ``GET /collections/rules`` · ``GET /collections/rules/{id}`` | ``receivables:read`` |
| ``POST /collections/rules`` · ``PUT /collections/rules/{id}`` · ``PUT /collections/rules/{id}/steps`` · ``DELETE /collections/rules/{id}`` | ``collections:write`` (client rule) / ``billing:configure`` (tenant default; checked in the service) |
| ``GET /collections/clients/{client_id}/plan`` | ``receivables:read`` ("What the agent will do today") |
| ``GET /collections/clients/{client_id}/agent-state`` | ``receivables:read`` |
| ``POST /collections/holds`` · ``POST /collections/holds/{id}/release`` · ``GET /collections/holds?client_id=`` | ``collections:write`` |
| ``GET /collections/clients/{client_id}/agent-log`` · ``GET /invoices/{id}/agent-log`` | ``receivables:read`` |
| ``POST /billing/prepare-run {legal_entity_id, period_start, period_end, client_id?}`` | ``billing:prepare`` (starts a ``billing`` agent run; Known gaps 5: the foundation's ``POST /agent-runs`` takes no kickoff inputs) |

Collections runs ("Run collections now") use the foundation's ``POST /agent-runs {agent_key: "collections",
subject_type: "client", subject_id}``.
"""

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict, model_validator

from app.core.actors import actor_from_ctx, require_human
from app.core.context import get_context
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.collection_rules import (
    AgentStateOut,
    CollectionPlanOut,
    HoldIn,
    HoldOut,
    LogOut,
    RuleIn,
    RuleOut,
    RuleUpdate,
    StepsIn,
)
from app.services import collection_rules_service as svc

router = APIRouter(tags=["collection-rules"])
DB = TenantDBSessionDep
Read = Annotated[Principal, Depends(require("receivables:read"))]
Write = Annotated[Principal, Depends(require("collections:write"))]
AnyWrite = Annotated[Principal, Depends(require("receivables:read"))]  # client vs default rule checked in the service
Prepare = Annotated[Principal, Depends(require("billing:prepare"))]


def _human() -> None:
    require_human(actor_from_ctx(get_context()))


@router.get("/collections/rules", response_model=list[RuleOut])
async def list_rules(_: Read, db: DB, client_id: uuid.UUID | None = None) -> list[RuleOut]:
    await svc.ensure_defaults(db)
    return await svc.list_rules(get_context(), db, client_id)


@router.post("/collections/rules", response_model=RuleOut, status_code=status.HTTP_201_CREATED)
async def create_rule(body: RuleIn, _: AnyWrite, db: DB) -> RuleOut:
    _human()
    await svc.ensure_defaults(db)
    return await svc.create_rule(get_context(), db, body)


@router.get("/collections/rules/{rule_id}", response_model=RuleOut)
async def get_rule(rule_id: uuid.UUID, _: Read, db: DB) -> RuleOut:
    return await svc.get_rule(get_context(), db, rule_id)


@router.put("/collections/rules/{rule_id}", response_model=RuleOut)
async def update_rule(rule_id: uuid.UUID, body: RuleUpdate, _: AnyWrite, db: DB) -> RuleOut:
    _human()
    return await svc.update_rule(get_context(), db, rule_id, body)


@router.put("/collections/rules/{rule_id}/steps", response_model=RuleOut)
async def put_steps(rule_id: uuid.UUID, body: StepsIn, _: AnyWrite, db: DB) -> RuleOut:
    _human()
    return await svc.put_steps(get_context(), db, rule_id, body.steps)


@router.delete("/collections/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(rule_id: uuid.UUID, _: AnyWrite, db: DB) -> Response:
    _human()
    await svc.delete_rule(get_context(), db, rule_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/collections/clients/{client_id}/plan", response_model=CollectionPlanOut)
async def client_plan(client_id: uuid.UUID, _: Read, db: DB) -> CollectionPlanOut:
    await _client(db, client_id)
    return await svc.plan_for(get_context(), db, client_id)


@router.get("/collections/clients/{client_id}/agent-state", response_model=AgentStateOut)
async def agent_state(client_id: uuid.UUID, _: Read, db: DB) -> AgentStateOut:
    await _client(db, client_id)
    return await svc.state(get_context(), db, client_id)


@router.get("/collections/clients/{client_id}/agent-log", response_model=list[LogOut])
async def client_log(client_id: uuid.UUID, _: Read, db: DB) -> list[LogOut]:
    await _client(db, client_id)
    return await svc.list_log(db, client_id=client_id)


@router.get("/invoices/{invoice_id}/agent-log", response_model=list[LogOut])
async def invoice_log(invoice_id: uuid.UUID, _: Read, db: DB) -> list[LogOut]:
    from app.core.errors import NotFound
    from app.models.billing import DebitNote, Invoice

    if await db.get(Invoice, invoice_id) is None and await db.get(DebitNote, invoice_id) is None:
        raise NotFound("Invoice not found")
    return await svc.list_log(db, document_id=invoice_id)


@router.post("/collections/holds", response_model=HoldOut, status_code=status.HTTP_201_CREATED)
async def create_hold(body: HoldIn, _: Write, db: DB) -> HoldOut:
    _human()
    return await svc.hold(get_context(), db, body)


@router.post("/collections/holds/{hold_id}/release", response_model=HoldOut)
async def release_hold(hold_id: uuid.UUID, _: Write, db: DB) -> HoldOut:
    _human()
    return await svc.release(get_context(), db, hold_id)


@router.get("/collections/holds", response_model=list[HoldOut])
async def list_holds(client_id: uuid.UUID, _: Write, db: DB, include_released: bool = False) -> list[HoldOut]:
    await _client(db, client_id)
    return await svc.list_holds(get_context(), db, client_id, include_released=include_released)


async def _client(db: Any, client_id: uuid.UUID) -> None:
    from app.core.errors import NotFound
    from app.models.clients import Client

    if await db.get(Client, client_id) is None:
        raise NotFound("Client not found")


# ---------------------------------------------------------------- billing preparation start
class PrepareRunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    legal_entity_id: uuid.UUID
    period_start: date
    period_end: date
    client_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _period(self) -> "PrepareRunIn":
        if self.period_end < self.period_start:
            raise ValueError("The period end is before its start")
        return self


@router.post("/billing/prepare-run")
async def prepare_run(body: PrepareRunIn, principal: Prepare, db: DB) -> dict[str, Any]:
    """"Prepare invoice run": a manual Billing Agent run owned by me for the period (it proposes a draft run)."""
    from app.core.errors import NotFound
    from app.models.legal_entities import LegalEntity
    from app.services import agent_runtime_service as runtime

    _human()
    if await db.get(LegalEntity, body.legal_entity_id) is None:
        raise NotFound("Legal entity not found")
    ref = await runtime.start_run(
        get_context(), db, agent_key="billing", trigger_kind="manual", trigger_key="manual",
        subject=("legal_entity", body.legal_entity_id), owner_user_id=principal.id,
        kickoff=svc.billing_kickoff(body.legal_entity_id, body.period_start, body.period_end, body.client_id),
    )  # fmt: skip
    return {"run_id": str(ref.run_id) if ref.run_id else None, "status": ref.status, "reason": ref.reason}
