# ruff: file-ignore[line-too-long]
"""Live FX feed (Epic 51, Story 51.3, FR-255; AD-20, AD-29, AD-49).

* ``fetch`` pulls reference rates through the ``fx`` connector port (Manual adapter -> ``[]`` -> an ``ok`` run
  with 0 rows) and inserts them into ``fx_rates`` with ``origin='feed'`` and ``source`` starting with ``feed:``.
  A re-fetch for the same date updates only feed rows; manual rows are never touched (counted as
  ``skipped_manual`` when a manual rate exists for the same pair and date). No rate is ever deleted.
* A failure marks the run ``failed``, increments the consecutive-failure counter once per day and emits
  ``FxFeedFailed`` once per day -> ``fx_feed_failed`` notification to Finance Admins.
* ``due_tenants`` / ``run_if_due``: the hourly beat fetches once per business day after ``fetch_hour_ist`` (IST).
* ``rate_for`` precedence and staleness live in ``legal_entities/fx.py`` (uses ``settings_row``).
Issued documents keep their snapshot rate (AD-20): nothing here touches billing.
"""

import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import ValidationFailed
from app.core.events import emit
from app.core.money import quantize_fx
from app.events.legal_entities import FxFeedFailed
from app.models.legal_entities import FxFeedRun, FxFeedSettings
from app.models.legal_entities import FxRate as FxRateRow
from app.schemas.fx_feed import FxFeedRunOut, FxFeedSettingsIn, FxFeedSettingsOut

IST = timezone(timedelta(hours=5, minutes=30))
NOTIFY_KEY = "fx_feed_failed"


def _tenant(db: AsyncSession) -> uuid.UUID:
    return uuid.UUID(str(db.sync_session.info["tenant_id"]))


def _now() -> datetime:
    return datetime.now(UTC)


async def settings_row(db: AsyncSession, *, create: bool = False) -> FxFeedSettings | None:
    tid = _tenant(db)
    row = (
        await db.execute(select(FxFeedSettings).where(FxFeedSettings.tenant_id == tid))
    ).scalar_one_or_none()
    if row is None and create:
        await db.execute(pg_insert(FxFeedSettings).values(tenant_id=tid).on_conflict_do_nothing())
        row = (await db.execute(select(FxFeedSettings).where(FxFeedSettings.tenant_id == tid))).scalar_one()
    return row


async def _provider_key(ctx: RequestContext, db: AsyncSession) -> str:
    from app.services.connectors import registry

    adapter = await registry.get(ctx, db, "fx")
    return str(getattr(adapter, "provider_key", "manual"))


async def _last_run(db: AsyncSession) -> FxFeedRun | None:
    return (
        await db.execute(
            select(FxFeedRun).order_by(FxFeedRun.on_date.desc(), FxFeedRun.started_at.desc()).limit(1)
        )
    ).scalar_one_or_none()


async def get_settings(ctx: RequestContext, db: AsyncSession) -> FxFeedSettingsOut:
    row = await settings_row(db, create=True)
    assert row is not None
    last = await _last_run(db)
    return FxFeedSettingsOut(
        enabled=row.enabled,
        pairs=list(row.pairs or []),
        precedence=row.precedence,
        max_stale_days=row.max_stale_days,
        fetch_hour_ist=row.fetch_hour_ist,
        provider_key=await _provider_key(ctx, db),
        consecutive_failures=row.consecutive_failures,
        last_run=FxFeedRunOut.model_validate(last) if last else None,
    )


async def update_settings(ctx: RequestContext, db: AsyncSession, data: FxFeedSettingsIn) -> FxFeedSettingsOut:
    from app.models.reference import Currency

    known = set((await db.execute(select(Currency.code))).scalars())
    unknown = sorted({c for p in data.pairs for c in p.split("/")} - known)
    if unknown:
        raise ValidationFailed(
            "Unknown currency", code="currency_not_supported", details={"currencies": unknown}
        )
    row = await settings_row(db, create=True)
    assert row is not None
    row.enabled = data.enabled
    row.pairs = list(data.pairs)
    row.precedence = data.precedence
    row.max_stale_days = data.max_stale_days
    row.fetch_hour_ist = data.fetch_hour_ist
    await db.flush()
    audit.record(
        "fx_feed.configured",
        "fx_feed_settings",
        row.tenant_id,
        session=db,
        enabled=data.enabled,
        pairs=list(data.pairs),
        precedence=data.precedence,
        max_stale_days=data.max_stale_days,
    )
    return await get_settings(ctx, db)


async def list_runs(ctx: RequestContext, db: AsyncSession, limit: int = 60) -> list[FxFeedRunOut]:  # ruff: ignore[unused-function-argument]
    rows = (
        await db.execute(
            select(FxFeedRun).order_by(FxFeedRun.on_date.desc(), FxFeedRun.started_at.desc()).limit(limit)
        )
    ).scalars()
    return [FxFeedRunOut.model_validate(r) for r in rows]


def _pairs(row: FxFeedSettings) -> list[tuple[str, str]]:
    out = []
    for p in row.pairs or []:
        a, _, b = p.partition("/")
        if a and b:
            out.append((a, b))
    return out


