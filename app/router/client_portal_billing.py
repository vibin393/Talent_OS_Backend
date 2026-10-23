# ruff: file-ignore[line-too-long]
"""Client Portal invoices (Epic 29, FR-149): ``/api/v1/client-portal/invoices``. Client session (org RLS
``client``) + the ``invoices:view`` portal permission. Issued documents with a registered (or not applicable) IRN
only; no manual flags, postings or internal notes."""

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from app.core.context import get_context
from app.core.permissions import Principal
from app.dependencies.client import ClientDBSessionDep, require_portal_permission
from app.schemas.billing_portal import PortalInvoiceDetail, PortalInvoiceSummary
from app.services.billing import portal

router = APIRouter(prefix="/client-portal/invoices", tags=["client-portal"])
Viewer = Annotated[Principal, Depends(require_portal_permission("invoices:view"))]
Kind = Literal["invoice", "credit_note", "debit_note"]


@router.get("", response_model=list[PortalInvoiceSummary])
async def list_documents(_: Viewer, db: ClientDBSessionDep) -> Any:
    return await portal.client_list(get_context(), db)


@router.get("/{kind}/{doc_id}", response_model=PortalInvoiceDetail)
async def get_document(kind: Kind, doc_id: uuid.UUID, _: Viewer, db: ClientDBSessionDep) -> Any:
    return await portal.client_detail(get_context(), db, kind, doc_id)


@router.get("/{kind}/{doc_id}/pdf")
async def get_pdf(kind: Kind, doc_id: uuid.UUID, _: Viewer, db: ClientDBSessionDep) -> Response:
    data, filename = await portal.client_pdf(get_context(), db, kind, doc_id)
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
