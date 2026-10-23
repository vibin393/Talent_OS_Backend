# ruff: file-ignore[typing-only-standard-library-import]
"""SLA clock evaluator (Epic 32, story 19.1): beat ``sla.evaluate_clocks`` per tenant.

Open clocks whose status is behind the clock (``at_risk_at`` / ``due_at`` passed) are locked in batches with
``FOR UPDATE SKIP LOCKED`` and moved forward (``healthy -> at_risk -> breached``; a breach records
``breached_at``). When the status differs from ``last_notified_status`` the evaluator emits
``SlaClockStatusChanged`` once and notifies the milestone's recipients (``sla_clock_at_risk`` /
``sla_clock_breached``). Running the beat twice changes nothing (idempotent). The Phase 1 demand SLA keeps its
own beat (``demands.tasks.evaluate_slas``).
"""

import inspect
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from loguru import logger
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import emit
from app.core.settings import get_settings
from app.events.sla import SlaClockStatusChanged
from app.models.sla import SlaClock
from app.services import notifications_service
from app.services.sla.registry import milestone as milestone_def

NOTIFICATION_FOR = {"at_risk": "sla_clock_at_risk", "breached": "sla_clock_breached"}


def _now() -> datetime:
    return datetime.now(UTC)


def _scope(clock: SlaClock) -> tuple[str | None, uuid.UUID | None]:
    if clock.org_vendor_id is not None:
        return "vendor", clock.org_vendor_id
    if clock.org_client_id is not None:
        return "client", clock.org_client_id
    return None, None


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _recipients(db: AsyncSession, clock: SlaClock) -> list[Any]:
    definition = milestone_def(clock.subject_type, clock.milestone)
    if definition is None or definition.recipients is None:
        return []
    try:
        found = await _maybe_await(definition.recipients(db, clock.subject_id))
    except Exception:
        logger.exception("SLA recipients of {}.{} failed", clock.subject_type, clock.milestone)
        return []
    return list(found or [])


def _link(clock: SlaClock) -> str | None:
    definition = milestone_def(clock.subject_type, clock.milestone)
    if definition is None or definition.link is None:
        return None
    try:
        link = definition.link(clock.subject_id)
    except Exception:
        logger.exception("SLA link of {}.{} failed", clock.subject_type, clock.milestone)
        return None
    return str(link) if link else None


async def _notify(db: AsyncSession, clock: SlaClock, status: str) -> None:
    type_key = NOTIFICATION_FOR.get(status)
    if type_key is None:
        return
    recipients: Iterable[Any] = await _recipients(db, clock)
    if not recipients:
        return
    definition = milestone_def(clock.subject_type, clock.milestone)
    label = definition.label if definition else f"{clock.subject_type} {clock.milestone}"
    title = f"SLA {'breached' if status == 'breached' else 'at risk'}: {label}"
    content = notifications_service.Content(
        title=title,
        body=f"Due {clock.due_at:%d %b %Y %H:%M} UTC",
        entity_type=clock.subject_type,
        entity_id=clock.subject_id,
        link_path=_link(clock),
    )
    await notifications_service.deliver(
        db, type_key, recipients, content, event_id=f"sla:{clock.id}:{status}"
    )


def _due_condition(now: datetime) -> Any:
    return and_(
        SlaClock.completed_at.is_(None),
        or_(
            and_(SlaClock.at_risk_at <= now, SlaClock.status == "healthy"),
            and_(SlaClock.due_at <= now, SlaClock.status != "breached"),
            # a status recorded but never announced (e.g. a crash between the two)
            and_(
                SlaClock.status != "healthy", SlaClock.last_notified_status.is_distinct_from(SlaClock.status)
            ),
        ),
    )


async def evaluate_clocks(db: AsyncSession, now: datetime | None = None, batch: int | None = None) -> int:
    """Advance due clocks of the session tenant; returns the number of status changes announced."""
    now = now or _now()
    size = batch or get_settings().workflow_wait_batch
    rows = (
        (
            await db.execute(
                select(SlaClock)
                .where(_due_condition(now))
                .order_by(SlaClock.due_at, SlaClock.id)
                .limit(size)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    announced = 0
    for clock in rows:
        status = "breached" if now >= clock.due_at else "at_risk" if now >= clock.at_risk_at else clock.status
        if status == "breached" and clock.breached_at is None:
            clock.breached_at = clock.due_at
        clock.status = status
        if status == clock.last_notified_status or status == "healthy":
            continue
        scope_type, scope_id = _scope(clock)
        emit(
            db,
            SlaClockStatusChanged(
                tenant_id=clock.tenant_id,
                clock_id=clock.id,
                subject_type=clock.subject_type,
                subject_id=clock.subject_id,
                milestone=clock.milestone,
                scope_type=scope_type,
                scope_id=scope_id,
                from_status=clock.last_notified_status or "healthy",
                to_status=status,
            ),
        )
        clock.last_notified_status = status
        await db.flush()
        try:
            async with db.begin_nested():
                await _notify(db, clock, status)
        except Exception:  # a failing recipient resolver never blocks the status change
            logger.exception("SLA notification for clock {} failed", clock.id)
        announced += 1
    await db.flush()
    return announced
