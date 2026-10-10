# ruff: noqa: E501
"""Installation health (Epic 69, spec A4 "Health"): connection tests with a log row per check, the hourly sweep, the
3-consecutive-failures -> ``degraded`` rule and recovery. Health never changes ``suspended``, ``reconsent_required``,
``pending_approval`` or ``uninstalled`` Installations' status; ``pending_consent`` connectors are promoted to ``active``
only by the caller (install / the human "Test again" click)."""

import asyncio
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import DomainError
from app.core.events import emit
from app.core.integrations import ConnectorAuthError, ConnectorError, ConnectorRateLimited, find_manifest
from app.events.ecosystem import ConnectorHealthChanged
from app.models.connectors import ProviderConnectorConfig
from app.models.marketplace import InstallationHealthLog
from app.models.oauth import AppInstallation

TEST_TIMEOUT_SECONDS = 10
SWEEP_STALE = timedelta(hours=6)
DEGRADE_AFTER_FAILURES = 3
MESSAGES = {
    "auth_failed": "The provider rejected the credentials.",
    "timeout": "The provider did not answer within 10 seconds.",
    "rate_limited": "The provider is rate limiting requests. Try again in a minute.",
    "config_missing": "The integration has no active configuration.",
    "connector_config_invalid": "The configuration is invalid.",
}


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    ok: bool
    code: str | None
    message: str | None
    latency_ms: int


def _now() -> datetime:
    return datetime.now(UTC)


def _sanitise(text: str | None) -> str | None:
    if not text:
        return None
    return " ".join(str(text).split())[:300]


async def _probe(ctx: RequestContext, db: AsyncSession, inst: AppInstallation) -> ProbeOutcome:
    from app.services.connectors import registry as connector_registry

    started = time.monotonic()
    manifest = find_manifest(str(inst.connector_key))
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
    code: str | None = None
    message: str | None = None
    if manifest is None or config is None:
        code = "config_missing"
    else:
        try:
            adapter = await connector_registry.build_for(ctx, db, manifest.category, config)
            async with asyncio.timeout(TEST_TIMEOUT_SECONDS):
                await adapter.handle("health", {})
        except ConnectorAuthError:
            code = "auth_failed"
        except ConnectorRateLimited:
            code = "rate_limited"
        except TimeoutError:
            code = "timeout"
        except ConnectorError as exc:
            code, message = getattr(exc, "code", None) or "error", _sanitise(str(exc))
        except DomainError as exc:
            code = getattr(exc, "code", None) or "error"
        except (NotImplementedError, ValueError):  # adapters without a health op count as reachable
            code = None
        except Exception as exc:
            code, message = "error", _sanitise(type(exc).__name__)
    latency = int((time.monotonic() - started) * 1000)
    if code is None:
        return ProbeOutcome(True, None, None, latency)
    return ProbeOutcome(False, code, MESSAGES.get(code) or message or "The connection test failed.", latency)


async def log(
    db: AsyncSession,
    inst: AppInstallation,
    *,
    trigger: str,
    result: str,
    error_code: str | None,
    latency_ms: int | None,
) -> None:
    db.add(
        InstallationHealthLog(
            id=uuid.uuid4(), tenant_id=inst.tenant_id, installation_id=inst.id, checked_at=_now(), trigger=trigger,
            result=result, error_code=error_code, latency_ms=latency_ms,
        )
    )  # fmt: skip
    await db.flush()


def _health_changed(db: AsyncSession, inst: AppInstallation, before: str) -> None:
    if inst.health_status != before or inst.last_error_code == "auth_failed":
        emit(
            db,
            ConnectorHealthChanged(
                tenant_id=inst.tenant_id, installation_id=inst.id, health_status=inst.health_status,
                error_code=inst.last_error_code,
            ),
        )  # fmt: skip


