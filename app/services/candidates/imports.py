"""Bulk resume import (FR-20, story 3.6).

``create_import`` validates the upload (<= 200 files or ZIP entries, each <= 10 MB; a ZIP is read in memory,
never extracted to disk, <= 200 MB uncompressed and compression ratio <= 100), stores every entry through
``files.store_upload`` and creates one queued item per entry. After commit, ``ImportItemsQueued`` enqueues
``process_import_item(tenant_id, item_id)`` per item (``app.tasks.candidates``).

``process_item`` (idempotent, skips items not queued/processing): extract text -> ``ai.run(bulk=True)`` ->
failed (``parse_unavailable`` / extraction status) | linked (exact email match: resume added as a new
version) | created / needs_review (new candidate, ``source=bulk_import``; a possible duplicate -> review).
The batch completes when every item is terminal and then emits ``ImportCompleted``.
"""

import io
import uuid
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass

from loguru import logger
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import DomainError, NotFound, ValidationFailed
from app.core.events import emit, subscribe
from app.core.pagination import Page, PageParams
from app.core.settings import get_settings
from app.events.candidates import ImportCompleted, ImportItemsQueued
from app.models.candidates import CandidateResume, ImportBatch, ImportItem
from app.schemas.candidates import (
    CandidateCreate,
    ImportBatchDetailOut,
    ImportBatchOut,
    ImportItemOut,
    ImportItemsPage,
)
from app.services import ai, files_service
from app.services.candidates import activity_port, duplicates, parsing, records
from app.services.candidates.common import now, principal_of

MAX_ITEMS = 200
MAX_ZIP_UNCOMPRESSED = 200 * 1024 * 1024
MAX_RATIO = 100
TERMINAL = frozenset({"created", "linked", "needs_review", "failed"})
IMPORT_FILE_OWNER = "import_item"


@dataclass(frozen=True, slots=True)
class Upload:
    filename: str
    data: bytes


def _too_large(message: str, **details: object) -> ValidationFailed:
    return ValidationFailed(message, code="import_too_large", details=dict(details))


def _max_entry_bytes() -> int:
    return get_settings().max_upload_mb * 1024 * 1024


def _is_archive(upload: Upload) -> bool:
    if not upload.data.startswith(b"PK\x03\x04"):
        return False
    return files_service.sniff_kind(upload.data) != "docx"


def _skip_entry(name: str) -> bool:
    parts = [p for p in name.replace("\\", "/").split("/") if p]
    if not parts or name.endswith("/"):
        return True
    return any(p == "__MACOSX" or p.startswith(".") for p in parts)


def expand_zip(data: bytes) -> list[Upload]:
    """Entries of a ZIP, read in memory with bomb guards. Raises ``import_too_large``."""
    limit = _max_entry_bytes()
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValidationFailed("The ZIP file is corrupt", code="invalid_archive") from exc
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir() and not _skip_entry(i.filename)]
        if len(infos) > MAX_ITEMS:
            raise _too_large(f"At most {MAX_ITEMS} files per import", max_files=MAX_ITEMS)
        declared = sum(i.file_size for i in infos)
        if declared > MAX_ZIP_UNCOMPRESSED:
            raise _too_large("The ZIP is too large when uncompressed", max_uncompressed_mb=200)
        compressed = sum(i.compress_size for i in infos) or 1
        if declared / compressed > MAX_RATIO:
            raise _too_large("The ZIP compression ratio is suspicious", max_ratio=MAX_RATIO)
        out: list[Upload] = []
        total = 0
        for info in infos:
            if info.file_size > limit:
                raise _too_large(
                    "A file in the ZIP is larger than the per-file limit", max_mb=limit // 1048576
                )
            if info.compress_size and info.file_size / info.compress_size > MAX_RATIO:
                raise _too_large("The ZIP compression ratio is suspicious", max_ratio=MAX_RATIO)
            try:
                with zf.open(info) as fh:
                    content = fh.read(limit + 1)  # never trust the declared size
            except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError, EOFError):
                content = b""  # corrupt / encrypted entry -> failed item
            if len(content) > limit:
                raise _too_large(
                    "A file in the ZIP is larger than the per-file limit", max_mb=limit // 1048576
                )
            total += len(content)
            if total > MAX_ZIP_UNCOMPRESSED:
                raise _too_large("The ZIP is too large when uncompressed", max_uncompressed_mb=200)
            name = info.filename.replace("\\", "/").rsplit("/", 1)[-1]
            out.append(Upload(name, content))
        return out


