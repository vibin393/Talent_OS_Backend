"""Access hooks the candidates service registers with other services (files, activity)."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.permissions import Principal
from app.models.candidates import CandidateResume
from app.services import files_service
from app.services.candidates import activity_port
from app.services.candidates.common import READ, UPDATE, can_access

_registered = False


async def candidate_access(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, write: bool
) -> bool:
    principal = ctx.principal
    if not isinstance(principal, Principal) or principal.type not in ("user", "app", "api_key"):  # Epic 67: Public API
        return False
    return await can_access(db, principal, candidate_id, UPDATE if write else READ)


async def resume_access(ctx: RequestContext, db: AsyncSession, resume_id: uuid.UUID, write: bool) -> bool:
    principal = ctx.principal
    if not isinstance(principal, Principal) or principal.type not in ("user", "app", "api_key"):  # Epic 67: Public API
        return False
    resume = await db.get(CandidateResume, resume_id)
    if resume is None:
        return False
    if resume.candidate_id is not None:
        return await candidate_access(ctx, db, resume.candidate_id, write)
    # Draft (not yet attached): its uploader, or whoever may see the import batch it came from.
    if resume.uploaded_by == principal.id:
        return True
    from sqlalchemy import select

    from app.models.candidates import ImportItem
    from app.services.candidates.imports import import_item_access

    item_id = (
        await db.execute(select(ImportItem.id).where(ImportItem.file_id == resume.file_id))
    ).scalar_one_or_none()
    return item_id is not None and await import_item_access(ctx, db, item_id, write)


async def _import_item_access(ctx: RequestContext, db: AsyncSession, item_id: uuid.UUID, write: bool) -> bool:
    from app.services.candidates.imports import import_item_access

    return await import_item_access(ctx, db, item_id, write)


def register() -> None:
    global _registered  # idempotent process-wide wiring
    if _registered:
        return
    files_service.register_access_checker("candidate", candidate_access, plural="candidates")
    files_service.register_access_checker("candidate_resume", resume_access, plural="candidate-resumes")
    files_service.register_access_checker("import_item", _import_item_access, plural="import-items")
    activity_port.register_entity("candidate", candidate_access, plural="candidates")
    _registered = True
