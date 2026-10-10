"""Client Portal receivables (Epic 30, FR-159): ``/api/v1/client-portal/receivables``. Client session (org RLS
``client``) + the ``invoices:view`` portal permission: the client's own statement and Payment Status only."""

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends

from app.core.context import get_context
from app.core.permissions import Principal
from app.dependencies.client import ClientDBSessionDep, require_portal_permission
from app.schemas.receivables_portal import PortalPaymentStatus, PortalStatement
from app.services.receivables import portal

router = APIRouter(prefix="/client-portal/receivables", tags=["client-portal"])
Viewer = Annotated[Principal, Depends(require_portal_permission("invoices:view"))]


@router.get("/statement", response_model=PortalStatement)
async def statement(_: Viewer, db: ClientDBSessionDep, start: date, end: date, currency: str = "INR") -> Any:
    return await portal.statement(get_context(), db, currency, start, end)


@router.get("/payment-status", response_model=list[PortalPaymentStatus])
async def payment_status(_: Viewer, db: ClientDBSessionDep) -> Any:
    return await portal.payment_statuses(get_context(), db)
