# ruff: noqa: E501
"""P2-10.1..10.4 end to end: case creation, Worker portal, review, eSign / policy / internal tasks, Start Gate,
reuse for a returning Worker, compliance merge, isolation and erasure."""

import logging
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import select, text

from app.core.database import tenant_session
from app.models.onboarding import OnboardingAcknowledgement
from app.services.onboarding_erasure import OnboardingHandler
from app.services.privacy.registry import ErasureContext
from tests.epic3 import make_pdf
from tests.modules.demands.helpers import DOrg, h
from tests.modules.onboarding.helpers import (
    ACCOUNT,
    BANK,
    CASES,
    CSRF,
    PAN,
    PORTAL,
    TASKS,
    case_of,
    create_template,
    doc_templates,
    event,
    full_tasks,
    placement,
    set_owner,
    tasks_of,
    worker,
)


async def _gate(org: DOrg, placement_id: Any) -> str | None:
    async with tenant_session(org.tenant_id) as db:
        return (
            await db.execute(
                text("SELECT status FROM start_gates WHERE placement_id = :p AND kind = 'onboarding'"),
                {"p": uuid.UUID(str(placement_id))},
            )
        ).scalar_one_or_none()


async def _submit(c: Any, task_id: Any, payload: dict[str, Any]) -> Any:
    return await c.post(f"{PORTAL}/tasks/{task_id}/submit", json={"payload": payload}, headers=CSRF)


