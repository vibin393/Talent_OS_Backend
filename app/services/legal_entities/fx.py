"""FX rates (AD-17, AD-20): tenant-recorded rates with a mandatory source.

``rate_for`` returns the exact-date rate, else the latest earlier rate within 7 days, else 422
``fx_rate_missing``; identity pairs return 1. Epic 51: ``upsert_rate`` writes manual rows only; with the FX feed
enabled ``rate_for`` also considers feed rows (``max_stale_days``) with the tenant precedence.
"""

import dataclasses
import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ValidationFailed
from app.core.money import quantize_fx, to_decimal
from app.models.legal_entities import FxRate as FxRateRow
from app.schemas.legal_entities import FxRateIn, FxRateOut

LOOKBACK_DAYS = 7


@dataclasses.dataclass(frozen=True, slots=True)
class FxRate:
    rate: Decimal
    rate_date: date
    source: str


def _tenant(db: AsyncSession) -> uuid.UUID:
    raw = db.sync_session.info.get("tenant_id")
    if raw is None:
        msg = "fx needs a tenant session"
        raise RuntimeError(msg)
    return uuid.UUID(str(raw))


async def _currency_known(db: AsyncSession, code: str) -> bool:
    from app.models.reference import Currency

    return (await db.execute(select(Currency.code).where(Currency.code == code))).first() is not None


async def upsert_rate(ctx: RequestContext, db: AsyncSession, data: FxRateIn) -> FxRateOut:
    if data.from_currency == data.to_currency:
        raise ValidationFailed("Pick two different currencies", code="fx_same_currency")
    for code in (data.from_currency, data.to_currency):
        if not await _currency_known(db, code):
            raise ValidationFailed("Unknown currency", code="currency_not_supported", details={"currency": code})
    rate = quantize_fx(to_decimal(data.rate))
    if rate <= 0:
        raise ValidationFailed("The rate must be positive", code="fx_rate_invalid")
    existing = (
        await db.execute(
            select(FxRateRow).where(
                FxRateRow.from_currency == data.from_currency,
                FxRateRow.to_currency == data.to_currency,
                FxRateRow.rate_date == data.rate_date,
                FxRateRow.origin == "manual",  # Epic 51: the manual endpoint never touches feed rows
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        existing = FxRateRow(
            id=uuid.uuid4(),
            tenant_id=_tenant(db),
            from_currency=data.from_currency,
            to_currency=data.to_currency,
            rate_date=data.rate_date,
            rate=rate,
            source=data.source,
            origin="manual",
            created_by=getattr(ctx.principal, "id", None),
        )
        db.add(existing)
    else:
        existing.rate = rate
        existing.source = data.source
    await db.flush()
    await db.refresh(existing)
    return FxRateOut.model_validate(existing)


async def list_rates(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    *,
    from_currency: str | None = None,
    to_currency: str | None = None,
    limit: int = 200,
) -> list[FxRateOut]:
    stmt = select(FxRateRow)
    if from_currency:
        stmt = stmt.where(FxRateRow.from_currency == from_currency.upper())
    if to_currency:
        stmt = stmt.where(FxRateRow.to_currency == to_currency.upper())
    rows = (await db.execute(stmt.order_by(FxRateRow.rate_date.desc()).limit(limit))).scalars()
    return [FxRateOut.model_validate(r) for r in rows]


async def rate_for(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    from_ccy: str,
    to_ccy: str,
    on_date: date,
) -> FxRate:
    a, b = from_ccy.upper(), to_ccy.upper()
    if a == b:
        return FxRate(Decimal(1), on_date, "identity")

    async def latest(origin: str, days: int) -> FxRateRow | None:
        return (
            await db.execute(
                select(FxRateRow)
                .where(
                    FxRateRow.from_currency == a,
                    FxRateRow.to_currency == b,
                    FxRateRow.origin == origin,
                    FxRateRow.rate_date <= on_date,
                    FxRateRow.rate_date >= on_date - timedelta(days=days),
                )
                .order_by(FxRateRow.rate_date.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    # Epic 51 (FR-255): with the feed enabled, feed rows (within max_stale_days) compete with manual rows (7-day
    # lookback): the most recent date wins; on the same date the tenant precedence decides (default manual_first,
    # coordinator decision 2). Feed disabled -> the P2 behaviour exactly (manual rows only).
    from app.services.legal_entities import fx_feed

    feed_settings = await fx_feed.settings_row(db)
    row = await latest("manual", LOOKBACK_DAYS)
    if feed_settings is not None and feed_settings.enabled:
        feed = await latest("feed", int(feed_settings.max_stale_days))
        if feed is not None and (
            row is None
            or feed.rate_date > row.rate_date
            or (feed.rate_date == row.rate_date and feed_settings.precedence == "feed_first")
        ):
            row = feed
    if row is None:
        raise ValidationFailed(
            "No exchange rate is recorded for this date",
            code="fx_rate_missing",
            details={"from_currency": a, "to_currency": b, "date": on_date.isoformat()},
        )
    return FxRate(Decimal(row.rate), row.rate_date, row.source)
