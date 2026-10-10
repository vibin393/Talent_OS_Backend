"""Client Portal workforce (Epic 18, FR-103): home tiles, team (``placements:view``), PO consumption
(``invoices:view``; Epic 16 read model mounted here) and compliance packs. Invoices are Epic 29's
``/client-portal/invoices`` and timesheets Epic 25's ``/client-portal/timesheets`` (linked, not duplicated)."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from app.core.context import get_context
from app.core.field_visibility import portal_guard_route
from app.core.permissions import Principal
from app.dependencies.client import ClientDBSessionDep, ClientDep, require_portal_permission
from app.schemas.client_hub_portal import (
    ClientHomeOut,
    ClientPlacementDetail,
    ClientPlacementOut,
    CompliancePackOut,
)
from app.schemas.commercials_portal import ClientPoConsumptionOut
from app.services.client_portal import compliance, home
from app.services.commercials import portal as commercials_portal
from app.services.placements import portal as placements_portal

router = APIRouter(
    prefix="/client-portal", tags=["client-portal"], route_class=portal_guard_route("client_user")
)
TeamViewer = Annotated[Principal, Depends(require_portal_permission("placements:view"))]
InvoiceViewer = Annotated[Principal, Depends(require_portal_permission("invoices:view"))]


@router.get("/home", response_model=ClientHomeOut)
async def get_home(_: ClientDep, db: ClientDBSessionDep) -> Any:
    return await home.home(get_context(), db)


@router.get("/team", response_model=list[ClientPlacementOut])
async def team(_: TeamViewer, db: ClientDBSessionDep, status: str | None = None) -> Any:
    return await placements_portal.client_placements(get_context(), db, status)


@router.get("/team/{placement_id}", response_model=ClientPlacementDetail)
async def team_member(placement_id: uuid.UUID, _: TeamViewer, db: ClientDBSessionDep) -> Any:
    return await placements_portal.client_placement_detail(get_context(), db, placement_id)


@router.get("/purchase-orders", response_model=list[ClientPoConsumptionOut])
async def purchase_orders(_: InvoiceViewer, db: ClientDBSessionDep) -> Any:
    return await commercials_portal.client_po_consumption(get_context(), db)


@router.get("/compliance-packs", response_model=list[CompliancePackOut])
async def packs(_: ClientDep, db: ClientDBSessionDep) -> Any:
    return await compliance.client_packs(get_context(), db)


@router.get("/compliance-packs/{pack_id}", response_model=CompliancePackOut)
async def pack(pack_id: uuid.UUID, _: ClientDep, db: ClientDBSessionDep) -> Any:
    return await compliance.client_pack(get_context(), db, pack_id)


@router.get("/compliance-packs/items/{item_id}/download")
async def download(item_id: uuid.UUID, _: ClientDep, db: ClientDBSessionDep) -> Response:
    ctx = get_context()
    data, filename, ctype = await compliance.client_download(ctx, db, item_id, ctx.ip)
    return Response(
        content=data, media_type=ctype, headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )
