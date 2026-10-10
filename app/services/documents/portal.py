"""Worker ("My Work") signatures (Epic 22 A6): the Worker's own signer rows, read in the worker org scope
(``org_grants`` on ``signature_request``). Another Worker's request is a 404."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.documents import SignatureRequest, Signer
from app.schemas.documents_portal import PortalSignatureOut
from app.services.documents.common import not_found


async def worker_signer(db: AsyncSession, candidate_id: uuid.UUID, signer_id: uuid.UUID) -> Signer:
    signer = await db.get(Signer, signer_id)
    if signer is None or signer.kind != "candidate" or signer.principal_id != candidate_id:
        raise not_found("Signature request")
    return signer


async def list_for_worker(db: AsyncSession, candidate_id: uuid.UUID) -> list[PortalSignatureOut]:
    rows = (
        await db.execute(
            select(Signer, SignatureRequest)
            .join(SignatureRequest, SignatureRequest.id == Signer.request_id)
            .where(
                Signer.kind == "candidate",
                Signer.principal_id == candidate_id,
                SignatureRequest.subject_kind != "offer",
                SignatureRequest.status != "draft",
            )
            .order_by(SignatureRequest.created_at.desc())
        )
    ).all()
    out: list[PortalSignatureOut] = []
    for s, r in rows:
        earlier = (
            await db.execute(
                select(Signer.id).where(
                    Signer.request_id == r.id, Signer.order < s.order, Signer.status != "signed"
                )
            )
        ).first()
        out.append(
            PortalSignatureOut(
                request_id=r.id,
                signer_id=s.id,
                human_id=r.human_id,
                title=r.title,
                status=r.status,
                signer_status=s.status,
                your_turn=r.status in ("sent", "partially_signed")
                and s.status in ("pending", "notified")
                and earlier is None,
                expires_at=r.expires_at,
                completed_at=r.completed_at,
                final_available=r.final_version_id is not None,
            )
        )
    return out
