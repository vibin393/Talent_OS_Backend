# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Possible-duplicate and SLA-risk flags (Epic 55, stories 55.4 / 55.5, FR-283 / FR-284).

* ``raise_duplicate`` **recomputes** the score server-side; below ``duplicate_threshold`` -> 422 ``below_threshold``.
  Reasons come from the scorer, never the model (the model adds only a short summary). Dedupe
  ``dup:{min(id)}:{max(id)}``; emits ``DemandDuplicateFlagged``; notifies the owners of both demands that can read the
  other one. A flag never blocks or merges.
* ``upsert_sla`` (the daily digest) keeps one open ``sla_risk`` flag per demand; ``expire`` resolves SLA flags whose
  demand is no longer at risk / open and expires duplicate flags on demands closed > 90 days ago.
* ``resolve`` (a person, ``demand:update``): ``linked_duplicate`` | ``distinct`` | ``dismissed`` (audited).
* ``FlagOut.related`` shows the related demand's REQ id and title **only when the viewer can read it** (NFR-46).
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import actor_from_ctx, require_human
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.core.pagination import Page, PageParams, paginate
from app.events.demand_intake import DemandDuplicateFlagged, DemandFlagResolved
from app.models.demand_insights import DemandFlag
from app.models.demands import Demand
from app.schemas.demand_insights import (
    FlagOut,
    FlagQuery,
    NextStep,
    ReasonOut,
    RelatedDemandOut,
    ResolveFlagIn,
)
from app.services.demand_insights import similarity


def _now() -> datetime:
    return datetime.now(UTC)


def dup_key(a: uuid.UUID, b: uuid.UUID) -> str:
    lo, hi = sorted((str(a), str(b)))
    return f"dup:{lo}:{hi}"


