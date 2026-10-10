# ruff: noqa: E501, F401, F811
"""Epic 72 calendar sync harness: the Epic 59 ``korg`` (Tenant Admin = organiser, Google connected through the in-memory
Fake provider, a Candidate with an Application in Submitted), calendar sync turned on, the Scheduling Agent off by
default (reschedule requests become Tasks) and one Interview scheduled 3 days ahead -- which writes the Talent OS event
(with the version tag), the organiser link and the push channel. Nothing touches the network."""

import dataclasses
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.database import tenant_session
from app.models.calendar_sync import CalendarChannel, CalendarEventLink
from app.models.interviews import Interview, InterviewParticipant
from app.models.tasks import Task
from app.services import ai
from app.services.connections import fake as fake_provider
from tests.agents.conftest import _agent_env, _agents_registered
from tests.agents.scheduling.conftest import KOrg, _env, make_korg
from tests.modules.identity.conftest import csrf
from tests.modules.interviews.helpers import _connections_env, internal, schedule

API = "/api/v1"


@dataclasses.dataclass
class COrg:
    k: KOrg
    interview_id: uuid.UUID

    @property
    def tenant_id(self) -> uuid.UUID:
        return self.k.tenant_id

    @property
    def fake(self) -> fake_provider.FakeProvider:
        return self.k.fake

    @property
    def client(self) -> Any:
        return self.k.admin.client


async def put_settings(k: KOrg, **body: Any) -> dict[str, Any]:
    resp = await k.admin.client.put(f"{API}/calendar-sync/settings", json=body, headers=csrf(k.admin.client))
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


async def make_corg(app: FastAPI, fake: fake_provider.FakeProvider, monkeypatch: pytest.MonkeyPatch, *, agent: bool = False) -> COrg:
    k = await make_korg(app, fake, monkeypatch)
    async with tenant_session(k.tenant_id) as db:
        await ai.set_features(db, k.tenant_id, {"agentic": True, "agent_scheduling": agent})
    await put_settings(k, enabled=True)
    resp = await schedule(k.admin, str(k.application_id), str(k.stage_id), [internal(k.admin.user_id)], start=timedelta(days=3))
    return COrg(k, uuid.UUID(resp.json()["id"]))


@pytest.fixture
async def corg(app: FastAPI, _connections_env: fake_provider.FakeProvider, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[COrg]:
    org = await make_corg(app, _connections_env, monkeypatch)
    yield org
    await org.k.admin.client.aclose()


async def interview(c: COrg) -> Interview:
    async with tenant_session(c.tenant_id) as db:
        row = await db.get(Interview, c.interview_id)
        assert row is not None
        db.expunge(row)
        return row


async def channel(c: COrg) -> CalendarChannel:
    async with tenant_session(c.tenant_id) as db:
        row = (await db.execute(select(CalendarChannel).order_by(CalendarChannel.created_at.desc()))).scalars().first()
        assert row is not None
        db.expunge(row)
        return row


async def link(c: COrg) -> CalendarEventLink:
    async with tenant_session(c.tenant_id) as db:
        row = (
            await db.execute(
                select(CalendarEventLink).where(CalendarEventLink.interview_id == c.interview_id, CalendarEventLink.role == "organizer", CalendarEventLink.state != "detached")
            )
        ).scalars().first()
        assert row is not None
        db.expunge(row)
        return row


async def participants(c: COrg) -> dict[str, InterviewParticipant]:
    async with tenant_session(c.tenant_id) as db:
        rows = list((await db.execute(select(InterviewParticipant).where(InterviewParticipant.interview_id == c.interview_id))).scalars())
        for r in rows:
            db.expunge(r)
        return {r.kind: r for r in rows}


async def tasks(c: COrg) -> list[str]:
    async with tenant_session(c.tenant_id) as db:
        return list((await db.execute(select(Task.title).where(Task.entity_type == "calendar_change", Task.status == "open"))).scalars())


async def sync(c: COrg, *, full: bool = False) -> dict[str, Any]:
    from app.services import calendar_sync_service as svc

    ch = await channel(c)
    async with tenant_session(c.tenant_id) as db:
        return await svc.run_delta(db, ch.channel_id, full=full)
