"""Shared helpers of the placements domain (Epic 19)."""

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound
from app.core.fiscal import today_for
from app.models.placements import Placement, PlacementSettings

DEFAULT_WORKER_TYPES: dict[str, str | None] = {
    "contract": "employee",
    "contract_to_hire": "employee",
    "c2h": "employee",
    "freelance": "contractor",
    "permanent": None,
}
DEFAULT_DROPOUT_RULES: dict[str, Any] = {
    "no_response_touchpoints": 2,
    "onboarding_pct_at_t7": 50,
    "counter_offer": True,
}
DEFAULT_TOUCHPOINT_TEMPLATE = (
    "Hello {{ first_name }},\n\n"
    "Your start date with {{ client_name }} as {{ role_title }} is {{ start_date }}. "
    "Please confirm you are joining (or ask us a question) here:\n{{ link }}\n\n"
    "-- {{ tenant_name }}"
)
PRE_START = ("pre_joining", "joining_confirmed")
COMPENSATION_EXTRA = frozenset(
    {"vendor_rate", "burden_pct", "vendor_burden_pct", "margin_amount", "margin_pct", "rate_check"}
)


def now() -> datetime:
    return datetime.now(UTC)


def as_uuid(value: Any) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def not_found() -> NotFound:
    return NotFound("Placement not found")


async def get_placement(db: AsyncSession, placement_id: uuid.UUID, *, lock: bool = False) -> Placement:
    stmt = select(Placement).where(Placement.id == placement_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise not_found()
    return row


async def settings_row(db: AsyncSession, tenant_id: uuid.UUID) -> PlacementSettings:
    row = await db.get(PlacementSettings, tenant_id)
    if row is None:
        row = PlacementSettings(
            tenant_id=tenant_id,
            human_id_prefix="PLC",
            touchpoint_offsets=[14, 7, 3, 1],
            gate_alert_offset_days=3,
            ending_soon_days=30,
            worker_type_by_employment_type={"contract": "employee", "c2h": "employee", "permanent": None},
            dropout_rules=dict(DEFAULT_DROPOUT_RULES),
            touchpoint_email_template=None,
        )
        db.add(row)
        await db.flush()
    return row


async def le_timezone(db: AsyncSession, legal_entity_id: uuid.UUID) -> str:
    from app.models.legal_entities import LegalEntity

    tz = (
        await db.execute(select(LegalEntity.timezone).where(LegalEntity.id == legal_entity_id))
    ).scalar_one_or_none()
    return tz or "Asia/Kolkata"


async def local_today(db: AsyncSession, placement: Placement) -> date:
    return today_for(await le_timezone(db, placement.legal_entity_id))


def stale() -> ConflictError:
    return ConflictError("The placement was changed by someone else; reload and retry", code="stale")


def tenant_of(ctx: RequestContext, db: AsyncSession) -> uuid.UUID:
    if ctx.tenant_id is not None:
        return ctx.tenant_id
    return uuid.UUID(str(db.sync_session.info["tenant_id"]))


async def names(
    db: AsyncSession,
    *,
    candidate_ids: set[uuid.UUID] = frozenset(),  # type: ignore[assignment]
    client_ids: set[uuid.UUID] = frozenset(),  # type: ignore[assignment]
) -> tuple[dict[uuid.UUID, str], dict[uuid.UUID, str]]:
    from app.models.candidates import Candidate
    from app.models.clients import Client

    cands: dict[uuid.UUID, str] = {}
    clients: dict[uuid.UUID, str] = {}
    if candidate_ids:
        for cid, name in (
            await db.execute(select(Candidate.id, Candidate.full_name).where(Candidate.id.in_(candidate_ids)))
        ).all():
            cands[cid] = name
    if client_ids:
        for cid, name in (
            await db.execute(select(Client.id, Client.name).where(Client.id.in_(client_ids)))
        ).all():
            clients[cid] = name
    return cands, clients
