"""Documents & eSignature public API (Epic 22 fills the Epic 13 contract layer, AD-36).

Contract functions (frozen signatures, ``tests/contracts/signatures.json``): ``render_pdf``,
``create_signature_request``, ``get_signature_request``. Extra public helpers: ``resolve_template``,
``create_document``, ``add_version``, ``list_documents``; ``documents.render.render_markdown`` is the shared
renderer (Epics 23, 29, 30). Onboarding / contracts / extensions call these and listen to
``SignatureRequestStatusChanged`` / ``SignatureRequestCompleted``; they never read the tables.
"""

import uuid
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ValidationFailed
from app.models.documents import Document, SignatureRequest, Signer
from app.schemas.documents import (
    DocumentOut,
    RenderedFile,
    SignatureRequestCreateIn,
    SignatureRequestIn,
    SignatureRequestOut,
    SignatureRequestSummary,
)
from app.services.documents import esign, render, repository, templates
from app.services.documents.common import REQUEST_CANCEL, has, not_found, now, user_id

_SIGNER_KINDS = {
    "worker": "candidate",
    "candidate": "candidate",
    "user": "user",
    "tenant_signatory": "user",
    "client_user": "client_user",
    "vendor_user": "vendor_user",
    "contact": "contact",
}


async def render_pdf(
    ctx: RequestContext,
    db: AsyncSession,
    template_id: uuid.UUID,
    merge_data: Mapping[str, Any],
    *,
    legal_entity_id: uuid.UUID | None,
) -> RenderedFile:
    """Merge + render the published template (Legal Entity variant first) into a stored, scanned PDF."""
    from app.core import storage
    from app.services import files_service

    tid = await templates.resolve_variant(db, template_id, legal_entity_id)
    pdf, template, _ = await templates.render_template(db, tid, merge_refs=None, merge_data=dict(merge_data))
    tenant_id = repository.tenant_of(db)
    task_ctx = ctx if ctx.tenant_id is not None else RequestContext(tenant_id=tenant_id)
    stored = await files_service.store_generated(
        task_ctx,
        db,
        pdf,
        key=storage.new_key(tenant_id),
        filename=f"{template.name}.pdf",
        content_type="application/pdf",
        owner_entity_type="document_template",
        owner_entity_id=template.id,
    )
    return RenderedFile(file_id=stored.id, sha256=stored.sha256, page_count=render.page_count(pdf))


def _summary(req: SignatureRequest) -> SignatureRequestSummary:
    return SignatureRequestSummary(
        id=req.id,
        status=req.status,
        subject_kind=req.subject_kind,
        subject_id=req.subject_id,
        doc_sha256=req.doc_sha256,
        final_sha256=req.final_sha256,
    )


async def create_signature_request(
    ctx: RequestContext, db: AsyncSession, data: SignatureRequestCreateIn
) -> SignatureRequestSummary:
    """Contract entry point (onboarding, contracts, extensions). System callers skip the owner access check."""
    version_id = data.document_version_id
    if data.document_id is not None and version_id is None:
        doc = await db.get(Document, data.document_id)
        if doc is None or doc.current_version_id is None:
            raise not_found("Document")
        version_id = doc.current_version_id
    signers = [
        esign.SignerInput(
            kind=_SIGNER_KINDS.get(s.kind, s.kind),
            principal_id=s.ref_id if _SIGNER_KINDS.get(s.kind, s.kind) != "contact" else None,
            contact_id=s.ref_id if _SIGNER_KINDS.get(s.kind, s.kind) == "contact" else None,
            email=s.email or None,
            name=s.name,
        )
        for s in sorted(data.signers, key=lambda s: s.order)
    ]
    req = await esign.create_request(
        ctx,
        db,
        owner_kind=data.owner_kind or data.subject_kind,
        owner_id=data.owner_id or data.subject_id,
        signers=signers,
        template_id=data.template_id,
        document_version_id=version_id if data.template_id is None else None,
        merge_refs=data.merge_refs,
        merge_data=data.merge_data,
        subject_kind=data.subject_kind,
        subject_id=data.subject_id,
        expires_at=data.expires_at,
        send=data.send,
        check_access=ctx.principal is not None,
    )
    return _summary(req)


async def get_signature_request(
    ctx: RequestContext, db: AsyncSession, request_id: uuid.UUID
) -> SignatureRequestSummary:
    req = (
        await esign.load_request(db, request_id)
        if ctx.principal is None
        else await esign.visible_request(ctx, db, request_id)
    )
    return _summary(req)


# ---------------------------------------------------------------- extra public helpers
async def resolve_template(
    ctx: RequestContext, db: AsyncSession, kind: str, legal_entity_id: uuid.UUID | None
) -> uuid.UUID:
    return await templates.resolve_template(ctx, db, kind, legal_entity_id)


async def create_document(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    owner_kind: str,
    owner_id: uuid.UUID,
    data: bytes,
    filename: str | None,
    category: str,
    title: str | None = None,
    access_level: str = "entity",
    expires_on: Any = None,
) -> DocumentOut:
    doc = await repository.upload_document(
        ctx,
        db,
        owner_kind=owner_kind,
        owner_id=owner_id,
        data=data,
        filename=filename,
        category=category,
        title=title,
        access_level=access_level,
        expires_on=expires_on,
    )
    return await repository.document_out(db, doc)


