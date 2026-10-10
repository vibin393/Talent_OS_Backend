# ruff: noqa: E501
"""Epic 63 Timesheet Agent with the Fake LLM: a nudge run (P2 ``remind`` rows ``scheduled``, one escalation Task, the
4 h gap on a second nudge), a flag run (AI explanations, a number not in the facts -> ``invalid``), "approve this
timesheet" is not a tool, and with the agent off the nudge tick starts nothing."""

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from sqlalchemy import select, update

from app.core.agentic import get_agent, tools
from app.core.database import tenant_session
from app.models.approvals import ApprovalRequest
from app.models.tasks import Task
from app.models.time import Timesheet, TimesheetReminderSent
from app.models.timesheet_flags import TimesheetFlag
from app.services import ai
from app.services.timesheet_flags import ticks
from tests.agents.finance.helpers import call, calls, outcomes, run_row, turns
from tests.modules.demands.helpers import DOrg
from tests.modules.time.helpers import active, first_timesheet, week, worker_save, worker_submit

PLAN = "time.get_nudge_plan"
REMIND = "time.send_timesheet_reminder"
ESCALATE = "time.escalate_approval"


async def _enable(org: DOrg, on: bool = True) -> None:
    async with tenant_session(org.tenant_id) as db:
        await ai.set_features(db, org.tenant_id, {"agentic": True, "agent_timesheet": on})


async def _timesheets(org: DOrg, placement_id: str) -> list[Timesheet]:
    async with tenant_session(org.tenant_id) as db:
        rows = list((await db.execute(select(Timesheet).where(Timesheet.placement_id == uuid.UUID(placement_id)).order_by(Timesheet.period_start))).scalars())
        for r in rows:
            db.expunge(r)
        return rows


def test_definition() -> None:
    spec = get_agent("timesheet")
    assert not spec.placeholder and spec.owner_epic == "63"
    registered = tools()
    assert registered[REMIND].risk == "low" and registered[REMIND].egress and registered[REMIND].send_intent is not None
    assert not any(w in t for t in spec.tools for w in ("approve", "reject", "reopen", "edit", "proxy"))


async def test_nudge_run_reminds_through_p2_and_escalates(app: FastAPI, dorg: DOrg) -> None:
    await _enable(dorg, on=False)
    s = await active(app, dorg, weeks_ago=3)
    first = await first_timesheet(s)
    assert (await worker_save(s, str(first["id"]), week(s.start, "8", "8", "8", "8", "8"))).status_code == 200
    assert (await worker_submit(s, str(first["id"]))).status_code == 200
    async with tenant_session(dorg.tenant_id) as db:
        ts = await db.get(Timesheet, uuid.UUID(str(first["id"])))
        assert ts is not None and ts.approval_request_id is not None
        await db.execute(
            update(ApprovalRequest).where(ApprovalRequest.id == ts.approval_request_id)
            .values(current_step_activated_at=datetime.now(UTC) - timedelta(days=8))
        )  # fmt: skip
    drafts = [t for t in await _timesheets(dorg, s.pid) if t.status == "draft" and t.period_end < (datetime.now(UTC) - timedelta(days=5)).date()]
    assert drafts, "a late draft is needed"
    await _enable(dorg)
    am = dorg.admin.user_id
    ids = [str(drafts[0].id), str(first["id"])]
    script = [
        call(PLAN, {"account_manager_id": str(am)}),
        call(REMIND, {"timesheet_ids": ids}),
        call(ESCALATE, {"timesheet_id": str(first["id"]), "days_pending": 6}),
        {"kind": "emit_result", "result": {"outcome": "done", "summary": "nudged", "nudges": 2, "escalations": 1}},
    ]
    from app.core.context import RequestContext
    from app.services.ai.fake_llm import fake_llm

    with fake_llm(turns(*script)):
        async with tenant_session(dorg.tenant_id) as db:
            ref = await ticks.start_nudge_run(RequestContext(tenant_id=dorg.tenant_id, actor_kind="system"), db, am)
    assert ref.run_id is not None, ref
    run = await run_row(dorg.tenant_id, ref.run_id)
    assert run.status == "completed", (run.status, run.reason_code)
    got = await calls(dorg.tenant_id, ref.run_id)
    assert [o[:2] for o in outcomes(got)] == [(PLAN, "executed"), (REMIND, "executed"), (ESCALATE, "executed")], outcomes(got)
    assert next(c for c in got if c.tool_key == REMIND).mode == "auto"
    async with tenant_session(dorg.tenant_id) as db:
        sent = list((await db.execute(select(TimesheetReminderSent).where(TimesheetReminderSent.timesheet_id.in_([uuid.UUID(i) for i in ids])))).scalars())
        tasks = list((await db.execute(select(Task).where(Task.notes.like("%[ts-escalation:%")))).scalars())
    assert sorted((r.kind, r.recipient_kind) for r in sent) == [("scheduled", "candidate"), ("scheduled", "user")]
    assert all(r.sent_by is not None for r in sent)
    assert len(tasks) == 1 and tasks[0].owner_id == am and "waiting 6 days" in tasks[0].title
    # a second nudge: no longer in today's plan (20 h window) and P2's 4 h gap answers reminder_too_soon
    from app.services import time_service, timesheet_flags_service

    ctx = RequestContext(tenant_id=dorg.tenant_id, actor_kind="system")
    async with tenant_session(dorg.tenant_id) as db:
        sent2, skipped = await timesheet_flags_service.send_reminders(ctx, db, am, [uuid.UUID(i) for i in ids], datetime.now(UTC).date())
        p2 = await time_service.remind(ctx, db, [uuid.UUID(i) for i in ids], kind="scheduled")
    assert sent2 == [] and {s_["reason"] for s_ in skipped} == {"not_in_plan"}
    assert {k.code for k in p2.skipped} == {"reminder_too_soon"} and p2.sent == []


