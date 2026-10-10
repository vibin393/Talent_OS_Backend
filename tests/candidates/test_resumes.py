"""Stories 3.3 / 3.4 without AI: upload, extraction statuses, drafts, versions, download."""

import io
import uuid
from pathlib import Path

from fastapi import FastAPI
from pypdf import PdfReader, PdfWriter
from sqlalchemy import select

from app.core.database import tenant_session
from app.models.ai import AIAction
from app.models.files import StoredFile
from tests.epic3 import CANDIDATES, Org3, create_candidate, h, make_docx, make_pdf, member, resume_pdf, upload

DRAFTS = f"{CANDIDATES}/resume-drafts"


def _encrypted_pdf() -> bytes:
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(resume_pdf())))
    writer.encrypt(user_password="secret", owner_password="owner")
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


async def test_draft_without_ai_key_then_create(org3: Org3, _isolated_files: Path) -> None:
    resp = await upload(org3.admin.client, DRAFTS, resume_pdf(), "Priya CV.pdf")
    assert resp.status_code == 201, resp.text
    draft = resp.json()
    assert draft["parse_status"] == "unavailable"
    assert draft["resume"]["parse_label"] == "AI unavailable – fill manually"  # noqa: RUF001
    assert draft["resume"]["text_status"] == "ok"
    assert draft["resume"]["candidate_id"] is None
    assert draft["resume"]["file"]["scan_status"] == "skipped_dev"
    assert draft["prefill"]["emails"] == []
    async with tenant_session(org3.tenant_id) as db:
        (action,) = (await db.execute(select(AIAction))).scalars()
        stored = (await db.execute(select(StoredFile))).scalar_one()
    assert (action.status, action.error_code) == ("skipped", "not_configured")
    assert stored.storage_key.startswith(f"t/{org3.tenant_id}/")
    assert (_isolated_files / stored.storage_key).is_file()

    created = await create_candidate(org3.admin, full_name="Priya Raman", resume_id=draft["resume"]["id"])
    assert created["source"] == "resume_upload"
    assert [(r["id"], r["version"]) for r in created["resumes"]] == [(draft["resume"]["id"], 1)]
    listed = await org3.admin.client.get(f"{CANDIDATES}/{created['id']}/resumes")
    assert [r["version"] for r in listed.json()] == [1]
    dl = await org3.admin.client.get(created["resumes"][0]["file"]["download_url"])
    assert dl.status_code == 200
    assert dl.content.startswith(b"%PDF-")

    # The draft is consumed: reusing it fails.
    again = await org3.admin.client.post(
        CANDIDATES, json={"full_name": "X", "resume_id": draft["resume"]["id"]}, headers=h(org3.admin)
    )
    assert again.status_code == 404


async def test_image_only_pdf_is_manual_entry(org3: Org3) -> None:
    resp = await upload(org3.admin.client, DRAFTS, make_pdf(None), "scan.pdf")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["resume"]["text_status"] == "image_only"
    assert body["parse_status"] == "failed_manual"
    assert body["resume"]["parse_label"] == "Parse failed – manual entry"  # noqa: RUF001
    async with tenant_session(org3.tenant_id) as db:
        assert (await db.execute(select(AIAction))).first() is None  # no AI call for unreadable text


async def test_encrypted_pdf_is_manual_entry(org3: Org3) -> None:
    body = (await upload(org3.admin.client, DRAFTS, _encrypted_pdf(), "locked.pdf")).json()
    assert (body["resume"]["text_status"], body["parse_status"]) == ("encrypted", "failed_manual")


async def test_docx_resume(org3: Org3) -> None:
    body = (await upload(org3.admin.client, DRAFTS, make_docx(), "cv.docx")).json()
    assert body["resume"]["text_status"] == "ok"
    assert body["resume"]["file"]["content_type"].endswith("wordprocessingml.document")


async def test_drafts_are_private_to_the_uploader(app: FastAPI, org3: Org3) -> None:
    draft = (await upload(org3.admin.client, DRAFTS, resume_pdf(), "cv.pdf")).json()
    recruiter = await member(app, org3, "recruiter")
    try:
        resp = await recruiter.client.post(
            CANDIDATES, json={"full_name": "Taken", "resume_id": draft["resume"]["id"]}, headers=h(recruiter)
        )
        assert resp.status_code == 404
        dl = await recruiter.client.get(draft["resume"]["file"]["download_url"])
        assert dl.status_code == 404
    finally:
        await recruiter.client.aclose()


async def test_new_versions_for_existing_candidate(org3: Org3) -> None:
    cand = await create_candidate(org3.admin)
    url = f"{CANDIDATES}/{cand['id']}/resumes"
    first = await upload(org3.admin.client, url, resume_pdf(), "v1.pdf")
    second = await upload(org3.admin.client, url, make_docx(), "v2.docx")
    assert first.status_code == 201, first.text
    assert (first.json()["resume"]["version"], second.json()["resume"]["version"]) == (1, 2)
    assert second.json()["diff"] == []  # nothing parsed without AI
    listed = (await org3.admin.client.get(url)).json()
    assert [r["version"] for r in listed] == [2, 1]
    apply = await org3.admin.client.post(
        f"{url}/{first.json()['resume']['id']}/apply",
        json={"fields": ["current_title"]},
        headers=h(org3.admin),
    )
    assert apply.status_code == 409
    assert apply.json()["error"]["code"] == "resume_not_parsed"
    missing = await org3.admin.client.post(f"{url}/{uuid.uuid4()}/apply", json={}, headers=h(org3.admin))
    assert missing.status_code == 404
