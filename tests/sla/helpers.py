# ruff: noqa: E501
"""Shared helpers of the Epic 32 SLA / escalation tests."""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from typing import Any

from sqlalchemy import text

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.permissions import CATALOG, Principal, Scope
from app.services import directory_service
from app.services.sla.registry import SlaMilestoneDef, _milestones

API = "/api/v1"
SLA = f"{API}/sla"


@contextmanager
def milestone(subject_type: str, key: str, definition: SlaMilestoneDef) -> Iterator[None]:
    with _milestones.override((subject_type, key), definition):
        yield


async def admin_ctx(tenant_id: uuid.UUID, user_id: uuid.UUID) -> RequestContext:
    async with tenant_session(tenant_id) as db:
        role = await directory_service.role_id_by_key(db, "tenant_admin")
    principal = Principal(
        type="user",
        id=user_id,
        tenant_id=tenant_id,
        role_ids=frozenset({role}) if role else frozenset(),
        permissions=dict.fromkeys(CATALOG, Scope.TENANT),
    )
    return RequestContext(tenant_id=tenant_id, principal=principal)


async def set_work_week(tenant_id: uuid.UUID, holidays: list[date] | None = None) -> None:
    """Mon-Fri 09:00-18:00 in the tenant timezone (Asia/Kolkata) plus ``holidays``."""
    week = {d: {"start": "09:00", "end": "18:00"} for d in ("mon", "tue", "wed", "thu", "fri")}
    import json

    async with tenant_session(tenant_id) as db:
        await db.execute(
            text(
                "INSERT INTO demand_settings (tenant_id, work_week, holidays) VALUES (:t, CAST(:w AS jsonb), CAST(:h AS date[])) "
                "ON CONFLICT (tenant_id) DO UPDATE SET work_week = EXCLUDED.work_week, holidays = EXCLUDED.holidays"
            ),
            {"t": tenant_id, "w": json.dumps(week), "h": list(holidays or [])},
        )


async def scalar(tenant_id: uuid.UUID, sql: str, **params: Any) -> Any:
    async with tenant_session(tenant_id) as db:
        return (await db.execute(text(sql), params)).scalar()


async def rows(tenant_id: uuid.UUID, sql: str, **params: Any) -> list[Any]:
    async with tenant_session(tenant_id) as db:
        return list((await db.execute(text(sql), params)).all())
