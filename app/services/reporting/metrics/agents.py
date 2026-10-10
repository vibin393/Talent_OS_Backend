# ruff: noqa: E501, N806
""" "AI agents" dashboard tiles (Epic 65 A4, FR-321, NFR-53). Gate ``ai:govern``; tenant-wide (RLS) and narrowed by
``?agent_key=``. Read through the ``p4tables`` projections only: ids, statuses, counts, tokens and cost -- never
Proposal content or conversation text. Cost is USD (``ai_prices``), no FX.

Parity (``tests/reporting/p4/test_parity.py``): ``agent_runs_by_status`` total = ``GET /agent-runs`` total for the
same range (``created_from`` / ``created_to``) and agent; ``proposal_outcomes`` = ``GET /proposals`` by status;
``agent_tokens_cost`` = ``GET /ai-governance/costs`` totals.
"""

from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Date, and_, case, cast, func, select

from app.services.reporting.metrics import TileContext, TileData, pct
from app.services.reporting.metrics.p2common import need
from app.services.reporting.metrics.p3common import label, range_link
from app.services.reporting.p4tables import (
    agent_configs,
    agent_proposals,
    agent_runs,
    agent_tool_calls,
    ai_actions_p4,
    copilot_feedback,
)

R, C, P, A = agent_runs, agent_tool_calls, agent_proposals, ai_actions_p4
RUN_STATUSES = ("queued", "running", "waiting", "completed", "stopped", "failed", "cancelled")
FINISHED = ("completed", "stopped", "failed", "cancelled")
OUTCOMES = ("approved", "edited", "rejected", "expired", "stale", "execution_failed")


def usd(micros: Any) -> str:
    return f"{Decimal(int(micros or 0)) / Decimal(1_000_000):.2f}"


def _runs_where(t: TileContext) -> list[Any]:
    f = t.filters
    conds = [R.c.created_at >= f.start, R.c.created_at < f.end]
    if f.agent_key:
        conds.append(R.c.agent_key == f.agent_key)
    return conds


def _props_where(t: TileContext) -> list[Any]:
    f = t.filters
    conds = [P.c.origin == "agent", P.c.created_at >= f.start, P.c.created_at < f.end]
    if f.agent_key:
        conds.append(P.c.agent_key == f.agent_key)
    return conds


def _actions_where(t: TileContext) -> list[Any]:
    f = t.filters
    conds = [A.c.kind == "agent_step", A.c.created_at >= f.start, A.c.created_at < f.end]
    if f.agent_key:
        conds.append(A.c.agent_key == f.agent_key)
    return conds


def _outcome_expr() -> Any:
    approved = P.c.status.in_(("approved", "executed"))
    return case((and_(approved, P.c.edited.is_(True)), "edited"), (approved, "approved"), else_=P.c.status)


# ---------------------------------------------------------------- tiles
async def agent_runs_by_status(t: TileContext) -> TileData:
    need(t, "agent_runs")
    counts = dict(
        (await t.db.execute(select(R.c.status, func.count()).where(*_runs_where(t)).group_by(R.c.status)))
        .tuples()
        .all()
    )
    rows = [{"key": s, "label": label(s), "value": int(counts.get(s, 0))} for s in RUN_STATUSES]
    q = {"agent_key": t.filters.agent_key} if t.filters.agent_key else {}
    return {
        "value": sum(int(v) for v in counts.values()), "rows": rows,
        "row_links": [range_link(t, "/admin/ai/runs", status=s, **q) for s in RUN_STATUSES],
        "link": range_link(t, "/admin/ai/runs", **q),
    }  # fmt: skip


async def _per_agent_runs(t: TileContext) -> list[tuple[Any, ...]]:
    finished = R.c.status.in_(FINISHED)
    stmt = (
        select(
            R.c.agent_key,
            func.count().label("runs"),
            func.count().filter(finished).label("finished"),
            func.count().filter(R.c.status == "completed").label("completed"),
            func.count().filter(R.c.status == "failed").label("failed"),
            func.count().filter(and_(finished, R.c.fallback_task_id.is_not(None))).label("fallback"),
            func.coalesce(func.sum(R.c.input_tokens + R.c.output_tokens), 0).label("tokens"),
            func.coalesce(func.sum(R.c.cost_micros), 0).label("cost"),
        )
        .where(*_runs_where(t))
        .group_by(R.c.agent_key)
        .order_by(R.c.agent_key)
    )
    return list((await t.db.execute(stmt)).tuples())


