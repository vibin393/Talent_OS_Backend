"""Story 4.4: AI intake from pasted text and PDF/DOCX files, file rules, AI down."""

from typing import Any

from sqlalchemy import select

from app.core.database import tenant_session
from app.models.ai import AIAction
from tests.epic3 import FakeAI, make_docx, make_pdf
from tests.modules.demands.helpers import DEMANDS, DOrg, h

INTAKE = f"{DEMANDS}/intake"

EXTRACTION: dict[str, Any] = {
    "title": "Senior Data Engineer",
    "mandatory_skills": ["PySpark", "Azure Data Factory"],
    "preferred_skills": ["Databricks"],
    "min_experience_years": 5,
    "max_experience_years": 8,
    "positions": 2,
    "location": "Chennai",
    "country": "in",
    "work_mode": "Hybrid",
    "bill_rate": 1800,
    "currency": "INR",
    "rate_unit": "hourly",
    "start_date": "2026-11-01",
    "employment_type": "contract",
    "certifications": [],
}


async def _post(org: DOrg, **kwargs: Any) -> Any:
    m = org.admin
    data = {"client_id": str(org.client_id), **kwargs.pop("data", {})}
    return await m.client.post(INTAKE, data=data, headers=h(m), **kwargs)


async def test_text_intake_creates_draft_with_ai_fields(dorg: DOrg, fake_ai: FakeAI) -> None:
    fake_ai.payload = {**EXTRACTION, "work_mode": "teleport", "pay_rate_min": 900, "pay_rate_max": 100}
    resp = await _post(dorg, data={"text": "Need 2 senior data engineers in Chennai..."})
    assert resp.status_code == 201, resp.text
    d = resp.json()
    assert d["status"] == "draft"
    assert d["title"] == "Senior Data Engineer"
    assert d["country"] == "IN"
    assert d["rate_unit"] == "hour"
    assert d["work_mode"] is None  # invalid enum dropped, not raised
    assert d["pay_rate_min"] is None and d["pay_rate_max"] is None  # inverted range dropped
    assert d["intake_ai_action_id"] is not None
    assert "bill_rate" in d["ai_extracted_fields"]
    assert "work_mode" not in d["ai_extracted_fields"]
    assert {s["label"] for s in d["skills"]} >= {"PySpark", "Databricks"}
    docs = (await dorg.admin.client.get(f"{DEMANDS}/{d['id']}/documents")).json()
    assert [(x["kind"], x["file_name"], x["scan_status"]) for x in docs] == [
        ("intake_source", "intake.txt", "skipped_dev")
    ]
    async with tenant_session(dorg.tenant_id) as db:
        action = await db.get(AIAction, d["intake_ai_action_id"])
        assert action is not None
        assert (action.feature, action.subject_type, str(action.subject_id)) == (
            "demand_intake",
            "demand",
            d["id"],
        )

    # Submitting needs confirmation of the AI fields.
    m = dorg.admin
    await m.client.patch(f"{DEMANDS}/{d['id']}", json={"work_mode": "hybrid"}, headers=h(m))
    resp = await m.client.post(f"{DEMANDS}/{d['id']}/submit", headers=h(m))
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "unconfirmed_ai_fields")
    # Editing a field clears its badge.
    resp = await m.client.patch(f"{DEMANDS}/{d['id']}", json={"bill_rate": "1700.00"}, headers=h(m))
    assert "bill_rate" not in resp.json()["ai_extracted_fields"]
    resp = await m.client.post(
        f"{DEMANDS}/{d['id']}/confirm-ai-fields", json={"fields": ["nope"]}, headers=h(m)
    )
    assert resp.status_code == 422
    resp = await m.client.post(f"{DEMANDS}/{d['id']}/confirm-ai-fields", json={}, headers=h(m))
    assert resp.status_code == 200
    assert resp.json()["ai_extracted_fields"] == []
    resp = await m.client.post(f"{DEMANDS}/{d['id']}/submit", headers=h(m))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "open"
    async with tenant_session(dorg.tenant_id) as db:
        action = await db.get(AIAction, d["intake_ai_action_id"])
        assert action is not None and action.decision == "edited"


async def test_pdf_and_docx_intake(dorg: DOrg, fake_ai: FakeAI) -> None:
    fake_ai.payload = EXTRACTION
    pdf = make_pdf(
        "Senior Data Engineer\nChennai, hybrid\nPySpark and Azure Data Factory required\n5-8 years\n"
    )
    resp = await _post(dorg, files={"file": ("sow.pdf", pdf, "application/pdf")})
    assert resp.status_code == 201, resp.text
    docs = (await dorg.admin.client.get(f"{DEMANDS}/{resp.json()['id']}/documents")).json()
    assert docs[0]["file_id"] is not None and docs[0]["content_type"] == "application/pdf"
    assert "PySpark" in fake_ai.calls[0]["messages"][0]["content"]

    resp = await _post(
        dorg, files={"file": ("jd.docx", make_docx("Data Engineer\nPySpark\n"), "application/octet-stream")}
    )
    assert resp.status_code == 201, resp.text


async def test_file_rules(dorg: DOrg, fake_ai: FakeAI) -> None:
    fake_ai.payload = EXTRACTION
    exe = b"MZ\x90\x00" + b"\x00" * 200
    resp = await _post(dorg, files={"file": ("sow.pdf", exe, "application/pdf")})
    assert (resp.status_code, resp.json()["error"]["code"]) == (422, "unsupported_file_type")
    big = b"%PDF-1.4\n" + b"0" * (10 * 1024 * 1024 + 10)
    resp = await _post(dorg, files={"file": ("big.pdf", big, "application/pdf")})
    assert (resp.status_code, resp.json()["error"]["code"]) == (422, "file_too_large")
    resp = await _post(dorg, data={"text": "x" * 60_001})
    assert (resp.status_code, resp.json()["error"]["code"]) == (422, "document_too_long")
    resp = await _post(dorg)
    assert (resp.status_code, resp.json()["error"]["code"]) == (422, "text_or_file_required")
    resp = await _post(
        dorg, data={"text": "hello"}, files={"file": ("a.pdf", make_pdf("x" * 80), "application/pdf")}
    )
    assert (resp.status_code, resp.json()["error"]["code"]) == (422, "text_or_file_required")
    assert fake_ai.calls == []


async def test_ai_down_falls_back(dorg: DOrg) -> None:
    resp = await _post(dorg, data={"text": "Need a Java developer"})
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "ai_unavailable")
    listed = (await dorg.admin.client.get(DEMANDS)).json()
    assert listed["total"] == 0


async def test_intake_requires_permission(app: Any, dorg: DOrg, fake_ai: FakeAI) -> None:
    from tests.modules.identity.conftest import join

    recruiter = await join(app, dorg.tenant, "recruiter", name="Rita Recruiter")
    try:
        resp = await recruiter.client.post(
            INTAKE, data={"client_id": str(dorg.client_id), "text": "x"}, headers=h(recruiter)
        )
        assert resp.status_code == 403
        async with tenant_session(dorg.tenant_id) as db:
            assert (await db.execute(select(AIAction))).first() is None
    finally:
        await recruiter.client.aclose()
