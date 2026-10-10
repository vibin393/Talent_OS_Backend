# ruff: noqa: E501, D401, N806, ARG001, N812
""" "Agent outcomes" dashboard tiles (Epic 65 A4; SM-30..SM-39, SM-C14..SM-C18). Gate ``report:read`` (internal
Users). Phase 4 tables are row-scoped by ``owner_user_id`` under the caller's ``report:read`` scope; P1-P3 facts use
the owners' scope helpers (``scoped_demand_ids`` / ``scoped_placements``). Baselines need
``ai_tenant_settings.agentic_enabled_at`` (``n/a`` otherwise). No tile shows Proposal content, conversation text,
self-ID or fairness data (SM-C16 is a link only). AI cost is USD.
"""

from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import Uuid, column, func, select, table

from app.core.permissions import scope_condition
from app.services.reporting.filters import REPORT_READ
from app.services.reporting.metrics import TileContext, TileData, pct
from app.services.reporting.metrics.agents import usd
from app.services.reporting.metrics.p2common import need
from app.services.reporting.metrics.p3common import median
from app.services.reporting.p4tables import (
    agent_proposals,
    agent_runs,
    agent_tool_calls,
    ai_actions_p4,
    ai_tenant_settings_p4,
    analytics_answers,
    audit_logs_p4,
    copilot_feedback,
    copilot_messages,
    onboarding_qa_messages,
    proposal_review_signals,
    scheduling_requests,
    screening_conversations,
    voice_sessions,
)
from app.services.reporting.summaries import NotAvailable

R, C, P = agent_runs, agent_tool_calls, agent_proposals
ESTIMATE = "Estimate"
# Minutes of coordination a human would have spent per executed agent action (SM-30 estimate; constant table).
MINUTES_SAVED: dict[str, int] = {
    "interviews.propose_slots": 20, "interviews.book_slot": 15, "interviews.reschedule": 15,
    "messaging.send_templated": 5, "messaging.create_draft": 5, "messaging.send": 5, "tasks.create": 2,
    "notes.add": 2, "notify.user": 1, "pipeline.add_candidate": 5, "outreach.send": 8, "screening.invite": 10,
    "submission.prepare_pack": 25, "demands.create_draft": 20, "compliance.chase": 6, "bgv.follow_up": 6,
    "timesheets.nudge": 3, "billing.prepare_run": 30, "collections.send_reminder": 6,
}  # fmt: skip
DEFAULT_MINUTES = 3


def _owner_scope(t: TileContext, col: Any) -> list[Any]:
    cond = scope_condition(t.principal, REPORT_READ, col)
    return [] if cond is None else [cond]


def _in_range(t: TileContext, col: Any) -> list[Any]:
    return [col >= t.filters.start, col < t.filters.end]


def _agent(t: TileContext, col: Any) -> list[Any]:
    return [col == t.filters.agent_key] if t.filters.agent_key else []


async def _enabled_at(t: TileContext) -> datetime | None:
    try:
        async with t.db.begin_nested():
            return (
                await t.db.execute(select(ai_tenant_settings_p4.c.agentic_enabled_at))
            ).scalar_one_or_none()
    except Exception:
        return None


def _weekdays(a: datetime, b: datetime) -> float:
    """Business days from ``a`` to ``b`` (weekends excluded; fractional days kept for the first/last day)."""
    if b <= a:
        return 0.0
    days = 0
    d: date = a.date()
    end = b.date()
    while d < end:
        if d.weekday() < 5:
            days += 1
        d += timedelta(days=1)
    return float(days)


