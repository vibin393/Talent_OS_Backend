# ruff: noqa: E501, ARG001
"""AI governance console read models (Epic 65 story 65.1, FR-319, NFR-47, NFR-56).

* ``list_runs`` -- the run explorer (foundation filters + trigger, multi-status, tainted, with violations).
* ``run_detail`` -- ``get_run`` + cost, step model tiers / cached tokens / cost from ``ai_actions``, a timeline, and the
  Copilot privacy strip (coordinator decision 6): Copilot runs never expose tool arguments, summaries or results here.
* ``search_tool_calls`` -- tool-call metadata (no arguments or summaries).
* ``violations`` -- refused tools, injection detections, possible cross-scope attempts.
* ``costs`` / ``summary`` -- ``ai_actions`` (``kind='agent_step'``) aggregates and header counts.

Callers gate on ``ai:govern`` (router). Every query runs in the tenant session (RLS).
"""

import uuid
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Date, String, and_, cast, false, func, literal, not_, null, or_, select, union_all
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agentic.agents import agents
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.pagination import Page, PageParams, paginate
from app.core.permissions import Principal
from app.models.agent_runtime import AgentPayload, AgentProposal, AgentRun, AgentRunStep, AgentToolCall
from app.models.ai import AIAction, AITenantSettings
from app.schemas.agentic import RunQuery, RunSummary
from app.schemas.ai_governance import (
    AgentBudgetRow,
    ConsoleStepOut,
    CostQuery,
    CostRow,
    CostSeriesPoint,
    CostsOut,
    GovernanceRunQuery,
    GovernanceSummaryOut,
    MonthTotals,
    RunDetailOut,
    TimelineItem,
    ToolCallQuery,
    ToolCallRow,
    ViolationQuery,
    ViolationRow,
)
from app.services import directory_service
from app.services.agent_runtime import runs, state
from app.services.agent_runtime.admin import agentic_state

REFUSED_CODES = ("not_allowlisted", "recipient_not_on_record", "grounding_invalid", "unknown_tool")


def _now() -> datetime:
    return datetime.now(UTC)


def usd(micros: int | None) -> str:
    return f"{Decimal(int(micros or 0)) / Decimal(1_000_000):.4f}"


def _principal(ctx: RequestContext) -> Principal:
    if not isinstance(ctx.principal, Principal):
        raise NotFound("Run not found")
    return ctx.principal


# ================================================================ runs
async def list_runs(ctx: RequestContext, db: AsyncSession, q: GovernanceRunQuery) -> Page[RunSummary]:
    stmt = select(AgentRun)
    statuses = [s for s in (q.status or "").split(",") if s]
    if statuses:
        stmt = stmt.where(AgentRun.status.in_(statuses))
    for column, value in (
        (AgentRun.agent_key, q.agent_key), (AgentRun.origin, q.origin), (AgentRun.trigger_kind, q.trigger_kind),
        (AgentRun.subject_type, q.subject_type), (AgentRun.subject_id, q.subject_id), (AgentRun.owner_user_id, q.owner_user_id),
    ):  # fmt: skip
        if value is not None:
            stmt = stmt.where(column == value)
    if q.created_from:
        stmt = stmt.where(AgentRun.created_at >= q.created_from)
    if q.created_to:
        stmt = stmt.where(AgentRun.created_at < q.created_to)
    if q.tainted:
        stmt = stmt.where(AgentRun.tainted.is_(True))
    if q.with_violations:
        stmt = stmt.where(AgentRun.policy_violations > 0)
    stmt = stmt.order_by(AgentRun.created_at.desc(), AgentRun.id)
    page = await paginate(db, stmt, PageParams(page=q.page, page_size=q.page_size))
    names = await directory_service.user_names(db, {r.owner_user_id for r in page.items})
    return Page[RunSummary](
        items=[RunSummary(**runs._summary(r, names.get(r.owner_user_id))) for r in page.items],
        total=page.total, page=page.page, page_size=page.page_size,
    )  # fmt: skip


async def run_total(ctx: RequestContext, db: AsyncSession, q: RunQuery) -> int:
    """Parity helper: the foundation list total for the same filters."""
    return (await runs.list_runs(ctx, db, q.model_copy(update={"page_size": 1}))).total


