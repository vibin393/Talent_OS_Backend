"""Timesheet CSV import (FR-131). Columns: ``worker_human_id`` or ``placement_id``, ``date`` (ISO),
``category``,
``hours`` (``Decimal(str)``; ``"7,5"`` is refused as ``bad_decimal``).

* ``preview``: same file (sha256) twice -> 409 ``import_duplicate_file`` (+ batch id); otherwise a
  ``file_import_batches`` row (kind ``timesheets``) holding the parsed rows and their errors
  (``unknown_placement``, ``outside_dates``, ``over_cap``, ``category_disabled``, ``timesheet_not_editable``,
  ``bad_decimal``).
* ``apply`` (route-level ``Idempotency-Key``): valid rows are written as proxy entries in one transaction, the
  affected Timesheets are submitted, and with ``mark_client_approved`` the Client step is recorded with
  ``recorded_by_user`` and the import file as evidence.
"""

import csv
import hashlib
import io
import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import ActorRef
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.models.imports import FileImportBatch
from app.models.placements import Placement
from app.models.time import Timesheet
from app.schemas.approvals import DecideIn
from app.schemas.time import COMPUTED_CATEGORIES, DayEntryIn, ImportApplyOut, ImportPreviewOut, ImportRowOut
from app.services.time import timesheets as ts_service
from app.services.time.common import DAY_MAX, ZERO, parse_hours, settings_row, tenant_of
from app.services.time.configs import config_version
from app.services.time.events import TimesheetImportApplied

KIND = "timesheets"
MAX_ROWS = 5000


async def _placement_for(
    db: AsyncSession, worker: str | None, placement_ref: str | None, on: date
) -> Placement | None:
    if placement_ref:
        try:
            pid = uuid.UUID(placement_ref)
        except ValueError:
            return None
        return (await db.execute(select(Placement).where(Placement.id == pid))).scalar_one_or_none()
    if not worker:
        return None
    from app.models.placements import WorkerProfile

    cand = (
        await db.execute(
            select(WorkerProfile.candidate_id).where(WorkerProfile.worker_human_id == worker.strip())
        )
    ).scalar_one_or_none()
    if cand is None:
        return None
    rows = list(
        (
            await db.execute(
                select(Placement)
                .where(Placement.candidate_id == cand, Placement.actual_start_date.is_not(None))
                .order_by(Placement.actual_start_date.desc())
            )
        ).scalars()
    )
    for p in rows:
        if p.actual_start_date and p.actual_start_date <= on and (p.end_date is None or p.end_date >= on):
            return p
    return rows[0] if rows else None


def _rows_out(rows: list[dict[str, Any]]) -> list[ImportRowOut]:
    return [ImportRowOut.model_validate(r) for r in rows]


def _out(batch: FileImportBatch) -> ImportPreviewOut:
    summary = batch.summary or {}
    return ImportPreviewOut(
        batch_id=batch.id,
        status=batch.status,
        row_count=batch.row_count,
        error_count=batch.error_count,
        mark_client_approved=bool(summary.get("mark_client_approved")),
        rows=_rows_out(list(summary.get("rows") or [])),
    )


