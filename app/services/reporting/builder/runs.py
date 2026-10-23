# ruff: file-ignore[line-too-long]
"""Interactive runs: preview, run, export, download (Epic 47 stories 47.2 / 47.3 / 47.5).

* ``preview`` -- unsaved definition (``report:build``), author-validated, <= ``preview_rows`` rows.
* ``run_report`` -- a saved report run **as the viewer** (owner, or ``report:read`` + a share); a page of
  <= ``preview_rows`` rows plus the total. Pinned-dashboard renders reuse a 5-minute cache per
  ``(user, report, version)``.
* ``export_report`` -- ``report:export`` (+ ``report:finance_export`` for finance sources). Up to
  ``SYNC_MAX_ROWS`` rows are rendered now; more are queued on ``reports`` (``reports.run_export``) and the file is
  announced with ``report_delivered``. Files expire after ``link_ttl_days``.
* ``download`` -- ``GET /report-runs/{id}/download``: only the run's principal (404 for anyone else), with
  ``report:read`` and visibility re-checked; 410 after expiry; audited ``report.downloaded``.
"""

import time
import uuid
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import ConflictError, Gone, NotFound
from app.core.permissions import Principal, check
from app.models.report_builder import ReportDefinition, ReportRun
from app.schemas.report_builder import ColumnOut, ExportOut, LimitHitOut, PreviewIn, RunResultOut, VizSpec
from app.services.reporting.builder import delivery, governance
from app.services.reporting.builder.common import BUILD, EXPORT, FINANCE_EXPORT, READ, now, principal_of
from app.services.reporting.builder.definitions import validated
from app.services.reporting.builder.execute import Outcome, record_run, run_definition
from app.services.reporting.builder.sharing import can_see, visible_report
from app.services.reporting.builder.sources_p1p2 import FINANCE_SOURCES

__all__ = [
    "PIN_CACHE_SECONDS",
    "SYNC_MAX_ROWS",
    "clear_cache",
    "download",
    "export_report",
    "file_access",
    "preview",
    "run_export",
    "run_report",
]

SYNC_MAX_ROWS = 5_000
PIN_CACHE_SECONDS = 300
_cache: dict[tuple[uuid.UUID, uuid.UUID, uuid.UUID, int, int], tuple[float, RunResultOut]] = {}


def clear_cache() -> None:
    _cache.clear()


def _result(outcome: Outcome, *, report: ReportDefinition | None) -> RunResultOut:
    return RunResultOut(
        run_id=outcome.run.id if outcome.run is not None else None,
        report_id=report.id if report is not None else None,
        definition_version=report.definition_version if report is not None else None,
        columns=[ColumnOut(**c) for c in outcome.columns],
        rows=outcome.json_rows(),
        total=outcome.total,
        truncated=outcome.truncated,
        row_limit=outcome.row_limit,
        masked_fields=outcome.masked_fields,
        limit_hit=LimitHitOut(kind=outcome.limit_kind, message=outcome.limit_message or "")  # type: ignore[arg-type]
        if outcome.limit_kind
        else None,
        viz=outcome.viz,
    )


async def preview(ctx: RequestContext, db: AsyncSession, data: PreviewIn) -> RunResultOut:
    principal = principal_of(ctx)
    check(principal, BUILD)
    defn, viz = await validated(ctx, db, data.definition, data.viz)
    stored = defn.model_dump(mode="json", by_alias=True, exclude_none=True)
    outcome = await run_definition(ctx, db, definition=stored, viz=viz, mode="preview")
    return _result(outcome, report=None)


