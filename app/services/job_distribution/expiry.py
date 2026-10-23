# ruff: file-ignore[line-too-long]
"""Board posting expiry (Epic 70, story 70.4; spec A5): daily scan (beat 08:30 IST, queue ``ecosystem``).

* ``live`` postings expiring within ``expiry_warning_days`` -> ``expiring`` + one Task per posting (entity
  ``board_posting``; assignee the publisher, else the Demand owner; due = ``expires_at``) "Board posting expiring:
  {title} on {board}" -- its actions are "Refresh" (user action: consumes credits) and "Close".
* ``expires_at`` passed -> ``expired`` (``close_reason='expired'``). Credits are never bought.
"""

from datetime import timedelta

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.demands import Demand
from app.models.job_distribution import BoardPosting
from app.services.job_distribution.common import board_label, board_settings, now
from app.services.job_distribution.postings import set_status


async def scan(db: AsyncSession) -> dict[str, int]:
    from app.schemas.tasks import TaskCreate
    from app.services import tasks_service

    settings = await board_settings(db)
    at = now()
    horizon = at + timedelta(days=int(settings.expiry_warning_days))
    counts = {"expiring": 0, "expired": 0}
    rows = list(
        (
            await db.execute(
                select(BoardPosting)
                .where(BoardPosting.status.in_(("live", "expiring")), BoardPosting.expires_at.is_not(None), BoardPosting.expires_at <= horizon)
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    for bp in rows:
        assert bp.expires_at is not None
        if bp.expires_at <= at:
            set_status(db, bp, "expired", reason="expired")
            counts["expired"] += 1
            continue
        if bp.status == "expiring":
            continue
        set_status(db, bp, "expiring")
        counts["expiring"] += 1
        demand = await db.get(Demand, bp.demand_id)
        owner = bp.published_by or (demand.owner_id if demand else None)
        if owner is None:
            continue
        data = TaskCreate.model_construct(
            title=f"Board posting expiring: {demand.title if demand else 'job'} on {board_label(bp.board_key)}"[:200],
            notes="Refresh it to keep it live (this uses board credits) or close it.",
            owner_id=owner, entity_type="board_posting", entity_id=bp.id, due_at=bp.expires_at, due_all_day=False,
            priority="normal", reminder_at=None,
        )  # fmt: skip
        try:
            async with db.begin_nested():
                await tasks_service.create_system_task(RequestContext(tenant_id=bp.tenant_id), db, data, source="agent")
        except Exception:  # pragma: no cover - logged
            logger.exception("Expiry Task for board posting {} not created", bp.id)
    await db.flush()
    return counts
