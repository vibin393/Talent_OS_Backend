# ruff: file-ignore[line-too-long]
"""``/public/v1/timesheets`` (Epic 67, spec A4): read only, scope ``timesheets:read``. Generated shape; see ``services/public_api/finance.py``."""

import uuid
from typing import Any

from app.core.public_api import public_router
from app.dependencies.public import PublicDBSessionDep
from app.router.public_v1._common import Listing, scope
from app.schemas.public_v1.common import Page
from app.schemas.public_v1.resources import TimesheetOut
from app.services import public_api_service as svc

router = public_router("timesheets")
Read = scope("timesheets:read")


@router.get("/timesheets", response_model=Page[TimesheetOut])
async def list_timesheets(_: Read, db: PublicDBSessionDep, page: Listing) -> dict[str, Any]:
    return await svc.finance.list_timesheets(db, **page.kw())


@router.get("/timesheets/{timesheet_id}", response_model=TimesheetOut)
async def get_timesheet(timesheet_id: uuid.UUID, _: Read, db: PublicDBSessionDep) -> dict[str, Any]:
    return await svc.finance.get_timesheet(db, timesheet_id)