async def agent_success_rates(t: TileContext) -> TileData:
    need(t, "agent_runs")
    rows = await _per_agent_runs(t)
    fin = sum(int(r[2]) for r in rows)
    return {
        "value": pct(sum(int(r[3]) for r in rows), fin), "unit": "%", "n": fin,
        "rows": [
            {"key": r[0], "label": r[0], "value": pct(int(r[3]), int(r[2])), "failure_pct": pct(int(r[4]), int(r[2])),
             "fallback_pct": pct(int(r[5]), int(r[2]))}
            for r in rows
        ],
    }  # fmt: skip


async def agent_scorecard(t: TileContext) -> TileData:
    need(t, "agent_runs", "agent_proposals", "agent_tool_calls")
    runs = {r[0]: r for r in await _per_agent_runs(t)}
    outcome = _outcome_expr()
    props = (
        await t.db.execute(
            select(P.c.agent_key, outcome, func.count())
            .where(*_props_where(t))
            .group_by(P.c.agent_key, outcome)
        )
    ).tuples()
    by_agent: dict[str, dict[str, int]] = {}
    for key, o, n in props:
        by_agent.setdefault(key, {})[str(o)] = int(n)
    hours = func.extract("epoch", P.c.decided_at - P.c.created_at) / 3600.0
    medians = dict(
        (
            await t.db.execute(
                select(P.c.agent_key, func.percentile_cont(0.5).within_group(hours))
                .where(*_props_where(t), P.c.decided_at.is_not(None)).group_by(P.c.agent_key)
            )
        ).tuples().all()
    )  # fmt: skip
    autos = dict(
        (
            await t.db.execute(
                select(R.c.agent_key, func.count())
                .select_from(C.join(R, R.c.id == C.c.run_id))
                .where(*_runs_where(t), C.c.outcome == "executed", C.c.mode == "auto", C.c.risk == "low")
                .group_by(R.c.agent_key)
            )
        ).tuples().all()
    )  # fmt: skip
    out: list[dict[str, Any]] = []
    for key in sorted(set(runs) | set(by_agent)):
        r = runs.get(key, (key, 0, 0, 0, 0, 0, 0, 0))
        o = by_agent.get(key, {})
        decided = sum(o.get(k, 0) for k in ("approved", "edited", "rejected"))
        total_props = sum(o.values())
        out.append({
            "key": key, "label": key, "runs": int(r[1]), "success_pct": pct(int(r[3]), int(r[2])),
            "failure_pct": pct(int(r[4]), int(r[2])), "fallback": int(r[5]), "proposals": total_props,
            "acceptance_pct": pct(o.get("approved", 0) + o.get("edited", 0), decided),
            "edit_pct": pct(o.get("edited", 0), decided), "rejection_pct": pct(o.get("rejected", 0), decided),
            "expired_pct": pct(o.get("expired", 0), total_props),
            "median_decision_hours": round(float(medians[key]), 1) if medians.get(key) is not None else None,
            "auto_actions": int(autos.get(key, 0)), "tokens": int(r[6]), "cost_usd": usd(r[7]),
            "cost_per_run_usd": usd(int(r[7]) / int(r[1])) if int(r[1]) else None,
        })  # fmt: skip
    return {"value": len(out), "rows": out}


async def proposal_outcomes(t: TileContext) -> TileData:
    need(t, "agent_proposals")
    outcome = _outcome_expr()
    counts = dict(
        (await t.db.execute(select(outcome, func.count()).where(*_props_where(t)).group_by(outcome)))
        .tuples()
        .all()
    )
    rows = [{"key": k, "label": label(k), "value": int(counts.get(k, 0))} for k in OUTCOMES]
    return {
        "value": sum(r["value"] for r in rows),
        "rows": rows,
        "link": range_link(t, "/admin/ai/proposals"),
    }


async def time_to_decision(t: TileContext) -> TileData:
    need(t, "agent_proposals")
    hours = func.extract("epoch", P.c.decided_at - P.c.created_at) / 3600.0
    p50, p90, n = (
        await t.db.execute(
            select(func.percentile_cont(0.5).within_group(hours), func.percentile_cont(0.9).within_group(hours), func.count())
            .where(*_props_where(t), P.c.decided_at.is_not(None))
        )
    ).one()  # fmt: skip
    return {
        "value": round(float(p50), 1) if p50 is not None else None, "unit": "h", "n": int(n),
        "rows": [{"key": "p50", "label": "Median", "value": round(float(p50), 1) if p50 is not None else None},
                 {"key": "p90", "label": "90th percentile", "value": round(float(p90), 1) if p90 is not None else None}],
    }  # fmt: skip


