# ruff: file-ignore[line-too-long]
"""Viewer-scoped report execution (Epic 47 story 47.2, FR-237, NFR-39).

``run_definition(ctx, db, ...)`` -- every run executes **as the viewer** (``ctx.principal``; for scheduled
runs the recipient's principal), never as the report owner:

1. **Repair check** (saved reports): a definition referencing a removed source / field / join / operator ->
   ``needs_repair`` (persisted in its own transaction, owner notified once) and 409 ``report_needs_repair``.
2. **Viewer adaptation** (``viewer.viewer_definition`` over ``catalogue_for(viewer)``): measures over masked
   fields are removed (with dependent ratios) and reported in ``masked_fields``; masked ``fields`` come back
   ``null``; a masked field in filters / group_by is refused by the foundation (422
   ``semantic_field_forbidden``) -- a run never silently drops a filter.
3. **Compile + execute** through ``core.semantic`` (``SemanticContext(purpose="report")``): read-only
   savepoint, 30 s statement timeout, bound parameters only.
4. **Limits**: preview / run return a page of ``preview_rows`` (``truncated`` = more rows exist); export /
   scheduled runs read at most ``export_row_cap`` rows and record ``limit_hit row_cap`` when more exist;
   ``SemanticTimeout`` -> ``limit_hit timeout``.

There is no SQL text in this package (``test_no_raw_sql``): the semantic layer builds every statement.
"""

import dataclasses
import time
import uuid
from typing import Any, Literal

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError
from app.core.permissions import Principal
from app.core.semantic import (
    SemanticContext,
    SemanticResult,
    SemanticTimeout,
    catalogue_for,
    compile,
    execute,
    get_source,
    validate,
)
from app.models.report_builder import ReportDefinition, ReportRun
from app.schemas.report_builder import VizSpec
from app.services.reporting.builder import governance
from app.services.reporting.builder.common import jsonable, now
from app.services.reporting.builder.validate import repair_issues
from app.services.reporting.builder.viewer import viewer_definition

__all__ = ["Mode", "Outcome", "column_pii", "mark_needs_repair", "record_run", "run_definition"]

Mode = Literal["preview", "run", "export", "scheduled"]
_EXPORT_MODES = frozenset({"export", "scheduled"})


@dataclasses.dataclass(slots=True)
class Outcome:
    columns: list[dict[str, Any]]
    rows: list[dict[str, Any]]  # raw values (Decimal, date, ...) -- ``json_rows`` for the API
    total: int | None
    truncated: bool
    row_limit: int
    masked_fields: list[str]
    limit_kind: str | None
    limit_message: str | None
    viz: VizSpec
    run: ReportRun | None
    pii_classes: frozenset[str]
    source_key: str = ""
    duration_ms: int = 0

    def json_rows(self) -> list[dict[str, Any]]:
        return [{k: jsonable(v) for k, v in row.items() if k != "subject_id"} for row in self.rows]


def _catalogue(principal: Principal, source_key: str) -> Any:
    return next((c for c in catalogue_for(principal) if c.key == source_key), None)


def column_pii(source_key: str, defn: Any, masked: set[str]) -> frozenset[str]:
    """Pii classes of the *unmasked* output columns (fields, group-bys and measure fields)."""
    try:
        src = get_source(source_key)
    except KeyError:
        return frozenset()
    keys = [*defn.fields, *(g.field for g in defn.group_by), *(m.field for m in defn.measures if m.field)]
    return frozenset(src.fields[k].pii_class for k in keys if k in src.fields and k not in masked)


async def mark_needs_repair(tenant_id: uuid.UUID, report_id: uuid.UUID, issues: list[dict[str, Any]]) -> None:
    """Flag a report (own transaction, so the flag survives the 409), pause its schedules, notify the owner once."""
    from app.core.database import tenant_session
    from app.services.reporting.builder import repair

    try:
        async with tenant_session(tenant_id) as db:
            report = await db.get(ReportDefinition, report_id, with_for_update=True)
            if report is not None:
                await repair.flag(db, report, issues)
    except Exception:  # the 409 is what matters to the caller
        logger.exception("Could not flag report {} as needs_repair", report_id)


