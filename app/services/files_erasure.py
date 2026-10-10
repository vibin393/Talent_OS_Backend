"""Files-domain erasure handler ``files.candidate`` (30) (Epic 12, FR-68).

Resume ``extracted_text`` / ``parsed_data`` -> NULL; every file the candidate owns (resume versions,
attachments, files owned by the candidate record) is deleted from storage with ``core.storage.delete`` and its
row kept as a tombstone (``deleted_at``, ``original_name = [erased]``). Attachment notes are cleared.
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.models.candidates import CandidateResume
from app.models.files import Attachment, StoredFile
from app.services.candidates_erasure import subject_ids
from app.services.privacy.common import now
from app.services.privacy.registry import ErasureContext, register_erasure_handler

ERASED = "[erased]"


async def _file_ids(db: AsyncSession, ids: list[uuid.UUID]) -> set[uuid.UUID]:
    resume_files = select(CandidateResume.file_id).where(CandidateResume.candidate_id.in_(ids))
    attachment_files = select(Attachment.file_id).where(
        Attachment.entity_type == "candidate", Attachment.entity_id.in_(ids)
    )
    owned = select(StoredFile.id).where(
        StoredFile.owner_entity_type == "candidate", StoredFile.owner_entity_id.in_(ids)
    )
    out: set[uuid.UUID] = set()
    for stmt in (resume_files, attachment_files, owned):
        out |= set((await db.execute(stmt)).scalars())
    return out


class CandidateFilesHandler:
    name = "files.candidate"
    order = 30
    tables = ("candidate_resumes", "stored_files", "attachments")

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        ids = await subject_ids(db, ctx.candidate_id)
        resumes = list((await db.execute(select(CandidateResume).where(CandidateResume.candidate_id.in_(ids)))).scalars())
        for resume in resumes:
            resume.extracted_text = None
            resume.parsed_data = None
        attachments = list(
            (
                await db.execute(
                    select(Attachment).where(Attachment.entity_type == "candidate", Attachment.entity_id.in_(ids))
                )
            ).scalars()
        )
        at = now()
        for att in attachments:
            att.note = None
            att.archived_at = att.archived_at or at
        file_ids = await _file_ids(db, ids)
        deleted = 0
        backend = storage.get_storage()
        for file in (await db.execute(select(StoredFile).where(StoredFile.id.in_(file_ids)))).scalars():
            if file.deleted_at is None:
                try:
                    await backend.delete(file.storage_key)
                except FileNotFoundError:
                    pass
                except Exception:
                    logger.exception("Could not delete stored file {} from storage", file.id)
                    raise
                file.deleted_at = at
                deleted += 1
            file.original_name = ERASED
        await db.flush()
        return {"candidate_resumes": len(resumes), "attachments": len(attachments), "files_deleted": deleted}

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        file_ids = await _file_ids(db, [ctx.candidate_id])
        files = list(
            (
                await db.execute(select(StoredFile).where(StoredFile.id.in_(file_ids), StoredFile.deleted_at.is_(None)))
            ).scalars()
        )
        resumes = (
            await db.execute(select(CandidateResume).where(CandidateResume.candidate_id == ctx.candidate_id))
        ).scalars()
        return {
            "resumes": [
                {"version": r.version, "uploaded_at": r.created_at, "parsed_data": r.parsed_data} for r in resumes
            ],
            "files": [{"name": f.original_name, "content_type": f.content_type, "size_bytes": f.size_bytes} for f in files],
            "_files": [{"id": str(f.id), "name": f.original_name, "storage_key": f.storage_key} for f in files],
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        ids = await subject_ids(db, ctx.candidate_id)
        findings: list[str] = []
        text_left = await db.execute(
            select(CandidateResume.id).where(
                CandidateResume.candidate_id.in_(ids),
                or_(CandidateResume.extracted_text.is_not(None), CandidateResume.parsed_data.is_not(None)),
            )
        )
        if text_left.first():
            findings.append("resume text or parsed data remains")
        file_ids = await _file_ids(db, ids)
        if file_ids:
            live = await db.execute(
                select(StoredFile.id).where(
                    StoredFile.id.in_(file_ids), or_(StoredFile.deleted_at.is_(None), StoredFile.original_name != ERASED)
                )
            )
            findings += [f"stored file {fid} not deleted" for fid in live.scalars()]
        notes = await db.execute(
            select(Attachment.id).where(
                and_(Attachment.entity_type == "candidate", Attachment.entity_id.in_(ids), Attachment.note.is_not(None))
            )
        )
        if notes.first():
            findings.append("attachment notes remain")
        return findings


register_erasure_handler(CandidateFilesHandler())
