# ruff: noqa: E501
"""Epic 32 story 19.2 (FR-170): escalation policies, watches and firing -- notify / reassign only, never a
decision."""

import ast
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.database import tenant_session
from app.models.approvals import ApprovalRequest
from app.models.sla import EscalationWatch
from app.schemas.approvals import DecideIn
from app.services import approvals, directory_service
from app.services.approvals import EscalationSpec, StepSpec
from app.services.sla import escalations
from tests.epic10 import API, DOrg, h, member
from tests.modules.identity.conftest import Member
from tests.sla.helpers import SLA, admin_ctx, rows, scalar

SUBJECT = "e32_escalation_subject"


@pytest.fixture
def subject() -> Iterator[list[Any]]:
    finals: list[Any] = []

    async def on_final(ctx: Any, db: Any, decision: Any) -> None:
        finals.append(decision)

    approvals.register_approval_subject(SUBJECT, on_final_decided=on_final)
    yield finals
    approvals.unregister_approval_subject(SUBJECT)


async def _team(dorg: DOrg, members: dict[uuid.UUID, bool]) -> None:
    a = dorg.admin
    resp = await a.client.post(
        f"{API}/admin/teams", json={"name": f"Team {uuid.uuid4().hex[:6]}"}, headers=h(a)
    )
    assert resp.status_code == 201, resp.text
    team_id = resp.json()["id"]
    for user_id, is_lead in members.items():
        resp = await a.client.put(
            f"{API}/admin/teams/{team_id}/members/{user_id}", json={"is_lead": is_lead}, headers=h(a)
        )
        assert resp.status_code == 200, resp.text


async def _start(dorg: DOrg, approver: uuid.UUID, spec: EscalationSpec | None) -> tuple[uuid.UUID, uuid.UUID]:
    ctx = await admin_ctx(dorg.tenant_id, dorg.admin.user_id)
    subject_id = uuid.uuid4()
    async with tenant_session(dorg.tenant_id) as db:
        result = await approvals.start(
            ctx,
            db,
            SUBJECT,
            subject_id,
            {},
            steps=[StepSpec("Manager review", "user", {"user_id": str(approver)}, escalation=spec)],
        )
    assert result.request_id is not None
    return result.request_id, subject_id


async def _watch(tenant_id: uuid.UUID, request_id: uuid.UUID) -> EscalationWatch:
    async with tenant_session(tenant_id) as db:
        watch = (
            await db.execute(select(EscalationWatch).where(EscalationWatch.subject_id == request_id))
        ).scalar_one()
        db.expunge(watch)
        return watch


async def _evaluate(tenant_id: uuid.UUID, at: datetime) -> int:
    async with tenant_session(tenant_id) as db:
        return await escalations.evaluate_escalations(db, now=at)


async def _request(tenant_id: uuid.UUID, request_id: uuid.UUID) -> ApprovalRequest:
    async with tenant_session(tenant_id) as db:
        row = await db.get(ApprovalRequest, request_id)
        assert row is not None
        db.expunge(row)
        return row


def _spec(
    after: int = 8,
    action: str = "notify",
    target: str = "manager_of_assignee",
    ref: uuid.UUID | None = None,
    **kw: Any,
) -> EscalationSpec:
    return EscalationSpec(
        after_business_hours=Decimal(after), action=action, target_kind=target, target_ref=ref, **kw
    )  # type: ignore[arg-type]


async def _notified(tenant_id: uuid.UUID, user_id: uuid.UUID) -> int:
    return int(
        await scalar(
            tenant_id,
            "SELECT count(*) FROM notifications WHERE type = 'escalation_raised' AND user_id = :u",
            u=user_id,
        )
    )


@pytest.fixture
async def people(app: FastAPI, dorg: DOrg) -> Any:
    approver = await member(app, dorg, "recruiter", name="Arun Approver")
    lead = await member(app, dorg, "recruiter", name="Leela Lead")
    yield approver, lead
    await approver.client.aclose()
    await lead.client.aclose()