# ---------------------------------------------------------------- SM-30
async def sm30_coordination_time(t: TileContext) -> TileData:
    need(t, "agent_runs", "agent_tool_calls", "agent_proposals")
    conds = [*_in_range(t, C.c.created_at), C.c.outcome == "executed", C.c.mode == "auto", C.c.risk == "low",
             *_owner_scope(t, R.c.owner_user_id), *_agent(t, R.c.agent_key)]  # fmt: skip
    autos = (await t.db.execute(
        select(C.c.tool_key, func.count()).select_from(C.join(R, R.c.id == C.c.run_id)).where(*conds).group_by(C.c.tool_key)
    )).tuples().all()  # fmt: skip
    approved = (await t.db.execute(
        select(P.c.tool_key, func.count()).where(
            P.c.origin == "agent", P.c.status.in_(("approved", "executed")), *_in_range(t, P.c.decided_at),
            *_owner_scope(t, P.c.owner_user_id), *_agent(t, P.c.agent_key),
        ).group_by(P.c.tool_key)
    )).tuples().all()  # fmt: skip
    minutes = sum(int(n) * MINUTES_SAVED.get(str(k), DEFAULT_MINUTES) for k, n in [*autos, *approved])
    recruiters = int((await t.db.execute(
        select(func.count(func.distinct(R.c.owner_user_id))).where(*_in_range(t, R.c.created_at), *_owner_scope(t, R.c.owner_user_id))
    )).scalar_one())  # fmt: skip
    weeks = max(1.0, ((t.filters.to_date - t.filters.from_date).days + 1) / 7.0)
    value = round(minutes / 60.0 / recruiters / weeks, 1) if recruiters else None
    return {"value": value, "unit": "hours", "estimate": True, "badge": ESTIMATE, "n": recruiters,
            "rows": [{"key": "actions", "label": "Agent actions counted", "value": sum(int(n) for _, n in [*autos, *approved])}]}  # fmt: skip


# ---------------------------------------------------------------- SM-31
async def _first_submission_days(t: TileContext, lo: datetime, hi: datetime) -> list[float]:
    from app.models.demands import Demand
    from app.models.pipeline import Submission
    from app.services.reporting.common import scoped_demand_ids

    first = (
        select(Submission.demand_id, func.min(Submission.sent_at).label("first"))
        .where(Submission.sent_at.is_not(None)).group_by(Submission.demand_id).subquery()
    )  # fmt: skip
    rows = (await t.db.execute(
        select(Demand.opened_at, first.c.first).join(first, first.c.demand_id == Demand.id)
        .where(Demand.opened_at >= lo, Demand.opened_at < hi, Demand.id.in_(scoped_demand_ids(t.principal, "demand:read")))
    )).tuples().all()  # fmt: skip
    return [_weekdays(o, f) for o, f in rows if o is not None and f is not None and f >= o]


async def sm31_time_to_first_submission(t: TileContext) -> TileData:
    now = t.now
    current = await _first_submission_days(t, now - timedelta(days=90), now)
    enabled = await _enabled_at(t)
    base = await _first_submission_days(t, enabled - timedelta(days=90), enabled) if enabled else []
    cur, prev = median(current), median(base)
    change = round(100.0 * (cur - prev) / prev, 1) if cur is not None and prev else None
    return {"value": cur, "unit": "business days", "n": len(current), "rows": [
        {"key": "baseline", "label": "Before AI agents", "value": prev if enabled else "n/a"},
        {"key": "change_pct", "label": "Change (target -20%)", "value": change},
    ]}  # fmt: skip


# ---------------------------------------------------------------- SM-32
async def sm32_proposal_acceptance(t: TileContext) -> TileData:
    need(t, "agent_proposals")
    decided = P.c.status.in_(("approved", "executed", "rejected", "execution_failed"))
    rows = (await t.db.execute(
        select(P.c.agent_key, func.count().filter(P.c.status.in_(("approved", "executed", "execution_failed"))), func.count())
        .where(P.c.origin == "agent", decided, *_in_range(t, P.c.decided_at), *_owner_scope(t, P.c.owner_user_id), *_agent(t, P.c.agent_key))
        .group_by(P.c.agent_key).order_by(P.c.agent_key)
    )).tuples().all()  # fmt: skip
    out = [
        {"key": k, "label": k, "value": pct(int(a), int(n)), "n": int(n), "target": 70} for k, a, n in rows
    ]
    total_a = sum(int(a) for _, a, _n in rows)
    total = sum(int(n) for _, _a, n in rows)
    return {"value": pct(total_a, total), "unit": "%", "n": total, "rows": out}


