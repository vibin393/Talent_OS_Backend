"""Story 4.2: approval chains, conditional skips, decisions, resubmission, My approvals, subject reuse."""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI

from app.core.database import tenant_session
from app.services import approvals
from tests.modules.demands.helpers import (
    APPROVALS,
    CHAINS,
    DEMANDS,
    DOrg,
    admin_ctx,
    create_demand,
    h,
    role_id,
)
from tests.modules.identity.conftest import Member, join


@pytest.fixture
async def rm(app: FastAPI, dorg: DOrg) -> AsyncIterator[Member]:
    member = await join(app, dorg.tenant, "recruitment_manager", name="Rhea Manager")
    yield member
    await member.client.aclose()


async def make_chain(org: DOrg, steps: list[dict[str, Any]], subject_type: str = "demand") -> dict[str, Any]:
    resp = await org.admin.client.post(
        CHAINS,
        json={"subject_type": subject_type, "name": f"{subject_type} chain", "steps": steps},
        headers=h(org.admin),
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def conditional_chain(org: DOrg, first_approver: uuid.UUID) -> dict[str, Any]:
    return await make_chain(
        org,
        [
            {
                "name": "High rate",
                "approver_user_id": str(first_approver),
                "condition": {"all": [{"field": "bill_rate", "op": "gte", "value": 2000}]},
            },
            {
                "name": "Recruitment manager",
                "approver_role_id": str(await role_id(org, "recruitment_manager")),
            },
        ],
    )


async def submit(m: Member, demand_id: str) -> dict[str, Any]:
    resp = await m.client.post(f"{DEMANDS}/{demand_id}/submit", headers=h(m))
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


async def mine(m: Member) -> list[dict[str, Any]]:
    resp = await m.client.get(f"{APPROVALS}/mine")
    assert resp.status_code == 200, resp.text
    return list(resp.json()["items"])


async def test_conditional_skip_then_role_approval_opens(dorg: DOrg, rm: Member) -> None:
    await conditional_chain(dorg, dorg.admin.user_id)
    d = await create_demand(dorg.admin, dorg.client_id, bill_rate="1500.00")
    submitted = await submit(dorg.admin, d["id"])
    assert submitted["status"] == "pending_approval"

    assert await mine(dorg.admin) == []
    items = await mine(rm)
    assert len(items) == 1
    req = items[0]
    assert req["subject"] == {"label": d["req_id"], "title": d["title"]}
    assert req["current_step_name"] == "Recruitment manager"
    assert [(x["position"], x["outcome"]) for x in req["decisions"]] == [(1, "skipped")]
    assert req["decisions"][0]["evaluated_condition"]["applies"] is False
    assert req["facts"]["bill_rate"] == "1500.00"

    # Not the current approver.
    resp = await dorg.admin.client.post(
        f"{APPROVALS}/{req['id']}/decide", json={"action": "approve"}, headers=h(dorg.admin)
    )
    assert (resp.status_code, resp.json()["error"]["code"]) == (403, "not_current_approver")

    resp = await rm.client.post(f"{APPROVALS}/{req['id']}/decide", json={"action": "approve"}, headers=h(rm))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"
    demand = (await dorg.admin.client.get(f"{DEMANDS}/{d['id']}")).json()
    assert demand["status"] == "open"
    assert demand["opened_at"] is not None
    assert await mine(rm) == []
    again = await rm.client.post(f"{APPROVALS}/{req['id']}/decide", json={"action": "approve"}, headers=h(rm))
    assert (again.status_code, again.json()["error"]["code"]) == (409, "request_not_pending")


async def test_missing_fact_does_not_skip(dorg: DOrg, rm: Member) -> None:
    await conditional_chain(dorg, dorg.admin.user_id)
    d = await create_demand(dorg.admin, dorg.client_id, bill_rate=None, pay_rate_min=None, pay_rate_max=None)
    await submit(dorg.admin, d["id"])
    items = await mine(dorg.admin)
    assert len(items) == 1
    assert items[0]["current_step_name"] == "High rate"
    assert items[0]["decisions"] == []
    # Approve step 1 -> step 2 (role) becomes current; admin no longer sees it, rm does.
    resp = await dorg.admin.client.post(
        f"{APPROVALS}/{items[0]['id']}/decide",
        json={"action": "approve", "comment": "ok"},
        headers=h(dorg.admin),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "pending"
    assert await mine(dorg.admin) == []
    assert len(await mine(rm)) == 1


@pytest.mark.parametrize("action", ["reject", "request_changes"])
async def test_return_needs_comment_and_resubmit_restarts(dorg: DOrg, rm: Member, action: str) -> None:
    await make_chain(
        dorg, [{"name": "RM", "approver_role_id": str(await role_id(dorg, "recruitment_manager"))}]
    )
    d = await create_demand(dorg.admin, dorg.client_id)
    await submit(dorg.admin, d["id"])
    req_id = (await mine(rm))[0]["id"]
    resp = await rm.client.post(f"{APPROVALS}/{req_id}/decide", json={"action": action}, headers=h(rm))
    assert (resp.status_code, resp.json()["error"]["code"]) == (422, "comment_required")
    resp = await rm.client.post(
        f"{APPROVALS}/{req_id}/decide", json={"action": action, "comment": "Rate too low"}, headers=h(rm)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == ("rejected" if action == "reject" else "changes_requested")
    demand = (await dorg.admin.client.get(f"{DEMANDS}/{d['id']}")).json()
    assert demand["status"] == "draft"
    assert demand["status_comment"] == "Rate too low"

    await submit(dorg.admin, d["id"])
    history = (await dorg.admin.client.get(f"{DEMANDS}/{d['id']}/approvals")).json()
    assert [r["status"] for r in history] == ["pending", resp.json()["status"]]
    assert history[0]["current_position"] == 1
    got = await dorg.admin.client.get(f"{APPROVALS}/{history[0]['id']}")
    assert got.status_code == 200


async def test_chain_validation(dorg: DOrg) -> None:
    m = dorg.admin
    rid = str(await role_id(dorg, "recruitment_manager"))
    bad_steps: list[list[dict[str, Any]]] = [
        [
            {
                "name": "x",
                "approver_role_id": rid,
                "condition": {"all": [{"field": "salary", "op": "eq", "value": 1}]},
            }
        ],
        [
            {
                "name": "x",
                "approver_role_id": rid,
                "condition": {"all": [{"field": "bill_rate", "op": "like", "value": 1}]},
            }
        ],
        [{"name": "x", "approver_role_id": rid, "approver_user_id": str(m.user_id)}],
        [{"name": "x"}],
        [{"name": "x", "approver_user_id": str(uuid.uuid4())}],
    ]
    for steps in bad_steps:
        resp = await m.client.post(
            CHAINS, json={"subject_type": "demand", "name": "c", "steps": steps}, headers=h(m)
        )
        assert resp.status_code == 422, (steps, resp.text)
    chain = await make_chain(dorg, [{"name": "x", "approver_role_id": rid}])
    dup = await m.client.post(
        CHAINS,
        json={"subject_type": "demand", "name": "c2", "steps": [{"name": "y", "approver_role_id": rid}]},
        headers=h(m),
    )
    assert (dup.status_code, dup.json()["error"]["code"]) == (409, "active_chain_exists")
    inactive = await m.client.post(
        CHAINS,
        json={
            "subject_type": "demand",
            "name": "c3",
            "is_active": False,
            "steps": [{"name": "y", "approver_role_id": rid}],
        },
        headers=h(m),
    )
    assert inactive.status_code == 201
    upd = await m.client.put(
        f"{CHAINS}/{chain['id']}",
        json={
            "subject_type": "demand",
            "name": "renamed",
            "steps": [
                {"name": "a", "approver_user_id": str(m.user_id)},
                {"name": "b", "approver_role_id": rid},
            ],
        },
        headers=h(m),
    )
    assert upd.status_code == 200, upd.text
    assert [s["position"] for s in upd.json()["steps"]] == [1, 2]
    assert len((await m.client.get(CHAINS)).json()) == 2
    assert (await m.client.delete(f"{CHAINS}/{chain['id']}", headers=h(m))).status_code == 204
    assert (await m.client.get(f"{CHAINS}/{chain['id']}")).status_code == 404


async def test_settings_permission_required(dorg: DOrg, rm: Member) -> None:
    assert (await rm.client.get(CHAINS)).status_code == 403


async def test_offer_subject_reuse(dorg: DOrg, rm: Member) -> None:
    calls: list[tuple[str, uuid.UUID, str | None]] = []

    async def on_approved(ctx: Any, db: Any, subject_id: uuid.UUID) -> None:
        calls.append(("approved", subject_id, None))

    async def on_returned(ctx: Any, db: Any, subject_id: uuid.UUID, outcome: str, comment: str) -> None:
        calls.append((outcome, subject_id, comment))

    from app.services.approvals import service as approvals_service

    # Restore the real offer subject afterwards (it is registered once, when the offers service is imported).
    original = approvals_service._subjects.get("offer")
    approvals.register_subject("offer", on_approved, on_returned)
    try:
        await make_chain(
            dorg,
            [
                {
                    "name": "Low margin",
                    "approver_role_id": str(await role_id(dorg, "recruitment_manager")),
                    "condition": {"all": [{"field": "margin_percent", "op": "lt", "value": 20}]},
                }
            ],
            subject_type="offer",
        )
        offer_id, healthy_offer = uuid.uuid4(), uuid.uuid4()
        async with tenant_session(dorg.tenant_id) as db:
            started = await approvals.start(
                admin_ctx(dorg), db, "offer", offer_id, {"margin_percent": "12.5"}
            )
            skipped = await approvals.start(
                admin_ctx(dorg), db, "offer", healthy_offer, {"margin_percent": 30}
            )
        assert started.status == "pending"
        assert skipped.status == "auto_approved" and skipped.request_id is not None
        items = await mine(rm)
        assert [i["subject_type"] for i in items] == ["offer"]
        resp = await rm.client.post(
            f"{APPROVALS}/{items[0]['id']}/decide", json={"action": "approve"}, headers=h(rm)
        )
        assert resp.status_code == 200, resp.text
        assert calls == [("approved", offer_id, None)]
    finally:
        approvals.unregister_subject("offer")
        if original is not None:
            approvals_service._subjects["offer"] = original


async def test_audit_contains_approval_changes(dorg: DOrg, rm: Member) -> None:
    from sqlalchemy import select

    from app.core.audit import AuditLog

    await make_chain(
        dorg, [{"name": "RM", "approver_role_id": str(await role_id(dorg, "recruitment_manager"))}]
    )
    d = await create_demand(dorg.admin, dorg.client_id)
    await submit(dorg.admin, d["id"])
    req_id = (await mine(rm))[0]["id"]
    await rm.client.post(f"{APPROVALS}/{req_id}/decide", json={"action": "approve"}, headers=h(rm))
    async with tenant_session(dorg.tenant_id) as db:
        actions = set((await db.execute(select(AuditLog.action))).scalars())
    assert {"approval_request.created", "approval_decision.created", "demand.updated"} <= actions
