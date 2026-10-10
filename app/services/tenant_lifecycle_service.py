"""Tenant lifecycle (Epic 12 story 12.6, FR-70): suspend, reactivate, offboard, export and delete.

``TENANT_TRANSITIONS`` (actor ``platform``, reason >= 10 characters):

| From                  | To          | Note                                                          |
|-----------------------|-------------|---------------------------------------------------------------|
| active                | suspended   | sessions revoked; sign-in / API keys 403 ``tenant_suspended``    |
| suspended             | active      |                                                               |
| active / suspended    | offboarding | export queued; only tenant admins may sign in, read-only       |
| offboarding           | active      | cancel, allowed before ``delete_after``                        |
| offboarding           | deleted     | **system only**, after ``delete_after`` once the export is ready |

Deletion runs on ``DATABASE_OWNER_URL`` (``talent_app`` may not delete audit rows): tenant rows in reverse-FK
order (incl. ``audit_logs`` / ``subject_keys``), then the ``t/<tid>/`` storage prefix, then a
``tenant_deletion_certificates`` row emailed as written confirmation.
"""

import hashlib
import io
import json
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core import audit, secure_links, storage
from app.core.context import RequestContext
from app.core.database import platform_session, tenant_session
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.core.mailer import EmailMessage, send_email
from app.core.models import Base
from app.core.settings import get_settings
from app.core.tenancy import MIXED_TABLES, PLATFORM_TABLES, requires_rls
from app.models.identity import Tenant
from app.models.platform import TenantDeletionCertificate, TenantExport
from app.schemas.platform import TenantExportOut, TenantLifecycleOut
from app.services import identity_bridge
from app.services.privacy.common import email_users

TENANT_TRANSITIONS: dict[tuple[str, str], frozenset[str]] = {
    ("active", "suspended"): frozenset({"platform"}),
    ("suspended", "active"): frozenset({"platform"}),
    ("active", "offboarding"): frozenset({"platform"}),
    ("suspended", "offboarding"): frozenset({"platform"}),
    ("offboarding", "active"): frozenset({"platform"}),
    ("offboarding", "deleted"): frozenset({"system"}),
}
EXPORT_LINK_DAYS = 30
EXPORT_LINK_USES = 5
MIN_REASON = 10


class TenantExportRequested:  # noqa: B903 - slotted event, same shape as the other domain events
    """Event: an offboarding export was queued (the handler enqueues ``export_tenant``)."""

    __slots__ = ("export_id", "tenant_id")

    def __init__(self, tenant_id: uuid.UUID, export_id: uuid.UUID) -> None:
        self.tenant_id = tenant_id
        self.export_id = export_id


def _now() -> datetime:
    return datetime.now(UTC)


async def lifecycle_out(db: AsyncSession, tenant: Tenant) -> TenantLifecycleOut:
    exports = (
        await db.execute(
            select(TenantExport)
            .where(TenantExport.tenant_id == tenant.id)
            .order_by(TenantExport.created_at.desc())
        )
    ).scalars()
    return TenantLifecycleOut(
        id=tenant.id,
        name=tenant.name,
        slug=tenant.slug,
        status=tenant.status,
        status_reason=tenant.status_reason,
        suspended_at=tenant.suspended_at,
        offboard_requested_at=tenant.offboard_requested_at,
        delete_after=tenant.delete_after,
        offboard_contact_email=tenant.offboard_contact_email,
        deleted_at=tenant.deleted_at,
        exports=[TenantExportOut.model_validate(e) for e in exports],
    )


def _action(old: str, to: str) -> str:
    if to == "suspended":
        return "tenant.suspended"
    if to == "offboarding":
        return "tenant.offboarding_started"
    return "tenant.reactivated" if old == "suspended" else "tenant.offboarding_cancelled"


def _operator(ctx: RequestContext) -> uuid.UUID | None:
    return getattr(ctx.principal, "id", None)


