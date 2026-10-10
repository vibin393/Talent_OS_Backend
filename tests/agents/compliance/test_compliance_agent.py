# ruff: noqa: E501
"""Epic 62 story 62.1 / 62.2 with the Fake LLM: a templated reminder (portal + email), escalation, the custom-note
Proposal, an unknown ★ tool, no duplicate chasing, the daily tick and the pre-start risk list."""

import uuid
from typing import Any

from sqlalchemy import select

from app.core.database import tenant_session
from app.models.agent_runtime import AgentProposal, AgentRun, AgentToolCall
from app.models.chasing import ComplianceChase
from app.models.comms import Email
from app.models.notifications import Notification
from app.models.tasks import Task
from app.services import agent_runtime_service as runtime
from app.services.ai.fake_llm import FakeTurn, fake_llm
from tests.agents.conftest import AgentOrg
from tests.modules.knowledge.helpers import E62Seed, enable, grant_ai, open_portal, seed_worker


async def _start(org: AgentOrg, seed: E62Seed, turns: list[FakeTurn]) -> Any:
    with fake_llm(turns):
        async with tenant_session(org.tenant_id) as db:
            return await runtime.start_run(
                org.ctx(), db, agent_key="compliance", trigger_kind="manual", trigger_key="manual",
                subject=("candidate", seed.candidate_id), owner_user_id=org.admin.user_id, dedup_key=f"t:{uuid.uuid4()}",
            )  # fmt: skip


async def _calls(org: AgentOrg, run_id: uuid.UUID) -> tuple[AgentRun, dict[str, list[AgentToolCall]]]:
    async with tenant_session(org.tenant_id) as db:
        run = await db.get(AgentRun, run_id)
        assert run is not None
        db.expunge(run)
        out: dict[str, list[AgentToolCall]] = {}
        for c in (
            await db.execute(
                select(AgentToolCall).where(AgentToolCall.run_id == run_id).order_by(AgentToolCall.step_no)
            )
        ).scalars():
            db.expunge(c)
            out.setdefault(c.tool_key, []).append(c)
    return run, out


def _reminder(seed: E62Seed, offset: int = 7, **extra: Any) -> FakeTurn:
    args = {
        "candidate_id": str(seed.candidate_id),
        "items": [{"item_kind": "checklist_item", "item_ref": str(seed.item_id), "offset_days": offset}],
        "channels": ["portal", "email"],
        **extra,
    }
    return FakeTurn("tool_calls", calls=(("compliance.send_chase_reminder", args),))


async def _ready(org: AgentOrg, *, start_in: int = 7, portal: bool = True) -> E62Seed:
    seed = await seed_worker(org, start_in=start_in)
    await enable(org.tenant_id, "compliance")
    await grant_ai(org.tenant_id, seed.candidate_id)
    if portal:
        await open_portal(org.tenant_id, seed.candidate_id)
    return seed


async def test_happy_reminder(agent_org: AgentOrg) -> None:
    seed = await _ready(agent_org)
    ref = await _start(agent_org, seed, [
        FakeTurn("tool_calls", calls=(("compliance.get_chase_plan", {"candidate_id": str(seed.candidate_id)}),)),
        _reminder(seed),
        FakeTurn("emit_result", result={"outcome": "done", "summary": "PAN reminder sent", "reminders_sent": 1, "escalated": False}),
    ])  # fmt: skip
    run, calls = await _calls(agent_org, ref.run_id)
    assert run.status == "completed", (
        run.reason_code,
        {k: [(c.outcome, c.error_code) for c in v] for k, v in calls.items()},
    )
    assert calls["compliance.send_chase_reminder"][0].outcome == "executed"
    async with tenant_session(agent_org.tenant_id) as db:
        chases = list((await db.execute(select(ComplianceChase))).scalars())
        emails = list((await db.execute(select(Email).where(Email.origin == "agent"))).scalars())
        notes = list(
            (
                await db.execute(select(Notification).where(Notification.type == "compliance_chase_reminder"))
            ).scalars()
        )
    assert len(chases) == 1 and chases[0].offset_days == 7 and set(chases[0].channels) == {"portal", "email"}
    assert len(emails) == 1 and "This message was prepared by an AI assistant" in emails[0].body_html
    assert "PAN card" in emails[0].body_html
    assert len(notes) == 1
    # chasing twice at the same offset is refused (and the person was just contacted)
    ref2 = await _start(
        agent_org,
        seed,
        [_reminder(seed), FakeTurn("emit_result", result={"outcome": "done", "summary": "x"})],
    )
    _, calls2 = await _calls(agent_org, ref2.run_id)
    assert calls2["compliance.send_chase_reminder"][0].error_code == "item_not_due"


