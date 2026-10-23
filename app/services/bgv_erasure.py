# ruff: file-ignore[line-too-long]
"""BGV erasure handler ``bgv.cases`` (58), Epic 20 A10 (AD-34).

* Rows inside the ``bgv_evidence`` retention window (``evidence_purge_after`` in the future, or in
  ``ctx.retained``) keep files and declarations and are reported as retained with the class and expiry date.
* Otherwise: evidence / document / response files are deleted (tombstone), declarations, responses, proposals and
  notes nulled; referee name / email / phone and response answers become ``[erased]``. Status, outcome and dates
  are kept.
* ``export`` returns the cases (status, outcome, check results) and the candidate's own submissions, never
  referee answers. ``verify`` finds residual referee contact data on non-retained rows.
"""

import uuid
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bgv import (
    BgvCase,
    BgvCaseEvent,
    BgvCheck,
    BgvDiscrepancy,
    BgvDocumentRequest,
    BgvEvidence,
    Referee,
    ReferenceCheck,
    ReferenceResponse,
)
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler

ERASED = "[erased]"
RETENTION_CLASS = "bgv_evidence"


def _retained(ctx: ErasureContext, case: BgvCase) -> bool:
    if case.id in ctx.retained.get("bgv_cases", set()):
        return True
    return (
        case.evidence_purge_after is not None
        and case.evidence_purged_at is None
        and case.evidence_purge_after > date.today()
    )


class BgvHandler:
    name = "bgv.cases"
    order = 58
    tables = (
        "bgv_cases",
        "bgv_case_events",
        "bgv_document_requests",
        "bgv_checks",
        "bgv_evidence",
        "bgv_discrepancies",
        "reference_checks",
        "referees",
        "reference_responses",
    )

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        from app.services import files_service

        ids = await subject_ids(db, ctx.candidate_id)
        counts = {"cases": 0, "retained": 0, "files": 0, "referees": 0}
        cases = list((await db.execute(select(BgvCase).where(BgvCase.candidate_id.in_(ids)))).scalars())
        for case in cases:
            counts["cases"] += 1
            case.cancel_reason = ERASED if case.cancel_reason else None
            case.adjudication_reasons = ERASED if case.adjudication_reasons else None
            for ev in (
                await db.execute(select(BgvCaseEvent).where(BgvCaseEvent.case_id == case.id))
            ).scalars():
                ev.detail = {}
            checks = list((await db.execute(select(BgvCheck).where(BgvCheck.case_id == case.id))).scalars())
            for c in checks:
                c.result_note = ERASED if c.result_note else None
                c.waived_reason = ERASED if c.waived_reason else None
                c.verifier_name = None
                c.source_contacted = None
            if _retained(ctx, case):
                counts["retained"] += 1
                continue
            for c in checks:
                c.provider_proposal = None
                for ev in (  # type: ignore[assignment]
                    await db.execute(select(BgvEvidence).where(BgvEvidence.check_id == c.id))
                ).scalars():
                    counts["files"] += int(await files_service.purge_file(db, ev.file_id))  # type: ignore[attr-defined]
            for req in (
                await db.execute(select(BgvDocumentRequest).where(BgvDocumentRequest.case_id == case.id))
            ).scalars():
                for fid in req.file_ids or []:
                    counts["files"] += int(await files_service.purge_file(db, fid))
                req.declared = None
                req.return_note = None
            for d in (
                await db.execute(select(BgvDiscrepancy).where(BgvDiscrepancy.case_id == case.id))
            ).scalars():
                for fid in d.response_file_ids or []:
                    counts["files"] += int(await files_service.purge_file(db, fid))
                d.candidate_response = None
                d.description = ERASED
                d.internal_note = None
                d.resolution_note = None
        rcs = list(
            (await db.execute(select(ReferenceCheck).where(ReferenceCheck.candidate_id.in_(ids)))).scalars()
        )
        rc_ids = [r.id for r in rcs] or [uuid.uuid4()]
        for rc in rcs:
            rc.review_note = None
        for ref in (
            await db.execute(select(Referee).where(Referee.reference_check_id.in_(rc_ids)))
        ).scalars():
            ref.name = ERASED
            ref.email = ERASED
            ref.phone = None
            ref.company = None
            ref.relationship = None
            counts["referees"] += 1
        for resp in (
            await db.execute(
                select(ReferenceResponse).where(ReferenceResponse.reference_check_id.in_(rc_ids))
            )
        ).scalars():
            resp.answers = {"erased": True}
            resp.ip_hash = None
        await db.flush()
        return counts

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        cases = list(
            (await db.execute(select(BgvCase).where(BgvCase.candidate_id == ctx.candidate_id))).scalars()
        )
        rcs = list(
            (
                await db.execute(
                    select(ReferenceCheck).where(ReferenceCheck.candidate_id == ctx.candidate_id)
                )
            ).scalars()
        )
        if not cases and not rcs:
            return None
        out_cases = []
        for case in cases:
            checks = (await db.execute(select(BgvCheck).where(BgvCheck.case_id == case.id))).scalars()
            docs = (
                await db.execute(select(BgvDocumentRequest).where(BgvDocumentRequest.case_id == case.id))
            ).scalars()
            discs = (
                await db.execute(select(BgvDiscrepancy).where(BgvDiscrepancy.case_id == case.id))
            ).scalars()
            out_cases.append(
                {
                    "reference": case.human_id,
                    "status": case.status,
                    "outcome_qualifier": case.outcome_qualifier,
                    "adjudicated_at": case.adjudicated_at,
                    "retention_class": RETENTION_CLASS,
                    "evidence_purge_after": case.evidence_purge_after,
                    "checks": [
                        {"type": c.type, "subject": c.subject_label, "status": c.status} for c in checks
                    ],
                    "your_submissions": [
                        {
                            "key": d.key,
                            "status": d.status,
                            "declared": d.declared,
                            "files": len(d.file_ids or []),
                        }
                        for d in docs
                    ],
                    "discrepancies": [
                        {
                            "description": d.description,
                            "status": d.status,
                            "your_response": d.candidate_response,
                        }
                        for d in discs
                    ],
                }
            )
        return {
            "bgv_cases": out_cases,
            "reference_checks": [{"status": r.status, "created_at": r.created_at} for r in rcs],
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        ids = await subject_ids(db, ctx.candidate_id)
        findings: list[str] = []
        rc_ids = [
            r
            for (r,) in (
                await db.execute(select(ReferenceCheck.id).where(ReferenceCheck.candidate_id.in_(ids)))
            ).all()
        ]
        if rc_ids:
            for ref in (
                await db.execute(select(Referee).where(Referee.reference_check_id.in_(rc_ids)))
            ).scalars():
                if ref.name != ERASED or ref.email != ERASED or ref.phone:
                    findings.append("referee contact data remains")
        for case in (await db.execute(select(BgvCase).where(BgvCase.candidate_id.in_(ids)))).scalars():
            if _retained(ctx, case):
                continue
            for req in (
                await db.execute(select(BgvDocumentRequest).where(BgvDocumentRequest.case_id == case.id))
            ).scalars():
                if req.declared:
                    findings.append(f"declarations remain on {case.human_id}")
        return findings


register_erasure_handler(BgvHandler())
