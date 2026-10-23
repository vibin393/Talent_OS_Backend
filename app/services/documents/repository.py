"""Document repository (Epic 22 A4, P2-9.2, FR-120): documents, versions, access levels, downloads.

* Bytes only via ``files_service.store_upload`` / ``store_generated`` (size, sniffing, malware scan). Every file
  stored here is owned by ``("document", <id>)`` so the generic ``/files/{id}/download`` re-runs the document
  rule (registered as the ``document`` access checker).
* Access = the owning record's checker (``files_service.check_access``; a missing checker denies) + the access
  level (``hr`` needs ``document:hr``, ``finance`` ``document:finance``). A forbidden document is omitted from
  lists and is a 404 on direct access -- in lists, API, downloads and exports alike.
* A signed document never gets a new version (409 ``document_signed``): ``supersede`` creates a new document
  and links ``superseded_by_id``.
* P1 attachments: ``AttachmentFacade`` serves the P1 ``/{entity}/{id}/attachments`` routes from documents (the
  migrated documents keep the attachment ids).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable  # noqa: TC003
from datetime import date  # noqa: TC003
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import ValidationFailed
from app.core.permissions import forbidden
from app.core.sequences import next_human_id
from app.models.documents import ACCESS_LEVELS, Document, DocumentVersion
from app.models.files import ATTACHMENT_CATEGORIES, Attachment, StoredFile
from app.schemas.documents import DocumentOut, DocumentPatchIn, DocumentVersionOut
from app.schemas.files import AttachmentOut
from app.services import files_service
from app.services.documents.common import DOCUMENT_HR, conflict, has, level_allowed, not_found, now, user_id
from app.services.documents.common import tenant_of as tenant_of

UPLOAD_KINDS = files_service.ATTACHMENT_KINDS
FILE_OWNER = "document"
RETENTION_BY_CATEGORY = {
    "payslip": "financial",
    "offer_letter": "signed_agreements",
    "relieving_letter": "signed_agreements",
}


# ================================================================ access
async def entity_access(
    ctx: RequestContext, db: AsyncSession, owner_kind: str, owner_id: uuid.UUID, *, write: bool
) -> bool:
    if ctx.principal is None:  # system / task context
        return True
    return await files_service.check_access(ctx, db, owner_kind, owner_id, write=write)


async def can_access(ctx: RequestContext, db: AsyncSession, doc: Document, *, write: bool = False) -> bool:
    if not level_allowed(ctx, doc.access_level):
        return False
    return await entity_access(ctx, db, doc.owner_kind, doc.owner_id, write=write)


async def file_access(ctx: RequestContext, db: AsyncSession, document_id: uuid.UUID, write: bool) -> bool:
    """``files_service`` access checker of entity type ``document``."""
    doc = await db.get(Document, document_id)
    return doc is not None and await can_access(ctx, db, doc, write=write)


async def load(
    ctx: RequestContext, db: AsyncSession, document_id: uuid.UUID, *, write: bool = False
) -> Document:
    doc = await db.get(Document, document_id)
    if doc is None or not await can_access(ctx, db, doc, write=False):
        raise not_found("Document")
    if write and not await can_access(ctx, db, doc, write=True):
        raise forbidden(f"{doc.owner_kind}:update")
    return doc


# ================================================================ read models
def version_out(v: DocumentVersion, f: StoredFile | None) -> DocumentVersionOut:
    return DocumentVersionOut(
        id=v.id,
        version_no=v.version_no,
        sha256=v.sha256,
        size=v.size,
        source=v.source,
        file_name=f.original_name if f else None,
        content_type=f.content_type if f else None,
        template_version_id=v.template_version_id,
        signature_request_id=v.signature_request_id,
        created_by_kind=v.created_by_kind,
        created_by_id=v.created_by_id,
        created_at=v.created_at,
        download_url=f"/api/v1/documents/{v.document_id}/versions/{v.id}/download",
    )


async def _versions(
    db: AsyncSession, document_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, list[DocumentVersionOut]]:
    ids = list(document_ids)
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(DocumentVersion, StoredFile)
            .outerjoin(StoredFile, StoredFile.id == DocumentVersion.file_id)
            .where(DocumentVersion.document_id.in_(ids))
            .order_by(DocumentVersion.version_no.desc())
        )
    ).all()
    out: dict[uuid.UUID, list[DocumentVersionOut]] = {}
    for v, f in rows:
        out.setdefault(v.document_id, []).append(version_out(v, f))
    return out


def _doc_out(doc: Document, versions: list[DocumentVersionOut], *, with_versions: bool) -> DocumentOut:
    current = next((v for v in versions if v.id == doc.current_version_id), versions[0] if versions else None)
    return DocumentOut(
        id=doc.id,
        human_id=doc.human_id,
        owner_kind=doc.owner_kind,
        owner_id=doc.owner_id,
        category=doc.category,
        title=doc.title,
        access_level=doc.access_level,
        retention_class=doc.retention_class,
        expires_on=doc.expires_on,
        signed=doc.signed,
        superseded_by_id=doc.superseded_by_id,
        archived_at=doc.archived_at,
        note=doc.note,
        current_version=current,
        versions=versions if with_versions else None,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
    )


async def document_out(db: AsyncSession, doc: Document, *, with_versions: bool = True) -> DocumentOut:
    await db.refresh(doc)
    versions = (await _versions(db, [doc.id])).get(doc.id, [])
    return _doc_out(doc, versions, with_versions=with_versions)


# ================================================================ writes
def _worker_for(
    owner_kind: str, owner_id: uuid.UUID, worker_candidate_id: uuid.UUID | None
) -> uuid.UUID | None:
    if worker_candidate_id is not None:
        return worker_candidate_id
    return owner_id if owner_kind == "candidate" else None


async def _next_version_no(db: AsyncSession, document_id: uuid.UUID) -> int:
    n = (
        await db.execute(
            select(func.coalesce(func.max(DocumentVersion.version_no), 0)).where(
                DocumentVersion.document_id == document_id
            )
        )
    ).scalar_one()
    return int(n) + 1


async def append_version(
    ctx: RequestContext,
    db: AsyncSession,
    doc: Document,
    file: StoredFile,
    *,
    source: str,
    template_version_id: uuid.UUID | None = None,
    signature_request_id: uuid.UUID | None = None,
) -> DocumentVersion:
    """Low-level: add ``file`` as the next version and make it current (no signed check)."""
    principal = ctx.principal
    kind = getattr(principal, "type", None) or "system"
    v = DocumentVersion(
        id=uuid.uuid4(),
        tenant_id=doc.tenant_id,
        document_id=doc.id,
        worker_candidate_id=doc.worker_candidate_id,
        version_no=await _next_version_no(db, doc.id),
        file_id=file.id,
        sha256=file.sha256,
        size=file.size_bytes,
        source=source,
        template_version_id=template_version_id,
        signature_request_id=signature_request_id,
        created_by_kind=str(kind)[:20],
        created_by_id=getattr(principal, "id", None),
    )
    db.add(v)
    await db.flush()
    doc.current_version_id = v.id
    await db.flush()
    return v


async def create_document(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    owner_kind: str,
    owner_id: uuid.UUID,
    category: str,
    title: str,
    access_level: str = "entity",
    expires_on: date | None = None,
    note: str | None = None,
    worker_candidate_id: uuid.UUID | None = None,
    retention_class: str | None = None,
    document_id: uuid.UUID | None = None,
) -> Document:
    """A document row without versions (callers add the first version)."""
    if access_level not in ACCESS_LEVELS:
        raise ValidationFailed("Unknown access level", code="invalid_access_level")
    doc = Document(
        id=document_id or uuid.uuid4(),
        tenant_id=tenant_of(db),
        human_id=await next_human_id(db, "DOC"),
        owner_kind=owner_kind,
        owner_id=owner_id,
        worker_candidate_id=_worker_for(owner_kind, owner_id, worker_candidate_id),
        category=category,
        title=title[:300] or "Document",
        access_level=access_level,
        retention_class=retention_class or RETENTION_BY_CATEGORY.get(category),
        expires_on=expires_on,
        note=note or None,
        created_by=user_id(ctx),
    )
    db.add(doc)
    await db.flush()
    return doc


async def store_bytes(
    ctx: RequestContext,
    db: AsyncSession,
    doc_id: uuid.UUID,
    data: bytes,
    filename: str | None,
    *,
    kinds: Iterable[str] = UPLOAD_KINDS,
) -> StoredFile:
    return await files_service.store_upload(ctx, db, data, filename, kinds, FILE_OWNER, doc_id)


async def store_generated_pdf(
    ctx: RequestContext, db: AsyncSession, doc_id: uuid.UUID, data: bytes, filename: str
) -> StoredFile:
    from app.core import storage

    tenant_id = tenant_of(db)
    task_ctx = ctx if ctx.tenant_id is not None else RequestContext(tenant_id=tenant_id)
    return await files_service.store_generated(
        task_ctx,
        db,
        data,
        key=storage.new_key(tenant_id),
        filename=filename,
        content_type="application/pdf",
        owner_entity_type=FILE_OWNER,
        owner_entity_id=doc_id,
    )


def _check_level_change(ctx: RequestContext, access_level: str) -> None:
    if access_level == "hr" and ctx.principal is not None and not has(ctx, DOCUMENT_HR):
        raise forbidden(DOCUMENT_HR)
    if not level_allowed(ctx, access_level):
        raise forbidden(f"document:{access_level}")


async def upload_document(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    owner_kind: str,
    owner_id: uuid.UUID,
    data: bytes,
    filename: str | None,
    category: str,
    title: str | None,
    access_level: str = "entity",
    expires_on: date | None = None,
    note: str | None = None,
    document_id: uuid.UUID | None = None,
) -> Document:
    if not await entity_access(ctx, db, owner_kind, owner_id, write=False):
        raise not_found("Resource")
    if not await entity_access(ctx, db, owner_kind, owner_id, write=True):
        raise forbidden(f"{owner_kind}:update")
    _check_level_change(ctx, access_level)
    doc_id = document_id or uuid.uuid4()
    file = await store_bytes(ctx, db, doc_id, data, filename)
    doc = await create_document(
        ctx,
        db,
        owner_kind=owner_kind,
        owner_id=owner_id,
        category=category,
        title=title or file.original_name,
        access_level=access_level,
        expires_on=expires_on,
        note=note,
        document_id=doc_id,
    )
    await append_version(ctx, db, doc, file, source="upload")
    return doc


async def add_version(
    ctx: RequestContext, db: AsyncSession, document_id: uuid.UUID, data: bytes, filename: str | None
) -> DocumentOut:
    doc = await load(ctx, db, document_id, write=True)
    if doc.signed:
        raise conflict("document_signed", "A signed document cannot be replaced; supersede it instead")
    if doc.archived_at is not None:
        raise conflict("document_archived", "The document is archived")
    file = await store_bytes(ctx, db, doc.id, data, filename)
    await append_version(ctx, db, doc, file, source="upload")
    return await document_out(db, doc)


async def supersede(
    ctx: RequestContext, db: AsyncSession, document_id: uuid.UUID, data: bytes, filename: str | None
) -> DocumentOut:
    old = await load(ctx, db, document_id, write=True)
    if old.superseded_by_id is not None:
        raise conflict("document_superseded", "The document was already superseded")
    new_id = uuid.uuid4()
    file = await store_bytes(ctx, db, new_id, data, filename)
    new = await create_document(
        ctx,
        db,
        owner_kind=old.owner_kind,
        owner_id=old.owner_id,
        category=old.category,
        title=old.title,
        access_level=old.access_level,
        expires_on=old.expires_on,
        worker_candidate_id=old.worker_candidate_id,
        retention_class=old.retention_class,
        document_id=new_id,
    )
    await append_version(ctx, db, new, file, source="upload")
    old.superseded_by_id = new.id
    await db.flush()
    return await document_out(db, new)


async def patch_document(
    ctx: RequestContext, db: AsyncSession, document_id: uuid.UUID, data: DocumentPatchIn
) -> DocumentOut:
    doc = await load(ctx, db, document_id, write=True)
    if doc.signed and (data.category or data.title or data.access_level):
        raise conflict("document_signed", "Only the expiry of a signed document can change")
    if data.access_level is not None and data.access_level != doc.access_level:
        if "hr" in (data.access_level, doc.access_level) and not has(ctx, DOCUMENT_HR):
            raise forbidden(DOCUMENT_HR)
        _check_level_change(ctx, data.access_level)
        doc.access_level = data.access_level
    if data.category is not None:
        doc.category = data.category
    if data.title is not None:
        doc.title = data.title
    if data.clear_expiry:
        doc.expires_on = None
    elif data.expires_on is not None:
        doc.expires_on = data.expires_on
    await db.flush()
    await db.refresh(doc)
    return await document_out(db, doc)


async def archive_document(ctx: RequestContext, db: AsyncSession, document_id: uuid.UUID) -> DocumentOut:
    doc = await load(ctx, db, document_id, write=True)
    if doc.signed:
        raise conflict("document_signed", "A signed document cannot be deleted")
    if doc.archived_at is None:
        doc.archived_at = now()
        await db.flush()
    await db.refresh(doc)
    return await document_out(db, doc)


async def get_document(ctx: RequestContext, db: AsyncSession, document_id: uuid.UUID) -> DocumentOut:
    return await document_out(db, await load(ctx, db, document_id))


async def list_documents(
    ctx: RequestContext,
    db: AsyncSession,
    owner_kind: str,
    owner_id: uuid.UUID,
    *,
    include_archived: bool = False,
) -> list[DocumentOut]:
    if not await entity_access(ctx, db, owner_kind, owner_id, write=False):
        raise not_found("Resource")
    stmt = select(Document).where(Document.owner_kind == owner_kind, Document.owner_id == owner_id)
    if not include_archived:
        stmt = stmt.where(Document.archived_at.is_(None))
    docs = [
        d
        for d in (await db.execute(stmt.order_by(Document.created_at.desc(), Document.id))).scalars()
        if level_allowed(ctx, d.access_level)
    ]
    versions = await _versions(db, [d.id for d in docs])
    return [_doc_out(d, versions.get(d.id, []), with_versions=False) for d in docs]


async def download_version(
    ctx: RequestContext, db: AsyncSession, document_id: uuid.UUID, version_id: uuid.UUID
) -> files_service.Download:
    doc = await load(ctx, db, document_id)
    v = await db.get(DocumentVersion, version_id)
    if v is None or v.document_id != doc.id:
        raise not_found("Document version")
    f = await db.get(StoredFile, v.file_id)
    if f is None or f.deleted_at is not None or f.scan_status not in files_service.DOWNLOADABLE_SCAN_STATUSES:
        raise not_found("File")
    data = await files_service.read_file(f)
    audit.record(
        "document.downloaded",
        "document",
        doc.id,
        session=db,
        version_no=v.version_no,
        access_level=doc.access_level,
    )
    return files_service.Download(data=data, filename=f.original_name, content_type=f.content_type)


async def version_bytes(db: AsyncSession, version_id: uuid.UUID) -> tuple[DocumentVersion, bytes]:
    v = await db.get(DocumentVersion, version_id)
    if v is None:
        raise not_found("Document version")
    f = await db.get(StoredFile, v.file_id)
    if f is None or f.deleted_at is not None:
        raise not_found("File")
    return v, await files_service.read_file(f)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ================================================================ P1 attachments facade
class AttachmentFacade:
    """``files_service`` attachment store over documents (ids, URLs and response shapes unchanged)."""

    async def _out(self, db: AsyncSession, doc: Document) -> AttachmentOut:
        v = await db.get(DocumentVersion, doc.current_version_id) if doc.current_version_id else None
        f = await db.get(StoredFile, v.file_id) if v else None
        assert f is not None
        return AttachmentOut(
            id=doc.id,
            entity_type=doc.owner_kind,
            entity_id=doc.owner_id,
            category=doc.category,
            expires_on=doc.expires_on,
            note=doc.note,
            archived_at=doc.archived_at,
            uploaded_by=doc.created_by,
            created_at=doc.created_at,
            file=files_service.file_out(f),
        )

    def _query(self, entity_type: str, entity_id: uuid.UUID) -> Any:
        return select(Document).where(
            Document.owner_kind == entity_type,
            Document.owner_id == entity_id,
            Document.category.in_(ATTACHMENT_CATEGORIES),
            Document.current_version_id.is_not(None),
        )

    async def list(
        self,
        ctx: RequestContext,
        db: AsyncSession,
        entity_type: str,
        entity_id: uuid.UUID,
        *,
        include_archived: bool,
    ) -> list[AttachmentOut]:
        stmt = self._query(entity_type, entity_id)
        if not include_archived:
            stmt = stmt.where(Document.archived_at.is_(None))
        docs = (await db.execute(stmt.order_by(Document.created_at.desc(), Document.id))).scalars()
        return [await self._out(db, d) for d in docs if level_allowed(ctx, d.access_level)]

    async def add(
        self,
        ctx: RequestContext,
        db: AsyncSession,
        entity_type: str,
        entity_id: uuid.UUID,
        *,
        data: bytes,
        filename: str | None,
        category: str,
        expires_on: date | None,
        note: str | None,
    ) -> AttachmentOut:
        doc_id = uuid.uuid4()
        file = await files_service.store_upload(ctx, db, data, filename, UPLOAD_KINDS, entity_type, entity_id)
        doc = await create_document(
            ctx,
            db,
            owner_kind=entity_type,
            owner_id=entity_id,
            category=category,
            title=file.original_name,
            expires_on=expires_on,
            note=note,
            document_id=doc_id,
        )
        await append_version(ctx, db, doc, file, source="upload")
        await db.refresh(doc)
        return await self._out(db, doc)

    async def owner(self, db: AsyncSession, attachment_id: uuid.UUID) -> tuple[str, uuid.UUID] | None:
        doc = await db.get(Document, attachment_id)
        return (doc.owner_kind, doc.owner_id) if doc is not None else None

    async def archive(self, ctx: RequestContext, db: AsyncSession, attachment_id: uuid.UUID) -> AttachmentOut:
        doc = await db.get(Document, attachment_id)
        if doc is None or not level_allowed(ctx, doc.access_level):
            raise not_found("Resource")
        if doc.signed:
            raise conflict("document_signed", "A signed document cannot be deleted")
        if doc.archived_at is None:
            doc.archived_at = now()
            await db.flush()
        await db.refresh(doc)
        return await self._out(db, doc)

    async def reassign(
        self,
        db: AsyncSession,
        entity_type: str,
        from_entity_id: uuid.UUID,
        to_entity_id: uuid.UUID,
        *,
        only_ids: Iterable[uuid.UUID] | None = None,
    ) -> list[uuid.UUID]:  # type: ignore[valid-type]
        stmt = select(Document).where(
            Document.owner_kind == entity_type, Document.category.in_(ATTACHMENT_CATEGORIES)
        )
        if only_ids is not None:
            ids = list(only_ids)
            if not ids:
                return []
            stmt = stmt.where(Document.id.in_(ids))
        else:
            stmt = stmt.where(Document.owner_id == from_entity_id)
        moved: list[uuid.UUID] = []
        for doc in (await db.execute(stmt)).scalars():
            if doc.signed:
                continue
            doc.owner_id = to_entity_id
            if entity_type == "candidate":
                doc.worker_candidate_id = to_entity_id
            moved.append(doc.id)
            versions = (
                await db.execute(select(DocumentVersion).where(DocumentVersion.document_id == doc.id))
            ).scalars()
            for v in versions:
                f = await db.get(StoredFile, v.file_id)
                if f is not None and f.owner_entity_type == entity_type:
                    f.owner_entity_id = to_entity_id
            legacy = await db.execute(select(Attachment).where(Attachment.migrated_document_id == doc.id))
            for att in legacy.scalars():
                att.entity_id = to_entity_id
        await db.flush()
        return moved

    async def count(self, db: AsyncSession, entity_type: str, entity_id: uuid.UUID) -> int:
        stmt = self._query(entity_type, entity_id).where(Document.archived_at.is_(None))
        return int((await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one())
