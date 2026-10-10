"""Epic 46 (AD-26): Worker "My Work" immigration -- own cases only (cross-Worker 404 on every route), the
``immigration`` notice + consent before the first upload, uploads into P2 documents."""

import uuid
from typing import Any

from fastapi import FastAPI
from sqlalchemy import select

from app.core.database import tenant_session
from app.core.security import CSRF_HEADER
from app.models.documents import Document
from app.models.privacy import Consent
from tests.epic3 import make_pdf
from tests.modules.demands.helpers import DOrg, _org, h
from tests.modules.immigration.helpers import IMM, WORK, adopt, open_case, post
from tests.modules.legal_entities.conftest import provision
from tests.modules.placements.helpers import direct_placement
from tests.p2 import sign_in_candidate

CSRF = {CSRF_HEADER: "csrf-token"}
PDF = make_pdf("visa page")


async def _setup(app: FastAPI) -> tuple[DOrg, dict[str, Any], list[dict[str, Any]], Any]:
    org = await _org(app, "Ada Admin")
    await provision(org.tenant)
    vt = await adopt(org, "ae_employment")
    p = await direct_placement(org)
    case = await open_case(org, vt["id"], uuid.UUID(p["candidate_id"]))
    items = (await post(org, f"/cases/{case['id']}/checklist/generate")).json()
    worker_items = [i for i in items if i["collect_from"] == "worker"]
    r = await post(
        org, f"/cases/{case['id']}/requests/send", {"request_ids": [i["id"] for i in worker_items]}
    )
    assert r.status_code == 200, r.text
    worker = await sign_in_candidate(app, uuid.UUID(p["candidate_id"]), org.tenant_id)
    return org, case, worker_items, worker


async def test_worker_sees_own_case_consents_and_uploads(app: FastAPI) -> None:
    org, case, items, worker = await _setup(app)
    mine = await worker.get(WORK)
    assert mine.status_code == 200, mine.text
    body = mine.json()
    assert [c["reference"] for c in body["cases"]] == [case["human_id"]]
    assert body["cases"][0]["visa_type_name"] == "UAE employment visa"
    assert body["consent_given"] is False and body["notice"]["title"]
    assert body["cases"][0]["open_requests"] == len(items)
    keys = set(body["cases"][0])
    for forbidden in ("owner_user_id", "counsel_id", "receipt_number_last4", "outcome_note"):
        assert forbidden not in keys
    detail = await worker.get(f"{WORK}/{case['id']}")
    assert detail.status_code == 200 and len(detail.json()["requests"]) == len(items)
    first = items[0]["id"]
    blocked = await worker.post(
        f"{WORK}/requests/{first}/upload", files={"file": ("p.pdf", PDF, "application/pdf")}, headers=CSRF
    )
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "consent_required"
    ok = await worker.post(f"{WORK}/consent", headers=CSRF)
    assert ok.status_code == 200 and ok.json()["status"] == "granted"
    up = await worker.post(
        f"{WORK}/requests/{first}/upload", files={"file": ("p.pdf", PDF, "application/pdf")}, headers=CSRF
    )
    assert up.status_code == 200, up.text
    assert up.json()["status"] == "uploaded"
    async with tenant_session(org.tenant_id) as db:
        consent = (
            await db.execute(
                select(Consent).where(
                    Consent.candidate_id == uuid.UUID(case["candidate_id"]), Consent.purpose == "immigration"
                )
            )
        ).scalar_one()
        doc = (
            await db.execute(select(Document).where(Document.owner_kind == "immigration_case"))
        ).scalar_one()
    assert consent.source == "worker_portal"
    assert doc.retention_class == "immigration" and doc.access_level == "hr"
    reqs = (await org.admin.client.get(f"{IMM}/cases/{case['id']}/requests")).json()
    assert next(r for r in reqs if r["id"] == first)["status"] == "uploaded"
    await org.admin.client.aclose()
    await worker.aclose()


async def test_other_worker_gets_404_everywhere(app: FastAPI) -> None:
    org, case, items, worker = await _setup(app)
    other_p = await direct_placement(org)
    other = await sign_in_candidate(app, uuid.UUID(other_p["candidate_id"]), org.tenant_id)
    assert (await other.get(WORK)).json()["cases"] == []
    assert (await other.get(f"{WORK}/{case['id']}")).status_code == 404
    await other.post(f"{WORK}/consent", headers=CSRF)
    r = await other.post(
        f"{WORK}/requests/{items[0]['id']}/upload",
        files={"file": ("p.pdf", PDF, "application/pdf")},
        headers=CSRF,
    )
    assert r.status_code == 404
    assert (await other.get(f"{WORK}/{uuid.uuid4()}")).status_code == 404
    _ = h
    await org.admin.client.aclose()
    await worker.aclose()
    await other.aclose()
