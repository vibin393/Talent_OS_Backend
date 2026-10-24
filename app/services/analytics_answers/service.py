# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Analytics answers: run, validate, store and re-preview governed questions (Epic 64, FR-316..FR-318, NFR-46).

The model controls only a foundation ``Definition`` + a report ``VizSpec`` + short labels. Pipeline of ``run_question``:

1. pydantic parse (already done by the tool input model: ``extra='forbid'``, closed operators);
2. ``core.semantic.validate`` (structure, registered keys, operators) -> 422 ``semantic_*`` with ``details.path``;
3. ``reporting.builder.validate.check_viz`` -> 422 ``report_viz_invalid``;
4. ``reporting.builder.execute.run_definition(mode="preview", record=False, max_rows=MODEL_ROWS_MAX)`` **as the
   caller** (P3 viewer adaptation: masked measures removed, masked fields ``null``; a masked field in filters / group-by
   is refused 422 ``semantic_field_forbidden`` by the foundation compile under the caller's principal; read-only
   savepoint, 30 s statement timeout);
5. minimum-cell suppression (``cells``);
6. persist the answer (definition, viz, explanation, counts -- **never rows**);
7. return <= ``MODEL_ROWS_MAX`` (50) model-facing rows; masked values stay ``null`` and are named in ``masked_fields``.

``ctx`` is the caller's context: for tools, ``agent_runtime_service.scope_context`` (Effective Rights presented as the
Accountable Owner, actor ``agent``); for the API, the signed-in user. There is no SQL text here (``test_no_raw_sql``).
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import ConflictError, Forbidden, NotFound, ValidationFailed
from app.core.pagination import Page, PageParams
from app.core.permissions import Principal
from app.core.semantic import (
    SemanticContext,
    SemanticError,
    SemanticTimeout,
    catalogue_for,
    compile,
    validate,
)
from app.core.semantic.schema import Definition
from app.models.analytics_answers import AnalyticsAnswer
from app.schemas.analytics_answers import (
    AnswerColumnOut,
    AnswerOut,
    AnswerSummaryOut,
    CatalogueOut,
    IssueOut,
    LimitHitOut,
    ReportGetOut,
    ReportListRowOut,
    ReportsListIn,
    ReportsListOut,
    RunQuestionIn,
    RunQuestionOut,
    ValidateIn,
    ValidateOut,
)
from app.schemas.report_builder import ColumnOut, RunResultOut, VizSpec
from app.schemas.report_builder import LimitHitOut as ReportLimitHitOut
from app.services.analytics_answers import catalogue as cat
from app.services.analytics_answers import cells
from app.services.analytics_answers.describe import describe
from app.services.reporting.builder.common import jsonable
from app.services.reporting.builder.execute import Outcome, run_definition
from app.services.reporting.builder.validate import REPAIR_CODES, check_viz, repair_issues
from app.services.reporting.builder.viewer import viewer_definition

__all__ = [
    "ANSWER_RETENTION_DAYS",
    "MODEL_ROWS_MAX",
    "compact_catalogue",
    "conversation_of_run",
    "explain",
    "get_answer",
    "get_report",
    "list_answers",
    "list_reports",
    "mark_saved",
    "preview_answer",
    "run_question",
    "validate_only",
]

MODEL_ROWS_MAX = 50  # PRD default and platform maximum (raising it is "Ask First")
ANSWER_RETENTION_DAYS = 90  # aligned with Copilot retention (FR-279)
NEEDS_REPAIR_MESSAGE = "This answer refers to a field that no longer exists. Ask again."


def _now() -> datetime:
    return datetime.now(UTC)


def _principal(ctx: RequestContext) -> Principal:
    principal = ctx.principal
    if not isinstance(principal, Principal) or principal.type != "user":
        raise Forbidden("Analytics answers are available to internal users only")
    return principal


def _stored(defn: Definition) -> dict[str, Any]:
    return defn.model_dump(mode="json", by_alias=True, exclude_none=True)


def explain(defn: Definition) -> str:
    """The deterministic "How was this calculated?" text (``describe`` over the registry labels)."""
    return describe(defn, cat.labels_for(defn.source))


