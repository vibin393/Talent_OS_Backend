# ruff: file-ignore[line-too-long, non-imperative-mood, missing-trailing-period, missing-terminal-punctuation]
"""SLA risk (Epic 55, spec 0.6, FR-284). Read-only on demands: **no Demand field ever changes.**

``assess(ctx, db, demand_id)``:
* **Late** -- any open clock ``breached`` -> at risk (basis ``late``).
* **Prediction** -- with ``prediction:read`` and a live ``time_to_fill`` prediction: projected fill date =
  ``opened_at + predicted_days``; ``p_late = P(fill date > due)`` (due = ``target_closure_date``, else the latest
  open milestone due) from the stored p20 / p80 interval (piecewise-linear CDF: 0.2 at p20, 0.8 at p80, clamped to
  [0, 1]); with a point estimate only, ``p_late`` is 1.0 / 0.0 ("point estimate"). At risk when
  ``p_late >= sla_risk_probability``.
* **Pipeline only** (no model or no ``prediction:read``): an open ``first_submission`` clock >= 50 % elapsed with no
  submissions; an open ``first_interview`` clock >= 60 % elapsed with no interviews; fewer than ``positions x 3``
  applications after 40 % of ``assignment`` + ``first_submission``.
* **Next steps** are deterministic links, never demand changes.

``scan`` lists at-risk open demands visible to the scan owner; ``send_digest`` re-assesses the given ids, upserts the
``sla_risk`` flags and sends one ``demand_sla_digest`` per recipient (primary recruiter, else the owner, plus the BU
Recruitment Managers) listing only demands **that recipient can read**.
"""

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.demands import Demand, DemandAssignment, DemandSlaClock
from app.schemas.demand_insights import NextStep, SlaClockOut, SlaRiskOut

MILESTONE_LABELS = {
    "assignment": "Assignment",
    "first_submission": "First submission",
    "first_interview": "First interview",
    "client_feedback": "Client feedback",
}
BASIS_PIPELINE = "Based on pipeline progress only (no time-to-fill model)"
BASIS_LATE = "A milestone is already late"


def _now() -> datetime:
    return datetime.now(UTC)


def _q3(value: float) -> Decimal:
    return Decimal(str(max(0.0, min(1.0, value)))).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def p_late_from_interval(due_days: float, p20: float, p80: float) -> float:
    """P(days to fill > due_days) for a piecewise-linear CDF with CDF(p20)=0.2, CDF(p80)=0.8."""
    if p80 <= p20:
        return 1.0 if due_days < p20 else 0.0
    slope = 0.6 / (p80 - p20)
    cdf = 0.2 + (due_days - p20) * slope
    return 1.0 - max(0.0, min(1.0, cdf))


def pipeline_rules(
    elapsed: dict[str, int], *, submissions: int, interviews: int, applications: int, positions: int
) -> list[tuple[str, str]]:
    """Pure pipeline-only rules: ``[(milestone, reason)]`` (``elapsed`` = open clock milestone -> % elapsed)."""
    hits: list[tuple[str, str]] = []
    sub = elapsed.get("first_submission")
    if sub is not None and sub >= 50 and submissions == 0:
        hits.append(("first_submission", f"First submission clock is {sub}% elapsed with no submissions"))
    intv = elapsed.get("first_interview")
    if intv is not None and intv >= 60 and interviews == 0:
        hits.append(
            ("first_interview", f"First interview clock is {intv}% elapsed with no interviews scheduled")
        )
    early = [elapsed[m] for m in ("assignment", "first_submission") if m in elapsed]
    if early and max(early) >= 40 and applications < positions * 3:
        m = "first_submission" if "first_submission" in elapsed else "assignment"
        hits.append(
            (
                m,
                f"{applications} applications for {positions} position(s) (expected at least {positions * 3})",
            )
        )
    return hits


@dataclasses.dataclass(frozen=True, slots=True)
class ClockState:
    milestone: str
    status: str
    due_at: datetime
    elapsed_pct: int
    hours_late: float


