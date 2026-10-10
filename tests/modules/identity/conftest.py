import dataclasses
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.database import platform_session, tenant_session
from app.core.security import CSRF_COOKIE, CSRF_HEADER
from app.models.identity import Role, Tenant
from app.services import access_service as access
from app.services import auth_service as service

PASSWORD = "correct horse battery"
USER_PASSWORD = "Tangerine-Voyage-42"


@dataclasses.dataclass(frozen=True)
class OperatorCreds:
    id: uuid.UUID
    email: str
    password: str


def unique_slug(prefix: str = "t") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def csrf(client: httpx.AsyncClient) -> dict[str, str]:
    return {CSRF_HEADER: client.cookies.get(CSRF_COOKIE) or ""}


def new_client(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


@pytest.fixture
async def operator() -> OperatorCreds:
    email = f"ops-{uuid.uuid4().hex[:10]}@example.com"
    out = await service.create_operator(email, "Ops Person", PASSWORD)
    return OperatorCreds(id=out.id, email=email, password=PASSWORD)


@pytest.fixture
async def op_client(app: FastAPI, operator: OperatorCreds) -> AsyncIterator[httpx.AsyncClient]:
    """Yield a client signed in as ``operator`` (session + CSRF cookies in its jar)."""
    async with new_client(app) as ac:
        resp = await ac.post(
            "/api/v1/platform/auth/login", json={"email": operator.email, "password": operator.password}
        )
        assert resp.status_code == 200, resp.text
        yield ac


async def invite(
    tenant_id: uuid.UUID,
    email: str,
    *role_keys: str,
    invited_by_type: str = "platform",
    business_unit_id: uuid.UUID | None = None,
    team_ids: tuple[uuid.UUID, ...] = (),
) -> str:
    """Seed the default roles (idempotent) and create an invitation for ``role_keys``; returns the token."""
    async with tenant_session(tenant_id) as db:
        roles = await access.seed_default_roles(db, tenant_id)
        custom = {
            r.key: r.id for r in (await db.execute(select(Role).where(Role.key.in_(role_keys)))).scalars()
        }
        issued = await service.create_invitation(
            db,
            tenant_id=tenant_id,
            email=email,
            role_ids=[{**custom, **roles}[k] for k in (role_keys or ("tenant_admin",))],
            invited_by_type=invited_by_type,
            invited_by_id=uuid.uuid4(),
            business_unit_id=business_unit_id,
            team_ids=team_ids,
        )
        return issued.token


def tenant_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "name": "Acme Staffing",
        "slug": unique_slug("acme"),
        "seat_limit": 25,
        "admin_email": f"Admin-{uuid.uuid4().hex[:8]}@Acme.example.com",
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------- signed-in tenant users (story 1.5)
@dataclasses.dataclass
class Member:
    """A signed-in tenant user (client holds its session + CSRF cookies)."""

    tenant_id: uuid.UUID
    slug: str
    email: str
    user_id: uuid.UUID
    client: httpx.AsyncClient

    def h(self) -> dict[str, str]:
        return csrf(self.client)


async def make_tenant(**overrides: Any) -> Tenant:
    async with platform_session() as db:
        tenant = Tenant(
            name="Initech Talent",
            slug=unique_slug("it"),
            timezone="Asia/Kolkata",
            currency="INR",
            locale="en-IN",
            **{"seat_limit": 10, **overrides},
        )
        db.add(tenant)
        await db.flush()
        await db.refresh(tenant)
        return tenant


async def join(app: FastAPI, tenant: Tenant, *role_keys: str, name: str = "Pat Member") -> Member:
    """Invite + accept a user holding ``role_keys`` (default Tenant Admin) into ``tenant``."""
    email = f"u-{uuid.uuid4().hex[:10]}@initech.example.com"
    token = await invite(tenant.id, email, *role_keys)
    client = new_client(app)
    resp = await client.post(
        f"/api/v1/auth/invitations/{token}/accept", json={"name": name, "password": USER_PASSWORD}
    )
    assert resp.status_code == 201, resp.text
    return Member(
        tenant_id=tenant.id,
        slug=tenant.slug,
        email=email,
        user_id=uuid.UUID(resp.json()["user"]["id"]),
        client=client,
    )


@dataclasses.dataclass
class Org:
    tenant: Tenant
    admin: Member


@pytest.fixture
async def org(app: FastAPI) -> AsyncIterator[Org]:
    """Yield a fresh tenant (seat limit 10) with a signed-in Tenant Admin."""
    tenant = await make_tenant()
    admin = await join(app, tenant, name="Ada Admin")
    yield Org(tenant=tenant, admin=admin)
    await admin.client.aclose()


@pytest.fixture
async def other_org(app: FastAPI) -> AsyncIterator[Org]:
    tenant = await make_tenant()
    admin = await join(app, tenant, name="Olga Other")
    yield Org(tenant=tenant, admin=admin)
    await admin.client.aclose()