# ================================================================ catalogue / validate
async def compact_catalogue(ctx: RequestContext, db: AsyncSession, source_key: str | None) -> CatalogueOut:  # ruff: ignore[unused-function-argument]  # NOSONAR
    return cat.compact(_principal(ctx), source_key)


def _issue(exc: Exception) -> IssueOut:
    if isinstance(exc, SemanticError):
        return IssueOut(code=exc.code, path=exc.path, message=exc.message)
    if isinstance(exc, ValidationFailed):
        details = getattr(exc, "details", None) or {}
        return IssueOut(code=exc.code, path=str(details.get("path", "")), message=exc.message)
    if isinstance(exc, Forbidden):
        return IssueOut(code="forbidden", path="source", message=str(getattr(exc, "message", "Forbidden")))
    return IssueOut(code="invalid", path="", message=str(exc))


async def _checked(
    ctx: RequestContext, db: AsyncSession, defn: Definition, viz: VizSpec
) -> tuple[Definition, VizSpec]:
    """Structure + viz keys + the caller's field permissions (dry compile of the viewer-adapted definition)."""
    principal = _principal(ctx)
    parsed = validate(_stored(defn))
    viz = check_viz(viz, parsed)
    catalogue = next((c for c in catalogue_for(principal) if c.key == parsed.source), None)
    adapted = viewer_definition(parsed, viz, catalogue)
    await compile(
        adapted.definition, SemanticContext(ctx, db, "report")
    )  # 403 / 422 for this caller; never executes
    return parsed, viz


async def validate_only(ctx: RequestContext, db: AsyncSession, data: ValidateIn) -> ValidateOut:
    try:
        await _checked(ctx, db, data.definition, data.viz)
    except (SemanticError, ValidationFailed, Forbidden) as exc:
        return ValidateOut(valid=False, errors=[_issue(exc)])
    return ValidateOut(valid=True, errors=[])


# ================================================================ run
def _model_columns(outcome: Outcome) -> list[AnswerColumnOut]:
    return [AnswerColumnOut(**c) for c in outcome.columns]


async def run_question(
    ctx: RequestContext,
    db: AsyncSession,
    data: RunQuestionIn,
    *,
    conversation_id: uuid.UUID | None,
    run_id: uuid.UUID | None,
) -> RunQuestionOut:
    principal = _principal(ctx)
    defn, viz = await _checked(ctx, db, data.definition, data.viz)
    stored = _stored(defn)
    outcome = await run_definition(
        ctx, db, definition=stored, viz=viz, mode="preview", record=False, max_rows=MODEL_ROWS_MAX
    )
    if outcome.limit_kind == "timeout":
        raise ConflictError(
            "This question took too long to answer. Try narrowing it (for example a shorter date range).",
            code="query_timeout",
        )
    rows, suppressed = cells.suppress(defn.source, defn, outcome.json_rows())
    explanation = explain(defn)
    answer = AnalyticsAnswer(
        tenant_id=ctx.tenant_id,
        user_id=principal.id,
        conversation_id=conversation_id,
        run_id=run_id,
        question=data.question[:500],
        assumptions=list(data.assumptions),
        source_key=defn.source,
        definition=stored,
        definition_version=1,
        viz=viz.model_dump(mode="json"),
        explanation=explanation,
        masked_fields=list(outcome.masked_fields),
        row_count=outcome.total,
        truncated=outcome.truncated,
        limit_kind=None,
        suppressed_cells=suppressed,
        status="ok",
        expires_at=_now() + timedelta(days=ANSWER_RETENTION_DAYS),
    )
    db.add(answer)
    await db.flush()
    audit.record(
        "analytics.answer_run",
        "analytics_answer",
        answer.id,
        session=db,
        answer_id=str(answer.id),
        source_key=defn.source,
        row_count=outcome.total,
        masked_fields=list(outcome.masked_fields),
        limit_kind=None,
        suppressed_cells=suppressed,
    )
    columns = _model_columns(outcome)
    visible = [r for r in rows if not r.get("suppressed")]
    return RunQuestionOut(
        answer_id=answer.id,
        columns=columns,
        rows=[[r.get(c.key) for c in columns] for r in visible[:MODEL_ROWS_MAX]],
        total=outcome.total,
        truncated=outcome.truncated,
        masked_fields=list(outcome.masked_fields),
        limit_hit=LimitHitOut(kind=outcome.limit_kind, message=outcome.limit_message or "")
        if outcome.limit_kind
        else None,
        suppressed_cells=suppressed,
        explanation=explanation,
    )