# ---------------------------------------------------------------- SM-33
async def sm33_scheduling_cycle_time(t: TileContext) -> TileData:
    need(t, "scheduling_requests")
    S = scheduling_requests
    hours = func.extract("epoch", S.c.updated_at - S.c.created_at) / 3600.0
    value, n = (await t.db.execute(
        select(func.percentile_cont(0.5).within_group(hours), func.count())
        .where(S.c.status == "booked", *_in_range(t, S.c.created_at), *_owner_scope(t, S.c.owner_user_id))
    )).one()  # fmt: skip
    return {
        "value": round(float(value), 1) if value is not None else None,
        "unit": "hours",
        "n": int(n),
        "target": 24,
    }


# ---------------------------------------------------------------- SM-34
async def sm34_screening_completion(t: TileContext) -> TileData:
    need(t, "screening_conversations")
    SC = screening_conversations
    invited = SC.c.invited_at
    done = (
        (SC.c.completed_at.is_not(None))
        & (SC.c.status == "completed")
        & (SC.c.completed_at <= invited + timedelta(hours=72))
    )
    a, n = (await t.db.execute(
        select(func.count().filter(done), func.count())
        .where(invited.is_not(None), *_in_range(t, invited), *_owner_scope(t, SC.c.owner_user_id))
    )).one()  # fmt: skip
    va, vn = 0, 0
    if "voice_sessions" in t.avail:
        V = voice_sessions
        va, vn = (await t.db.execute(
            select(func.count().filter(V.c.reviewed_at <= V.c.created_at + timedelta(hours=72)), func.count())
            .where(*_in_range(t, V.c.created_at), *_owner_scope(t, V.c.owner_user_id))
        )).one()  # fmt: skip
    return {
        "value": pct(int(a) + int(va), int(n) + int(vn)),
        "unit": "%",
        "n": int(n) + int(vn),
        "target": 60,
    }


# ---------------------------------------------------------------- SM-35
async def sm35_copilot_adoption(t: TileContext) -> TileData:
    need(t, "copilot_messages", "copilot_feedback")
    M, F = copilot_messages, copilot_feedback
    week_ago = t.now - timedelta(days=7)
    conv = table(
        "copilot_conversations", column("id", Uuid()), column("user_id", Uuid()), column("deleted_at")
    )
    try:
        async with t.db.begin_nested():
            weekly = int((await t.db.execute(
                select(func.count(func.distinct(conv.c.user_id))).select_from(M.join(conv, conv.c.id == M.c.conversation_id))
                .where(M.c.role == "user", M.c.created_at >= week_ago)
            )).scalar_one())  # fmt: skip
    except Exception as exc:
        raise NotAvailable("copilot_conversations") from exc
    users = int(
        (
            await t.db.execute(
                select(func.count())
                .select_from(table("users", column("is_active")))
                .where(column("is_active").is_(True))
            )
        ).scalar_one()
    )
    helpful, rated = (await t.db.execute(
        select(func.count().filter(F.c.rating == "helpful"), func.count()).where(*_in_range(t, F.c.created_at))
    )).one()  # fmt: skip
    return {"value": pct(weekly, users), "unit": "%", "n": users, "target": 50,
            "rows": [{"key": "helpful", "label": "Helpful", "value": pct(int(helpful), int(rated)), "target": 80}]}  # fmt: skip


# ---------------------------------------------------------------- SM-36 / SM-37
async def sm36_prestart_compliance(t: TileContext) -> TileData:
    # A0: the P2 start gates carry no completion timestamp, so "complete >= 2 days before start" cannot be computed.
    raise NotAvailable("start_gates.completed_at")


async def sm37_time_and_cash(t: TileContext) -> TileData:
    need(t, "timesheets")
    from app.services.reporting.p2tables import timesheets as TS

    on_time = TS.c.submitted_at <= func.cast(TS.c.period_end + timedelta(days=2), TS.c.submitted_at.type)
    a, n = (await t.db.execute(
        select(func.count().filter(on_time), func.count()).where(TS.c.submitted_at.is_not(None), *_in_range(t, TS.c.submitted_at))
    )).one()  # fmt: skip
    dso_value: Any = None
    try:
        from app.services.reporting import summaries

        les = list(t.filters.legal_entity_ids) or await summaries.legal_entity_ids(t.db)
        values = [await summaries.dso(t.ctx, t.db, le, t.filters.from_date, t.filters.to_date) for le in les]
        known = [float(v) for v in values if v is not None]
        dso_value = round(sum(known) / len(known), 1) if known else None
    except Exception:
        dso_value = None
    return {"value": pct(int(a), int(n)), "unit": "%", "n": int(n),
            "rows": [{"key": "dso", "label": "DSO (days)", "value": dso_value}]}  # fmt: skip


