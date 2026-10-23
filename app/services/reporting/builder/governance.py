# ruff: file-ignore[line-too-long]
"""Report builder governance (Epic 47 story 47.5, FR-240, NFR-39/40).

* Limits: ``report_builder_settings`` (one row per tenant, lazily defaulted). A tenant may *lower* the platform
  caps (500-row preview, 100,000-row export, 30 s statement timeout), never raise them.
* Limit messages name the limit that stopped a run ("Stopped at 100,000 rows ...").
* Audit: every preview / run / export / download / share / schedule change writes ``report.<action>``.
* Run log: ``GET /reports/custom/runs`` (``audit:read``, Tenant Admin by default), filterable.
"""

import dataclasses
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.pagination import Page, PageParams, paginate
from app.core.permissions import check
from app.models.report_builder import (
    PLATFORM_EXPORT_ROW_CAP,
    PLATFORM_PREVIEW_ROWS,
    PLATFORM_TIMEOUT_S,
    ReportBuilderSettings,
    ReportDefinition,
    ReportRun,
)
from app.schemas.report_builder import BuilderSettingsIn, BuilderSettingsOut, RunLogOut
from app.services.reporting.builder.common import SHARE_TENANT, principal_of

__all__ = [
    "RUN_LOG_PERMISSION",
    "Limits",
    "audit_run",
    "get_settings",
    "limit_message",
    "limits",
    "list_runs",
    "put_settings",
]

RUN_LOG_PERMISSION = "audit:read"


@dataclasses.dataclass(frozen=True, slots=True)
class Limits:
    preview_rows: int = PLATFORM_PREVIEW_ROWS
    export_row_cap: int = PLATFORM_EXPORT_ROW_CAP
    statement_timeout_s: int = PLATFORM_TIMEOUT_S
    attachment_max_mb: int = 5
    link_ttl_days: int = 7
    max_recipients: int = 50
    max_schedules: int = 500


_FIELDS = tuple(f.name for f in dataclasses.fields(Limits))


async def limits(db: AsyncSession) -> Limits:
    row = (await db.execute(select(ReportBuilderSettings))).scalar_one_or_none()
    if row is None:
        return Limits()
    base = Limits()
    values = {name: getattr(row, name) for name in _FIELDS}
    # Never above the platform maximums, whatever the row says.
    values["preview_rows"] = min(values["preview_rows"], base.preview_rows)
    values["export_row_cap"] = min(values["export_row_cap"], base.export_row_cap)
    values["statement_timeout_s"] = min(values["statement_timeout_s"], base.statement_timeout_s)
    return Limits(**values)


async def get_settings(ctx: RequestContext, db: AsyncSession) -> BuilderSettingsOut:
    principal_of(ctx)
    return BuilderSettingsOut(**dataclasses.asdict(await limits(db)))


async def put_settings(ctx: RequestContext, db: AsyncSession, data: BuilderSettingsIn) -> BuilderSettingsOut:
    check(principal_of(ctx), SHARE_TENANT)
    row = (await db.execute(select(ReportBuilderSettings).with_for_update())).scalar_one_or_none()
    if row is None:
        row = ReportBuilderSettings(tenant_id=ctx.tenant_id, **dataclasses.asdict(Limits()))
        db.add(row)
    for name, value in data.model_dump(exclude_none=True).items():
        setattr(row, name, value)
    await db.flush()
    return BuilderSettingsOut(**dataclasses.asdict(await limits(db)))


def limit_message(kind: str, cap: int, timeout_s: int = PLATFORM_TIMEOUT_S) -> str:
    if kind == "row_cap":
        return f"Stopped at {cap:,} rows — narrow the filters or schedule a monthly split."
    return f"Stopped after {timeout_s} seconds (query time limit) — narrow the filters or group by a coarser bucket."


def audit_run(db: AsyncSession, action: str, run: ReportRun, **extra: Any) -> None:
    audit.record(
        f"report.{action}",
        "report_definition" if run.report_id else "report_run",
        run.report_id or run.id,
        session=db,
        run_id=str(run.id),
        report_id=str(run.report_id) if run.report_id else None,
        version=run.definition_version,
        source=run.source_key,
        status=run.status,
        row_count=run.row_count,
        limit_kind=run.limit_kind,
        masked_fields=list(run.masked_fields or []),
        **extra,
    )


async def purge_expired_files(db: AsyncSession, at: datetime | None = None) -> int:
    """Delete the files of expired report runs (P1 export retention); the run rows stay as the log."""
    from app.services import files_service
    from app.services.reporting.builder.common import now

    at = at or now()
    runs = (
        (
            await db.execute(
                select(ReportRun)
                .where(
                    ReportRun.file_id.is_not(None),
                    ReportRun.expires_at.is_not(None),
                    ReportRun.expires_at <= at,
                )
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    for run in runs:
        file_id = run.file_id
        run.file_id = None
        await db.flush()
        if file_id is not None:
            await files_service.purge_file(db, file_id)
    await db.flush()
    return len(runs)


def _run_out(run: ReportRun, report_name: str | None) -> RunLogOut:
    return RunLogOut(
        id=run.id,
        report_id=run.report_id,
        report_name=report_name,
        source_key=run.source_key,
        definition_version=run.definition_version,
        schedule_id=run.schedule_id,
        slot_at=run.slot_at,
        run_kind=run.run_kind,
        principal_user_id=run.principal_user_id,
        status=run.status,
        limit_kind=run.limit_kind,
        skip_reason=run.skip_reason,
        error_code=run.error_code,
        row_count=run.row_count,
        duration_ms=run.duration_ms,
        masked_fields=list(run.masked_fields or []),
        format=run.format,
        delivery=run.delivery,
        expires_at=run.expires_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


async def list_runs(
    ctx: RequestContext,
    db: AsyncSession,
    params: PageParams,
    *,
    report_id: uuid.UUID | None = None,
    status: str | None = None,
    run_kind: str | None = None,
    user_id: uuid.UUID | None = None,
    since: datetime | None = None,
) -> Page[RunLogOut]:
    check(principal_of(ctx), RUN_LOG_PERMISSION)
    stmt: Select[Any] = select(ReportRun, ReportDefinition.name).outerjoin(
        ReportDefinition, ReportDefinition.id == ReportRun.report_id
    )
    if report_id is not None:
        stmt = stmt.where(ReportRun.report_id == report_id)
    if status is not None:
        stmt = stmt.where(ReportRun.status == status)
    if run_kind is not None:
        stmt = stmt.where(ReportRun.run_kind == run_kind)
    if user_id is not None:
        stmt = stmt.where(ReportRun.principal_user_id == user_id)
    if since is not None:
        stmt = stmt.where(ReportRun.started_at >= since)
    stmt = stmt.order_by(ReportRun.started_at.desc(), ReportRun.id.desc())
    page = await paginate(db, stmt, params)
    from app.services import entity_refs

    rows = list(page.items)
    users = await entity_refs.users(db, {r[0].principal_user_id for r in rows})
    items = []
    for run, name in rows:
        out = _run_out(run, name)
        person = users.get(run.principal_user_id)
        out.principal_name = person.name if person else None
        items.append(out)
    return Page[RunLogOut](items=items, total=page.total, page=page.page, page_size=page.page_size)
