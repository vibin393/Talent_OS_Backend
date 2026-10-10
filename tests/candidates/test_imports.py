"""Story 3.6: bulk import (files or ZIP), limits, per-item outcomes, idempotent task, batch completion.

Celery runs eagerly in tests (CELERY_TASK_ALWAYS_EAGER=true): items are processed right after the upload
request commits."""

import io
import uuid
import zipfile
from typing import Any

import httpx
from sqlalchemy import func, select

from app.core.database import tenant_session
from app.models.candidates import Candidate
from app.tasks.candidates import process_import_item
from tests.epic3 import CANDIDATES, FakeAI, Org3, create_candidate, h, make_zip, parsed_payload, resume_pdf

IMPORTS = f"{CANDIDATES}/imports"


def _payload_for(content: str) -> dict[str, Any]:
    if "NEWPERSON" in content:
        return parsed_payload(full_name="Nisha New", emails=["nisha.new@example.com"], phones=[], links=[])
    if "EXISTING" in content:
        return parsed_payload(full_name="Eve Existing", emails=["eve@example.com"], phones=[], links=[])
    if "SIMILAR" in content:
        return parsed_payload(
            full_name="Rahul Sharmaa",
            emails=["other.rahul@example.com"],
            phones=[],
            links=[],
            current_employer="Infosys",
        )
    return parsed_payload()


def _pdf(tag: str) -> bytes:
    return resume_pdf(
        f"{tag}\nSome resume text with enough characters to pass the image-only threshold easily."
    )


CORRUPT = b"%PDF-1.4\nthis is not really a pdf at all"


async def _post_files(
    client: httpx.AsyncClient, files: list[tuple[str, bytes]], field: str = "files"
) -> httpx.Response:
    return await client.post(
        IMPORTS,
        files=[(field, (name, data, "application/octet-stream")) for name, data in files],
        headers={"X-CSRF-Token": client.cookies.get("tos_csrf") or ""},
    )


async def _batch(org: Org3, batch_id: str) -> dict[str, Any]:
    resp = await org.admin.client.get(f"{IMPORTS}/{batch_id}", params={"page_size": 100})
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


async def test_three_files_created_linked_failed(org3: Org3, fake_ai: FakeAI) -> None:
    fake_ai.payload_for = _payload_for
    existing = await create_candidate(
        org3.admin, full_name="Eve Existing", emails=[{"email": "eve@example.com"}]
    )
    resp = await _post_files(
        org3.admin.client,
        [("new.pdf", _pdf("NEWPERSON")), ("existing.pdf", _pdf("EXISTING")), ("corrupt.pdf", CORRUPT)],
    )
    assert resp.status_code == 201, resp.text
    batch = await _batch(org3, resp.json()["id"])
    assert batch["status"] == "completed"
    assert batch["completed_at"] is not None
    assert batch["counts"] == {"created": 1, "linked": 1, "failed": 1}
    by_name = {i["filename"]: i for i in batch["items"]["items"]}
    assert by_name["existing.pdf"]["status"] == "linked"
    assert by_name["existing.pdf"]["candidate_id"] == existing["id"]
    assert by_name["corrupt.pdf"]["status"] == "failed"
    assert by_name["corrupt.pdf"]["reason"] == "failed"
    assert by_name["corrupt.pdf"]["resume_id"] is not None  # kept for "create manually"
    created = (await org3.admin.client.get(f"{CANDIDATES}/{by_name['new.pdf']['candidate_id']}")).json()
    assert created["source"] == "bulk_import"
    assert created["full_name"] == "Nisha New"
    assert [r["version"] for r in created["resumes"]] == [1]
    eve = (await org3.admin.client.get(f"{CANDIDATES}/{existing['id']}")).json()
    assert [r["version"] for r in eve["resumes"]] == [1]

    listed = (await org3.admin.client.get(IMPORTS)).json()
    assert [b["id"] for b in listed["items"]] == [batch["id"]]

    # Retrying a finished item is a no-op (idempotent task).
    async with tenant_session(org3.tenant_id) as db:
        before = (await db.execute(select(func.count()).select_from(Candidate))).scalar_one()
    assert process_import_item(str(org3.tenant_id), by_name["new.pdf"]["id"]) == "created"
    async with tenant_session(org3.tenant_id) as db:
        assert (await db.execute(select(func.count()).select_from(Candidate))).scalar_one() == before


async def test_zip_with_possible_duplicate_needs_review(org3: Org3, fake_ai: FakeAI) -> None:
    fake_ai.payload_for = _payload_for
    await create_candidate(org3.admin, full_name="Rahul Sharma", current_employer="Infosys")
    archive = make_zip(
        {
            "resumes/similar.pdf": _pdf("SIMILAR"),
            "__MACOSX/resumes/._similar.pdf": b"junk",
            ".hidden.pdf": b"junk",
            "resumes/": b"",
            "notes.txt": b"plain text is not a resume",
        }
    )
    resp = await _post_files(org3.admin.client, [("batch.zip", archive)], field="files[]")
    assert resp.status_code == 201, resp.text
    batch = await _batch(org3, resp.json()["id"])
    assert batch["total"] == 2
    by_name = {i["filename"]: i for i in batch["items"]["items"]}
    assert by_name["similar.pdf"]["status"] == "needs_review"
    assert by_name["similar.pdf"]["duplicate_id"] is not None
    assert by_name["notes.txt"]["status"] == "failed"
    assert by_name["notes.txt"]["reason"] == "unsupported_file_type"
    assert batch["status"] == "completed"


async def test_without_ai_items_fail_with_parse_unavailable(org3: Org3) -> None:
    resp = await _post_files(org3.admin.client, [("a.pdf", _pdf("NEWPERSON"))])
    batch = await _batch(org3, resp.json()["id"])
    (item,) = batch["items"]["items"]
    assert (item["status"], item["reason"]) == ("failed", "parse_unavailable")


async def test_limits(org3: Org3) -> None:
    too_many = await _post_files(org3.admin.client, [(f"{i}.pdf", b"%PDF-1.4") for i in range(201)])
    assert too_many.status_code == 422
    assert too_many.json()["error"]["code"] == "import_too_large"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bomb.pdf", b"%PDF-" + b"0" * (5 * 1024 * 1024))  # compresses far beyond 100:1
    bomb = await _post_files(org3.admin.client, [("bomb.zip", buf.getvalue())])
    assert bomb.status_code == 422
    assert bomb.json()["error"]["code"] == "import_too_large"

    many = make_zip({f"r{i}.pdf": uuid.uuid4().bytes * 4 for i in range(201)}, compression=zipfile.ZIP_STORED)
    over = await _post_files(org3.admin.client, [("many.zip", many)])
    assert over.status_code == 422
    assert over.json()["error"]["code"] == "import_too_large"

    empty = await org3.admin.client.post(IMPORTS, headers=h(org3.admin))
    assert empty.status_code == 422
