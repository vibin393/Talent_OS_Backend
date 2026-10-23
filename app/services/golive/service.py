"""Go-live import orchestration (Epic 35 A8, FR-185): upload, validate (owner dry runs), preview, apply.

A batch is a foundation ``file_import_batches`` row (``kind='golive.<kind>'``) plus ``golive_import_rows``.
Raw row values are never stored: the error CSV and apply re-read the scanned file. Validation runs inline (one
savepoint per row, always rolled back); apply runs each row in its own savepoint.
"""

import csv
import hashlib
import io
import uuid
from typing import Any

from loguru import logger
from sqlalchemy import Table, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, storage
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.core.settings import get_settings
from app.events.golive import GoLiveImportApplied, GoLiveImportValidated
from app.models.golive import GoLiveImportRow
from app.models.imports import FileImportBatch
from app.services import files_service, imports_service
from app.services.golive import owners, parsing, templates

PREFIX = "golive."
MAX_PREVIEW_ERRORS = 200
SCAN_OK = ("clean", "skipped_dev")
_rows: Table = GoLiveImportRow.__table__  # type: ignore[assignment]

_register = getattr(audit, "register_audit_action", None)
if _register is not None:  # pragma: no branch - registered once the audit catalogue lands
    try:
        _register("golive.applied", "Go-live import applied (rows applied / failed / skipped)")
    except Exception:
        logger.debug("golive.applied audit action not registered")


def _tenant(ctx: RequestContext, db: AsyncSession) -> uuid.UUID:
    return ctx.tenant_id or uuid.UUID(str(db.sync_session.info["tenant_id"]))


# ------------------------------------------------------------------ kinds / templates
def list_kinds() -> list[dict[str, Any]]:
    return [
        {
            "kind": k.kind,
            "label": k.label,
            "available": owners.available(k.kind),
            "owner_epic": k.owner_epic,
            "depends_on": list(k.depends_on),
        }
        for k in templates.KINDS.values()
    ]


def _require_available(kind: str) -> templates.KindDef:
    k = templates.kind_def(kind)
    if not owners.available(kind):
        raise ConflictError(
            "This import type is not available yet", code="kind_unavailable", details={"kind": kind}
        )
    return k


# ------------------------------------------------------------------ read
async def _batch(db: AsyncSession, batch_id: uuid.UUID, *, lock: bool = False) -> FileImportBatch:
    stmt = select(FileImportBatch).where(
        FileImportBatch.id == batch_id, FileImportBatch.kind.startswith(PREFIX)
    )
    if lock:
        stmt = stmt.with_for_update()
    batch = (await db.execute(stmt)).scalar_one_or_none()
    if batch is None:
        raise NotFound("Import not found")
    return batch


def _row_errors(row_no: int, errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "row": row_no,
            "column": e.get("column"),
            "code": e.get("code", "invalid"),
            "message": e.get("message", ""),
        }
        for e in errors
    ]


async def _out(db: AsyncSession, batch: FileImportBatch) -> dict[str, Any]:
    counts: dict[str, int] = dict(
        (
            await db.execute(
                select(_rows.c.status, func.count())
                .where(_rows.c.batch_id == batch.id)
                .group_by(_rows.c.status)
            )
        ).all()  # type: ignore[arg-type]
    )
    err_rows = (
        await db.execute(
            select(_rows.c.row_no, _rows.c.errors)
            .where(_rows.c.batch_id == batch.id, func.jsonb_array_length(_rows.c.errors) > 0)
            .order_by(_rows.c.row_no)
            .limit(MAX_PREVIEW_ERRORS)
        )
    ).all()
    errors: list[dict[str, Any]] = []
    for row_no, errs in err_rows:
        errors.extend(_row_errors(row_no, errs or []))
        if len(errors) >= MAX_PREVIEW_ERRORS:
            break

    def get(status: str) -> int:
        return int(counts.get(status, 0))

    return {
        "id": batch.id,
        "kind": batch.kind.removeprefix(PREFIX),
        "status": batch.status,
        "row_count": batch.row_count,
        "valid_count": get("valid") + get("applied") + get("failed"),
        "error_count": get("invalid") + get("skipped"),
        "applied_count": get("applied"),
        "failed_count": get("failed"),
        "skipped_count": get("skipped"),
        "errors": errors[:MAX_PREVIEW_ERRORS],
        "created_at": batch.created_at,
        "applied_at": batch.applied_at,
    }