async def clock_states(db: AsyncSession, demand: Demand, at: datetime | None = None) -> list[ClockState]:
    from app.core import business_hours
    from app.services.demands import sla

    at = at or _now()
    cal, tz = await business_hours.tenant_calendar(db)
    clocks = (await sla.clocks_for(db, [demand.id])).get(demand.id, [])
    out: list[ClockState] = []
    for clock in clocks:
        if clock.completed_at is not None:
            continue
        target = float(clock.target_hours) or 1.0
        if clock.clock == "business":
            elapsed = business_hours.elapsed_business_hours(clock.started_at, at, cal, tz)
        else:
            elapsed = business_hours.elapsed_calendar_hours(clock.started_at, at)
        status = sla.compute_status(clock, at, cal, tz)
        out.append(
            ClockState(
                clock.milestone,
                status,
                clock.due_at,
                round(min(elapsed / target, 9.99) * 100),
                max(0.0, elapsed - target),
            )
        )
    return out


def _next_steps(demand: Demand, milestones: set[str], sourcing_on: bool) -> list[NextStep]:
    base = f"/demands/{demand.id}"
    steps: list[NextStep] = []
    if sourcing_on:
        steps.append(NextStep(label="Run Sourcing Agent", url=f"{base}?tab=matches"))
    else:
        steps.append(NextStep(label="Review matches", url=f"{base}?tab=matches"))
    if milestones & {"first_submission", "assignment"}:
        steps.append(NextStep(label="Widen skills using the skills graph", url=f"{base}?tab=overview#skills"))
    if "first_interview" in milestones:
        steps.append(NextStep(label="Schedule pending interviews", url=f"{base}?tab=pipeline"))
    if demand.hiring_manager_contact_id is not None and milestones & {"first_interview", "client_feedback"}:
        steps.append(
            NextStep(label="Ask the client for feedback", url=f"/contacts/{demand.hiring_manager_contact_id}")
        )
    return steps[:5]