async def test_full_onboarding_flow(app: FastAPI, dorg: DOrg, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    m = dorg.admin
    await set_owner(dorg)
    nda, policy = await doc_templates(dorg)
    await create_template(dorg, full_tasks(nda, policy), client_id=dorg.client_id)
    p = await placement(dorg)
    case = await case_of(dorg, p["id"])
    assert case is not None and case.human_id and case.human_id.startswith("ONB-")
    assert case.invited_at is not None and case.status == "not_started"
    assert await _gate(dorg, p["id"]) == "pending"
    # PlacementCreated again -> the same case (idempotent)
    await event(dorg, "PlacementCreated", {"placement_id": p["id"]})
    assert (await case_of(dorg, p["id"])).id == case.id  # type: ignore[union-attr]
    tasks = await tasks_of(dorg, case.id)
    assert set(tasks) == {"personal", "bank", "tax", "emergency", "nda", "it_policy", "laptop"}
    assert tasks["nda"].signature_request_id is not None  # created on invite
    assert tasks["laptop"].p1_task_id is not None and tasks["laptop"].owner_user_id == m.user_id

    c = await worker(app, dorg, p["candidate_id"])
    try:
        listed = (await c.get(f"{PORTAL}/cases")).json()
        assert [x["id"] for x in listed] == [str(case.id)]
        detail = (await c.get(f"{PORTAL}/cases/{case.id}")).json()
        kinds = {t["kind"] for t in detail["tasks"]}
        assert "internal" not in kinds and len(detail["tasks"]) == 6
        assert "laptop" not in str(detail).lower()
        assert len((await c.get(f"{PORTAL}/cases/{case.id}")).content) < 50_000
        # internal task is a 404 for the Worker
        assert (await _submit(c, tasks["laptop"].id, {})).status_code == 404
        # drafts (partial progress) never keep account numbers
        draft = await c.patch(
            f"{PORTAL}/tasks/{tasks['bank'].id}/draft",
            json={"draft": {"holder_name": "Asha", "account_number": ACCOUNT}},
            headers=CSRF,
        )
        assert draft.status_code == 204, draft.text
        assert (await tasks_of(dorg, case.id))["bank"].draft == {"holder_name": "Asha"}
        # bank: double-entry mismatch, then OK
        bad = await _submit(c, tasks["bank"].id, {**BANK, "account_number_confirm": "1"})
        assert (bad.status_code, bad.json()["error"]["code"]) == (422, "account_mismatch")
        bad_ifsc = await _submit(c, tasks["bank"].id, {**BANK, "ifsc": "HDFC1234567"})
        assert bad_ifsc.json()["error"]["code"] == "invalid_ifsc"
        ok = await _submit(c, tasks["bank"].id, BANK)
        assert ok.status_code == 200, ok.text
        assert ok.json()["status"] == "submitted" and ok.json()["masked"]["bank_last4"] == "9012"
        # personal, emergency, tax
        assert (
            await _submit(
                c,
                tasks["personal"].id,
                {"date_of_birth": "1995-04-02", "current_address": "12 MG Road", "city": "Pune"},
            )
        ).status_code == 200
        assert (
            await _submit(
                c,
                tasks["emergency"].id,
                {"name": "Ravi Rao", "relation": "Brother", "phone": "+91 98765 43210"},
            )
        ).status_code == 200
        full = await _submit(
            c, tasks["tax"].id, {"pan": PAN, "regime": "new", "note": "aadhaar 1234 5678 9012"}
        )
        assert (full.status_code, full.json()["error"]["code"]) == (422, "full_aadhaar_not_allowed")
        assert (
            await _submit(
                c, tasks["tax"].id, {"pan": PAN, "regime": "new", "declarations": {"section_80c": "150000"}}
            )
        ).status_code == 200
        # policy: view + acknowledge -> immutable row, auto-approved
        pdf = await c.get(f"{PORTAL}/tasks/{tasks['it_policy'].id}/policy")
        assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
        ack = await c.post(
            f"{PORTAL}/tasks/{tasks['it_policy'].id}/acknowledge", json={"accepted": True}, headers=CSRF
        )
        assert ack.status_code == 204, ack.text
        sig = (await c.get(f"{PORTAL}/tasks/{tasks['nda'].id}/signature")).json()
        assert sig["signature_request_id"] == str(tasks["nda"].signature_request_id) and sig["signer_id"]
    finally:
        await c.aclose()

    tasks = await tasks_of(dorg, case.id)
    assert tasks["it_policy"].status == "approved"
    async with tenant_session(dorg.tenant_id) as db:
        acks = (await db.execute(select(OnboardingAcknowledgement))).scalars().all()
        assert len(acks) == 1 and len(acks[0].rendered_sha256) == 64
    with pytest.raises(Exception):  # noqa: B017 - immutability trigger
        async with tenant_session(dorg.tenant_id) as db:
            await db.execute(text("UPDATE onboarding_acknowledgements SET rendered_sha256 = 'x'"))
    # no sensitive values in onboarding rows or logs
    for t in tasks.values():
        assert ACCOUNT not in str(t.data) and PAN not in str(t.data)
    assert ACCOUNT not in caplog.text and PAN not in caplog.text

    # HR review: PAN needs the name check; return with comment, resubmit, approve
    no_check = await m.client.post(f"{TASKS}/{tasks['tax'].id}/approve", json={}, headers=h(m))
    assert (no_check.status_code, no_check.json()["error"]["code"]) == (422, "pan_name_check_required")
    short = await m.client.post(f"{TASKS}/{tasks['tax'].id}/return", json={"comment": "bad"}, headers=h(m))
    assert short.status_code == 422
    ret = await m.client.post(
        f"{TASKS}/{tasks['tax'].id}/return", json={"comment": "PAN name differs from ID"}, headers=h(m)
    )
    assert ret.status_code == 200 and ret.json()["status"] == "returned"
    c = await worker(app, dorg, p["candidate_id"])
    try:
        detail = (await c.get(f"{PORTAL}/cases/{case.id}")).json()
        returned = next(t for t in detail["tasks"] if t["kind"] == "tax_details")
        assert returned["status"] == "returned" and returned["return_comment"] == "PAN name differs from ID"
        assert (
            await _submit(c, tasks["tax"].id, {"pan": PAN, "pan_name": "Asha Rao", "regime": "old"})
        ).status_code == 200
    finally:
        await c.aclose()
    ok = await m.client.post(
        f"{TASKS}/{tasks['tax'].id}/approve", json={"pan_name_checked": True}, headers=h(m)
    )
    assert ok.status_code == 200, ok.text
    bulk = await m.client.post(
        f"{CASES}/{case.id}/approve",
        json={"task_ids": [str(tasks[k].id) for k in ("bank", "personal", "emergency", "nda")]},
        headers=h(m),
    )
    assert bulk.status_code == 200
    assert set(bulk.json()["approved"]) == {str(tasks[k].id) for k in ("bank", "personal", "emergency")}
    assert bulk.json()["failed"] == {str(tasks["nda"].id): "transition_not_allowed"}
    # workflow actor may never waive
    from app.core.context import RequestContext
    from app.core.errors import ConflictError
    from app.schemas.onboarding import WaiveIn
    from app.services.onboarding import review

    with pytest.raises(ConflictError):
        async with tenant_session(dorg.tenant_id) as db:
            await review.waive(
                RequestContext(tenant_id=dorg.tenant_id, actor_kind="workflow"),
                db,
                tasks["nda"].id,
                WaiveIn(reason="Workflow tries to waive"),
                actor="workflow",
            )
    # eSign declined -> returned, re-issue; completed -> approved
    await event(
        dorg,
        "SignatureRequestStatusChanged",
        {
            "request_id": str(tasks["nda"].signature_request_id),
            "subject_kind": "onboarding_task",
            "subject_id": str(tasks["nda"].id),
            "from_status": "sent",
            "to_status": "declined",
        },
    )
    assert (await tasks_of(dorg, case.id))["nda"].status == "returned"
    re = await m.client.post(f"{TASKS}/{tasks['nda'].id}/reissue-signature", headers=h(m))
    assert re.status_code == 200, re.text
    new_req = re.json()["signature_request_id"]
    assert new_req and new_req != str(tasks["nda"].signature_request_id)
    await event(
        dorg,
        "SignatureRequestCompleted",
        {
            "request_id": new_req,
            "subject_kind": "onboarding_task",
            "subject_id": str(tasks["nda"].id),
            "document_id": str(uuid.uuid4()),
        },
    )
    assert (await tasks_of(dorg, case.id))["nda"].status == "approved"
    assert (await case_of(dorg, p["id"])).status == "in_progress"  # type: ignore[union-attr]
    # IT owner completes the laptop task -> case complete, gate met, Worker 360 shows last4 only
    done = await m.client.post(f"/api/v1/tasks/{tasks['laptop'].p1_task_id}/complete", headers=h(m))
    assert done.status_code == 200, done.text
    final = await case_of(dorg, p["id"])
    assert final is not None and final.status == "complete" and final.progress_pct == 100
    assert await _gate(dorg, p["id"]) == "met"
    detail = (await m.client.get(f"{CASES}/{case.id}")).json()
    assert detail["bank_last4"] == "9012" and detail["pan_last4"] == "234F" and detail["gate_status"] == "met"
    assert ACCOUNT not in str(detail) and PAN not in str(detail)
    async with tenant_session(dorg.tenant_id) as db:
        actions = set(
            (
                await db.execute(text("SELECT action FROM audit_logs WHERE entity_type = 'onboarding_task'"))
            ).scalars()
        )
    assert {"onboarding.task_approved", "onboarding.task_returned", "onboarding.task_submitted"} <= actions
    board = (await m.client.get(f"{CASES}?starts_within=30&status=complete")).json()
    assert [r["id"] for r in board] == [str(case.id)] and board[0]["progress_pct"] == 100

    # Returning worker within a year: only the new NDA and the laptop are pending
    p2 = await placement(dorg, uuid.UUID(p["candidate_id"]), days=40)
    case2 = await case_of(dorg, p2["id"])
    assert case2 is not None
    t2 = await tasks_of(dorg, case2.id)
    pending = {k for k, t in t2.items() if t.status != "approved"}
    assert pending == {"nda", "laptop"}, {k: t.status for k, t in t2.items()}
    assert t2["bank"].reused_from_task_id == tasks["bank"].id and t2["bank"].decided_by_kind == "system"

    # Erasure: submitted values gone, statuses kept
    handler = OnboardingHandler()
    ectx = ErasureContext(
        tenant_id=dorg.tenant_id, candidate_id=uuid.UUID(p["candidate_id"]), run_id=uuid.uuid4(), reason="dsr"
    )
    async with tenant_session(dorg.tenant_id) as db:
        exported = await handler.export(db, ectx)
        assert exported and any(t["submitted_values"] for t in exported["tasks"])
        await handler.erase(db, ectx)
    async with tenant_session(dorg.tenant_id) as db:
        assert await handler.verify(db, ectx) == []
    assert (await tasks_of(dorg, case.id))["tax"].status == "approved"


async def test_aadhaar_document_needs_redaction_review(app: FastAPI, dorg: DOrg) -> None:
    m = dorg.admin
    await set_owner(dorg)
    await create_template(
        dorg,
        [
            {
                "key": "aadhaar",
                "kind": "document_upload",
                "label": "Aadhaar (masked)",
                "config": {"document_category": "identity", "aadhaar": True},
            }
        ],
        client_id=dorg.client_id,
    )
    p = await placement(dorg)
    case = await case_of(dorg, p["id"])
    assert case is not None
    task = (await tasks_of(dorg, case.id))["aadhaar"]
    c = await worker(app, dorg, p["candidate_id"])
    try:
        none_yet = await _submit(c, task.id, {"aadhaar_last4": "9012", "masked_image": True})
        assert none_yet.json()["error"]["code"] == "document_required"
        up = await c.post(
            f"{PORTAL}/tasks/{task.id}/files",
            files={"file": ("aadhaar.pdf", make_pdf("XXXX XXXX 9012"), "application/pdf")},
            headers=CSRF,
        )
        assert up.status_code == 201, up.text
        ok = await _submit(c, task.id, {"aadhaar_last4": "9012", "masked_image": True})
        assert ok.status_code == 200, ok.text
    finally:
        await c.aclose()
    refused = await m.client.post(f"{TASKS}/{task.id}/approve", json={}, headers=h(m))
    assert (refused.status_code, refused.json()["error"]["code"]) == (409, "redaction_review_required")
    assert (await m.client.post(f"{TASKS}/{task.id}/confirm-redaction", headers=h(m))).status_code == 200
    ok2 = await m.client.post(f"{TASKS}/{task.id}/approve", json={}, headers=h(m))
    assert ok2.status_code == 200 and ok2.json()["status"] == "approved"
    assert (await case_of(dorg, p["id"])).status == "complete"  # type: ignore[union-attr]


async def test_no_template_compliance_merge_credentials_and_waive(app: FastAPI, dorg: DOrg) -> None:
    m = dorg.admin
    await set_owner(dorg)
    p = await placement(dorg)
    assert await case_of(dorg, p["id"]) is None and await _gate(dorg, p["id"]) is None
    unmatched = (await m.client.get(f"{CASES}/unmatched")).json()
    assert p["id"] in [u["placement_id"] for u in unmatched]
    # compliance requirements arrive before the case exists -> held, merged at creation
    _, policy = await doc_templates(dorg)
    await event(
        dorg,
        "ComplianceRequirementsEvaluated",
        {
            "subject_kind": "placement",
            "subject_id": p["id"],
            "candidate_id": p["candidate_id"],
            "placement_id": p["id"],
            "bgv_package_ids": [],
            "credential_type_keys": ["nursing_license"],
            "document_categories": [],
            "policy_template_ids": [policy],
        },
    )
    held = await case_of(dorg, p["id"])
    assert held is not None and held.initialized_at is None
    assert (await m.client.get(f"{CASES}/by-placement/{p['id']}")).status_code == 404
    await create_template(
        dorg,
        [{"key": "emergency", "kind": "emergency_contact", "label": "Emergency contact"}],
        client_id=dorg.client_id,
    )
    started = await m.client.post(CASES, json={"placement_id": p["id"]}, headers=h(m))
    assert started.status_code == 201, started.text
    case_id = started.json()["id"]
    t = await tasks_of(dorg, case_id)
    assert set(t) == {"emergency", f"policy:{policy}", "credential:nursing_license"}
    assert (
        t["credential:nursing_license"].source == "compliance" and t["credential:nursing_license"].mandatory
    )
    # credential recorded -> submitted; verified -> approved
    cred = uuid.uuid4()
    await event(
        dorg,
        "CredentialRecorded",
        {"credential_id": str(cred), "candidate_id": p["candidate_id"], "type_key": "nursing_license"},
    )
    assert (await tasks_of(dorg, case_id))["credential:nursing_license"].status == "submitted"
    await event(
        dorg,
        "CredentialVerified",
        {
            "credential_id": str(cred),
            "candidate_id": p["candidate_id"],
            "type_key": "nursing_license",
            "verification_status": "valid",
        },
    )
    assert (await tasks_of(dorg, case_id))["credential:nursing_license"].status == "approved"
    # HR waives the rest (human, reason >= 10 chars)
    short = await m.client.post(f"{TASKS}/{t['emergency'].id}/waive", json={"reason": "no"}, headers=h(m))
    assert short.status_code == 422
    for key in ("emergency", f"policy:{policy}"):
        w = await m.client.post(
            f"{TASKS}/{t[key].id}/waive",
            json={"reason": "Collected on paper at the client site"},
            headers=h(m),
        )
        assert w.status_code == 200, w.text
    assert (await case_of(dorg, p["id"])).status == "complete"  # type: ignore[union-attr]
    assert await _gate(dorg, p["id"]) == "met"
    # items after Complete -> optional, case not reopened
    await event(
        dorg,
        "ComplianceRequirementsEvaluated",
        {
            "subject_kind": "placement",
            "subject_id": p["id"],
            "candidate_id": p["candidate_id"],
            "placement_id": p["id"],
            "bgv_package_ids": [],
            "credential_type_keys": ["first_aid"],
            "document_categories": [],
            "policy_template_ids": [],
        },
    )
    late = (await tasks_of(dorg, case_id))["credential:first_aid"]
    assert late.mandatory is False
    assert (await case_of(dorg, p["id"])).status == "complete"  # type: ignore[union-attr]


async def test_template_validation_and_drop_cancels(app: FastAPI, dorg: DOrg) -> None:
    m = dorg.admin
    missing = await m.client.post(
        "/api/v1/onboarding/templates",
        json={
            "name": "Bad",
            "tasks": [
                {
                    "key": "nda",
                    "kind": "esign",
                    "label": "NDA",
                    "config": {"document_template_id": str(uuid.uuid4())},
                }
            ],
        },
        headers=h(m),
    )
    assert (missing.status_code, missing.json()["error"]["code"]) == (422, "document_template_not_found")
    no_owner = await m.client.post(
        "/api/v1/onboarding/templates",
        json={"name": "Bad", "tasks": [{"key": "it", "kind": "internal", "label": "Access"}]},
        headers=h(m),
    )
    assert no_owner.json()["error"]["code"] == "owner_required"
    dup = await m.client.post(
        "/api/v1/onboarding/templates",
        json={
            "name": "Bad",
            "tasks": [
                {"key": "a", "kind": "emergency_contact", "label": "A"},
                {"key": "a", "kind": "personal_details", "label": "B"},
            ],
        },
        headers=h(m),
    )
    assert dup.json()["error"]["code"] == "duplicate_task_key"
    tpl = await create_template(
        dorg, [{"key": "emergency", "kind": "emergency_contact", "label": "Emergency"}]
    )
    patched = await m.client.patch(
        f"/api/v1/onboarding/templates/{tpl['id']}", json={"name": "Renamed"}, headers=h(m)
    )
    assert patched.json()["version"] == 2
    p = await placement(dorg)
    match = await m.client.get(f"/api/v1/onboarding/templates/match?placement_id={p['id']}")
    assert match.json()["template_id"] == tpl["id"]
    case = await case_of(dorg, p["id"])
    assert case is not None and case.template_snapshot and case.template_snapshot["version"] == 2
    drop = await m.client.post(
        f"/api/v1/placements/{p['id']}/drop", json={"reason": "declined"}, headers=h(m)
    )
    assert drop.status_code == 200, drop.text
    assert (await case_of(dorg, p["id"])).status == "cancelled"  # type: ignore[union-attr]
    assert await _gate(dorg, p["id"]) == "not_required"


async def test_isolation(app: FastAPI, dorg: DOrg, other_dorg: DOrg) -> None:
    await set_owner(dorg)
    await create_template(dorg, [{"key": "emergency", "kind": "emergency_contact", "label": "Emergency"}])
    p = await placement(dorg)
    other = await placement(dorg)
    case = await case_of(dorg, p["id"])
    other_case = await case_of(dorg, other["id"])
    assert case is not None and other_case is not None
    task = (await tasks_of(dorg, case.id))["emergency"]
    o = other_dorg.admin
    for url in (f"{CASES}/{case.id}", f"{CASES}/by-placement/{p['id']}"):
        assert (await o.client.get(url)).status_code == 404
    for action, body in (
        ("approve", {}),
        ("return", {"comment": "Please redo this"}),
        ("waive", {"reason": "Not needed for this client"}),
    ):
        assert (
            await o.client.post(f"{TASKS}/{task.id}/{action}", json=body, headers=h(o))
        ).status_code == 404
    assert (await o.client.post(f"{CASES}/{case.id}/invite", headers=h(o))).status_code == 404
    assert (await o.client.get(CASES)).json() == []
    c = await worker(app, dorg, other["candidate_id"])
    try:
        assert (await c.get(f"{PORTAL}/cases/{case.id}")).status_code == 404
        assert (await _submit(c, task.id, {"name": "X", "phone": "+91 98765 43210"})).status_code == 404
        assert (
            await c.patch(f"{PORTAL}/tasks/{task.id}/draft", json={"draft": {}}, headers=CSRF)
        ).status_code == 404
        assert [x["id"] for x in (await c.get(f"{PORTAL}/cases")).json()] == [str(other_case.id)]
    finally:
        await c.aclose()
    # org RLS: a worker-scoped session sees only its own rows and never internal tasks
    from app.core.database import OrgScope

    async with tenant_session(
        dorg.tenant_id, org=OrgScope.of("worker", uuid.UUID(other["candidate_id"]))
    ) as db:
        ids = set((await db.execute(text("SELECT id FROM onboarding_cases"))).scalars())
        assert ids == {other_case.id}
        assert (
            await db.execute(text("SELECT count(*) FROM onboarding_tasks WHERE assignee = 'user'"))
        ).scalar() == 0
        assert (await db.execute(text("SELECT count(*) FROM onboarding_templates"))).scalar() == 0


async def test_credential_task_links_existing_valid_credential(app: FastAPI, dorg: DOrg) -> None:
    """P2 fix: a "Provide credential" task is approved at creation when the worker already holds a Valid credential
    of that type; a credential recorded before the case and verified after it is linked and approved too."""
    from datetime import date, timedelta

    from tests.modules.compliance.helpers import record, verify
    from tests.modules.offers.helpers import create_candidate

    await set_owner(dorg)
    await create_template(
        dorg,
        [{"key": "passport", "kind": "credential", "label": "Passport", "config": {"credential_type_key": "passport"}}],
        client_id=dorg.client_id,
    )
    future = (date.today() + timedelta(days=400)).isoformat()
    # 1) Valid before the case exists -> the task is created approved and linked.
    cand = await create_candidate(dorg.admin, f"Cred Holder {uuid.uuid4().hex[:4]}")
    rec = await record(dorg, cand, "passport", number="K1234567", expires_on=future, issuer="RPO Pune")
    assert rec.status_code == 201, rec.text
    assert (await verify(dorg, rec.json()["id"])).status_code == 200
    p = await placement(dorg, cand)
    await event(dorg, "PlacementCreated", {"placement_id": p["id"]})
    case = await case_of(dorg, p["id"])
    assert case is not None
    task = (await tasks_of(dorg, case.id))["passport"]
    assert task.status == "approved" and str(task.credential_id) == rec.json()["id"]
    assert task.decided_by_kind == "system"
    # 2) Recorded (pending) before the case, verified Valid after -> linked and approved.
    cand2 = await create_candidate(dorg.admin, f"Cred Later {uuid.uuid4().hex[:4]}")
    rec2 = await record(dorg, cand2, "passport", number="K7654321", expires_on=future, issuer="RPO Pune")
    assert rec2.status_code == 201, rec2.text
    p2 = await placement(dorg, cand2)
    await event(dorg, "PlacementCreated", {"placement_id": p2["id"]})
    case2 = await case_of(dorg, p2["id"])
    assert case2 is not None
    assert (await tasks_of(dorg, case2.id))["passport"].status == "pending"
    assert (await verify(dorg, rec2.json()["id"])).status_code == 200
    payload = {
        "credential_id": rec2.json()["id"],
        "candidate_id": str(cand2),
        "type_key": "passport",
        "verification_status": "valid",
    }
    await event(dorg, "CredentialVerified", payload)  # no-op if the event bus already delivered it
    task2 = (await tasks_of(dorg, case2.id))["passport"]
    assert task2.status == "approved" and str(task2.credential_id) == rec2.json()["id"]
