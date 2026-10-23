# ruff: file-ignore[line-too-long]
"""Evidence retention (Epic 20 A8, AD-34): ``bgv_evidence`` class, 12 months after adjudication by default.

``purge_due`` (daily beat ``bgv.purge_evidence``, queue ``maintenance``; also the ``expire_fn`` of the
retention bindings) handles cases past ``evidence_purge_after``: deletes evidence, document-request and
discrepancy-response files (storage + tombstone), nulls declarations, candidate responses and provider proposals,
keeps status / outcome / reasons / dates, sets ``evidence_purged_at`` and audits ``bgv.evidence_purged``.
``preview_due`` lists what would be purged (AD-34 preview convention).
"""

import uuid
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import ActorRef
from app.models.bgv import BgvCase, BgvCheck, BgvDiscrepancy, BgvDocumentRequest, BgvEvidence
from app.services.bgv.common import now, record_event, today


async def _due(db: AsyncSession, on: date | None) -> list[BgvCase]:
    stmt = select(BgvCase).where(
        BgvCase.evidence_purge_after.isnot(None),
        BgvCase.evidence_purge_after <= (on or today()),
        BgvCase.evidence_purged_at.is_(None),
    )
    return list((await db.execute(stmt.with_for_update(skip_locked=True))).scalars())


async def preview_due(db: AsyncSession, on: date | None = None) -> list[dict[str, Any]]:
    return [
        {
            "case_id": str(c.id),
            "human_id": c.human_id,
            "evidence_purge_after": c.evidence_purge_after.isoformat() if c.evidence_purge_after else None,
        }
        for c in await _due(db, on)
    ]


async def purge_case(db: AsyncSession, case: BgvCase) -> int:
    from app.services import files_service

    files = 0
    check_ids = [
        c for (c,) in (await db.execute(select(BgvCheck.id).where(BgvCheck.case_id == case.id))).all()
    ]
    evidence = (
        list((await db.execute(select(BgvEvidence).where(BgvEvidence.check_id.in_(check_ids)))).scalars())
        if check_ids
        else []
    )
    for ev in evidence:
        files += int(await files_service.purge_file(db, ev.file_id))
    for req in (
        await db.execute(select(BgvDocumentRequest).where(BgvDocumentRequest.case_id == case.id))
    ).scalars():
        for fid in req.file_ids or []:
            files += int(await files_service.purge_file(db, fid))
        req.declared = None
    for d in (await db.execute(select(BgvDiscrepancy).where(BgvDiscrepancy.case_id == case.id))).scalars():
        for fid in d.response_file_ids or []:
            files += int(await files_service.purge_file(db, fid))
        d.candidate_response = None
    for c in (await db.execute(select(BgvCheck).where(BgvCheck.case_id == case.id))).scalars():
        c.provider_proposal = None
    case.evidence_purged_at = now()
    record_event(db, case, "evidence_purged", ActorRef("system", None), files=files)
    audit.record(
        "bgv.evidence_purged",
        "bgv_case",
        case.id,
        session=db,
        actor_type="system",
        actor_id=None,
        files=files,
    )
    await db.flush()
    return files


async def purge_due(db: AsyncSession, on: date | None = None) -> dict[str, int]:
    cases = await _due(db, on)
    files = 0
    for case in cases:
        files += await purge_case(db, case)
    return {"cases": len(cases), "files": files}


async def expire_binding(db: AsyncSession, *_: Any, **__: Any) -> dict[str, int]:
    """``expire_fn`` of the ``bgv_evidence`` retention bindings (Epic 35's generic scan)."""
    return await purge_due(db)


async def candidate_for_row(db: AsyncSession, row_id: uuid.UUID, *, table: str) -> uuid.UUID | None:
    if table == "bgv_evidence":
        return (
            await db.execute(select(BgvEvidence.candidate_id).where(BgvEvidence.id == row_id))
        ).scalar_one_or_none()
    model = BgvDocumentRequest if table == "bgv_document_requests" else BgvDiscrepancy
    return (await db.execute(select(model.candidate_id).where(model.id == row_id))).scalar_one_or_none()


async def anchor_for_row(db: AsyncSession, row_id: uuid.UUID, *, table: str) -> Any:
    if table == "bgv_evidence":
        check_id = (
            await db.execute(select(BgvEvidence.check_id).where(BgvEvidence.id == row_id))
        ).scalar_one_or_none()
        case_id = (
            await db.execute(select(BgvCheck.case_id).where(BgvCheck.id == check_id))
        ).scalar_one_or_none()
    else:
        model = BgvDocumentRequest if table == "bgv_document_requests" else BgvDiscrepancy
        case_id = (await db.execute(select(model.case_id).where(model.id == row_id))).scalar_one_or_none()
    if case_id is None:
        return None
    return (
        await db.execute(select(BgvCase.adjudicated_at).where(BgvCase.id == case_id))
    ).scalar_one_or_none()