async def run_report(
    ctx: RequestContext, db: AsyncSession, report_id: uuid.UUID, *, offset: int = 0, cached: bool = False
) -> RunResultOut:
    principal = principal_of(ctx)
    report = await visible_report(db, principal, report_id)
    key = (ctx.tenant_id, principal.id, report.id, report.definition_version, offset)  # type: ignore[assignment]
    if cached:
        hit = _cache.get(key)  # type: ignore[arg-type]
        if hit is not None and hit[0] > time.monotonic():
            return hit[1]
    outcome = await run_definition(
        ctx, db, definition=report.definition, viz=VizSpec.model_validate(report.viz), mode="run",
        report=report, offset=offset,
    )  # fmt: skip
    out = _result(outcome, report=report)
    _cache[key] = (time.monotonic() + PIN_CACHE_SECONDS, out)  # type: ignore[index]
    return out


def _check_export(principal: Principal, report: ReportDefinition) -> None:
    check(principal, EXPORT)
    if report.source_key in FINANCE_SOURCES:
        check(principal, FINANCE_EXPORT)


async def _store(
    ctx: RequestContext, db: AsyncSession, report: ReportDefinition, outcome: Outcome, fmt: str
) -> ReportRun:
    from app.services.reporting.common import tenant_tz

    run = outcome.run
    assert run is not None
    tz = await tenant_tz(db)
    at = run.finished_at or now()
    data = delivery.render(
        fmt, report.name, outcome.columns, outcome.rows, tz=tz, run_at=at,
        masked_fields=outcome.masked_fields, limit_message=outcome.limit_message,
    )  # fmt: skip
    run.file_id = await delivery.store(
        ctx, db, run, data, fmt=fmt, name=delivery.filename(report.name, fmt, at, tz)
    )
    run.expires_at = at + timedelta(days=(await governance.limits(db)).link_ttl_days)
    run.delivery = "link"
    await db.flush()
    return run


def _export_out(run: ReportRun, fmt: str, mode: str, outcome: Outcome | None = None) -> ExportOut:
    ready = run.file_id is not None and run.status in ("done", "limit_hit")
    return ExportOut(
        run_id=run.id,
        mode=mode,  # type: ignore[arg-type]
        status=run.status,
        format=fmt,  # type: ignore[arg-type]
        row_count=run.row_count,
        masked_fields=list(run.masked_fields or []),
        limit_hit=LimitHitOut(kind=outcome.limit_kind, message=outcome.limit_message or "")  # type: ignore[arg-type]
        if outcome is not None and outcome.limit_kind
        else None,
        download_url=delivery.download_path(run.id) if ready else None,
    )


async def export_report(ctx: RequestContext, db: AsyncSession, report_id: uuid.UUID, fmt: str) -> ExportOut:
    principal = principal_of(ctx)
    report = await visible_report(db, principal, report_id)
    _check_export(principal, report)
    viz = VizSpec.model_validate(report.viz)
    probe = await run_definition(
        ctx, db, definition=report.definition, viz=viz, mode="export", report=report, record=False,
        max_rows=SYNC_MAX_ROWS,
    )  # fmt: skip
    if probe.truncated and probe.limit_kind is None:
        run = ReportRun(
            tenant_id=ctx.tenant_id,
            report_id=report.id,
            source_key=report.source_key,
            definition_version=report.definition_version,
            run_kind="export",
            principal_user_id=principal.id,
            status="queued",
            format=fmt,
            started_at=now(),
        )
        db.add(run)
        await db.flush()
        governance.audit_run(db, "export_queued", run)
        from app.core.events import emit
        from app.services.reporting.builder.events import ReportExportQueued

        emit(db, ReportExportQueued(tenant_id=ctx.tenant_id, run_id=run.id))  # type: ignore[arg-type]
        return _export_out(run, fmt, "async")
    run = await record_run(ctx, db, probe, report=report, mode="export", fmt=fmt)
    run = await _store(ctx, db, report, probe, fmt)
    return _export_out(run, fmt, "sync", probe)


