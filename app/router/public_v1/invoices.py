# ruff: file-ignore[line-too-long]
"""``/public/v1/invoices`` (Epic 67, spec A4): read only, scope ``invoices:read``. Generated shape; see ``services/public_api/finance.py``."""

import uuid
from typing import Any

from app.core.public_api import public_router
from app.dependencies.public import PublicDBSessionDep
from app.router.public_v1._common import Listing, scope
from app.schemas.public_v1.common import Page
from app.schemas.public_v1.resources import InvoiceOut
from app.services import public_api_service as svc

router = public_router("invoices")
Read = scope("invoices:read")


@router.get("/invoices", response_model=Page[InvoiceOut])
async def list_invoices(_: Read, db: PublicDBSessionDep, page: Listing) -> dict[str, Any]:
    return await svc.finance.list_invoices(db, **page.kw())


@router.get("/invoices/{invoice_id}", response_model=InvoiceOut)
async def get_invoice(invoice_id: uuid.UUID, _: Read, db: PublicDBSessionDep) -> dict[str, Any]:
    return await svc.finance.get_invoice(db, invoice_id)