async def run_detail(ctx: RequestContext, db: AsyncSession, run_id: uuid.UUID) -> RunDetailOut:
    principal = _principal(ctx)
    base = await runs.get_run(ctx, db, run_id)
    copilot = base.origin == "copilot"
    data = base.model_dump()
    if copilot:  # the Copilot privacy rule: metadata only, whoever asks through the console
        data["result"] = None
        data["tool_calls"] = [
            {**c, "args_redacted": None, "result_summary": None} for c in data["tool_calls"]
        ]
        data["related_refs"] = []
    action_ids = [s["ai_action_id"] for s in data["steps"] if s.get("ai_action_id")]
    actions: dict[uuid.UUID, Any] = {}
    if action_ids:
        rows = await db.execute(
            select(AIAction.id, AIAction.model_tier, AIAction.cache_read_tokens, AIAction.cost_micros, AIAction.created_at)
            .where(AIAction.id.in_(action_ids))
        )  # fmt: skip
        actions = {r[0]: r for r in rows.tuples()}
    step_rows = {
        s.step_no: s
        for s in (await db.execute(select(AgentRunStep).where(AgentRunStep.run_id == run_id))).scalars()
    }
    steps: list[ConsoleStepOut] = []
    for s in data["steps"]:
        a = actions.get(s.get("ai_action_id"))
        row = step_rows.get(s["step_no"])
        steps.append(ConsoleStepOut(
            **s, model_tier=a[1] if a else None,
            cache_read_tokens=(a[2] if a else None) if a and a[2] is not None else (row.cache_read_tokens if row else None),
            cost_micros=a[3] if a else None,
        ))  # fmt: skip
    data["steps"] = steps
    has_payload = False
    if not copilot and principal.has("ai:payload_read"):
        has_payload = bool(
            (
                await db.execute(
                    select(func.count()).select_from(AgentPayload).where(AgentPayload.run_id == run_id)
                )
            ).scalar_one()
        )
    timeline = await _timeline(
        db, run_id, base.created_at, base.started_at, base.finished_at, base.status, step_rows, actions
    )
    return RunDetailOut(
        **data,
        cost_usd=usd(base.cost_micros),
        payload_available=has_payload,
        copilot_private=copilot,
        timeline=timeline,
    )


async def _timeline(
    db: AsyncSession,
    run_id: uuid.UUID,
    created: datetime,
    started: datetime | None,
    finished: datetime | None,
    status: str,
    steps: dict[int, AgentRunStep],
    actions: dict[uuid.UUID, Any],
) -> list[TimelineItem]:
    items: list[TimelineItem] = [TimelineItem(at=created, kind="status", ref=None, label="Queued")]
    if started:
        items.append(TimelineItem(at=started, kind="status", ref=None, label="Started"))
    for no, step in sorted(steps.items()):
        a = actions.get(step.ai_action_id) if step.ai_action_id else None
        at = a[4] if a else step.created_at
        items.append(TimelineItem(at=at, kind="model_call", ref=str(step.ai_action_id) if step.ai_action_id else None,
                                  label=f"Step {no} · Model call"))  # fmt: skip
    calls = (
        await db.execute(
            select(AgentToolCall.id, AgentToolCall.tool_key, AgentToolCall.outcome, AgentToolCall.created_at)
            .where(AgentToolCall.run_id == run_id)
        )
    ).tuples()  # fmt: skip
    for cid, key, outcome, at in calls:
        items.append(TimelineItem(at=at, kind="tool_call", ref=str(cid), label=f"{key} · {outcome}"))
    props = (
        await db.execute(
            select(AgentProposal.id, AgentProposal.human_id, AgentProposal.status, AgentProposal.created_at,
                   AgentProposal.decided_at, AgentProposal.origin)
            .where(AgentProposal.run_id == run_id)
        )
    ).tuples()  # fmt: skip
    for pid, human_id, pstatus, at, decided_at, _origin in props:
        items.append(TimelineItem(at=at, kind="proposal", ref=str(pid), label=f"{human_id} proposed"))
        if decided_at is not None:
            items.append(
                TimelineItem(at=decided_at, kind="decision", ref=str(pid), label=f"{human_id} {pstatus}")
            )
    if finished:
        items.append(TimelineItem(at=finished, kind="status", ref=None, label=status.capitalize()))
    order = {"status": 0, "model_call": 1, "tool_call": 2, "proposal": 3, "decision": 4}
    return sorted(items, key=lambda i: (i.at, order[i.kind]))


# ================================================================ tool calls
async def search_tool_calls(ctx: RequestContext, db: AsyncSession, q: ToolCallQuery) -> Page[ToolCallRow]:
    c, r = AgentToolCall, AgentRun
    stmt = select(
        c.id, c.run_id, r.agent_key, c.step_no, c.tool_key, c.risk, c.mode, c.mode_reason, c.outcome, c.error_code,
        c.target_type, c.target_id, c.proposal_id, c.egress, c.created_at,
    ).join(r, r.id == c.run_id)  # fmt: skip
    for column, value in (
        (r.agent_key, q.agent_key), (c.tool_key, q.tool_key), (c.outcome, q.outcome), (c.risk, q.risk),
        (c.mode, q.mode), (c.run_id, q.run_id), (c.target_type, q.target_type), (c.target_id, q.target_id),
    ):  # fmt: skip
        if value is not None:
            stmt = stmt.where(column == value)
    if q.from_:
        stmt = stmt.where(c.created_at >= q.from_)
    if q.to:
        stmt = stmt.where(c.created_at < q.to)
    stmt = stmt.order_by(c.created_at.desc(), c.id)
    page = await paginate(db, stmt, PageParams(page=q.page, page_size=q.page_size))
    return Page[ToolCallRow](
        items=[ToolCallRow(**row._mapping) for row in page.items], total=page.total, page=page.page,
        page_size=page.page_size,
    )  # fmt: skip


