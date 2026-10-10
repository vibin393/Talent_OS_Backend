# ruff: noqa: E501
"""``/public/v1/payments`` (Epic 67, spec A4): read only, scope ``invoices:read``. Generated shape; see ``services/public_api/finance.py``."""

import uuid
from typing import Any

from app.core.public_api import public_router
from app.dependencies.public import PublicDBSessionDep
from app.router.public_v1._common import Listing, scope
from app.schemas.public_v1.common import Page
from app.schemas.public_v1.resources import PaymentOut
from app.services import public_api_service as svc

router = public_router("payments")
Read = scope("invoices:read")


@router.get("/payments", response_model=Page[PaymentOut])
async def list_payments(_: Read, db: PublicDBSessionDep, page: Listing) -> dict[str, Any]:
    return await svc.finance.list_payments(db, **page.kw())


@router.get("/payments/{payment_id}", response_model=PaymentOut)
async def get_payment(payment_id: uuid.UUID, _: Read, db: PublicDBSessionDep) -> dict[str, Any]:
    return await svc.finance.get_payment(db, payment_id)
