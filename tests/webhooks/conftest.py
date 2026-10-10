# ruff: noqa: E501
"""Epic 68 webhook test harness: hooks discovered, a stub DNS resolver (public / private answers per host) and a fake
HTTP transport (``httpx.MockTransport``) -- tests never call real URLs. ``Receiver`` records every request and answers
with a programmable status; by default it echoes the verification challenge."""

import json
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.core import net_safety
from app.core.database import tenant_session
from app.core.hooks import autodiscover
from app.models.public_events import OutboxEvent
from app.models.webhooks import WebhookSubscription
from app.services import ecosystem_service, oauth_service
from app.services.public_events.outbox import to_row
from app.services.webhooks import common, delivery
from tests.epic12 import _isolated_files, _privacy_env, fake_ai, org3, other3  # noqa: F401

PRIVATE_HOSTS: set[str] = {"private.example.com"}


@dataclass
class Receiver:
    status: int | Callable[[httpx.Request], int] = 200
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None
    requests: list[httpx.Request] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        content = request.content
        try:
            doc = json.loads(content)
        except ValueError:
            doc = {}
        if self.body is not None:
            out = self.body
        elif isinstance(doc, dict) and doc.get("type") == "webhook.verification":
            out = json.dumps({"challenge": doc["data"]["challenge"]}).encode()
        else:
            out = b"ok"
        code = self.status(request) if callable(self.status) else self.status
        return httpx.Response(code, headers=self.headers, content=out)


@pytest.fixture(scope="session", autouse=True)
def _discovered() -> None:
    autodiscover("app.services")


@pytest.fixture(autouse=True)
def _fresh() -> None:
    oauth_service.clear_cache()
    ecosystem_service.bust_cache()


@pytest.fixture
def receiver() -> Iterator[Receiver]:
    rec = Receiver()

    async def resolve(host: str) -> list[str]:
        return ["10.0.0.7"] if host in PRIVATE_HOSTS else ["93.184.216.34"]

    net_safety.set_resolver(resolve)
    delivery.set_transport(lambda: httpx.MockTransport(rec.handle))
    yield rec
    delivery.set_transport(None)
    net_safety.set_resolver(None)


@pytest.fixture
def manual_pump() -> Iterator[None]:
    """Drive the engine step by step (no automatic pump after commit)."""
    common.AUTO_KICK["on"] = False
    yield
    common.AUTO_KICK["on"] = True


async def sub_row(tenant_id: uuid.UUID, sub_id: uuid.UUID) -> WebhookSubscription:
    async with tenant_session(tenant_id) as db:
        return (await db.execute(select(WebhookSubscription).where(WebhookSubscription.id == sub_id))).scalar_one()


async def outbox_row(tenant_id: uuid.UUID, event_type: str = "placement.started", data: dict[str, Any] | None = None,
                     subject_type: str = "placement", subject_id: uuid.UUID | None = None, installation_id: uuid.UUID | None = None) -> Any:  # fmt: skip
    """Insert a dispatched public outbox row (the sink's input) and return its ``OutboxRow``."""
    sid = subject_id or uuid.uuid4()
    async with tenant_session(tenant_id) as db:
        e = OutboxEvent(
            id=uuid.uuid4(), tenant_id=tenant_id, event_type=event_type, event_version=1, visibility="public",
            subject_type=subject_type, subject_id=sid, occurred_at=common.now(),
            data=data if data is not None else {"id": str(sid), "candidate_id": str(uuid.uuid4()), "start_date": "2026-10-10"},
            source_event="Test", actor_kind="user", app_installation_id=installation_id, dispatch_status="dispatched",
        )  # fmt: skip
        db.add(e)
        await db.flush()
        await db.refresh(e)
        return to_row(e)