async def test_notify_after_8_hours_repeat_twice_then_done(
    dorg: DOrg, subject: Any, people: tuple[Member, Member]
) -> None:
    approver, lead = people
    await _team(dorg, {approver.user_id: False, lead.user_id: True})
    request_id, subject_id = await _start(
        dorg, approver.user_id, _spec(8, repeat_every_hours=Decimal(8), max_repeats=2)
    )
    watch = await _watch(dorg.tenant_id, request_id)
    assert watch.status == "active" and watch.step_position == 1
    started = watch.started_at
    assert watch.next_fire_at == started + timedelta(hours=8)  # no work week configured: calendar hours

    assert await _evaluate(dorg.tenant_id, started + timedelta(hours=7)) == 0
    assert await _evaluate(dorg.tenant_id, started + timedelta(hours=8, minutes=1)) == 1
    assert await _notified(dorg.tenant_id, lead.user_id) == 1
    assert await _notified(dorg.tenant_id, approver.user_id) == 1  # notify_original_assignee
    timeline = await rows(
        dorg.tenant_id,
        "SELECT body, data FROM activities WHERE entity_type = :t AND entity_id = :i",
        t=SUBJECT,
        i=subject_id,
    )
    assert len(timeline) == 1 and timeline[0].body.startswith(
        "Escalated to the team lead after 8 business hours (1 of 3)"
    )
    assert timeline[0].data["target_user_ids"] == [str(lead.user_id)]
    assert (
        await scalar(dorg.tenant_id, "SELECT count(*) FROM audit_logs WHERE action = 'escalation.fired'") == 1
    )

    assert await _evaluate(dorg.tenant_id, started + timedelta(hours=16, minutes=1)) == 1
    assert await _evaluate(dorg.tenant_id, started + timedelta(hours=24, minutes=1)) == 1
    assert await _evaluate(dorg.tenant_id, started + timedelta(hours=60)) == 0
    watch = await _watch(dorg.tenant_id, request_id)
    assert watch.status == "done" and watch.fires == 3 and len(watch.history) == 3
    assert await _notified(dorg.tenant_id, lead.user_id) == 3
    assert (await _request(dorg.tenant_id, request_id)).status == "pending"


async def test_reassign_to_role_named_user_and_team_lead(
    app: FastAPI, dorg: DOrg, subject: Any, people: tuple[Member, Member]
) -> None:
    approver, lead = people
    await _team(dorg, {approver.user_id: False, lead.user_id: True})
    async with tenant_session(dorg.tenant_id) as db:
        role_id = await directory_service.role_id_by_key(db, "recruiter")
    named = await member(app, dorg, "recruiter", name="Nina Named")

    r_role, _ = await _start(dorg, approver.user_id, _spec(1, "reassign", "role", role_id))
    r_user, _ = await _start(dorg, approver.user_id, _spec(1, "reassign", "user", named.user_id))
    r_lead, _ = await _start(dorg, approver.user_id, _spec(1, "reassign", "manager_of_assignee"))
    later = datetime.now(UTC) + timedelta(hours=2)
    assert await _evaluate(dorg.tenant_id, later) == 3

    req = await _request(dorg.tenant_id, r_role)
    assert (req.current_approver_kind, req.current_approver_role_id, req.current_approver_user_id) == (
        "role",
        role_id,
        None,
    )
    req = await _request(dorg.tenant_id, r_user)
    assert (req.current_approver_kind, req.current_approver_user_id) == ("user", named.user_id)
    assert req.chain_snapshot[0]["reassigned"]["reason"] == "escalation"
    req = await _request(dorg.tenant_id, r_lead)
    assert req.current_approver_user_id == lead.user_id and req.status == "pending"
    assert (
        await scalar(
            dorg.tenant_id, "SELECT count(*) FROM audit_logs WHERE action = 'approval.step_reassigned'"
        )
        == 3
    )
    assert (
        await scalar(
            dorg.tenant_id,
            "SELECT count(*) FROM approval_decisions WHERE request_id IN (:a, :b, :c)",
            a=r_role,
            b=r_user,
            c=r_lead,
        )
        == 0
    )

    # the new approver can now decide (a human); the escalation itself never did
    ctx = await admin_ctx(dorg.tenant_id, named.user_id)
    async with tenant_session(dorg.tenant_id) as db:
        out = await approvals.decide(ctx, db, r_user, DecideIn(action="approve"))
    assert out.status == "approved"
    await named.client.aclose()


async def test_no_team_lead_falls_back_to_admins(
    dorg: DOrg, subject: Any, people: tuple[Member, Member]
) -> None:
    approver, _ = people  # in no team
    request_id, _ = await _start(dorg, approver.user_id, _spec(1, "reassign", "manager_of_assignee"))
    assert await _evaluate(dorg.tenant_id, datetime.now(UTC) + timedelta(hours=2)) == 1
    watch = await _watch(dorg.tenant_id, request_id)
    assert watch.last_outcome == "target_unresolved_fallback"
    assert await _notified(dorg.tenant_id, dorg.admin.user_id) == 1
    assert (await _request(dorg.tenant_id, request_id)).current_approver_user_id == approver.user_id