# ================================================================ answers (owner only)
async def _owned(
    ctx: RequestContext, db: AsyncSession, answer_id: uuid.UUID, *, lock: bool = False
) -> AnalyticsAnswer:
    principal = _principal(ctx)
    stmt = select(AnalyticsAnswer).where(
        AnalyticsAnswer.id == answer_id, AnalyticsAnswer.user_id == principal.id
    )
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Answer not found", code="analytics_answer_not_found")
    return row


async def _report_name(ctx: RequestContext, db: AsyncSession, report_id: uuid.UUID | None) -> str | None:
    if report_id is None:
        return None
    from app.services.reporting import builder

    try:
        return (await builder.get(ctx, db, report_id)).name
    except (NotFound, Forbidden):
        return None


async def _answer_out(ctx: RequestContext, db: AsyncSession, a: AnalyticsAnswer) -> AnswerOut:
    return AnswerOut(
        id=a.id,
        question=a.question,
        assumptions=list(a.assumptions or []),
        source_key=a.source_key,
        source_label=cat.source_label(a.source_key),
        definition=a.definition,
        definition_version=a.definition_version,
        viz=VizSpec.model_validate(a.viz),
        explanation=a.explanation,
        masked_fields=list(a.masked_fields or []),
        row_count=a.row_count,
        truncated=a.truncated,
        limit_kind=a.limit_kind,
        suppressed_cells=a.suppressed_cells,
        min_cell_size=cells.min_cell_size(a.source_key),
        status=a.status,
        saved_report_id=a.saved_report_id,
        saved_report_name=await _report_name(ctx, db, a.saved_report_id),
        conversation_id=a.conversation_id,
        run_id=a.run_id,
        created_at=a.created_at,
        expires_at=a.expires_at,
    )


async def get_answer(ctx: RequestContext, db: AsyncSession, answer_id: uuid.UUID) -> AnswerOut:
    return await _answer_out(ctx, db, await _owned(ctx, db, answer_id))


async def list_answers(
    ctx: RequestContext, db: AsyncSession, params: PageParams, *, conversation_id: uuid.UUID | None = None
) -> Page[AnswerSummaryOut]:
    principal = _principal(ctx)
    where = [AnalyticsAnswer.user_id == principal.id]
    if conversation_id is not None:
        where.append(AnalyticsAnswer.conversation_id == conversation_id)
    total = int(
        (await db.execute(select(func.count()).select_from(AnalyticsAnswer).where(*where))).scalar_one()
    )
    rows = (
        await db.execute(
            select(AnalyticsAnswer)
            .where(*where)
            .order_by(AnalyticsAnswer.created_at.desc(), AnalyticsAnswer.id)
            .limit(params.page_size)
            .offset(params.offset)
        )
    ).scalars()
    items = [
        AnswerSummaryOut(
            id=a.id,
            question=a.question,
            source_key=a.source_key,
            source_label=cat.source_label(a.source_key),
            status=a.status,
            row_count=a.row_count,
            saved_report_id=a.saved_report_id,
            conversation_id=a.conversation_id,
            run_id=a.run_id,
            created_at=a.created_at,
        )
        for a in rows
    ]
    return Page[AnswerSummaryOut](items=items, total=total, page=params.page, page_size=params.page_size)


async def _flag_needs_repair(tenant_id: uuid.UUID, answer_id: uuid.UUID) -> None:
    """Own transaction, so the flag survives the 409 (same pattern as the P3 repair flag)."""
    from loguru import logger

    from app.core.database import tenant_session

    try:
        async with tenant_session(tenant_id) as other:
            row = await other.get(AnalyticsAnswer, answer_id, with_for_update=True)
            if row is not None:
                row.status = "needs_repair"
    except Exception:  # the 409 is what matters to the caller
        logger.exception("Could not flag analytics answer {} as needs_repair", answer_id)


