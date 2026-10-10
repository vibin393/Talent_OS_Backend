"""FR-44 with a real Epic 4 chain: conditional skip on margin, approve, request changes, no chain."""

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI

from tests.modules.approvals.test_approvals_api import make_chain
from tests.modules.demands.helpers import APPROVALS, DOrg, h, role_id
from tests.modules.identity.conftest import Member, join
from tests.modules.offers.helpers import create_offer, get_offer, patch, setup_selected, submit


@pytest.fixture
async def rm(app: FastAPI, dorg: DOrg) -> AsyncIterator[Member]:
    member = await join(app, dorg.tenant, "recruitment_manager", name="Rhea Manager")
    yield member
    await member.client.aclose()


async def _chain(org: DOrg) -> None:
    await make_chain(
        org,
        [
            {
                "name": "Low margin",
                "approver_user_id": str(org.admin.user_id),
                "condition": {"all": [{"field": "margin_percent", "op": "lt", "value": 15}]},
            },
            {
                "name": "Recruitment manager",
                "approver_role_id": str(await role_id(org, "recruitment_manager")),
            },
        ],
        subject_type="offer",
    )


async def test_margin_skip_then_approve(dorg: DOrg, rm: Member) -> None:
    await _chain(dorg)
    s = await setup_selected(dorg)
    offer = await create_offer(
        dorg.admin, s.application_id, bill_rate={"amount": "1700.00", "currency": "INR"}
    )
    assert float(offer["current_version"]["margin_pct"]) >= 20
    pending = await submit(dorg.admin, offer["id"])
    assert pending["status"] == "pending_approval"
    assert pending["current_version"]["frozen"] is True
    req = pending["approvals"][0]
    assert [(d["position"], d["outcome"]) for d in req["decisions"]] == [(1, "skipped")]
    mine = (await rm.client.get(f"{APPROVALS}/mine")).json()["items"]
    assert [i["subject_id"] for i in mine] == [offer["id"]]
    assert mine[0]["subject"]["label"] == offer["human_id"]
    resp = await rm.client.post(f"{APPROVALS}/{req['id']}/decide", json={"action": "approve"}, headers=h(rm))
    assert resp.status_code == 200, resp.text
    after = await get_offer(dorg.admin, offer["id"])
    assert after["status"] == "approved"
    assert after["current_version"]["approval_state"] == "approved"
    assert after["current_version"]["approved_at"] is not None


async def test_request_changes_returns_to_draft_with_comment(dorg: DOrg, rm: Member) -> None:
    await _chain(dorg)
    s = await setup_selected(dorg)
    offer = await create_offer(dorg.admin, s.application_id)  # 13.47 % -> step 1 applies
    pending = await submit(dorg.admin, offer["id"])
    req = pending["approvals"][0]
    assert req["current_step_name"] == "Low margin"
    resp = await dorg.admin.client.post(
        f"{APPROVALS}/{req['id']}/decide",
        json={"action": "request_changes", "comment": "Raise the bill rate"},
        headers=h(dorg.admin),
    )
    assert resp.status_code == 200, resp.text
    back = await get_offer(dorg.admin, offer["id"])
    assert back["status"] == "draft"
    assert back["status_comment"] == "Raise the bill rate"
    assert back["current_version"]["frozen"] is False
    assert back["current_version"]["approval_state"] == "returned"
    edited = await patch(dorg.admin, back, bill_rate={"amount": "1800.00", "currency": "INR"})
    assert edited.status_code == 200 and edited.json()["current_version"]["version_no"] == 1


async def test_no_chain_auto_approved(dorg: DOrg) -> None:
    s = await setup_selected(dorg)
    offer = await create_offer(dorg.admin, s.application_id)
    approved = await submit(dorg.admin, offer["id"])
    assert approved["status"] == "approved"
    assert [x["to_status"] for x in approved["history"]] == ["draft", "approved"]
