# ruff: noqa: E501
"""Epic 55 stories 55.3-55.5 under the Fake LLM: duplicate flags (server-side score, below threshold, visibility),
the JD Proposal on an opened demand (approve -> unpublished version, staleness), the JD on a draft (auto, bias
flags), the SLA check (flag + tasks, no demand change), the daily scan (one digest per recipient, filtered by access)
and the no-owner scan (FR-265)."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import FastAPI
from sqlalchemy import select

from app.core.database import tenant_session
from app.models.agent_runtime import AgentProposal, AgentRun, AgentToolCall
from app.models.demand_insights import DemandFlag
from app.models.demands import Demand, DemandSlaClock, JdVersion
from app.models.notifications import Notification
from app.models.tasks import Task
from app.schemas.agentic import RunRef
from app.services import agent_runtime_service as runtime
from app.services.ai.fake_llm import fake_llm
from tests.agents.demand.conftest import DOrg, disable_triggers, h, script, start_demand_run

DEMANDS = "/api/v1/demands"


def body(org: DOrg, **over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "title": "Senior Data Engineer", "client_id": str(org.client_id), "positions": 2, "location": "Pune",
        "work_mode": "hybrid", "employment_type": "contract", "mandatory_skills": ["PySpark", "Azure Data Factory"], "country": "IN",
        "hiring_manager_contact_id": str(org.contact_id),
    }  # fmt: skip
    data.update(over)
    return data


async def create(org: DOrg, member: Any = None, **over: Any) -> dict[str, Any]:
    m = member or org.admin
    resp = await m.client.post(DEMANDS, json=body(org, **over), headers=h(m))
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def open_(org: DOrg, member: Any = None, **over: Any) -> dict[str, Any]:
    m = member or org.admin
    d = await create(org, m, **over)
    resp = await m.client.post(f"{DEMANDS}/{d['id']}/submit", headers=h(m))
    assert resp.status_code == 200 and resp.json()["status"] == "open", resp.text
    return dict(resp.json())


async def start(
    org: DOrg,
    trigger_key: str | None,
    subject: tuple[str, uuid.UUID] | None,
    *,
    kind: str = "event",
    owner: uuid.UUID | None = None,
) -> RunRef:
    return await start_demand_run(org, trigger_key, subject, owner=owner, kind=kind)  # type: ignore[no-any-return]


async def outcomes(org: DOrg, run_id: uuid.UUID) -> tuple[AgentRun, dict[str, list[AgentToolCall]]]:
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


async def test_duplicate_positive_flag(dorg: DOrg) -> None:
    await disable_triggers(dorg, "draft_created", "demand_opened")
    a = await create(dorg)
    b = await create(dorg)
    with fake_llm(script("duplicate_positive", demand_id=a["id"], related_id=b["id"])):
        ref = await start(dorg, "draft_created", ("demand", uuid.UUID(a["id"])))
    run, calls = await outcomes(dorg, ref.run_id)  # type: ignore[arg-type]
    assert run.status == "completed", (
        run.reason_code,
        {k: [(c.outcome, c.error_code) for c in v] for k, v in calls.items()},
    )
    assert calls["demands.flag_duplicate"][0].outcome == "executed"
    async with tenant_session(dorg.tenant_id) as db:
        flag = (
            await db.execute(select(DemandFlag).where(DemandFlag.demand_id == uuid.UUID(a["id"])))
        ).scalar_one()
        notes = list(
            (
                await db.execute(select(Notification).where(Notification.type == "demand_duplicate_flagged"))
            ).scalars()
        )
        demand_a = await db.get(Demand, uuid.UUID(a["id"]))
    assert (
        flag.kind == "possible_duplicate"
        and flag.related_demand_id == uuid.UUID(b["id"])
        and flag.status == "open"
    )
    assert flag.score >= Decimal("0.72")
    fields = {r["field"] for r in flag.reasons}
    assert {"title", "skills", "hiring_contact"} <= fields
    assert flag.summary == "Same role for the same hiring manager."
    assert len(notes) == 1 and a["req_id"] in notes[0].title and b["req_id"] in notes[0].title
    assert demand_a is not None and demand_a.status == "draft"  # a flag never blocks or changes the demand
    # the banner / tab read: the related demand is visible to its reader
    resp = await dorg.admin.client.get(f"{DEMANDS}/{a['id']}/insights")
    assert resp.status_code == 200, resp.text
    banner = resp.json()["flags"][0]
    assert banner["related"]["related_visible"] is True and banner["related"]["human_id"] == b["req_id"]
    # Mark as distinct clears it
    resolved = await dorg.admin.client.post(
        f"{DEMANDS}/flags/{flag.id}/resolve", json={"resolution": "distinct"}, headers=dorg.admin.h()
    )
    assert resolved.status_code == 200 and resolved.json()["status"] == "distinct"
    again = await dorg.admin.client.post(
        f"{DEMANDS}/flags/{flag.id}/resolve", json={"resolution": "dismissed"}, headers=dorg.admin.h()
    )
    assert again.status_code == 409 and again.json()["error"]["code"] == "flag_not_open"


async def test_duplicate_below_threshold(dorg: DOrg) -> None:
    await disable_triggers(dorg, "draft_created", "demand_opened")
    a = await create(dorg)
    b = await create(
        dorg,
        title="Office Administrator",
        location="Delhi",
        work_mode="onsite",
        mandatory_skills=["MS Excel"],
        hiring_manager_contact_id=None,
    )
    with fake_llm(script("duplicate_below_threshold", demand_id=a["id"], related_id=b["id"])):
        ref = await start(dorg, "draft_created", ("demand", uuid.UUID(a["id"])))
    _, calls = await outcomes(dorg, ref.run_id)  # type: ignore[arg-type]
    call = calls["demands.flag_duplicate"][0]
    assert call.outcome == "invalid" and "below_threshold" in (call.error_code or call.result_summary or "")
    async with tenant_session(dorg.tenant_id) as db:
        assert (await db.execute(select(DemandFlag))).first() is None


async def test_opened_demand_jd_is_a_proposal(dorg: DOrg) -> None:
    await disable_triggers(dorg, "draft_created", "demand_opened")
    d = await open_(dorg)
    did = uuid.UUID(d["id"])
    with fake_llm(script("opened_jd_proposal", demand_id=did)):
        ref = await start(dorg, "demand_opened", ("demand", did))
    run, calls = await outcomes(dorg, ref.run_id)  # type: ignore[arg-type]
    assert run.status == "completed" and calls["demands.update"][0].outcome == "proposed"
    async with tenant_session(dorg.tenant_id) as db:
        p = (
            await db.execute(select(AgentProposal).where(AgentProposal.tool_key == "demands.update"))
        ).scalar_one()
        versions = list((await db.execute(select(JdVersion).where(JdVersion.demand_id == did))).scalars())
    assert p.risk == "high" and p.assignee_user_id == dorg.admin.user_id and versions == []
    insights = await dorg.admin.client.get(f"{DEMANDS}/{did}/insights")
    assert insights.json()["pending_jd_proposal_id"] == str(p.id)
    resp = await dorg.admin.client.post(
        f"/api/v1/proposals/{p.id}/decide", json={"decision": "approve"},
        headers={**dorg.admin.h(), "Idempotency-Key": str(uuid.uuid4())},
    )  # fmt: skip
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "executed", resp.json()
    async with tenant_session(dorg.tenant_id) as db:
        versions = list((await db.execute(select(JdVersion).where(JdVersion.demand_id == did))).scalars())
        demand = await db.get(Demand, did)
    assert (
        len(versions) == 1
        and versions[0].author_type == "user"
        and versions[0].author_id == dorg.admin.user_id
    )
    assert demand is not None and demand.published_jd_version_id is None and demand.status == "open"


async def test_jd_proposal_goes_stale(dorg: DOrg) -> None:
    await disable_triggers(dorg, "draft_created", "demand_opened")
    d = await open_(dorg)
    did = uuid.UUID(d["id"])
    with fake_llm(script("opened_jd_proposal", demand_id=did)):
        await start(dorg, "demand_opened", ("demand", did))
    async with tenant_session(dorg.tenant_id) as db:
        p = (
            await db.execute(select(AgentProposal).where(AgentProposal.tool_key == "demands.update"))
        ).scalar_one()
    edit = await dorg.admin.client.patch(f"{DEMANDS}/{did}", json={"positions": 4}, headers=dorg.admin.h())
    assert edit.status_code == 200, edit.text
    resp = await dorg.admin.client.post(
        f"/api/v1/proposals/{p.id}/decide", json={"decision": "approve"},
        headers={**dorg.admin.h(), "Idempotency-Key": str(uuid.uuid4())},
    )  # fmt: skip
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "proposal_stale"


async def test_draft_jd_saved_automatically_with_bias_flags(dorg: DOrg) -> None:
    await disable_triggers(dorg, "draft_created", "demand_opened")
    d = await create(dorg)
    did = uuid.UUID(d["id"])
    with fake_llm(script("draft_jd_auto", demand_id=did)):
        ref = await start(dorg, "draft_created", ("demand", did))
    run, calls = await outcomes(dorg, ref.run_id)  # type: ignore[arg-type]
    assert run.status == "completed" and calls["demands.update_draft"][0].outcome == "executed"
    assert calls["demands.preview_jd"][0].outcome == "executed"
    async with tenant_session(dorg.tenant_id) as db:
        versions = list((await db.execute(select(JdVersion).where(JdVersion.demand_id == did))).scalars())
    assert len(versions) == 1 and versions[0].author_type == "ai" and "rockstar" not in versions[0].content
    # the preview's bias flags (young, rockstar) are returned to the model -- checked through the service directly
    from app.services.demand_insights.jd import bias_matches

    async with tenant_session(dorg.tenant_id) as db:
        terms = {m["term"].lower() for m in await bias_matches(db, "We want a young rockstar engineer")}
    assert {"young", "rockstar"} <= terms


async def _late_clock(org: DOrg, demand_id: uuid.UUID, milestone: str = "first_submission") -> None:
    now = datetime.now(UTC)
    async with tenant_session(org.tenant_id) as db:
        db.add(
            DemandSlaClock(
                id=uuid.uuid4(), tenant_id=org.tenant_id, demand_id=demand_id, milestone=milestone,
                started_at=now - timedelta(days=3), due_at=now - timedelta(hours=6), status="breached",
                last_notified_status="breached", target_hours=Decimal("48"), clock="calendar", at_risk_percent=75,
            )
        )  # fmt: skip


async def test_sla_check_raises_flag_and_tasks(dorg: DOrg) -> None:
    await disable_triggers(dorg, "draft_created", "demand_opened")
    d = await open_(dorg)
    did = uuid.UUID(d["id"])
    await _late_clock(dorg, did)
    async with tenant_session(dorg.tenant_id) as db:
        before = (await db.get(Demand, did)).updated_at  # type: ignore[union-attr]
    with fake_llm(script("sla_check", demand_id=did)):
        ref = await start(dorg, "sla_changed", ("demand", did))
    run, calls = await outcomes(dorg, ref.run_id)  # type: ignore[arg-type]
    assert run.status == "completed", run.reason_code
    assert [c.outcome for c in calls["tasks.create"]] == ["executed", "executed"]
    assert calls["flags.raise"][0].outcome == "executed" and calls["sla.status"][0].outcome == "executed"
    status = await dorg.admin.client.get(f"{DEMANDS}/{did}/insights")
    sla = status.json()["sla"]
    assert (
        sla["at_risk"] is True
        and sla["basis"] == "late"
        and sla["reasons"][0].startswith("First submission is")
    )
    assert any(s["label"] in ("Review matches", "Run Sourcing Agent") for s in sla["next_steps"])
    async with tenant_session(dorg.tenant_id) as db:
        tasks = list((await db.execute(select(Task).where(Task.entity_id == did))).scalars())
        after = (await db.get(Demand, did)).updated_at  # type: ignore[union-attr]
    assert len(tasks) == 2 and before == after  # no demand field changed


async def test_sla_changed_trigger_filters_status(dorg: DOrg) -> None:
    from app.core.events import _dispatch
    from app.events.demands import DemandSlaStatusChanged

    calls: list[tuple[Any, ...]] = []
    import app.tasks.runtime as rt

    original = rt.enqueue
    rt.enqueue = lambda task, *args, **kw: calls.append(args)  # type: ignore[assignment]
    try:
        did = uuid.uuid4()
        _dispatch(DemandSlaStatusChanged(dorg.tenant_id, did, "first_submission", "healthy", ()))
        _dispatch(DemandSlaStatusChanged(dorg.tenant_id, did, "first_submission", "at_risk", ()))
    finally:
        rt.enqueue = original  # type: ignore[assignment]
    mine = [c for c in calls if len(c) == 6 and c[2] == "sla_changed"]
    assert len(mine) == 1 and mine[0][5] == f"sla:{did}:first_submission:at_risk"


async def test_daily_scan_digest_per_recipient(app: FastAPI, dorg: DOrg) -> None:
    from tests.modules.identity.conftest import join

    await disable_triggers(dorg, "draft_created", "demand_opened")
    tenant_obj: Any = type("T", (), {"id": dorg.tenant_id, "slug": dorg.admin.slug})
    recruiter = await join(app, tenant_obj, "recruiter", name="Rita Recruiter")
    x = await open_(dorg, title="Platform Engineer")
    y = await open_(dorg, title="QA Engineer")
    # the recruiter becomes the primary assignee of Y only (own / team scope: cannot read X)
    resp = await dorg.admin.client.put(
        f"{DEMANDS}/{y['id']}/assignments",
        json={"user_ids": [str(recruiter.user_id)], "primary_user_id": str(recruiter.user_id)},
        headers=dorg.admin.h(),
    )
    assert resp.status_code == 200, resp.text
    for d in (x, y):
        await _late_clock(dorg, uuid.UUID(d["id"]))
    put = await dorg.admin.client.put(
        "/api/v1/demands/agent-settings",
        json={"sla_scan_owner_id": str(dorg.admin.user_id)},
        headers=dorg.admin.h(),
    )
    assert put.status_code == 200, put.text
    with fake_llm(script("sla_scan", demand_id=x["id"], other_demand_id=y["id"])):
        ref = await start(dorg, "daily_sla_scan", None, kind="schedule", owner=dorg.admin.user_id)
    run, calls = await outcomes(dorg, ref.run_id)  # type: ignore[arg-type]
    assert run.status == "completed", run.reason_code
    assert calls["sla.send_digest"][0].outcome == "executed"
    async with tenant_session(dorg.tenant_id) as db:
        digests = {
            n.user_id: n
            for n in (
                await db.execute(select(Notification).where(Notification.type == "demand_sla_digest"))
            ).scalars()
        }
        flags = list((await db.execute(select(DemandFlag).where(DemandFlag.kind == "sla_risk"))).scalars())
    assert len(flags) == 2 and all(f.basis == "late" for f in flags)
    rita = digests[recruiter.user_id]
    assert y["req_id"] in (rita.body or "") and x["req_id"] not in (rita.body or "")
    ada = digests[dorg.admin.user_id]
    assert x["req_id"] in (ada.body or "")
    await recruiter.client.aclose()


async def test_scan_without_owner_creates_task(dorg: DOrg) -> None:
    async with tenant_session(dorg.tenant_id) as db:
        ref = await runtime.start_run(
            dorg.ctx(),
            db,
            agent_key="demand",
            trigger_kind="schedule",
            trigger_key="daily_sla_scan",
            dedup_key="sched:test",
        )
    assert ref.run_id is None and ref.reason == "owner_unavailable"
    async with tenant_session(dorg.tenant_id) as db:
        tasks = list(
            (
                await db.execute(select(Task).where(Task.title.like("Agent could not run: no owner%")))
            ).scalars()
        )
    assert len(tasks) == 1
