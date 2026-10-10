"""AI administration (Epic 12 story 12.4, FR-62..FR-64): feature toggles, budget/usage, AI action log.

* ``features`` / ``update_features`` -- the enabled map (read by any user; changed with ``ai:admin``).
  Disabled features are refused by ``app.dependencies.ai.require_ai_feature`` (403 ``ai_feature_disabled``)
  and again by the gateway (``feature_disabled``); manual paths stay available.
* ``usage`` -- month-to-date tokens, budget, % and breakdowns by feature and by day.
* ``list_actions`` / ``get_action`` / ``export_csv`` -- the AI action log (``ai_log:read``); outputs erased
  by a DSR (``input_ref == "erased"``) read ``[erased]``. The CSV is capped at 50k rows and audited.
"""

import csv
import dataclasses
import io
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Query
from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import NotFound, ValidationFailed
from app.core.pagination import Page, PageParams, paginate
from app.models.ai import AI_FEATURES, AIAction
from app.services.ai import budget as budget_guard
from app.services.ai import gateway

ERASED = "[erased]"
MAX_EXPORT_ROWS = 50_000


@dataclasses.dataclass(frozen=True)
class AIActionFilters:
    feature: Annotated[str | None, Query(max_length=50)] = None
    initiator_id: Annotated[uuid.UUID | None, Query()] = None
    subject_type: Annotated[str | None, Query(max_length=50)] = None
    subject_id: Annotated[uuid.UUID | None, Query()] = None
    decision: Annotated[str | None, Query(max_length=20)] = None
    status: Annotated[str | None, Query(max_length=20)] = None
    from_: Annotated[datetime | None, Query(alias="from")] = None
    to: Annotated[datetime | None, Query()] = None
    # Epic 65 (FR-319): agent steps -- kind (single | agent_step), agent and run.
    kind: Annotated[str | None, Query(max_length=20)] = None
    agent_key: Annotated[str | None, Query(max_length=40)] = None
    agent_run_id: Annotated[uuid.UUID | None, Query()] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            ("from" if k == "from_" else k): audit.jsonable(v)
            for k, v in dataclasses.asdict(self).items()
            if v is not None
        }


async def features(ctx: RequestContext, db: AsyncSession) -> dict[str, bool]:  # noqa: ARG001
    return await gateway.features_map(db)


async def update_features(ctx: RequestContext, db: AsyncSession, changes: dict[str, bool]) -> dict[str, bool]:
    unknown = sorted(set(changes) - set(AI_FEATURES))
    if unknown:
        raise ValidationFailed(
            "Unknown AI feature", details={"fields": dict.fromkeys(unknown, "Unknown feature")}
        )
    assert ctx.tenant_id is not None
    # Epic 53: ``agentic`` / ``agent_<key>`` are managed by the agents API (notice, burn-in): ignored here.
    changes = {k: v for k, v in changes.items() if not gateway.is_agent_feature(k)}
    before = await gateway.features_map(db)
    after = await gateway.set_features(db, ctx.tenant_id, changes)
    changed = {k: after[k] for k in after if before.get(k) != after[k]}
    if changed:
        audit.record("ai.features_updated", "ai_tenant_settings", ctx.tenant_id, session=db, changed=changed)
    return after


async def is_enabled(db: AsyncSession, feature: str) -> bool:
    return await gateway.is_feature_enabled(db, feature)


async def usage(ctx: RequestContext, db: AsyncSession, month: str | None) -> dict[str, Any]:
    assert ctx.tenant_id is not None
    period = month or budget_guard.period_of()
    try:
        datetime.strptime(period, "%Y-%m").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValidationFailed("month must be YYYY-MM", details={"fields": {"month": "Use YYYY-MM"}}) from exc
    return await budget_guard.usage_report(db, ctx.tenant_id, await gateway.monthly_budget(db), period)


def _filtered(filters: AIActionFilters) -> Select[tuple[AIAction]]:
    stmt = select(AIAction)
    for field in (
        "feature",
        "initiator_id",
        "subject_type",
        "subject_id",
        "decision",
        "status",
        "kind",
        "agent_key",
    ):
        value = getattr(filters, field)
        if value is not None:
            stmt = stmt.where(getattr(AIAction, field) == value)
    if filters.agent_run_id is not None:  # Epic 65: "Run or AI action id" search matches either id
        stmt = stmt.where(or_(AIAction.agent_run_id == filters.agent_run_id, AIAction.id == filters.agent_run_id))
    if filters.from_ is not None:
        stmt = stmt.where(AIAction.created_at >= filters.from_)
    if filters.to is not None:
        stmt = stmt.where(AIAction.created_at < filters.to)
    return stmt.order_by(AIAction.created_at.desc(), AIAction.id.desc())


