"""SLA targets, clocks and escalation policies (Epic 32, stories 19.1-19.2). Reads need ``sla:read``, writes
``sla:manage`` (Tenant Admin by default). Demand SLA targets stay on ``/settings/sla-targets`` (Phase 1).

* ``GET /sla/milestones``; ``GET|POST /sla/targets``, ``PATCH|DELETE /sla/targets/{id}``
* ``GET /sla/clocks?subject_type=&subject_id=``; ``GET /sla/summary?subject_type=&milestone=&from=&to=``
* ``GET|POST /sla/escalation-policies``, ``GET|PATCH /sla/escalation-policies/{id}``,
  ``POST /sla/escalation-policies/{id}/archive``
* ``GET|PUT /sla/approval-chains/{chain_id}/escalations`` (per-step escalation of an approval chain)
* ``GET /sla/escalations?subject_kind=&subject_id=`` (watch history; the subject must be readable)
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Response, status

from app.core.context import get_context
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.sla import (
    ChainEscalationsIn,
    ChainEscalationsOut,
    EscalationPolicyIn,
    EscalationPolicyOut,
    EscalationPolicyUpdate,
    EscalationWatchOut,
    GenericSlaTargetIn,
    GenericSlaTargetOut,
    GenericSlaTargetUpdate,
    SlaClockView,
    SlaMilestoneOut,
    SlaSummaryOut,
)
from app.services.sla import api as svc

router = APIRouter(prefix="/sla", tags=["sla"])

Read = Annotated[Principal, Depends(require("sla:read"))]
Manage = Annotated[Principal, Depends(require("sla:manage"))]
DB = TenantDBSessionDep
SubjectType = Annotated[str, Query(min_length=1, max_length=40)]


@router.get("/milestones", response_model=list[SlaMilestoneOut])
async def milestones(_: Read) -> Any:
    return svc.list_milestones()


@router.get("/targets", response_model=list[GenericSlaTargetOut])
async def list_targets(
    _: Read, db: DB, subject_type: Annotated[str | None, Query(max_length=40)] = None
) -> Any:
    return await svc.list_targets(get_context(), db, subject_type)


@router.post("/targets", response_model=GenericSlaTargetOut, status_code=status.HTTP_201_CREATED)
async def create_target(body: GenericSlaTargetIn, _: Manage, db: DB) -> Any:
    return await svc.create_target(get_context(), db, body)


@router.patch("/targets/{target_id}", response_model=GenericSlaTargetOut)
async def update_target(target_id: uuid.UUID, body: GenericSlaTargetUpdate, _: Manage, db: DB) -> Any:
    return await svc.update_target(get_context(), db, target_id, body)


@router.delete("/targets/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_target(target_id: uuid.UUID, _: Manage, db: DB) -> Response:
    await svc.delete_target(get_context(), db, target_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/clocks", response_model=list[SlaClockView])
async def list_clocks(_: Read, db: DB, subject_type: SubjectType, subject_id: uuid.UUID | None = None) -> Any:
    return await svc.list_clocks(get_context(), db, subject_type, subject_id)


@router.get("/summary", response_model=SlaSummaryOut)
async def summary(
    _: Read,
    db: DB,
    subject_type: SubjectType,
    milestone: Annotated[str | None, Query(max_length=40)] = None,
    start: Annotated[datetime | None, Query(alias="from")] = None,
    end: Annotated[datetime | None, Query(alias="to")] = None,
    vendor_id: uuid.UUID | None = None,
    client_id: uuid.UUID | None = None,
) -> Any:
    return await svc.summary(
        get_context(),
        db,
        subject_type=subject_type,
        milestone=milestone,
        start=start,
        end=end,
        vendor_id=vendor_id,
        client_id=client_id,
    )


@router.get("/escalation-policies", response_model=list[EscalationPolicyOut])
async def list_policies(_: Read, db: DB, include_archived: bool = False) -> Any:
    return await svc.list_policies(get_context(), db, include_archived)


@router.post("/escalation-policies", response_model=EscalationPolicyOut, status_code=status.HTTP_201_CREATED)
async def create_policy(body: EscalationPolicyIn, _: Manage, db: DB) -> Any:
    return await svc.create_policy(get_context(), db, body)


@router.get("/escalation-policies/{policy_id}", response_model=EscalationPolicyOut)
async def get_policy(policy_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await svc.get_policy(get_context(), db, policy_id)


@router.patch("/escalation-policies/{policy_id}", response_model=EscalationPolicyOut)
async def update_policy(policy_id: uuid.UUID, body: EscalationPolicyUpdate, _: Manage, db: DB) -> Any:
    return await svc.update_policy(get_context(), db, policy_id, body)


@router.post("/escalation-policies/{policy_id}/archive", response_model=EscalationPolicyOut)
async def archive_policy(policy_id: uuid.UUID, _: Manage, db: DB) -> Any:
    return await svc.archive_policy(get_context(), db, policy_id)


@router.get("/approval-chains/{chain_id}/escalations", response_model=ChainEscalationsOut)
async def get_chain_escalations(chain_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await svc.get_chain_escalations(get_context(), db, chain_id)


@router.put("/approval-chains/{chain_id}/escalations", response_model=ChainEscalationsOut)
async def set_chain_escalations(chain_id: uuid.UUID, body: ChainEscalationsIn, _: Manage, db: DB) -> Any:
    return await svc.set_chain_escalations(get_context(), db, chain_id, body)


@router.get("/escalations", response_model=list[EscalationWatchOut])
async def list_escalations(
    _: Read, db: DB, subject_kind: Annotated[Literal["approval_step", "task"], Query()], subject_id: uuid.UUID
) -> Any:
    return await svc.list_escalations(get_context(), db, subject_kind, subject_id)
