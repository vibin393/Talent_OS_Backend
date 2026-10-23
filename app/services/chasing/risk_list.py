# ruff: file-ignore[line-too-long]
"""Pre-start compliance risk list (Epic 62 story 62.2, B1): Placements starting within N days (default 14) or active
with expired mandatory items; missing items, last contact, portal last seen, chases sent, escalation Task and BGV
status. Data scope = ``placement:read`` (recruiter / Account Manager / BU) through the P1 placement list filter."""

import uuid
from datetime import date, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.chasing import ComplianceChase
from app.schemas.chasing import RiskItem, RiskListOut, RiskRow
from app.services import entity_refs
from app.services.chasing import facts as facts_mod
from app.services.chasing.chases import CADENCE, local_today


async def risk_list(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    days: int = 14,
    client_id: uuid.UUID | None = None,
    escalated_only: bool = False,
    candidate_id: uuid.UUID | None = None,
    limit: int = 200,
    today: date | None = None,
) -> RiskListOut:
    from app.models.bgv import BgvCase
    from app.models.compliance import PlacementComplianceItem
    from app.models.placements import Placement
    from app.services.placements.lists import scoped

    days = max(1, min(days, 60))
    day = today or await local_today(db)
    expired_active = (
        select(PlacementComplianceItem.placement_id)
        .where(PlacementComplianceItem.mandatory.is_(True), PlacementComplianceItem.status == "expired")
        .scalar_subquery()
    )
    stmt = select(Placement).where(
        or_(
            Placement.status.in_(("pre_joining", "joining_confirmed"))
            & Placement.planned_start_date.between(day, day + timedelta(days=days)),
            (Placement.status == "active") & Placement.id.in_(expired_active),
        )
    )
    if client_id is not None:
        stmt = stmt.where(Placement.client_id == client_id)
    if candidate_id is not None:
        stmt = stmt.where(Placement.candidate_id == candidate_id)
    stmt = scoped(stmt.order_by(Placement.planned_start_date).limit(500), ctx.principal)
    placements = list((await db.execute(stmt)).scalars())
    rows: list[RiskRow] = []
    for p in placements:
        items = list(
            (
                await db.execute(
                    select(PlacementComplianceItem).where(
                        PlacementComplianceItem.placement_id == p.id,
                        PlacementComplianceItem.mandatory.is_(True),
                        PlacementComplianceItem.status.in_(("missing", "expiring", "expired")),
                    )
                )
            ).scalars()
        )
        if not items and p.status != "active":
            continue
        chases = list(
            (
                await db.execute(
                    select(ComplianceChase)
                    .where(ComplianceChase.placement_id == p.id)
                    .order_by(ComplianceChase.sent_at.desc())
                )
            ).scalars()
        )
        escalated = next((c for c in chases if c.escalated), None)
        if escalated_only and escalated is None:
            continue
        bgv = (
            await db.execute(
                select(BgvCase.status)
                .where(BgvCase.placement_id == p.id)
                .order_by(BgvCase.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        who = await entity_refs.addressee(db, "candidate", p.candidate_id)
        rows.append(
            RiskRow(
                placement_id=p.id, placement_ref=getattr(p, "human_id", None), candidate_id=p.candidate_id,
                worker_name=(who.full_name if who and who.full_name else "Worker"), client_id=p.client_id,
                client_name=await entity_refs.entity_label(db, "client", p.client_id), start_date=p.planned_start_date,
                days_to_start=(p.planned_start_date - day).days, status=p.status,
                missing_items=[RiskItem(label=i.label, status=i.status, kind=i.kind) for i in items],
                last_contact=await facts_mod.last_contact(db, p.candidate_id),
                portal_last_seen=await facts_mod.portal_last_seen(db, p.candidate_id), chases_sent=len(chases),
                escalation_task_id=escalated.task_id if escalated else None, escalated=escalated is not None, bgv_status=bgv,
            )
        )  # fmt: skip
        if len(rows) >= limit:
            break
    return RiskListOut(days=days, rows=rows, cadence=CADENCE)


__all__ = ["risk_list"]
