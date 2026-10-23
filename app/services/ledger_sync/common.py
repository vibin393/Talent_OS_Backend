# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Shared helpers of the ledger sync domain (Epic 71): settings, the active accounting Installation / config of a Legal
Entity, sanitised messages and fix-it Tasks (``ledger_sync`` and ``hrms_sync`` are independent siblings in ``p5-dag``,
so these helpers are kept per package). Adapters are reached only through ``integrations_service.adapter_for``."""

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.integrations import find_manifest
from app.models.connectors import ProviderConnectorConfig
from app.models.ledger_sync import LedgerSettings
from app.models.oauth import AppInstallation

__all__ = [
    "RUNNABLE",
    "EntityInstall",
    "category_installation",
    "entity_install",
    "get_settings_row",
    "now",
    "sanitise",
    "system_ctx",
    "tenant_of",
]

RUNNABLE = ("active", "degraded", "reconsent_required")
_SECRETISH = re.compile(r"(?i)(bearer|token|secret|password|api[_-]?key)\S*")
_DIGITS = re.compile(r"\b\d{9,18}\b")
_PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")


def now() -> datetime:
    return datetime.now(UTC)


def tenant_of(db: AsyncSession) -> uuid.UUID:
    return uuid.UUID(str(db.sync_session.info["tenant_id"]))


def system_ctx(db: AsyncSession) -> RequestContext:
    return RequestContext(tenant_id=tenant_of(db), actor_kind="system")


def sanitise(message: str | None) -> str | None:
    """Provider messages without secrets, PANs or long numbers (account / Aadhaar), at most 500 characters."""
    if not message:
        return None
    text = _SECRETISH.sub("[redacted]", str(message))
    text = _PAN.sub("[redacted]", text)
    text = _DIGITS.sub("[redacted]", text)
    return text[:500]


async def get_settings_row(db: AsyncSession) -> LedgerSettings:
    row = (await db.execute(select(LedgerSettings))).scalar_one_or_none()
    if row is None:
        await db.execute(pg_insert(LedgerSettings).values(id=uuid.uuid4(), tenant_id=tenant_of(db)).on_conflict_do_nothing(constraint="uq_ledger_settings_tenant_id"))
        row = (await db.execute(select(LedgerSettings))).scalar_one()
    return row


class EntityInstall:
    """The active Installation of a category for a Legal Entity and the config serving it."""

    def __init__(self, installation: AppInstallation, config: ProviderConnectorConfig) -> None:
        self.installation = installation
        self.config = config

    @property
    def installation_id(self) -> uuid.UUID:
        return self.installation.id

    @property
    def config_id(self) -> uuid.UUID:
        return self.config.id

    @property
    def connector_key(self) -> str:
        return str(self.installation.connector_key)


async def category_installation(db: AsyncSession, category: str, legal_entity_id: uuid.UUID) -> EntityInstall | None:
    """The runnable connector Installation of ``category`` serving ``legal_entity_id`` (entity config first, then the
    tenant default config), or ``None``."""
    rows = (
        await db.execute(
            select(AppInstallation, ProviderConnectorConfig)
            .join(ProviderConnectorConfig, ProviderConnectorConfig.installation_id == AppInstallation.id)
            .where(
                AppInstallation.kind == "connector",
                AppInstallation.status.in_(RUNNABLE),
                ProviderConnectorConfig.status == "active",
                ProviderConnectorConfig.category == category,
            )
            .order_by(ProviderConnectorConfig.legal_entity_id.is_(None), AppInstallation.created_at)
        )
    ).all()
    fallback: EntityInstall | None = None
    for inst, cfg in rows:
        m = find_manifest(inst.connector_key)
        if m is None or m.category != category:
            continue
        if cfg.legal_entity_id == legal_entity_id:
            return EntityInstall(inst, cfg)
        if cfg.legal_entity_id is None and fallback is None:
            fallback = EntityInstall(inst, cfg)
    return fallback


async def entity_install(db: AsyncSession, installation_id: uuid.UUID, legal_entity_id: uuid.UUID) -> EntityInstall | None:
    """The config of ``installation_id`` serving ``legal_entity_id`` (entity config, else the default)."""
    inst = await db.get(AppInstallation, installation_id)
    if inst is None or inst.kind != "connector":
        return None
    rows = list(
        (
            await db.execute(
                select(ProviderConnectorConfig).where(
                    ProviderConnectorConfig.installation_id == installation_id, ProviderConnectorConfig.status == "active"
                )
            )
        ).scalars()
    )
    match = next((c for c in rows if c.legal_entity_id == legal_entity_id), None) or next((c for c in rows if c.legal_entity_id is None), None)
    return EntityInstall(inst, match) if match is not None else None


async def require_installation(db: AsyncSession, installation_id: uuid.UUID, category: str) -> AppInstallation:
    inst = await db.get(AppInstallation, installation_id)
    m = find_manifest(inst.connector_key) if inst is not None and inst.kind == "connector" else None
    if inst is None or m is None or m.category != category or inst.status == "uninstalled":
        raise NotFound("Installation not found")
    return inst


def manifest_category(inst: Any) -> str | None:
    m = find_manifest(getattr(inst, "connector_key", None))
    return m.category if m is not None else None


async def fix_it_task(
    db: AsyncSession,
    *,
    owner_id: uuid.UUID | None,
    entity_type: str,
    entity_id: uuid.UUID,
    title: str,
    notes: str | None = None,
    source: str = "collections",
    fallback_permission: str = "ledger:sync",
) -> uuid.UUID | None:
    """Create (or reuse the open) system Task on ``(entity_type, entity_id)`` with ``title``. The owner defaults to
    the first active holder of ``fallback_permission``. Returns the Task id (``None`` when nobody can own it)."""
    from app.models.tasks import Task
    from app.schemas.tasks import TaskCreate
    from app.services import tasks_service

    existing = (
        await db.execute(
            select(Task.id).where(Task.entity_type == entity_type, Task.entity_id == entity_id, Task.status == "open", Task.title == title[:200]).limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    if owner_id is None:
        from app.services.identity_bridge import users_with_permission

        holders = sorted(await users_with_permission(db, fallback_permission), key=lambda u: str(u["id"]))
        owner_id = holders[0]["id"] if holders else None
    if owner_id is None:
        return None
    data = TaskCreate.model_construct(
        title=title[:200], notes=notes, owner_id=owner_id, entity_type=entity_type, entity_id=entity_id, due_at=None,  # type: ignore[arg-type]
        due_all_day=False, priority="normal", reminder_at=None,
    )  # P5 Task entity types (500_01) are outside the P1 request Literal
    async with db.begin_nested():
        out = await tasks_service.create_system_task(system_ctx(db), db, data, source=source)
    return out.id


def notify(tenant_id: uuid.UUID, type_key: str, payload: dict[str, Any]) -> None:
    """Queue a notification fan-out (never blocks the sync path)."""
    from loguru import logger

    from app.tasks import notifications as notification_tasks
    from app.tasks.runtime import enqueue

    try:
        enqueue(notification_tasks.fan_out, str(tenant_id), type_key, payload)
    except Exception:  # pragma: no cover - logged
        logger.exception("Notification {} failed", type_key)