async def add_version(
    ctx: RequestContext, db: AsyncSession, document_id: uuid.UUID, data: bytes, filename: str | None
) -> DocumentOut:
    return await repository.add_version(ctx, db, document_id, data, filename)


async def list_documents(
    ctx: RequestContext,
    db: AsyncSession,
    owner_kind: str,
    owner_id: uuid.UUID,
    *,
    include_archived: bool = False,
) -> list[DocumentOut]:
    return await repository.list_documents(ctx, db, owner_kind, owner_id, include_archived=include_archived)


# ---------------------------------------------------------------- API helpers (router)
async def create_from_api(
    ctx: RequestContext, db: AsyncSession, data: SignatureRequestIn
) -> SignatureRequestOut:
    signers = [esign.SignerInput(s.kind, s.principal_id, s.contact_id, s.email, s.name) for s in data.signers]
    for s in signers:
        if (
            s.kind in ("candidate", "user", "client_user", "vendor_user")
            and s.principal_id is None
            and not s.email
        ):
            raise ValidationFailed("Pick the signer or give an email address", code="signer_incomplete")
    req = await esign.create_request(
        ctx,
        db,
        owner_kind=data.owner.kind,
        owner_id=data.owner.id,
        signers=signers,
        template_id=data.template_id,
        document_version_id=data.document_version_id,
        merge_refs=data.merge_refs or None,
        merge_data=data.merge_data,
        subject_kind=data.subject.kind if data.subject else None,
        subject_id=data.subject.id if data.subject else None,
        title=data.title,
        expires_at=now() + timedelta(days=data.expires_in_days),
        send=data.send,
        legal_entity_id=data.legal_entity_id,
    )
    return await esign.request_out(db, req)


async def list_requests(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    owner_kind: str | None,
    owner_id: uuid.UUID | None,
    status: str | None,
) -> list[SignatureRequestOut]:
    stmt = select(SignatureRequest).join(Document, Document.id == SignatureRequest.document_id)
    if owner_kind and owner_id:
        if not await repository.entity_access(ctx, db, owner_kind, owner_id, write=False):
            raise not_found("Resource")
        stmt = stmt.where(Document.owner_kind == owner_kind, Document.owner_id == owner_id)
    if status:
        stmt = stmt.where(SignatureRequest.status == status)
    out: list[SignatureRequestOut] = []
    for req in (await db.execute(stmt.order_by(SignatureRequest.created_at.desc()).limit(200))).scalars():
        doc = await db.get(Document, req.document_id)
        if doc is not None and await repository.can_access(ctx, db, doc):
            out.append(await esign.request_out(db, req, with_events=False))
    return out


async def get_request(ctx: RequestContext, db: AsyncSession, request_id: uuid.UUID) -> SignatureRequestOut:
    return await esign.request_out(db, await esign.visible_request(ctx, db, request_id))


async def send(ctx: RequestContext, db: AsyncSession, request_id: uuid.UUID) -> SignatureRequestOut:
    req = await esign.visible_request(ctx, db, request_id, for_update=True)
    doc = await db.get(Document, req.document_id)
    if doc is None or not await repository.can_access(ctx, db, doc, write=True):
        raise not_found("Signature request")
    await esign.send_request(ctx, db, req)
    return await esign.request_out(db, req)


async def cancel(
    ctx: RequestContext, db: AsyncSession, request_id: uuid.UUID, reason: str
) -> SignatureRequestOut:
    req = await esign.visible_request(ctx, db, request_id, for_update=True)
    if req.created_by != user_id(ctx) and not has(ctx, REQUEST_CANCEL):
        from app.core.permissions import forbidden

        raise forbidden(REQUEST_CANCEL)
    await esign.cancel_request(ctx, db, req, reason)
    return await esign.request_out(db, req)


async def internal_signer(ctx: RequestContext, db: AsyncSession, signer_id: uuid.UUID) -> Signer:
    """The signed-in tenant user's own signer row (404 otherwise)."""
    signer = await db.get(Signer, signer_id)
    if (
        signer is None
        or signer.kind != "user"
        or signer.principal_id is None
        or signer.principal_id != user_id(ctx)
    ):
        raise not_found("Signature request")
    return signer


async def my_pending(ctx: RequestContext, db: AsyncSession) -> list[dict[str, Any]]:
    uid = user_id(ctx)
    rows = (
        await db.execute(
            select(Signer, SignatureRequest)
            .join(SignatureRequest, SignatureRequest.id == Signer.request_id)
            .where(Signer.kind == "user", Signer.principal_id == uid)
            .order_by(SignatureRequest.created_at.desc())
            .limit(100)
        )
    ).all()
    return [
        {
            "signer_id": s.id,
            "request_id": r.id,
            "human_id": r.human_id,
            "title": r.title,
            "status": r.status,
            "signer_status": s.status,
            "expires_at": r.expires_at,
        }
        for s, r in rows
    ]
