# ruff: noqa: E501
"""Timesheet flags API (Epic 63, spec A8). Internal principals only (flags are never Client-visible); API keys may
read and review like any internal principal holding the permission.

| Method + path | Permission |
|---|---|
| ``GET /timesheets/{id}/flags`` | ``timesheet:read`` |
| ``GET /timesheet-flags`` (status, rule, client_id, from, to; paged) | ``timesheet:monitor`` |
| ``GET /timesheet-flags/counts?timesheet_ids=`` | ``timesheet:read`` (monitor "Flags" column) |
| ``POST /timesheet-flags/{id}/review {status, note?}`` | ``timesheet:approve`` or ``timesheet:monitor`` |
| ``POST /timesheet-flags/nudge-now`` | ``timesheet:monitor`` ("Nudge now": a Timesheet Agent run for me) |
"""

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from app.core.context import get_context
from app.core.pagination import Page, PageParams
from app.core.permissions import Principal, require, require_any
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.timesheet_flags import FlagCount, FlagListRow, FlagOut, FlagReviewIn
from app.services import timesheet_flags_service as svc

router = APIRouter(tags=["timesheet-flags"])
DB = TenantDBSessionDep
Read = Annotated[Principal, Depends(require("timesheet:read"))]
Monitor = Annotated[Principal, Depends(require("timesheet:monitor"))]
Reviewer = Annotated[Principal, Depends(require_any("timesheet:approve", "timesheet:monitor"))]


@router.get("/timesheets/{timesheet_id}/flags", response_model=list[FlagOut])
async def timesheet_flags(timesheet_id: uuid.UUID, _: Read, db: DB) -> list[FlagOut]:
    return await svc.list_for_timesheet(get_context(), db, timesheet_id)


@router.get("/timesheet-flags", response_model=Page[FlagListRow])
async def list_flags(
    _: Monitor,
    db: DB,
    params: Annotated[PageParams, Depends()],
    status: str | None = None,
    rule: str | None = None,
    client_id: uuid.UUID | None = None,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
) -> Page[FlagListRow]:
    rows, total = await svc.list_flags(
        get_context(), db, status=status, rule=rule, client_id=client_id, date_from=date_from, date_to=date_to,
        limit=params.page_size, offset=params.offset,
    )  # fmt: skip
    return Page[FlagListRow](items=rows, total=total, page=params.page, page_size=params.page_size)


@router.get("/timesheet-flags/counts", response_model=list[FlagCount])
async def flag_counts(_: Read, db: DB, timesheet_ids: Annotated[list[uuid.UUID], Query(max_length=500)]) -> list[FlagCount]:
    counts = await svc.counts(db, list(timesheet_ids))
    return [FlagCount(timesheet_id=k, open_count=v[0], max_severity=v[1]) for k, v in counts.items()]


@router.post("/timesheet-flags/{flag_id}/review", response_model=FlagOut)
async def review_flag(flag_id: uuid.UUID, body: FlagReviewIn, _: Reviewer, db: DB) -> FlagOut:
    return await svc.review(get_context(), db, flag_id, body)


@router.post("/timesheet-flags/nudge-now")
async def nudge_now(principal: Monitor, db: DB) -> dict[str, Any]:
    """"Nudge now": the Timesheet Agent reminds everyone in my list who hasn't been reminded today (manual trigger)."""
    from app.core.actors import actor_from_ctx, require_human

    ctx = get_context()
    require_human(actor_from_ctx(ctx))
    ref = await svc.start_nudge_run(ctx, db, principal.id, trigger_key="manual", trigger_kind="manual")
    return {"run_id": str(ref.run_id) if ref.run_id else None, "status": ref.status, "reason": ref.reason}