async def run_export(db: AsyncSession, run_id: uuid.UUID) -> str:
    """Celery body of ``reports.run_export``: a queued export, re-run with the requester's current access."""
    from app.services import entity_refs
    from app.services import notifications_service as notifications

    run = await db.get(ReportRun, run_id, with_for_update=True)
    if run is None or run.status != "queued":
        return run.status if run is not None else "missing"
    run.status = "running"
    await db.flush()
    report = await db.get(ReportDefinition, run.report_id) if run.report_id else None
    principal = await entity_refs.principal_for_user(db, run.tenant_id, run.principal_user_id)
    if report is None or report.archived_at is not None or principal is None:
        return await _fail(db, run, "not_authorised")
    if (
        not await can_see(db, principal, report.id)
        or not principal.has(EXPORT)
        or (report.source_key in FINANCE_SOURCES and not principal.has(FINANCE_EXPORT))
    ):
        return await _fail(db, run, "not_authorised")
    ctx = RequestContext(tenant_id=run.tenant_id, principal=principal)
    fmt = run.format or "xlsx"
    try:
        async with db.begin_nested():
            outcome = await run_definition(
                ctx, db, definition=report.definition, viz=VizSpec.model_validate(report.viz), mode="export",
                report=report, record=False,
            )  # fmt: skip
    except Exception as exc:
        return await _fail(db, run, getattr(exc, "code", "export_failed"))
    outcome.run = run
    await record_run(ctx, db, outcome, report=report, mode="export", fmt=fmt)
    await _store(ctx, db, report, outcome, fmt)
    await notifications.deliver(
        db,
        "report_delivered",
        [run.principal_user_id],
        notifications.Content(
            title=f"Your export of '{report.name}' is ready",
            body=outcome.limit_message,
            entity_type="report_run",
            entity_id=run.id,
            link_path=delivery.download_path(run.id),
        ),
        event_id=f"report_delivered:{run.id}",
    )
    return run.status


async def _fail(db: AsyncSession, run: ReportRun, code: str) -> str:
    run.status = "failed"
    run.error_code = str(code)[:60]
    run.finished_at = now()
    await db.flush()
    governance.audit_run(db, "export_failed", run, error_code=code)
    return "failed"


async def _downloadable(db: AsyncSession, principal: Principal, run: ReportRun) -> bool:
    if run.principal_user_id != principal.id or run.file_id is None:
        return False
    if run.report_id is None:
        return True
    return principal.has(READ) and await can_see(db, principal, run.report_id)


async def download(ctx: RequestContext, db: AsyncSession, run_id: uuid.UUID) -> tuple[str, str, bytes]:
    """``(filename, content type, bytes)`` of a run's file -- its own principal only (404 otherwise)."""
    from app.services import files_service

    principal = principal_of(ctx)
    run = await db.get(ReportRun, run_id)
    if run is None or not await _downloadable(db, principal, run):
        raise NotFound("Report file not found", code="report_file_not_found")
    if run.expires_at is not None and run.expires_at <= now():
        raise Gone("This report link has expired", code="report_link_expired")
    if run.status not in ("done", "limit_hit"):
        raise ConflictError("This report file is not ready", code="report_file_not_ready")
    stored = await files_service.get_file(db, run.file_id)  # type: ignore[arg-type]
    if stored is None:
        raise Gone("This report file has been removed", code="report_link_expired")
    data = await files_service.read_file(stored)
    audit.record(
        "report.downloaded",
        "report_run",
        run.id,
        session=db,
        run_id=str(run.id),
        report_id=str(run.report_id) if run.report_id else None,
        recipient=str(principal.id),
    )
    return stored.original_name or f"report.{run.format}", stored.content_type, data


async def file_access(ctx: RequestContext, db: AsyncSession, run_id: uuid.UUID, write: bool) -> bool:
    """``files`` access hook for report files: the run's principal only, until expiry."""
    principal = ctx.principal
    if write or not isinstance(principal, Principal) or principal.type != "user":
        return False
    run = await db.get(ReportRun, run_id)
    if run is None or (run.expires_at is not None and run.expires_at <= now()):
        return False
    return await _downloadable(db, principal, run)