async def test_flag_run_explains_and_refuses_unknown_numbers(app: FastAPI, dorg: DOrg) -> None:
    await _enable(dorg, on=False)
    s = await active(app, dorg)
    ts = await first_timesheet(s)
    assert (await worker_save(s, str(ts["id"]), week(s.start, "8", "8", "8", "8", "8", "4"))).status_code == 200
    assert (await worker_submit(s, str(ts["id"]))).status_code == 200
    async with tenant_session(dorg.tenant_id) as db:
        flag = (await db.execute(select(TimesheetFlag).where(TimesheetFlag.timesheet_id == uuid.UUID(str(ts["id"])), TimesheetFlag.rule == "weekend_work"))).scalar_one()
        version = flag.timesheet_version
    await _enable(dorg)
    tid = str(ts["id"])
    script = [
        call("time.list_flags", {"timesheet_id": tid}),
        call("time.get_timesheet", {"timesheet_id": tid}),
        call("time.get_context", {"timesheet_id": tid}),
        call("time.save_flag_explanations", {"timesheet_id": tid, "items": [{"flag_id": str(flag.id), "explanation": "37 hours fell on a weekend.", "group_key": "schedule"}]}),
        call("time.save_flag_explanations", {"timesheet_id": tid, "items": [{"flag_id": str(flag.id), "explanation": "4 hours were logged on a non-working day; worth confirming it was agreed.", "group_key": "schedule"}]}),
        call("time.approve_timesheet", {"timesheet_id": tid}),
        {"kind": "emit_result", "result": {"outcome": "done", "summary": "explained", "flags_explained": 1}},
    ]
    from app.services.ai.fake_llm import fake_llm

    with fake_llm(turns(*script)):
        async with tenant_session(dorg.tenant_id) as db:
            out = await ticks.start_flag_run(db, uuid.UUID(tid), version)
    run_id = uuid.UUID(str(out))
    got = await calls(dorg.tenant_id, run_id)
    saves = [(c.outcome, c.error_code) for c in got if c.tool_key == "time.save_flag_explanations"]
    assert saves == [("invalid", "explanation_unknown_number"), ("executed", None)], outcomes(got)
    assert next(c for c in got if c.tool_key in ("time.approve_timesheet", "time__approve_timesheet")).outcome == "policy_violation"
    async with tenant_session(dorg.tenant_id) as db:
        row = await db.get(TimesheetFlag, flag.id)
        ts_row = await db.get(Timesheet, uuid.UUID(tid))
    assert row is not None and row.explanation_source == "ai" and row.group_key == "schedule"
    assert ts_row is not None and ts_row.status in ("submitted", "in_approval")  # never approved by the agent


async def test_agent_off_starts_nothing(app: FastAPI, dorg: DOrg) -> None:
    await _enable(dorg, on=False)
    await active(app, dorg, weeks_ago=3)
    async with tenant_session(dorg.tenant_id) as db:
        out = await ticks.nudge_tick(db, force=True)
    assert all(str(r).startswith("not_started:") for r in out["runs"]), out