async def _readable(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> Demand | None:
    from app.core.errors import DomainError
    from app.services.demands.common import READ, get_for

    try:
        return await get_for(ctx, db, demand_id, READ, for_update=False)
    except DomainError:
        return None


async def flag_out(ctx: RequestContext, db: AsyncSession, row: DemandFlag) -> FlagOut:
    related: RelatedDemandOut | None = None
    if row.related_demand_id is not None:
        other = await _readable(ctx, db, row.related_demand_id)
        related = (
            RelatedDemandOut(
                related_visible=True,
                demand_id=other.id,
                human_id=other.req_id,
                title=other.title,
                status=other.status,
            )
            if other is not None
            else RelatedDemandOut(related_visible=False)
        )
    demand = await db.get(Demand, row.demand_id)
    return FlagOut(
        id=row.id, demand_id=row.demand_id, demand_human_id=demand.req_id if demand is not None else None,
        kind=row.kind, status=row.status, score=row.score,  # type: ignore[arg-type]
        reasons=[ReasonOut(**r) for r in row.reasons or []], summary=row.summary, milestone=row.milestone,
        basis=row.basis, p_late=row.p_late, next_steps=[NextStep(**n) for n in row.next_steps or []],
        related=related, raised_by_kind=row.raised_by_kind, run_id=row.run_id, created_at=row.created_at,
        resolved_at=row.resolved_at, resolution_note=row.resolution_note,
    )  # fmt: skip


async def raise_duplicate(
    ctx: RequestContext,
    db: AsyncSession,
    demand_id: uuid.UUID,
    related_demand_id: uuid.UUID,
    summary: str | None,
    *,
    run_id: uuid.UUID | None,
    raised_by_kind: str = "agent",
) -> tuple[DemandFlag, Decimal]:
    from app.services.demand_intake import settings as settings_mod
    from app.services.demands.common import READ, get_for

    if demand_id == related_demand_id:
        raise ValidationFailed("A demand can't duplicate itself", code="same_demand")
    demand = await get_for(ctx, db, demand_id, READ, for_update=False)
    other = await get_for(ctx, db, related_demand_id, READ, for_update=False)  # re-resolved under scope
    if other.client_id != demand.client_id:
        raise ValidationFailed("Duplicates are flagged within the same client only", code="different_client")
    s = await settings_mod.row(db)
    facts = await similarity.facts_for(db, [demand, other])
    result = similarity.score(facts[demand.id], facts[other.id])
    threshold = Decimal(str(s.duplicate_threshold))
    if result.score < threshold:
        raise ValidationFailed(
            "The demands are not similar enough to flag", code="below_threshold",
            details={"score": str(result.score), "threshold": str(threshold)},
        )  # fmt: skip
    key = dup_key(demand.id, other.id)
    existing = (
        await db.execute(select(DemandFlag).where(DemandFlag.dedupe_key == key, DemandFlag.status == "open"))
    ).scalar_one_or_none()
    if existing is not None:
        return existing, result.score
    from app.services.agent_runtime.sanitize import outbound

    row = DemandFlag(
        id=uuid.uuid4(), tenant_id=demand.tenant_id, demand_id=demand.id, kind="possible_duplicate",
        related_demand_id=other.id, score=result.score, reasons=[r.model_dump() for r in result.reasons][:8],
        summary=outbound(summary, max_len=500).text if summary else None, status="open",
        raised_by_kind=raised_by_kind, run_id=run_id, dedupe_key=key, next_steps=[],
    )  # fmt: skip
    db.add(row)
    await db.flush()
    emit(db, DemandDuplicateFlagged(demand.tenant_id, row.id, demand.id, other.id, str(result.score)))
    await _notify_duplicate(db, row, demand, other)
    return row, result.score


async def _notify_duplicate(db: AsyncSession, row: DemandFlag, demand: Demand, other: Demand) -> None:
    """``demand_duplicate_flagged`` to the owners of both demands -- only those who can read the other one."""
    from app.services import entity_refs
    from app.services import notifications_service as ns

    recipients: list[uuid.UUID] = []
    for owner, mine, theirs in ((demand.owner_id, demand, other), (other.owner_id, other, demand)):
        if owner in recipients:
            continue
        if await entity_refs.user_can_read(
            db, row.tenant_id, owner, "demand", theirs.id
        ) and await entity_refs.user_can_read(db, row.tenant_id, owner, "demand", mine.id):
            recipients.append(owner)
    if not recipients:
        return
    content = ns.Content(
        title=f"{demand.req_id} may duplicate {other.req_id}", entity_type="demand", entity_id=demand.id,
        link_path=f"/demands/{demand.id}?tab=ai_assist",
    )  # fmt: skip
    await ns.deliver(db, "demand_duplicate_flagged", recipients, content, event_id=f"dup_flag:{row.id}")


async def upsert_sla(
    db: AsyncSession,
    demand: Demand,
    *,
    milestone: str | None,
    basis: str,
    p_late: Decimal | None,
    reasons: list[str],
    next_steps: list[NextStep],
    run_id: uuid.UUID | None,
) -> tuple[DemandFlag, bool]:
    key = f"sla:{demand.id}"
    row = (
        await db.execute(select(DemandFlag).where(DemandFlag.dedupe_key == key, DemandFlag.status == "open"))
    ).scalar_one_or_none()
    created = row is None
    if row is None:
        row = DemandFlag(
            id=uuid.uuid4(), tenant_id=demand.tenant_id, demand_id=demand.id, kind="sla_risk", status="open",
            raised_by_kind="agent", dedupe_key=key, reasons=[], next_steps=[],
        )  # fmt: skip
        db.add(row)
    row.milestone = milestone
    row.basis = basis if basis in ("prediction", "pipeline_only", "late") else None
    row.p_late = p_late
    row.reasons = [{"field": "sla", "detail": r[:200]} for r in reasons[:8]]
    row.next_steps = [n.model_dump() for n in next_steps[:5]]
    row.run_id = run_id
    await db.flush()
    return row, created


async def resolve(ctx: RequestContext, db: AsyncSession, flag_id: uuid.UUID, data: ResolveFlagIn) -> FlagOut:
    from app.services.demands.common import UPDATE, get_for

    require_human(actor_from_ctx(ctx))
    row = (
        await db.execute(select(DemandFlag).where(DemandFlag.id == flag_id).with_for_update())
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("Flag not found")
    await get_for(ctx, db, row.demand_id, UPDATE)  # 404 / 403 unless the person may update the demand
    if row.status != "open":
        raise ConflictError("This flag is already resolved", code="flag_not_open")
    if data.resolution == "linked_duplicate" and row.kind != "possible_duplicate":
        raise ValidationFailed("Only a possible duplicate can be linked", code="invalid_resolution")
    row.status = data.resolution
    row.resolved_by = getattr(ctx.principal, "id", None)
    row.resolved_at = _now()
    row.resolution_note = data.note
    await db.flush()
    audit.record(
        "demand_insights.flag_resolved",
        "demand_flag",
        row.id,
        session=db,
        resolution=data.resolution,
        kind=row.kind,
    )
    if row.resolved_by is not None:
        emit(
            db,
            DemandFlagResolved(
                row.tenant_id, row.id, row.demand_id, row.kind, data.resolution, row.resolved_by
            ),
        )
    return await flag_out(ctx, db, row)


async def for_demand(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> list[FlagOut]:
    rows = list(
        (
            await db.execute(
                select(DemandFlag)
                .where(DemandFlag.demand_id == demand_id)
                .order_by(DemandFlag.created_at.desc(), DemandFlag.id)
                .limit(50)
            )
        ).scalars()
    )
    return [await flag_out(ctx, db, r) for r in rows]


async def open_for(db: AsyncSession, demand_id: uuid.UUID) -> list[DemandFlag]:
    return list(
        (
            await db.execute(
                select(DemandFlag).where(DemandFlag.demand_id == demand_id, DemandFlag.status == "open")
            )
        ).scalars()
    )


async def list_flags(
    ctx: RequestContext, db: AsyncSession, q: FlagQuery, params: PageParams | None = None
) -> Page[FlagOut]:
    """Flags of demands visible to the caller (``demand:read``)."""
    from app.services.demands.common import READ, access_condition, principal_of

    stmt = select(DemandFlag).join(Demand, Demand.id == DemandFlag.demand_id)
    cond = access_condition(principal_of(ctx), READ)
    if cond is not None:
        stmt = stmt.where(cond)
    if q.kind:
        stmt = stmt.where(DemandFlag.kind == q.kind)
    if q.status:
        stmt = stmt.where(DemandFlag.status == q.status)
    if q.demand_id:
        stmt = stmt.where(DemandFlag.demand_id == q.demand_id)
    page = await paginate(
        db, stmt.order_by(DemandFlag.created_at.desc(), DemandFlag.id), params or PageParams()
    )
    rows: list[Any] = list(page.items)
    return Page(
        items=[await flag_out(ctx, db, r) for r in rows],
        total=page.total,
        page=page.page,
        page_size=page.page_size,
    )


async def expire(ctx: RequestContext, db: AsyncSession) -> int:  # ruff: ignore[unused-function-argument]
    """Duplicate flags on demands closed / cancelled > 90 days ago -> ``expired``; SLA flags whose demand is no
    longer open or has no open clock at risk -> ``resolved`` (system)."""
    from app.models.demands import DemandSlaClock

    changed = 0
    cutoff = _now() - timedelta(days=90)
    rows = (
        await db.execute(
            select(DemandFlag, Demand)
            .join(Demand, Demand.id == DemandFlag.demand_id)
            .where(DemandFlag.status == "open")
            .with_for_update(of=DemandFlag, skip_locked=True)
        )
    ).all()
    for flag, demand in rows:
        if flag.kind == "possible_duplicate":
            if demand.status in ("closed", "cancelled") and demand.updated_at < cutoff:
                flag.status, flag.resolved_at = "expired", _now()
                changed += 1
            continue
        if demand.status != "open":
            flag.status, flag.resolved_at = "resolved", _now()
            changed += 1
            continue
        risky = (
            await db.execute(
                select(DemandSlaClock.id).where(
                    DemandSlaClock.demand_id == demand.id,
                    DemandSlaClock.completed_at.is_(None),
                    DemandSlaClock.status.in_(("at_risk", "breached")),
                )
            )
        ).first()
        if risky is None and flag.basis == "late":
            flag.status, flag.resolved_at = "resolved", _now()
            changed += 1
    await db.flush()
    return changed


__all__ = [
    "dup_key",
    "expire",
    "flag_out",
    "for_demand",
    "list_flags",
    "open_for",
    "raise_duplicate",
    "resolve",
    "upsert_sla",
]
