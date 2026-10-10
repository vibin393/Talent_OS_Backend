"""P2-9.2: repository, access levels, supersede, audited download, P1 attachments facade and migration."""

import importlib.util
import uuid
from pathlib import Path
from types import ModuleType

from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.audit import AuditLog
from app.core.database import tenant_session
from app.models.documents import Document, DocumentVersion
from tests.epic3 import make_pdf, upload
from tests.modules.demands.helpers import DOrg, h
from tests.modules.documents.helpers import DOCS, candidate_with_email, member_with, upload_doc

MIGRATION = Path(__file__).resolve().parents[3] / "alembic" / "versions" / "20261003_209_03_doc_attach_mig.py"


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mig_209_03", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_upload_list_version_supersede_and_download_audit(dorg: DOrg) -> None:
    m = dorg.admin
    cid, _ = await candidate_with_email(dorg)
    doc = await upload_doc(dorg, cid, category="certificate", expires_on="2027-01-31")
    assert doc["human_id"].startswith("DOC-") and doc["current_version"]["version_no"] == 1
    v2 = await upload(m.client, f"{DOCS}/{doc['id']}/versions", make_pdf("v2"), "v2.pdf")
    assert v2.status_code == 201 and v2.json()["current_version"]["version_no"] == 2
    listed = (await m.client.get(DOCS, params={"owner_kind": "candidate", "owner_id": str(cid)})).json()
    assert [d["id"] for d in listed] == [doc["id"]] and listed[0]["expires_on"] == "2027-01-31"
    sup = await upload(m.client, f"{DOCS}/{doc['id']}/supersede", make_pdf("v3"), "v3.pdf")
    assert sup.status_code == 201
    old = (await m.client.get(f"{DOCS}/{doc['id']}")).json()
    assert old["superseded_by_id"] == sup.json()["id"] and len(old["versions"]) == 2
    url = sup.json()["current_version"]["download_url"]
    got = await m.client.get(url)
    assert got.status_code == 200 and got.headers["cache-control"] == "private, no-store"
    async with tenant_session(dorg.tenant_id) as db:
        audited = (await db.execute(select(AuditLog).where(AuditLog.action == "document.downloaded"))).scalars().all()
        assert len(audited) == 1
    archived = await m.client.delete(f"{DOCS}/{doc['id']}", headers=h(m))
    assert archived.status_code == 200 and archived.json()["archived_at"]


async def test_hr_documents_hidden_without_permission(app: FastAPI, dorg: DOrg) -> None:
    cid, _ = await candidate_with_email(dorg)
    public = await upload_doc(dorg, cid, category="other")
    hr = await upload_doc(dorg, cid, category="other", access_level="hr")
    reader = await member_with(app, dorg, "document:read", "document:write", "candidate:read", "candidate:update")
    try:
        listed = (await reader.client.get(DOCS, params={"owner_kind": "candidate", "owner_id": str(cid)})).json()
        assert [d["id"] for d in listed] == [public["id"]]
        assert (await reader.client.get(f"{DOCS}/{hr['id']}")).status_code == 404
        hr_url = hr["current_version"]["download_url"]
        assert (await reader.client.get(hr_url)).status_code == 404
        file_id = None
        async with tenant_session(dorg.tenant_id) as db:
            v = await db.get(DocumentVersion, uuid.UUID(hr["current_version"]["id"]))
            file_id = v.file_id if v else None
        assert (await reader.client.get(f"/api/v1/files/{file_id}/download")).status_code == 404
        raise_hr = await reader.client.patch(f"{DOCS}/{public['id']}", json={"access_level": "hr"}, headers=h(reader))
        assert raise_hr.status_code == 403
        attachments = (await reader.client.get(f"/api/v1/candidates/{cid}/attachments")).json()
        assert hr["id"] not in [a["id"] for a in attachments]
    finally:
        await reader.client.aclose()


