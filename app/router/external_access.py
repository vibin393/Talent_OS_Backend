"""Internal administration of external access (Epic 14, FR-78, FR-79).

* ``/api/v1/clients/{client_id}/portal-users`` (``client_portal:manage``) -- Client 360 "Portal access".
* ``/api/v1/vendors/{vendor_id}/portal-users`` (``vendor_portal:manage``) -- Vendor Users.
"""

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Response, status

from app.core.context import get_context
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.external_auth import (
    ClientUserGrantIn,
    ClientUserOut,
    ClientUserUpdate,
    VendorUserInviteIn,
    VendorUserOut,
    VendorUserUpdate,
)
from app.services.external_auth import access

router = APIRouter(tags=["external-access"])

ClientManage = Annotated[Principal, Depends(require("client_portal:manage"))]
VendorManage = Annotated[Principal, Depends(require("vendor_portal:manage"))]
DB = TenantDBSessionDep


# ---------------------------------------------------------------- client users
@router.get("/clients/{client_id}/portal-users", response_model=list[ClientUserOut])
async def list_client_users(client_id: uuid.UUID, _: ClientManage, db: DB) -> Any:
    return await access.list_client_users(get_context(), db, client_id)


@router.post(
    "/clients/{client_id}/portal-users", response_model=ClientUserOut, status_code=status.HTTP_201_CREATED
)
async def grant_client_user(client_id: uuid.UUID, body: ClientUserGrantIn, _: ClientManage, db: DB) -> Any:
    return await access.grant_client_user(get_context(), db, client_id, body)


@router.patch("/clients/{client_id}/portal-users/{client_user_id}", response_model=ClientUserOut)
async def update_client_user(
    client_id: uuid.UUID, client_user_id: uuid.UUID, body: ClientUserUpdate, _: ClientManage, db: DB
) -> Any:
    return await access.update_client_user(get_context(), db, client_id, client_user_id, body)


@router.post(
    "/clients/{client_id}/portal-users/{client_user_id}/resend", status_code=status.HTTP_204_NO_CONTENT
)
async def resend_client_invite(
    client_id: uuid.UUID, client_user_id: uuid.UUID, _: ClientManage, db: DB
) -> Response:
    await access.resend_client_invite(get_context(), db, client_id, client_user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/clients/{client_id}/portal-users/{client_user_id}/{action}", response_model=ClientUserOut)
async def client_user_action(
    client_id: uuid.UUID,
    client_user_id: uuid.UUID,
    action: Literal["suspend", "reactivate", "revoke", "reset-mfa"],
    _: ClientManage,
    db: DB,
) -> Any:
    if action == "reset-mfa":
        return await access.reset_client_mfa(get_context(), db, client_id, client_user_id)
    return await access.set_client_user_status(get_context(), db, client_id, client_user_id, action)


# ---------------------------------------------------------------- vendor users
@router.get("/vendors/{vendor_id}/portal-users", response_model=list[VendorUserOut])
async def list_vendor_users(vendor_id: uuid.UUID, _: VendorManage, db: DB) -> Any:
    return await access.list_vendor_users(get_context(), db, vendor_id)


@router.post(
    "/vendors/{vendor_id}/portal-users", response_model=VendorUserOut, status_code=status.HTTP_201_CREATED
)
async def invite_vendor_user(vendor_id: uuid.UUID, body: VendorUserInviteIn, _: VendorManage, db: DB) -> Any:
    return await access.invite_vendor_user(get_context(), db, vendor_id, body)


@router.patch("/vendors/{vendor_id}/portal-users/{vendor_user_id}", response_model=VendorUserOut)
async def update_vendor_user(
    vendor_id: uuid.UUID, vendor_user_id: uuid.UUID, body: VendorUserUpdate, _: VendorManage, db: DB
) -> Any:
    return await access.update_vendor_user(get_context(), db, vendor_id, vendor_user_id, body)


@router.post(
    "/vendors/{vendor_id}/portal-users/{vendor_user_id}/resend", status_code=status.HTTP_204_NO_CONTENT
)
async def resend_vendor_invite(
    vendor_id: uuid.UUID, vendor_user_id: uuid.UUID, _: VendorManage, db: DB
) -> Response:
    await access.resend_vendor_invite(get_context(), db, vendor_id, vendor_user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/vendors/{vendor_id}/portal-users/{vendor_user_id}/{action}", response_model=VendorUserOut)
async def vendor_user_action(
    vendor_id: uuid.UUID,
    vendor_user_id: uuid.UUID,
    action: Literal["deactivate", "reactivate", "reset-mfa"],
    _: VendorManage,
    db: DB,
) -> Any:
    if action == "reset-mfa":
        return await access.reset_vendor_mfa(get_context(), db, vendor_id, vendor_user_id)
    return await access.set_vendor_user_status(
        get_context(), db, vendor_id, vendor_user_id, active=action == "reactivate"
    )
