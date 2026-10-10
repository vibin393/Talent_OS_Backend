# ruff: noqa: E501, F811
"""Epic 59 Scheduling Agent test harness.

* ``korg`` -- a tenant (Tenant Admin = Application owner and the first interviewer, calendar connected through the fake
  provider), an open demand with the default interview plan, a Candidate (email, Bengaluru -> ``Asia/Kolkata``) and an
  Application in Submitted; ``agentic`` + ``agent_scheduling`` on (no burn-in: features set directly). The admin's
  working hours are all day, every day (so slots exist whatever day the suite runs).
* Model calls use the scripted Fake LLM keyed by prompt (``agent_scheduling``); ``offer_ids`` makes the offer ids of
  ``scheduling.propose`` predictable for the script (``await_event`` refs).
* The calendar provider is the in-memory fake (``fake.busy`` per account email); nothing touches the network.
"""

import dataclasses
import itertools
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.permissions import CATALOG, Principal, Scope
from app.core.settings import get_settings
from app.models.agent_runtime import AgentRun, AgentToolCall
from app.models.scheduling_agent import SchedulingRequest, SchedulingSlotOffer
from app.services import ai
from app.services.ai.fake_llm import FakeTurn
from app.services.connections import fake as fake_provider
from tests.modules.identity.conftest import Member, csrf
from tests.modules.interviews.helpers import _connections_env, plan, submitted_app  # noqa: F401
from tests.modules.pipeline.helpers import POrg, _porg

API = "/api/v1"
ALL_DAY = {d: [["00:00", "24:00"]] for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}


@dataclasses.dataclass
class KOrg:
    porg: POrg
    application_id: uuid.UUID
    candidate_id: uuid.UUID
    candidate_email: str
    stage_id: uuid.UUID
    fake: fake_provider.FakeProvider

    @property
    def tenant_id(self) -> uuid.UUID:
        return self.porg.tenant.id

    @property
    def admin(self) -> Member:
        return self.porg.admin

    def ctx(self) -> RequestContext:
        return RequestContext(
            tenant_id=self.tenant_id,
            principal=Principal(type="user", id=self.admin.user_id, tenant_id=self.tenant_id, permissions=dict.fromkeys(CATALOG, Scope.TENANT)),
        )  # fmt: skip


