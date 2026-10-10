"""End-date alerts and automatic end-of-term cases (FR-162). Daily beat, idempotent per tenant.

* For each ``active`` placement: ``days_left = end_date - local today``; for every offset with ``days_left <=
  offset`` and no ``placement_end_alerts`` row, insert the rows and emit one ``PlacementEndingSoon`` (smallest
  offset) to the AM and recruiter -- unless an open / confirmed extension exists for **this** end date or an
  offboarding case is open. A changed end date starts a fresh series (rows are keyed by end date).
* End of term: ``end_date + grace < today`` with no case -> ``offboarding.initiate(system, end_of_term)``; the
  partial unique "one open case per placement" makes concurrent runs create exactly one.
"""

import uuid
from datetime import date, timedelta

from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import ActorRef
from app.core.context import RequestContext
from app.core.errors import ConflictError
from app.core.events import emit
from app.models.placements import Placement
from app.models.placements_exit import PlacementEndAlert
from app.schemas.placements_exit import EndingSoonRow
from app.services.placements import exit_settings, extension_checks, extensions, offboarding
from app.services.placements.common import local_today, names, tenant_of
from app.services.placements.exit_events import PlacementEndingSoon

SYSTEM = ActorRef("system", None)


async def _today(db: AsyncSession, p: Placement, today: date | None) -> date:
    return today if today is not None else await local_today(db, p)


async def _active(db: AsyncSession) -> list[Placement]:
    stmt = select(Placement).where(Placement.status == "active", Placement.end_date.is_not(None))
    return list((await db.execute(stmt)).scalars())


async def run_alerts(ctx: RequestContext, db: AsyncSession, today: date | None = None) -> int:
    settings = await exit_settings.settings_row(db, tenant_of(ctx, db))
    offsets = sorted({int(o) for o in settings.alert_offsets_days or []}, reverse=True)
    sent = 0
    for p in await _active(db):
        assert p.end_date is not None
        days_left = (p.end_date - await _today(db, p, today)).days
        due = [o for o in offsets if days_left <= o]
        if not due or days_left < 0:
            continue
        existing = set(
            (
                await db.execute(
                    select(PlacementEndAlert.offset_days).where(
                        PlacementEndAlert.placement_id == p.id, PlacementEndAlert.end_date == p.end_date
                    )
                )
            ).scalars()
        )
        missing = [o for o in due if o not in existing]
        if not missing:
            continue
        if await extensions.end_date_series_blocked(db, p.id, p.end_date):
            continue
        if await extension_checks.open_case(db, p.id) is not None:
            continue
        inserted: list[int] = []
        for o in missing:
            stmt = (
                pg_insert(PlacementEndAlert)
                .values(
                    id=uuid.uuid4(),
                    tenant_id=p.tenant_id,
                    placement_id=p.id,
                    end_date=p.end_date,
                    offset_days=o,
                )
                .on_conflict_do_nothing(constraint="uq_placement_end_alerts_key")
                .returning(PlacementEndAlert.id)
            )
            if (await db.execute(stmt)).scalar_one_or_none() is not None:
                inserted.append(o)
        if not inserted:
            continue
        recipients = tuple(u for u in {p.account_manager_user_id, p.recruiter_user_id} if u is not None)
        emit(
            db,
            PlacementEndingSoon(
                tenant_id=p.tenant_id,
                placement_id=p.id,
                end_date=p.end_date,
                offset_days=min(inserted),
                days_left=days_left,
                recipient_ids=recipients,
            ),
        )
        sent += 1
    return sent


async def run_end_of_term(ctx: RequestContext, db: AsyncSession, today: date | None = None) -> int:
    settings = await exit_settings.settings_row(db, tenant_of(ctx, db))
    grace = int(settings.auto_case_grace_days or 0)
    created = 0
    for p in await _active(db):
        assert p.end_date is not None
        if p.end_date + timedelta(days=grace) >= await _today(db, p, today):
            continue
        if await extension_checks.open_case(db, p.id) is not None:
            continue
        try:
            async with db.begin_nested():
                await offboarding.initiate(ctx, db, p.id, "end_of_term", p.end_date, actor=SYSTEM)
            created += 1
        except ConflictError:
            continue
        except Exception:  # unique violation from a concurrent run, or a bad row: never stops the beat
            logger.exception("end-of-term case not created for placement {}", p.id)
    return created


async def daily(ctx: RequestContext, db: AsyncSession, today: date | None = None) -> dict[str, int]:
    return {"alerts": await run_alerts(ctx, db, today), "cases": await run_end_of_term(ctx, db, today)}


async def ending_soon(
    ctx: RequestContext, db: AsyncSession, within_days: int | None = None
) -> list[EndingSoonRow]:
    from app.services.placements.lists import scoped

    settings = await exit_settings.settings_row(db, tenant_of(ctx, db))
    window = within_days or max([int(o) for o in settings.alert_offsets_days or []] or [60])
    stmt = scoped(
        select(Placement).where(Placement.status == "active", Placement.end_date.is_not(None)), ctx.principal
    )
    rows: list[Placement] = list((await db.execute(stmt.order_by(Placement.end_date))).scalars())
    cands, clients = await names(
        db, candidate_ids={p.candidate_id for p in rows}, client_ids={p.client_id for p in rows}
    )
    result: list[EndingSoonRow] = []
    for p in rows:
        assert p.end_date is not None
        left = (p.end_date - await local_today(db, p)).days
        if left > window:
            continue
        ext = await extensions.open_extension(db, p.id)
        case = await extension_checks.open_case(db, p.id)
        result.append(
            EndingSoonRow(
                placement_id=p.id,
                human_id=p.human_id,
                candidate_id=p.candidate_id,
                candidate_name=cands.get(p.candidate_id),
                client_id=p.client_id,
                client_name=clients.get(p.client_id),
                role_title=p.role_title,
                worker_type=p.worker_type,
                end_date=p.end_date,
                days_left=left,
                open_extension_id=ext.id if ext else None,
                open_case_id=case.id if case else None,
            )
        )
    return result
