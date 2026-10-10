# ruff: noqa: E501
"""Reference checks (referee links, answers visibility, expiry), evidence retention purge and erasure."""

import uuid
from datetime import date, timedelta

from fastapi import FastAPI
from freezegun import freeze_time
from sqlalchemy import select, text

from app.core.database import tenant_session
from app.models.bgv import BgvCase, BgvDocumentRequest, Referee
from app.models.files import StoredFile
from app.services.bgv import references, retention
from app.services.bgv_erasure import BgvHandler
from app.services.privacy.registry import ErasureContext
from tests.modules.bgv.helpers import (
    API,
    CSRF,
    PORTAL,
    consent_and_documents,
    link_token,
    mails,
    placement_with_case,
)
from tests.modules.demands.helpers import DOrg, h
from tests.modules.identity.conftest import join, new_client
from tests.modules.placements.helpers import direct_placement
from tests.p2 import sign_in_candidate


async def test_reference_flow_visibility_and_expiry(app: FastAPI, dorg: DOrg) -> None:
    placement = await direct_placement(dorg)
    cand = placement["candidate_id"]
    viewer = await join(app, dorg.tenant, "compliance", name="Cora Compliance")
    m = dorg.admin
    r = await m.client.post(
        f"{API}/candidates/{cand}/reference-checks", json={"min_referees": 2}, headers=h(m)
    )
    assert r.status_code == 201, r.text
    rc = r.json()
    assert rc["status"] == "requested" and len(rc["questionnaire_snapshot"]["questions"]) >= 3

    worker = await sign_in_candidate(app, uuid.UUID(cand), dorg.tenant_id)
    try:
        mine = (await worker.get(f"{PORTAL}/reference-checks")).json()
        assert mine[0]["id"] == rc["id"] and mine[0]["consent_given"] is False
        notice_id = mine[0]["notice_id"]
        refs = [
            {
                "name": "Meera Manager",
                "relationship": "Manager",
                "company": "Infosys",
                "email": "meera@ref.example.com",
                "phone": "98450 12345",
            },
            {"name": "Ravi Peer", "email": "ravi@ref.example.com"},
        ]
        r = await worker.post(
            f"{PORTAL}/reference-checks/{rc['id']}/referees", json={"referees": refs}, headers=CSRF
        )
        assert r.status_code == 409 and r.json()["error"]["code"] == "reference_consent_missing"
        own = (await m.client.get(f"/api/v1/candidates/{cand}")).json()["emails"][0]["email"]
        r = await worker.post(
            f"{PORTAL}/reference-checks/{rc['id']}/referees",
            json={
                "referees": [{"name": "Me Myself", "email": own}],
                "agreed": True,
                "consent_notice_id": notice_id,
            },
            headers=CSRF,
        )
        assert r.status_code == 422
        r = await worker.post(
            f"{PORTAL}/reference-checks/{rc['id']}/referees",
            json={"referees": refs, "agreed": True, "consent_notice_id": notice_id},
            headers=CSRF,
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "sent" and len(r.json()["referees"]) == 2
    finally:
        await worker.aclose()
    detail = (await m.client.get(f"{API}/reference-checks/{rc['id']}")).json()
    assert {x["phone"] for x in detail["referees"]} == {"+919845012345", None}

    msgs = {m_.to: m_ for m_ in mails(dorg.tenant_id, "reference_check")}
    token = link_token(msgs["meera@ref.example.com"])
    pub = new_client(app)
    view = await pub.get(f"{API}/public/links/{token}/reference")
    assert view.status_code == 200 and view.json()["status"] == "sent"
    frame = await pub.get(f"{API}/public/links/{token}")
    assert frame.status_code == 200 and frame.json()["purpose"] == "reference_check"
    bad = await pub.post(f"{API}/public/links/{token}/reference", json={"answers": {"performance": 9}})
    assert bad.status_code == 422
    answers = {
        "relationship": "Managed her 3 years",
        "role": "Data engineer",
        "performance": 5,
        "rehire": True,
    }
    ok = await pub.post(f"{API}/public/links/{token}/reference", json={"answers": answers})
    assert ok.status_code == 200, ok.text
    again = await pub.post(f"{API}/public/links/{token}/reference", json={"answers": answers})
    assert again.status_code == 410
    await pub.aclose()

    # the second referee's link expires -> the check is responded (one answer)
    async with tenant_session(dorg.tenant_id) as db:
        await db.execute(
            text(
                "UPDATE referees SET expires_at = now() - interval '1 minute' WHERE email = 'ravi@ref.example.com'"
            )
        )
    async with tenant_session(dorg.tenant_id) as db:
        out = await references.reminders_and_expiry(db)
    assert out["expired"] == 1
    detail = (await m.client.get(f"{API}/reference-checks/{rc['id']}")).json()
    assert detail["status"] == "responded"
    assert {x["status"] for x in detail["referees"]} == {"responded", "expired"}

    # answers: scorecard:read only; never in the worker portal
    resp = await viewer.client.get(f"{API}/reference-checks/{rc['id']}/responses")
    assert resp.status_code == 200 and resp.json()["responses"][0]["answers"]["performance"] == 5
    worker = await sign_in_candidate(app, uuid.UUID(cand), dorg.tenant_id)
    try:
        assert "Managed her" not in (await worker.get(f"{PORTAL}/reference-checks")).text
    finally:
        await worker.aclose()
    r = await viewer.client.post(
        f"{API}/reference-checks/{rc['id']}/review", json={"note": "Positive"}, headers=h(viewer)
    )
    assert r.status_code == 200 and r.json()["status"] == "reviewed"


async def test_responses_need_scorecard_read(app: FastAPI, dorg: DOrg) -> None:
    placement = await direct_placement(dorg)
    hr = await join(app, dorg.tenant, "hr_onboarding", name="Hari HR")
    r = await dorg.admin.client.post(
        f"{API}/candidates/{placement['candidate_id']}/reference-checks", json={}, headers=h(dorg.admin)
    )
    resp = await hr.client.get(f"{API}/reference-checks/{r.json()['id']}/responses")
    assert resp.status_code == 403


async def test_purge_removes_files_and_keeps_outcome(app: FastAPI, dorg: DOrg) -> None:
    _, placement, case = await placement_with_case(dorg)
    cid = uuid.UUID(case["id"])
    m = dorg.admin
    await m.client.post(f"{API}/bgv-cases/{case['id']}/start", headers=h(m))
    worker = await sign_in_candidate(app, uuid.UUID(placement["candidate_id"]), dorg.tenant_id)
    try:
        await consent_and_documents(app, dorg, case, worker)
    finally:
        await worker.aclose()
    adjudicated = date(2026, 10, 5)
    async with tenant_session(dorg.tenant_id) as db:
        await db.execute(
            text(
                "UPDATE bgv_cases SET status = 'clear', adjudicated_at = :a, adjudication_reasons = 'All verified fine here', "
                "evidence_purge_after = :p WHERE id = :i"
            ),
            {"a": adjudicated, "p": adjudicated + timedelta(days=365), "i": cid},
        )
        file_ids = [
            f
            for r in (
                await db.execute(select(BgvDocumentRequest).where(BgvDocumentRequest.case_id == cid))
            ).scalars()
            for f in r.file_ids
        ]
    assert file_ids
    async with tenant_session(dorg.tenant_id) as db:
        assert (await retention.purge_due(db))["cases"] == 0  # not yet due
    with freeze_time(adjudicated + timedelta(days=366)):
        async with tenant_session(dorg.tenant_id) as db:
            assert await retention.preview_due(db)
            out = await retention.purge_due(db)
    assert out["cases"] == 1 and out["files"] >= len(file_ids)
    async with tenant_session(dorg.tenant_id) as db:
        case_row = await db.get(BgvCase, cid)
        assert case_row is not None and case_row.status == "clear" and case_row.adjudication_reasons
        assert case_row.evidence_purged_at is not None
        files = (await db.execute(select(StoredFile).where(StoredFile.id.in_(file_ids)))).scalars().all()
        assert all(f.deleted_at is not None for f in files)
        reqs = (
            (await db.execute(select(BgvDocumentRequest).where(BgvDocumentRequest.case_id == cid)))
            .scalars()
            .all()
        )
        assert all(r.declared is None for r in reqs)


async def test_erasure_retained_window_and_referees(app: FastAPI, dorg: DOrg) -> None:
    _, placement, case = await placement_with_case(dorg)
    cand = uuid.UUID(placement["candidate_id"])
    m = dorg.admin
    await m.client.post(f"{API}/bgv-cases/{case['id']}/start", headers=h(m))
    worker = await sign_in_candidate(app, cand, dorg.tenant_id)
    rc = (await m.client.post(f"{API}/candidates/{cand}/reference-checks", json={}, headers=h(m))).json()
    try:
        await consent_and_documents(app, dorg, case, worker)
        notice = (await worker.get(f"{PORTAL}/reference-checks")).json()[0]["notice_id"]
        r = await worker.post(
            f"{PORTAL}/reference-checks/{rc['id']}/referees",
            json={
                "referees": [{"name": "Meera Manager", "email": "meera2@ref.example.com"}],
                "agreed": True,
                "consent_notice_id": notice,
            },
            headers=CSRF,
        )
        assert r.status_code == 200, r.text
    finally:
        await worker.aclose()
    handler = BgvHandler()
    ctx = ErasureContext(tenant_id=dorg.tenant_id, candidate_id=cand, run_id=uuid.uuid4(), reason="dsr")
    async with tenant_session(dorg.tenant_id) as db:
        exported = await handler.export(db, ctx)
        assert exported and exported["bgv_cases"][0]["your_submissions"]
        counts = await handler.erase(db, ctx)
        assert counts["referees"] == 1 and counts["cases"] == 1
    async with tenant_session(dorg.tenant_id) as db:
        assert await handler.verify(db, ctx) == []
        ref = (
            await db.execute(select(Referee).where(Referee.reference_check_id == uuid.UUID(rc["id"])))
        ).scalar_one()
        assert ref.email == "[erased]" and ref.name == "[erased]"
        reqs = (
            (
                await db.execute(
                    select(BgvDocumentRequest).where(BgvDocumentRequest.case_id == uuid.UUID(case["id"]))
                )
            )
            .scalars()
            .all()
        )
        assert all(r.declared is None for r in reqs)
