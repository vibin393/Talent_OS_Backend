"""Leave & Holidays, internal app (Epic 26, FR-134..FR-136): ``/api/v1/time/holiday-calendars``,
``/api/v1/time/leave-*``.

Permissions: ``holiday:read`` / ``holiday:manage`` (calendars), ``leave:read`` (requests, balances, history),
``leave:manage_types`` (types, overrides, leave settings, Client leave settings), ``leave:adjust_balance``
(audited adjustments, accrual run, verify), ``leave:approve`` (proxy requests, withdraw / cancel on behalf;
approver decisions go through ``/approvals``), ``leave:read_team`` (team calendar). Each endpoint calls one
service function.
"""

import uuid
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, status

from app.core.actors import actor_from_ctx
from app.core.context import get_context
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.leave import (
    AccrualRunIn,
    AccrualRunOut,
    AdjustBalanceIn,
    CopyCalendarIn,
    CopyCalendarOut,
    HolidayCalendarDetail,
    HolidayCalendarIn,
    HolidayCalendarOut,
    HolidayCalendarUpdate,
    HolidayIn,
    HolidayOut,
    HolidayUpdate,
    LeaveBalanceOut,
    LeaveCalendarOut,
    LeaveClientSettingsIn,
    LeaveClientSettingsOut,
    LeaveLedgerEntryOut,
    LeavePreviewOut,
    LeaveRequestIn,
    LeaveRequestOut,
    LeaveSettingsOut,
    LeaveSettingsUpdate,
    LeaveTypeIn,
    LeaveTypeOut,
    LeaveTypeOverrideIn,
    LeaveTypeOverrideOut,
    LeaveTypeUpdate,
    PlacementHolidaysOut,
    ProxyLeaveRequestIn,
    VerifyBalancesOut,
)
from app.services.time import holidays, leave_balances, leave_requests, leave_settings, leave_types

router = APIRouter(prefix="/time", tags=["leave"])
DB = TenantDBSessionDep

HolidayRead = Annotated[Principal, Depends(require("holiday:read"))]
HolidayManage = Annotated[Principal, Depends(require("holiday:manage"))]
LeaveRead = Annotated[Principal, Depends(require("leave:read"))]
ManageTypes = Annotated[Principal, Depends(require("leave:manage_types"))]
Adjust = Annotated[Principal, Depends(require("leave:adjust_balance"))]
Approve = Annotated[Principal, Depends(require("leave:approve"))]
Team = Annotated[Principal, Depends(require("leave:read_team"))]


# ---------------------------------------------------------------- settings
@router.get("/leave-settings", response_model=LeaveSettingsOut)
async def get_leave_settings(_: ManageTypes, db: DB) -> Any:
    return await leave_settings.get_settings(get_context(), db)


@router.patch("/leave-settings", response_model=LeaveSettingsOut)
async def update_leave_settings(data: LeaveSettingsUpdate, _: ManageTypes, db: DB) -> Any:
    return await leave_settings.update_settings(get_context(), db, data)


@router.get("/leave-client-settings/{client_id}", response_model=LeaveClientSettingsOut)
async def get_client_settings(client_id: uuid.UUID, _: ManageTypes, db: DB) -> Any:
    return await leave_types.get_client_settings(get_context(), db, client_id)


@router.put("/leave-client-settings/{client_id}", response_model=LeaveClientSettingsOut)
async def put_client_settings(
    client_id: uuid.UUID, data: LeaveClientSettingsIn, _: ManageTypes, db: DB
) -> Any:
    return await leave_types.put_client_settings(get_context(), db, client_id, data)


# ---------------------------------------------------------------- holiday calendars
@router.get("/holiday-calendars", response_model=list[HolidayCalendarOut])
async def list_calendars(
    _: HolidayRead,
    db: DB,
    year: Annotated[int | None, Query(ge=2000, le=2100)] = None,
    include_archived: bool = False,
) -> Any:
    return await holidays.list_calendars(get_context(), db, year=year, include_archived=include_archived)


@router.post("/holiday-calendars", response_model=HolidayCalendarDetail, status_code=status.HTTP_201_CREATED)
async def create_calendar(data: HolidayCalendarIn, _: HolidayManage, db: DB) -> Any:
    return await holidays.create_calendar(get_context(), db, data)


