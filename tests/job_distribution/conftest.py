# ruff: noqa: E501, F401
"""Epic 70 test harness: a career site tenant with a live careers posting, job boards installed through the Epic 69
install flow in Fake mode (test connection passes), eager connector jobs. Fakes only; no socket is opened."""

import dataclasses
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core import bot_challenge, rate_limit
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.integrations import manifest, manifest_sha256
from app.core.permissions import CATALOG, Principal, Scope
from app.models.connectors import ProviderConnectorConfig
from app.services import ecosystem_service, oauth_service
from app.services.connectors.job_board.fake import FakeJobBoard
from app.services.connectors.job_board.feed import JobFeedAdapter
from tests.epic3 import _isolated_files, _no_real_ai
from tests.modules.demands.helpers import h
from tests.portal.conftest import Site, _site
from tests.portal.conftest import publish as publish_posting

SECRETS = {
    "naukri": {"api_key": "k-naukri", "api_secret": "s-naukri"},
    "indeed": {"client_id": "cid", "client_secret": "csec", "webhook_secret": "whsec-indeed"},
    "linkedin": {"oauth_refresh": "rt-linkedin"},
    "job_feed": {},
    "fake_job_board": {"api_key": "k-fake", "webhook_secret": "whsec-fake"},
}
CONFIG = {"naukri": {"account_id": "ACC1"}, "indeed": {"employer_id": "EMP1"}, "linkedin": {"organization_urn": "urn:li:organization:1"}}


@pytest.fixture(autouse=True)
def _env() -> Iterator[None]:
    from app.services.job_distribution import mapping

    rate_limit.memory_backend().reset()
    bot_challenge.set_verifier(None)
    oauth_service.clear_cache()
    ecosystem_service.bust_cache()
    FakeJobBoard.reset()
    JobFeedAdapter.reset()
    mapping.clear_schema_cache()
    yield
    FakeJobBoard.reset()
    JobFeedAdapter.reset()
    bot_challenge.set_verifier(None)


def human(site: Site) -> RequestContext:
    return RequestContext(
        tenant_id=site.tenant_id,
        principal=Principal(type="user", id=site.admin.user_id, tenant_id=site.tenant_id, permissions=dict.fromkeys(CATALOG, Scope.TENANT)),  # type: ignore[arg-type]
    )


async def install(site: Site, key: str, *, mode: str = "fake") -> tuple[uuid.UUID, uuid.UUID]:
    from app.schemas.marketplace import InstallIn
    from app.services import marketplace_service

    data = InstallIn(
        kind="connector", connector_key=key, secrets=SECRETS[key], accountable_user_id=site.admin.user_id,
        config={**CONFIG.get(key, {}), "mode": mode}, accept_manifest_sha256=manifest_sha256(manifest(key)),
    )  # fmt: skip
    async with tenant_session(site.tenant_id) as db:
        out = await marketplace_service.install(human(site), db, data)
        assert out.status == "active", out
    async with tenant_session(site.tenant_id) as db:
        cfg = (await db.execute(select(ProviderConnectorConfig).where(ProviderConnectorConfig.installation_id == out.id))).scalar_one()
        return out.id, cfg.id


@pytest.fixture
async def site(app: FastAPI) -> AsyncIterator[Site]:
    s = await _site(app, "Acme", "SENTINEL_CLIENT")
    yield s
    await s.admin.client.aclose()


@pytest.fixture
async def other_site(app: FastAPI) -> AsyncIterator[Site]:
    s = await _site(app, "Globex", "Other Client Ltd")
    yield s
    await s.admin.client.aclose()


async def live_posting(site: Site, **kw: Any) -> dict[str, Any]:
    kw.setdefault("location", "Bengaluru, Karnataka")
    kw.setdefault("employment_type", "permanent")
    return await publish_posting(site, **kw)


def bp_url(demand_id: str) -> str:
    return f"/api/v1/demands/{demand_id}/board-postings"


FIELDS = {"industry": "it_services", "functional_area": "engineering"}


async def post_boards(site: Site, demand_id: str, boards: list[str], fields: dict[str, dict[str, Any]] | None = None, **kw: Any) -> Any:
    body = {"board_keys": boards, "board_fields": fields if fields is not None else dict.fromkeys(boards, FIELDS), "acknowledge_bias_flags": True, **kw}
    headers = {**h(site.admin), "Idempotency-Key": str(uuid.uuid4())}
    return await site.admin.client.post(bp_url(demand_id), json=body, headers=headers)


@pytest.fixture
async def public(app: FastAPI) -> AsyncIterator[Any]:
    from tests.modules.identity.conftest import new_client

    client = new_client(app)
    yield client
    await client.aclose()
