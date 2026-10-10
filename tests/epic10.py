"""Shared fixtures / helpers for the Epic 10 tests (comms, tasks, notifications, workflow).

Celery runs eagerly in tests (``CELERY_TASK_ALWAYS_EAGER``), so after-commit handlers deliver emails, fan out
notifications and run workflow rules before the request returns. Emails go to the throwaway outbox.
"""

import dataclasses
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, text

from app.core import mailer
from app.core.database import tenant_session
from app.core.settings import get_settings
from app.services import comms_service
from tests.modules.demands.helpers import DOrg, create_client, create_demand, dorg, h, open_demand, other_dorg
from tests.modules.identity.conftest import Member, join

__all__ = ["DOrg", "FakeMailbox", "create_client", "create_demand", "dorg", "h", "open_demand", "other_dorg"]

API = "/api/v1"
TEMPLATES = f"{API}/email-templates"
EMAILS = f"{API}/emails"
TASKS = f"{API}/tasks"
NOTIFICATIONS = f"{API}/notifications"
PREFS = f"{API}/me/notification-preferences"
RULES = f"{API}/workflow/rules"
WORKFLOW = f"{API}/workflow"


# ================================================================ outbox / mailbox
@pytest.fixture
def outbox() -> Iterator[mailer.OutboxMailer]:
    box = mailer.get_mailer()
    assert isinstance(box, mailer.OutboxMailer)
    box.sent.clear()
    yield box


@dataclasses.dataclass
class FakeMailbox:
    """Test double of the Epic 7 mailbox provider: ``mode`` = ok | expired | none | error."""

    provider: str = "microsoft"
    mode: str = "ok"
    sent: list[mailer.EmailMessage] = dataclasses.field(default_factory=list)

    async def get_send_connection(
        self, ctx: Any, db: Any, user_id: uuid.UUID
    ) -> comms_service.SendConnection | None:
        if self.mode == "none":
            return None
        return comms_service.SendConnection(provider=self.provider, account_email="me@example.com")

    async def send_mail(self, conn: comms_service.SendConnection, message: mailer.EmailMessage) -> str:
        if self.mode == "expired":
            raise comms_service.ConnectionExpired("token expired")
        if self.mode == "error":
            msg = "provider down"
            raise RuntimeError(msg)
        self.sent.append(message)
        return f"prov-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def mailbox() -> Iterator[FakeMailbox]:
    fake = FakeMailbox()
    comms_service.set_mailbox_provider(fake)
    yield fake
    comms_service.set_mailbox_provider(None)


@pytest.fixture
def webhook_secret(monkeypatch: pytest.MonkeyPatch) -> str:
    secret = "test-webhook-secret"
    monkeypatch.setattr(get_settings(), "email_webhook_secret", secret)
    return secret


# ================================================================ data helpers
async def member(app: FastAPI, org: DOrg, *roles: str, name: str = "Rita Recruiter") -> Member:
    return await join(app, org.tenant, *roles, name=name)


async def candidate(m: Member, email: str | None = "priya.sharma@example.com", **body: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"full_name": "Priya Sharma", **body}
    if email:
        payload["emails"] = [{"email": email, "is_primary": True}]
    resp = await m.client.post(f"{API}/candidates", json=payload, headers=h(m))
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def contact(
    m: Member, client_id: uuid.UUID, email: str | None = "rahul.mehta@example.com"
) -> dict[str, Any]:
    body: dict[str, Any] = {"name": "Rahul Mehta", "type": "hiring_manager"}
    if email:
        body["email"] = email
    resp = await m.client.post(f"{API}/clients/{client_id}/contacts", json=body, headers=h(m))
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def count(tenant_id: uuid.UUID, table: str, where: str = "true", **params: Any) -> int:
    async with tenant_session(tenant_id) as db:
        return int(
            (await db.execute(text(f"SELECT count(*) FROM {table} WHERE {where}"), params)).scalar_one()
        )


async def rows(tenant_id: uuid.UUID, sql: str, **params: Any) -> list[Any]:
    async with tenant_session(tenant_id) as db:
        return list((await db.execute(text(sql), params)).mappings().all())


async def scalar_count(tenant_id: uuid.UUID, stmt: Any) -> int:
    async with tenant_session(tenant_id) as db:
        return int((await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one())


@pytest.fixture
async def recruiter(app: FastAPI, dorg: DOrg) -> AsyncIterator[Member]:
    m = await member(app, dorg, "recruiter", name="Rita Recruiter")
    yield m
    await m.client.aclose()
