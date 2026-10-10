# ruff: noqa: E501
"""Client Portal hiring forecasts (Epic 44, FR-226): ``/api/v1/client-portal/forecasts``.

Client session (org RLS ``client``: ``workforce_plans.client_id``) + the ``forecasts:submit`` portal permission
(403 ``portal_permission_required`` otherwise). Another Client's forecast is 404; internal plans of the same Client
are never listed. Responses use ``schemas/workforce_portal.py`` only (no budget, rate, approval chain or internal
note); every JSON response is scanned outside production. Not the P2 ``client_portal_workforce.py``
(home / team).
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response, status

from app.core.context import get_context
from app.core.field_visibility import portal_guard_route
from app.core.permissions import Principal
from app.dependencies.client import ClientDBSessionDep, require_portal_permission
from app.schemas.workforce_portal import ForecastIn, ForecastPortalDetail, ForecastPortalOut, ForecastUpdate
from app.services.workforce import forecasts

router = APIRouter(
    prefix="/client-portal", tags=["client-portal"], route_class=portal_guard_route("client_user")
)
Submitter = Annotated[Principal, Depends(require_portal_permission("forecasts:submit"))]


@router.get("/forecasts", response_model=list[ForecastPortalOut])
async def list_forecasts(_: Submitter, db: ClientDBSessionDep) -> Any:
    return await forecasts.client_list(get_context(), db)


@router.post("/forecasts", response_model=ForecastPortalDetail, status_code=status.HTTP_201_CREATED)
async def create_forecast(body: ForecastIn, _: Submitter, db: ClientDBSessionDep) -> Any:
    return await forecasts.client_create(get_context(), db, body)


@router.get("/forecasts/{forecast_id}", response_model=ForecastPortalDetail)
async def get_forecast(forecast_id: uuid.UUID, _: Submitter, db: ClientDBSessionDep) -> Any:
    return await forecasts.client_get(get_context(), db, forecast_id)


@router.patch("/forecasts/{forecast_id}", response_model=ForecastPortalDetail)
async def update_forecast(
    forecast_id: uuid.UUID, body: ForecastUpdate, _: Submitter, db: ClientDBSessionDep
) -> Any:
    return await forecasts.client_update(get_context(), db, forecast_id, body)


@router.delete("/forecasts/{forecast_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_forecast(forecast_id: uuid.UUID, _: Submitter, db: ClientDBSessionDep) -> Response:
    await forecasts.client_delete(get_context(), db, forecast_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