async def agent_tokens_cost(t: TileContext) -> TileData:
    day = cast(func.date_trunc("day", A.c.created_at), Date)
    rows = (
        await t.db.execute(
            select(day, func.coalesce(A.c.agent_key, "unknown"), func.coalesce(func.sum(A.c.cost_micros), 0),
                   func.coalesce(func.sum(func.coalesce(A.c.input_tokens, 0) + func.coalesce(A.c.output_tokens, 0)), 0))
            .where(*_actions_where(t)).group_by(day, A.c.agent_key).order_by(day)
        )
    ).tuples()  # fmt: skip
    series: dict[str, list[dict[str, Any]]] = {}
    total_micros = 0
    total_tokens = 0
    for d, key, micros, tokens in rows:
        series.setdefault(str(key), []).append(
            {"bucket": d.isoformat(), "value": float(usd(micros)), "tokens": int(tokens)}
        )
        total_micros += int(micros)
        total_tokens += int(tokens)
    return {
        "value": float(usd(total_micros)), "unit": "USD",
        "rows": [{"key": "tokens", "label": "Tokens", "value": total_tokens},
                 {"key": "cost_micros", "label": "Cost (micro-USD)", "value": total_micros}],
        "series": [{"key": k, "label": k, "points": pts} for k, pts in sorted(series.items())],
        "link": range_link(t, "/admin/ai/costs"),
    }  # fmt: skip


async def copilot_ratings(t: TileContext) -> TileData:
    need(t, "copilot_feedback")
    f = t.filters
    F = copilot_feedback
    helpful, n = (
        await t.db.execute(
            select(func.count().filter(F.c.rating == "helpful"), func.count()).where(F.c.created_at >= f.start, F.c.created_at < f.end)
        )
    ).one()  # fmt: skip
    return {
        "value": pct(int(helpful), int(n)),
        "unit": "%",
        "n": int(n),
        "link": range_link(t, "/admin/ai/copilot-usage"),
    }


async def policy_violations_7d(t: TileContext) -> TileData:
    need(t, "agent_runs", "agent_tool_calls")
    since = t.now - timedelta(days=7)
    conds = [C.c.created_at >= since, C.c.outcome == "policy_violation"]
    if t.filters.agent_key:
        conds.append(R.c.agent_key == t.filters.agent_key)
    calls = int(
        (
            await t.db.execute(
                select(func.count()).select_from(C.join(R, R.c.id == C.c.run_id)).where(*conds)
            )
        ).scalar_one()
    )
    run_conds = [R.c.created_at >= since, R.c.reason_code == "policy_violation"]
    if t.filters.agent_key:
        run_conds.append(R.c.agent_key == t.filters.agent_key)
    runs = int((await t.db.execute(select(func.count()).where(*run_conds))).scalar_one())
    return {"value": calls + runs, "link": {"path": "/admin/ai/violations", "query": {}}}


async def top_failure_reasons(t: TileContext) -> TileData:
    need(t, "agent_runs")
    stmt = (
        select(R.c.reason_code, R.c.agent_key, func.count())
        .where(*_runs_where(t), R.c.status.in_(("failed", "stopped")), R.c.reason_code.is_not(None))
        .group_by(R.c.reason_code, R.c.agent_key).order_by(func.count().desc()).limit(20)
    )  # fmt: skip
    rows = [
        {"key": f"{a}:{r}", "label": label(str(r)), "agent_key": a, "value": int(n)}
        for r, a, n in (await t.db.execute(stmt)).tuples()
    ]
    return {"value": sum(r["value"] for r in rows), "rows": rows}


async def budget_usage(t: TileContext) -> TileData:
    need(t, "agent_configs")
    month_start = t.now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    budgets = dict(
        (await t.db.execute(select(agent_configs.c.agent_key, agent_configs.c.monthly_token_budget)))
        .tuples()
        .all()
    )
    used = dict(
        (
            await t.db.execute(
                select(A.c.agent_key, func.coalesce(func.sum(func.coalesce(A.c.input_tokens, 0) + func.coalesce(A.c.output_tokens, 0)), 0))
                .where(A.c.kind == "agent_step", A.c.created_at >= month_start).group_by(A.c.agent_key)
            )
        ).tuples().all()
    )  # fmt: skip
    keys = sorted(k for k in set(budgets) | set(used) if k and not str(k).startswith("_"))
    if t.filters.agent_key:
        keys = [k for k in keys if k == t.filters.agent_key]
    rows = [
        {"key": k, "label": k, "budget": budgets.get(k), "used": int(used.get(k, 0)),
         "pct": pct(int(used.get(k, 0)), budgets.get(k)) if budgets.get(k) else None}
        for k in keys
    ]  # fmt: skip
    return {"value": len(rows), "rows": rows, "link": {"path": "/admin/ai/costs", "query": {}}}


__all__ = [
    "agent_runs_by_status",
    "agent_scorecard",
    "agent_success_rates",
    "agent_tokens_cost",
    "budget_usage",
    "copilot_ratings",
    "policy_violations_7d",
    "proposal_outcomes",
    "time_to_decision",
    "top_failure_reasons",
    "usd",
]
