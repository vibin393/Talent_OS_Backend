"""Client Portal leave calendar (Epic 26, A7): ``/api/v1/client-portal/leave-calendar``.

Only when the Client opted in (``client_team_calendar``, default ``client_team_calendar_default``) -- else
404.
Restricted Client Users see only their scoped Placements. Worker name, dates, half-day marks and "On leave"
only
(no leave type, reason or balances; field-visibility guard).
"""

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Query

from app.core.context import get_context
from app.core.field_visibility import portal_guard_route
from app.dependencies.client import ClientDBSessionDep, ClientDep
from app.schemas.leave_portal import ClientLeaveCalendarOut
from app.services.time import leave_portal

router = APIRouter(
    prefix="/client-portal", tags=["client-portal"], route_class=portal_guard_route("client_user")
)


@router.get("/leave-calendar", response_model=ClientLeaveCalendarOut)
async def leave_calendar(
    principal: ClientDep, db: ClientDBSessionDep, from_: Annotated[date, Query(alias="from")], to: date
) -> Any:
    assert principal.tenant_id is not None
    assert principal.org_id is not None
    return await leave_portal.client_calendar(
        get_context(), db, principal.tenant_id, principal.org_id, from_, to
    )
