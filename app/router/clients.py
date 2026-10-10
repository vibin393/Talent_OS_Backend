"""Client and contact endpoints (Epic 2): ``/api/v1/clients``, ``/api/v1/contacts``.

Each endpoint checks its permission via ``require(...)`` and calls one ``clients_service`` function with the
caller's RLS-scoped tenant session.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.core.context import get_context
from app.core.pagination import Page, PageParams
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.clients import (
    Client360Out,
    ClientCreate,
    ClientCreatedOut,
    ClientFilters,
    ClientOut,
    ClientUpdate,
    ContactCreate,
    ContactOut,
    ContactUpdate,
    QuickSearchHit,
)
from app.services import clients_service

router = APIRouter(prefix="/clients", tags=["clients"])
contacts_router = APIRouter(prefix="/contacts", tags=["clients"])

ClientRead = Annotated[Principal, Depends(require("client:read"))]
ClientCreatePerm = Annotated[Principal, Depends(require("client:create"))]
ClientUpdatePerm = Annotated[Principal, Depends(require("client:update"))]
ClientDelete = Annotated[Principal, Depends(require("client:delete"))]
ContactRead = Annotated[Principal, Depends(require("contact:read"))]
ContactCreatePerm = Annotated[Principal, Depends(require("contact:create"))]
ContactUpdatePerm = Annotated[Principal, Depends(require("contact:update"))]
ContactDelete = Annotated[Principal, Depends(require("contact:delete"))]
Paging = Annotated[PageParams, Depends()]
DB = TenantDBSessionDep


# ================================================================ clients
@router.get("", response_model=Page[ClientOut])
async def list_clients(
    _: ClientRead, db: DB, filters: Annotated[ClientFilters, Depends()], params: Paging
) -> Page[ClientOut]:
    return await clients_service.list_clients(get_context(), db, filters, params)


@router.post("", response_model=ClientCreatedOut, status_code=status.HTTP_201_CREATED)
async def create_client(body: ClientCreate, _: ClientCreatePerm, db: DB) -> ClientCreatedOut:
    return await clients_service.create_client(get_context(), db, body)


@router.get("/quick-search", response_model=list[QuickSearchHit])
async def quick_search(
    _: ClientRead, db: DB, q: Annotated[str, Query(min_length=1, max_length=200)], limit: int = 5
) -> list[QuickSearchHit]:
    return await clients_service.quick_search(get_context(), db, q, max(1, min(limit, 20)))


@router.get("/{client_id}", response_model=ClientOut)
async def get_client(client_id: uuid.UUID, _: ClientRead, db: DB) -> ClientOut:
    return await clients_service.get_client(get_context(), db, client_id)


@router.get("/{client_id}/360", response_model=Client360Out)
async def client_360(client_id: uuid.UUID, _: ClientRead, db: DB) -> Client360Out:
    return await clients_service.client_360(get_context(), db, client_id)


@router.patch("/{client_id}", response_model=ClientOut)
async def update_client(client_id: uuid.UUID, body: ClientUpdate, _: ClientUpdatePerm, db: DB) -> ClientOut:
    return await clients_service.update_client(get_context(), db, client_id, body)


@router.post("/{client_id}/archive", response_model=ClientOut)
async def archive_client(client_id: uuid.UUID, _: ClientDelete, db: DB) -> ClientOut:
    return await clients_service.archive_client(get_context(), db, client_id)


@router.post("/{client_id}/unarchive", response_model=ClientOut)
async def unarchive_client(client_id: uuid.UUID, _: ClientDelete, db: DB) -> ClientOut:
    return await clients_service.unarchive_client(get_context(), db, client_id)


# ================================================================ contacts
@router.get("/{client_id}/contacts", response_model=Page[ContactOut])
async def list_contacts(
    client_id: uuid.UUID,
    _: ContactRead,
    db: DB,
    params: Paging,
    include_archived: bool = False,
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> Page[ContactOut]:
    return await clients_service.list_contacts(
        get_context(), db, client_id, params, include_archived=include_archived, q=q
    )


@router.post("/{client_id}/contacts", response_model=ContactOut, status_code=status.HTTP_201_CREATED)
async def create_contact(
    client_id: uuid.UUID, body: ContactCreate, _: ContactCreatePerm, db: DB
) -> ContactOut:
    return await clients_service.create_contact(get_context(), db, client_id, body)


@contacts_router.get("/{contact_id}", response_model=ContactOut)
async def get_contact(contact_id: uuid.UUID, _: ContactRead, db: DB) -> ContactOut:
    return await clients_service.get_contact(get_context(), db, contact_id)


@contacts_router.patch("/{contact_id}", response_model=ContactOut)
async def update_contact(
    contact_id: uuid.UUID, body: ContactUpdate, _: ContactUpdatePerm, db: DB
) -> ContactOut:
    return await clients_service.update_contact(get_context(), db, contact_id, body)


@contacts_router.post("/{contact_id}/archive", response_model=ContactOut)
async def archive_contact(contact_id: uuid.UUID, _: ContactDelete, db: DB) -> ContactOut:
    return await clients_service.archive_contact(get_context(), db, contact_id)


@contacts_router.post("/{contact_id}/unarchive", response_model=ContactOut)
async def unarchive_contact(contact_id: uuid.UUID, _: ContactDelete, db: DB) -> ContactOut:
    return await clients_service.unarchive_contact(get_context(), db, contact_id)
