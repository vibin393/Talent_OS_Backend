"""Vendors-domain erasure handler ``vendors.submissions`` (57), Epic 17 A10 (AD-34, FR-68).

Claims ``vendor_submissions``, ``vendor_submission_events`` and ``vendor_candidate_ownerships`` for the
subject
(and every record merged into it): submissions linked to the candidate or carrying one of its email addresses.

* The vendor's snapshot (name, email, phone, location, internal reason text) is nulled and ``erased_at`` set;
  the vendor sees an anonymised row.
* The submitted resume file is deleted (storage object + ``stored_files`` tombstone) unless ``ctx.retained``
  keeps the submission.
* Ownerships of the subject are deleted.
Statement lines (``vendor_statement_lines``) carry only a worker name snapshot and are financial records
(retention class ``financial``); they hold no candidate id and are not claimed here.
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.models.candidates import CandidateEmail
from app.models.files import StoredFile
from app.models.vendors import VendorCandidateOwnership, VendorSubmission
from app.services.candidates_erasure import subject_ids
from app.services.privacy.common import now
from app.services.privacy.registry import ErasureContext, register_erasure_handler

ERASED = "[erased]"


async def _scope(db: AsyncSession, ids: list[uuid.UUID]) -> list[VendorSubmission]:
    emails = list(
        (await db.execute(select(CandidateEmail.email).where(CandidateEmail.candidate_id.in_(ids)))).scalars()
    )
    cond = VendorSubmission.candidate_id.in_(ids)
    if emails:
        cond = or_(cond, VendorSubmission.email.in_(emails))  # type: ignore[assignment]
    return list((await db.execute(select(VendorSubmission).where(cond))).scalars())


async def _purge_file(db: AsyncSession, file_id: uuid.UUID) -> bool:
    f = await db.get(StoredFile, file_id)
    if f is None:
        return False
    if f.deleted_at is None:
        try:
            await storage.get_storage().delete(f.storage_key)
        except FileNotFoundError:
            pass
        except Exception:
            logger.exception("Could not delete stored file {} from storage", f.id)
            raise
        f.deleted_at = now()
    f.original_name = ERASED
    return True


class VendorSubmissionsHandler:
    name = "vendors.submissions"
    order = 57
    tables = ("vendor_submissions", "vendor_submission_events", "vendor_candidate_ownerships")

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        ids = await subject_ids(db, ctx.candidate_id)
        retained = ctx.retained.get("vendor_submissions", set())
        subs = await _scope(db, ids)
        erased = files = 0
        for sub in subs:
            if sub.id in retained:
                continue
            if sub.resume_file_id is not None and await _purge_file(db, sub.resume_file_id):
                files += 1
            sub.full_name = None
            sub.email = None
            sub.phone_e164 = None
            sub.current_location = None
            sub.reason_text_internal = None
            sub.erased_at = now()
            erased += 1
        owned = await db.execute(
            delete(VendorCandidateOwnership).where(VendorCandidateOwnership.candidate_id.in_(ids))
        )
        await db.flush()
        return {"vendor_submissions": erased, "files": files, "ownerships": int(owned.rowcount or 0)}

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        ids = await subject_ids(db, ctx.candidate_id)
        subs = await _scope(db, ids)
        if not subs:
            return None
        return {
            "vendor_submissions": [
                {
                    "human_id": s.human_id,
                    "status": s.status,
                    "full_name": s.full_name,
                    "email": s.email,
                    "phone": s.phone_e164,
                    "submitted_at": s.created_at.isoformat() if s.created_at else None,
                }
                for s in subs
            ]
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        ids = await subject_ids(db, ctx.candidate_id)
        retained = ctx.retained.get("vendor_submissions", set())
        left = (
            await db.execute(
                select(VendorSubmission.human_id).where(
                    VendorSubmission.candidate_id.in_(ids),
                    VendorSubmission.full_name.is_not(None),
                    VendorSubmission.id.not_in(list(retained) or [uuid.uuid4()]),
                )
            )
        ).scalars()
        return [f"vendor submission {h} snapshot remains" for h in left]


register_erasure_handler(VendorSubmissionsHandler())