async def get_import(ctx: RequestContext, db: AsyncSession, batch_id: uuid.UUID) -> dict[str, Any]:  # ruff: ignore[unused-function-argument]
    return await _out(db, await _batch(db, batch_id))


async def list_imports(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    stmt = select(FileImportBatch).where(FileImportBatch.kind.startswith(PREFIX))
    if kind:
        stmt = select(FileImportBatch).where(FileImportBatch.kind == PREFIX + templates.kind_def(kind).kind)
    batches = (await db.execute(stmt.order_by(FileImportBatch.created_at.desc()).limit(200))).scalars().all()
    return [await _out(db, b) for b in batches]


async def list_rows(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    batch_id: uuid.UUID,
    status: str | None = None,
) -> list[dict[str, Any]]:
    batch = await _batch(db, batch_id)
    stmt = select(_rows.c.row_no, _rows.c.status, _rows.c.errors, _rows.c.result_ref).where(
        _rows.c.batch_id == batch.id
    )
    if status:
        stmt = stmt.where(_rows.c.status == status)
    rows = (await db.execute(stmt.order_by(_rows.c.row_no))).all()
    return [
        {
            "row_no": r.row_no,
            "status": r.status,
            "errors": _row_errors(r.row_no, r.errors or []),
            "result_ref": r.result_ref,
        }
        for r in rows
    ]


def _csv_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


async def _file_rows(db: AsyncSession, batch: FileImportBatch, k: templates.KindDef) -> list[dict[str, str]]:
    if batch.file_id is None:
        raise ConflictError("The import file is no longer available", code="file_missing")
    stored = await files_service.get_file(db, batch.file_id)
    if stored is None or stored.deleted_at is not None:
        raise ConflictError("The import file is no longer available", code="file_missing")
    if stored.scan_status not in SCAN_OK:
        raise ConflictError("The import file has not passed the malware scan", code="file_not_scanned")
    data = await files_service.read_file(stored)
    if hashlib.sha256(data).hexdigest() != batch.sha256:
        raise ConflictError("The import file changed after upload", code="file_changed")
    return parsing.parse(data, k, max_rows=get_settings().golive_max_rows)


async def errors_csv(ctx: RequestContext, db: AsyncSession, batch_id: uuid.UUID) -> str:  # ruff: ignore[unused-function-argument]
    batch = await _batch(db, batch_id)
    k = templates.kind_def(batch.kind.removeprefix(PREFIX))
    rows = (
        await db.execute(
            select(_rows.c.row_no, _rows.c.errors)
            .where(_rows.c.batch_id == batch.id, func.jsonb_array_length(_rows.c.errors) > 0)
            .order_by(_rows.c.row_no)
        )
    ).all()
    values: list[dict[str, str]] = []
    if rows:
        try:
            values = await _file_rows(db, batch, k)
        except (ConflictError, ValidationFailed):
            values = []
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(["row", "column", "code", "message", "value"])
    for row_no, errs in rows:
        src = values[row_no - 1] if 0 < row_no <= len(values) else {}
        for e in errs or []:
            column = e.get("column")
            value = src.get(column, "") if column else ""
            if column in k.sensitive:
                value = parsing.mask(value)
            writer.writerow(
                [row_no, column or "", e.get("code", ""), _csv_cell(e.get("message", "")), _csv_cell(value)]
            )
    return buf.getvalue()


# ------------------------------------------------------------------ upload + validate
async def _run_rows(
    ctx: RequestContext, db: AsyncSession, kind: str, rows: list[dict[str, str]], batch_id: uuid.UUID
) -> list[tuple[int, list[dict[str, Any]], bool]]:
    """Dry-validate every row; returns ``(row_no, errors, worker_pending)``."""
    handler = owners.HANDLERS[kind]
    scratch: dict[str, Any] = {}
    out: list[tuple[int, list[dict[str, Any]], bool]] = []
    for i, row in enumerate(rows, start=1):
        scratch["row_no"] = i
        errors: list[dict[str, Any]] = []
        pending = False
        try:
            async with db.begin_nested() as sp:
                await handler(ctx, db, row, batch_id, validate_only=True, scratch=scratch)
                await sp.rollback()
        except owners.WorkerPending:
            pending = True
        except Exception as exc:
            errors = owners.from_any(exc)
        out.append((i, errors, pending))
    return out


async def upload(
    ctx: RequestContext, db: AsyncSession, kind: str, filename: str | None, data: bytes
) -> tuple[dict[str, Any], bool]:
    """Return ``(GoLiveImportOut, created)``; the same file + kind returns the existing live batch."""
    k = _require_available(kind)
    settings = get_settings()
    if len(data) > settings.golive_max_file_mb * 1024 * 1024:
        raise ValidationFailed(
            f"The file is larger than {settings.golive_max_file_mb} MB",
            code="file_too_large",
            details={"max_mb": settings.golive_max_file_mb},
        )
    name = (filename or "").lower()
    if name and not name.endswith(".csv"):
        raise ValidationFailed(
            "Upload a UTF-8 CSV file", code="unsupported_file_type", details={"allowed": ["csv"]}
        )
    digest = hashlib.sha256(data).hexdigest()
    existing = (
        await db.execute(
            select(FileImportBatch)
            .where(
                FileImportBatch.kind == PREFIX + kind,
                FileImportBatch.sha256 == digest,
                FileImportBatch.status != "discarded",
            )
            .order_by(FileImportBatch.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return await _out(db, existing), False
    rows = parsing.parse(data, k, max_rows=settings.golive_max_rows)
    tenant_id = _tenant(ctx, db)
    stored = await files_service.store_generated(
        ctx,
        db,
        data,
        key=storage.new_key(tenant_id),
        filename=filename or f"golive-{kind}.csv",
        content_type="text/csv",
        owner_entity_type="file_import_batch",
        owner_entity_id=uuid.uuid4(),  # re-pointed at the batch below
    )
    batch_id = uuid.uuid4()
    results = await _run_rows(ctx, db, kind, rows, batch_id)
    invalid = sum(1 for _, errs, _ in results if errs)
    pending = sum(1 for _, errs, p in results if p and not errs)
    out = await imports_service.register_preview(
        ctx, db, PREFIX + kind, stored.id, digest, len(rows), invalid, {"pending_workers": pending}
    )
    # register_preview allocates its own id: use it.
    batch_id = out.id
    await files_service.set_file_owner(db, stored.id, "file_import_batch", batch_id)
    chunk: list[dict[str, Any]] = []
    for row_no, errs, _ in results:
        chunk.append(
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_id,
                "batch_id": batch_id,
                "row_no": row_no,
                "status": "invalid" if errs else "valid",
                "errors": errs,
            }
        )
        if len(chunk) >= 1000:
            await db.execute(insert(_rows), chunk)
            chunk = []
    if chunk:
        await db.execute(insert(_rows), chunk)
    emit(
        db,
        GoLiveImportValidated(
            tenant_id=tenant_id, batch_id=batch_id, kind=kind, row_count=len(rows), error_count=invalid
        ),
    )
    logger.info("golive {} batch {} validated: {} rows, {} invalid", kind, batch_id, len(rows), invalid)
    return await _out(db, await _batch(db, batch_id)), True


# ------------------------------------------------------------------ apply / discard
async def _check_dependencies(
    ctx: RequestContext,
    db: AsyncSession,
    kind: str,
    rows: list[dict[str, str]],
    states: dict[int, str],
    batch_id: uuid.UUID,
) -> None:
    """Epic 52: before applying anything, re-validate each valid row; a row whose ``depends_on`` record is still
    missing (``owners.DependencyPending``) refuses the whole apply with 422 ``dependency_missing``."""
    from app.services.golive.p3_imports import DEPENDENCY_COLUMNS

    if kind not in DEPENDENCY_COLUMNS:
        return
    handler = owners.HANDLERS[kind]
    missing: list[int] = []
    requires: set[str] = set()
    for i, row in enumerate(rows, start=1):
        if states.get(i) != "valid":
            continue
        try:
            async with db.begin_nested() as sp:
                await handler(ctx, db, row, batch_id, validate_only=True, scratch={"row_no": i})
                await sp.rollback()
        except owners.DependencyPending as exc:
            missing.append(i)
            requires.add(exc.requires)
        except Exception:
            continue
    if missing:
        raise ValidationFailed(
            "Import the records this file depends on first",
            code="dependency_missing",
            details={"requires": sorted(requires), "rows": missing[:50], "missing_count": len(missing)},
        )


async def apply(
    ctx: RequestContext, db: AsyncSession, batch_id: uuid.UUID, *, skip_invalid: bool, confirm: str
) -> dict[str, Any]:
    batch = await _batch(db, batch_id, lock=True)
    kind = batch.kind.removeprefix(PREFIX)
    if confirm != "APPLY":
        raise ValidationFailed("Type APPLY to confirm", code="confirm_required")
    if batch.status != "previewed":
        raise ConflictError(
            "Only a previewed import can be applied", code="import_state", details={"status": batch.status}
        )
    k = _require_available(kind)
    states: dict[int, str] = dict(
        (await db.execute(select(_rows.c.row_no, _rows.c.status).where(_rows.c.batch_id == batch.id))).all()  # type: ignore[arg-type]
    )
    invalid_rows = sorted(n for n, s in states.items() if s == "invalid")
    if invalid_rows and not skip_invalid:
        raise ConflictError(
            "Fix the errors or choose to skip invalid rows",
            code="has_errors",
            details={"error_rows": len(invalid_rows), "rows": invalid_rows[:50]},
        )
    rows = await _file_rows(db, batch, k)
    if "workers" in k.depends_on:
        missing: list[int] = []
        cache: dict[str, uuid.UUID | None] = {}
        for i, row in enumerate(rows, start=1):
            email = (row.get("worker_email") or "").strip().lower()
            if states.get(i) != "valid" or not email:
                continue
            if email not in cache:
                cache[email] = await owners.worker_id(ctx, db, email)
            if cache[email] is None:
                missing.append(i)
        if missing:
            raise ConflictError(
                "Apply the Workers import first: some referenced Workers do not exist",
                code="dependency_missing",
                details={"requires": "workers", "rows": missing[:50], "missing_count": len(missing)},
            )
    handler = owners.HANDLERS[kind]
    await _check_dependencies(ctx, db, kind, rows, states, batch.id)  # Epic 52: depends_on kinds applied first
    scratch: dict[str, Any] = {}
    applied = failed = skipped = 0
    for i, row in enumerate(rows, start=1):
        if states.get(i) != "valid":
            if states.get(i) == "invalid":
                skipped += 1
                await db.execute(
                    update(_rows)
                    .where(_rows.c.batch_id == batch.id, _rows.c.row_no == i)
                    .values(status="skipped")
                )
            continue
        scratch["row_no"] = i
        values: dict[str, Any]
        try:
            async with db.begin_nested():
                ref = await handler(ctx, db, row, batch.id, validate_only=False, scratch=scratch)
            values = {"status": "applied", "result_ref": (ref or "")[:80] or None, "errors": []}
            applied += 1
        except Exception as exc:
            errs = owners.from_any(exc) if not isinstance(exc, owners.WorkerPending) else owners.pending_errors(exc)
            values = {"status": "failed", "errors": errs}
            failed += 1
        await db.execute(
            update(_rows).where(_rows.c.batch_id == batch.id, _rows.c.row_no == i).values(**values)
        )
    await imports_service.mark_applied(ctx, db, batch.id)
    batch.summary = {**(batch.summary or {}), "applied": applied, "failed": failed, "skipped": skipped}
    await db.flush()
    audit.record(
        "golive.applied", "file_import_batch", batch.id, session=db,
        kind=kind, applied=applied, failed=failed, skipped=skipped, rows=len(rows),
    )  # fmt: skip
    emit(
        db,
        GoLiveImportApplied(
            tenant_id=batch.tenant_id,
            batch_id=batch.id,
            kind=kind,
            applied_count=applied,
            failed_count=failed,
            skipped_count=skipped,
        ),
    )
    await db.refresh(batch)
    return await _out(db, batch)


async def discard(ctx: RequestContext, db: AsyncSession, batch_id: uuid.UUID) -> dict[str, Any]:
    batch = await _batch(db, batch_id)
    await imports_service.discard(ctx, db, batch.id)
    await db.refresh(batch)
    return await _out(db, batch)
