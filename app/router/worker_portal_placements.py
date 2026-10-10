"""Worker "My Work" placements, profile and bank details (Epic 19 A8; ``/api/v1/portal/work``).

The Candidate principal with the worker org scope (AD-27, R17); calls only ``services/placements/portal.py`` and
returns ``schemas/placements_portal.py`` (no bill / vendor rate, margin or burden; responses are scanned outside
production by the field-visibility guard). Another Worker's ids -> 404.
"""

import uuid
from typing import Any

from fastapi import APIRouter, status

from app.core.context import get_context
from app.core.field_visibility import portal_guard_route
from app.dependencies.worker import WorkerDBSessionDep, WorkerDep
from app.schemas.placements_api import BankAccountIn
from app.schemas.placements_portal import (
    PortalBankAccountOut,
    PortalPlacementOut,
    PortalProfileOut,
    PortalProfileUpdate,
)
from app.services.placements import portal

router = APIRouter(prefix="/portal/work", tags=["portal-work"], route_class=portal_guard_route("worker"))


@router.get("/placements", response_model=list[PortalPlacementOut])
async def my_placements(principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.list_placements(db, principal.tenant_id, principal.candidate_id)


@router.get("/placements/{placement_id}", response_model=PortalPlacementOut)
async def my_placement(placement_id: uuid.UUID, principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.get_placement(db, principal.tenant_id, principal.candidate_id, placement_id)


@router.post("/placements/{placement_id}/confirm-joining", response_model=PortalPlacementOut)
async def confirm_joining(placement_id: uuid.UUID, principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.confirm_joining(
        get_context(), db, principal.tenant_id, principal.candidate_id, placement_id
    )


@router.get("/profile", response_model=PortalProfileOut)
async def my_profile(principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.get_profile(db, principal.candidate_id)


@router.patch("/profile", response_model=PortalProfileOut)
async def update_my_profile(data: PortalProfileUpdate, principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.update_profile(get_context(), db, principal.candidate_id, data)


@router.get("/bank-accounts", response_model=list[PortalBankAccountOut])
async def my_bank_accounts(principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.bank_accounts(db, principal.candidate_id)


@router.post("/bank-accounts", response_model=PortalBankAccountOut, status_code=status.HTTP_201_CREATED)
async def request_bank_change(data: BankAccountIn, principal: WorkerDep, db: WorkerDBSessionDep) -> Any:
    return await portal.request_bank_change(
        get_context(), db, principal.tenant_id, principal.candidate_id, data
    )
