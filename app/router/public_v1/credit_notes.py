# ruff: noqa: E501
"""``/public/v1/credit_notes`` (Epic 67, spec A4): read only, scope ``invoices:read``. Generated shape; see ``services/public_api/finance.py``."""

import uuid
from typing import Any

from app.core.public_api import public_router
from app.dependencies.public import PublicDBSessionDep
from app.router.public_v1._common import Listing, scope
from app.schemas.public_v1.common import Page
from app.schemas.public_v1.resources import CreditNoteOut
from app.services import public_api_service as svc

router = public_router("credit_notes")
Read = scope("invoices:read")


@router.get("/credit_notes", response_model=Page[CreditNoteOut])
async def list_credit_notes(_: Read, db: PublicDBSessionDep, page: Listing) -> dict[str, Any]:
    return await svc.finance.list_credit_notes(db, **page.kw())


@router.get("/credit_notes/{note_id}", response_model=CreditNoteOut)
async def get_credit_note(note_id: uuid.UUID, _: Read, db: PublicDBSessionDep) -> dict[str, Any]:
    return await svc.finance.get_credit_note(db, note_id)
