# ruff: noqa: E501
"""Epic 20 acceptance flow: case creation -> consent + documents -> provider link -> discrepancy -> response ->
resolve -> ★ adjudicate Clear -> gate met; plus consent withdrawal, ad-hoc / dropped cases and isolation."""

import uuid

import pytest
from fastapi import FastAPI
from sqlalchemy import select, text

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import ConflictError
from app.models.bgv import BgvCase, BgvCheck
from app.models.tasks import Task
from app.schemas.bgv import AdjudicateIn, WaiveIn
from app.services.bgv import adjudication, cases, checks
from app.services.privacy.consent import withdraw_consent
from tests.modules.bgv.helpers import (
    API,
    CSRF,
    PDF,
    PNG,
    PORTAL,
    case_for,
    consent_and_documents,
    evaluated,
    full_aadhaar,
    link_token,
    mails,
    make_package,
    make_provider,
    placement_with_case,
    sys_ctx,
)
from tests.modules.demands.helpers import DOrg, h
from tests.modules.identity.conftest import join
from tests.modules.placements.helpers import PLACEMENTS, direct_placement
from tests.p2 import make_client_user, sign_in_candidate, sign_in_external

REASONS = "All checks verified; employment date mismatch explained by relieving letter."


async def _gate(org: DOrg, placement_id: str) -> dict:  # type: ignore[type-arg]
    gates = (await org.admin.client.get(f"{PLACEMENTS}/{placement_id}/start-gates")).json()
    (bgv,) = [g for g in gates if g["kind"] == "bgv"]
    return bgv