# ================================================================ violations
def _violation_union(kind: str) -> Any:
    c, r = AgentToolCall, AgentRun
    call_cols = (
        literal(kind).label("kind"), c.run_id.label("run_id"), r.agent_key.label("agent_key"),
        c.tool_key.label("tool_key"), c.error_code.label("error_code"), c.mode_reason.label("mode_reason"),
        r.tainted.label("tainted"), c.created_at.label("created_at"),
    )  # fmt: skip
    run_cols = (
        literal(kind).label("kind"), r.id.label("run_id"), r.agent_key.label("agent_key"),
        cast(null(), String).label("tool_key"), r.reason_code.label("error_code"), cast(null(), String).label("mode_reason"),
        r.tainted.label("tainted"), func.coalesce(r.finished_at, r.created_at).label("created_at"),
    )  # fmt: skip
    calls = select(*call_cols).join(r, r.id == c.run_id)
    run_rows = select(*run_cols)
    if kind == "refused_tool":
        return union_all(
            calls.where(c.outcome == "policy_violation"), run_rows.where(r.reason_code == "policy_violation")
        )
    if kind == "injection_detected":
        return union_all(
            calls.where(c.mode_reason == "suspicious_input"),
            run_rows.where(r.tainted.is_(True), r.policy_violations > 0),
        )
    # possible_cross_scope: a not-found target in a tainted run, or one that is neither the run subject nor related.
    pair = func.jsonb_build_array(func.jsonb_build_array(c.target_type, cast(c.target_id, String)))
    is_subject = and_(c.target_type == r.subject_type, c.target_id == r.subject_id)
    related = r.related_refs.op("@>")(cast(pair, JSONB))
    return calls.where(
        c.outcome == "not_found",
        or_(
            r.tainted.is_(True),
            and_(c.target_id.is_not(None), not_(func.coalesce(is_subject, false())), not_(related)),
        ),
    )


async def violations(ctx: RequestContext, db: AsyncSession, q: ViolationQuery) -> Page[ViolationRow]:
    sub = _violation_union(q.kind).subquery()
    stmt = select(sub)
    if q.agent_key:
        stmt = stmt.where(sub.c.agent_key == q.agent_key)
    if q.from_:
        stmt = stmt.where(sub.c.created_at >= q.from_)
    if q.to:
        stmt = stmt.where(sub.c.created_at < q.to)
    stmt = stmt.order_by(sub.c.created_at.desc(), sub.c.run_id)
    page = await paginate(db, stmt, PageParams(page=q.page, page_size=q.page_size))
    return Page[ViolationRow](
        items=[ViolationRow(**row._mapping) for row in page.items], total=page.total, page=page.page,
        page_size=page.page_size,
    )  # fmt: skip


async def violations_since(db: AsyncSession, since: datetime) -> int:
    total = 0
    for kind in ("refused_tool", "injection_detected", "possible_cross_scope"):
        sub = _violation_union(kind).subquery()
        total += int(
            (
                await db.execute(select(func.count()).select_from(sub).where(sub.c.created_at >= since))
            ).scalar_one()
        )
    return total


# ================================================================ costs
def _bounds(q: CostQuery) -> tuple[datetime, datetime]:
    today = _now().date()
    start = q.from_ or (today - timedelta(days=29))
    end = q.to or today
    return datetime.combine(start, time.min, UTC), datetime.combine(end + timedelta(days=1), time.min, UTC)


def _row(key: str, runs_n: Any, inp: Any, out: Any, cached: Any, cost: Any) -> CostRow:
    return CostRow(
        key=key, runs=int(runs_n or 0), input_tokens=int(inp or 0), output_tokens=int(out or 0),
        cache_read_tokens=int(cached or 0), cost_micros=int(cost or 0), cost_usd=usd(int(cost or 0)),
    )  # fmt: skip


def _aggs() -> tuple[Any, ...]:
    a = AIAction
    return (
        func.count(func.distinct(a.agent_run_id)),
        func.coalesce(func.sum(a.input_tokens), 0),
        func.coalesce(func.sum(a.output_tokens), 0),
        func.coalesce(func.sum(a.cache_read_tokens), 0),
        func.coalesce(func.sum(a.cost_micros), 0),
    )


