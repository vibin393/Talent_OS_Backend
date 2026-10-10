# ruff: noqa: E501
"""Epic 41 story 41.2 (FR-210, PRD 5.4): internal applications with manager approval -- source ``internal`` + overlay,
disclosure, manager resolution (Worker AM, User team lead, fallback role, unresolved), confidentiality with a
recruiter screen first, the pipeline guard, rejection reason, withdraw, Employee views and Worker isolation."""

import uuid
from typing import Any

from fastapi import FastAPI

from tests.modules.demands.helpers import DOrg, h
from tests.modules.mobility.helpers import (
    WORK,
    M,
    active_worker,
    apply_user,
    apply_worker,
    decide,
    demand,
    employee,
    overlay,
    rows,
    scalar,
    stage_key,
    team_with_lead,
    worker,
)
from tests.modules.pipeline.helpers import move, stages


async def _request(org: DOrg, request_id: Any) -> Any:
    return (
        await rows(
            org,
            "SELECT status, current_approver_user_id, current_approver_role_id, current_position FROM approval_requests WHERE id = :i",
            i=request_id,
        )
    )[0]


async def _notified(org: DOrg, user_id: Any) -> int:
    return int(await scalar(org, "SELECT count(*) FROM notifications WHERE recipient_kind = 'user' AND recipient_id = :u", u=user_id))


async def test_worker_application_goes_to_account_manager_and_guard_holds_it(app: FastAPI, dorg: DOrg) -> None:
    am = await employee(app, dorg, "Amit Account Manager", "account_manager")
    d = await demand(dorg, "both")
    _, cid = await active_worker(dorg, account_manager=am.user_id)
    w = await worker(app, dorg, cid)

    no_ack = await apply_worker(w, d["id"], {"disclosure_ack": False})
    assert no_ack.status_code == 422 and no_ack.json()["error"]["code"] == "disclosure_required", no_ack.text

    r = await apply_worker(w, d["id"])
    assert r.status_code == 201, r.text
    mine = r.json()
    assert mine["status"] == "manager_approval" and mine["stage_name"] is None and mine["can_withdraw"] is True
    status, req_id, manager, _, app_id, cand = await overlay(dorg, mine["id"])
    assert (status, manager, cand) == ("manager_approval", am.user_id, cid)
    assert await scalar(dorg, "SELECT source FROM applications WHERE id = :a", a=app_id) == "internal"
    assert await scalar(dorg, "SELECT source_ref FROM applications WHERE id = :a", a=app_id) == mine["id"]
    req = await _request(dorg, req_id)
    assert req[0] == "pending" and req[1] == am.user_id

    again = await apply_worker(w, d["id"])
    assert again.status_code == 409 and again.json()["error"]["code"] == "application_exists"

    # Pipeline guard: the recruiter cannot move it before approval.
    ids = await stages(dorg.admin)
    held = await move(dorg.admin, str(app_id), ids["shortlisted"])
    assert held.status_code == 409 and held.json()["error"]["code"] == "manager_approval_pending", held.text

    # The recruiter view shows the overlay; another user cannot decide.
    view = await dorg.admin.client.get(f"{M}/applications?demand_id={d['id']}")
    assert view.status_code == 200 and view.json()[0]["status"] == "manager_approval"
    assert view.json()[0]["manager_name"] == "Amit Account Manager"
    by_app = await dorg.admin.client.get(f"{M}/applications/by-application/{app_id}")
    assert by_app.status_code == 200 and by_app.json()["id"] == mine["id"]
    assert (await decide(dorg.admin, req_id, "approve")).status_code == 403

    ok = await decide(am, req_id, "approve")
    assert ok.status_code == 200, ok.text
    assert (await overlay(dorg, mine["id"]))[0] == "approved"
    moved = await move(dorg.admin, str(app_id), ids["shortlisted"])
    assert moved.status_code == 200, moved.text
    (listed,) = (await w.get(f"{WORK}/applications")).json()
    assert listed["status"] == "approved" and listed["stage_name"] == "Shortlisted"


async def test_user_application_team_lead_rejects_with_reason(app: FastAPI, dorg: DOrg) -> None:
    d = await demand(dorg, "internal", title="Internal Analyst")
    emp = await employee(app, dorg, "Usha User")
    lead = await employee(app, dorg, "Lata Lead")
    await team_with_lead(dorg, lead.user_id, emp.user_id)

    preview = await emp.client.get(f"{M}/me/profile")
    assert preview.status_code == 200 and preview.json()["will_create"] is True and preview.json()["email"] == emp.email

    r = await apply_user(emp, d["id"])
    assert r.status_code == 201, r.text
    mid = r.json()["id"]
    _, req_id, manager, _, app_id, cand = await overlay(dorg, mid)
    assert manager == lead.user_id
    assert await scalar(dorg, "SELECT source FROM candidates WHERE id = :c", c=cand) == "internal"
    assert await scalar(dorg, "SELECT candidate_id FROM mobility_employee_links WHERE user_id = :u", u=emp.user_id) == cand
    assert (await emp.client.get(f"{M}/me/profile")).json()["candidate_id"] == str(cand)

    assert (await decide(lead, req_id, "reject")).status_code == 422  # comment required (engine)
    short = await decide(lead, req_id, "reject", "No")
    assert short.status_code == 422 and short.json()["error"]["code"] == "reason_required", short.text
    assert (await overlay(dorg, mid))[0] == "manager_approval"
    rej = await decide(lead, req_id, "reject", "Needed on the current project until March")
    assert rej.status_code == 200, rej.text
    status, *_ = await overlay(dorg, mid)
    assert status == "manager_rejected"
    assert await stage_key(dorg, app_id) == "rejected"
    assert await scalar(
        dorg,
        "SELECT reason_code FROM application_stage_history WHERE application_id = :a ORDER BY at DESC LIMIT 1",
        a=app_id,
    ) == "internal_manager_rejected"
    (mine,) = (await emp.client.get(f"{M}/my-applications")).json()
    assert mine["status"] == "manager_rejected" and mine["rejection_reason"] == "Needed on the current project until March"
    assert mine["can_withdraw"] is False