async def run_definition(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    definition: dict[str, Any],
    viz: VizSpec,
    mode: Mode,
    report: ReportDefinition | None = None,
    run: ReportRun | None = None,
    offset: int = 0,
    fmt: str | None = None,
    record: bool = True,
    max_rows: int | None = None,
) -> Outcome:
    """Execute ``definition`` as ``ctx.principal``; records a ``report_runs`` row (or updates ``run``).

    ``max_rows`` (export probe) reads fewer rows than the export cap; ``truncated`` then means "more rows exist"
    and no ``row_cap`` limit is recorded.
    """
    principal = ctx.principal
    assert isinstance(principal, Principal)
    if report is not None:
        issues = repair_issues(definition)
        if issues:
            await mark_needs_repair(report.tenant_id, report.id, issues)
            raise ConflictError(
                "This report needs repair: it uses fields that no longer exist",
                code="report_needs_repair",
                details={"issues": issues},
            )
    defn = validate(definition)
    caps = await governance.limits(db)
    adapted = viewer_definition(defn, viz, _catalogue(principal, defn.source))
    sctx = SemanticContext(ctx, db, "report")
    cq = await compile(adapted.definition, sctx)  # 403 / 422 for this viewer
    cap = caps.export_row_cap if mode in _EXPORT_MODES else caps.preview_rows
    limit = min(cap, max_rows) if max_rows is not None else cap
    started = time.monotonic()
    limit_kind: str | None = None
    result: SemanticResult | None = None
    try:
        result = await execute(cq, sctx, limit=limit, offset=max(0, offset))
    except SemanticTimeout:
        limit_kind = "timeout"
    if result is not None and mode in _EXPORT_MODES and result.truncated and limit >= cap:
        limit_kind = "row_cap"
    message = (
        governance.limit_message(limit_kind, caps.export_row_cap, caps.statement_timeout_s)
        if limit_kind
        else None
    )
    masked = sorted(set(cq.masked_fields) | set(adapted.masked))
    outcome = Outcome(
        columns=[c.model_dump() for c in cq.columns],
        rows=result.rows if result is not None else [],
        total=result.total if result is not None else None,
        truncated=result.truncated if result is not None else False,
        row_limit=limit,
        masked_fields=masked,
        limit_kind=limit_kind,
        limit_message=message,
        viz=adapted.viz,
        run=run,
        pii_classes=column_pii(defn.source, adapted.definition, set(masked)),
        source_key=defn.source,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    if record:
        await record_run(ctx, db, outcome, report=report, mode=mode, fmt=fmt)
    return outcome


async def record_run(
    ctx: RequestContext,
    db: AsyncSession,
    outcome: Outcome,
    *,
    report: ReportDefinition | None,
    mode: Mode,
    fmt: str | None = None,
    status: str | None = None,
) -> ReportRun:
    """Write (or complete) the run row of ``outcome`` and its ``report.<mode>`` audit entry."""
    principal = ctx.principal
    assert isinstance(principal, Principal)
    run = outcome.run
    if run is None:
        run = ReportRun(
            tenant_id=ctx.tenant_id,
            report_id=report.id if report is not None else None,
            source_key=outcome.source_key,
            definition_version=report.definition_version if report is not None else None,
            run_kind=mode,
            principal_user_id=principal.id,
            started_at=now(),
        )
        db.add(run)
        outcome.run = run
    run.status = status or ("limit_hit" if outcome.limit_kind else "done")
    run.limit_kind = outcome.limit_kind if run.status == "limit_hit" else None
    run.row_count = len(outcome.rows)
    run.duration_ms = outcome.duration_ms
    run.masked_fields = list(outcome.masked_fields)
    run.format = fmt
    run.finished_at = now()
    if report is not None:
        report.last_run_at = run.finished_at
    await db.flush()
    governance.audit_run(db, "scheduled_run" if mode == "scheduled" else mode, run)
    return run
