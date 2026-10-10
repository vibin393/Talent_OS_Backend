"""Retention overrides (Epic 35 A3): keep one subject's rows of a class longer (e.g. a Contract requiring longer
BGV retention; Epic 20 / the commercials domain calls ``set_override``)."""

import uuid
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.models.privacy import RetentionOverride
from app.services.privacy.common import actor_id, require_tenant

# Epic 65 (NFR-52): oversight floors -- a tenant may not shorten these classes below the minimum
# (agent logs keep the human-oversight trail for at least 6 months).
MIN_PERIOD_MONTHS: dict[str, int] = {"agent_logs": 6}


def check_oversight_floor(class_key: str, period_months: int) -> None:
    from app.core.errors import ValidationFailed

    minimum = MIN_PERIOD_MONTHS.get(class_key)
    if minimum is not None and period_months < minimum:
        raise ValidationFailed(
            f"Agent logs must be kept for at least {minimum} months for oversight",
            code="retention_below_oversight_minimum",
            details={"minimum_months": minimum, "fields": {"period_months": f"Minimum {minimum}"}},
        )


async def set_override(ctx: RequestContext, db: AsyncSession, data: Any) -> uuid.UUID:
    """Upsert ``(subject_kind, subject_id, class_key) -> until_date`` (only ever lengthened)."""
    tenant_id = require_tenant(ctx)
    row = (
        await db.execute(
            select(RetentionOverride).where(
                RetentionOverride.subject_kind == data.subject_kind,
                RetentionOverride.subject_id == data.subject_id,
                RetentionOverride.class_key == data.class_key,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = RetentionOverride(
            id=uuid.uuid4(), tenant_id=tenant_id, subject_kind=data.subject_kind, subject_id=data.subject_id,
            class_key=data.class_key, until_date=data.until_date, reason=data.reason, created_by=actor_id(ctx),
        )  # fmt: skip
        db.add(row)
    elif data.until_date > row.until_date:
        row.until_date = data.until_date
        row.reason = data.reason or row.reason
    await db.flush()
    audit.record(
        "retention.override_set", "retention_override", row.id, session=db, class_key=data.class_key,
        until=str(row.until_date),
    )  # fmt: skip
    return row.id


async def override_until(
    db: AsyncSession, subject_kind: str, subject_id: uuid.UUID, class_key: str
) -> date | None:
    return (
        await db.execute(
            select(RetentionOverride.until_date).where(
                RetentionOverride.subject_kind == subject_kind,
                RetentionOverride.subject_id == subject_id,
                RetentionOverride.class_key == class_key,
            )
        )
    ).scalar_one_or_none()
