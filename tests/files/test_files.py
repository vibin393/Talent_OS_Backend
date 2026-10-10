"""AD-13 files module: sniffing, size limit, scanning, storage keys, permission-checked downloads,
generic attachments (candidate registered)."""

import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core import scanning
from app.core.database import tenant_session
from app.core.scanning import ScanResult
from app.models.files import StoredFile
from app.services.files_service import sniff_kind
from tests.epic3 import (
    CANDIDATES,
    PNG_BYTES,
    Org3,
    create_candidate,
    h,
    make_docx,
    member,
    resume_pdf,
    upload,
)


def test_sniffing_ignores_names_and_uses_magic_bytes() -> None:
    assert sniff_kind(resume_pdf()) == "pdf"
    assert sniff_kind(make_docx()) == "docx"
    assert sniff_kind(PNG_BYTES) == "png"
    assert sniff_kind(b"\xff\xd8\xff\xe0jpeg") == "jpeg"
    assert sniff_kind(b"PK\x03\x04not really a zip") is None
    assert sniff_kind(b"hello") is None


async def _attachments_url(org: Org3) -> tuple[str, dict[str, object]]:
    cand = await create_candidate(org.admin)
    return f"{CANDIDATES}/{cand['id']}/attachments", cand


async def test_attachment_upload_list_download_archive(org3: Org3, _isolated_files: Path) -> None:
    url, cand = await _attachments_url(org3)
    pdf = resume_pdf()
    resp = await upload(
        org3.admin.client,
        url,
        pdf,
        "Certificate.pdf",
        category="certificate",
        expires_on="2027-01-31",
        note="AWS",
    )
    assert resp.status_code == 201, resp.text
    att = resp.json()
    assert att["category"] == "certificate"
    assert att["expires_on"] == "2027-01-31"
    assert att["file"]["scan_status"] == "skipped_dev"
    assert att["file"]["content_type"] == "application/pdf"

    async with tenant_session(org3.tenant_id) as db:
        row = (
            await db.execute(select(StoredFile).where(StoredFile.id == uuid.UUID(att["file"]["id"])))
        ).scalar_one()
    assert row.storage_key.startswith(f"t/{org3.tenant_id}/")
    assert (_isolated_files / row.storage_key).read_bytes() == pdf

    listed = await org3.admin.client.get(url)
    assert [a["id"] for a in listed.json()] == [att["id"]]

    download = await org3.admin.client.get(att["file"]["download_url"])
    assert download.status_code == 200
    assert download.content == pdf
    assert download.headers["content-disposition"].startswith("attachment;")
    assert download.headers["x-content-type-options"] == "nosniff"
    assert download.headers["content-security-policy"] == "sandbox"

    archived = await org3.admin.client.delete(f"/api/v1/attachments/{att['id']}", headers=h(org3.admin))
    assert archived.status_code == 200
    assert archived.json()["archived_at"] is not None
    assert (await org3.admin.client.get(url)).json() == []
    assert len((await org3.admin.client.get(url, params={"include_archived": True})).json()) == 1
    assert cand["id"]


async def test_png_attachment_and_bad_category(org3: Org3) -> None:
    url, _ = await _attachments_url(org3)
    ok = await upload(org3.admin.client, url, PNG_BYTES, "id.png", category="id_proof")
    assert ok.status_code == 201, ok.text
    assert ok.json()["file"]["content_type"] == "image/png"
    bad = await upload(org3.admin.client, url, PNG_BYTES, "id.png", category="selfie")
    assert bad.status_code == 422


async def test_wrong_type_is_rejected(org3: Org3) -> None:
    resp = await upload(org3.admin.client, f"{CANDIDATES}/resume-drafts", PNG_BYTES, "cv.pdf")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "unsupported_file_type"


async def test_too_large_is_rejected(org3: Org3) -> None:
    big = b"%PDF-1.4\n" + b"0" * (11 * 1024 * 1024)
    resp = await upload(org3.admin.client, f"{CANDIDATES}/resume-drafts", big, "big.pdf")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "file_too_large"


async def test_infected_file_is_not_stored(
    org3: Org3, monkeypatch: pytest.MonkeyPatch, _isolated_files: Path
) -> None:
    async def infected(data: bytes, filename: str) -> ScanResult:
        return ScanResult("infected", "Eicar")

    monkeypatch.setattr(scanning, "scan", infected)
    resp = await upload(org3.admin.client, f"{CANDIDATES}/resume-drafts", resume_pdf(), "cv.pdf")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "file_infected"
    assert not any(p.is_file() for p in _isolated_files.rglob("*"))
    async with tenant_session(org3.tenant_id) as db:
        assert (await db.execute(select(StoredFile))).first() is None


async def test_scanner_error_refuses_upload(org3: Org3, monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(data: bytes, filename: str) -> ScanResult:
        return ScanResult("error", "down")

    monkeypatch.setattr(scanning, "scan", broken)
    resp = await upload(org3.admin.client, f"{CANDIDATES}/resume-drafts", resume_pdf(), "cv.pdf")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "scan_unavailable"


async def test_download_requires_access_to_the_owner(app: FastAPI, org3: Org3, other3: Org3) -> None:
    interviewer = await member(app, org3, "interviewer")
    try:
        url, _ = await _attachments_url(org3)
        att = (await upload(org3.admin.client, url, resume_pdf(), "c.pdf", category="other")).json()
        dl = att["file"]["download_url"]
        assert (await interviewer.client.get(dl)).status_code == 404  # candidate not in own scope
        assert (await interviewer.client.get(url)).status_code == 404
        assert (await other3.admin.client.get(dl)).status_code == 404  # other tenant
        assert (await other3.admin.client.get(url)).status_code == 404
        cross = await upload(other3.admin.client, url, resume_pdf(), "c.pdf", category="other")
        assert cross.status_code == 404
        gone = await other3.admin.client.delete(f"/api/v1/attachments/{att['id']}", headers=h(other3.admin))
        assert gone.status_code == 404
        assert (await org3.admin.client.get(f"/api/v1/files/{uuid.uuid4()}/download")).status_code == 404
        assert (await org3.admin.client.get(f"/api/v1/widgets/{uuid.uuid4()}/attachments")).status_code == 404
    finally:
        await interviewer.client.aclose()


async def test_viewer_cannot_attach(app: FastAPI, org3: Org3) -> None:
    viewer = await member(app, org3, "viewer")
    try:
        url, _ = await _attachments_url(org3)
        resp = await upload(viewer.client, url, resume_pdf(), "c.pdf", category="other")
        assert resp.status_code == 403
        assert (await viewer.client.get(url)).status_code == 200
    finally:
        await viewer.client.aclose()