async def test_watch_closes_on_decision(dorg: DOrg, subject: Any) -> None:
    request_id, _ = await _start(dorg, dorg.admin.user_id, _spec(1))
    ctx = await admin_ctx(dorg.tenant_id, dorg.admin.user_id)
    async with tenant_session(dorg.tenant_id) as db:
        await approvals.decide(ctx, db, request_id, DecideIn(action="approve"))
    watch = await _watch(dorg.tenant_id, request_id)
    assert watch.status == "done" and watch.last_outcome == "request_approved"
    assert await _evaluate(dorg.tenant_id, datetime.now(UTC) + timedelta(hours=5)) == 0


async def test_escalated_five_times_stays_pending(
    dorg: DOrg, subject: list[Any], people: tuple[Member, Member]
) -> None:
    approver, lead = people
    await _team(dorg, {approver.user_id: False, lead.user_id: True})
    request_id, _ = await _start(
        dorg, approver.user_id, _spec(1, repeat_every_hours=Decimal(1), max_repeats=4)
    )
    watch = await _watch(dorg.tenant_id, request_id)
    for i in range(1, 8):
        await _evaluate(dorg.tenant_id, watch.started_at + timedelta(hours=i, minutes=1))
    watch = await _watch(dorg.tenant_id, request_id)
    assert watch.fires == 5 and watch.status == "done"
    assert (await _request(dorg.tenant_id, request_id)).status == "pending"
    assert subject == []  # no final decision callback ever ran
    assert (
        await scalar(
            dorg.tenant_id, "SELECT count(*) FROM approval_decisions WHERE request_id = :r", r=request_id
        )
        == 0
    )


async def test_policy_api_chain_step_attachment_and_archive(dorg: DOrg, other_dorg: DOrg) -> None:
    a = dorg.admin
    bad = await a.client.post(
        f"{SLA}/escalation-policies",
        json={"name": "x", "after_value": "4", "target_kind": "named_user"},
        headers=h(a),
    )
    assert bad.status_code == 422
    too_many = await a.client.post(
        f"{SLA}/escalation-policies",
        json={
            "name": "x",
            "after_value": "4",
            "target_kind": "assignee_manager",
            "repeat_every_value": "1",
            "max_repeats": 9,
        },
        headers=h(a),
    )
    assert too_many.status_code == 422
    resp = await a.client.post(
        f"{SLA}/escalation-policies",
        json={
            "name": "Approvals: 1 business day",
            "after_value": "1",
            "after_unit": "business_days",
            "target_kind": "assignee_manager",
            "action": "notify",
        },
        headers=h(a),
    )
    assert resp.status_code == 201, resp.text
    policy = resp.json()
    assert policy["after_business_hours"] == "8.00" and policy["after_value"] == "1.00"
    assert policy["summary"] == "Notify the assignee's team lead after 1 business day"
    upd = await a.client.patch(
        f"{SLA}/escalation-policies/{policy['id']}", json={"after_value": "2"}, headers=h(a)
    )
    assert upd.status_code == 200 and upd.json()["after_business_hours"] == "16.00"

    chain = await a.client.post(
        f"{API}/approval-chains",
        json={
            "subject_type": "demand",
            "name": "Demand approval",
            "steps": [{"name": "Admin", "approver_user_id": str(a.user_id)}],
        },
        headers=h(a),
    )
    assert chain.status_code == 201, chain.text
    chain_id = chain.json()["id"]
    put = await a.client.put(
        f"{SLA}/approval-chains/{chain_id}/escalations",
        json={"steps": [{"position": 1, "policy_id": policy["id"]}]},
        headers=h(a),
    )
    assert put.status_code == 200, put.text
    [step] = put.json()["steps"]
    assert step["policy_id"] == policy["id"] and step["position"] == 1

    # a chain request opens a watch from the step's owned copy
    ctx = await admin_ctx(dorg.tenant_id, a.user_id)
    async with tenant_session(dorg.tenant_id) as db:
        result = await approvals.start(ctx, db, "demand", uuid.uuid4(), {"priority": "high"})
    assert result.request_id is not None
    watch = await _watch(dorg.tenant_id, result.request_id)
    assert watch.policy_id == uuid.UUID(step["copy_id"]) and watch.spec["after_unit"] == "business_days"

    # cross-tenant: 404 on every id route
    o = other_dorg.admin
    assert (await o.client.get(f"{SLA}/escalation-policies/{policy['id']}")).status_code == 404
    assert (
        await o.client.patch(f"{SLA}/escalation-policies/{policy['id']}", json={"name": "y"}, headers=h(o))
    ).status_code == 404
    assert (
        await o.client.post(f"{SLA}/escalation-policies/{policy['id']}/archive", headers=h(o))
    ).status_code == 404
    assert (await o.client.get(f"{SLA}/approval-chains/{chain_id}/escalations")).status_code == 404
    assert (
        await o.client.get(
            f"{SLA}/escalations",
            params={"subject_kind": "approval_step", "subject_id": str(result.request_id)},
        )
    ).status_code == 404
    hist = await a.client.get(
        f"{SLA}/escalations", params={"subject_kind": "approval_step", "subject_id": str(result.request_id)}
    )
    assert hist.status_code == 200 and hist.json()[0]["status"] == "active"

    # archiving the library policy cancels watches started from it or its copies
    arch = await a.client.post(f"{SLA}/escalation-policies/{policy['id']}/archive", headers=h(a))
    assert arch.status_code == 200 and arch.json()["archived_at"] is not None
    watch = await _watch(dorg.tenant_id, result.request_id)
    assert watch.status == "cancelled" and watch.last_outcome == "policy_archived"
    listed = await a.client.get(f"{SLA}/escalation-policies")
    assert listed.json() == []