@router.patch("/holiday-calendars/holidays/{holiday_id}", response_model=HolidayOut)
async def update_holiday(holiday_id: uuid.UUID, data: HolidayUpdate, _: HolidayManage, db: DB) -> Any:
    return await holidays.update_holiday(get_context(), db, holiday_id, data)


@router.delete("/holiday-calendars/holidays/{holiday_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_holiday(holiday_id: uuid.UUID, _: HolidayManage, db: DB) -> None:
    await holidays.delete_holiday(get_context(), db, holiday_id)


@router.get("/holiday-calendars/{calendar_id}", response_model=HolidayCalendarDetail)
async def get_calendar(calendar_id: uuid.UUID, _: HolidayRead, db: DB) -> Any:
    return await holidays.get_calendar(get_context(), db, calendar_id)


@router.patch("/holiday-calendars/{calendar_id}", response_model=HolidayCalendarDetail)
async def update_calendar(
    calendar_id: uuid.UUID, data: HolidayCalendarUpdate, _: HolidayManage, db: DB
) -> Any:
    return await holidays.update_calendar(get_context(), db, calendar_id, data)


@router.delete("/holiday-calendars/{calendar_id}")
async def delete_calendar(calendar_id: uuid.UUID, _: HolidayManage, db: DB) -> dict[str, str]:
    return await holidays.delete_calendar(get_context(), db, calendar_id)


@router.post(
    "/holiday-calendars/{calendar_id}/holidays",
    response_model=HolidayOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_holiday(calendar_id: uuid.UUID, data: HolidayIn, _: HolidayManage, db: DB) -> Any:
    return await holidays.add_holiday(get_context(), db, calendar_id, data)


@router.post(
    "/holiday-calendars/{calendar_id}/copy",
    response_model=CopyCalendarOut,
    status_code=status.HTTP_201_CREATED,
)
async def copy_calendar(calendar_id: uuid.UUID, data: CopyCalendarIn, _: HolidayManage, db: DB) -> Any:
    return await holidays.copy_calendar(get_context(), db, calendar_id, data.year)


@router.get("/placements/{placement_id}/holidays", response_model=PlacementHolidaysOut)
async def placement_holidays(
    placement_id: uuid.UUID,
    _: HolidayRead,
    db: DB,
    from_: Annotated[date, Query(alias="from")],
    to: Annotated[date, Query()],
) -> Any:
    return await holidays.placement_holidays(get_context(), db, placement_id, from_, to)


# ---------------------------------------------------------------- leave types
@router.get("/leave-types", response_model=list[LeaveTypeOut])
async def list_types(_: LeaveRead, db: DB, include_archived: bool = False) -> Any:
    return await leave_types.list_types(get_context(), db, include_archived=include_archived)


@router.post("/leave-types", response_model=LeaveTypeOut, status_code=status.HTTP_201_CREATED)
async def create_type(data: LeaveTypeIn, _: ManageTypes, db: DB) -> Any:
    return await leave_types.create_type(get_context(), db, data)


@router.get("/leave-types/{leave_type_id}", response_model=LeaveTypeOut)
async def get_type(leave_type_id: uuid.UUID, _: LeaveRead, db: DB) -> Any:
    return await leave_types.get_type_out(get_context(), db, leave_type_id)


@router.patch("/leave-types/{leave_type_id}", response_model=LeaveTypeOut)
async def update_type(leave_type_id: uuid.UUID, data: LeaveTypeUpdate, _: ManageTypes, db: DB) -> Any:
    return await leave_types.update_type(get_context(), db, leave_type_id, data)


@router.delete("/leave-types/{leave_type_id}")
async def delete_type(leave_type_id: uuid.UUID, _: ManageTypes, db: DB) -> dict[str, str]:
    return await leave_types.delete_type(get_context(), db, leave_type_id)


@router.put("/leave-types/{leave_type_id}/overrides/{client_id}", response_model=LeaveTypeOverrideOut)
async def put_override(
    leave_type_id: uuid.UUID, client_id: uuid.UUID, data: LeaveTypeOverrideIn, _: ManageTypes, db: DB
) -> Any:
    return await leave_types.put_override(get_context(), db, leave_type_id, client_id, data)


@router.delete("/leave-types/{leave_type_id}/overrides/{client_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_override(leave_type_id: uuid.UUID, client_id: uuid.UUID, _: ManageTypes, db: DB) -> None:
    await leave_types.delete_override(get_context(), db, leave_type_id, client_id)


# ---------------------------------------------------------------- balances
@router.get("/leave-balances", response_model=list[LeaveBalanceOut])
async def list_balances(
    _: LeaveRead,
    db: DB,
    candidate_id: uuid.UUID,
    leave_year: Annotated[str | None, Query(max_length=10)] = None,
) -> Any:
    return await leave_balances.balances(get_context(), db, candidate_id, leave_year=leave_year)


@router.get("/leave-balances/history", response_model=list[LeaveLedgerEntryOut])
async def balance_history(
    _: LeaveRead, db: DB, candidate_id: uuid.UUID, leave_type_id: uuid.UUID | None = None
) -> Any:
    return await leave_balances.history(get_context(), db, candidate_id, leave_type_id=leave_type_id)


@router.post("/leave-balances/adjust", response_model=LeaveBalanceOut)
async def adjust_balance(data: AdjustBalanceIn, _: Adjust, db: DB) -> Any:
    return await leave_balances.adjust(get_context(), db, data)


@router.post("/leave-balances/verify", response_model=VerifyBalancesOut)
async def verify_balances(_: Adjust, db: DB) -> Any:
    return await leave_balances.verify_balances(get_context(), db)


@router.post("/leave-accruals/run", response_model=AccrualRunOut)
async def run_accruals(data: AccrualRunIn, _: Adjust, db: DB) -> Any:
    month = data.month.replace(day=1)
    posted = await leave_balances.run_monthly_accruals(get_context(), db, month)
    return AccrualRunOut(month=f"{month.year:04d}-{month.month:02d}", posted=posted)


# ---------------------------------------------------------------- requests
@router.get("/leave-requests", response_model=list[LeaveRequestOut])
async def list_requests(
    _: LeaveRead,
    db: DB,
    candidate_id: uuid.UUID | None = None,
    placement_id: uuid.UUID | None = None,
    client_id: uuid.UUID | None = None,
    status_: Annotated[str | None, Query(alias="status", max_length=200)] = None,
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: date | None = None,
) -> Any:
    return await leave_requests.list_requests(
        get_context(),
        db,
        candidate_id=candidate_id,
        placement_id=placement_id,
        client_id=client_id,
        status=status_,
        start=from_,
        end=to,
    )


@router.get("/leave-requests/preview", response_model=LeavePreviewOut)
async def preview_request(
    _: LeaveRead,
    db: DB,
    candidate_id: uuid.UUID,
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
    return await leave_requests.preview(get_context(), db, candidate_id, data)


@router.post("/leave-requests", response_model=LeaveRequestOut, status_code=status.HTTP_201_CREATED)
async def proxy_request(data: ProxyLeaveRequestIn, _: Approve, db: DB) -> Any:
    ctx = get_context()
    req = await leave_requests.create_request(
        ctx, db, data.candidate_id, data, actor=actor_from_ctx(ctx), proxy_reason=data.proxy_reason
    )
    return await leave_requests.get_out(ctx, db, req.id)


@router.get("/leave-requests/{request_id}", response_model=LeaveRequestOut)
async def get_request(request_id: uuid.UUID, _: LeaveRead, db: DB) -> Any:
    return await leave_requests.get_out(get_context(), db, request_id)


@router.post("/leave-requests/{request_id}/withdraw", response_model=LeaveRequestOut)
async def withdraw_request(request_id: uuid.UUID, _: Approve, db: DB) -> Any:
    ctx = get_context()
    req = await leave_requests.get_request(db, request_id, lock=True)
    await leave_requests.withdraw(ctx, db, req, actor_from_ctx(ctx))
    return await leave_requests.get_out(ctx, db, req.id)


@router.post("/leave-requests/{request_id}/cancel", response_model=LeaveRequestOut)
async def cancel_request(request_id: uuid.UUID, _: Approve, db: DB) -> Any:
    ctx = get_context()
    req = await leave_requests.get_request(db, request_id, lock=True)
    await leave_requests.request_cancel(ctx, db, req, actor_from_ctx(ctx))
    return await leave_requests.get_out(ctx, db, req.id)


# ---------------------------------------------------------------- team calendar
@router.get("/leave-calendar", response_model=LeaveCalendarOut)
async def team_calendar(
    _: Team, db: DB, from_: Annotated[date, Query(alias="from")], to: date, client_id: uuid.UUID | None = None
) -> Any:
    return await leave_requests.team_calendar(get_context(), db, start=from_, end=to, client_id=client_id)
