# ruff: noqa: E501
"""Epic 65 story 65.6 (FR-323, C-18): an alternative-process request stops agent screening -- active screening runs
are cancelled (``alternative_process``), the open conversation is paused with a timeline note, and a new screening
run on that Application is cancelled before step 1."""

import uuid

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.database import tenant_session
from app.models.activity import Activity
from app.models.agent_runtime import AgentRun
from app.models.fairness import AedtAlternativeRequest
from app.services import agent_runtime_service as runtime
from app.services.ai.fake_llm import FakeTurn, fake_llm
from app.services.ai_governance import aedt_guard
from tests.agents.screening_helpers import SOrg, conv_of, make_sorg, runs_of
from tests.agents.test_screening_agent import _script, _start


@pytest.fixture
async def sorg(app: FastAPI) -> SOrg:
    return await make_sorg(app)


async def _request(org: SOrg) -> uuid.UUID:
    rid = uuid.uuid4()
    async with tenant_session(org.tenant_id) as db:
        db.add(AedtAlternativeRequest(
            id=rid, tenant_id=org.tenant_id, application_id=org.application_id, candidate_id=org.candidate_id,
            demand_id=org.demand_id, channel="portal", status="open",
        ))  # fmt: skip
    return rid


async def test_alternative_request_cancels_and_pauses(sorg: SOrg) -> None:
    org = sorg
    with fake_llm(_script()) as fake:
        cid, _ = await _start(org, fake)
    assert any(r.status in ("queued", "running", "waiting") for r in await runs_of(org, cid))
    rid = await _request(org)
    async with tenant_session(org.tenant_id) as db:
        n = await aedt_guard.on_alternative_requested(db, org.tenant_id, rid)
    assert n >= 1
    runs = await runs_of(org, cid)
    assert all(r.status not in ("queued", "running", "waiting") for r in runs)
    assert any(r.reason_code == "alternative_process" for r in runs)
    conv = await conv_of(org, cid)
    assert conv.status == "paused"
    async with tenant_session(org.tenant_id) as db:
        notes = list(
            (
                await db.execute(select(Activity.body).where(Activity.entity_id == org.application_id))
            ).scalars()
        )
    assert aedt_guard.NOTE in notes


async def test_new_run_cancelled_before_step_one(sorg: SOrg, monkeypatch: pytest.MonkeyPatch) -> None:
    org = sorg
    await _request(org)
    monkeypatch.setattr(aedt_guard, "SCREENING_AGENTS", ("_test_conversational",))
    wait = FakeTurn(
        "await_event", wait={"condition": "reply", "ref": str(uuid.uuid4()), "hours": 24, "reason": "wait"}
    )
    async with tenant_session(org.tenant_id) as db:
        from app.services import ai

        await ai.set_features(db, org.tenant_id, {"agentic": True})
    with fake_llm([wait]):
        async with tenant_session(org.tenant_id) as db:
            ref = await runtime.start_run(
                org.ctx(), db, agent_key="_test_conversational", trigger_kind="manual",
                subject=("application", org.application_id), owner_user_id=org.admin.user_id,
            )  # fmt: skip
    assert ref.run_id is not None
    async with tenant_session(org.tenant_id) as db:
        cancelled = await aedt_guard.on_run_started(db, org.tenant_id, ref.run_id)
        run = await db.get(AgentRun, ref.run_id)
        assert run is not None
        status, reason = run.status, run.reason_code
    assert cancelled is True and status == "cancelled" and reason == "alternative_process"
