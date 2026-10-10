# ruff: noqa: E501
"""``/public/v1/clients`` (Epic 67, spec A4): read only, scope ``clients:read``. Generated shape; see ``services/public_api/directory.py``."""

import uuid
from typing import Any

from app.core.public_api import public_router
from app.dependencies.public import PublicDBSessionDep
from app.router.public_v1._common import Listing, scope
from app.schemas.public_v1.common import Page
from app.schemas.public_v1.resources import ClientOut
from app.services import public_api_service as svc

router = public_router("clients")
Read = scope("clients:read")


@router.get("/clients", response_model=Page[ClientOut])
async def list_clients(_: Read, db: PublicDBSessionDep, page: Listing) -> dict[str, Any]:
    return await svc.directory.list_clients(db, **page.kw())


@router.get("/clients/{client_id}", response_model=ClientOut)
async def get_client(client_id: uuid.UUID, _: Read, db: PublicDBSessionDep) -> dict[str, Any]:
    return await svc.directory.get_client(db, client_id)
