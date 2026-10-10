"""Shared helpers for the Epic 7 interview tests.

Builds on the Epic 6 ``porg`` fixture (tenant + admin + client + Open demand). ``submitted_app`` creates a
candidate with an email, adds it to the demand, shortlists it and moves it to Submitted with the ``system``
actor (the submission send path's move). The interviews clock is controlled with ``clock`` (``set_clock``).
"""

import dataclasses
import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.mailer import EmailMessage, OutboxMailer
from app.core.permissions import CATALOG, Principal, Scope
from app.services.connections import fake as fake_provider
from app.services.connections import store
from app.services.interviews import common
from tests.modules.identity.conftest import Member, csrf
from tests.modules.pipeline.helpers import POrg, add_one, create_candidate, shortlist

API = "/api/v1"
LINK_RE = re.compile(r"/l/([A-Za-z0-9_\-]{20,})")


def h(m: Member) -> dict[str, str]:
    return csrf(m.client)


def admin_ctx(org: POrg) -> RequestContext:
    principal = Principal(
        type="user",
        id=org.admin.user_id,
        tenant_id=org.tenant_id,
        permissions=dict.fromkeys(CATALOG, Scope.TENANT),
    )
    return RequestContext(tenant_id=org.tenant_id, principal=principal)


@dataclasses.dataclass
class Clock:
    """Controls ``app.services.interviews.common.now`` (``None`` = real time)."""

    value: datetime | None = None

    def set(self, value: datetime) -> None:
        self.value = value
        common.set_clock(lambda: self.value or datetime.now(UTC))

    def reset(self) -> None:
        self.value = None
        common.set_clock(None)


@pytest.fixture
def clock() -> Iterator[Clock]:
    c = Clock()
    yield c
    c.reset()


@pytest.fixture(autouse=True)
def _connections_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[fake_provider.FakeProvider]:
    """No real OAuth provider; in-memory state store; a clean fake provider."""
    from app.core.settings import get_settings

    s = get_settings()
    for name in (
        "google_oauth_client_id",
        "google_oauth_client_secret",
        "ms_oauth_client_id",
        "ms_oauth_client_secret",
    ):
        monkeypatch.setattr(s, name, "")
    monkeypatch.setattr(s, "calendar_provider_fake", False)
    mem = store.MemoryStore()
    store.set_store(mem)
    fake = fake_provider.instance()
    fake.reset()
    yield fake
    store.set_store(None)
    fake.reset()


def local(tz: str, delta: timedelta) -> str:
    """Naive local ``YYYY-MM-DDTHH:MM`` of ``now + delta`` in ``tz`` (rounded to the minute)."""
    at = (datetime.now(UTC) + delta).astimezone(ZoneInfo(tz)).replace(second=0, microsecond=0, tzinfo=None)
    return at.isoformat(timespec="minutes")


async def submitted_app(org: POrg, name: str = "Priya Raman") -> dict[str, str]:
    cand = await create_candidate(org.admin, name)
    app_id = await add_one(org.admin, org.demand_id, cand["id"])
    await shortlist(org.admin, app_id)
    from app.services import pipeline_service

    async with tenant_session(org.tenant_id) as db:
        await pipeline_service.transition_application(
            admin_ctx(org), db, uuid.UUID(app_id), "submitted", "system"
        )
    return {
        "application_id": app_id,
        "candidate_id": cand["id"],
        "candidate_email": cand["emails"][0]["email"],
    }


async def plan(m: Member, demand_id: str) -> list[dict[str, Any]]:
    resp = await m.client.get(f"{API}/demands/{demand_id}/interview-plan")
    assert resp.status_code == 200, resp.text
    return list(resp.json()["stages"])


def internal(user_id: uuid.UUID | str, **extra: Any) -> dict[str, Any]:
    return {"kind": "interviewer_internal", "user_id": str(user_id), **extra}


def external(email: str, name: str = "Eli External", **extra: Any) -> dict[str, Any]:
    return {"kind": "interviewer_external", "email": email, "name": name, **extra}


async def schedule(
    m: Member,
    app_id: str,
    stage_id: str,
    participants: list[dict[str, Any]],
    *,
    tz: str = "Europe/London",
    start: timedelta | None = None,
    start_local: str | None = None,
    expect: int = 201,
    **extra: Any,
) -> httpx.Response:
    body = {
        "stage_id": stage_id,
        "start_local": start_local or local(tz, start if start is not None else timedelta(days=3)),
        "timezone": tz,
        "duration_minutes": 60,
        "mode": "video",
        "meeting_url": "https://zoom.example.com/j/123",
        "participants": participants,
        **extra,
    }
    resp = await m.client.post(f"{API}/applications/{app_id}/interviews", json=body, headers=h(m))
    assert resp.status_code == expect, resp.text
    return resp


def mails(outbox: OutboxMailer, kind: str | None = None, to: str | None = None) -> list[EmailMessage]:
    return [m for m in outbox.sent if (kind is None or m.kind == kind) and (to is None or m.to == to)]


def links_for(outbox: OutboxMailer, to: str) -> list[str]:
    tokens: list[str] = []
    for msg in outbox.sent:
        if msg.to == to:
            tokens += LINK_RE.findall(msg.text)
    return list(dict.fromkeys(tokens))


async def stage_key(m: Member, app_id: str) -> str:
    resp = await m.client.get(f"{API}/applications/{app_id}")
    assert resp.status_code == 200, resp.text
    return str(resp.json()["stage"]["key"])