async def costs(ctx: RequestContext, db: AsyncSession, q: CostQuery) -> CostsOut:
    a = AIAction
    lo, hi = _bounds(q)
    where = [a.kind == "agent_step", a.created_at >= lo, a.created_at < hi]
    if q.agent_key:
        where.append(a.agent_key == q.agent_key)
    if q.group_by == "agent":
        key_col: Any = func.coalesce(a.agent_key, "unknown")
    elif q.group_by == "day":
        key_col = cast(cast(func.date_trunc("day", a.created_at), Date), String)
    else:
        key_col = func.to_char(func.date_trunc("month", a.created_at), "YYYY-MM")
    grouped = await db.execute(select(key_col, *_aggs()).where(*where).group_by(key_col).order_by(key_col))
    rows = [_row(str(k), *rest) for k, *rest in grouped.tuples()]
    tot = (await db.execute(select(*_aggs()).where(*where))).one()
    day_col = cast(func.date_trunc("day", a.created_at), Date)
    series_rows = await db.execute(
        select(day_col, func.coalesce(a.agent_key, "unknown"), func.coalesce(func.sum(a.cost_micros), 0))
        .where(*where).group_by(day_col, a.agent_key).order_by(day_col)
    )  # fmt: skip
    series = [CostSeriesPoint(day=d, agent_key=k, cost_usd=usd(int(c))) for d, k, c in series_rows.tuples()]
    month_lo = state.month_start()
    m_runs, m_in, m_out, _cached, m_cost = (
        await db.execute(select(*_aggs()).where(a.kind == "agent_step", a.created_at >= month_lo))
    ).one()
    tenant_budget = (await db.execute(select(AITenantSettings.monthly_token_budget))).scalar_one_or_none()
    return CostsOut(
        group_by=q.group_by, rows=rows, totals=_row("total", *tot), series=series,
        month=MonthTotals(tokens=int(m_in) + int(m_out), cost_usd=usd(int(m_cost)), runs=int(m_runs)),
        tenant_monthly_token_budget=tenant_budget, agents=await agent_budgets(db),
    )  # fmt: skip


async def agent_budgets(db: AsyncSession) -> list[AgentBudgetRow]:
    month_lo = state.month_start()
    run_costs = dict(
        (
            await db.execute(
                select(AgentRun.agent_key, func.avg(AgentRun.cost_micros))
                .where(AgentRun.created_at >= month_lo, AgentRun.status.in_(("completed", "stopped", "failed", "cancelled")))
                .group_by(AgentRun.agent_key)
            )
        ).tuples().all()
    )  # fmt: skip
    out: list[AgentBudgetRow] = []
    for key, spec in sorted(agents().items()):
        if key.startswith("_") or spec.placeholder:
            continue
        config = await state.config_for(db, key)
        budget = config.monthly_token_budget if config is not None else None
        used = await state.agent_tokens_this_month(db, key)
        avg = run_costs.get(key)
        out.append(AgentBudgetRow(
            agent_key=key, agent_name=spec.name, monthly_token_budget=budget, used_this_month=used,
            pct_used=round(100.0 * used / budget, 1) if budget else None,
            expected_cost_per_run_micros=getattr(spec, "expected_cost_per_run_micros", None),
            actual_cost_per_run_micros=int(avg) if avg is not None else None,
        ))  # fmt: skip
    return out


# ================================================================ summary
async def summary(ctx: RequestContext, db: AsyncSession) -> GovernanceSummaryOut:
    now = _now()
    day_lo = datetime.combine(now.date(), time.min, UTC)
    by_status = dict(
        (
            await db.execute(
                select(AgentRun.status, func.count())
                .where(AgentRun.created_at >= day_lo)
                .group_by(AgentRun.status)
            )
        )
        .tuples()
        .all()
    )
    pending = int(
        (
            await db.execute(
                select(func.count()).select_from(AgentProposal)
                .where(AgentProposal.status == "pending", AgentProposal.origin == "agent")
            )
        ).scalar_one()
    )  # fmt: skip
    burn_in: list[str] = []
    for key in sorted(agents()):
        if key.startswith("_"):
            continue
        config = await state.config_for(db, key)
        if config is not None and config.burn_in_until is not None and config.burn_in_until > now:
            burn_in.append(key)
    return GovernanceSummaryOut(
        runs_today={str(k): int(v) for k, v in by_status.items()}, pending_proposals=pending,
        violations_7d=await violations_since(db, now - timedelta(days=7)), kill_switch=await agentic_state(ctx, db),
        burn_in_agents=burn_in,
    )  # fmt: skip


__all__ = [
    "REFUSED_CODES",
    "agent_budgets",
    "costs",
    "list_runs",
    "run_detail",
    "run_total",
    "search_tool_calls",
    "summary",
    "usd",
    "violations",
    "violations_since",
]