async def test_acceptance_flow_to_clear(app: FastAPI, dorg: DOrg) -> None:
    package, placement, case = await placement_with_case(dorg)
    cid = case["id"]
    assert case["status"] == "initiated" and case["required"] is True
    assert {c["type"] for c in case["checks"]} == {"employment", "identity"}
    assert {d["key"] for d in case["documents"]} == {"employment_history", "relieving_letter", "aadhaar"}
    gate = await _gate(dorg, placement["id"])
    assert gate["status"] == "pending" and gate["overridable"] is False
    # idempotent
    assert await evaluated(dorg, placement, [package["id"]]) == 0

    compliance = await join(app, dorg.tenant, "compliance", name="Cora Compliance")
    c = compliance.client
    provider = await make_provider(dorg)
    resp = await c.post(f"{API}/bgv-cases/{cid}/start", headers=h(compliance))
    assert resp.status_code == 200, resp.text and resp.json()["status"] == "documents_pending"
    assert resp.json()["status"] == "documents_pending"
    assert mails(dorg.tenant_id, "bgv_documents_requested")

    emp = next(x for x in case["checks"] if x["type"] == "employment")
    idc = next(x for x in case["checks"] if x["type"] == "identity")
    r = await c.patch(
        f"{API}/bgv-checks/{emp['id']}",
        json={"assignee_kind": "provider", "provider_id": provider["id"]},
        headers=h(compliance),
    )
    assert r.status_code == 200, r.text
    r = await c.patch(
        f"{API}/bgv-checks/{idc['id']}",
        json={"assignee_kind": "internal", "assignee_user_id": str(compliance.user_id)},
        headers=h(compliance),
    )
    assert r.status_code == 200, r.text
    # no consent yet -> 409
    r = await c.post(f"{API}/bgv-checks/{emp['id']}/start", headers=h(compliance))
    assert r.status_code == 409 and r.json()["error"]["code"] == "bgv_consent_missing"

    worker = await sign_in_candidate(app, uuid.UUID(placement["candidate_id"]), dorg.tenant_id)
    try:
        mine = await worker.get(f"{PORTAL}/bgv/cases")
        assert mine.status_code == 200 and [x["id"] for x in mine.json()] == [cid]
        assert mine.json()[0]["status"] == "in_progress"
        # a full Aadhaar number is refused anywhere
        bad = await worker.post(
            f"{PORTAL}/bgv/cases/{cid}/documents/aadhaar",
            data={"aadhaar_last4": full_aadhaar()},
            files=[("files", ("a.png", PNG, "image/png"))],
            headers=CSRF,
        )
        assert bad.status_code == 422 and bad.json()["error"]["code"] == "aadhaar_full_number_forbidden"
        await consent_and_documents(app, dorg, case, worker)

        r = await c.post(f"{API}/bgv-checks/{emp['id']}/start", headers=h(compliance))
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "in_progress" and r.json()["provider_link_active"] is True
        r = await c.post(f"{API}/bgv-checks/{idc['id']}/start", headers=h(compliance))
        assert r.status_code == 200, r.text
        async with tenant_session(dorg.tenant_id) as db:
            task = (await db.execute(select(Task).where(Task.source == "bgv"))).scalars().first()
            assert task is not None and task.owner_id == compliance.user_id
        detail = (await c.get(f"{API}/bgv-cases/{cid}")).json()
        assert detail["status"] == "in_verification"

        # ---- provider link: minimal view, own files only, proposal only
        (msg,) = mails(dorg.tenant_id, "bgv_provider_check")
        token = link_token(msg)
        assert token not in str(detail)
        pub = app_client(app)
        view = await pub.get(f"{API}/public/links/{token}/bgv-check")
        assert view.status_code == 200, view.text
        body = view.json()
        assert body["check_type"] == "employment" and body["subject_label"] == "Employer: Infosys"
        assert body["candidate_name"].endswith(".") and "aadhaar" not in body["declarations"]
        assert len(body["files"]) == 1  # the relieving letter only (not the Aadhaar image)
        ok = await pub.get(f"{API}/public/links/{token}/bgv-check/files/{body['files'][0]['file_id']}")
        assert ok.status_code == 200 and ok.content.startswith(b"%PDF")
        aadhaar_file = next(d for d in detail["documents"] if d["key"] == "aadhaar")["file_ids"][0]
        nope = await pub.get(f"{API}/public/links/{token}/bgv-check/files/{aadhaar_file}")
        assert nope.status_code == 404
        again = await pub.get(f"{API}/public/links/{token}/bgv-check")
        assert again.status_code == 200  # GET never consumes
        sub = await pub.post(
            f"{API}/public/links/{token}/bgv-check",
            data={
                "result": "discrepancy",
                "note": "Joining date differs by 2 months",
                "verifier_name": "HR desk",
            },
            files=[("files", ("hr-mail.pdf", PDF, "application/pdf"))],
        )
        assert sub.status_code == 200, sub.text
        used = await pub.post(f"{API}/public/links/{token}/bgv-check", data={"result": "verified"})
        assert used.status_code == 410
        await pub.aclose()
        chk = next(
            x for x in (await c.get(f"{API}/bgv-cases/{cid}")).json()["checks"] if x["id"] == emp["id"]
        )
        assert chk["status"] == "in_progress" and chk["provider_proposal"]["result"] == "discrepancy"
        assert len(chk["evidence_file_ids"]) == 1

        # ---- Compliance records the discrepancy (accepting the proposal)
        r = await c.post(
            f"{API}/bgv-checks/{emp['id']}/result",
            json={"result": "discrepancy", "accept_proposal": True},
            headers=h(compliance),
        )
        assert r.status_code == 422 and r.json()["error"]["code"] == "discrepancy_required"
        r = await c.post(
            f"{API}/bgv-checks/{emp['id']}/result",
            json={
                "result": "discrepancy",
                "accept_proposal": True,
                "discrepancy": {
                    "severity": "minor",
                    "description": "Joining date differs from declaration",
                    "internal_note": "HR said typo",
                },
            },
            headers=h(compliance),
        )
        assert r.status_code == 200, r.text
        assert r.json()["verifier_name"] == "HR desk"
        detail = (await c.get(f"{API}/bgv-cases/{cid}")).json()
        assert detail["status"] == "discrepancy" and detail["discrepancy_awaiting_response"] is True
        # failed is blocked while awaiting the candidate
        r = await c.post(
            f"{API}/bgv-cases/{cid}/adjudicate",
            json={"outcome": "failed", "reasons": REASONS},
            headers=h(compliance),
        )
        assert r.status_code == 409 and r.json()["error"]["code"] == "discrepancy_awaiting_response"

        # ---- worker responds (no internal note visible)
        mine = await worker.get(f"{PORTAL}/bgv/discrepancies")
        assert mine.status_code == 200 and len(mine.json()) == 1
        assert "HR said typo" not in mine.text and "internal_note" not in mine.text
        did = mine.json()[0]["id"]
        resp = await worker.post(
            f"{PORTAL}/bgv/discrepancies/{did}/respond",
            data={"response": "My relieving letter shows the correct date."},
            files=[("files", ("relieving.pdf", PDF, "application/pdf"))],
            headers=CSRF,
        )
        assert resp.status_code == 200, resp.text and resp.json()["status"] == "responded"
    finally:
        await worker.aclose()

    # identity verified -> all checks final, nothing awaiting -> under_review
    r = await c.post(
        f"{API}/bgv-checks/{idc['id']}/result",
        json={"result": "verified", "note": "Matches", "verified_on": "2026-10-05"},
        headers=h(compliance),
    )
    assert r.status_code == 200, r.text
    assert (await c.get(f"{API}/bgv-cases/{cid}")).json()["status"] == "under_review"

    # ★ a workflow actor cannot adjudicate
    async with tenant_session(dorg.tenant_id) as db:
        with pytest.raises(ConflictError) as exc:
            await adjudication.adjudicate(
                RequestContext(tenant_id=dorg.tenant_id, actor_kind="workflow"),
                db,
                uuid.UUID(cid),
                AdjudicateIn(outcome="clear", reasons=REASONS),
            )
        assert exc.value.code == "transition_not_allowed"

    r = await c.post(
        f"{API}/bgv-discrepancies/{did}/resolve",
        json={"resolution_note": "Relieving letter confirms"},
        headers=h(compliance),
    )
    assert r.status_code == 200, r.text
    short = await c.post(
        f"{API}/bgv-cases/{cid}/adjudicate", json={"outcome": "clear", "reasons": "ok"}, headers=h(compliance)
    )
    assert short.status_code == 422
    r = await c.post(
        f"{API}/bgv-cases/{cid}/adjudicate",
        json={"outcome": "clear", "with_exceptions": True, "reasons": REASONS},
        headers=h(compliance),
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert (
        out["status"] == "clear"
        and out["outcome_qualifier"] == "with_exceptions"
        and out["evidence_purge_after"]
    )
    assert (await _gate(dorg, placement["id"]))["status"] == "met"
    timeline = (await c.get(f"{API}/bgv-cases/{cid}/timeline")).json()
    kinds = [e["kind"] for e in timeline]
    for k in ("created", "started", "consent_given", "documents_submitted", "provider_proposal", "check_recorded",
              "discrepancy_raised", "discrepancy_responded", "discrepancy_resolved", "adjudicated"):  # fmt: skip
        assert k in kinds, k
    async with tenant_session(dorg.tenant_id) as db:
        audited = (
            await db.execute(
                text("SELECT count(*) FROM audit_logs WHERE entity_type = 'bgv_case' AND entity_id = :i"),
                {"i": cid},
            )
        ).scalar()
    assert audited and audited > 0
    # the worker sees clear, never the reasons
    worker = await sign_in_candidate(app, uuid.UUID(placement["candidate_id"]), dorg.tenant_id)
    try:
        mine = await worker.get(f"{PORTAL}/bgv/cases")
        assert mine.json()[0]["status"] == "clear" and "relieving letter confirms" not in mine.text.lower()
        assert REASONS not in mine.text
    finally:
        await worker.aclose()


def app_client(app: FastAPI):  # type: ignore[no-untyped-def]  # noqa: ANN201
    from tests.modules.identity.conftest import new_client

    return new_client(app)


async def test_consent_withdrawal_halts_and_blocks(app: FastAPI, dorg: DOrg) -> None:
    _, placement, case = await placement_with_case(dorg)
    cid = case["id"]
    m = dorg.admin
    provider = await make_provider(dorg)
    await m.client.post(f"{API}/bgv-cases/{cid}/start", headers=h(m))
    worker = await sign_in_candidate(app, uuid.UUID(placement["candidate_id"]), dorg.tenant_id)
    try:
        await consent_and_documents(app, dorg, case, worker)
    finally:
        await worker.aclose()
    emp = next(x for x in case["checks"] if x["type"] == "employment")
    await m.client.patch(
        f"{API}/bgv-checks/{emp['id']}",
        json={"assignee_kind": "provider", "provider_id": provider["id"]},
        headers=h(m),
    )
    r = await m.client.post(f"{API}/bgv-checks/{emp['id']}/start", headers=h(m))
    assert r.status_code == 200 and r.json()["provider_link_active"] is True
    token = link_token(mails(dorg.tenant_id, "bgv_provider_check")[-1])

    async with tenant_session(dorg.tenant_id) as db:
        await withdraw_consent(
            RequestContext(tenant_id=dorg.tenant_id),
            db,
            uuid.UUID(placement["candidate_id"]),
            "bgv",
            by_kind="candidate",
        )
    detail = (await m.client.get(f"{API}/bgv-cases/{cid}")).json()
    chk = next(x for x in detail["checks"] if x["id"] == emp["id"])
    assert detail["consent_active"] is False and detail["consent_withdrawn_at"]
    assert chk["halted_reason"] == "consent_withdrawn" and chk["provider_link_active"] is False
    pub = app_client(app)
    gone = await pub.get(f"{API}/public/links/{token}/bgv-check")
    assert gone.status_code == 410
    await pub.aclose()
    r = await m.client.post(f"{API}/bgv-checks/{emp['id']}/result", json={"result": "verified"}, headers=h(m))
    assert r.status_code == 409 and r.json()["error"]["code"] == "bgv_consent_missing"
    r = await m.client.post(f"{API}/bgv-checks/{emp['id']}/start", headers=h(m))
    assert r.status_code == 409 and r.json()["error"]["code"] == "bgv_consent_missing"
    async with tenant_session(dorg.tenant_id) as db:
        notes = (
            (
                await db.execute(
                    text("SELECT title FROM notifications WHERE title LIKE '%withdrew BGV consent%'")
                )
            )
            .scalars()
            .all()
        )
    assert notes
    r = await m.client.post(
        f"{API}/bgv-cases/{cid}/cancel", json={"reason": "Consent withdrawn"}, headers=h(m)
    )
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    refreshed = await m.client.post(f"{PLACEMENTS}/{placement['id']}/start-gates/refresh", headers=h(m))
    assert refreshed.status_code == 200, refreshed.text
    assert (await _gate(dorg, placement["id"]))["status"] == "not_required"


async def test_adhoc_case_no_blocking_gate_and_dropped_cancels(dorg: DOrg) -> None:
    package = await make_package(dorg)
    placement = await direct_placement(dorg)
    m = dorg.admin
    r = await m.client.post(
        f"{API}/candidates/{placement['candidate_id']}/bgv-cases",
        json={"package_id": package["id"], "placement_id": placement["id"]},
        headers=h(m),
    )
    assert r.status_code == 201, r.text
    assert r.json()["required"] is False
    gates = (await m.client.get(f"{PLACEMENTS}/{placement['id']}/start-gates")).json()
    assert all(not g["required"] for g in gates if g["kind"] == "bgv")
    dup = await m.client.post(
        f"{API}/candidates/{placement['candidate_id']}/bgv-cases",
        json={"package_id": package["id"], "placement_id": placement["id"]},
        headers=h(m),
    )
    assert dup.status_code == 409
    async with tenant_session(dorg.tenant_id) as db:
        assert await cases.handle_placement_dropped(sys_ctx(dorg), db, uuid.UUID(placement["id"])) == 1
    assert (await case_for(dorg, placement["id"]))["status"] == "cancelled"


async def test_reuse_clear_case_links_new_placement(dorg: DOrg) -> None:
    package, placement, case = await placement_with_case(dorg)
    async with tenant_session(dorg.tenant_id) as db:
        await db.execute(
            text(
                "UPDATE bgv_cases SET status = 'clear', adjudicated_at = now(), placement_id = NULL WHERE id = :i"
            ),
            {"i": uuid.UUID(case["id"])},
        )
    second = await direct_placement(dorg, candidate_id=uuid.UUID(placement["candidate_id"]))
    assert await evaluated(dorg, second, [package["id"]]) == 0
    linked = await case_for(dorg, second["id"])
    assert linked["id"] == case["id"]


@pytest.mark.parametrize("kind", ["workflow", "ai", "system"])
async def test_waive_is_human_only(dorg: DOrg, kind: str) -> None:
    _, _, case = await placement_with_case(dorg)
    async with tenant_session(dorg.tenant_id) as db:
        with pytest.raises(ConflictError) as exc:
            await checks.waive(
                RequestContext(tenant_id=dorg.tenant_id, actor_kind=kind),
                db,
                uuid.UUID(case["checks"][0]["id"]),
                WaiveIn(reason="Not applicable for this client"),
            )
    assert exc.value.code == "transition_not_allowed"


async def test_isolation_cross_tenant_worker_and_client(app: FastAPI, dorg: DOrg, other_dorg: DOrg) -> None:
    _, placement, case = await placement_with_case(dorg)
    cid = case["id"]
    o = other_dorg.admin
    assert (await o.client.get(f"{API}/bgv-cases/{cid}")).status_code == 404
    assert (await o.client.post(f"{API}/bgv-cases/{cid}/start", headers=h(o))).status_code == 404
    assert (await o.client.get(f"{API}/bgv-checks/{case['checks'][0]['id']}/packet")).status_code == 404
    other_placement = await direct_placement(dorg)
    stranger = await sign_in_candidate(app, uuid.UUID(other_placement["candidate_id"]), dorg.tenant_id)
    try:
        assert (await stranger.get(f"{PORTAL}/bgv/cases/{cid}")).status_code == 404
        assert (await stranger.get(f"{PORTAL}/bgv/cases")).json() == []
    finally:
        await stranger.aclose()
    # client portal: no contract sharing -> null; another client's placement -> 404
    cu = await make_client_user(dorg.tenant_id, dorg.client_id)
    ext = await sign_in_external(app, "client_user", cu, dorg.tenant_id, dorg.client_id)
    try:
        r = await ext.client.get(f"{API}/client-portal/placements/{placement['id']}/bgv-status")
        assert r.status_code == 200, r.text and r.json() == {"status": None}
        assert r.json() == {"status": None}
    finally:
        await ext.client.aclose()
    from tests.modules.demands.helpers import create_client

    other_client = await create_client(dorg.admin)
    cu2 = await make_client_user(dorg.tenant_id, other_client)
    ext2 = await sign_in_external(app, "client_user", cu2, dorg.tenant_id, other_client)
    try:
        r = await ext2.client.get(f"{API}/client-portal/placements/{placement['id']}/bgv-status")
        assert r.status_code == 404
    finally:
        await ext2.client.aclose()
    # org RLS: a worker session sees no rows of another candidate, and no reference answers at all
    from app.core.database import OrgScope

    async with tenant_session(
        dorg.tenant_id, org=OrgScope.of("worker", uuid.UUID(other_placement["candidate_id"]))
    ) as db:
        assert (
            await db.execute(select(BgvCase).where(BgvCase.id == uuid.UUID(cid)))
        ).scalar_one_or_none() is None
        assert (await db.execute(select(BgvCheck).where(BgvCheck.case_id == uuid.UUID(cid)))).first() is None


async def test_lawful_attestation_and_archive(dorg: DOrg) -> None:
    m = dorg.admin
    body = {"name": "Criminal", "checks": [{"type": "criminal_record", "count": 1}]}
    r = await m.client.post(f"{API}/bgv/packages", json=body, headers=h(m))
    assert r.status_code == 422 and r.json()["error"]["code"] == "lawful_attestation_required"
    r = await m.client.post(f"{API}/bgv/packages", json={**body, "lawful_confirmed": True}, headers=h(m))
    assert r.status_code == 201 and r.json()["lawful_confirmed_by"] == str(m.user_id)
    pkg = r.json()
    placement = await direct_placement(dorg)
    await evaluated(dorg, placement, [pkg["id"]])
    r = await m.client.post(f"{API}/bgv/packages/{pkg['id']}/archive", headers=h(m))
    assert r.status_code == 200 and r.json()["archived_at"]
    case = await case_for(dorg, placement["id"])
    assert case["package_name"] == "Criminal" and case["checks"][0]["type"] == "criminal_record"