async def preview_answer(ctx: RequestContext, db: AsyncSession, answer_id: uuid.UUID) -> RunResultOut:
    """A fresh run **as the current viewer** (the owner) with the P3 ``preview_rows``; rows are never stored."""
    answer = await _owned(ctx, db, answer_id)
    if repair_issues(answer.definition):
        await _flag_needs_repair(answer.tenant_id, answer.id)
        raise ConflictError(NEEDS_REPAIR_MESSAGE, code="analytics_answer_needs_repair")
    viz = VizSpec.model_validate(answer.viz)
    try:
        defn = validate(answer.definition)
        outcome = await run_definition(
            ctx, db, definition=answer.definition, viz=viz, mode="preview", record=False
        )
    except SemanticError as exc:
        if exc.code in REPAIR_CODES:
            await _flag_needs_repair(answer.tenant_id, answer.id)
            raise ConflictError(NEEDS_REPAIR_MESSAGE, code="analytics_answer_needs_repair") from exc
        raise
    if outcome.limit_kind == "timeout":
        raise SemanticTimeout()
    rows, suppressed = cells.suppress(defn.source, defn, outcome.json_rows())
    audit.record(
        "analytics.answer_previewed",
        "analytics_answer",
        answer.id,
        session=db,
        answer_id=str(answer.id),
        source_key=answer.source_key,
        row_count=outcome.total,
        masked_fields=list(outcome.masked_fields),
        suppressed_cells=suppressed,
    )
    return RunResultOut(
        run_id=None,
        report_id=None,
        definition_version=None,
        columns=[ColumnOut(**c) for c in outcome.columns],
        rows=[{k: jsonable(v) for k, v in r.items()} for r in rows],
        total=outcome.total,
        truncated=outcome.truncated,
        row_limit=outcome.row_limit,
        masked_fields=list(outcome.masked_fields),
        limit_hit=ReportLimitHitOut(kind=outcome.limit_kind, message=outcome.limit_message or "")
        if outcome.limit_kind
        else None,
        viz=outcome.viz,
    )


async def mark_saved(
    ctx: RequestContext, db: AsyncSession, answer_id: uuid.UUID, report_id: uuid.UUID
) -> AnswerOut:
    """Link the answer to the report the user saved in the P3 builder (the report must be visible to them)."""
    answer = await _owned(ctx, db, answer_id, lock=True)
    from app.services.reporting import builder

    try:
        await builder.get(ctx, db, report_id)
    except NotFound:
        raise ValidationFailed(
            "That report is not visible to you", code="report_not_visible", details={"path": "report_id"}
        ) from None
    answer.saved_report_id = report_id
    await db.flush()
    audit.record(
        "analytics.answer_saved",
        "analytics_answer",
        answer.id,
        session=db,
        answer_id=str(answer.id),
        report_id=str(report_id),
        source_key=answer.source_key,
    )
    return await _answer_out(ctx, db, answer)


# ================================================================ P3 reports (read tools)
async def list_reports(ctx: RequestContext, db: AsyncSession, data: ReportsListIn) -> ReportsListOut:
    from app.services.reporting import builder

    page = await builder.list_reports(ctx, db, PageParams(page=1, page_size=50), tab="all", q=data.q or None)
    rows = [
        ReportListRowOut(
            id=r.id,
            name=r.name,
            source_key=r.source_key,
            description=(r.description or "")[:300] or None,
            needs_repair=r.status == "needs_repair",
        )
        for r in page.items
        if data.source_key is None or r.source_key == data.source_key
    ]
    return ReportsListOut(rows=rows[: data.limit])


async def get_report(ctx: RequestContext, db: AsyncSession, report_id: uuid.UUID) -> ReportGetOut:
    from app.services.reporting import builder

    r = await builder.get(ctx, db, report_id)  # 404 report_not_found unless visible
    return ReportGetOut(id=r.id, name=r.name, definition=r.definition, viz=r.viz)


async def conversation_of_run(db: AsyncSession, run_id: uuid.UUID | None) -> uuid.UUID | None:
    """The Copilot conversation of an agent run (``agent_runs.conversation_id``; ``RunOut`` does not carry it). Read by
    run id under tenant RLS -- never taken from tool arguments."""
    if run_id is None:
        return None
    from app.models.agent_runtime import AgentRun

    return (
        await db.execute(select(AgentRun.conversation_id).where(AgentRun.id == run_id))
    ).scalar_one_or_none()
