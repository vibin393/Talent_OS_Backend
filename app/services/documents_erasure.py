"""Documents-domain erasure handler ``documents.files`` (61), Epic 22 A12 (AD-34, NFR-26).

Claims ``documents``, ``document_versions``, ``signature_requests``, ``signers``, ``signatures``,
``signature_events`` and ``signer_otps`` for documents whose ``worker_candidate_id`` (or candidate owner) is the
subject and signers whose principal is the subject.

* Signed documents, completed requests and their evidence are **retained** under ``signed_agreements``
  (reported with the class); within retention nothing is deleted. ``ctx.retained`` is honoured too.
* Unsigned documents: storage objects deleted (``stored_files`` tombstones), title ``[erased]``, notes cleared.
* Open requests of the subject are cancelled; on non-completed requests typed name / IP / UA are nulled and
  signer name / email become ``[erased]``.
* ``signer_otps`` are always deleted.
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.models.documents import (
    OPEN_REQUEST_STATUSES,
    Document,
    DocumentVersion,
    Signature,
    SignatureEvent,
    SignatureRequest,
    Signer,
    SignerOtp,
)
from app.models.files import StoredFile
from app.services.candidates_erasure import subject_ids
from app.services.privacy.common import now
from app.services.privacy.registry import ErasureContext, register_erasure_handler

ERASED = "[erased]"
RETENTION_CLASS = "signed_agreements"


async def _scope(db: AsyncSession, ids: list[uuid.UUID]) -> tuple[list[Document], list[SignatureRequest]]:
    docs = list(
        (
            await db.execute(
                select(Document).where(
                    or_(
                        Document.worker_candidate_id.in_(ids),
                        (Document.owner_kind == "candidate") & Document.owner_id.in_(ids),
                    )
                )
            )
        ).scalars()
    )
    signer_req_ids = select(Signer.request_id).where(Signer.kind == "candidate", Signer.principal_id.in_(ids))
    doc_ids = [d.id for d in docs]
    reqs = list(
        (
            await db.execute(
                select(SignatureRequest).where(
                    or_(
                        SignatureRequest.id.in_(signer_req_ids),
                        SignatureRequest.document_id.in_(doc_ids or [uuid.uuid4()]),
                    )
                )
            )
        ).scalars()
    )
    return docs, reqs


def _retained_doc(doc: Document, ctx: ErasureContext) -> bool:
    return doc.signed or doc.id in ctx.retained.get("documents", set())


def _retained_req(req: SignatureRequest, ctx: ErasureContext) -> bool:
    return req.status == "completed" or req.id in ctx.retained.get("signature_requests", set())


async def _purge_file(db: AsyncSession, file_id: uuid.UUID) -> bool:
    f = await db.get(StoredFile, file_id)
    if f is None:
        return False
    deleted = False
    if f.deleted_at is None:
        try:
            await storage.get_storage().delete(f.storage_key)
        except FileNotFoundError:
            pass
        except Exception:
            logger.exception("Could not delete stored file {} from storage", f.id)
            raise
        f.deleted_at = now()
        deleted = True
    f.original_name = ERASED
    return deleted


async def erase_unretained(
    db: AsyncSession, ctx: ErasureContext, *, include_signed: bool = False
) -> dict[str, int]:
    from app.services.documents import esign

    ids = await subject_ids(db, ctx.candidate_id)
    docs, reqs = await _scope(db, ids)
    counts = {"documents": 0, "files_deleted": 0, "signature_requests": 0, "retained": 0, "signer_otps": 0}
    for req in reqs:
        if req.status in (*OPEN_REQUEST_STATUSES, "draft"):
            esign.transition(db, req, "cancelled", "system")
            req.cancel_reason = "erasure"
            await esign._close_signers(db, req)
    await db.flush()
    for req in reqs:
        if _retained_req(req, ctx) and not include_signed:
            counts["retained"] += 1
            continue
        for sig in (await db.execute(select(Signature).where(Signature.request_id == req.id))).scalars():
            sig.typed_name = None
            sig.ip = None
            sig.user_agent = None
            sig.email = None
        for ev in (
            await db.execute(select(SignatureEvent).where(SignatureEvent.request_id == req.id))
        ).scalars():
            ev.ip = None
            ev.user_agent = None
        for s in (await db.execute(select(Signer).where(Signer.request_id == req.id))).scalars():
            s.name = ERASED
            s.email = ERASED
        counts["signature_requests"] += 1
    req_ids = [r.id for r in reqs]
    if req_ids:
        result = await db.execute(delete(SignerOtp).where(SignerOtp.request_id.in_(req_ids)))
        counts["signer_otps"] = int(result.rowcount or 0)
    for doc in docs:
        if _retained_doc(doc, ctx) and not include_signed:
            counts["retained"] += 1
            continue
        versions = (
            await db.execute(select(DocumentVersion).where(DocumentVersion.document_id == doc.id))
        ).scalars()
        for v in versions:
            counts["files_deleted"] += int(await _purge_file(db, v.file_id))
        if not doc.signed:
            doc.title = ERASED
            doc.note = None
            doc.archived_at = doc.archived_at or now()
        counts["documents"] += 1
    await db.flush()
    return counts


class DocumentsHandler:
    name = "documents.files"
    order = 61
    tables = (
        "documents",
        "document_versions",
        "signature_requests",
        "signers",
        "signatures",
        "signature_events",
        "signer_otps",
    )

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        return await erase_unretained(db, ctx)

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        docs, reqs = await _scope(db, [ctx.candidate_id])
        if not docs and not reqs:
            return None
        sigs: list[dict[str, Any]] = []
        for req in reqs:
            for sig in (await db.execute(select(Signature).where(Signature.request_id == req.id))).scalars():
                sigs.append(
                    {
                        "request": req.human_id,
                        "signed_at": sig.signed_at,
                        "channel": sig.channel,
                        "typed_name": sig.typed_name,
                    }
                )
        return {
            "documents": [
                {
                    "reference": d.human_id,
                    "title": d.title,
                    "category": d.category,
                    "signed": d.signed,
                    "created_at": d.created_at,
                    "retention_class": RETENTION_CLASS if d.signed else d.retention_class,
                }
                for d in docs
            ],
            "signature_requests": [
                {"reference": r.human_id, "title": r.title, "status": r.status} for r in reqs
            ],
            "signatures": sigs,
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        ids = await subject_ids(db, ctx.candidate_id)
        docs, reqs = await _scope(db, ids)
        findings: list[str] = []
        for req in reqs:
            if _retained_req(req, ctx):
                continue
            left = (
                await db.execute(
                    select(Signature.id).where(
                        Signature.request_id == req.id, Signature.typed_name.is_not(None)
                    )
                )
            ).first()
            if left:
                findings.append(f"signature typed name remains on {req.human_id}")
            emails = (
                await db.execute(select(Signer.id).where(Signer.request_id == req.id, Signer.email != ERASED))
            ).first()
            if emails:
                findings.append(f"signer email remains on {req.human_id}")
        for doc in docs:
            if _retained_doc(doc, ctx):
                continue
            if doc.note is not None or doc.title != ERASED:
                findings.append(f"document {doc.human_id} text remains")
        otps = (
            await db.execute(
                select(SignerOtp.id).where(SignerOtp.request_id.in_([r.id for r in reqs] or [uuid.uuid4()]))
            )
        ).first()
        if otps:
            findings.append("signer codes remain")
        return findings


register_erasure_handler(DocumentsHandler())
