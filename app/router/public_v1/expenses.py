# ruff: noqa: E501
"""``/public/v1/expense_claims`` (Epic 67, spec A4): read only, scope ``expenses:read``. Generated shape; see ``services/public_api/finance.py``."""

import uuid
from typing import Any

from app.core.public_api import public_router
from app.dependencies.public import PublicDBSessionDep
from app.router.public_v1._common import Listing, scope
from app.schemas.public_v1.common import Page
from app.schemas.public_v1.resources import ExpenseClaimOut
from app.services import public_api_service as svc

router = public_router("expense_claims")
Read = scope("expenses:read")


@router.get("/expense_claims", response_model=Page[ExpenseClaimOut])
async def list_expense_claims(_: Read, db: PublicDBSessionDep, page: Listing) -> dict[str, Any]:
    return await svc.finance.list_expense_claims(db, **page.kw())


@router.get("/expense_claims/{claim_id}", response_model=ExpenseClaimOut)
async def get_expense_claim(claim_id: uuid.UUID, _: Read, db: PublicDBSessionDep) -> dict[str, Any]:
    return await svc.finance.get_expense_claim(db, claim_id)
