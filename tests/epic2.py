"""Shared fixtures for Epic 2 (clients, contacts, activity) tests; imported by their conftest files."""

from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from fastapi import FastAPI

from tests.modules.identity.conftest import Member, Org, join, make_tenant

JoinAs = Callable[..., Awaitable[Member]]


@pytest.fixture
async def org(app: FastAPI) -> AsyncIterator[Org]:
    tenant = await make_tenant()
    admin = await join(app, tenant, name="Ada Admin")
    yield Org(tenant=tenant, admin=admin)
    await admin.client.aclose()


@pytest.fixture
async def other(app: FastAPI) -> AsyncIterator[Org]:
    tenant = await make_tenant()
    admin = await join(app, tenant, name="Olga Other")
    yield Org(tenant=tenant, admin=admin)
    await admin.client.aclose()


@pytest.fixture
async def join_as(app: FastAPI, org: Org) -> AsyncIterator[JoinAs]:
    """Yield a factory that signs in a new member of ``org`` holding the given role keys."""
    members: list[Member] = []

    async def _join(*role_keys: str, name: str = "Pat Member") -> Member:
        member = await join(app, org.tenant, *role_keys, name=name)
        members.append(member)
        return member

    yield _join
    for m in members:
        await m.client.aclose()
