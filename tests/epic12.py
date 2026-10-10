"""Shared fixtures / helpers for the Epic 12 tests (privacy, platform, API keys, settings, rate limits).

* Files go to a temporary ``LocalStorage`` (``_isolated_files`` from Epic 3).
* The AI gateway never touches the network; unlike the Epic 3 suites, the **real** privacy consent checker
  stays installed (``ai.set_consent_checker(None)`` restores it).
* ``mfa_operator`` is a signed-in platform operator with MFA enrolled (Epic 12 operator endpoints need it).
"""

import dataclasses
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update

from app.core import rate_limit
from app.core.database import platform_session, tenant_session
from app.core.settings import get_settings
from app.models.identity import PlatformOperator
from app.services import ai
from app.services import auth_service as identity
from tests.epic3 import (  # noqa: F401 - re-exported fixtures
    CANDIDATES,
    FakeAI,
    Org3,
    _isolated_files,
    create_candidate,
    fake_ai,
    h,
    member,
    org3,
    other3,
    parsed_payload,
    resume_pdf,
    upload,
)
from tests.modules.identity.conftest import PASSWORD, csrf, new_client

PRIVACY = "/api/v1/privacy"
PLATFORM = "/api/v1/platform"


@pytest.fixture(autouse=True)
def _privacy_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    s = get_settings()
    monkeypatch.setattr(s, "anthropic_api_key", "")
    monkeypatch.setattr(s, "clamav_host", "")
    ai.set_consent_checker(None)  # the privacy checker (installed by create_app) is the default
    ai.budget.clear_cache()
    rate_limit.memory_backend().reset()
    yield
    ai.set_client_factory(None)
    ai.set_consent_checker(None)
    ai.budget.clear_cache()


@dataclasses.dataclass
class Operator:
    id: uuid.UUID
    email: str
    client: httpx.AsyncClient

    def h(self) -> dict[str, str]:  # noqa: F811 - method, not the module helper
        return csrf(self.client)


@pytest.fixture
async def mfa_operator(app: FastAPI) -> AsyncIterator[Operator]:
    email = f"ops-{uuid.uuid4().hex[:10]}@example.com"
    out = await identity.create_operator(email, "Ops Person", PASSWORD)
    client = new_client(app)
    resp = await client.post("/api/v1/platform/auth/login", json={"email": email, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    async with platform_session() as db:  # enrolment itself is covered by story 1.4 tests
        await db.execute(update(PlatformOperator).where(PlatformOperator.id == out.id).values(mfa_enabled=True))
    yield Operator(id=out.id, email=email, client=client)
    await client.aclose()


async def candidate_row(tenant_id: uuid.UUID, candidate_id: uuid.UUID | str) -> Any:
    from app.models.candidates import Candidate

    async with tenant_session(tenant_id) as db:
        return (await db.execute(select(Candidate).where(Candidate.id == uuid.UUID(str(candidate_id))))).scalar_one()


async def set_basis(tenant_id: uuid.UUID, basis: str) -> None:
    async with tenant_session(tenant_id) as db:
        await ai.set_processing_basis(db, tenant_id, basis)


def sent(kind: str | None = None, to: str | None = None) -> list[Any]:
    """Messages captured by the outbox mailer (newest last)."""
    from app.core.mailer import get_mailer

    return [
        m
        for m in list(get_mailer().sent)  # type: ignore[attr-defined]
        if (kind is None or m.kind == kind) and (to is None or m.to == to)
    ]