async def run_test(
    ctx: RequestContext, db: AsyncSession, inst: AppInstallation, *, trigger: str
) -> ProbeOutcome:
    """Run the connection test, record it and apply health. Status changes here: ``active`` -> ``degraded`` on an
    auth failure, ``degraded`` -> ``active`` on a pass (system recovery)."""
    if inst.kind != "connector":
        return ProbeOutcome(True, None, None, 0)
    outcome = await _probe(ctx, db, inst)
    before = inst.health_status
    inst.health_checked_at = _now()
    if outcome.ok:
        inst.health_status, inst.last_error_code = "ok", None
        if inst.status == "degraded":
            inst.status = "active"
    else:
        inst.health_status = "failing"
        inst.last_error_code, inst.last_error_at = outcome.code, _now()
        if outcome.code == "auth_failed" and inst.status == "active":
            inst.status = "degraded"
    await log(
        db,
        inst,
        trigger=trigger,
        result="ok" if outcome.ok else "failing",
        error_code=outcome.code,
        latency_ms=outcome.latency_ms,
    )
    _health_changed(db, inst, before)
    await db.flush()
    return outcome


async def on_sync_failed(
    db: AsyncSession, installation_id: uuid.UUID, consecutive_failures: int, error_code: str | None
) -> bool:
    """``ConnectorSyncFailed`` with ``consecutive_failures >= 3`` -> log ``job_failures`` and ``degraded`` (system)."""
    if consecutive_failures < DEGRADE_AFTER_FAILURES:
        return False
    inst = await db.get(AppInstallation, installation_id)
    if inst is None or inst.status != "active":
        return False
    before = inst.health_status
    inst.status, inst.health_status = "degraded", "degraded"
    inst.last_error_code, inst.last_error_at = (error_code or "sync_failed")[:60], _now()
    await log(
        db, inst, trigger="job_failures", result="degraded", error_code=inst.last_error_code, latency_ms=None
    )
    _health_changed(db, inst, before)
    await db.flush()
    from app.services.marketplace import notify

    await notify.send(
        db,
        "installation_degraded",
        {"installation_id": str(inst.id), "name": inst.display_name, "error_code": inst.last_error_code},
    )
    return True


async def on_job_succeeded(db: AsyncSession, installation_id: uuid.UUID) -> bool:
    """Recovery: a ``degraded`` Installation (not an auth failure) whose last 3 finished jobs succeeded -> ``active``."""
    from app.models.connectors import ConnectorJob

    inst = await db.get(AppInstallation, installation_id)
    if inst is None or inst.status != "degraded" or inst.last_error_code == "auth_failed":
        return False
    recent = list(
        (
            await db.execute(
                select(ConnectorJob.status)
                .where(
                    ConnectorJob.installation_id == installation_id,
                    ConnectorJob.status.in_(("succeeded", "failed", "dead")),
                )
                .order_by(ConnectorJob.updated_at.desc())
                .limit(DEGRADE_AFTER_FAILURES)
            )
        ).scalars()
    )
    if len(recent) < DEGRADE_AFTER_FAILURES or any(s != "succeeded" for s in recent):
        return False
    before = inst.health_status
    inst.status, inst.health_status, inst.last_error_code = "active", "ok", None
    await log(db, inst, trigger="job_failures", result="ok", error_code=None, latency_ms=None)
    _health_changed(db, inst, before)
    await db.flush()
    return True


async def due_for_sweep(db: AsyncSession, now: datetime | None = None) -> list[AppInstallation]:
    cutoff = (now or _now()) - SWEEP_STALE
    rows = (
        await db.execute(
            select(AppInstallation).where(
                AppInstallation.kind == "connector",
                AppInstallation.status.in_(("active", "degraded")),
                (AppInstallation.health_checked_at.is_(None)) | (AppInstallation.health_checked_at < cutoff),
            )
        )
    ).scalars()
    return list(rows)


async def sweep(ctx: RequestContext, db: AsyncSession) -> int:
    """Beat ``marketplace.health_sweep`` (hourly): scheduled tests for stale active / degraded connectors."""
    count = 0
    for inst in await due_for_sweep(db):
        await run_test(ctx, db, inst, trigger="scheduled")
        count += 1
    return count


async def history(
    db: AsyncSession, installation_id: uuid.UUID, limit: int = 50
) -> list[InstallationHealthLog]:
    rows = (
        await db.execute(
            select(InstallationHealthLog)
            .where(InstallationHealthLog.installation_id == installation_id)
            .order_by(InstallationHealthLog.checked_at.desc())
            .limit(limit)
        )
    ).scalars()
    return list(rows)


__all__ = [
    "MESSAGES",
    "ProbeOutcome",
    "due_for_sweep",
    "history",
    "log",
    "on_job_succeeded",
    "on_sync_failed",
    "run_test",
    "sweep",
]
