"""Hash verification (Epic 22 A7, P2-9.4, FR-119).

* ``verify_upload`` (``document:verify``): SHA-256 of an uploaded file matched against ``final_sha256`` then
  ``doc_sha256`` -> ``final | original | none`` with the signers' names and times; audited
  ``document.verified`` and recorded as a ``verified`` signature event.
* ``public_verify(certificate_id)``: only validity, title, completion time and signer count (no emails).
"""

import hashlib
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.database import platform_session, tenant_session
from app.models.documents import EsignCertificateLookup, Signature, SignatureRequest, Signer
from app.schemas.documents import PublicVerifyOut, VerifyOut, VerifySigner
from app.services.documents.esign import _event


async def _signers(db: AsyncSession, request_id: uuid.UUID) -> list[VerifySigner]:
    rows = (
        await db.execute(
            select(Signer, Signature)
            .outerjoin(Signature, Signature.signer_id == Signer.id)
            .where(Signer.request_id == request_id)
            .order_by(Signer.order)
        )
    ).all()
    return [VerifySigner(name=s.name, signed_at=sig.signed_at if sig else None) for s, sig in rows]


async def verify_upload(ctx: RequestContext, db: AsyncSession, data: bytes) -> VerifyOut:
    digest = hashlib.sha256(data).hexdigest()
    match = "none"
    req = (
        await db.execute(
            select(SignatureRequest)
            .where(SignatureRequest.final_sha256 == digest)
            .order_by(SignatureRequest.completed_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if req is not None:
        match = "final"
    else:
        req = (
            await db.execute(
                select(SignatureRequest)
                .where(SignatureRequest.doc_sha256 == digest)
                .order_by(SignatureRequest.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if req is not None:
            match = "original"
    audit.record("document.verified", "signature_request", req.id if req else None, session=db, match=match)
    if req is None:
        await db.flush()
        return VerifyOut(match="none")
    _event(db, req, "verified", match=match)
    await db.flush()
    del ctx
    return VerifyOut(
        match=match,  # type: ignore[arg-type]
        request_id=req.id,
        human_id=req.human_id,
        status=req.status,
        completed_at=req.completed_at,
        signers=await _signers(db, req.id),
    )


async def public_verify(certificate_id: uuid.UUID) -> PublicVerifyOut:
    async with platform_session() as pdb:
        lookup = await pdb.get(EsignCertificateLookup, certificate_id)
    if lookup is None:
        return PublicVerifyOut(valid=False)
    async with tenant_session(lookup.tenant_id) as db:
        req = await db.get(SignatureRequest, lookup.request_id)
        if req is None or req.status != "completed" or req.final_sha256 is None:
            return PublicVerifyOut(valid=False)
        count = len(await _signers(db, req.id))
        return PublicVerifyOut(valid=True, title=req.title, completed_at=req.completed_at, signer_count=count)