# ---------------------------------------------------------------- SM-38 / SM-39
async def sm38_analytics_self_service(t: TileContext) -> TileData:
    need(t, "analytics_answers")
    AA = analytics_answers
    saved, n = (await t.db.execute(
        select(func.count().filter(AA.c.saved_report_id.is_not(None)), func.count()).where(*_in_range(t, AA.c.created_at))
    )).one()  # fmt: skip
    score: Any = None
    try:
        res = table(
            "ai_eval_results", column("agent_key"), column("suite"), column("score"), column("created_at")
        )
        async with t.db.begin_nested():
            score = (await t.db.execute(
                select(res.c.score).where(res.c.agent_key == "analytics", res.c.suite == "agent")
                .order_by(res.c.created_at.desc()).limit(1)
            )).scalar_one_or_none()  # fmt: skip
    except Exception:
        score = None
    return {"value": pct(int(saved), int(n)), "unit": "%", "n": int(n),
            "rows": [{"key": "eval_score", "label": "Latest evaluation score", "value": float(score) if score is not None else None}]}  # fmt: skip


async def sm39_governance_integrity(t: TileContext) -> TileData:
    from app.core.actors import AUTOMATED_KINDS
    from app.core.agentic import FORBIDDEN_ACTIONS

    L = audit_logs_p4
    star = [f"{entity}.{transition}" for entity, transition in FORBIDDEN_ACTIONS]
    by_bots = int((await t.db.execute(
        select(func.count()).where(L.c.actor_type.in_(sorted(AUTOMATED_KINDS)), L.c.action.in_(star), *_in_range(t, L.c.occurred_at))
    )).scalar_one())  # fmt: skip
    agent_rows, linked = (await t.db.execute(
        select(func.count(), func.count().filter(L.c.agent_run_id.is_not(None))).where(L.c.actor_type == "agent", *_in_range(t, L.c.occurred_at))
    )).one()  # fmt: skip
    return {"value": by_bots, "target": 0, "rows": [
        {"key": "star_by_automation", "label": "★ transitions by non-human actors", "value": by_bots},
        {"key": "agent_rows_linked", "label": "Agent audit rows with a run id", "value": pct(int(linked), int(agent_rows))},
    ]}  # fmt: skip


# ---------------------------------------------------------------- SM-C14..C18
async def smc14_rubber_stamping(t: TileContext) -> TileData:
    need(t, "proposal_review_signals", "agent_proposals")
    S = proposal_review_signals
    approvals = S.c.decision.in_(("approved", "edited_approved"))
    rows = (await t.db.execute(
        select(P.c.agent_key, func.count().filter(S.c.rubber_stamp.is_(True)), func.count())
        .select_from(S.join(P, P.c.id == S.c.proposal_id))
        .where(approvals, *_in_range(t, S.c.decided_at), *_owner_scope(t, P.c.owner_user_id), *_agent(t, P.c.agent_key))
        .group_by(P.c.agent_key).order_by(P.c.agent_key)
    )).tuples().all()  # fmt: skip
    total_r = sum(int(r) for _, r, _n in rows)
    total = sum(int(n) for _, _r, n in rows)
    return {"value": pct(total_r, total), "unit": "%", "n": total,
            "rows": [{"key": k, "label": k, "value": pct(int(r), int(n)), "n": int(n)} for k, r, n in rows]}  # fmt: skip