async def test_escalation_when_portal_never_opened(agent_org: AgentOrg) -> None:
    seed = await _ready(agent_org, start_in=3, portal=False)
    ref = await _start(agent_org, seed, [
        _reminder(seed, 3),
        FakeTurn("tool_calls", calls=(("compliance.escalate_chase", {"candidate_id": str(seed.candidate_id), "placement_id": str(seed.placement_id), "reason": "portal_not_opened"}),)),
        FakeTurn("emit_result", result={"outcome": "done", "summary": "escalated", "reminders_sent": 1, "escalated": True}),
    ])  # fmt: skip
    _, calls = await _calls(agent_org, ref.run_id)
    assert calls["compliance.escalate_chase"][0].outcome == "executed", calls["compliance.escalate_chase"][
        0
    ].error_code
    async with tenant_session(agent_org.tenant_id) as db:
        task = (await db.execute(select(Task).where(Task.entity_id == seed.placement_id))).scalar_one()
        chase = (await db.execute(select(ComplianceChase))).scalar_one()
    assert (
        task.title.startswith("Call ")
        and "portal not opened" in task.title
        and task.owner_id == agent_org.admin.user_id
    )
    assert chase.escalated and chase.task_id == task.id


async def test_escalation_refused_when_not_due(agent_org: AgentOrg) -> None:
    seed = await _ready(agent_org, start_in=7)
    ref = await _start(agent_org, seed, [
        FakeTurn("tool_calls", calls=(("compliance.escalate_chase", {"candidate_id": str(seed.candidate_id), "reason": "deadline_close"}),)),
        FakeTurn("emit_result", result={"outcome": "done", "summary": "x"}),
    ])  # fmt: skip
    _, calls = await _calls(agent_org, ref.run_id)
    assert calls["compliance.escalate_chase"][0].error_code == "escalation_not_due"


async def test_custom_note_is_a_proposal_then_sent_on_approval(agent_org: AgentOrg) -> None:
    seed = await _ready(agent_org)
    ref = await _start(agent_org, seed, [
        _reminder(seed, custom_note="Please bring the original on day one."),
        FakeTurn("emit_result", result={"outcome": "done", "summary": "proposed"}),
    ])  # fmt: skip
    _, calls = await _calls(agent_org, ref.run_id)
    assert calls["compliance.send_chase_reminder"][0].outcome == "proposed"
    async with tenant_session(agent_org.tenant_id) as db:
        p = (
            await db.execute(
                select(AgentProposal).where(AgentProposal.tool_key == "compliance.send_chase_reminder")
            )
        ).scalar_one()
        assert p.risk == "high" and (await db.execute(select(ComplianceChase))).first() is None
    resp = await agent_org.admin.client.post(
        f"/api/v1/proposals/{p.id}/decide", json={"decision": "approve"},
        headers={**agent_org.admin.h(), "Idempotency-Key": str(uuid.uuid4())},
    )  # fmt: skip
    assert resp.status_code == 200 and resp.json()["status"] == "executed", resp.text
    async with tenant_session(agent_org.tenant_id) as db:
        email = (await db.execute(select(Email).where(Email.origin == "agent"))).scalar_one()
    assert "Please bring the original on day one." in email.body_html


async def test_unknown_star_tool_is_a_policy_violation(agent_org: AgentOrg) -> None:
    seed = await _ready(agent_org)
    ref = await _start(agent_org, seed, [
        FakeTurn("tool_calls", calls=(("compliance__waive_requirement", {"item_ref": str(seed.item_id)}),)),
        FakeTurn("emit_result", result={"outcome": "done", "summary": "x"}),
    ])  # fmt: skip
    run, calls = await _calls(agent_org, ref.run_id)
    outcomes = [c.outcome for v in calls.values() for c in v]
    assert "policy_violation" in outcomes and run.policy_violations >= 1


async def test_daily_tick_and_risk_list(agent_org: AgentOrg) -> None:
    from app.services import chasing_service

    seed = await _ready(agent_org)
    with fake_llm([FakeTurn("emit_result", result={"outcome": "nothing_to_do", "summary": "x"})]):
        async with tenant_session(agent_org.tenant_id) as db:
            out = await chasing_service.ticks.compliance_tick(db, force=True)
    assert len(out["runs"]) == 1 and not out["runs"][0].startswith("not_started"), out
    async with tenant_session(agent_org.tenant_id) as db:
        run = (await db.execute(select(AgentRun).where(AgentRun.agent_key == "compliance"))).scalar_one()
    assert run.trigger_key == "daily_scan" and run.subject_id == seed.candidate_id
    resp = await agent_org.admin.client.get("/api/v1/compliance/pre-start-risk?days=14")
    assert resp.status_code == 200, resp.text
    rows = resp.json()["rows"]
    assert (
        rows[0]["placement_id"] == str(seed.placement_id)
        and rows[0]["missing_items"][0]["label"] == "PAN card"
    )
    assert rows[0]["portal_last_seen"] is not None


async def test_uploaded_item_is_not_chased(agent_org: AgentOrg) -> None:
    from app.services import chasing_service

    seed = await seed_worker(agent_org, start_in=7, item_status="pending")
    async with tenant_session(agent_org.tenant_id) as db:
        assert seed.candidate_id not in await chasing_service.chases.due_candidates(
            db, await chasing_service.chases.local_today(db)
        )
