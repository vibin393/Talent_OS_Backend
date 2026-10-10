"""Worker "My Work" leave (Epic 26; ``/api/v1/portal/work/leave-*``, ``/holidays``, ``/optional-holidays``).

The Candidate principal with the worker org scope (AD-27); calls only ``services/time/leave_portal.py`` and
returns
``schemas/leave_portal.py``. Another Worker's ids -> 404.
"""

import uuid
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, status

from app.core.context import get_context
from app.core.field_visibility import portal_guard_route
from app.dependencies.worker import WorkerDep
from app.schemas.leave import LeaveRequestIn
from app.schemas.leave_portal import (
    WorkerHolidaysOut,
    WorkerLeaveBalanceOut,
    WorkerLeavePreviewOut,
    WorkerLeaveRequestOut,
    WorkerLeaveTypeOut,
    WorkerOptionalHolidayIn,
)
from app.services.time import leave_portal

router = APIRouter(prefix="/portal/work", tags=["portal-work"], route_class=portal_guard_route("worker"))


@router.get("/leave-types", response_model=list[WorkerLeaveTypeOut])
async def my_leave_types(principal: WorkerDep) -> Any:
    return await leave_portal.my_types(get_context(), principal.tenant_id, principal.candidate_id)


@router.get("/leave-balances", response_model=list[WorkerLeaveBalanceOut])
async def my_balances(principal: WorkerDep) -> Any:
    return await leave_portal.my_balances(get_context(), principal.tenant_id, principal.candidate_id)


@router.get("/leave-requests", response_model=list[WorkerLeaveRequestOut])
async def my_requests(principal: WorkerDep) -> Any:
    return await leave_portal.my_requests(get_context(), principal.tenant_id, principal.candidate_id)


@router.get("/leave-requests/preview", response_model=WorkerLeavePreviewOut)
async def preview(
    principal: WorkerDep,
    leave_type_id: uuid.UUID,
    start_date: date,
    end_date: date,
    placement_id: uuid.UUID | None = None,
    start_half: Literal["pm"] | None = None,
    end_half: Literal["am"] | None = None,
) -> Any:
    data = LeaveRequestIn(
        placement_id=placement_id,
        leave_type_id=leave_type_id,
        start_date=start_date,
        end_date=end_date,
        start_half=start_half,
        end_half=end_half,
    )
    return await leave_portal.preview(get_context(), principal.tenant_id, principal.candidate_id, data)


@router.post("/leave-requests", response_model=WorkerLeaveRequestOut, status_code=status.HTTP_201_CREATED)
async def create_request(data: LeaveRequestIn, principal: WorkerDep) -> Any:
    return await leave_portal.create(get_context(), principal.tenant_id, principal.candidate_id, data)


@router.post("/leave-requests/{request_id}/withdraw", response_model=WorkerLeaveRequestOut)
async def withdraw(request_id: uuid.UUID, principal: WorkerDep) -> Any:
    return await leave_portal.withdraw(get_context(), principal.tenant_id, principal.candidate_id, request_id)


@router.post("/leave-requests/{request_id}/cancel", response_model=WorkerLeaveRequestOut)
async def cancel(request_id: uuid.UUID, principal: WorkerDep) -> Any:
    return await leave_portal.cancel(get_context(), principal.tenant_id, principal.candidate_id, request_id)


@router.get("/holidays", response_model=WorkerHolidaysOut)
async def my_holidays(
    principal: WorkerDep,
    year: Annotated[int | None, Query(ge=2000, le=2100)] = None,
    placement_id: uuid.UUID | None = None,
) -> Any:
    from app.services.time.common import today

    return await leave_portal.my_holidays(
        get_context(), principal.tenant_id, principal.candidate_id, year or today().year, placement_id
    )


@router.post("/optional-holidays", response_model=WorkerHolidaysOut)
async def set_optional(data: WorkerOptionalHolidayIn, principal: WorkerDep) -> Any:
    return await leave_portal.set_optional(
        get_context(),
        principal.tenant_id,
        principal.candidate_id,
        data.holiday_id,
        data.placement_id,
        selected=data.selected,
    )
