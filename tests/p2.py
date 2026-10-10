"""Shared helpers for the Phase 2 foundation tests (Epic 13): external principals and their signed-in clients."""

import dataclasses
import uuid
from collections.abc import Sequence

import httpx
from fastapi import FastAPI

from app.core.database import platform_session, tenant_session
from app.core.security import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIES
from app.models.clients import Contact
from app.models.external_auth import ClientUser, VendorUser
from app.models.vendors import Vendor
from app.services.external_auth import sessions
from tests.modules.identity.conftest import new_client


@dataclasses.dataclass
class External:
    principal_type: str
    id: uuid.UUID
    tenant_id: uuid.UUID
    org_id: uuid.UUID
    client: httpx.AsyncClient
    token: str
    csrf: str

    def h(self) -> dict[str, str]:
        return {CSRF_HEADER: self.csrf}


async def make_client_user(
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    *,
    permissions: Sequence[str] = ("submissions:review", "demands:approve", "time:approve"),
    restricted: bool = False,
    status: str = "active",
) -> uuid.UUID:
    async with tenant_session(tenant_id) as db:
        contact = Contact(id=uuid.uuid4(), tenant_id=tenant_id, client_id=client_id, name="Cora Client", type="other")
        db.add(contact)
        await db.flush()
        cu = ClientUser(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            client_id=client_id,
            contact_id=contact.id,
            email=f"c-{uuid.uuid4().hex[:8]}@client.example.com",
            display_name="Cora Client",
            status=status,
            portal_permissions=list(permissions),
            restricted=restricted,
        )
        db.add(cu)
        return cu.id


async def make_vendor(tenant_id: uuid.UUID, *, status: str = "active") -> uuid.UUID:
    async with tenant_session(tenant_id) as db:
        vendor = Vendor(
            id=uuid.uuid4(), tenant_id=tenant_id, human_id=f"VEN-{uuid.uuid4().hex[:6]}", legal_name="Vela Vendors", status=status
        )
        db.add(vendor)
        return vendor.id


async def make_vendor_user(tenant_id: uuid.UUID, vendor_id: uuid.UUID, *, status: str = "active") -> uuid.UUID:
    async with tenant_session(tenant_id) as db:
        vu = VendorUser(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            vendor_id=vendor_id,
            email=f"v-{uuid.uuid4().hex[:8]}@vendor.example.com",
            display_name="Vik Vendor",
            status=status,
            portal_permissions=["submissions:create"],
        )
        db.add(vu)
        return vu.id


async def sign_in_external(
    app: FastAPI, principal_type: str, principal_id: uuid.UUID, tenant_id: uuid.UUID, org_id: uuid.UUID
) -> External:
    async with platform_session() as pdb:
        token, csrf = await sessions.create_session(pdb, principal_type, principal_id, tenant_id)  # type: ignore[arg-type]
    client = new_client(app)
    client.cookies.set(SESSION_COOKIES[principal_type], token)  # type: ignore[index]
    client.cookies.set(CSRF_COOKIE, csrf)
    return External(principal_type, principal_id, tenant_id, org_id, client, token, csrf)


async def sign_in_candidate(app: FastAPI, candidate_id: uuid.UUID, tenant_id: uuid.UUID) -> httpx.AsyncClient:
    from app.services import auth_service

    async with platform_session() as pdb:
        issued = await auth_service.create_session(
            pdb, principal_type="candidate", principal_id=candidate_id, tenant_id=tenant_id
        )
    client = new_client(app)
    client.cookies.set(SESSION_COOKIES["candidate"], issued.token)
    # Story 22.9: tos_csrf is bound to the session (HMAC). Helpers across epics send the placeholder header
    # ``csrf-token``; rewrite it to the bound value so they keep exercising the real double-submit check.
    from app.core.security import csrf_token_for

    bound = csrf_token_for(issued.token)
    client.cookies.set(CSRF_COOKIE, bound)

    async def _bind_csrf(request: httpx.Request) -> None:
        if request.headers.get(CSRF_HEADER) == "csrf-token":
            request.headers[CSRF_HEADER] = bound

    client.event_hooks["request"].append(_bind_csrf)
    return client