def _private(row: AIAction, viewer: uuid.UUID | None) -> bool:
    """Epic 65 (coordinator decision 6): a Copilot agent step's content is private to the user who asked."""
    return row.agent_key == "copilot" and (viewer is None or row.initiator_id != viewer)


def action_out(row: AIAction, viewer: uuid.UUID | None = None) -> dict[str, Any]:
    erased = row.input_ref == "erased"
    private = _private(row, viewer)
    return {
        "id": row.id,
        "feature": row.feature,
        "prompt_id": row.prompt_id,
        "prompt_version": row.prompt_version,
        "model": row.model,
        "initiator_type": row.initiator_type,
        "initiator_id": row.initiator_id,
        "subject_type": row.subject_type,
        "subject_id": row.subject_id,
        "input_ref": row.input_ref,
        "status": row.status,
        "error_code": row.error_code,
        "output": ERASED if erased else (None if private else row.output),
        "limitations_note": ERASED if erased else (None if private else row.limitations_note),
        "latency_ms": row.latency_ms,
        "input_tokens": row.input_tokens,
        "output_tokens": row.output_tokens,
        "consent_basis": row.consent_basis,
        "decision": row.decision,
        "decided_by": row.decided_by,
        "decided_at": row.decided_at,
        "created_at": row.created_at,
        # Epic 65 (FR-319): agent step metadata and the run link.
        "kind": row.kind,
        "agent_key": row.agent_key,
        "agent_run_id": row.agent_run_id,
        "step_no": row.step_no,
        "model_tier": row.model_tier,
        "cost_micros": row.cost_micros,
        "run_link": f"/admin/ai/runs/{row.agent_run_id}" if row.agent_run_id else None,
    }


def _viewer(ctx: RequestContext) -> uuid.UUID | None:
    return getattr(ctx.principal, "id", None)


async def list_actions(
    ctx: RequestContext, db: AsyncSession, filters: AIActionFilters, params: PageParams
) -> Page[dict[str, Any]]:
    page = await paginate(db, _filtered(filters), params)
    return Page[dict[str, Any]](
        items=[action_out(r, _viewer(ctx)) for r in page.items],
        total=page.total,
        page=page.page,
        page_size=page.page_size,
    )


async def get_action(ctx: RequestContext, db: AsyncSession, action_id: uuid.UUID) -> dict[str, Any]:
    row = await db.get(AIAction, action_id)
    if row is None:
        raise NotFound("AI action not found")
    return action_out(row, _viewer(ctx))


CSV_COLUMNS = (
    "id", "created_at", "feature", "prompt_version", "model", "initiator_type", "initiator_id", "subject_type",
    "subject_id", "status", "error_code", "latency_ms", "input_tokens", "output_tokens", "consent_basis",
    "decision", "decided_at",
    # Epic 65: agent steps.
    "kind", "agent_key", "agent_run_id", "step_no", "model_tier", "cost_micros",
)  # fmt: skip


def _cell(value: Any) -> str:
    text = "" if value is None else (value.isoformat() if isinstance(value, datetime) else str(value))
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@dataclasses.dataclass(frozen=True)
class CsvExport:
    content: str
    rows: int
    truncated: bool


async def export_csv(ctx: RequestContext, db: AsyncSession, filters: AIActionFilters) -> CsvExport:  # noqa: ARG001
    rows = list((await db.execute(_filtered(filters).limit(MAX_EXPORT_ROWS + 1))).scalars())
    truncated = len(rows) > MAX_EXPORT_ROWS
    rows = rows[:MAX_EXPORT_ROWS]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        writer.writerow([_cell(getattr(row, c)) for c in CSV_COLUMNS])
    audit.record(
        "ai.actions_exported", "ai_action", None, session=db, rows=len(rows), filters=filters.as_dict()
    )
    return CsvExport(buf.getvalue(), len(rows), truncated)
