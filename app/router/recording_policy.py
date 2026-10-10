# ruff: noqa: E501
"""Recording Policy settings API (Epic 58, story 58.2; FR-294) on the foundation table ``recording_policies``.

| Method + path | Permission |
|---|---|
| ``GET /recording-policies`` · ``GET /recording-policies/evaluate?country=&region=`` | ``ai:agent_configure`` or ``screening:conduct`` |
| ``PUT /recording-policies`` (upsert by jurisdiction) · ``PUT /recording-policies/{id}`` · ``DELETE /recording-policies/{id}`` (human) | ``ai:agent_configure`` |
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response, status

from app.core.actors import actor_from_ctx, require_human
from app.core.context import get_context
from app.core.permissions import Principal, require, require_any
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.voice import PolicyEvaluationOut, RecordingPolicyIn, RecordingPolicyOut
from app.services import voice_service as svc

router = APIRouter(tags=["recording-policy"])
DB = TenantDBSessionDep
Read = Annotated[Principal, Depends(require_any("ai:agent_configure", "screening:conduct"))]
Configure = Annotated[Principal, Depends(require("ai:agent_configure"))]


def _human() -> None:
    require_human(actor_from_ctx(get_context()))


@router.get("/recording-policies", response_model=list[RecordingPolicyOut])
async def list_rules(_: Read, db: DB) -> Any:
    return await svc.policy.list_rules(get_context(), db)


@router.get("/recording-policies/evaluate", response_model=PolicyEvaluationOut)
async def evaluate(
    _: Read,
    db: DB,
    country: Annotated[str | None, Query(max_length=60)] = None,
    region: Annotated[str | None, Query(max_length=60)] = None,
) -> Any:
    return await svc.policy.evaluate(get_context(), db, country, region)


@router.put("/recording-policies", response_model=RecordingPolicyOut)
async def upsert(body: RecordingPolicyIn, _: Configure, db: DB) -> Any:
    _human()
    return await svc.policy.upsert_rule(get_context(), db, body)


@router.put("/recording-policies/{rule_id}", response_model=RecordingPolicyOut)
async def update(rule_id: uuid.UUID, body: RecordingPolicyIn, _: Configure, db: DB) -> Any:
    _human()
    return await svc.policy.upsert_rule(get_context(), db, body, rule_id)


@router.delete("/recording-policies/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete(rule_id: uuid.UUID, _: Configure, db: DB) -> Response:
    _human()
    await svc.policy.delete_rule(get_context(), db, rule_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
