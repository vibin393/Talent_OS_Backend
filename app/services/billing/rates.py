# ruff: file-ignore[line-too-long, unused-function-argument]
"""SAC codes and effective-dated GST rates (FR-146): seeded per tenant on first use (18 %), non-overlapping per SAC
(EXCLUDE gist). ``rates_for`` resolves the rate of each SAC for a document date (422 ``tax_rate_missing``)."""

import uuid
from collections.abc import Iterable
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import and_, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.billing import SacCode, TaxRate
from app.schemas.billing import SacCodeIn, SacCodeOut, TaxRateIn, TaxRateOut
from app.services.billing.common import conflict, invalid, tenant_of

SEED_SAC: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("998511", "Executive search services", ()),
    ("998512", "Permanent placement services", ("placement_fee", "conversion_fee")),
    ("998513", "Contract staffing services", ("time", "adjustment", "schedule_fee", "milestone")),
    ("998514", "Temporary staffing services", ()),
    ("998515", "Payroll services", ()),
    ("998519", "Other employment and labour supply services", ("expense", "pure_agent_expense", "manual")),
)
SEED_RATE = Decimal("18")
SEED_FROM = date(2017, 7, 1)
DEFAULT_SAC = "998513"


async def ensure_tax_master(db: AsyncSession) -> None:
    """Idempotent per-tenant seed of the SAC catalogue and the 18 % rate (FR-146)."""
    tid = tenant_of(db)
    if (await db.execute(select(SacCode.id).limit(1))).first() is not None:
        return
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"billing_tax_master:{tid}"})
    if (await db.execute(select(SacCode.id).limit(1))).first() is not None:
        return
    for code, desc, kinds in SEED_SAC:
        await db.execute(
            pg_insert(SacCode)
            .values(
                id=uuid.uuid4(), tenant_id=tid, code=code, description=desc, default_for_line_kind=list(kinds)
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "code"])
        )
        exists = (await db.execute(select(TaxRate.id).where(TaxRate.sac_code == code).limit(1))).first()
        if exists is None:
            db.add(
                TaxRate(
                    id=uuid.uuid4(),
                    tenant_id=tid,
                    sac_code=code,
                    rate_pct=SEED_RATE,
                    effective_from=SEED_FROM,
                )
            )
    await db.flush()


async def default_sac(db: AsyncSession, line_kind: str) -> str:
    await ensure_tax_master(db)
    row = (
        await db.execute(
            select(SacCode.code)
            .where(SacCode.active.is_(True), SacCode.default_for_line_kind.any(line_kind))
            .limit(1)
        )
    ).scalar_one_or_none()
    return row or DEFAULT_SAC


async def rates_for(db: AsyncSession, sac_codes: Iterable[str], on_date: date) -> dict[str, Decimal]:
    await ensure_tax_master(db)
    wanted = sorted(set(sac_codes))
    if not wanted:
        return {}
    rows = await db.execute(
        select(TaxRate.sac_code, TaxRate.rate_pct).where(
            TaxRate.sac_code.in_(wanted),
            TaxRate.effective_from <= on_date,
            or_(TaxRate.effective_to.is_(None), TaxRate.effective_to >= on_date),
        )
    )
    found = {code: Decimal(rate) for code, rate in rows.all()}
    missing = [c for c in wanted if c not in found]
    if missing:
        raise invalid("tax_rate_missing", "No GST rate is set for this SAC on the document date",
                      sac_codes=missing, date=on_date.isoformat())  # fmt: skip
    return found


async def list_sac(ctx: RequestContext, db: AsyncSession) -> list[SacCodeOut]:
    await ensure_tax_master(db)
    rows = await db.execute(select(SacCode).order_by(SacCode.code))
    return [SacCodeOut.model_validate(r) for r in rows.scalars()]


async def upsert_sac(ctx: RequestContext, db: AsyncSession, data: SacCodeIn) -> SacCodeOut:
    await ensure_tax_master(db)
    row = (await db.execute(select(SacCode).where(SacCode.code == data.code))).scalar_one_or_none()
    if row is None:
        row = SacCode(id=uuid.uuid4(), tenant_id=tenant_of(db), code=data.code)
        db.add(row)
    row.description = data.description
    row.default_for_line_kind = list(data.default_for_line_kind)
    row.active = data.active
    await db.flush()
    await db.refresh(row)
    return SacCodeOut.model_validate(row)


async def list_rates(ctx: RequestContext, db: AsyncSession) -> list[TaxRateOut]:
    await ensure_tax_master(db)
    rows = await db.execute(select(TaxRate).order_by(TaxRate.sac_code, TaxRate.effective_from))
    return [TaxRateOut.model_validate(r) for r in rows.scalars()]


async def add_rate(ctx: RequestContext, db: AsyncSession, data: TaxRateIn) -> TaxRateOut:
    """Add an effective-dated rate; the open-ended previous rate of the SAC is closed the day before."""
    await ensure_tax_master(db)
    if (await db.execute(select(SacCode.id).where(SacCode.code == data.sac_code))).first() is None:
        raise invalid("sac_unknown", "Unknown SAC code", sac_code=data.sac_code)
    previous = (
        await db.execute(
            select(TaxRate).where(
                TaxRate.sac_code == data.sac_code,
                TaxRate.effective_to.is_(None),
                TaxRate.effective_from < data.effective_from,
            )
        )
    ).scalar_one_or_none()
    if previous is not None:
        previous.effective_to = data.effective_from - timedelta(days=1)
    row = TaxRate(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        sac_code=data.sac_code,
        rate_pct=data.rate_pct,
        effective_from=data.effective_from,
        effective_to=data.effective_to,
    )
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError as exc:
        raise conflict("tax_rate_overlap", "This rate overlaps an existing rate of the SAC") from exc
    await db.refresh(row)
    return TaxRateOut.model_validate(row)


__all__ = [
    "add_rate",
    "and_",
    "default_sac",
    "ensure_tax_master",
    "list_rates",
    "list_sac",
    "rates_for",
    "upsert_sac",
]