def expand_uploads(uploads: Sequence[Upload]) -> list[Upload]:
    if not uploads:
        raise ValidationFailed(
            "No files were uploaded", details={"fields": [{"field": "files", "message": "required"}]}
        )
    if len(uploads) > MAX_ITEMS:
        raise _too_large(f"At most {MAX_ITEMS} files per import", max_files=MAX_ITEMS)
    entries: list[Upload] = []
    for up in uploads:
        if _is_archive(up):
            entries.extend(expand_zip(up.data))
        else:
            if len(up.data) > _max_entry_bytes():
                raise _too_large(
                    "A file is larger than the per-file limit", max_mb=get_settings().max_upload_mb
                )
            entries.append(up)
        if len(entries) > MAX_ITEMS:
            raise _too_large(f"At most {MAX_ITEMS} files per import", max_files=MAX_ITEMS)
    if not entries:
        raise ValidationFailed("The upload contains no files", code="import_empty")
    return entries


# ================================================================ create
async def create_import(ctx: RequestContext, db: AsyncSession, uploads: Sequence[Upload]) -> ImportBatchOut:
    entries = expand_uploads(uploads)
    from app.services import imports_service

    await imports_service.enforce_sandbox_cap(ctx.tenant_id, len(entries))  # Epic 66: sandbox import cap
    principal = principal_of(ctx)
    batch = ImportBatch(
        id=uuid.uuid4(), tenant_id=ctx.tenant_id, status="queued", total=len(entries), created_by=principal.id
    )
    db.add(batch)
    await db.flush()
    queued: list[uuid.UUID] = []
    for entry in entries:
        item = ImportItem(
            id=uuid.uuid4(),
            tenant_id=ctx.tenant_id,
            batch_id=batch.id,
            filename=(entry.filename or "file")[:255],
            status="queued",
        )
        db.add(item)
        await db.flush()
        try:
            async with db.begin_nested():
                if not entry.data:
                    raise ValidationFailed("Empty or unreadable file", code="unreadable_file")
                stored = await files_service.store_upload(
                    ctx,
                    db,
                    entry.data,
                    entry.filename,
                    files_service.RESUME_KINDS,
                    IMPORT_FILE_OWNER,
                    item.id,
                )
            item.file_id = stored.id
            queued.append(item.id)
        except DomainError as exc:
            item.status = "failed"
            item.reason = exc.code
    await db.flush()
    await _maybe_complete(ctx, db, batch.id)
    if queued:
        emit(db, ImportItemsQueued(tenant_id=ctx.tenant_id, batch_id=batch.id, item_ids=tuple(queued)))  # type: ignore[arg-type]
    logger.info("Import batch {} created: {} items, {} queued", batch.id, len(entries), len(queued))
    return await _batch_out(db, batch)


def _enqueue_items(event: ImportItemsQueued) -> None:
    from app.tasks.candidates import process_import_item

    for item_id in event.item_ids:
        try:
            process_import_item.delay(str(event.tenant_id), str(item_id))
        except Exception:
            logger.exception("Could not enqueue import item {} (batch {})", item_id, event.batch_id)


def _log_completed(event: ImportCompleted) -> None:
    # Epic 10 turns this into a notification for the importer.
    logger.info("Import batch {} completed: {}", event.batch_id, event.counts)


subscribe(ImportItemsQueued, _enqueue_items)
subscribe(ImportCompleted, _log_completed)


# ================================================================ process (Celery task body)
async def _counts(db: AsyncSession, batch_id: uuid.UUID) -> dict[str, int]:
    rows = await db.execute(
        select(ImportItem.status, func.count())
        .where(ImportItem.batch_id == batch_id)
        .group_by(ImportItem.status)
    )
    return {status: int(n) for status, n in rows}


async def _maybe_complete(ctx: RequestContext, db: AsyncSession, batch_id: uuid.UUID) -> None:
    batch = await db.get(ImportBatch, batch_id, with_for_update=True)
    if batch is None or batch.status == "completed":
        return
    counts = await _counts(db, batch_id)
    if sum(n for s, n in counts.items() if s not in TERMINAL) == 0:
        batch.status = "completed"
        batch.completed_at = now()
        await db.flush()
        emit(
            db,
            ImportCompleted(
                tenant_id=ctx.tenant_id,  # type: ignore[arg-type]
                batch_id=batch_id,
                created_by=batch.created_by,
                counts=counts,
            ),
        )
    elif batch.status == "queued" and any(s != "queued" for s in counts):
        batch.status = "processing"
        await db.flush()