async def transition(
    ctx: RequestContext,
    tenant_id: uuid.UUID,
    to: str,
    reason: str,
    *,
    actor: str = "platform",
    contact_email: str | None = None,
    confirm_slug: str | None = None,
) -> TenantLifecycleOut:
    if len((reason or "").strip()) < MIN_REASON:
        raise ValidationFailed(
            "A reason of at least 10 characters is required",
            details={"fields": {"reason": "At least 10 characters"}},
        )
    operator_id = _operator(ctx) if actor == "platform" else None
    export_id: uuid.UUID | None = None
    async with platform_session() as pdb:
        tenant = (
            await pdb.execute(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
        ).scalar_one_or_none()
        if tenant is None:
            raise NotFound("Tenant not found")
        allowed = TENANT_TRANSITIONS.get((tenant.status, to))
        if allowed is None or actor not in allowed:
            raise ConflictError(
                f"Cannot move a {tenant.status} tenant to {to}",
                code="invalid_transition",
                details={"from": tenant.status, "to": to},
            )
        now = _now()
        old = tenant.status
        if to == "offboarding":
            if confirm_slug is not None and confirm_slug != tenant.slug:
                raise ValidationFailed(
                    "Type the tenant slug to confirm", details={"fields": {"confirm_slug": "Does not match"}}
                )
            tenant.offboard_requested_at = now
            tenant.delete_after = now + timedelta(days=get_settings().offboard_grace_days)
            tenant.offboard_contact_email = contact_email
            export = TenantExport(id=uuid.uuid4(), tenant_id=tenant.id, kind="offboarding", status="queued")
            pdb.add(export)
            export_id = export.id
        if old == "offboarding" and to == "active":
            if tenant.delete_after is not None and now >= tenant.delete_after:
                raise ConflictError("The grace period is over", code="grace_period_over")
            tenant.offboard_requested_at = None
            tenant.delete_after = None
        if to == "suspended":
            tenant.suspended_at = now
        if to == "active":
            tenant.suspended_at = None
        if to == "deleted":
            tenant.deleted_at = now
        tenant.status = to
        tenant.status_reason = reason.strip()
        if to in ("suspended", "offboarding", "deleted"):
            kinds = ("user", "candidate", "support") if to != "offboarding" else ("candidate", "support")
            await identity_bridge.revoke_tenant_sessions(pdb, tenant.id, kinds)
        await pdb.flush()
        out = await lifecycle_out(pdb, tenant)
    if to != "deleted":
        async with tenant_session(tenant_id) as db:
            audit.record(
                _action(old, to),
                "tenant",
                tenant_id,
                session=db,
                actor_type=actor,
                actor_id=operator_id,
                reason=reason.strip(),
                **{"from": old},
            )
            if export_id is not None:
                emit(db, TenantExportRequested(tenant_id=tenant_id, export_id=export_id))
    return out


# ================================================================ export
def _jsonable(value: Any) -> Any:
    return audit.jsonable(value)


async def export_tenant(tenant_id: uuid.UUID, export_id: uuid.UUID) -> str:
    """Dump every tenant-owned table as JSONL (audit PII decrypted) plus all files into
    ``platform/exports/<tid>/<id>.zip``; email a ``tenant_export`` link (30 days, 5 uses)."""
    async with platform_session() as pdb:
        export = await pdb.get(TenantExport, export_id)
        if export is None or export.status == "ready":
            return "skipped"
        export.status = "running"
    try:
        buf = io.BytesIO()
        files: list[tuple[str, str]] = []
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            async with tenant_session(tenant_id) as db:
                for table in Base.metadata.sorted_tables:
                    if not requires_rls(table.name) and table.name not in MIXED_TABLES:
                        continue
                    if "tenant_id" not in table.c:
                        continue
                    if table.name == "audit_logs":
                        rows = (
                            (await db.execute(select(audit.AuditLog).order_by(audit.AuditLog.occurred_at)))
                            .scalars()
                            .all()
                        )
                        revealed = await audit.reveal_rows(db, list(rows))
                        lines = [json.dumps(r.model_dump(mode="json"), ensure_ascii=False) for r in revealed]
                    else:
                        try:
                            async with db.begin_nested():
                                result = await db.execute(select(table))
                                rows = list(result)
                        except Exception:
                            # A model whose migration has not run in this database: skip, never fail the export.
                            logger.warning("Tenant export: table {} unavailable, skipped", table.name)
                            continue
                        lines = [
                            json.dumps(
                                {k: _jsonable(v) for k, v in row._mapping.items() if k != "text_tsv"},
                                ensure_ascii=False,
                            )
                            for row in rows
                        ]
                    zf.writestr(f"data/{table.name}.jsonl", "\n".join(lines) + ("\n" if lines else ""))
                    if table.name == "stored_files":
                        files = [
                            (str(r._mapping["storage_key"]), str(r._mapping["id"]))
                            for r in (await db.execute(select(table)))
                            if r._mapping.get("deleted_at") is None
                        ]
            backend = storage.get_storage()
            for key, file_id in files:
                try:
                    zf.writestr(f"files/{file_id}", await backend.get(key))
                except Exception:
                    logger.warning("Tenant export: file {} missing", file_id)
        content = buf.getvalue()
        key = f"platform/exports/{tenant_id}/{export_id}.zip"
        await storage.get_storage().put(key, content, "application/zip")
        digest = hashlib.sha256(content).hexdigest()
        async with tenant_session(tenant_id) as db:
            audit.set_actor(db, "system", None)
            _link, token = await secure_links.issue(
                db,
                tenant_id=tenant_id,
                purpose="tenant_export",
                subject_type="tenant",
                subject_id=tenant_id,
                ttl=timedelta(days=EXPORT_LINK_DAYS),
                max_uses=EXPORT_LINK_USES,
                metadata={"storage_key": key, "filename": f"tenant-export-{tenant_id}.zip"},
            )
            admins = await identity_bridge.users_with_permission(db, "__tenant_admin_only__")
            audit.record(
                "tenant.export_ready", "tenant", tenant_id, session=db, export_id=export_id, sha256=digest
            )
        async with platform_session() as pdb:
            export = await pdb.get(TenantExport, export_id)
            assert export is not None
            export.status = "ready"
            export.storage_key = key
            export.sha256 = digest
            export.size_bytes = len(content)
            export.expires_at = _now() + timedelta(days=EXPORT_LINK_DAYS)
            tenant = await pdb.get(Tenant, tenant_id)
            contact = tenant.offboard_contact_email if tenant is not None else None
        recipients = [*admins, *([{"email": contact}] if contact else [])]
        await email_users(
            recipients,
            "Your Talent OS data export is ready",
            f"The full export of your workspace data is ready (SHA-256 {digest}).\n\nDownload: "
            f"{secure_links.link_url(token)}\n\nThe link can be used {EXPORT_LINK_USES} times within {EXPORT_LINK_DAYS} days.\n",
            tenant_id=tenant_id,
            kind="tenant_export",
        )
        return "ready"
    except Exception:
        logger.exception("Tenant export {} failed", export_id)
        async with platform_session() as pdb:
            export = await pdb.get(TenantExport, export_id)
            if export is not None:
                export.status = "failed"
                export.error = "export_failed"
        return "failed"


# ================================================================ deletion (owner role)
def _owner_url() -> str:
    s = get_settings()
    return s.database_owner_url or s.owner_db_url()


def _deletable_tables() -> list[Any]:
    """Tables holding the tenant's rows, children first (reverse FK order)."""
    out = []
    for table in reversed(Base.metadata.sorted_tables):
        if table.name == "tenants" or "tenant_id" not in table.c:
            continue
        if table.name in ("tenant_deletion_certificates",):
            continue
        out.append(table)
    return out


async def delete_tenant_data(tenant_id: uuid.UUID) -> dict[str, int]:
    engine = create_async_engine(_owner_url(), poolclass=NullPool)
    counts: dict[str, int] = {}
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)}
            )
            existing = set(
                (
                    await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
                ).scalars()
            )
            for table in _deletable_tables():
                if table.name not in existing:
                    continue
                result = await conn.execute(table.delete().where(table.c.tenant_id == tenant_id))
                if result.rowcount:
                    counts[table.name] = int(result.rowcount)
    finally:
        await engine.dispose()
    return counts


