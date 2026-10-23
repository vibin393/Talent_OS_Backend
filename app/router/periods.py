# ruff: file-ignore[line-too-long]
"""Accounting Period close / reopen (Epic 30, FR-161): ``/api/v1/periods``.

``period:close`` (★, Idempotency-Key) closes after the checklist (blockers refuse, warnings need
acknowledgement); ``period:reopen`` (Finance Admin) requests a reopen through approval subject
``period_reopen``. Reads need ``receivables:read`` or ``period:close``.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, status

from app.core.context import get_context
from app.core.idempotency import IdempotencyGuard, idempotency
from app.core.permissions import Principal, require, require_any
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.receivables import (
    CheckResultOut,
    CloseEventOut,
    CloseIn,
    PeriodOut,
    PeriodReportOut,
    ReopenIn,
)
from app.services.receivables import periods_close

router = APIRouter(prefix="/periods", tags=["periods"])
DB = TenantDBSessionDep
Read = Annotated[Principal, Depends(require_any("receivables:read", "period:close"))]
Close = Annotated[Principal, Depends(require("period:close"))]
Reopen = Annotated[Principal, Depends(require("period:reopen"))]


@router.get("", response_model=list[PeriodOut])
async def list_periods(_: Read, db: DB, legal_entity_id: uuid.UUID, year: int | None = None) -> Any:
    return await periods_close.list_periods(get_context(), db, legal_entity_id, year)


@router.get("/report", response_model=PeriodReportOut)
async def period_report(_: Read, db: DB, legal_entity_id: uuid.UUID, year: int | None = None) -> Any:
    return await periods_close.report(get_context(), db, legal_entity_id, year)


@router.get("/{period_id}/checklist", response_model=list[CheckResultOut])
async def checklist(period_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await periods_close.checklist(get_context(), db, period_id)


@router.post("/{period_id}/close", response_model=PeriodOut)
async def close(period_id: uuid.UUID, data: CloseIn, _: Close, db: DB, idem: Annotated[IdempotencyGuard, Depends(idempotency())]) -> Any:
    return await idem.run(lambda: periods_close.close(get_context(), db, period_id, data))


@router.post("/{period_id}/reopen", response_model=CloseEventOut, status_code=status.HTTP_202_ACCEPTED)
async def reopen(period_id: uuid.UUID, data: ReopenIn, _: Reopen, db: DB) -> Any:
    return await periods_close.request_reopen(get_context(), db, period_id, data)