async def preview(
    ctx: RequestContext, db: AsyncSession, data: bytes, filename: str | None, *, mark_client_approved: bool
) -> ImportPreviewOut:
    from app.core import storage
    from app.services import files_service, imports_service

    digest = hashlib.sha256(data).hexdigest()
    dup = (
        (
            await db.execute(
                select(FileImportBatch.id).where(
                    FileImportBatch.kind == KIND,
                    FileImportBatch.sha256 == digest,
                    FileImportBatch.status.in_(("previewed", "applied")),
                )
            )
        )
        .scalars()
        .first()
    )
    if dup is not None:
        raise ConflictError(
            "This file was already imported", code="import_duplicate_file", details={"batch_id": str(dup)}
        )
    if mark_client_approved and not (await settings_row(db, tenant_of(ctx, db))).import_allow_client_approved:
        raise ValidationFailed(
            "Importing client-approved hours is turned off", code="client_approved_import_disabled"
        )
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationFailed("The file must be UTF-8 CSV", code="unsupported_file_type") from exc
    reader = csv.DictReader(io.StringIO(text))
    fields = {f.strip().lower() for f in (reader.fieldnames or [])}
    if not {"date", "category", "hours"} <= fields or not ({"worker_human_id", "placement_id"} & fields):
        raise ValidationFailed(
            "Columns: worker_human_id or placement_id, date, category, hours", code="import_columns_missing"
        )
    rows: list[dict[str, Any]] = []
    day_totals: dict[tuple[uuid.UUID, date], Decimal] = defaultdict(lambda: ZERO)
    for i, raw in enumerate(reader, start=2):
        if len(rows) >= MAX_ROWS:
            raise ValidationFailed("Too many rows", code="import_too_large")
        rec = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        row: dict[str, Any] = {
            "row": i,
            "worker": rec.get("worker_human_id") or None,
            "placement_id": None,
            "date": None,
            "category": rec.get("category") or None,
            "hours": rec.get("hours") or None,
            "timesheet_id": None,
            "errors": [],
        }
        errors: list[str] = row["errors"]
        try:
            on = date.fromisoformat(rec.get("date", ""))
            row["date"] = on
        except ValueError:
            errors.append("bad_date")
            rows.append(row)
            continue
        hours = parse_hours(rec.get("hours"))
        if hours is None:
            errors.append("bad_decimal")
        placement = await _placement_for(db, rec.get("worker_human_id"), rec.get("placement_id"), on)
        if placement is None:
            errors.append("unknown_placement")
            rows.append(row)
            continue
        row["placement_id"] = placement.id
        if (
            placement.actual_start_date is None
            or on < placement.actual_start_date
            or (placement.end_date is not None and on > placement.end_date)
        ):
            errors.append("outside_dates")
            rows.append(row)
            continue
        await ts_service.create_period_timesheets(ctx, db, placement, up_to=max(on, date.today()))
        ts = (
            await db.execute(
                select(Timesheet).where(
                    Timesheet.placement_id == placement.id,
                    Timesheet.kind == "regular",
                    Timesheet.period_start <= on,
                    Timesheet.period_end >= on,
                )
            )
        ).scalar_one_or_none()
        if ts is None:
            errors.append("outside_dates")
            rows.append(row)
            continue
        row["timesheet_id"] = ts.id
        if ts.status not in ts_service.EDITABLE:
            errors.append("timesheet_not_editable")
        cfg = await config_version(db, ts.placement_id, ts.config_version)
        category = rec.get("category", "")
        if category in COMPUTED_CATEGORIES or category not in (cfg.enabled_categories or []):
            errors.append("category_disabled")
        if hours is not None:
            day_totals[ts.id, on] += hours
            if day_totals[ts.id, on] > min(Decimal(cfg.daily_cap), DAY_MAX):
                errors.append("over_cap")
        rows.append(row)
    error_count = sum(1 for r in rows if r["errors"])
    tenant_id = tenant_of(ctx, db)
    stored = await files_service.store_generated(
        ctx,
        db,
        data,
        key=storage.new_key(tenant_id),
        filename=filename or "timesheets.csv",
        content_type="text/csv",
        owner_entity_type="timesheet_import",
        owner_entity_id=uuid.uuid4(),
    )
    summary = {
        "mark_client_approved": mark_client_approved,
        "rows": [
            {
                **r,
                "placement_id": str(r["placement_id"]) if r["placement_id"] else None,
                "timesheet_id": str(r["timesheet_id"]) if r["timesheet_id"] else None,
                "date": r["date"].isoformat() if r["date"] else None,
            }
            for r in rows
        ],
    }
    out = await imports_service.register_preview(
        ctx, db, KIND, stored.id, digest, len(rows), error_count, summary
    )
    batch = await db.get(FileImportBatch, out.id)
    assert batch is not None
    return _out(batch)


async def get_batch(ctx: RequestContext, db: AsyncSession, batch_id: uuid.UUID) -> ImportPreviewOut:  # ruff: ignore[unused-function-argument]
    batch = (
        await db.execute(
            select(FileImportBatch).where(FileImportBatch.id == batch_id, FileImportBatch.kind == KIND)
        )
    ).scalar_one_or_none()
    if batch is None:
        raise NotFound("Import batch not found")
    return _out(batch)


async def apply(ctx: RequestContext, db: AsyncSession, batch_id: uuid.UUID) -> ImportApplyOut:
    from app.services import approvals, imports_service
    from app.services.time.approval import CLIENT_KINDS, _client_user_for, current_request

    batch = (
        await db.execute(
            select(FileImportBatch).where(FileImportBatch.id == batch_id, FileImportBatch.kind == KIND)
        )
    ).scalar_one_or_none()
    if batch is None:
        raise NotFound("Import batch not found")
    await imports_service.mark_applied(ctx, db, batch_id)
    summary = batch.summary or {}
    by_ts: dict[uuid.UUID, list[DayEntryIn]] = defaultdict(list)
    for r in summary.get("rows") or []:
        if r.get("errors") or not r.get("timesheet_id"):
            continue
        by_ts[uuid.UUID(r["timesheet_id"])].append(
            DayEntryIn(date=date.fromisoformat(r["date"]), category=r["category"], hours=r["hours"])
        )
    submitted: list[uuid.UUID] = []
    applied = 0
    user_id = getattr(ctx.principal, "id", None)
    for tid, cells in by_ts.items():
        ts = await ts_service.get_timesheet(db, tid, lock=True)
        await ts_service.upsert_entries(ctx, db, ts, cells, proxy=True)
        applied += len(cells)
        await ts_service.submit(ctx, db, ts, confirm_zero=True, proxy_reason=f"import:{batch.id}")
        submitted.append(ts.id)
        if summary.get("mark_client_approved") and batch.file_id is not None:
            request = await current_request(db, ts)
            if (
                request is not None
                and request.status == "pending"
                and request.current_approver_kind in CLIENT_KINDS
            ):
                attributed = request.current_approver_client_user_id or await _client_user_for(
                    db, ts.client_id
                )
                if attributed is not None:
                    ts.client_approval_evidence_file_id = batch.file_id
                    await approvals.decide(
                        ctx,
                        db,
                        request.id,
                        DecideIn(action="approve", comment="Client approval recorded on import"),
                        actor=ActorRef(
                            "recorded_by_user",
                            user_id,
                            attributed_to=("client_user", attributed),
                            evidence_file_id=batch.file_id,
                        ),
                    )
    await db.flush()
    emit(db, TimesheetImportApplied(tenant_id=tenant_of(ctx, db), batch_id=batch.id, applied_rows=applied))
    return ImportApplyOut(batch_id=batch.id, applied_rows=applied, submitted_timesheet_ids=submitted)
