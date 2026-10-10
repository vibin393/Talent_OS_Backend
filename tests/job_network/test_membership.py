# ruff: noqa: E501
"""Membership and opt-in (Epic 75, story 75.1; FR-386)."""

import uuid

import pytest

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import Forbidden
from app.core.permissions import CATALOG, Principal, Scope
from app.schemas.job_network import NetworkJoinIn
from app.services import job_network_service as svc
from tests.job_network.conftest import Site
from tests.modules.demands.helpers import h

JOIN = "/api/v1/job-network/join"
SETTINGS = "/api/v1/job-network/settings"


async def test_join_without_verification_returns_422_with_missing(site: Site) -> None:
    resp = await site.admin.client.post(JOIN, json={"terms_version": "2026-10", "list_mode": "all", "show_logo": True}, headers=h(site.admin))
    assert resp.status_code == 422, resp.text
    err = resp.json()["error"]
    assert err["code"] == "network_verification_required"
    missing = set(err["details"]["missing"])
    assert "gstin" in missing and "legal_entity" in missing


async def test_terms_outdated_returns_409(site: Site) -> None:
    resp = await site.admin.client.post(JOIN, json={"terms_version": "1999-01", "list_mode": "all", "show_logo": True}, headers=h(site.admin))
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "terms_outdated"


async def test_settings_available_before_join(site: Site) -> None:
    resp = await site.admin.client.get(SETTINGS, headers=h(site.admin))
    assert resp.status_code == 200, resp.text
    assert resp.json()["joined"] is False


async def test_join_refused_for_api_key_actor(site: Site) -> None:
    ctx = RequestContext(
        tenant_id=site.tenant_id,
        principal=Principal(type="api_key", id=uuid.uuid4(), tenant_id=site.tenant_id, permissions=dict.fromkeys(CATALOG, Scope.TENANT)),  # type: ignore[arg-type]
        actor_kind="api_key",
    )
    async with tenant_session(site.tenant_id) as db:
        with pytest.raises(Forbidden) as exc:
            await svc.join(ctx, db, NetworkJoinIn(terms_version="2026-10", list_mode="all", show_logo=True))
    assert exc.value.code == "human_only_action"
