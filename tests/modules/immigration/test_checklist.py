"""Epic 46 story 46.3 (FR-234): checklist generation (per dependant), send (worker notification / counsel Secure
Link), uploads into P2 documents (retention class ``immigration``), verify / reject / waive, counsel link rules."""

import re
import uuid
from typing import Any

from sqlalchemy import select, text

from app.core.database import tenant_session
from app.core.settings import get_settings
from app.models.documents import Document
from tests.epic12 import sent
from tests.modules.demands.helpers import DOrg, h
from tests.modules.immigration.helpers import IMM, PDF, adopt, open_case, post
from tests.modules.placements.helpers import direct_placement

PUBLIC = "/api/v1/public/links"


async def _counsel(org: DOrg) -> str:
    r = await post(
        org, "/counsel", {"name": "Fragomen Desk", "firm": "Fragomen", "email": "desk@counsel.example.com"}
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def _case_with_items(
    org: DOrg, *, worker: bool = True, key: str = "uk_skilled_worker"
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    vt = await adopt(org, key)
    p = await direct_placement(org) if worker else None
    cand = uuid.UUID(p["candidate_id"]) if p else None
    case = await open_case(org, vt["id"], cand, counsel_id=await _counsel(org))
    dep = await post(org, f"/cases/{case['id']}/dependants", {"name": "Kiran Rao", "relationship": "child"})
    assert dep.status_code == 201
    items = (await post(org, f"/cases/{case['id']}/checklist/generate")).json()
    return case, items


async def test_generate_per_dependant_and_idempotent(dorg: DOrg) -> None:
    case, items = await _case_with_items(dorg)
    keys = [i["checklist_key"] for i in items]
    assert keys.count("dependant_passport") == 1
    dep_item = next(i for i in items if i["checklist_key"] == "dependant_passport")
    assert dep_item["dependant_name"] == "Kiran Rao" and "Kiran Rao" in dep_item["label"]
    again = (await post(dorg, f"/cases/{case['id']}/checklist/generate")).json()
    assert len(again) == len(items)


async def test_internal_upload_verify_reject_waive(dorg: DOrg) -> None:
    case, items = await _case_with_items(dorg)
    passport = next(i for i in items if i["checklist_key"] == "passport")
    bad = await dorg.admin.client.post(
        f"{IMM}/requests/{passport['id']}/upload",
        files={"file": ("x.txt", b"plain text", "text/plain")},
        headers=h(dorg.admin),
    )
    assert bad.status_code == 422
    up = await dorg.admin.client.post(
        f"{IMM}/requests/{passport['id']}/upload",
        files={"file": ("passport.pdf", PDF, "application/pdf")},
        headers=h(dorg.admin),
    )
    assert up.status_code == 200, up.text
    assert up.json()["status"] == "uploaded"
    async with tenant_session(dorg.tenant_id) as db:
        doc = await db.get(Document, uuid.UUID(up.json()["document_id"]))
    assert doc is not None
    assert (doc.owner_kind, doc.access_level, doc.retention_class) == (
        "immigration_case",
        "hr",
        "immigration",
    )
    assert str(doc.owner_id) == case["id"] and str(doc.worker_candidate_id) == case["candidate_id"]
    v = await post(dorg, f"/requests/{passport['id']}/verify")
    assert v.status_code == 200 and v.json()["request"]["status"] == "verified"
    assert any("6 months" in w for w in v.json()["warnings"])
    tb = next(i for i in items if i["checklist_key"] == "tb_certificate")
    await dorg.admin.client.post(
        f"{IMM}/requests/{tb['id']}/upload",
        files={"file": ("tb.pdf", PDF, "application/pdf")},
        headers=h(dorg.admin),
    )
    rj = await post(dorg, f"/requests/{tb['id']}/reject", {"reason": "Clinic not approved"})
    assert rj.status_code == 200 and rj.json()["status"] == "rejected"
    cos = next(i for i in items if i["checklist_key"] == "cos_reference")
    wv = await post(dorg, f"/requests/{cos['id']}/waive", {"reason": "Sponsor holds it"})
    assert wv.json()["status"] == "waived"
    kinds = [e["kind"] for e in (await dorg.admin.client.get(f"{IMM}/cases/{case['id']}/timeline")).json()]
    assert {"document_uploaded", "document_verified", "document_rejected"} <= set(kinds)


def _token(to: str) -> str:
    msgs = sent(kind="immigration_upload", to=to)
    assert msgs, "no upload link email"
    m = re.search(r"/l/([A-Za-z0-9_\-]+)", msgs[-1].text)
    assert m
    return m.group(1)


def _origin() -> dict[str, str]:
    return {"Origin": get_settings().frontend_url.rstrip("/")}


async def test_counsel_secure_link_flow(dorg: DOrg) -> None:
    case, items = await _case_with_items(dorg, key="us_h1b")
    counsel_items = [i["id"] for i in items if i["collect_from"] == "counsel"]
    assert len(counsel_items) == 2  # credential evaluation + DS-160
    out = await post(dorg, f"/cases/{case['id']}/requests/send", {"request_ids": counsel_items})
    assert out.status_code == 200 and out.json()["counsel_links"] == 1
    token = _token("desk@counsel.example.com")
    anon = dorg.admin.client
    view = await anon.get(f"{PUBLIC}/{token}/immigration-upload")
    assert view.status_code == 200, view.text
    assert view.json()["reference"] == case["human_id"] and len(view.json()["items"]) == len(counsel_items)
    assert view.json()["notice_title"]
    frame = await anon.get(f"{PUBLIC}/{token}")
    assert frame.status_code == 200 and frame.json()["purpose"] == "immigration_upload"
    wrong = await anon.post(
        f"{PUBLIC}/{token}/immigration-upload",
        data={"item_id": str(uuid.uuid4()), "consent": "true"},
        files={"file": ("a.pdf", PDF, "application/pdf")},
    )
    assert wrong.status_code == 404
    no_consent = await anon.post(
        f"{PUBLIC}/{token}/immigration-upload",
        data={"item_id": counsel_items[0]},
        files={"file": ("a.pdf", PDF, "application/pdf")},
    )
    assert no_consent.status_code == 422
    for item in counsel_items:
        ok = await anon.post(
            f"{PUBLIC}/{token}/immigration-upload",
            data={"item_id": item, "consent": "true"},
            files={"file": ("a.pdf", PDF, "application/pdf")},
        )
        assert ok.status_code == 200, ok.text
    used = await anon.post(
        f"{PUBLIC}/{token}/immigration-upload",
        data={"item_id": counsel_items[0], "consent": "true"},
        files={"file": ("a.pdf", PDF, "application/pdf")},
    )
    assert used.status_code == 410
    reqs = (await dorg.admin.client.get(f"{IMM}/cases/{case['id']}/requests")).json()
    assert {r["status"] for r in reqs if r["id"] in counsel_items} == {"uploaded"}


async def test_expired_counsel_link_410(dorg: DOrg) -> None:
    case, items = await _case_with_items(dorg, key="us_h1b")
    counsel_items = [i["id"] for i in items if i["collect_from"] == "counsel"]
    await post(dorg, f"/cases/{case['id']}/requests/send", {"request_ids": counsel_items})
    token = _token("desk@counsel.example.com")
    async with tenant_session(dorg.tenant_id) as db:
        await db.execute(
            text("UPDATE secure_links SET expires_at = now() - interval '1 day' WHERE subject_id = :c"),
            {"c": uuid.UUID(case["id"])},
        )
    r = await dorg.admin.client.post(
        f"{PUBLIC}/{token}/immigration-upload",
        data={"item_id": counsel_items[0], "consent": "true"},
        files={"file": ("a.pdf", PDF, "application/pdf")},
    )
    assert r.status_code == 410 and r.json()["error"]["code"] == "link_expired"


async def test_worker_items_notify_worker(dorg: DOrg) -> None:
    case, items = await _case_with_items(dorg)
    worker_items = [i["id"] for i in items if i["collect_from"] == "worker"]
    out = await post(dorg, f"/cases/{case['id']}/requests/send", {"request_ids": worker_items})
    assert out.status_code == 200 and out.json()["notified_worker"] == len(worker_items)
    async with tenant_session(dorg.tenant_id) as db:
        rows = (
            await db.execute(text("SELECT type FROM notifications WHERE type = 'immigration_case_step'"))
        ).all()
    assert rows, "the worker / owner got an immigration_case_step notification"


async def test_non_worker_gets_secure_link(dorg: DOrg) -> None:
    case, items = await _case_with_items(dorg, worker=False)
    worker_items = [i["id"] for i in items if i["collect_from"] == "worker"][:2]
    out = await post(dorg, f"/cases/{case['id']}/requests/send", {"request_ids": worker_items})
    assert out.status_code == 200 and out.json()["worker_links"] == 1
    async with tenant_session(dorg.tenant_id) as db:
        doc_count = (
            await db.execute(select(Document.id).where(Document.owner_kind == "immigration_case"))
        ).all()
    assert doc_count == []