async def process_item(ctx: RequestContext, db: AsyncSession, item_id: uuid.UUID) -> str | None:
    """Process one import item. Idempotent: items that are not queued/processing are skipped."""
    item = await db.get(ImportItem, item_id, with_for_update=True)
    if item is None:
        logger.warning("Import item {} not found", item_id)
        return None
    if item.status not in {"queued", "processing"}:
        return item.status
    item.status = "processing"
    await db.flush()
    await _maybe_complete(ctx, db, item.batch_id)
    status, reason = await _process(ctx, db, item)
    if status == "queued":  # paused (budget): not processed, re-queued later
        item.status, item.reason = status, reason
        await db.flush()
        return PAUSED_BUDGET
    item.status = status
    item.reason = reason
    await db.flush()
    await _maybe_complete(ctx, db, item.batch_id)
    return status


async def _process(ctx: RequestContext, db: AsyncSession, item: ImportItem) -> tuple[str, str | None]:
    if item.file_id is None:
        return "failed", "file_missing"
    file = await files_service.get_file(db, item.file_id)
    if file is None:
        return "failed", "file_missing"
    data = await files_service.read_file(file)
    # A retried item may already have its resume (e.g. crash after ingest): reuse it.
    resume = (
        await db.execute(select(CandidateResume).where(CandidateResume.file_id == file.id))
    ).scalar_one_or_none()
    if resume is None:
        # Epic 12 (FR-63): at >= 100 % of the AI budget bulk parsing pauses; the item stays queued with reason
        # ``paused_budget`` until ``resume_budget_paused`` re-queues it (budget raised / new month).
        if await ai.bulk_blocked(db, ctx.tenant_id):
            return "queued", PAUSED_BUDGET
        ing = await parsing.ingest_file(
            ctx, db, file_id=file.id, data=data, candidate_id=None, input_ref_prefix="import_item", bulk=True
        )
        resume, parsed = ing.resume, ing.parsed
    else:
        parsed = await parsing.parsed_of(ctx, db, resume)
        if resume.candidate_id is not None:
            item.candidate_id = resume.candidate_id
            return "linked", None
    if parsed is None:
        reason = "parse_unavailable" if resume.text_status == "ok" else resume.text_status
        return "failed", reason
    if not parsed.full_name:
        return "failed", "parse_incomplete"

    # Exact email -> link the resume to the existing candidate as a new version.
    for email in parsed.emails:
        existing = await duplicates.link_by_exact_email(ctx, db, email)
        if existing is not None:
            resume.candidate_id = existing
            resume.version = await parsing.next_version(db, existing)
            await db.flush()
            if resume.ai_action_id is not None:
                await ai.set_subject(db, resume.ai_action_id, "candidate", existing)
            await activity_port.add_system_entry(
                ctx,
                db,
                "candidate",
                existing,
                "Resume imported",
                resume_id=str(resume.id),
                version=resume.version,
            )
            item.candidate_id = existing
            return "linked", None

    prefill, _ = parsing.prefill_of(parsed)
    data_in = CandidateCreate.model_validate(
        {**prefill.model_dump(exclude={"full_name", "source"}), "full_name": parsed.full_name}
    )
    batch = await db.get(ImportBatch, item.batch_id)
    owner = batch.created_by if batch is not None and batch.created_by else principal_of(ctx).id
    cand, _ = await records.create_record(
        ctx, db, data_in, owner_id=owner, source="bulk_import", skill_origin="import"
    )
    resume.candidate_id = cand.id
    resume.version = await parsing.next_version(db, cand.id)
    await db.flush()
    if resume.ai_action_id is not None:
        await ai.set_subject(db, resume.ai_action_id, "candidate", cand.id)
    item.candidate_id = cand.id
    hits = await duplicates.find_duplicates(ctx, db, cand)
    if hits:
        item.duplicate_id = hits[0].duplicate_id
        return "needs_review", None
    return "created", None


async def mark_failed(ctx: RequestContext, db: AsyncSession, item_id: uuid.UUID, reason: str) -> None:
    """Terminal failure after an unexpected processing error (called in a fresh transaction)."""
    item = await db.get(ImportItem, item_id, with_for_update=True)
    if item is None or item.status in TERMINAL:
        return
    item.status = "failed"
    item.reason = reason
    await db.flush()
    await _maybe_complete(ctx, db, item.batch_id)


# ================================================================ read
async def _batch_out(db: AsyncSession, batch: ImportBatch) -> ImportBatchOut:
    await db.refresh(batch)
    return ImportBatchOut(
        id=batch.id,
        status=batch.status,
        total=batch.total,
        counts=await _counts(db, batch.id),
        created_by=batch.created_by,
        created_at=batch.created_at,
        completed_at=batch.completed_at,
    )