async def process_deletions() -> list[uuid.UUID]:
    """Daily: delete tenants whose grace period is over and whose offboarding export is ready."""
    now = _now()
    async with platform_session() as pdb:
        due = [
            t
            for t in await identity_bridge.list_tenants(pdb, ("offboarding",))
            if t.delete_after and t.delete_after <= now
        ]
        ready: list[Tenant] = []
        for tenant in due:
            export = (
                await pdb.execute(
                    select(TenantExport).where(
                        TenantExport.tenant_id == tenant.id,
                        TenantExport.kind == "offboarding",
                        TenantExport.status == "ready",
                    )
                )
            ).first()
            if export is not None:
                ready.append(tenant)
        snapshots = []
        for t in ready:
            keys = (
                await pdb.execute(
                    select(TenantExport.storage_key).where(
                        TenantExport.tenant_id == t.id, TenantExport.storage_key.is_not(None)
                    )
                )
            ).scalars()
            snapshots.append((t.id, t.name, t.offboard_contact_email, [k for k in keys if k]))
    deleted: list[uuid.UUID] = []
    for tenant_id, name, contact, export_keys in snapshots:
        counts = await delete_tenant_data(tenant_id)
        files = await storage.delete_tenant_prefix(tenant_id)
        for key in export_keys:
            try:
                await storage.get_storage().delete(key)
                files += 1
            except Exception:
                logger.exception("Could not delete tenant export {}", key)
        at = _now()
        canonical = json.dumps(
            {
                "tenant_id": str(tenant_id),
                "tenant_name": name,
                "deleted_at": at.isoformat(),
                "row_counts": counts,
                "files_deleted": files,
            },
            sort_keys=True,
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        async with platform_session() as pdb:
            pdb.add(
                TenantDeletionCertificate(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    tenant_name=name,
                    deleted_at=at,
                    row_counts=counts,
                    files_deleted=files,
                    sha256=digest,
                )
            )
            tenant = await pdb.get(Tenant, tenant_id)
            if tenant is not None:
                tenant.status = "deleted"
                tenant.deleted_at = at
                tenant.status_reason = "Deleted after the offboarding grace period"
            audit.record(
                "tenant.deleted",
                "tenant",
                tenant_id,
                session=pdb,
                actor_type="system",
                actor_id=None,
                sha256=digest,
            )
        if contact:
            try:
                await send_email(
                    EmailMessage(
                        to=contact,
                        subject=f"Confirmation: {name} data deleted from Talent OS",
                        text=(
                            f"This confirms that all data of {name} was deleted from Talent OS on {at:%Y-%m-%d %H:%M} UTC.\n\n"
                            f"Rows deleted: {sum(counts.values())}\nFiles deleted: {files}\nCertificate SHA-256: {digest}\n"
                        ),
                        kind="tenant_deletion_certificate",
                    )
                )
            except Exception:
                logger.exception("Could not email the deletion certificate for tenant {}", tenant_id)
        deleted.append(tenant_id)
    return deleted


def platform_tables_with_tenant() -> list[str]:
    return sorted(
        t for t in PLATFORM_TABLES if t in Base.metadata.tables and "tenant_id" in Base.metadata.tables[t].c
    )
