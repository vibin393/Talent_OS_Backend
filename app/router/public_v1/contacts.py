# ruff: noqa: E501
"""``/public/v1/contacts`` (Epic 67, spec A4): read only, scope ``contacts:read``. Generated shape; see ``services/public_api/directory.py``."""

import uuid
from typing import Any

from app.core.public_api import public_router
from app.dependencies.public import PublicDBSessionDep
from app.router.public_v1._common import Listing, scope
from app.schemas.public_v1.common import Page
from app.schemas.public_v1.resources import ContactOut
from app.services import public_api_service as svc

router = public_router("contacts")
Read = scope("contacts:read")


@router.get("/contacts", response_model=Page[ContactOut])
async def list_contacts(_: Read, db: PublicDBSessionDep, page: Listing, client_id: uuid.UUID | None = None) -> dict[str, Any]:
    return await svc.directory.list_contacts(db, **page.kw(), client_id=client_id)


@router.get("/contacts/{contact_id}", response_model=ContactOut)
async def get_contact(contact_id: uuid.UUID, _: Read, db: PublicDBSessionDep) -> dict[str, Any]:
    return await svc.directory.get_contact(db, contact_id)