def _visible_batches(ctx: RequestContext) -> Select[tuple[ImportBatch]]:
    """Import batches are visible to their creator (and tenant-wide candidate readers)."""
    from app.core.permissions import Scope

    principal = principal_of(ctx)
    stmt = select(ImportBatch)
    if not principal.has("candidate:read", Scope.TENANT):
        stmt = stmt.where(ImportBatch.created_by == principal.id)
    return stmt


async def list_imports(ctx: RequestContext, db: AsyncSession, params: PageParams) -> Page[ImportBatchOut]:
    stmt = _visible_batches(ctx)
    total = int((await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one())
    rows = (
        await db.execute(
            stmt.order_by(ImportBatch.created_at.desc(), ImportBatch.id)
            .limit(params.page_size)
            .offset(params.offset)
        )
    ).scalars()
    return Page[ImportBatchOut](
        items=[await _batch_out(db, b) for b in rows],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


async def get_import(
    ctx: RequestContext, db: AsyncSession, batch_id: uuid.UUID, params: PageParams, status: str | None = None
) -> ImportBatchDetailOut:
    batch = (await db.execute(_visible_batches(ctx).where(ImportBatch.id == batch_id))).scalar_one_or_none()
    if batch is None:
        raise NotFound("Import not found")
    stmt = select(ImportItem).where(ImportItem.batch_id == batch.id)
    if status:
        stmt = stmt.where(ImportItem.status == status)
    total = int((await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one())
    items = list(
        (
            await db.execute(
                stmt.order_by(ImportItem.created_at, ImportItem.id)
                .limit(params.page_size)
                .offset(params.offset)
            )
        ).scalars()
    )
    file_ids = [i.file_id for i in items if i.file_id]
    resumes: dict[uuid.UUID, uuid.UUID] = {}
    if file_ids:
        for rid, fid in await db.execute(
            select(CandidateResume.id, CandidateResume.file_id).where(CandidateResume.file_id.in_(file_ids))
        ):
            resumes[fid] = rid
    base = await _batch_out(db, batch)
    return ImportBatchDetailOut(
        **base.model_dump(),
        items=ImportItemsPage(
            items=[
                ImportItemOut(
                    id=i.id,
                    filename=i.filename,
                    file_id=i.file_id,
                    resume_id=resumes.get(i.file_id) if i.file_id else None,
                    status=i.status,
                    reason=i.reason,
                    candidate_id=i.candidate_id,
                    duplicate_id=i.duplicate_id,
                    updated_at=i.updated_at,
                )
                for i in items
            ],
            total=total,
            page=params.page,
            page_size=params.page_size,
        ),
    )


async def import_item_access(ctx: RequestContext, db: AsyncSession, item_id: uuid.UUID, write: bool) -> bool:  # noqa: ARG001
    """File access for import items (before a resume exists): whoever may see the batch."""
    item = await db.get(ImportItem, item_id)
    if item is None:
        return False
    stmt = _visible_batches(ctx).where(ImportBatch.id == item.batch_id)
    return (await db.execute(stmt)).scalar_one_or_none() is not None


async def read_uploads(files: Sequence[object]) -> list[Upload]:
    """Read multipart uploads (``UploadFile``-like) with a hard cap per file."""
    if len(files) > MAX_ITEMS:
        raise _too_large(f"At most {MAX_ITEMS} files per import", max_files=MAX_ITEMS)
    out: list[Upload] = []
    for f in files:
        data: bytes = await f.read(MAX_ZIP_UNCOMPRESSED + 1)  # type: ignore[attr-defined]
        if len(data) > MAX_ZIP_UNCOMPRESSED:
            raise _too_large("The upload is too large", max_mb=200)
        out.append(Upload(str(getattr(f, "filename", None) or "file"), data))
    return out


PAUSED_BUDGET = "paused_budget"


async def resume_budget_paused(db: AsyncSession, tenant_id: uuid.UUID) -> int:
    """Re-enqueue import items paused by the AI budget (Epic 12). Returns how many were re-queued."""
    if await ai.bulk_blocked(db, tenant_id):
        return 0
    items = list(
        (
            await db.execute(select(ImportItem).where(ImportItem.status == "queued", ImportItem.reason == PAUSED_BUDGET))
        ).scalars()
    )
    by_batch: dict[uuid.UUID, list[uuid.UUID]] = {}
    for item in items:
        item.reason = None
        by_batch.setdefault(item.batch_id, []).append(item.id)
    await db.flush()
    for batch_id, ids in by_batch.items():
        emit(db, ImportItemsQueued(tenant_id=tenant_id, batch_id=batch_id, item_ids=tuple(ids)))
    return len(items)