async def connect(m: Member, provider: str = "google") -> None:
    start = await m.client.post(f"{API}/connections/{provider}/start", headers=csrf(m.client))
    assert start.status_code == 200, start.text
    state = parse_qs(urlparse(start.json()["authorize_url"]).query)["state"][0]
    resp = await m.client.get(
        f"{API}/connections/{provider}/callback",
        params={"code": "organiser", "state": state},
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.text


async def set_all_day(m: Member, tz: str = "Asia/Kolkata") -> None:
    resp = await m.client.put(
        f"{API}/me/scheduling-preferences",
        json={"timezone": tz, "working_hours": ALL_DAY, "holiday_country": "IN"},
        headers=csrf(m.client),
    )
    assert resp.status_code == 200, resp.text


async def make_korg(app: FastAPI, fake: fake_provider.FakeProvider, monkeypatch: pytest.MonkeyPatch) -> KOrg:
    monkeypatch.setattr(get_settings(), "calendar_provider_fake", True)
    porg = await _porg(app, "Ada Admin")
    fake.account_email = porg.admin.email.lower()
    await connect(porg.admin)
    a = await submitted_app(porg)
    async with tenant_session(porg.tenant.id) as db:
        await ai.set_features(db, porg.tenant.id, {"agentic": True, "agent_scheduling": True})
        from app.models.candidates import Candidate

        cand = await db.get(Candidate, uuid.UUID(a["candidate_id"]))
        assert cand is not None
        cand.location = {"city": "Bengaluru", "state": "KA", "country": "India"}
    await set_all_day(porg.admin)
    stages = await plan(porg.admin, porg.demand_id)
    # candidate hours all day too (the suite may run at any hour)
    resp = await porg.admin.client.put(
        f"{API}/settings/agents/scheduling",
        json={"candidate_hours": {"start": "00:00", "end": "24:00"}, "default_buffer_minutes": 0},
        headers=csrf(porg.admin.client),
    )
    assert resp.status_code == 200, resp.text
    return KOrg(
        porg,
        uuid.UUID(a["application_id"]),
        uuid.UUID(a["candidate_id"]),
        a["candidate_email"],
        uuid.UUID(stages[0]["id"]),
        fake,
    )


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", False)
    monkeypatch.setattr(get_settings(), "anthropic_api_key", "fake-test-key")


@pytest.fixture
async def korg(
    app: FastAPI, _connections_env: fake_provider.FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[KOrg]:
    org = await make_korg(app, _connections_env, monkeypatch)
    yield org
    await org.admin.client.aclose()


@pytest.fixture
def offer_ids(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[uuid.UUID]]:
    """Predictable ids for the offers ``scheduling.propose`` creates (the script's ``await_event`` refs)."""
    from app.services.scheduling_agent import service

    ids = [uuid.uuid4() for _ in range(8)]
    it = itertools.chain(ids, iter(uuid.uuid4, None))
    monkeypatch.setattr(service, "uuid", SimpleNamespace(uuid4=lambda: next(it), UUID=uuid.UUID))
    yield ids


def calls(*items: tuple[str, dict[str, Any]]) -> FakeTurn:
    return FakeTurn("tool_calls", calls=tuple(items))


def wait(ref: uuid.UUID) -> FakeTurn:
    return FakeTurn(
        "await_event",
        wait={"condition": "slot_pick", "ref": str(ref), "hours": 168, "reason": "waiting for the candidate"},
    )


def done(outcome: str = "done") -> FakeTurn:
    return FakeTurn("emit_result", result={"outcome": outcome, "summary": outcome})


def offer_script(k: KOrg, req_id: uuid.UUID, offer_id: uuid.UUID, channel: str = "email") -> list[FakeTurn]:
    return [
        calls(("interviews.participants", {"request_id": str(req_id)})),
        calls(("scheduling.propose", {"request_id": str(req_id)})),
        calls(
            (
                "scheduling.offer_slots",
                {"offer_id": str(offer_id), "candidate_id": str(k.candidate_id), "channel": channel},
            )
        ),
        wait(offer_id),
    ]


async def request_of(k: KOrg, req_id: uuid.UUID) -> SchedulingRequest:
    async with tenant_session(k.tenant_id) as db:
        row = await db.get(SchedulingRequest, req_id)
        assert row is not None
        db.expunge(row)
        return row


async def offers_of(k: KOrg, req_id: uuid.UUID) -> list[SchedulingSlotOffer]:
    async with tenant_session(k.tenant_id) as db:
        rows = list(
            (
                await db.execute(
                    select(SchedulingSlotOffer)
                    .where(SchedulingSlotOffer.request_id == req_id)
                    .order_by(SchedulingSlotOffer.round, SchedulingSlotOffer.created_at)
                )
            ).scalars()
        )
        for r in rows:
            db.expunge(r)
        return rows


async def runs_of(k: KOrg) -> list[AgentRun]:
    async with tenant_session(k.tenant_id) as db:
        rows = list(
            (
                await db.execute(
                    select(AgentRun).where(AgentRun.agent_key == "scheduling").order_by(AgentRun.created_at)
                )
            ).scalars()
        )
        for r in rows:
            db.expunge(r)
        return rows


async def tool_calls(k: KOrg, run_id: uuid.UUID) -> list[AgentToolCall]:
    async with tenant_session(k.tenant_id) as db:
        rows = list(
            (
                await db.execute(
                    select(AgentToolCall)
                    .where(AgentToolCall.run_id == run_id)
                    .order_by(AgentToolCall.step_no, AgentToolCall.created_at)
                )
            ).scalars()
        )
        for r in rows:
            db.expunge(r)
        return rows


def soon(hours: int = 48) -> timedelta:
    return timedelta(hours=hours)
