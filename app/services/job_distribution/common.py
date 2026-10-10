# ruff: noqa: E501, D401
"""Shared helpers of the job distribution domain (Epic 70)."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound
from app.core.integrations import find_manifest
from app.core.permissions import Principal
from app.models.connectors import ProviderConnectorConfig
from app.models.job_distribution import BoardPosting, JobBoardSettings
from app.models.oauth import AppInstallation

PUBLISH_PERMISSION = "posting:publish_external"
RUNNABLE = ("active", "degraded", "reconsent_required")
BOARD_LABELS = {
    "naukri": "Naukri", "linkedin": "LinkedIn", "indeed": "Indeed", "job_feed": "XML feed",
    "fake_job_board": "Demo board", "manual": "Manual",
}  # fmt: skip
CLOSED_STATUSES = ("closed", "expired", "abandoned")
OPEN_STATUSES = ("draft", "publishing", "live", "expiring", "failed", "rejected")


def now() -> datetime:
    return datetime.now(UTC)


def tenant_of(db: AsyncSession) -> uuid.UUID:
    return uuid.UUID(str(db.sync_session.info["tenant_id"]))


def board_label(key: str) -> str:
    return BOARD_LABELS.get(key, key.replace("_", " ").title())


def user_id_of(ctx: RequestContext) -> uuid.UUID | None:
    p = ctx.principal
    if isinstance(p, Principal):
        return p.acting_user_id if p.type == "app" else (p.id if p.type == "user" else None)
    return None


async def board_installation(db: AsyncSession, board_key: str) -> tuple[AppInstallation, ProviderConnectorConfig] | None:
    """The tenant's runnable Installation of ``board_key`` and its active config (``slot_key = board_key``)."""
    if find_manifest(board_key) is None:
        return None
    inst = (
        await db.execute(
            select(AppInstallation)
            .where(
                AppInstallation.kind == "connector",
                AppInstallation.connector_key == board_key,
                AppInstallation.status.in_(RUNNABLE),
            )
            .order_by(AppInstallation.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    if inst is None:
        return None
    cfg = (
        await db.execute(
            select(ProviderConnectorConfig)
            .where(ProviderConnectorConfig.installation_id == inst.id, ProviderConnectorConfig.status == "active")
            .order_by(ProviderConnectorConfig.legal_entity_id.is_not(None), ProviderConnectorConfig.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    return (inst, cfg) if cfg is not None else None


async def installed_boards(db: AsyncSession) -> list[tuple[AppInstallation, ProviderConnectorConfig]]:
    rows = (
        await db.execute(
            select(AppInstallation).where(AppInstallation.kind == "connector", AppInstallation.status.in_(RUNNABLE))
        )
    ).scalars()
    out = []
    for inst in rows:
        m = find_manifest(str(inst.connector_key or ""))
        if m is None or m.category != "job_board":
            continue
        pair = await board_installation(db, str(inst.connector_key))
        if pair is not None and pair[0].id == inst.id:
            out.append(pair)
    return out


def config_mode(cfg: ProviderConnectorConfig | None) -> str | None:
    return str((cfg.settings or {}).get("mode")) if cfg is not None and (cfg.settings or {}).get("mode") else None


async def get_board_posting(db: AsyncSession, board_posting_id: uuid.UUID, *, for_update: bool = False) -> BoardPosting:
    stmt = select(BoardPosting).where(BoardPosting.id == board_posting_id)
    if for_update:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Board posting not found", code="board_posting_not_found")
    return row


async def board_settings(db: AsyncSession, *, create: bool = True) -> JobBoardSettings:
    row = (await db.execute(select(JobBoardSettings))).scalar_one_or_none()
    if row is None and create:
        row = JobBoardSettings(id=uuid.uuid4(), tenant_id=tenant_of(db), expiry_warning_days=3, applicant_digest=True, auto_close_on_demand_change=True)
        db.add(row)
        await db.flush()
    if row is None:  # pragma: no cover - create=False only
        msg = "settings missing"
        raise ConflictError(msg)
    return row


def safe_message(value: Any, limit: int = 500) -> str:
    from app.services.connectors.job_board.live_base import sanitise_message

    return sanitise_message(value, limit)
