# ruff: noqa: E501, D400, D415
"""Uninstall extras (Epic 69, spec A4 "uninstall" steps 2-4, FR-355 / FR-410):

* queued / failed ``connector_jobs`` of the Installation -> ``dead`` (``last_error='uninstalled'``);
* erasure propagation: **before** the secrets are deleted, one ``connector_jobs`` row ``request_erasure`` (key
  ``<connector_key>:request_erasure:installation:<id>:1``) with the external ids linked to candidates, workers and
  applicants runs once with the still-present credentials (10 s cap; the outcome is recorded on the job). Uninstall
  then completes whatever the job's outcome (uninstall always works).
* ``connector_links``, ``installation_health_log``, ``app_grants``, audit and job history are kept.
"""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.integrations import find_manifest
from app.models.connectors import ConnectorJob, ProviderConnectorConfig
from app.models.integrations import ConnectorLink
from app.models.oauth import AppInstallation

ERASURE_ENTITY_TYPES = ("candidate", "worker", "applicant")
ERASURE_TIMEOUT_SECONDS = 10


def _now() -> datetime:
    return datetime.now(UTC)


async def kill_jobs(
    db: AsyncSession,
    installation_id: uuid.UUID,
    *,
    reason: str = "uninstalled",
    config_id: uuid.UUID | None = None,
) -> int:
    stmt = update(ConnectorJob).where(
        ConnectorJob.installation_id == installation_id, ConnectorJob.status.in_(("queued", "failed"))
    )
    if config_id is not None:
        stmt = stmt.where(ConnectorJob.config_id == config_id)
    result = await db.execute(stmt.values(status="dead", last_error=reason, next_attempt_at=None))
    return int(getattr(result, "rowcount", 0) or 0)


def supports_erasure(inst: AppInstallation) -> bool:
    if inst.kind != "connector":
        return False
    manifest = find_manifest(str(inst.connector_key))
    return bool(manifest is not None and manifest.erasure)


async def request_erasure(
    ctx: RequestContext, db: AsyncSession, inst: AppInstallation
) -> ConnectorJob | None:
    """Run the one ``request_erasure`` job while the credentials still exist. Returns the job (None = not supported)."""
    from app.services.connectors import registry as connector_registry

    manifest = find_manifest(str(inst.connector_key))
    if manifest is None or not manifest.erasure:
        return None
    links = (
        await db.execute(
            select(ConnectorLink.entity_type, ConnectorLink.external_id).where(
                ConnectorLink.installation_id == inst.id, ConnectorLink.entity_type.in_(ERASURE_ENTITY_TYPES)
            )
        )
    ).all()
    external_ids: dict[str, list[str]] = {}
    for entity_type, external_id in links:
        external_ids.setdefault(entity_type, []).append(str(external_id))
    config = (
        await db.execute(
            select(ProviderConnectorConfig)
            .where(
                ProviderConnectorConfig.installation_id == inst.id, ProviderConnectorConfig.status == "active"
            )
            .order_by(ProviderConnectorConfig.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    key = f"{manifest.key}:request_erasure:installation:{inst.id}:1"
    job = (
        await db.execute(select(ConnectorJob).where(ConnectorJob.idempotency_key == key))
    ).scalar_one_or_none()
    if job is None:
        job = ConnectorJob(
            id=uuid.uuid4(), tenant_id=inst.tenant_id, category=manifest.category, op="request_erasure",
            subject_kind="installation", subject_id=inst.id, idempotency_key=key,
            config_id=config.id if config else None, status="running", attempts=1, max_attempts=1,
            payload={"external_ids": external_ids}, installation_id=inst.id,
        )  # fmt: skip
        db.add(job)
        await db.flush()
    payload: dict[str, Any] = {"external_ids": external_ids}
    try:
        if config is None:
            raise RuntimeError("no active configuration")
        adapter = await connector_registry.build_for(ctx, db, manifest.category, config)
        async with asyncio.timeout(ERASURE_TIMEOUT_SECONDS):
            result = await adapter.handle("request_erasure", payload)
        job.status, job.result, job.last_error = "succeeded", dict(result or {}), None
    except Exception as exc:
        job.status, job.last_error = "dead", f"request_erasure failed: {type(exc).__name__}"[:500]
    audit.record(
        "marketplace.erasure_requested", "app_installation", inst.id, session=db,
        job_id=str(job.id), status=job.status, counts={k: len(v) for k, v in external_ids.items()},
    )  # fmt: skip
    await db.flush()
    return job


__all__ = ["ERASURE_ENTITY_TYPES", "kill_jobs", "request_erasure", "supports_erasure"]
