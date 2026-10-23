"""Rate-card gate at the Phase 1 proceed points (Epic 16 A6 wiring). Field mappings:

| Point | subject | role | location | experience_years | bill rate / currency / unit |
|---|---|---|---|---|---|
| Demand submit | ``demand`` / demand id | demand title | demand location | demand ``min_experience_years`` | demand ``bill_rate`` / ``currency`` / ``rate_unit`` |
| Submission create | ``submission`` / application id | demand title | demand location | candidate ``total_experience_months`` / 12 | submission bill rate / currency / rate unit |
| Offer release | ``offer`` / offer id | demand title | offer version location (else demand) | candidate ``total_experience_months`` / 12 | offer version ``bill_rate`` / ``bill_currency`` / ``bill_rate_unit`` |

Records without a bill rate (or currency / unit) skip validation. ``on_date`` is today in the tenant timezone.
"""  # ruff: ignore[line-too-long, missing-trailing-period, missing-terminal-punctuation]

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import column, select, table
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.fiscal import today_for
from app.schemas.commercials import RateCheckRequest, RateCheckResult
from app.services.commercials.rate_checks import require_rate_clearance

_tenants = table("tenants", column("id"), column("timezone"))
_candidates = table("candidates", column("id"), column("total_experience_months"))


async def _today(db: AsyncSession, tenant_id: uuid.UUID | None) -> date:
    tz = None
    if tenant_id is not None:
        tz = (
            await db.execute(select(_tenants.c.timezone).where(_tenants.c.id == tenant_id))
        ).scalar_one_or_none()
    return today_for(tz or "Asia/Kolkata")


async def candidate_experience_years(db: AsyncSession, candidate_id: uuid.UUID | None) -> Decimal | None:
    if candidate_id is None:
        return None
    months = (
        await db.execute(
            select(_candidates.c.total_experience_months).where(_candidates.c.id == candidate_id)
        )
    ).scalar_one_or_none()
    return None if months is None else (Decimal(months) / Decimal(12)).quantize(Decimal("0.1"))


async def _gate(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    subject_kind: str,
    subject_id: uuid.UUID,
    client_id: uuid.UUID,
    role: str | None,
    location: str | None,
    experience_years: Decimal | None,
    bill_rate: Decimal | None,
    currency: str | None,
    rate_unit: str | None,
) -> RateCheckResult | None:
    if bill_rate is None or not currency or not rate_unit:
        return None
    req = RateCheckRequest(
        subject_kind=subject_kind,
        subject_id=subject_id,
        client_id=client_id,
        on_date=await _today(db, ctx.tenant_id),
        role=role,
        location=location,
        experience_years=experience_years,
        bill_rate=Decimal(bill_rate),
        currency=currency,
        rate_unit=rate_unit,
    )
    return await require_rate_clearance(ctx, db, req)


async def demand_submit(ctx: RequestContext, db: AsyncSession, demand: Any) -> RateCheckResult | None:
    return await _gate(
        ctx,
        db,
        subject_kind="demand",
        subject_id=demand.id,
        client_id=demand.client_id,
        role=demand.title,
        location=demand.location,
        experience_years=demand.min_experience_years,
        bill_rate=demand.bill_rate,
        currency=demand.currency,
        rate_unit=demand.rate_unit,
    )


async def submission_create(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    demand: Any,
    application_id: uuid.UUID,
    candidate_id: uuid.UUID,
    bill_rate: Decimal | None,
    currency: str | None,
    rate_unit: str | None,
) -> RateCheckResult | None:
    return await _gate(
        ctx,
        db,
        subject_kind="submission",
        subject_id=application_id,
        client_id=demand.client_id,
        role=demand.title,
        location=demand.location,
        experience_years=await candidate_experience_years(db, candidate_id),
        bill_rate=bill_rate,
        currency=currency,
        rate_unit=rate_unit,
    )


async def offer_release(
    ctx: RequestContext, db: AsyncSession, *, offer: Any, version: Any, demand: Any
) -> RateCheckResult | None:
    return await _gate(
        ctx,
        db,
        subject_kind="offer",
        subject_id=offer.id,
        client_id=offer.client_id,
        role=getattr(demand, "title", None) or getattr(version, "title", None),
        location=getattr(version, "location", None) or getattr(demand, "location", None),
        experience_years=await candidate_experience_years(db, offer.candidate_id),
        bill_rate=getattr(version, "bill_rate", None),
        currency=getattr(version, "bill_currency", None),
        rate_unit=getattr(version, "bill_rate_unit", None),
    )
