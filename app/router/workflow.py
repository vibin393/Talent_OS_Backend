"""Workflow rule endpoints (Epic 10, story 10.4). Tenant admins only (``workflow:read`` / ``workflow:manage``).

* ``GET/POST /workflow/rules``, ``GET/PATCH /workflow/rules/{id}``, ``POST /workflow/rules/{id}/activate|
  deactivate|archive``, ``POST /workflow/rules/{id}/dry-run``, ``GET /workflow/rules/{id}/executions``
* ``POST /workflow/dry-run`` (unsaved rule), ``GET /workflow/executions/{id}``, ``GET /workflow/registry``
"""  # ruff: ignore[line-too-long]

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, status

from app.core.context import get_context
from app.core.pagination import Page, PageParams
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.workflow import (
    DraftDryRunIn,
    DryRunIn,
    DryRunOut,
    ExecutionOut,
    RegistryOut,
    RuleIn,
    RuleOut,
    RuleUpdate,
)
from app.services import workflow as svc

router = APIRouter(prefix="/workflow", tags=["workflow"])

Read = Annotated[Principal, Depends(require("workflow:read"))]
Manage = Annotated[Principal, Depends(require("workflow:manage"))]
Paging = Annotated[PageParams, Depends()]
DB = TenantDBSessionDep


@router.get("/registry", response_model=RegistryOut)
async def registry(_: Read, db: DB) -> Any:
    return await svc.rules.registry_for(get_context(), db)


@router.get("/rules", response_model=Page[RuleOut])
async def list_rules(_: Read, db: DB, params: Paging, include_archived: bool = False) -> Any:
    return await svc.list_rules(get_context(), db, params, include_archived)


@router.post("/rules", response_model=RuleOut, status_code=status.HTTP_201_CREATED)
async def create_rule(body: RuleIn, _: Manage, db: DB) -> Any:
    return await svc.create_rule(get_context(), db, body)


@router.get("/rules/{rule_id}", response_model=RuleOut)
async def get_rule(rule_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await svc.get_rule(get_context(), db, rule_id)


@router.patch("/rules/{rule_id}", response_model=RuleOut)
async def update_rule(rule_id: uuid.UUID, body: RuleUpdate, _: Manage, db: DB) -> Any:
    return await svc.update_rule(get_context(), db, rule_id, body)


@router.post("/rules/{rule_id}/activate", response_model=RuleOut)
async def activate_rule(rule_id: uuid.UUID, _: Manage, db: DB) -> Any:
    return await svc.set_active(get_context(), db, rule_id, True)


@router.post("/rules/{rule_id}/deactivate", response_model=RuleOut)
async def deactivate_rule(rule_id: uuid.UUID, _: Manage, db: DB) -> Any:
    return await svc.set_active(get_context(), db, rule_id, False)


@router.post("/rules/{rule_id}/archive", response_model=RuleOut)
async def archive_rule(rule_id: uuid.UUID, _: Manage, db: DB) -> Any:
    return await svc.archive_rule(get_context(), db, rule_id)


@router.post("/rules/{rule_id}/dry-run", response_model=DryRunOut)
async def dry_run_rule(rule_id: uuid.UUID, body: DryRunIn, _: Manage, db: DB) -> Any:
    return await svc.dry_run_rule(get_context(), db, rule_id, body)


@router.get("/rules/{rule_id}/executions", response_model=Page[ExecutionOut])
async def list_executions(
    rule_id: uuid.UUID,
    _: Read,
    db: DB,
    params: Paging,
    status_filter: Annotated[str | None, Query(alias="status", max_length=20)] = None,
    mode: Annotated[Literal["live", "dry_run"] | None, Query()] = None,
) -> Any:
    return await svc.list_executions(get_context(), db, rule_id, params, status_filter, mode)


@router.post("/dry-run", response_model=DryRunOut)
async def dry_run_draft(body: DraftDryRunIn, _: Manage, db: DB) -> Any:
    return await svc.dry_run_draft(get_context(), db, body)


@router.get("/executions/{execution_id}", response_model=ExecutionOut)
async def get_execution(execution_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await svc.get_execution(get_context(), db, execution_id)
