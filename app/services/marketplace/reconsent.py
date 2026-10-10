# ruff: noqa: E501, D401
"""Re-consent (Epic 69, spec A4 "Re-consent", FR-355): when a connector's manifest changes, every Installation whose
consented ``manifest_sha256`` differs is diffed (``core.integrations.manifest_diff`` against the consented snapshot in
``marketplace_listing_versions``). A widening diff -> ``pending_manifest_version`` + ``reconsent_required`` (system;
notification ``installation_reconsent_required``) and the adapters keep the consented egress hosts
(``consented_hosts``, wired into the connector registry). A non-widening diff applies silently (new grant row with the
same scopes)."""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.events import emit
from app.core.integrations import find_manifest, manifest_diff, manifest_sha256
from app.events.ecosystem import InstallationChanged
from app.models.oauth import App, AppGrant, AppInstallation
from app.schemas.marketplace import DataFlowLineOut, UpgradeDiffOut
from app.services.marketplace import platform_catalog

LIVE = ("pending_consent", "pending_approval", "active", "degraded", "reconsent_required", "suspended")


async def apply_manifest_changes(ctx: RequestContext, db: AsyncSession) -> int:  # noqa: ARG001
    """Tenant pass after ``platform_catalog.sync``; returns the number of Installations marked ``reconsent_required``."""
    rows = list(
        (
            await db.execute(
                select(AppInstallation).where(
                    AppInstallation.kind == "connector", AppInstallation.status.in_(LIVE)
                )
            )
        ).scalars()
    )
    marked = 0
    for inst in rows:
        current = find_manifest(str(inst.connector_key))
        if current is None:
            continue
        digest = manifest_sha256(current)
        if digest == inst.manifest_sha256:
            continue
        old = await platform_catalog.consented_manifest(current, inst.manifest_sha256)
        diff = manifest_diff(old, current) if old is not None else None
        if diff is None or diff.requires_reconsent:
            if inst.pending_manifest_version == current.version and inst.status == "reconsent_required":
                continue
            inst.pending_manifest_version = current.version
            if inst.status in ("active", "degraded", "pending_consent"):
                inst.status = "reconsent_required"
            marked += 1
            audit.record(
                "marketplace.reconsent_required",
                "app_installation",
                inst.id,
                session=db,
                manifest_version=current.version,
            )
            emit(db, InstallationChanged(
                tenant_id=inst.tenant_id, installation_id=inst.id, kind=inst.kind, app_id=None,
                connector_key=inst.connector_key, change="reconsent_required", status=inst.status, actor_kind="system",
            ))  # fmt: skip
        else:
            inst.manifest_version, inst.manifest_sha256, inst.pending_manifest_version = (
                current.version,
                digest,
                None,
            )
            grant = (
                await db.execute(
                    select(AppGrant).where(
                        AppGrant.installation_id == inst.id, AppGrant.superseded_at.is_(None)
                    )
                )
            ).scalar_one_or_none()
            if grant is not None:
                from datetime import UTC, datetime

                now = datetime.now(UTC)
                grant.superseded_at = now
                db.add(
                    AppGrant(
                        id=uuid.uuid4(), tenant_id=inst.tenant_id, installation_id=inst.id, manifest_version=current.version,
                        manifest_sha256=digest, scopes=list(grant.scopes), sensitive_purposes=dict(grant.sensitive_purposes or {}),
                        data_flows=[platform_catalog.flow_dict(f) for f in current.data_flows], granted_by=grant.granted_by,
                        granted_at=now,
                    )
                )  # fmt: skip
    await db.flush()
    return marked


async def consented_hosts(db: AsyncSession, installation_id: uuid.UUID, manifest: Any) -> list[str] | None:
    """The consented egress hosts when the Installation consented to an older manifest (None = use the current)."""
    inst = await db.get(AppInstallation, installation_id)
    if inst is None or inst.manifest_sha256 == manifest_sha256(manifest):
        return None
    old = await platform_catalog.consented_manifest(manifest, inst.manifest_sha256)
    return list(old.egress_hosts) if old is not None else []


def _flows(flows: Any) -> list[DataFlowLineOut]:
    out: list[DataFlowLineOut] = []
    for f in flows:
        d = platform_catalog.flow_dict(f) if not isinstance(f, dict) else f
        out.append(DataFlowLineOut(
            direction=str(d.get("direction")), entity=str(d.get("entity")), fields=list(d.get("fields") or []),
            purpose=str(d.get("purpose")), destination_country=str(d.get("destination_country") or "IN"),
            personal_data=bool(d.get("personal_data", True)),
        ))  # fmt: skip
    return out


async def upgrade_diff(ctx: RequestContext, db: AsyncSession, installation_id: uuid.UUID) -> UpgradeDiffOut:  # noqa: ARG001
    from app.core.database import platform_session
    from app.core.errors import NotFound

    inst = await db.get(AppInstallation, installation_id)
    if inst is None:
        raise NotFound("Installation not found")
    if inst.kind == "app":
        async with platform_session() as pdb:
            app = await pdb.get(App, inst.app_id)
        if app is None:
            raise NotFound("App not found")
        grant = (
            await db.execute(
                select(AppGrant).where(AppGrant.installation_id == inst.id, AppGrant.superseded_at.is_(None))
            )
        ).scalar_one_or_none()
        old_flows = {
            (f.get("direction"), f.get("entity"), f.get("purpose"))
            for f in (grant.data_flows if grant else []) or []
            if isinstance(f, dict)
        }
        new_flows = [
            f
            for f in (app.manifest or {}).get("data_flows", [])
            if (f.get("direction"), f.get("entity"), f.get("purpose")) not in old_flows
        ]
        added_scopes = sorted(set(app.requested_scopes) - set(inst.granted_scopes or []))
        return UpgradeDiffOut(
            installation_id=inst.id, from_version=inst.manifest_version, to_version=app.manifest_version,
            manifest_sha256=app.manifest_sha256, added_scopes=added_scopes, added_flows=_flows(new_flows),
            requires_reconsent=app.manifest_sha256 != inst.manifest_sha256,
        )  # fmt: skip
    current = find_manifest(str(inst.connector_key))
    if current is None:
        raise NotFound("Connector not found")
    digest = manifest_sha256(current)
    old = await platform_catalog.consented_manifest(current, inst.manifest_sha256)
    if old is None or digest == inst.manifest_sha256:
        return UpgradeDiffOut(
            installation_id=inst.id,
            from_version=inst.manifest_version,
            to_version=current.version,
            manifest_sha256=digest,
            requires_reconsent=old is None,
        )
    diff = manifest_diff(old, current)
    return UpgradeDiffOut(
        installation_id=inst.id, from_version=inst.manifest_version, to_version=current.version, manifest_sha256=digest,
        added_scopes=list(diff.added_scopes), added_flows=_flows(diff.added_flows), added_hosts=list(diff.added_hosts),
        requires_reconsent=diff.requires_reconsent,
    )  # fmt: skip


__all__ = ["apply_manifest_changes", "consented_hosts", "upgrade_diff"]
