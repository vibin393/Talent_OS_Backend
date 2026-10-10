"""File import batches (Epic 13, AD-23 / FR-185): every previewed / applied file is recorded with its SHA-256 so
the same file is never applied twice (409 ``import_already_applied`` unless ``force``, which is audited).

Table ``file_import_batches`` (the spec's ``import_batches``; that name belongs to the Phase 1 candidate bulk
import).
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.models.imports import FileImportBatch


class ImportBatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    kind: str
    file_id: uuid.UUID | None
    sha256: str
    status: str
    row_count: int
    error_count: int
    summary: dict[str, Any]
    applied_at: datetime | None
    applied_by: uuid.UUID | None
    created_at: datetime


def _tenant(db: AsyncSession) -> uuid.UUID:
    return uuid.UUID(str(db.sync_session.info["tenant_id"]))


async def enforce_sandbox_cap(tenant_id: uuid.UUID | None, rows: int) -> None:
    """Epic 66 (FR-339): a Sandbox Tenant imports at most ``settings.sandbox_import_cap`` records per file
    (422 ``sandbox_import_cap``)."""
    from app.core.settings import get_settings
    from app.core.tenancy import is_sandbox

    cap = get_settings().sandbox_import_cap
    if tenant_id is not None and rows > cap and await is_sandbox(tenant_id):
        raise ValidationFailed(
            f"Sandbox workspaces import at most {cap} records per file",
            code="sandbox_import_cap",
            details={"cap": cap, "rows": rows},
        )


async def register_preview(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    kind: str,
    file_id: uuid.UUID | None,
    sha256: str,
    row_count: int,
    error_count: int,
    summary: dict[str, Any],
) -> ImportBatchOut:
    digest = (sha256 or "").strip().lower()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValidationFailed("sha256 must be 64 hex characters", code="invalid_sha256")
    await enforce_sandbox_cap(_tenant(db), row_count)
    batch = FileImportBatch(
        id=uuid.uuid4(),
        tenant_id=_tenant(db),
        kind=kind,
        file_id=file_id,
        sha256=digest,
        status="previewed",
        row_count=row_count,
        error_count=error_count,
        summary=summary,
    )
    db.add(batch)
    await db.flush()
    await db.refresh(batch)
    return ImportBatchOut.model_validate(batch)


async def _batch(db: AsyncSession, batch_id: uuid.UUID) -> FileImportBatch:
    batch = (
        await db.execute(select(FileImportBatch).where(FileImportBatch.id == batch_id).with_for_update())
    ).scalar_one_or_none()
    if batch is None:
        raise NotFound("Import batch not found")
    return batch


async def mark_applied(
    ctx: RequestContext, db: AsyncSession, batch_id: uuid.UUID, *, force: bool = False
) -> ImportBatchOut:
    batch = await _batch(db, batch_id)
    if batch.status != "previewed":
        raise ConflictError("Only a previewed import can be applied", code="import_state", details={"status": batch.status})
    duplicate = (
        await db.execute(
            select(FileImportBatch.id).where(
                FileImportBatch.kind == batch.kind,
                FileImportBatch.sha256 == batch.sha256,
                FileImportBatch.status == "applied",
            )
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        if not force:
            raise ConflictError(
                "This file was already applied", code="import_already_applied", details={"batch_id": str(duplicate)}
            )
        # Forced re-apply (audited): applied as well, marked so the duplicate guard ignores it.
        audit.record(
            "import.forced_reapply", "file_import_batch", batch.id, session=db,
            previous_batch_id=str(duplicate), kind=batch.kind,
        )  # fmt: skip
        batch.summary = {**(batch.summary or {}), "forced_reapply_of": str(duplicate)}
    batch.status = "applied"
    batch.applied_at = datetime.now(UTC)
    batch.applied_by = getattr(ctx.principal, "id", None)
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError as exc:  # concurrent apply of the same file
        raise ConflictError("This file was already applied", code="import_already_applied", details={"batch_id": None}) from exc
    return ImportBatchOut.model_validate(batch)


async def mark_failed(ctx: RequestContext, db: AsyncSession, batch_id: uuid.UUID, *, reason: str | None = None) -> ImportBatchOut:  # noqa: ARG001
    batch = await _batch(db, batch_id)
    batch.status = "failed"
    if reason:
        batch.summary = {**(batch.summary or {}), "failure": reason[:500]}
    await db.flush()
    return ImportBatchOut.model_validate(batch)


async def discard(ctx: RequestContext, db: AsyncSession, batch_id: uuid.UUID) -> ImportBatchOut:  # noqa: ARG001
    batch = await _batch(db, batch_id)
    if batch.status == "applied":
        raise ConflictError("An applied import cannot be discarded", code="import_state", details={"status": batch.status})
    batch.status = "discarded"
    await db.flush()
    return ImportBatchOut.model_validate(batch)