async def _run_row(db: AsyncSession, on_date: date, provider_key: str) -> FxFeedRun:
    run = (
        await db.execute(
            select(FxFeedRun)
            .where(FxFeedRun.on_date == on_date, FxFeedRun.provider_key == provider_key)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if run is None:
        run = FxFeedRun(
            id=uuid.uuid4(), tenant_id=_tenant(db), on_date=on_date, provider_key=provider_key, status="ok"
        )
        db.add(run)
    run.started_at = _now()
    run.finished_at = None
    run.error = None
    return run


async def fetch(
    ctx: RequestContext, db: AsyncSession, on_date: date, *, manual_trigger: bool = False
) -> FxFeedRunOut:
    from app.services.connectors import registry

    row = await settings_row(db, create=True)
    assert row is not None
    if manual_trigger and not row.enabled:
        raise ValidationFailed("Enable the FX feed first", code="fx_feed_disabled")
    adapter = await registry.get(ctx, db, "fx")
    provider_key = str(getattr(adapter, "provider_key", "manual"))
    run = await _run_row(db, on_date, provider_key)
    previous_failed = run.status == "failed"  # already failed today: count the day once
    try:
        rates = list(await adapter.fetch_rates(on_date, _pairs(row)))  # type: ignore[attr-defined]
    except Exception as exc:  # provider down -> failed run + once-a-day alert
        logger.warning("fx feed fetch failed for {} ({})", on_date, type(exc).__name__)
        run.status = "failed"
        run.fetched = run.inserted = run.skipped_manual = 0
        run.error = f"{type(exc).__name__}: {str(exc)[:300]}"
        run.finished_at = _now()
        if not previous_failed:
            row.consecutive_failures += 1
        if row.last_alert_on != on_date:
            row.last_alert_on = on_date
            emit(
                db,
                FxFeedFailed(
                    tenant_id=row.tenant_id, on_date=on_date, consecutive_days=row.consecutive_failures
                ),
            )
        await db.flush()
        audit.record("fx_feed.fetch_failed", "fx_feed_run", run.id, session=db, on_date=on_date.isoformat())
        return FxFeedRunOut.model_validate(run)

    from app.models.reference import Currency

    known = set((await db.execute(select(Currency.code))).scalars())
    inserted = skipped = invalid = 0
    for dto in rates:
        a, b = dto.from_ccy.upper(), dto.to_ccy.upper()
        rate = quantize_fx(Decimal(dto.rate))
        if a == b or a not in known or b not in known or rate <= 0:
            invalid += 1
            continue
        source = dto.source if str(dto.source or "").startswith("feed:") else f"feed:{provider_key}"
        manual = (
            await db.execute(
                select(FxRateRow.id).where(
                    FxRateRow.from_currency == a,
                    FxRateRow.to_currency == b,
                    FxRateRow.rate_date == dto.rate_date,
                    FxRateRow.origin == "manual",
                )
            )
        ).first()
        if manual is not None:
            skipped += 1  # the manual row stays authoritative and untouched; the feed row is still stored
        stmt = pg_insert(FxRateRow).values(
            id=uuid.uuid4(),
            tenant_id=row.tenant_id,
            from_currency=a,
            to_currency=b,
            rate_date=dto.rate_date,
            rate=rate,
            source=source[:200],
            origin="feed",
            created_by=None,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_fx_rates_pair_date_origin",
            set_={"rate": stmt.excluded.rate, "source": stmt.excluded.source, "updated_at": _now()},
            where=FxRateRow.origin == "feed",
        )
        await db.execute(stmt)
        inserted += 1
    run.fetched = len(rates)
    run.inserted = inserted
    run.skipped_manual = skipped
    run.status = "partial" if invalid and inserted else ("failed" if invalid and not inserted else "ok")
    run.error = f"{invalid} rate(s) ignored (unknown currency or invalid rate)" if invalid else None
    run.finished_at = _now()
    if run.status != "failed":
        row.consecutive_failures = 0
    await db.flush()
    audit.record(
        "fx_feed.fetched",
        "fx_feed_run",
        run.id,
        session=db,
        on_date=on_date.isoformat(),
        inserted=inserted,
        manual_trigger=manual_trigger,
    )
    return FxFeedRunOut.model_validate(run)


def is_due(row: FxFeedSettings, now: datetime) -> bool:
    """Pure: enabled, a business day in IST and at/after the fetch hour."""
    local = now.astimezone(IST)
    return bool(row.enabled) and local.weekday() < 5 and local.hour >= int(row.fetch_hour_ist)


async def run_if_due(ctx: RequestContext, db: AsyncSession, now: datetime | None = None) -> str:
    now = now or _now()
    row = await settings_row(db)
    if row is None or not is_due(row, now):
        return "skipped"
    today = now.astimezone(IST).date()
    done = (
        await db.execute(
            select(FxFeedRun.id).where(FxFeedRun.on_date == today, FxFeedRun.status.in_(("ok", "partial")))
        )
    ).first()
    if done is not None:
        return "done"
    out = await fetch(ctx, db, today)
    return out.status