async def test_fallback_role_and_unresolved_manager(app: FastAPI, dorg: DOrg) -> None:
    d = await demand(dorg, "internal")
    rm = await employee(app, dorg, "Rita Recruitment Manager", "recruitment_manager")
    emp = await employee(app, dorg, "Nina No Team")
    r = await apply_user(emp, d["id"])
    assert r.status_code == 201, r.text
    _, req_id, manager, *_ = await overlay(dorg, r.json()["id"])
    assert manager is None
    req = await _request(dorg, req_id)
    assert req[1] is None and req[2] is not None  # role step
    assert (await decide(rm, req_id, "approve")).status_code == 200

    s = await dorg.admin.client.patch(f"{M}/settings", json={"fallback_approver_role_key": "compliance"}, headers=h(dorg.admin))
    assert s.status_code == 200, s.text
    other = await employee(app, dorg, "Omar Orphan")
    d2 = await demand(dorg, "internal")
    bad = await apply_user(other, d2["id"])
    assert bad.status_code == 409 and bad.json()["error"]["code"] == "manager_unresolved", bad.text
    assert await scalar(dorg, "SELECT count(*) FROM applications WHERE demand_id = :d", d=uuid.UUID(d2["id"])) == 0


async def test_screen_first_manager_not_notified_until_step_activates(app: FastAPI, dorg: DOrg) -> None:
    am = await employee(app, dorg, "Arjun AM", "account_manager")
    await dorg.admin.client.patch(f"{M}/settings", json={"screen_before_manager": True}, headers=h(dorg.admin))
    d = await demand(dorg, "internal")
    _, cid = await active_worker(dorg, account_manager=am.user_id)
    w = await worker(app, dorg, cid)
    before = await _notified(dorg, am.user_id)
    r = await apply_worker(w, d["id"])
    assert r.status_code == 201, r.text
    status, req_id, manager, *_ = await overlay(dorg, r.json()["id"])
    assert status == "applied" and manager == am.user_id
    req = await _request(dorg, req_id)
    assert req[1] == dorg.admin.user_id  # the demand owner screens first
    assert await _notified(dorg, am.user_id) == before
    assert (await decide(am, req_id, "approve")).status_code == 403

    assert (await decide(dorg.admin, req_id, "approve")).status_code == 200
    assert (await overlay(dorg, r.json()["id"]))[0] == "manager_approval"
    assert (await _request(dorg, req_id))[1] == am.user_id
    assert await _notified(dorg, am.user_id) > before


async def test_withdraw_cancels_approval_and_isolation(app: FastAPI, dorg: DOrg) -> None:
    d = await demand(dorg, "internal")
    _, cid_a = await active_worker(dorg)
    _, cid_b = await active_worker(dorg)
    wa = await worker(app, dorg, cid_a)
    wb = await worker(app, dorg, cid_b)
    r = await apply_worker(wa, d["id"])
    assert r.status_code == 201, r.text
    mid = r.json()["id"]

    assert (await wb.get(f"{WORK}/applications")).json() == []
    stolen = await wb.post(f"{WORK}/applications/{mid}/withdraw", headers={"X-CSRF-Token": "csrf-token"})
    assert stolen.status_code == 404
    emp = await employee(app, dorg)
    assert (await emp.client.post(f"{M}/applications/{mid}/withdraw", headers=emp.h())).status_code == 404

    done = await wa.post(f"{WORK}/applications/{mid}/withdraw", headers={"X-CSRF-Token": "csrf-token"})
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "withdrawn" and done.json()["can_withdraw"] is False
    _, req_id, _, _, app_id, _ = await overlay(dorg, mid)
    assert (await _request(dorg, req_id))[0] == "cancelled"
    assert await stage_key(dorg, app_id) == "withdrawn"
    again = await wa.post(f"{WORK}/applications/{mid}/withdraw", headers={"X-CSRF-Token": "csrf-token"})
    assert again.status_code == 409
    board = (await wa.get(WORK)).json()["items"]
    assert board[0]["application_status"] == "withdrawn"