async def test_task_watch_reassigns_and_closes_on_completion(
    dorg: DOrg, people: tuple[Member, Member]
) -> None:
    approver, lead = people
    await _team(dorg, {approver.user_id: False, lead.user_id: True})
    a = dorg.admin
    policy = (
        await a.client.post(
            f"{SLA}/escalation-policies",
            json={
                "name": "Tasks",
                "after_value": "2",
                "after_unit": "hours",
                "target_kind": "assignee_manager",
                "action": "reassign",
            },
            headers=h(a),
        )
    ).json()
    task = await a.client.post(
        f"{API}/tasks", json={"title": "Call the client", "owner_id": str(approver.user_id)}, headers=h(a)
    )
    assert task.status_code == 201, task.text
    task_id = uuid.UUID(task.json()["id"])
    async with tenant_session(dorg.tenant_id) as db:
        watch_id = await escalations.open_task_watch(db, task_id, uuid.UUID(policy["id"]))
    assert watch_id is not None
    assert await _evaluate(dorg.tenant_id, datetime.now(UTC) + timedelta(hours=3)) == 1
    owner = await scalar(dorg.tenant_id, "SELECT owner_id FROM tasks WHERE id = :t", t=task_id)
    assert owner == lead.user_id

    task2 = (
        await a.client.post(
            f"{API}/tasks", json={"title": "Follow up", "owner_id": str(approver.user_id)}, headers=h(a)
        )
    ).json()
    async with tenant_session(dorg.tenant_id) as db:
        await escalations.open_task_watch(db, uuid.UUID(task2["id"]), uuid.UUID(policy["id"]))
    done = await a.client.post(f"{API}/tasks/{task2['id']}/complete", headers=h(a))
    assert done.status_code == 200, done.text
    status = await scalar(
        dorg.tenant_id,
        "SELECT status FROM escalation_watches WHERE subject_id = :t",
        t=uuid.UUID(task2["id"]),
    )
    assert status == "done"


def test_escalations_never_reference_decide() -> None:
    """FR-170 invariant (AST): no code path in ``services/sla`` or ``services/workflow`` calls or imports
    ``decide`` (or a starred human-only service function)."""
    import app.services.sla as sla_pkg
    import app.services.workflow as wf_pkg

    forbidden = {
        "decide", "issue_invoice", "record_payment", "adjudicate", "finalize_export", "close_period", "write_off",
        "accept_vendor_submission",
    }  # fmt: skip
    for package in (sla_pkg, wf_pkg):
        for path in sorted(Path(package.__file__).resolve().parent.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            assert "decide(" not in source, path.name
            for node in ast.walk(ast.parse(source)):
                names: set[str] = set()
                if isinstance(node, ast.ImportFrom):
                    names = {a.name for a in node.names}
                elif isinstance(node, ast.Attribute):
                    names = {node.attr}
                elif isinstance(node, ast.Name):
                    names = {node.id}
                assert not (names & forbidden), f"{path.name} references {names & forbidden}"