async def _prediction(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> Any:
    from app.core.errors import DomainError
    from app.services.analytics import reads

    principal = ctx.principal
    if principal is None or "prediction:read" not in getattr(principal, "permissions", {}):
        return None
    try:
        out = await reads.predictions_for(ctx, db, "demand", demand_id)
    except DomainError:
        return None
    return next(
        (p for p in out.predictions if p.kind == "time_to_fill" and p.status == "ok" and p.predicted_days),
        None,
    )


async def _sourcing_on(db: AsyncSession) -> bool:
    from app.services import ai

    features = await ai.features_map(db)
    return bool(features.get("agentic") and features.get("agent_sourcing"))


async def assess(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, *, at: datetime | None = None
) -> SlaRiskOut:
    from app.services.demand_intake import settings as settings_mod
    from app.services.demands.common import READ, get_for

    demand = await get_for(ctx, db, demand_id, READ, for_update=False)
    return await assess_row(
        ctx, db, demand, threshold=Decimal(str((await settings_mod.row(db)).sla_risk_probability)), at=at
    )


async def assess_row(
    ctx: RequestContext, db: AsyncSession, demand: Demand, *, threshold: Decimal, at: datetime | None = None,
    sourcing_on: bool | None = None,
) -> SlaRiskOut:  # fmt: skip
    at = at or _now()
    states = await clock_states(db, demand, at)
    counts = {
        "applications": demand.applications_count, "submissions": demand.submissions_count,
        "interviews": demand.interviews_scheduled_count, "positions": demand.positions,
    }  # fmt: skip
    clocks_out = [
        SlaClockOut(milestone=s.milestone, status=s.status, due_at=s.due_at, elapsed_pct=s.elapsed_pct)
        for s in states
    ]
    reasons: list[str] = []
    risky_milestones: set[str] = set()
    at_risk = False
    basis = "pipeline_only"
    basis_text = BASIS_PIPELINE
    p_late: Decimal | None = None
    milestone: str | None = None
    for s in states:
        if s.status == "breached":
            at_risk, basis, basis_text = True, "late", BASIS_LATE
            risky_milestones.add(s.milestone)
            milestone = milestone or s.milestone
            reasons.append(
                f"{MILESTONE_LABELS.get(s.milestone, s.milestone)} is {max(1, round(s.hours_late))} hours late"
            )
    prediction = await _prediction(ctx, db, demand.id) if demand.status == "open" else None
    if prediction is not None and basis != "late":
        basis = "prediction"
        start = demand.opened_at or demand.created_at
        days = float(prediction.predicted_days)
        due: datetime | None = None
        if demand.target_closure_date is not None:
            due = datetime.combine(demand.target_closure_date, datetime.min.time(), tzinfo=UTC)
        elif states:
            due = max(s.due_at for s in states)
        if due is not None:
            due_days = (due - start).total_seconds() / 86400
            if prediction.interval is not None:
                p = p_late_from_interval(
                    due_days, float(prediction.interval.p20), float(prediction.interval.p80)
                )
                p_late = _q3(p)
                basis_text = f"Based on the time-to-fill prediction ({int((p_late * 100).to_integral_value(ROUND_HALF_UP))}% likely to miss)"
            else:
                p_late = Decimal("1.000") if days > due_days else Decimal("0.000")
                basis_text = "Based on the time-to-fill prediction (point estimate)"
            if p_late >= threshold:
                at_risk = True
                milestone = milestone or (
                    "target_closure"
                    if demand.target_closure_date is not None
                    else max(states, key=lambda s: s.due_at).milestone
                )
                reasons.append(
                    f"The projected fill date ({(start + timedelta(days=days)):%d %b %Y}) is after the target"
                )
                risky_milestones |= {s.milestone for s in states}
        else:
            basis_text = "Based on the time-to-fill prediction (no due date to compare)"
    elif basis != "late":
        hits = pipeline_rules(
            {s.milestone: s.elapsed_pct for s in states}, submissions=demand.submissions_count,
            interviews=demand.interviews_scheduled_count, applications=demand.applications_count,
            positions=demand.positions,
        )  # fmt: skip
        for m, reason in hits:
            at_risk = True
            risky_milestones.add(m)
            milestone = milestone or m
            reasons.append(reason)
    sourcing = sourcing_on if sourcing_on is not None else await _sourcing_on(db)
    steps = _next_steps(demand, risky_milestones, sourcing) if at_risk else []
    return SlaRiskOut(
        demand_id=demand.id, at_risk=at_risk, basis=basis if (at_risk or basis == "prediction") else "pipeline_only",
        basis_text=basis_text, p_late=p_late, milestone=milestone, clocks=clocks_out, counts=counts,
        reasons=reasons[:5], next_steps=steps,
    )  # fmt: skip


async def scan(
    ctx: RequestContext, db: AsyncSession, *, limit: int | None = None
) -> tuple[list[tuple[Demand, SlaRiskOut]], int]:
    """At-risk open demands visible to ``ctx`` that have SLA clocks, most at risk first."""
    from app.services.demand_intake import settings as settings_mod
    from app.services.demands.common import READ, access_condition, principal_of

    s = await settings_mod.row(db)
    limit = limit or s.sla_digest_max_items
    stmt = (
        select(Demand)
        .where(
            Demand.status == "open",
            Demand.archived_at.is_(None),
            Demand.id.in_(select(DemandSlaClock.demand_id).where(DemandSlaClock.completed_at.is_(None))),
        )
        .order_by(Demand.created_at, Demand.id)
        .limit(1000)
    )
    cond = access_condition(principal_of(ctx), READ)
    if cond is not None:
        stmt = stmt.where(cond)
    demands = list((await db.execute(stmt)).scalars())
    threshold = Decimal(str(s.sla_risk_probability))
    sourcing = await _sourcing_on(db)
    found: list[tuple[Demand, SlaRiskOut]] = []
    for d in demands:
        out = await assess_row(ctx, db, d, threshold=threshold, sourcing_on=sourcing)
        if out.at_risk:
            found.append((d, out))
    found.sort(key=lambda x: (0 if x[1].basis == "late" else 1, -(x[1].p_late or Decimal(0)), x[0].req_id))
    return found[:limit], len(demands)


async def recipients_for(db: AsyncSession, demand: Demand) -> list[uuid.UUID]:
    from app.services import directory_service, entity_refs

    primary = (
        await db.execute(
            select(DemandAssignment.user_id).where(
                DemandAssignment.demand_id == demand.id,
                DemandAssignment.is_primary.is_(True),
                DemandAssignment.removed_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    ids: list[uuid.UUID] = [primary or demand.owner_id]
    for m in sorted(await directory_service.bu_manager_ids(db, demand.business_unit_id), key=str):
        if m not in ids:
            ids.append(m)
    return sorted(await entity_refs.active_user_ids(db, ids), key=lambda u: ids.index(u))


async def send_digest(
    ctx: RequestContext,
    db: AsyncSession,
    items: list[tuple[uuid.UUID, str | None]],
    *,
    run_id: uuid.UUID | None,
) -> tuple[int, int]:
    """Returns ``(digests_sent, flags_raised)``."""
    from app.core.errors import DomainError
    from app.services import entity_refs
    from app.services import notifications_service as ns
    from app.services.agent_runtime.sanitize import outbound
    from app.services.demand_insights import flags
    from app.services.demand_intake import settings as settings_mod
    from app.services.demands.common import READ, get_for

    s = await settings_mod.row(db)
    threshold = Decimal(str(s.sla_risk_probability))
    tenant_id = entity_refs.session_tenant(db)
    per_recipient: dict[uuid.UUID, list[str]] = {}
    flags_raised = 0
    seen: set[uuid.UUID] = set()
    for demand_id, note in items[: s.sla_digest_max_items * 4]:
        if demand_id in seen:
            continue
        seen.add(demand_id)
        try:
            demand = await get_for(ctx, db, demand_id, READ, for_update=False)
        except DomainError:
            continue  # not visible to the scan owner: ignored
        risk = await assess_row(ctx, db, demand, threshold=threshold)
        if not risk.at_risk:
            continue
        _, created = await flags.upsert_sla(
            db, demand, milestone=risk.milestone, basis=risk.basis, p_late=risk.p_late, reasons=risk.reasons,
            next_steps=risk.next_steps, run_id=run_id,
        )  # fmt: skip
        flags_raised += int(created)
        line = f"{demand.req_id} {demand.title}: {'; '.join(risk.reasons[:2]) or risk.basis_text}"
        if note:
            line += f" -- {outbound(note, max_len=300).text}"
        line += f" ({risk.basis_text}) /demands/{demand.id}?tab=ai_assist"
        for rid in await recipients_for(db, demand):
            if await entity_refs.user_can_read(db, tenant_id, rid, "demand", demand.id):
                per_recipient.setdefault(rid, []).append(line[:600])
    day = _now().strftime("%Y-%m-%d")
    sent = 0
    for rid, lines in per_recipient.items():
        content = ns.Content(
            title=f"{len(lines)} demand{'s' if len(lines) != 1 else ''} may miss their SLA",
            body="\n".join(lines[: s.sla_digest_max_items])[:2000], link_path="/demands?flag=sla_risk",
        )  # fmt: skip
        delivered = await ns.deliver(
            db, "demand_sla_digest", [rid], content, event_id=f"sla_digest:{day}:{rid}"
        )
        sent += int(bool(delivered))
    await db.flush()
    return sent, flags_raised


__all__ = [
    "BASIS_PIPELINE",
    "MILESTONE_LABELS",
    "assess",
    "assess_row",
    "clock_states",
    "p_late_from_interval",
    "pipeline_rules",
    "recipients_for",
    "scan",
    "send_digest",
]