async def test_p1_attachments_api_is_a_documents_facade(dorg: DOrg) -> None:
    m = dorg.admin
    cid, _ = await candidate_with_email(dorg)
    resp = await upload(
        m.client, f"/api/v1/candidates/{cid}/attachments", make_pdf("cert"), "cert.pdf", category="certificate",
        expires_on="2027-05-01", note="Original",
    )  # fmt: skip
    assert resp.status_code == 201, resp.text
    att = resp.json()
    assert set(att) == {"id", "entity_type", "entity_id", "category", "expires_on", "note", "archived_at", "uploaded_by", "created_at", "file"}
    assert att["file"]["download_url"] == f"/api/v1/files/{att['file']['id']}/download"
    docs = (await m.client.get(DOCS, params={"owner_kind": "candidate", "owner_id": str(cid)})).json()
    assert [(d["id"], d["category"], d["expires_on"]) for d in docs] == [(att["id"], "certificate", "2027-05-01")]
    listed = (await m.client.get(f"/api/v1/candidates/{cid}/attachments")).json()
    assert listed == [att]
    archived = await m.client.delete(f"/api/v1/attachments/{att['id']}", headers=h(m))
    assert archived.status_code == 200 and archived.json()["archived_at"]


async def test_attachment_migration_is_idempotent(dorg: DOrg, owner_db: AsyncEngine) -> None:
    m = dorg.admin
    cid, _ = await candidate_with_email(dorg)
    # A stored file to hang a legacy attachment on.
    doc = await upload_doc(dorg, cid, category="other")
    async with tenant_session(dorg.tenant_id) as db:
        v = await db.get(DocumentVersion, uuid.UUID(doc["current_version"]["id"]))
        assert v is not None
        file_id = v.file_id
    mig = _migration()
    legacy_id = uuid.uuid4()
    async with owner_db.connect() as conn:
        tx = await conn.begin()
        try:
            await conn.execute(text("ALTER TABLE attachments DISABLE TRIGGER tos_attachments_read_only"))
            await conn.execute(
                text(
                    "INSERT INTO attachments (id, tenant_id, entity_type, entity_id, file_id, category, expires_on, note) "
                    "VALUES (:id, :t, 'candidate', :c, :f, 'payslip', '2027-02-01', 'legacy')"
                ),
                {"id": legacy_id, "t": dorg.tenant_id, "c": cid, "f": file_id},
            )
            for _ in range(2):
                for sql in (mig.MIGRATE_DOCUMENTS, mig.BUMP_SEQUENCES, mig.MIGRATE_VERSIONS):
                    await conn.execute(text(sql))
                for stmt in mig.LINK:
                    await conn.execute(text(stmt))
            rows = (await conn.execute(text("SELECT category, retention_class, expires_on FROM documents WHERE id = :id"), {"id": legacy_id})).all()
            assert len(rows) == 1 and rows[0].category == "payslip" and rows[0].retention_class == "financial"
            versions = (await conn.execute(text("SELECT source FROM document_versions WHERE document_id = :id"), {"id": legacy_id})).all()
            assert [r.source for r in versions] == ["migrated"]
            linked = (await conn.execute(text("SELECT migrated_document_id FROM attachments WHERE id = :id"), {"id": legacy_id})).scalar()
            assert linked == legacy_id
            await conn.execute(text("ALTER TABLE attachments ENABLE TRIGGER tos_attachments_read_only"))
        finally:
            await tx.rollback()
    del m


async def test_attachments_table_is_read_only(dorg: DOrg, owner_db: AsyncEngine) -> None:
    import pytest
    from sqlalchemy.exc import DBAPIError

    async with owner_db.connect() as conn:
        tx = await conn.begin()
        try:
            with pytest.raises(DBAPIError, match="document_immutable"):
                await conn.execute(
                    text(
                        "INSERT INTO attachments (id, tenant_id, entity_type, entity_id, file_id, category) "
                        "VALUES (gen_random_uuid(), :t, 'candidate', gen_random_uuid(), gen_random_uuid(), 'other')"
                    ),
                    {"t": dorg.tenant_id},
                )
        finally:
            await tx.rollback()
    async with tenant_session(dorg.tenant_id) as db:
        assert (await db.execute(select(Document))).scalars().all() == []
