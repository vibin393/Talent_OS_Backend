"""Calendar / mailbox connection endpoints (Epic 7, story 7.4, FR-53). Tokens never leave the server."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse

from app.core.context import get_context
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.connections import ConnectionOut, FreeBusyIn, FreeBusyOut, ProviderOut, StartOut
from app.services import connections_service as svc

router = APIRouter(prefix="/connections", tags=["connections"])

ManageOwn = Annotated[Principal, Depends(require("connection:manage_own"))]
DB = TenantDBSessionDep


@router.get("/providers", response_model=list[ProviderOut])
async def list_providers(_: ManageOwn) -> Any:
    return svc.list_providers()


@router.get("", response_model=list[ConnectionOut])
async def list_connections(_: ManageOwn, db: DB) -> Any:
    return await svc.list_mine(get_context(), db)


@router.post("/free-busy", response_model=FreeBusyOut)
async def free_busy(
    body: FreeBusyIn, _: Annotated[Principal, Depends(require("interview:schedule"))], db: DB
) -> Any:
    return await svc.free_busy(get_context(), db, body)


@router.post("/{provider}/start", response_model=StartOut)
async def start(provider: str, _: ManageOwn) -> Any:
    return await svc.start(get_context(), provider)


@router.get("/{provider}/callback", response_class=RedirectResponse, status_code=302)
async def callback(
    provider: str,
    _: ManageOwn,
    db: DB,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    target = await svc.complete_callback(get_context(), db, provider, code, state, error)
    return RedirectResponse(
        target, status_code=302, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
    )


@router.delete("/{connection_id}", response_model=ConnectionOut)
async def disconnect(connection_id: uuid.UUID, _: ManageOwn, db: DB) -> Any:
    return await svc.disconnect(get_context(), db, connection_id)
