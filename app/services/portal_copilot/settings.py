# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Tenant portal Copilot settings (Epic 77 A1 / A5): one ``portal_copilot_settings`` row per tenant (internal-only org
RLS -- always read on an internal session). Defaults apply until an administrator saves them."""

import dataclasses
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import tenant_session
from app.models.portal_copilot import (
    DEFAULT_QPH_CANDIDATE,
    DEFAULT_QPH_EXTERNAL,
    DEFAULT_RETENTION_DAYS,
    PortalCopilotSettings,
)

FIELDS = (
    "client_enabled", "vendor_enabled", "candidate_enabled", "questions_per_hour_external",
    "questions_per_hour_candidate", "retention_days", "handoff_queue_role_key", "disclosure_extra",
)  # fmt: skip


@dataclasses.dataclass(frozen=True, slots=True)
class Settings:
    client_enabled: bool = False
    vendor_enabled: bool = False
    candidate_enabled: bool = False
    questions_per_hour_external: int = DEFAULT_QPH_EXTERNAL
    questions_per_hour_candidate: int = DEFAULT_QPH_CANDIDATE
    retention_days: int = DEFAULT_RETENTION_DAYS
    handoff_queue_role_key: str | None = None
    disclosure_extra: str | None = None
    updated_at: datetime | None = None

    def enabled(self, kind: str) -> bool:
        return bool(getattr(self, f"{kind}_enabled"))

    def questions_per_hour(self, kind: str) -> int:
        return self.questions_per_hour_candidate if kind == "candidate" else self.questions_per_hour_external


async def _row(db: AsyncSession) -> PortalCopilotSettings | None:
    return (await db.execute(select(PortalCopilotSettings))).scalar_one_or_none()


def _of(row: PortalCopilotSettings | None) -> Settings:
    if row is None:
        return Settings()
    return Settings(**{f: getattr(row, f) for f in FIELDS}, updated_at=row.updated_at)


async def get(tenant_id: uuid.UUID, db: AsyncSession | None = None) -> Settings:
    """Settings of ``tenant_id``; ``db`` must be an internal session when given."""
    if db is not None:
        return _of(await _row(db))
    async with tenant_session(tenant_id) as idb:
        return _of(await _row(idb))


async def update(
    db: AsyncSession, tenant_id: uuid.UUID, changes: dict[str, Any], updated_by: uuid.UUID | None
) -> Settings:
    row = await _row(db)
    if row is None:
        row = PortalCopilotSettings(id=uuid.uuid4(), tenant_id=tenant_id)
        db.add(row)
    for key, value in changes.items():
        setattr(row, key, value)
    row.updated_by = updated_by
    await db.flush()
    await db.refresh(row)
    return _of(row)


__all__ = ["FIELDS", "Settings", "get", "update"]
