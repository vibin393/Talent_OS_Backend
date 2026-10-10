"""Epic 17 A4-A7: releases, portal submissions, review / accept, vendor-facing progress (FR-94..FR-97)."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.database import tenant_session
from app.core.mailer import get_mailer
from app.models.pipeline import Application
from app.models.vendors import VendorCandidateOwnership, VendorSubmission
from tests.modules.demands.helpers import DOrg, h, open_demand
from tests.modules.vendors.conftest import (
    PORTAL,
    Setup,
    active_vendor,
    create_vendor,
    release,
    submit,
    transition,
    vendor_user,
)


async def _sub(org: DOrg, sub_id: str) -> VendorSubmission:
    async with tenant_session(org.tenant_id) as db:
        row = (
            await db.execute(select(VendorSubmission).where(VendorSubmission.id == uuid.UUID(sub_id)))
        ).scalar_one()
        db.expunge(row)
        return row


async def test_release_and_submit_happy_path(setup: Setup) -> None:
    # The vendor sees the release snapshot; the client is confidential.
    resp = await setup.user.client.get(f"{PORTAL}/releases")
    assert resp.status_code == 200, resp.text
    [rel] = resp.json()
    assert rel["client_name"] == "Confidential client" and rel["max_vendor_rate"] == "1350.0000"
    assert rel["remaining_slots"] == 3 and "bill_rate" not in rel
    resp = await submit(setup.user, setup.release["id"])
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "submitted" and body["label"] == "Received"
    assert "candidate_id" not in body and "margin" not in str(body)
    # After commit: the DPDP notice link was emailed to the candidate.
    sent = (
        [m for m in get_mailer().sent if m.kind == "vendor_sourced_notice"]
        if hasattr(get_mailer(), "sent")
        else []
    )
    sub = await _sub(setup.org, body["id"])
    assert sub.attested_at is not None and sub.resume_file_id is not None
    if hasattr(get_mailer(), "sent"):
        assert any(m.to == sub.email for m in sent)
    rel2 = (await setup.user.client.get(f"{PORTAL}/releases/{setup.release['id']}")).json()
    assert rel2["remaining_slots"] == 2


@pytest.mark.parametrize(
    ("fields", "status", "code"),
    [
        ({"vendor_rate": "1400"}, 422, "vendor_rate_above_max"),
        ({"attestation": "false"}, 422, "attestation_required"),
        ({"currency": "USD"}, 422, "rate_unit_mismatch"),
    ],
)
async def test_submit_validation(setup: Setup, fields: dict[str, Any], status: int, code: str) -> None:
    resp = await submit(setup.user, setup.release["id"], **fields)
    assert resp.status_code == status, resp.text
    assert resp.json()["error"]["code"] == code


async def test_deadline_and_withdrawn_release(app: FastAPI, setup: Setup) -> None:
    org = setup.org
    demand = await open_demand(org.admin, org.client_id)
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    rel = (await release(org, demand["id"], [setup.vendor["id"]], deadline_at=past))["items"][0]["release"]
    resp = await submit(setup.user, rel["id"])
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "deadline_passed"
    resp = await org.admin.client.post(
        f"/api/v1/vendor-releases/{setup.release['id']}/withdraw", headers=h(org.admin)
    )
    assert resp.status_code == 200 and resp.json()["status"] == "withdrawn"
    resp = await submit(setup.user, setup.release["id"])
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "release_closed"


async def test_limit_under_concurrency(app: FastAPI, setup: Setup) -> None:
    results = await asyncio.gather(*(submit(setup.user, setup.release["id"]) for _ in range(8)))
    codes = sorted(r.status_code for r in results)
    assert codes.count(201) == 3, [r.text for r in results if r.status_code not in (201, 409)]
    assert all(r.json()["error"]["code"] == "vendor_limit_reached" for r in results if r.status_code == 409)


async def test_withdraw_rules(setup: Setup) -> None:
    sub = (await submit(setup.user, setup.release["id"])).json()
    resp = await setup.user.client.post(f"{PORTAL}/submissions/{sub['id']}/withdraw", headers=setup.user.h())
    assert resp.status_code == 200 and resp.json()["status"] == "withdrawn"
    sub2 = (await submit(setup.user, setup.release["id"])).json()
    # The recruiter opens it -> under_review; the vendor can no longer withdraw.
    resp = await setup.org.admin.client.get(f"/api/v1/vendor-submissions/{sub2['id']}")
    assert resp.status_code == 200 and resp.json()["status"] == "under_review"
    resp = await setup.user.client.post(f"{PORTAL}/submissions/{sub2['id']}/withdraw", headers=setup.user.h())
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "already_in_review"


async def test_onboarding_vendor_is_read_only_and_permission_403(app: FastAPI, dorg: DOrg) -> None:
    v = await create_vendor(dorg)
    assert (await transition(dorg, v["id"], "onboarding")).status_code == 200
    ext = await vendor_user(app, dorg, v["id"])
    try:
        assert (await ext.client.get(f"{PORTAL}/dashboard")).json()["can_operate"] is False
        resp = await ext.client.post(
            f"{PORTAL}/documents",
            data={"category": "insurance"},
            files={"file": ("i.pdf", b"%PDF-1.4 x", "application/pdf")},
            headers=ext.h(),
        )
        assert resp.status_code == 409 and resp.json()["error"]["code"] == "vendor_not_active"
    finally:
        await ext.client.aclose()
    active = await active_vendor(dorg)
    ext2 = await vendor_user(app, dorg, active["id"], permissions=("releases:view",))
    try:
        resp = await ext2.client.get(f"{PORTAL}/statements")
        assert resp.status_code == 403
        details = resp.json()["error"]["details"]
        assert details["required_permission"] == "statements:view" and details["ask"] == "account_manager"
    finally:
        await ext2.client.aclose()


async def test_accept_creates_candidate_application_and_ownership(setup: Setup) -> None:
    sub = (await submit(setup.user, setup.release["id"])).json()
    org = setup.org
    detail = (await org.admin.client.get(f"/api/v1/vendor-submissions/{sub['id']}")).json()
    assert detail["duplicate_findings"] == [] and detail["margin"]["margin_amount"] == "200.00"
    resp = await org.admin.client.post(
        f"/api/v1/vendor-submissions/{sub['id']}/accept", json={"create": True}, headers=h(org.admin)
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["status"] == "accepted" and out["application_id"] and out["candidate_id"]
    async with tenant_session(org.tenant_id) as db:
        app_row = (
            await db.execute(select(Application).where(Application.id == uuid.UUID(out["application_id"])))
        ).scalar_one()
        assert app_row.source == "vendor" and app_row.source_ref == sub["id"]
        own = (await db.execute(select(VendorCandidateOwnership))).scalars().all()
        assert len(own) == 1 and (own[0].expires_at - own[0].starts_at).days == 90
    portal_view = (await setup.user.client.get(f"{PORTAL}/submissions/{sub['id']}")).json()
    assert portal_view["label"] == "Shortlisted" and "candidate_id" not in portal_view
    # Contract stub filled: sourcing for the application.
    from app.core.context import RequestContext
    from app.services import vendors_service

    async with tenant_session(org.tenant_id) as db:
        src = await vendors_service.sourcing_for_application(
            RequestContext(tenant_id=org.tenant_id), db, uuid.UUID(out["application_id"])
        )
    assert src is not None and str(src.vendor_id) == setup.vendor["id"]


async def test_second_vendor_same_person_is_duplicate_without_identity(app: FastAPI, setup: Setup) -> None:
    org = setup.org
    email = "dup.person@mail.example.com"
    first = (await submit(setup.user, setup.release["id"], email=email)).json()
    other = await active_vendor(org, "Other Staffing Co")
    rel = (await release(org, setup.demand["id"], [other["id"]]))["items"][0]["release"]
    ext = await vendor_user(app, org, other["id"])
    try:
        resp = await submit(ext, rel["id"], email=email)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["status"] == "duplicate" and body["outcome_category"] == "Already submitted"
        raw = resp.text
        assert "TalentBridge" not in raw and first["id"] not in raw and setup.vendor["id"] not in raw
        listing = (await ext.client.get(f"{PORTAL}/submissions")).text
        assert "TalentBridge" not in listing and first["id"] not in listing
    finally:
        await ext.client.aclose()


async def test_accept_is_human_only(setup: Setup) -> None:
    from app.core.context import RequestContext
    from app.core.errors import Forbidden
    from app.schemas.vendors import AcceptIn
    from app.services.vendors import review

    sub = (await submit(setup.user, setup.release["id"])).json()
    async with tenant_session(setup.org.tenant_id) as db:
        for kind in ("workflow", "ai", "system"):
            with pytest.raises(Forbidden):
                await review.accept(
                    RequestContext(tenant_id=setup.org.tenant_id, actor_kind=kind),
                    db,
                    uuid.UUID(sub["id"]),
                    AcceptIn(create=True),
                )


async def test_reject_shows_category_only_and_progress_follows_pipeline(setup: Setup) -> None:
    org = setup.org
    sub = (await submit(setup.user, setup.release["id"])).json()
    resp = await org.admin.client.post(
        f"/api/v1/vendor-submissions/{sub['id']}/reject",
        json={"reason_code": "skills_mismatch", "note": "Weak on Spark internals"},
        headers=h(org.admin),
    )
    assert resp.status_code == 200
    view = (await setup.user.client.get(f"{PORTAL}/submissions/{sub['id']}")).text
    assert "Weak on Spark" not in view and "Not a fit for this role" in view
    # Accepted submission: a pipeline stage change updates the vendor label.
    sub2 = (await submit(setup.user, setup.release["id"])).json()
    out = (
        await org.admin.client.post(
            f"/api/v1/vendor-submissions/{sub2['id']}/accept", json={"create": True}, headers=h(org.admin)
        )
    ).json()
    from app.core.context import RequestContext
    from app.services.vendors import progress

    async with tenant_session(org.tenant_id) as db:
        changed = await progress.on_stage_changed(
            RequestContext(tenant_id=org.tenant_id), db, uuid.UUID(out["application_id"]), "interviewing"
        )
    assert changed
    view2 = (await setup.user.client.get(f"{PORTAL}/submissions/{sub2['id']}")).json()
    assert view2["label"] == "Interviewing"
    assert [t["label"] for t in view2["timeline"]][-1] == "Interviewing"


async def test_revised_resume_lock(setup: Setup) -> None:
    from tests.epic3 import make_pdf

    sub = (await submit(setup.user, setup.release["id"])).json()
    files = {"resume": ("cv2.pdf", make_pdf("Updated resume"), "application/pdf")}
    resp = await setup.user.client.post(
        f"{PORTAL}/submissions/{sub['id']}/resume", files=files, headers=setup.user.h()
    )
    assert resp.status_code == 200, resp.text
    async with tenant_session(setup.org.tenant_id) as db:
        row = (
            await db.execute(
                select(VendorSubmission).where(VendorSubmission.id == uuid.UUID(sub["id"])).with_for_update()
            )
        ).scalar_one()
        row.progress_key = "submitted"
    resp = await setup.user.client.post(
        f"{PORTAL}/submissions/{sub['id']}/resume", files=files, headers=setup.user.h()
    )
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "resume_locked"


async def test_infected_resume_rejected(setup: Setup, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import scanning

    async def infected(data: bytes, name: str) -> Any:
        return scanning.ScanResult("infected", "Eicar-Test")

    monkeypatch.setattr(scanning, "scan", infected)
    resp = await submit(setup.user, setup.release["id"])
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "file_rejected"


async def test_notice_consent_withdrawal_auto_rejects(setup: Setup) -> None:
    from app.core import secure_links
    from app.services.vendors import notice

    sub = (await submit(setup.user, setup.release["id"])).json()
    # Issue a link with a known token (the emailed one is never stored raw).
    async with tenant_session(setup.org.tenant_id) as db:
        _link, token = await secure_links.issue(
            db,
            tenant_id=setup.org.tenant_id,
            purpose=notice.PURPOSE,
            subject_type=notice.SUBJECT,
            subject_id=uuid.UUID(sub["id"]),
            ttl=timedelta(days=1),
            max_uses=20,
        )
    from app.services import public_links_service

    view = await public_links_service.view_link(token)
    assert view.purpose == notice.PURPOSE and view.status == "pending"
    out = await public_links_service.respond_consent(token, False, None, ip_hash=None, user_agent=None)
    assert out.status == "withdrawn"
    row = await _sub(setup.org, sub["id"])
    assert row.status == "rejected" and row.reject_reason_code == "consent_withdrawn"


async def test_demand_leaving_open_closes_releases(setup: Setup) -> None:
    from app.core.context import RequestContext
    from app.services.vendors import releases

    async with tenant_session(setup.org.tenant_id) as db:
        n = await releases.close_for_demand(
            RequestContext(tenant_id=setup.org.tenant_id), db, uuid.UUID(setup.demand["id"])
        )
    assert n == 1
    resp = await submit(setup.user, setup.release["id"])
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "release_closed"


async def test_release_requires_open_demand_and_active_vendor(dorg: DOrg) -> None:
    from tests.modules.demands.helpers import create_demand

    draft = await create_demand(dorg.admin, dorg.client_id)
    v = await active_vendor(dorg)
    resp = await dorg.admin.client.post(
        f"/api/v1/demands/{draft['id']}/vendor-releases",
        json={"vendor_ids": [v["id"]]},
        headers=h(dorg.admin),
    )
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "demand_not_open"
    demand = await open_demand(dorg.admin, dorg.client_id)
    prospect = await create_vendor(dorg)
    out = await release(dorg, demand["id"], [v["id"], prospect["id"]])
    by = {i["vendor_id"]: i for i in out["items"]}
    assert by[v["id"]]["status"] == "released" and by[prospect["id"]]["reason"] == "vendor_not_active"
    again = await release(dorg, demand["id"], [v["id"]])
    assert again["items"][0]["reason"] == "already_released"