async def smc15_contact_fatigue(t: TileContext) -> TileData:
    need(t, "agent_tool_calls", "agent_runs")
    sent = int((await t.db.execute(
        select(func.count()).select_from(C.join(R, R.c.id == C.c.run_id))
        .where(C.c.outcome == "executed", C.c.tool_key.like("messaging.%"), *_in_range(t, C.c.created_at),
               *_owner_scope(t, R.c.owner_user_id), *_agent(t, R.c.agent_key))
    )).scalar_one())  # fmt: skip
    handoffs = 0
    if "screening_conversations" in t.avail:
        SC = screening_conversations
        handoffs = int((await t.db.execute(
            select(func.count()).where(SC.c.status == "handed_off", *_in_range(t, SC.c.created_at), *_owner_scope(t, SC.c.owner_user_id))
        )).scalar_one())  # fmt: skip
    opt_outs = 0
    try:
        cons = table("consents", column("purpose"), column("status"), column("withdrawn_at"))
        async with t.db.begin_nested():
            opt_outs = int((await t.db.execute(
                select(func.count()).where(cons.c.purpose == "ai_agent_communication", cons.c.withdrawn_at.is_not(None),
                                           *_in_range(t, cons.c.withdrawn_at))
            )).scalar_one())  # fmt: skip
    except Exception:
        opt_outs = 0
    per_k = round(1000.0 * (handoffs + opt_outs) / sent, 1) if sent else None
    return {"value": per_k, "unit": "per 1,000 messages", "n": sent,
            "rows": [{"key": "opt_outs", "label": "Opt-outs", "value": opt_outs}, {"key": "handoffs", "label": "Talk to a person", "value": handoffs}]}  # fmt: skip


async def smc16_impact_ratios(t: TileContext) -> TileData:
    """Link only: no tile shows fairness data (P3 rule)."""
    return {"value": None, "link": {"path": "/fairness", "query": {}}, "badge": "Open bias audit"}


async def smc17_ai_cost_per_placement(t: TileContext) -> TileData:
    from app.services.reporting.metrics.p2common import scoped_placements
    from app.services.reporting.p2tables import placements as PL

    need(t, "placements")
    A = ai_actions_p4
    cost = int((await t.db.execute(
        select(func.coalesce(func.sum(A.c.cost_micros), 0)).where(A.c.kind == "agent_step", *_in_range(t, A.c.created_at), *_agent(t, A.c.agent_key))
    )).scalar_one())  # fmt: skip
    f = t.filters
    started = int((await t.db.execute(
        select(func.count()).where(PL.c.id.in_(scoped_placements(t, PL.c.actual_start_date >= f.from_date, PL.c.actual_start_date <= f.to_date)))
    )).scalar_one())  # fmt: skip
    return {
        "value": float(usd(cost / started)) if started else None,
        "unit": "USD",
        "n": started,
        "rows": [{"key": "cost_usd", "label": "Agent cost (USD)", "value": usd(cost)}],
    }


async def smc18_wrong_answers(t: TileContext) -> TileData:
    need(t, "copilot_feedback")
    F = copilot_feedback
    unhelpful, rated = (await t.db.execute(
        select(func.count().filter(F.c.rating == "unhelpful"), func.count()).where(*_in_range(t, F.c.created_at))
    )).one()  # fmt: skip
    wrong, answers = 0, 0
    if "onboarding_qa_messages" in t.avail:
        Q = onboarding_qa_messages
        wrong, answers = (await t.db.execute(
            select(func.count().filter(Q.c.feedback == "incorrect"), func.count()).where(Q.c.role == "assistant", *_in_range(t, Q.c.created_at))
        )).one()  # fmt: skip
    return {"value": pct(int(unhelpful) + int(wrong), int(rated) + int(answers)), "unit": "%", "target": 2,
            "rows": [{"key": "copilot_unhelpful", "label": "Copilot unhelpful", "value": pct(int(unhelpful), int(rated))},
                     {"key": "qa_wrong", "label": "Onboarding answers marked wrong", "value": pct(int(wrong), int(answers))}]}  # fmt: skip


__all__ = [
    "MINUTES_SAVED",
    "sm30_coordination_time",
    "sm31_time_to_first_submission",
    "sm32_proposal_acceptance",
    "sm33_scheduling_cycle_time",
    "sm34_screening_completion",
    "sm35_copilot_adoption",
    "sm36_prestart_compliance",
    "sm37_time_and_cash",
    "sm38_analytics_self_service",
    "sm39_governance_integrity",
    "smc14_rubber_stamping",
    "smc15_contact_fatigue",
    "smc16_impact_ratios",
    "smc17_ai_cost_per_placement",
    "smc18_wrong_answers",
]
