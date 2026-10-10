"""Fixtures for the Epic 13 legal-entity tests: a tenant with its provisioned default Legal Entity and a signed-in
Tenant Admin; GSTIN builders."""

import dataclasses
import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI

from app.core.database import tenant_session
from app.models.identity import Tenant
from app.services import legal_entities_service
from app.services.legal_entities.gstin import check_character
from tests.modules.identity.conftest import Member, join, make_tenant, op_client, operator  # noqa: F401

API = "/api/v1"


@dataclasses.dataclass
class LeOrg:
    tenant: Tenant
    admin: Member
    entity_id: uuid.UUID

    @property
    def tenant_id(self) -> uuid.UUID:
        return self.tenant.id


def gstin(state: str = "29", pan: str = "ABCDE1234F", entity: str = "1") -> str:
    first14 = f"{state}{pan}{entity}Z"
    return first14 + check_character(first14)


async def provision(tenant: Tenant) -> uuid.UUID:
    async with tenant_session(tenant.id) as db:
        return (await legal_entities_service.provision_default_entity(db, tenant)).id


async def _org(app: FastAPI) -> LeOrg:
    tenant = await make_tenant(seat_limit=20)
    entity_id = await provision(tenant)
    admin = await join(app, tenant, name="Lena Ledger")
    return LeOrg(tenant=tenant, admin=admin, entity_id=entity_id)


@pytest.fixture
async def leorg(app: FastAPI) -> AsyncIterator[LeOrg]:
    org = await _org(app)
    yield org
    await org.admin.client.aclose()


@pytest.fixture
async def other_leorg(app: FastAPI) -> AsyncIterator[LeOrg]:
    org = await _org(app)
    yield org
    await org.admin.client.aclose()
