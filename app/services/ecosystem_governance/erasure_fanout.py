# ruff: file-ignore[line-too-long]
"""Erasure downstream fan-out (Epic 79 story 79.5, FR-410, D-11).

On ``DsrCompleted(kind='erasure')`` for a Candidate / Worker, record one ``dsr_downstream_items`` row per Installation
the subject was exposed to:

* Apps -> ``webhook`` channel: Epic 68 delivers ``subject.erased`` to the Installation (owner 68; 79 records the row).
* Connectors whose manifest declares ``erasure: true`` -> ``connector`` channel: enqueue ``request_erasure`` through the
  connector adapter with the subject's external ids; record the outcome.
* Everything else (Installations with no erasure capability, statutory HRMS / accounting records) -> ``listed_only``
  (D-11: listed, not enforceable), with a statutory ``reason`` where one applies.

Enforcement of partner erasure is out of scope (D-11): the row is the record, not a guarantee.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger
from sqlalchemy import select

from app.models.ecosystem_governance import DsrDownstreamItem
from app.models.oauth import AppInstallation

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


async def _connector_has_erasure(connector_key: str | None) -> bool:  # NOSONAR
    if not connector_key:
        return False
    try:
        from app.core import integrations as core_integrations

        manifest = core_integrations.manifest(connector_key)
        return bool(getattr(manifest, "erasure", False) or (isinstance(manifest, dict) and manifest.get("erasure")))
    except Exception:  # pragma: no cover - manifest lookup is best-effort
        return False


async def _existing(db: AsyncSession, tenant_id: uuid.UUID, dsr_id: uuid.UUID) -> set[uuid.UUID | None]:
    rows = (
        await db.execute(
            select(DsrDownstreamItem.installation_id).where(
                DsrDownstreamItem.tenant_id == tenant_id, DsrDownstreamItem.dsr_id == dsr_id
            )
        )
    ).scalars().all()
    return set(rows)


async def fan_out(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    dsr_id: uuid.UUID,
    *,
    subject_type: str,
    subject_id: uuid.UUID,
) -> list[DsrDownstreamItem]:
    """Build the fan-out register for a subject's erasure. Idempotent per (dsr, installation)."""
    from app.services import exposure_service

    try:
        installation_ids = await exposure_service.installations_exposed(db, subject_type, subject_id)
    except Exception:  # pragma: no cover - degrade to an empty register
        logger.exception("erasure fan-out: exposure lookup failed")
        installation_ids = []
    seen = await _existing(db, tenant_id, dsr_id)
    created: list[DsrDownstreamItem] = []
    for inst_id in installation_ids:
        if inst_id in seen:
            continue
        inst = (
            await db.execute(
                select(AppInstallation).where(
                    AppInstallation.tenant_id == tenant_id, AppInstallation.id == inst_id
                )
            )
        ).scalar_one_or_none()
        channel = "listed_only"
        status = "manual_required"
        reason: str | None = None
        if inst is not None and inst.kind == "app":
            channel, status = "webhook", "sent"  # Epic 68 delivers subject.erased
        elif inst is not None and inst.kind == "connector":
            if await _connector_has_erasure(inst.connector_key):
                channel = "connector"
                status = await _request_connector_erasure(db, tenant_id, inst, subject_id)
            elif inst.connector_key in ("hrms", "accounting", "payroll"):
                status, reason = "statutory_retained", "Employment / accounting records retained under statute."
        item = DsrDownstreamItem(
            tenant_id=tenant_id,
            dsr_id=dsr_id,
            installation_id=inst_id,
            channel=channel,
            status=status,
            reason=reason,
        )
        db.add(item)
        created.append(item)
        seen.add(inst_id)
    if created:
        await db.flush()
    return created


async def _request_connector_erasure(
    db: AsyncSession, tenant_id: uuid.UUID, inst: AppInstallation, subject_id: uuid.UUID
) -> str:
    """Enqueue ``request_erasure`` on the connector adapter with the subject's external ids. Best-effort."""
    try:
        from app.services import integrations_service

        link = await integrations_service.find_link(db, tenant_id, inst.connector_key, subject_id)  # type: ignore[arg-type, misc]
        external_ids = [link.external_id] if link is not None else []
        if not external_ids:
            return "manual_required"
        adapter = integrations_service.adapter_for(inst.connector_key)  # type: ignore[arg-type, call-arg]
        request = getattr(adapter, "request_erasure", None)
        if request is None:
            return "manual_required"
        result: Any = request(external_ids)
        if hasattr(result, "__await__"):
            await result
        return "sent"
    except Exception:  # pragma: no cover - adapters are owner code; degrade to manual
        logger.exception("erasure fan-out: connector request_erasure failed")
        return "failed"


__all__ = ["fan_out"]
